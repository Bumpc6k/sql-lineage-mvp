"""L1：单表加工 SQL 生成（模板引擎 + 知识库口径复用）。

生成规则（每一条都可追溯，绝不凭空编字段）
------------------------------------------
1. **目标列名**：优先取知识库里目标表已登记的字段（中文名匹配），其次取该表已登记口径的
   指标名，再次取源字段同名列；都找不到 → 不生成该列，落到 ``warnings`` + ``unresolved``。
2. **列表达式**：三档次序
   a. 知识库口径公式（``kb_metrics.formula_full``）—— 把公式里的中文业务名按
      ``kb_metrics.depends_on`` 精确替换成 ``别名.字段``；公式里已有聚合函数就直接用，没有则按
      分组维度套 ``SUM``（比率类会额外提示人工确认）；
   b. 存量脚本字段血缘（``warehouse_graph.json`` 边上真实出现过的表达式）—— 原样复用、只重映射别名；
   c. 源表同名列直取（``t1.col``）。
3. **分区**：分区字段与分区值都来自存量脚本的分区过滤证据；用户显式指定的分区字段若得不到
   印证会给出 warning。
4. **收尾自检**：生成的 SQL 会立刻丢回 :mod:`lineage.parser` 解析一遍，``ast_check`` 里带真实结果。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from lineage_core.graph import table_layer
from lineage.generate.llm import GenerateLLM
from lineage.generate.spec import (
    DATE_PLACEHOLDER,
    Evidence,
    GraphIndex,
    KbView,
    assign_aliases,
    detect_alias,
    has_aggregate,
    infer_join_key,
    layer_cn,
    open_store,
    parse_check,
    rewrite_alias,
    short_name,
    split_table,
    strip_alias_suffix,
)

__all__ = [
    "DATE_PLACEHOLDER",
    "ColumnSpec",
    "JoinSpec",
    "generate_sql",
    "format_sql_text",
    "render_insert",
]

#: 比率类口径：套聚合时要额外提示人工确认
_RATIO_TYPES = ("比率", "条件分支")
#: 指标口径丰富度权重（同名词多条口径时的排序参考）
_TYPE_RICHNESS = {
    "比率": 3.0, "条件分支": 2.6, "窗口函数": 2.4, "算术计算": 2.0,
    "聚合": 1.8, "函数转换": 1.0, "直取": 0.2, "": 0.5, None: 0.5,
}


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class ColumnSpec:
    """一列生成的完整证据链。"""

    column: str
    expression: str
    chinese: str = ""
    role: str = "metric"                 # group | metric | dim | passthrough
    source: str = ""                     # kb_metric | graph_expression | source_field | unresolved
    source_table: str = ""
    evidence: List[str] = field(default_factory=list)
    needs_review: bool = False
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "column": self.column,
            "chinese_name": self.chinese,
            "expression": self.expression,
            "role": self.role,
            "source": self.source,
            "source_table": self.source_table,
            "evidence": list(self.evidence),
            "needs_review": bool(self.needs_review),
            "note": self.note,
        }


@dataclass
class JoinSpec:
    """一条 JOIN（``on`` 为空表示关联键未确认，SQL 里会留 TODO 注释）。"""

    table: str
    alias: str
    kind: str = "LEFT"
    on: str = ""
    reason: str = ""
    confirmed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"table": self.table, "alias": self.alias, "kind": self.kind,
                "on": self.on, "reason": self.reason, "confirmed": bool(self.confirmed)}


@dataclass
class GenContext:
    """生成上下文（L1 / L2 共用）。"""

    kb: KbView
    graph: GraphIndex
    dialect: str = "hive"
    date: str = DATE_PLACEHOLDER
    ev: Evidence = field(default_factory=Evidence)

    @property
    def store(self):
        return self.kb.store


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _same(a: Any, b: Any) -> bool:
    return str(a or "").strip().lower() == str(b or "").strip().lower()


def _name_match(row: Dict[str, Any], name: str) -> bool:
    return _same(row.get("chinese_name"), name) or _same(row.get("metric_name"), name)


def _richness(row: Dict[str, Any], sources: Sequence[str]) -> Tuple:
    """同名词多条口径的排序：依赖全在源表内 > 口径更丰富 > 置信度 > 表名。"""
    deps = [d for d in (row.get("depends_on") or []) if isinstance(d, dict)]
    inside = sum(1 for d in deps if d.get("table") in set(sources))
    body = row.get("formula") or ""
    ops = len(re.findall(r"[+\-*/]", body))
    weight = _TYPE_RICHNESS.get(row.get("metric_type"), 0.5) or 0.5
    return (-inside, -len(deps), -(weight + min(ops, 4) * 0.4),
            -float(row.get("confidence") or 0), str(row.get("table_name") or ""))


def _substitute(expression: str, mapping: Sequence[Tuple[str, str]]) -> Tuple[str, List[str]]:
    """把公式里的中文业务名替换成 ``别名.字段``（长名优先，避免「产量（条）」被「产量」截断）。

    返回 ``(替换后的表达式, 未命中的中文名列表)``。
    """
    out = expression or ""
    for chinese, ref in sorted(mapping, key=lambda x: len(x[0]), reverse=True):
        if chinese:
            out = out.replace(chinese, ref)
    # 残留在表达式里的中文标识符 = 没找到来源的字段，必须让人工确认
    leftovers = [t for t in re.findall(r"[A-Za-z0-9_]*[\u4e00-\u9fff][\w\u4e00-\u9fff]*", out)]
    return out, leftovers


def _strip_comment(expression: str) -> str:
    """去掉表达式里残留的 SQL 注释（``-- xxx``）。"""
    return re.sub(r"--[^\n]*", "", expression or "").strip()


def _rhs_of_formula(formula: str) -> str:
    """``产量 = SUM(产量)`` -> ``SUM(产量)``。"""
    text = (formula or "").strip()
    if "=" in text:
        text = text.split("=", 1)[1]
    return _strip_comment(text).strip()


def _qualify(alias: str, column: str) -> str:
    return f"{alias}.{column}" if alias else column


def _chinese_of(kb: KbView, table: str, column: str) -> str:
    row = kb.field(table, column) if table else None
    return str((row or {}).get("chinese_name") or "")


def _field_evidence(kb: KbView, table: str, column: str, prefix: str = "字段") -> str:
    row = kb.field(table, column)
    if not row:
        return f"{prefix} {table}.{column}（知识库未登记中文名）"
    chinese = row.get("chinese_name") or "未定名"
    return (f"{prefix} {table}.{column} = 「{chinese}」"
            f"（中文名来源 {row.get('chinese_source') or '—'}，置信度 {row.get('confidence')}）")


# --------------------------------------------------------------------------- #
# 列解析：分组列 / 指标列
# --------------------------------------------------------------------------- #
def find_column_in_sources(ctx: GenContext, name: str, sources: Sequence[str]) -> Optional[Tuple[str, str]]:
    """在源表里找列：先精确同名列，再中文业务名；返回 ``(表, 列)``。"""
    key = (name or "").strip()
    if not key:
        return None
    for table in sources:
        if ctx.kb.field(table, key):
            return table, key
    for table in sources:
        row = ctx.kb.find_field_by_chinese(table, key)
        if row:
            return table, str(row.get("column_name"))
    return None


def resolve_group_column(ctx: GenContext, name: str, target: str, target_fields: Sequence[Dict[str, Any]],
                         sources: Sequence[str], aliases: Dict[str, str]) -> Optional[ColumnSpec]:
    """解析分组维度列（必须能在源表或目标表里找到真实列，否则不生成）。"""
    hit = find_column_in_sources(ctx, name, sources)
    if hit:
        table, column = hit
        alias = aliases.get(table, "")
        chinese = _chinese_of(ctx.kb, table, column)
        spec = ColumnSpec(
            column=column, expression=f"{_qualify(alias, column)}", chinese=chinese,
            role="group", source="source_field", source_table=table,
            evidence=[
                f"分组维度 {column}：{_field_evidence(ctx.kb, table, column)}",
                f"来源表 {table}（别名 {alias}）",
            ],
        )
        ctx.ev.say(f"分组维度「{name}」-> {table}.{column}{'（' + chinese + '）' if chinese else ''}")
        return spec
    # 目标表有这列但源表没有：可能来自维表（由调用方补 JOIN）
    for row in target_fields:
        if str(row.get("column_name")) == name or (row.get("chinese_name") or "") == name:
            ctx.ev.warn(
                f"分组维度「{name}」在目标表 {target} 存在，但源表中找不到同名列 / 中文名；"
                f"已跳过该列，请补充维表 JOIN 或确认字段名"
            )
            return None
    ctx.ev.warn(f"分组维度「{name}」在源表与目标表里都找不到，已跳过（不生成未经验证的列）")
    return None


def resolve_metric_column(ctx: GenContext, metric: str, target: str,
                          target_fields: Sequence[Dict[str, Any]], sources: Sequence[str],
                          aliases: Dict[str, str], aggregated: bool,
                          group_desc: str = "") -> ColumnSpec:
    """解析一个指标列：目标列名 + 表达式 + 依据，全程可追溯。"""
    ev = ctx.ev
    rows = ctx.kb.find_metrics(metric)
    target_rows = [r for r in rows if _same(r.get("table_name"), target)]
    source_rows = [r for r in rows if any(_same(r.get("table_name"), s) for s in sources)]
    row: Optional[Dict[str, Any]] = None
    role = "other"
    exact_target = [r for r in target_rows if _name_match(r, metric)]
    if exact_target:
        row, role = sorted(exact_target, key=lambda r: _richness(r, sources))[0], "target"
    elif source_rows:
        row, role = sorted(source_rows, key=lambda r: _richness(r, sources))[0], "source"
    elif target_rows:
        row, role = sorted(target_rows, key=lambda r: _richness(r, sources))[0], "target"
    elif rows:
        row, role = sorted(rows, key=lambda r: _richness(r, sources))[0], "other"

    if row is None:
        # 没有任何口径：按「源表同名列直取」处理，并明确标注需人工确认
        hit = find_column_in_sources(ctx, metric, sources)
        if not hit:
            ev.warn(f"指标「{metric}」既没有知识库口径、源表里也没有同名列 / 同中文名，已跳过该列")
            return ColumnSpec(column="", expression="", role="metric", source="unresolved",
                              needs_review=True,
                              note=f"未解析：知识库无口径且源表无同名列「{metric}」")
        table, column = hit
        alias = aliases.get(table, "")
        expr = _qualify(alias, column)
        if aggregated:
            expr = f"SUM({expr})"
        chinese = _chinese_of(ctx.kb, table, column)
        ev.warn(f"指标「{metric}」知识库未登记口径：已按源字段 {table}.{column} 直取生成，请人工确认业务口径")
        return ColumnSpec(
            column=column, expression=expr, chinese=chinese, role="metric",
            source="source_field", source_table=table, needs_review=True,
            evidence=[f"无口径登记，按源字段直取：{_field_evidence(ctx.kb, table, column)}"],
            note="知识库无口径，按源字段直取",
        )

    deps = [d for d in (row.get("depends_on") or []) if isinstance(d, dict)]
    metric_type = str(row.get("metric_type") or "")
    agg = str(row.get("aggregate_func") or "").strip()

    # ---- 目标列名 -------------------------------------------------------- #
    column = ""
    column_why = ""
    field_row = ctx.kb.find_field_by_chinese(target, metric)
    if role == "target" and row.get("metric_name"):
        column, column_why = str(row["metric_name"]), f"目标表已登记口径的指标名（{row.get('source_file')}）"
    elif field_row:
        column = str(field_row.get("column_name"))
        column_why = f"目标表字段中文名匹配（{field_row.get('chinese_source')}）"
    else:
        nt = [r for r in target_fields if _same(r.get("column_name"), row.get("metric_name"))]
        if nt:
            column, column_why = str(nt[0]["column_name"]), "目标表同名字段"
    dep_cols = [str(d.get("column")) for d in deps if d.get("column")]
    if not column and len(dep_cols) == 1:
        same_name = [r for r in target_fields if _same(r.get("column_name"), dep_cols[0])]
        if same_name:
            column = dep_cols[0]
            column_why = "按命名规范沿用源列名（目标表存在同名列）"
        else:
            column = dep_cols[0]
            column_why = "按命名规范沿用源列名（**目标表无同名列，需人工确认**）"
    if not column:
        column = str(row.get("metric_name") or metric)
        column_why = "沿用知识库口径的指标名（**目标表结构待确认**）"

    # ---- 表达式 ---------------------------------------------------------- #
    mapping: List[Tuple[str, str]] = []
    dep_missing: List[str] = []
    for dep in deps:
        chinese = str(dep.get("chinese_name") or "")
        col = str(dep.get("column") or "")
        table = str(dep.get("table") or "")
        owner = table if table in sources else ""
        if not owner:
            for cand in sources:
                if ctx.kb.field(cand, col):
                    owner = cand
                    break
        if not owner:
            dep_missing.append(f"{chinese or col}（口径里记的表是 {table or '未标注'}，不在本次源表中）")
            continue
        mapping.append((chinese or col, _qualify(aliases.get(owner, ""), col)))

    formula = _rhs_of_formula(row.get("formula_full") or row.get("formula") or "")
    evidence: List[str] = []
    needs_review = False
    note = ""
    metric_cite = (
        f"口径依据：{row.get('metric_name')} = {row.get('formula')}（{metric_type}"
        f"{'，聚合 ' + agg if agg else ''}）；来源 {row.get('source_file')} 第 {row.get('source_stmt')} 条语句"
    )
    for dep in deps:
        evidence.append(
            f"口径依赖字段：{dep.get('chinese_name') or dep.get('column')} -> "
            f"{dep.get('table') or '未标注'}.{dep.get('column')}"
        )

    # 表达式三档优先级：
    # a) 目标表已有该列、且存量脚本里有真实表达式（非目标表口径时优先，忠实还原加工逻辑）
    # b) 知识库口径公式（把中文业务名替换成别名.字段）
    # c) 源字段直取
    graph_hit = None
    for item in ctx.graph.expressions_into(target, sources):
        if _same(item.get("target_column"), column) and item.get("expression"):
            graph_hit = item
            break
    expression, source_kind = "", ""
    if graph_hit and role != "target" and graph_hit.get("source_table") in sources:
        owner = str(graph_hit["source_table"])
        old = detect_alias(str(graph_hit["expression"]))
        body = strip_alias_suffix(str(graph_hit["expression"]))
        if old:
            body = strip_alias_suffix(rewrite_alias(str(graph_hit["expression"]), old, aliases.get(owner, old)))
        expression = body
        if aggregated and not has_aggregate(body):
            expression = f"{agg or 'SUM'}({body})"
            evidence.append(f"聚合方式：存量表达式不含聚合，已按分组维度 {group_desc or '（见 GROUP BY）'} "
                            f"套 {agg or 'SUM'}()")
        source_kind = "graph_expression"
        evidence.append(f"表达式沿用存量脚本字段血缘：{body}"
                        f"（来源 {', '.join(graph_hit.get('files') or []) or ctx.graph.name}）")
        evidence.append(metric_cite)
    elif formula and mapping:
        substituted, leftovers = _substitute(formula, mapping)
        if aggregated and not has_aggregate(substituted):
            func = agg or "SUM"
            substituted, _ = _substitute(formula, [(c, f"{func}({r})") for c, r in mapping])
            evidence.append(f"聚合方式：口径未含聚合函数，按分组维度 {group_desc or '（见 GROUP BY）'} 套 {func}()")
            if metric_type in _RATIO_TYPES:
                needs_review = True
                note = f"{metric_type}类口径已按 {func} 分子/分母处理，请确认是否应先算比率再聚合"
                ev.warn(f"指标「{metric}」是{metric_type}类口径：已对分子分母分别套 {func}()，"
                        f"如业务要求先算比率再平均，请手工改为 {func}(比率字段)")
        leftovers = [x for x in leftovers if x not in ("AS",)]
        if leftovers:
            needs_review = True
            note = (note + "；" if note else "") + "公式里有无法定位来源的中文标识符：" + "、".join(sorted(set(leftovers)))
            ev.warn(f"指标「{metric}」的公式含知识库未登记来源的标识符：{'、'.join(sorted(set(leftovers)))}，"
                    f"已原样保留在 SQL 中，请人工确认")
        expression, source_kind = substituted, "kb_metric"
        evidence.append(metric_cite)
        if dep_missing:
            needs_review = True
            ev.warn(f"指标「{metric}」的口径依赖不在本次源表中：{'；'.join(dep_missing)}")
    else:
        hit = find_column_in_sources(ctx, column, sources) or \
            (find_column_in_sources(ctx, metric, sources) if not deps else None)
        if hit:
            table, col = hit
            expression = _qualify(aliases.get(table, ""), col)
            if aggregated:
                expression = f"SUM({expression})"
            source_kind = "source_field"
            evidence.append(f"源字段直取：{_field_evidence(ctx.kb, table, col)}")
            evidence.append(metric_cite)
        else:
            needs_review = True
            note = (note + "；" if note else "") + "找不到可用的表达式来源，未生成该列"
            ev.warn(f"指标「{metric}」找不到可行的表达式来源（口径公式 / 存量血缘 / 源字段都不可用），已跳过")
            return ColumnSpec(column="", expression="", chinese=metric, role="metric",
                              source="unresolved", needs_review=True, note=note)

    evidence.append(f"目标列 {target}.{column}：{column_why}")
    if role == "other":
        ev.warn(f"指标「{metric}」的知识库口径来自 {row.get('table_name')}（既不是目标表也不是源表），"
                f"已按该口径生成，请确认口径适用范围")
    chinese = metric or _chinese_of(ctx.kb, target, column)
    spec = ColumnSpec(column=column, expression=expression, chinese=chinese, role="metric",
                      source=source_kind, source_table=str(row.get("table_name") or ""),
                      evidence=evidence, needs_review=needs_review, note=note)
    ev.say(f"指标「{metric}」-> {column} = {expression}"
           + (f"（依据口径 {row.get('metric_name')}@{row.get('table_name')}，"
              f"{row.get('metric_type')}，来源 {row.get('source_file')}）" if row is not None else ""))
    for line in evidence:
        ev.say(f"  · {line}")
    return spec


# --------------------------------------------------------------------------- #
# SQL 渲染
# --------------------------------------------------------------------------- #
def render_insert(target: str, columns: Sequence[ColumnSpec], primary: str,
                  aliases: Dict[str, str], joins: Sequence[JoinSpec] = (),
                  where_lines: Sequence[str] = (), group_by: Sequence[str] = (),
                  partition_field: str = "", date: str = DATE_PLACEHOLDER,
                  comments: Sequence[str] = (), indent: str = "    ",
                  dialect: str = "hive", extra_join_hints: Sequence[str] = ()) -> str:
    """拼 ``INSERT OVERWRITE ... SELECT``（纯字符串模板，不做任何隐式推断）。"""
    lines: List[str] = []
    for text in comments:
        lines.append(f"-- {text}")
    partition = f" PARTITION ({partition_field} = '{date}')" if partition_field else ""
    lines.append(f"INSERT OVERWRITE TABLE {target}{partition}")
    lines.append("SELECT")
    rendered: List[str] = []
    for col in columns:
        comment = f"  -- {col.chinese}" if col.chinese else ""
        if col.needs_review:
            comment += "  [需人工确认]" if comment else "  -- [需人工确认]"
        rendered.append(f"{indent}{col.expression} AS {col.column},{comment}")
    if rendered:
        rendered[-1] = rendered[-1].rstrip(",")
    lines.extend(rendered)
    lines.append(f"FROM {primary} {aliases.get(primary, '')}".rstrip())
    for join in joins:
        if join.on:
            lines.append(f"{join.kind} JOIN {join.table} {join.alias} ON {join.on}"
                         + (f"  -- {join.reason}" if join.reason else ""))
        else:
            lines.append(f"-- TODO 关联键待人工确认：{join.kind} JOIN {join.table} {join.alias} "
                         f"ON <{aliases.get(primary, 't1')}.? = {join.alias}.?>"
                         + (f"  -- {join.reason}" if join.reason else ""))
            lines.append(f"CROSS JOIN {join.table} {join.alias}")
    for hint in extra_join_hints:
        lines.append(f"-- {hint}")
    if where_lines:
        lines.append("WHERE " + ("\n  AND ".join(where_lines)))
    if group_by:
        lines.append("GROUP BY " + ", ".join(group_by))
    lines.append(";")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# L1 主入口
# --------------------------------------------------------------------------- #
def generate_sql(payload: Dict[str, Any]) -> Dict[str, Any]:
    """``POST /generate/sql`` 与 ``generate sql`` 的实现：单表加工 SQL 生成。"""
    started = time.perf_counter()
    dialect = str(payload.get("dialect") or "hive")
    target_in = str(payload.get("target_table") or "").strip()
    sources_in = [str(s).strip() for s in (payload.get("source_tables") or []) if str(s).strip()]
    metrics = [str(m).strip() for m in (payload.get("metrics") or []) if str(m).strip()]
    group_by_in = [str(g).strip() for g in (payload.get("group_by") or []) if str(g).strip()]
    date = str(payload.get("date") or DATE_PLACEHOLDER)
    all_columns = bool(payload.get("all_columns"))

    if not target_in:
        return {"success": False, "mode": "L1", "error": "target_table 不能为空"}
    if not sources_in:
        return {"success": False, "mode": "L1", "error": "source_tables 不能为空（至少一张源表）"}
    if not metrics and not group_by_in:
        return {"success": False, "mode": "L1",
                "error": "metrics 与 group_by 至少给一个（否则没有可生成的内容）",
                "hint": "示例：{\"source_tables\":[\"ods.ods_卷烟产量流水\"],"
                        "\"target_table\":\"cdw.dwd_卷烟产量明细\",\"metrics\":[\"产量\"],"
                        "\"group_by\":[\"plant_code\"]}"}

    try:
        store = open_store(payload.get("db"))
    except FileNotFoundError as exc:
        return {"success": False, "mode": "L1", "error": str(exc)}

    kb = KbView(store)
    graph = GraphIndex.load(payload.get("graph"))
    ctx = GenContext(kb=kb, graph=graph, dialect=dialect, date=date)
    ev = ctx.ev
    try:
        return _generate_sql_core(ctx, target_in, sources_in, metrics, group_by_in,
                                  date, all_columns, payload, started)
    finally:
        store.close()


def _generate_sql_core(ctx: GenContext, target_in: str, sources_in: Sequence[str],
                       metrics: Sequence[str], group_by_in: Sequence[str], date: str,
                       all_columns: bool, payload: Dict[str, Any],
                       started: float) -> Dict[str, Any]:
    kb, graph, ev = ctx.kb, ctx.graph, ctx.ev
    dialect = ctx.dialect

    target = kb.resolve(target_in)
    target_info = kb.table(target_in)
    target_known = target_info is not None
    target_fields = kb.fields(target) if target_known else []
    if not target_known:
        ev.warn(f"目标表 {target_in} 未在知识库登记：目标列名/结构无法校验，"
                f"已按源表字段与命名规范生成，务必人工确认目标表 DDL")

    sources: List[str] = []
    for name in sources_in:
        resolved = kb.resolve(name)
        info = kb.table(name)
        if info is None:
            ev.warn(f"源表 {name} 未在知识库登记：字段无法校验，仅按血缘图/同名列处理")
            if not graph.has(resolved) and not graph.has(name):
                ev.warn(f"源表 {name} 在血缘图 {graph.name or '（未加载）'} 里也不存在")
        if resolved not in sources:
            sources.append(resolved)
    aliases = assign_aliases(sources)
    primary = sources[0]

    # ---- 分区 ------------------------------------------------------------ #
    partition_field = str(payload.get("partition_field") or "").strip()
    confirmed_pf = graph.partition_field(target, sources)
    if partition_field:
        if confirmed_pf and confirmed_pf != partition_field:
            ev.warn(f"指定的分区字段 {partition_field} 与存量脚本里的分区键 {confirmed_pf} 不一致，请确认")
        elif not confirmed_pf:
            ev.warn(f"分区字段 {partition_field} 未在存量脚本 / 血缘图里得到印证，请确认源表确实按 {partition_field} 分区")
    else:
        partition_field = confirmed_pf or ""
        if partition_field:
            ev.say(f"分区字段 {partition_field}：依据存量脚本的分区过滤"
                   f"（{', '.join(graph.source_files(target, sources)) or graph.name}）")
    if partition_field and all(not kb.field(s, partition_field) for s in sources) and not confirmed_pf:
        ev.warn(f"源表知识库字段里没有 {partition_field}（若是分区列通常不出现在 SELECT 列里，可忽略）")

    # ---- 列 -------------------------------------------------------------- #
    group_specs: List[ColumnSpec] = []
    group_refs: List[str] = []
    for name in group_by_in:
        spec = resolve_group_column(ctx, name, target, target_fields, sources, aliases)
        if spec:
            group_specs.append(spec)
            group_refs.append(spec.expression)
    aggregated = bool(group_by_in)
    group_desc = "、".join(group_by_in) if group_by_in else ""

    metric_specs: List[ColumnSpec] = []
    for name in metrics:
        spec = resolve_metric_column(ctx, name, target, target_fields, sources, aliases,
                                     aggregated, group_desc)
        if spec.column:
            metric_specs.append(spec)

    columns = group_specs + metric_specs
    if not columns:
        return {
            "success": False, "mode": "L1",
            "error": "没有任何列可以被生成（全部落到 warnings）",
            "explain": ev.explain, "warnings": ev.warnings,
            "target_table": target, "source_tables": sources,
        }

    # 目标列覆盖率（Hive 按位置写入，列数不一致会报错）
    if target_known and target_fields:
        known = [str(f.get("column_name")) for f in target_fields
                 if str(f.get("column_name")) != partition_field]
        # 分区列不参与 SELECT
        covered = [c.column for c in columns]
        missing = [c for c in known if c not in covered]
        if missing:
            ev.warn(f"目标表知识库登记 {len(known)} 个非分区列，本 SQL 只覆盖 {len(covered)} 个；"
                    f"缺失 {len(missing)} 个：{', '.join(missing[:8])}"
                    f"{' 等' if len(missing) > 8 else ''}。"
                    f"Hive/Spark 按位置写入要求列数一致 —— 若目标表是既有表，请补齐其余列"
                    f"（可传 all_columns=true 让生成器按存量血缘补齐），或确认是新建表。")

    if all_columns and target_known:
        extra = _append_missing_columns(ctx, target, target_fields, sources, aliases, columns,
                                       group_refs, aggregated)
        if extra:
            columns = columns + extra

    # ---- JOIN ------------------------------------------------------------ #
    joins, hints = _build_joins(ctx, target, sources, aliases, payload)

    # ---- WHERE / 注释 ----------------------------------------------------- #
    where_lines: List[str] = []
    if partition_field:
        where_lines.append(f"{aliases.get(primary, '')}.{partition_field} = '{date}'"
                           if aliases.get(primary) else f"{partition_field} = '{date}'")
    user_where = str(payload.get("where") or "").strip()
    if user_where:
        where_lines.append(user_where.rstrip(";"))
        ev.say(f"额外过滤条件由调用方提供：{user_where}")

    target_cn = (target_info or {}).get("chinese_name") or "未登记"
    tier = (target_info or {}).get("layer") or table_layer(target)
    comments = [
        "=" * 61,
        "生成器：sql-lineage-mvp generate（L1 单表加工 SQL）",
        f"目标表：{target}（{target_cn}，{layer_cn(tier)}）",
        "源  表：" + "、".join(f"{s}（{aliases.get(s)}）" for s in sources),
        f"分区  ：{partition_field} = '{date}'" if partition_field else "分区  ：无",
        "依据  ：知识库口径公式 + 字段中文名 + 存量脚本字段血缘（见 explain）",
        "=" * 61,
    ]
    sql = render_insert(target, columns, primary, aliases, joins=joins, where_lines=where_lines,
                        group_by=group_refs, partition_field=partition_field, date=date,
                        comments=comments, dialect=dialect, extra_join_hints=hints)

    ast = parse_check(sql, dialect)
    if not ast["parse_ok"]:
        ev.warn(f"生成 SQL 未能通过语法自检：{ast.get('error') or '解析不到输出表'}")
    else:
        ev.say(f"语法自检通过：解析出 {ast['statement_count']} 条语句、"
               f"{len(ast['input_tables'])} 张源表、{ast['column_lineage_count']} 条字段血缘")

    result: Dict[str, Any] = {
        "success": True,
        "mode": "L1",
        "dialect": dialect,
        "target_table": target,
        "target_table_known": target_known,
        "target_layer": tier,
        "source_tables": sources,
        "aliases": aliases,
        "partition": {"field": partition_field, "value": date} if partition_field else {},
        "group_by": group_refs,
        "columns": [c.to_dict() for c in columns],
        "joins": [j.to_dict() for j in joins],
        "sql": sql,
        "explain": ev.explain,
        "warnings": ev.warnings,
        "ast_check": ast,
        "db": str(kb.store.path),
        "graph_file": graph.name,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    _maybe_review(ctx, payload, result)
    return result


def _append_missing_columns(ctx: GenContext, target: str, target_fields: Sequence[Dict[str, Any]],
                           sources: Sequence[str], aliases: Dict[str, str],
                           columns: List[ColumnSpec], group_refs: Sequence[str],
                           aggregated: bool) -> List[ColumnSpec]:
    """按存量脚本字段血缘补齐尚未覆盖的目标列（补齐的列一律标注需人工确认）。"""
    kb, graph, ev = ctx.kb, ctx.graph, ctx.ev
    covered = {c.column for c in columns}
    out: List[ColumnSpec] = []
    index = {item.get("target_column"): item for item in graph.expressions_into(target, sources)}
    for field in target_fields:
        name = str(field.get("column_name"))
        if name in covered:
            continue
        item = index.get(name)
        if not item or not item.get("expression"):
            continue
        owner = item.get("source_table")
        if owner not in sources:
            continue
        old = detect_alias(item["expression"])
        body = strip_alias_suffix(rewrite_alias(item["expression"], old, aliases.get(owner, old))) \
            if old else strip_alias_suffix(item["expression"])
        spec = ColumnSpec(
            column=name, expression=body, chinese=str(field.get("chinese_name") or ""),
            role="passthrough", source="graph_expression", source_table=owner,
            evidence=[f"补齐列：沿用存量脚本字段血缘 {body}"
                      f"（来源 {', '.join(item.get('files') or []) or graph.name}）"],
            needs_review=True, note="按存量血缘补齐的列，需确认是否要保留",
        )
        out.append(spec)
        covered.add(name)
    if out:
        ev.warn(f"all_columns=true：额外按存量血缘补齐 {len(out)} 列（均标注需人工确认）："
                f"{', '.join(c.column for c in out[:10])}"
                f"{' 等' if len(out) > 10 else ''}")
        if aggregated:
            ev.warn("补齐的列未参与分组/聚合，与 GROUP BY 同时使用时可能语义不一致（Hive 严格模式下会报错），"
                    "请人工确认是否需要去掉聚合或改为 SUM(col)")
    return out


def _build_joins(ctx: GenContext, target: str, sources: Sequence[str],
                 aliases: Dict[str, str], payload: Dict[str, Any]) -> Tuple[List[JoinSpec], List[str]]:
    """拼接 JOIN：维表按「同名列推断关联键」，其它表给 TODO 提示。"""
    kb, graph, ev = ctx.kb, ctx.graph, ctx.ev
    joins: List[JoinSpec] = []
    hints: List[str] = []
    primary = sources[0] if sources else ""
    explicit = {str(j.get("table")): j for j in (payload.get("joins") or []) if isinstance(j, dict)}
    for table in sources[1:]:
        override = explicit.get(table) or explicit.get(kb.resolve(table))
        if override and override.get("on"):
            joins.append(JoinSpec(table=table, alias=aliases.get(table, ""),
                                  kind=str(override.get("kind") or "LEFT"),
                                  on=str(override["on"]), reason="调用方显式指定",
                                  confirmed=True))
            continue
        layer = kb.layer(table)
        key, why = infer_join_key(primary, table, kb=kb, graph=graph)
        if layer == "dim" and key:
            on = f"{aliases.get(primary, '')}.{key} = {aliases.get(table, '')}.{key}"
            confirmed = "**" not in why
            joins.append(JoinSpec(table=table, alias=aliases.get(table, ""), kind="LEFT", on=on,
                                  reason=f"维表关联键：{why}", confirmed=confirmed))
            if not confirmed:
                ev.warn(f"维表 {table} 的关联键 {why}，请人工确认")
        elif key:
            on = f"{aliases.get(primary, '')}.{key} = {aliases.get(table, '')}.{key}"
            joins.append(JoinSpec(table=table, alias=aliases.get(table, ""), kind="JOIN", on=on,
                                  reason=f"关联键：{why}；同层表关联可能放大粒度（**需人工确认**）",
                                  confirmed=False))
            ev.warn(f"同层源表 {table} 的关联键 {why}，请人工确认（可能放大粒度）")
        else:
            joins.append(JoinSpec(table=table, alias=aliases.get(table, ""), kind="CROSS", on="",
                                  reason="找不到同名列 / 命名规范可推断的关联键，关联条件待人工确认",
                                  confirmed=False))
            ev.warn(f"源表 {table} 与 {primary} 没有可推断的关联键，已生成 CROSS JOIN 占位，"
                    f"**必须手工补真实关联条件**")
    # 维表 JOIN 提示：目标表在血缘图里有维表上游时给出行内提示
    for edge in graph.edges_into(target):
        dim = edge.get("source") or ""
        if dim in sources or kb.layer(dim) != "dim":
            continue
        dim_cn = (kb.table(dim) or {}).get("chinese_name") or ""
        key, why = infer_join_key(primary, dim, kb=kb, graph=graph)
        if key:
            hints.append(f"可选维表（本次未加入生成）：{dim}{'（' + dim_cn + '）' if dim_cn else ''}"
                         f"  LEFT JOIN {dim} <别名> ON {aliases.get(primary, 't1')}.{key} = <别名>.{key}"
                         f"  -- 关联键：{why}")
        else:
            hints.append(f"可选维表（本次未加入生成）：{dim}{'（' + dim_cn + '）' if dim_cn else ''}"
                         f"  LEFT JOIN {dim} <别名> ON {aliases.get(primary, 't1')}.<关联键> = <别名>.<关联键>"
                         f"  -- 关联键需人工确认（血缘图不记录 ON 条件）")
    return joins, hints


def _maybe_review(ctx: GenContext, payload: Dict[str, Any], result: Dict[str, Any]) -> None:
    """可插拔 LLM 评审（不配置 key 时完全跳过，不影响结果）。"""
    enabled = payload.get("use_llm", "auto")
    llm = GenerateLLM(enabled=enabled)
    result["llm"] = llm.info()
    if not llm.available:
        result["llm"]["hint"] = "未配置 LLM_API_KEY（或 use_llm=false），已使用纯模板生成（离线可用）"
        return
    notes = llm.review_sql(result.get("sql") or "", "\n".join(result.get("explain") or [])[:3000])
    if notes:
        result["llm"]["notes"] = notes
    else:
        result["llm"]["hint"] = "LLM 评审无返回，已按模板结果输出"


# --------------------------------------------------------------------------- #
# 终端输出
# --------------------------------------------------------------------------- #
def format_sql_text(result: Dict[str, Any]) -> str:
    """CLI 文本输出（``generate sql``）。"""
    if not result.get("success"):
        return "生成失败：" + str(result.get("error") or "未知错误")
    lines: List[str] = ["=" * 72,
                        f"L1 单表加工 SQL 生成：{result['target_table']}（{result.get('target_layer')} 层）",
                        "=" * 72]
    for line in result.get("sql", "").splitlines():
        lines.append(line)
    lines.append("=" * 72)
    lines.append(f"生成列 {len(result.get('columns') or [])} 个 | "
                 f"源表 {', '.join(result.get('source_tables') or [])} | "
                 f"方言 {result.get('dialect')} | 耗时 {result.get('elapsed_seconds')}s")
    ast = result.get("ast_check") or {}
    lines.append(f"语法自检：{'✅ 通过' if ast.get('parse_ok') else '❌ 失败'}"
                 f"（语句 {ast.get('statement_count')} 条 / 源表 {len(ast.get('input_tables') or [])} 张 /"
                 f" 字段血缘 {ast.get('column_lineage_count')} 条）"
                 + (f"  {ast.get('error')}" if ast.get("error") else ""))
    lines.append("-" * 72)
    lines.append("为什么这么生成（explain）：")
    for idx, text in enumerate(result.get("explain") or [], start=1):
        lines.append(f"  [{idx}] {text}")
    if result.get("warnings"):
        lines.append("-" * 72)
        lines.append(f"需人工确认（{len(result['warnings'])} 项）：")
        for idx, text in enumerate(result["warnings"], start=1):
            lines.append(f"  ⚠ [{idx}] {text}")
    lines.append("=" * 72)
    return "\n".join(lines)
