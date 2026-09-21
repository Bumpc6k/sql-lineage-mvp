"""L2：分层链路生成（需求 → 多段 INSERT SQL + 链路图）。

思路：**能复用就别造**。
1. 需求关键词 → 知识库检索 / 模糊匹配，定位「目标表」（``target_layer`` 层）；
2. 从 ``warehouse_graph.json`` 取该表的**存量血缘**，逐层往上推主路径（``ads → dws → dwd → ods``），
   每层一段 ``INSERT OVERWRITE``；
3. 每段 SQL 的列与表达式**全部来自存量脚本的字段级血缘**（边上真实出现过的表达式，仅重映射别名），
   同层 / 维表上游作为该段的「链路外依赖」如实列出；
4. 目标表在知识库里没有对应表时（全新需求）退化为「新建表模式」：表名按命名规范推导、
   字段取上游真实列，并在 warnings 里逐条标注需人工确认。

每段都能被 L4 反向校验（过血缘引擎 → 与 ``warehouse_graph.json`` 逐边对比）。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from lineage_core.graph import table_layer
from lineage.generate.llm import GenerateLLM
from lineage.generate.spec import (
    DATE_PLACEHOLDER,
    Evidence,
    GraphIndex,
    KbView,
    assign_aliases,
    detect_alias,
    infer_join_key,
    layer_cn,
    layer_index,
    open_store,
    parse_check,
    rewrite_alias,
    short_name,
    split_table,
    step_ok,
    strip_alias_suffix,
)
from lineage.generate.sql_builder import ColumnSpec, JoinSpec, render_insert

__all__ = ["generate_pipeline", "format_pipeline_text"]

#: 需求里的动词 / 修饰词（定位目标表时先剥掉）
_VERB_RE = re.compile(
    r"^(请|帮我|帮忙|我想|我要|需要|要)?"
    r"(生成|做|建|建立|创建|产出|出|跑|开发|新增|新增一个|搞|写)"
    r"((一个|一张|个|张|份)?(报表|报告|看板|明细|汇总|表)?)?"
)
_NOISE_RE = re.compile(r"(一个|一张|一份|个|张|份|的|吧|请|帮我|报表|报告|看板)$")
#: 分层前缀（命名规范推导用）
_LAYER_PREFIX = {"src": "src", "ods": "ods", "dim": "dim", "dwd": "dwd", "dws": "dws", "ads": "ads"}
#: 链路展开的「同层折叠」最大跳数（防止环路 / 自引用）
MAX_COLLAPSE = 4


def _clean_requirement(text: str) -> str:
    out = _VERB_RE.sub("", (text or "").strip())
    out = _NOISE_RE.sub("", out).strip()
    return out or (text or "").strip()


def _keywords(text: str) -> List[str]:
    """需求 → 关键词（整串 + 按标点切分 + 去动词后的核心词）。"""
    raw = (text or "").strip()
    out: List[str] = []
    for piece in re.split(r"[，,、;；/\s]+", raw):
        piece = piece.strip()
        if piece and piece not in out:
            out.append(piece)
    cleaned = _clean_requirement(raw)
    if cleaned and cleaned not in out:
        out.insert(0, cleaned)
    # 中文 2~3 字连续片段（「产销存月报」-> 产销存 / 销存月 ...）作为兜底关键词
    for seg in re.findall(r"[\u4e00-\u9fff]{2,4}", cleaned):
        if seg not in out:
            out.append(seg)
    return out


# --------------------------------------------------------------------------- #
# 目标表定位
# --------------------------------------------------------------------------- #
def _candidate_tables(kb: KbView, graph: GraphIndex, layer: str, key: str) -> List[Dict[str, Any]]:
    """目标层的候选表（知识库为准，血缘图补充），带匹配得分。"""
    out: List[Dict[str, Any]] = []
    seen = set()
    for info in kb.table_candidates(layer):
        name = info.get("table_name") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"table": name, "chinese_name": info.get("chinese_name") or "",
                    "score": _score(name, info.get("chinese_name") or "", key), "source": "kb"})
    for name in graph.tables():
        if name in seen or graph.layer(name) != layer:
            continue
        seen.add(name)
        info = kb.table(name) or {}
        out.append({"table": name, "chinese_name": info.get("chinese_name") or "",
                    "score": _score(name, info.get("chinese_name") or "", key), "source": "graph"})
    out.sort(key=lambda x: (-x["score"], x["table"]))
    return out


def _score(table: str, chinese: str, key: str) -> float:
    """表名 / 中文名与需求的匹配分（越大越相关）。"""
    if not key:
        return 0.0
    short = short_name(table)
    score = 0.0
    if key == short or key == chinese:
        score += 10.0
    if key and (key in short or key in chinese):
        score += 6.0
    if key and (short in key or (chinese and chinese in key)):
        score += 5.0
    # 中文字序包含（「产销存月报」vs「产销存」）
    chars = [c for c in key if "\u4e00" <= c <= "\u9fff"]
    if chars:
        hit = sum(1 for c in chars if c in short or c in chinese)
        score += hit / len(chars) * 4.0
    return round(score, 3)


def find_target_table(kb: KbView, graph: GraphIndex, requirement: str, target_layer: str,
                      llm: GenerateLLM, override: str = "") -> Dict[str, Any]:
    """定位目标表：显式指定 > 知识库精确/模糊 > 血缘图 > 新建。"""
    out: Dict[str, Any] = {"matched": False, "table": "", "mode": "new", "candidates": [],
                          "matched_by": "", "score": 0.0}
    if override:
        table = kb.resolve(override)
        out.update({"matched": True, "table": table, "mode": "reuse",
                    "matched_by": "调用方指定 target_table", "score": 100.0})
        return out

    key = _clean_requirement(requirement)
    candidates = _candidate_tables(kb, graph, target_layer, key)
    out["candidates"] = candidates[:10]
    if candidates and candidates[0]["score"] >= 5.0:
        top = candidates[0]
        out.update({"matched": True, "table": top["table"], "mode": "reuse", "score": top["score"],
                    "matched_by": f"需求关键词「{key}」匹配表名/中文名（得分 {top['score']}）"})
        return out

    # 候选分不够时，交给可插拔 LLM 从候选清单里挑（编造的一律丢弃，见 llm.pick_tables）
    names = [c["table"] for c in candidates]
    picked = llm.pick_tables(requirement, names) if names else None
    if picked:
        out.update({"matched": True, "table": picked["tables"][0], "mode": "reuse", "score": 1.0,
                    "matched_by": f"LLM 从候选表中挑选（{picked.get('reason') or '未给理由'}）",
                    "llm_picked": picked["tables"]})
        return out
    return out


# --------------------------------------------------------------------------- #
# 阶段（stage）构建
# --------------------------------------------------------------------------- #
def _facts_and_dims(graph: GraphIndex, kb: KbView, target: str) -> Tuple[List[str], List[str]]:
    facts: List[str] = []
    dims: List[str] = []
    for edge in graph.edges_into(target):
        src = edge.get("source") or ""
        if not src:
            continue
        if kb.layer(src) == "dim":
            dims.append(src)
        else:
            facts.append(src)
    return facts, dims


def _chain_predecessor(graph: GraphIndex, kb: KbView, target: str,
                       exclude: Sequence[str] = ()) -> Optional[str]:
    """主路径前驱：优先分层更低的表；全是同层时按映射数折叠一层。"""
    skip = set(exclude)
    current = target
    for _ in range(MAX_COLLAPSE):
        facts, _dims = _facts_and_dims(graph, kb, current)
        facts = [f for f in facts if f not in skip]
        if not facts:
            return None
        cur_layer = layer_index(kb.layer(current))
        lower = [f for f in facts if layer_index(kb.layer(f)) < cur_layer]
        pool = lower or facts
        best = max(pool, key=lambda f: (graph.column_mapping_count(f, current), f))
        if layer_index(kb.layer(best)) < cur_layer:
            return best
        # 同层：继续往上折叠（中间层不单独出段）
        skip.add(current)
        current = best
    return None


def _order_sources(graph: GraphIndex, kb: KbView, target: str, facts: Sequence[str],
                   dims: Sequence[str]) -> List[str]:
    """源表顺序：映射到目标列最多的同层表优先（保证 JOIN 主表是事实表）。"""
    ordered = sorted(facts, key=lambda f: (-graph.column_mapping_count(f, target), f))
    return ordered + sorted(dims)


def _stage_columns(graph: GraphIndex, kb: KbView, target: str, sources: Sequence[str],
                   aliases: Dict[str, str], ev: Evidence) -> List[ColumnSpec]:
    """按存量字段血缘还原目标表的 SELECT 列（表达式原样复用，只重映射别名）。"""
    items = graph.expressions_into(target, sources)
    by_column: Dict[str, Dict[str, Any]] = {}
    for item in items:
        name = item.get("target_column")
        if name and name not in by_column:
            by_column[name] = item
    specs: Dict[str, ColumnSpec] = {}
    for name, item in by_column.items():
        owner = item.get("source_table") or ""
        raw = item.get("expression") or ""
        if not raw:
            continue
        old = detect_alias(raw)
        body = strip_alias_suffix(rewrite_alias(raw, old, aliases.get(owner, old))) if old else strip_alias_suffix(raw)
        field = kb.field(target, name)
        specs[name] = ColumnSpec(
            column=name, expression=body,
            chinese=str((field or {}).get("chinese_name") or ""),
            role="passthrough", source="graph_expression", source_table=owner,
            evidence=[f"列 {name}：沿用存量脚本表达式 {body}"
                      f"（来源 {', '.join(item.get('files') or []) or graph.name}）"],
        )
    # 目标表知识库字段顺序优先，其余追加
    order: List[str] = []
    for field in kb.fields(target):
        col = str(field.get("column_name"))
        if col in specs:
            order.append(col)
    order += [c for c in specs if c not in order]
    columns = [specs[c] for c in order]
    unknown = [c for c in by_column if c not in specs]
    if unknown:
        ev.warn(f"目标表 {target} 有 {len(unknown)} 个列的血缘表达式为空，已跳过：{', '.join(unknown[:6])}")
    return columns


def _fallback_columns(kb: KbView, target: str, primary: str, alias: str, ev: Evidence) -> List[ColumnSpec]:
    """血缘缺失时的兜底：按上游表的真实列直取（新建表模式）。"""
    columns: List[ColumnSpec] = []
    for field in kb.fields(primary):
        col = str(field.get("column_name"))
        columns.append(ColumnSpec(
            column=col, expression=f"{alias}.{col}", chinese=str(field.get("chinese_name") or ""),
            role="passthrough", source="source_field", source_table=primary,
            evidence=[f"列 {col}：知识库字段 {primary}.{col}"
                      f"（中文名「{field.get('chinese_name') or '未定名'}」，来源 {field.get('chinese_source')}）"],
        ))
    ev.warn(f"目标表 {target} 在血缘图里没有字段级血缘，已按上游表 {primary} 的真实列直取生成，"
            f"**表结构需人工确认**")
    return columns


def _build_stage(ctx: Dict[str, Any], target: str, chain: Sequence[str]) -> Dict[str, Any]:
    """构建一个阶段：源表（含维表 + 链路外依赖）+ 列 + SQL + 依据。"""
    kb: KbView = ctx["kb"]
    graph: GraphIndex = ctx["graph"]
    date = ctx["date"]
    ev = Evidence()
    facts, dims = _facts_and_dims(graph, kb, target)
    if not facts and not dims:
        ev.warn(f"目标表 {target} 在血缘图里没有任何上游，无法生成该段 SQL")
        return {"target_table": target, "layer": kb.layer(target), "sql": "", "columns": [],
                "sources": [], "depends_on": [], "external_inputs": [], "joins": [],
                "explain": ev.explain, "warnings": ev.warnings, "metrics": [], "parse_ok": False}
    sources = _order_sources(graph, kb, target, facts, dims)
    aliases = assign_aliases(sources)
    primary = sources[0]

    columns = _stage_columns(graph, kb, target, sources, aliases, ev)
    if not columns:
        columns = _fallback_columns(kb, target, primary, aliases.get(primary, ""), ev)

    joins: List[JoinSpec] = []
    for table in sources[1:]:
        layer = kb.layer(table)
        key, why = infer_join_key(primary, table, kb=kb, graph=graph)
        if key:
            joins.append(JoinSpec(
                table=table, alias=aliases[table], kind="LEFT",
                on=f"{aliases[primary]}.{key} = {aliases[table]}.{key}",
                reason=f"关联键：{why}", confirmed=("**" not in why)))
            if "**" in why:
                ev.warn(f"源表 {table} 的关联键 {why}，请人工确认")
        else:
            joins.append(JoinSpec(table=table, alias=aliases[table], kind="CROSS", on="",
                                  reason="关联键待人工确认", confirmed=False))
            ev.warn(f"源表 {table} 与 {primary} 找不到可推断的关联键，请在落地前补齐关联条件")

    pf = graph.partition_field(target, sources)
    where_lines: List[str] = []
    if pf:
        where_lines.append(f"{aliases[primary]}.{pf} = '{date}'")
        original = graph.partition_value(target, sources)
        if original:
            ev.say(f"分区 {pf}：存量脚本里的分区值是 {original}，生成时替换为 {date} 占位（由调度参数注入）")
    else:
        ev.warn(f"目标表 {target} 的存量脚本里没有分区过滤证据，本段 SQL 未生成分区条件，请人工确认")

    target_info = kb.table(target) or {}
    comments = [
        "=" * 61,
        f"生成器：sql-lineage-mvp generate（L2 分层链路 · {layer_cn(kb.layer(target))}）",
        f"目标表：{target}（{target_info.get('chinese_name') or '未登记'}）",
        "源  表：" + "、".join(f"{s}（{aliases.get(s)}）" for s in sources),
        f"依据  ：存量脚本字段级血缘（{graph.name}）",
        "=" * 61,
    ]
    sql = render_insert(target, columns, primary, aliases, joins=joins, where_lines=where_lines,
                        group_by=(), partition_field=pf, date=date, comments=comments)

    depends_on = [s for s in sources if s in chain and s != target]
    external = [s for s in sources if s not in depends_on]
    metrics = [m.get("chinese_name") or m.get("metric_name") for m in kb.metrics(target)]
    ev.say(f"本段 {len(columns)} 列 / {len(sources)} 张源表（链路内依赖 {len(depends_on)} 张，"
           f"链路外依赖 {len(external)} 张）/ 目标表已登记口径 {len(metrics)} 条")
    if external:
        ev.say("链路外依赖（不由本链路生成，假定已有调度产出）：" + "、".join(external))
    return {
        "target_table": target,
        "layer": kb.layer(target),
        "layer_cn": layer_cn(kb.layer(target)),
        "sql": sql,
        "columns": [c.to_dict() for c in columns],
        "sources": sources,
        "aliases": aliases,
        "depends_on": depends_on,
        "external_inputs": external,
        "joins": [j.to_dict() for j in joins],
        "partition_field": pf,
        "metrics": metrics,
        "explain": ev.explain,
        "warnings": ev.warnings,
    }


def _build_new_table_stage(ctx: Dict[str, Any], target: str, primary: str, dims: Sequence[str],
                           explain_extra: Sequence[str]) -> Dict[str, Any]:
    """新建表模式：血缘图里没有目标表，按上游真实列直取生成一段 SQL。"""
    kb: KbView = ctx["kb"]
    graph: GraphIndex = ctx["graph"]
    date = ctx["date"]
    ev = Evidence()
    for line in explain_extra:
        ev.say(line)
    sources = [primary] + [d for d in dims if d != primary]
    aliases = assign_aliases(sources)
    columns = _fallback_columns(kb, target, primary, aliases[primary], ev)
    joins: List[JoinSpec] = []
    for table in sources[1:]:
        key, why = infer_join_key(primary, table, kb=kb, graph=graph)
        joins.append(JoinSpec(table=table, alias=aliases[table], kind="LEFT" if key else "CROSS",
                              on=f"{aliases[primary]}.{key} = {aliases[table]}.{key}" if key else "",
                              reason=(f"关联键：{why}" if key else "关联键待人工确认"),
                              confirmed=bool(key) and "**" not in why))
    pf = graph.partition_field(target, sources) or "dt"
    ev.warn(f"新建表模式：目标表 {target} 的表名按命名规范推导、字段取上游 {primary} 的真实列，"
            f"**表结构与口径必须人工确认**")
    comments = [
        "=" * 61,
        "生成器：sql-lineage-mvp generate（L2 新建表模式 · 需人工确认）",
        f"目标表：{target}（知识库未登记，按命名规范推导）",
        "源  表：" + "、".join(f"{s}（{aliases.get(s)}）" for s in sources),
        "=" * 61,
    ]
    sql = render_insert(target, columns, primary, aliases, joins=joins,
                        where_lines=[f"{aliases[primary]}.{pf} = '{date}'"],
                        group_by=(), partition_field=pf, date=date, comments=comments)
    return {
        "target_table": target, "layer": kb.layer(target), "layer_cn": layer_cn(kb.layer(target)),
        "sql": sql, "columns": [c.to_dict() for c in columns], "sources": sources,
        "aliases": aliases, "depends_on": [primary], "external_inputs": list(dims) + [primary],
        "joins": [j.to_dict() for j in joins], "partition_field": pf, "metrics": [],
        "explain": ev.explain, "warnings": ev.warnings, "is_new_table": True,
    }


def _new_table_name(kb: KbView, target_layer: str, requirement: str) -> str:
    """新建表名按命名规范推导：``<层>.<层>_<需求关键词>``。"""
    key = _clean_requirement(requirement)
    key = re.sub(r"[^\w\u4e00-\u9fff]+", "", key) or "新建表"
    prefix = _LAYER_PREFIX.get(target_layer, target_layer)
    return f"{target_layer}.{prefix}_{key}"


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def generate_pipeline(payload: Dict[str, Any]) -> Dict[str, Any]:
    """``POST /generate/pipeline`` 与 ``generate pipeline``：分层链路生成。"""
    started = time.perf_counter()
    requirement = str(payload.get("requirement") or "").strip()
    target_layer = str(payload.get("target_layer") or "ads").strip().lower()
    dialect = str(payload.get("dialect") or "hive")
    max_stages = int(payload.get("max_stages") or 4)
    date = str(payload.get("date") or DATE_PLACEHOLDER)
    if not requirement and not payload.get("target_table"):
        return {"success": False, "mode": "L2", "error": "requirement 不能为空（或直接给 target_table）"}

    try:
        store = open_store(payload.get("db"))
    except FileNotFoundError as exc:
        return {"success": False, "mode": "L2", "error": str(exc)}

    kb = KbView(store)
    graph = GraphIndex.load(payload.get("graph"))
    llm = GenerateLLM(enabled=payload.get("use_llm", "auto"))
    try:
        found = find_target_table(kb, graph, requirement, target_layer, llm,
                                  override=str(payload.get("target_table") or "").strip())
        ctx = {"kb": kb, "graph": graph, "date": date, "dialect": dialect}
        warnings: List[str] = []
        explain: List[str] = []

        if found["matched"] and found["mode"] == "reuse":
            chain = _build_chain(graph, kb, found["table"], max_stages, warnings)
            stages = [_build_stage(ctx, t, chain) for t in chain]
            explain.append(f"目标表定位：{found['table']}（{found['matched_by']}）")
        else:
            # ---- 新建表模式 -------------------------------------------------- #
            key = _clean_requirement(requirement)
            new_table = _new_table_name(kb, target_layer, requirement)
            lower = [c for c in found["candidates"] if c["table"] != new_table]
            if (not lower) and graph.available:
                lower = [{"table": t, "score": _score(t, "", key)} for t in graph.tables()
                         if kb.layer(t) != target_layer]
                lower.sort(key=lambda x: -x["score"])
            if not lower:
                store.close()
                return {"success": False, "mode": "L2",
                        "error": f"需求「{requirement}」既匹配不到 {target_layer} 层的现有表，也找不到可用的上游表",
                        "hint": "请用 target_table 显式指定目标表，或先用 kb build 建立知识库"}
            primary = lower[0]["table"]
            dims = [t for t in graph.upstream_tables(primary) if kb.layer(t) == "dim"]
            explain.append(f"未匹配到 {target_layer} 层现有表，进入新建表模式：按命名规范推导目标表 {new_table}")
            explain.append(f"上游选定：{primary}（关键词「{key}」匹配得分 {lower[0]['score']}）")
            stages = [_build_new_table_stage(ctx, new_table, primary, dims, explain)]
            warnings.append("本次为新建表模式：表名与字段结构均为推导结果，落地前请人工评审 DDL")

        # ---- 逐段语法自检 ------------------------------------------------- #
        for stage in stages:
            stage["ast_check"] = parse_check(stage.get("sql") or "", dialect)
            if not stage["ast_check"]["parse_ok"]:
                stage["warnings"].append(
                    f"本段 SQL 未通过语法自检：{stage['ast_check'].get('error') or '未解析出输出表'}")

        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []
        for idx, stage in enumerate(stages, start=1):
            nodes.append({"id": stage["target_table"], "table": stage["target_table"],
                          "layer": stage["layer"], "stage": idx, "role": "stage"})
        for stage in stages:
            for dep in stage["depends_on"]:
                edges.append({"source": dep, "target": stage["target_table"], "kind": "chain"})
            for ext in stage["external_inputs"]:
                if not any(n["id"] == ext for n in nodes):
                    nodes.append({"id": ext, "table": ext, "layer": kb.layer(ext),
                                  "stage": 0, "role": "external"})
                edges.append({"source": ext, "target": stage["target_table"], "kind": "external"})

        total_columns = sum(len(s.get("columns") or []) for s in stages)
        explain.append(f"链路共 {len(stages)} 段 / {total_columns} 个字段映射 / "
                       f"{len([e for e in edges if e['kind'] == 'chain'])} 条段间依赖")
        explain.append("每段 SQL 的列表达式均来自存量脚本字段级血缘（warehouse_graph.json），"
                       "未凭空编字段；链路外依赖已如实列出")
        result: Dict[str, Any] = {
            "success": True,
            "mode": "L2",
            "requirement": requirement,
            "target_layer": target_layer,
            "target_table": stages[0]["target_table"] if stages else "",
            "table_mode": found["mode"],
            "matched_by": found["matched_by"],
            "max_stages": max_stages,
            "dialect": dialect,
            "date_placeholder": date,
            "stages": stages,
            "pipeline": {"nodes": nodes, "edges": edges},
            "explain": explain,
            "warnings": warnings + [w for s in stages for w in (s.get("warnings") or [])],
            "db": str(kb.store.path),
            "graph_file": graph.name,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
        if not found["matched"]:
            result["warnings"].append("本次未复用现有表：需求关键词没有命中所选分层的任何表")
        result["llm"] = llm.info()
        return result
    finally:
        store.close()


def _build_chain(graph: GraphIndex, kb: KbView, target: str, max_stages: int,
                 warnings: List[str]) -> List[str]:
    """主路径链路：从目标表逐层往上推，最多 ``max_stages`` 段。"""
    chain: List[str] = [target]
    while len(chain) < max_stages:
        predecessor = _chain_predecessor(graph, kb, chain[-1], exclude=chain)
        if not predecessor:
            break
        if kb.layer(predecessor) in ("src", "stg"):
            warnings.append(f"链路在 {chain[-1]} 处停止：其上游 {predecessor} 属于"
                            f"{layer_cn(kb.layer(predecessor))}（贴源抽取，通常由数据集成工具负责），"
                            f"未生成该段")
            break
        chain.append(predecessor)
    skipped = [f for f in _facts_and_dims(graph, kb, chain[0])[0] if f not in chain]
    if skipped:
        warnings.append(f"目标表 {chain[0]} 的其它上游未展开为独立阶段"
                        f"（已达 max_stages={max_stages} 或不在主路径上），"
                        f"这些表在本链路中按「链路外依赖」处理：{', '.join(skipped)}")
    if len(chain) == max_stages:
        warnings.append(f"链路已达段数上限 max_stages={max_stages}，更上游的层级未展开")
    return chain


# --------------------------------------------------------------------------- #
# 终端输出
# --------------------------------------------------------------------------- #
def format_pipeline_text(result: Dict[str, Any]) -> str:
    if not result.get("success"):
        return "生成失败：" + str(result.get("error") or "未知错误")
    lines: List[str] = ["=" * 72,
                        f"L2 分层链路生成：{result.get('requirement')}"
                        f" → {result.get('target_table')}（{result.get('table_mode')} 模式）",
                        "=" * 72]
    for idx, stage in enumerate(result.get("stages") or [], start=1):
        lines.append(f"[{idx}/{len(result['stages'])}] {stage['layer_cn']}层 -> {stage['target_table']}"
                     f"   依赖 {'、'.join(stage.get('depends_on') or []) or '—'}"
                     + (f"；链路外依赖 {'、'.join(stage['external_inputs'])}" if stage.get("external_inputs") else ""))
        lines.append("-" * 72)
        for line in (stage.get("sql") or "").splitlines():
            lines.append("  " + line)
        ast = stage.get("ast_check") or {}
        lines.append(f"  语法自检：{'✅' if ast.get('parse_ok') else '❌'} "
                     f"{ast.get('statement_count', 0)} 语句 / {ast.get('column_lineage_count', 0)} 字段血缘")
        lines.append("")
    pipeline = result.get("pipeline") or {}
    nodes = pipeline.get("nodes") or []
    edges = pipeline.get("edges") or []
    lines.append("-" * 72)
    lines.append(f"链路图：{len(nodes)} 个节点 / {len(edges)} 条边")
    for edge in edges:
        mark = "（链路内）" if edge.get("kind") == "chain" else "（链路外依赖）"
        lines.append(f"  {edge['source']} → {edge['target']} {mark}")
    lines.append("-" * 72)
    lines.append("生成依据：")
    for idx, text in enumerate(result.get("explain") or [], start=1):
        lines.append(f"  [{idx}] {text}")
    if result.get("warnings"):
        lines.append("-" * 72)
        lines.append(f"需人工确认（{len(result['warnings'])} 项）：")
        for idx, text in enumerate(result["warnings"], start=1):
            lines.append(f"  ⚠ [{idx}] {text}")
    lines.append("=" * 72)
    return "\n".join(lines)
