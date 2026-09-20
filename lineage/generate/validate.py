"""L4：反向校验（生成的 SQL 回炉 → 血缘合并 → 体检 → 报告）。

校验项
------
============  ==============================================================  ==========
类型          判定                                                            级别
============  ==============================================================  ==========
parse         每段 SQL 能否被血缘引擎解析、能否解析出输出表                      error
cycle         合并后的链路是否成环（``LineageGraph.detect_cycles``）             error
layer_rule    是否跨层直连（分层差 > 1；维表除外）                              error/warning
dangling      产出表在链路内无人消费（且不是终点层）                             warning
orphan        输入表在链路内无上游（且不是源系统层）                            info
external      输入表是「链路外依赖」（stage 明确声明已有调度产出）                 info
kb_metric     产出表已登记口径是否都在生成 SQL 里出现 / 表达式是否与口径一致       warning
graph_diff    与 ``warehouse_graph.json`` 逐边对比：新链路 / 缺失链路              info/warning
============  ==============================================================  ==========

``passed = 没有任何 error 级问题``；结果会落一份与工作流模式同款的 HTML 体检报告。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..graph import LineageGraph, build_graph, table_layer
from ..knowledge import collect_target_fields, knowledge_unavailable, match_knowledge
from ..parser import SqlLineageParser
from ..report import make_report_id, report_bases, reports_dir, save_report
from .spec import (
    GraphIndex,
    SOURCE_LAYERS,
    TERMINAL_LAYERS,
    layer_cn,
    layer_of,
    open_store,
    step_ok,
)

__all__ = ["validate_generation", "format_validate_text"]

#: 合并链路里最多保留多少条字段血缘（避免响应过大）
MAX_COLUMNS = 400
#: 报告里最多列出的问题条数
MAX_ISSUES = 60


def _collect_stages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把各种入参形态归一成 ``[{name, sql, target_table, layer, depends_on, external_inputs}]``。"""
    stages: List[Dict[str, Any]] = []

    def add(item: Any, index: int) -> None:
        if isinstance(item, dict):
            sql = str(item.get("sql") or item.get("sql_text") or "").strip()
            stages.append({
                "name": str(item.get("name") or item.get("target_table") or f"stage{index}"),
                "sql": sql,
                "target_table": str(item.get("target_table") or ""),
                "layer": str(item.get("layer") or ""),
                "depends_on": [str(x) for x in (item.get("depends_on") or [])],
                "external_inputs": [str(x) for x in (item.get("external_inputs") or [])],
            })
        elif isinstance(item, str):
            stages.append({"name": f"sql{index}", "sql": item.strip(), "target_table": "",
                           "layer": "", "depends_on": [], "external_inputs": []})

    raw = payload.get("stages") or (payload.get("pipeline") or {}).get("stages") or []
    if raw:
        for idx, item in enumerate(raw, start=1):
            add(item, idx)
    for idx, text in enumerate(payload.get("sql_list") or [], start=len(stages) + 1):
        add(text, idx)
    if payload.get("sql"):
        add(payload["sql"], len(stages) + 1)
    if payload.get("sql_file"):
        path = Path(str(payload["sql_file"]))
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if text.lstrip().startswith(("{", "[")):
                try:
                    data = json.loads(text)
                    for idx, item in enumerate(data.get("stages") if isinstance(data, dict) else data,
                                              start=len(stages) + 1):
                        add(item, idx)
                except (ValueError, TypeError):
                    add(text, len(stages) + 1)
            else:
                add(text, len(stages) + 1)
    return [s for s in stages if s["sql"]]


def _norm_expr(expression: str) -> str:
    """表达式归一：去别名前缀 / AS 后缀 / 空白 / 大小写，便于口径比对。"""
    text = re.sub(r"--[^\n]*", " ", expression or "")
    text = re.sub(r"\b[A-Za-z_][A-Za-z0-9_]*\.", "", text)
    text = re.sub(r"\s+AS\s+[\w\u4e00-\u9fff]+", "", text, flags=re.I)
    text = re.sub(r"[\s`'\";]", "", text)
    return text.upper()


def _expected_expr(metric: Dict[str, Any]) -> str:
    """把知识库口径里的**中文业务名**换成真实列名后再归一，才能与生成的 SQL 表达式比对。

    例：``产销率 = ROUND(销量 / NULLIF(产量, 0), 4)`` + depends_on(销量->sale_qty, 产量->output_qty)
    -> ``ROUND(SALE_QTY/NULLIF(OUTPUT_QTY,0),4)``。
    """
    formula = str(metric.get("formula_full") or metric.get("formula") or "")
    body = formula.split("=", 1)[-1] if "=" in formula else formula
    deps = [d for d in (metric.get("depends_on") or []) if isinstance(d, dict)]
    pairs = []
    for dep in deps:
        chinese = str(dep.get("chinese_name") or "")
        column = str(dep.get("column") or "")
        if chinese and column:
            pairs.append((chinese, column))
    for chinese, column in sorted(pairs, key=lambda x: -len(x[0])):
        body = body.replace(chinese, column)
    return _norm_expr(body)


def validate_generation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """``POST /generate/validate`` 与 ``generate validate``：反向校验 + 体检报告。"""
    started = time.perf_counter()
    dialect = str(payload.get("dialect") or "hive")
    stages = _collect_stages(payload)
    if not stages:
        return {"success": False, "mode": "L4",
                "error": "没有可校验的 SQL：请传 stages / pipeline / sql_list / sql_file 之一"}

    global_graph = GraphIndex.load(payload.get("graph"))
    parser = SqlLineageParser(dialect=dialect)
    issues: List[Dict[str, Any]] = []
    statements: List[Dict[str, Any]] = []
    columns: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    stage_reports: List[Dict[str, Any]] = []
    produced: Dict[str, str] = {}
    produced_order: List[str] = []

    for stage in stages:
        try:
            parsed = parser.parse_sql(stage["sql"], source=stage["name"])
        except Exception as exc:  # noqa: BLE001 — 解析失败本身就是一条 error 级问题
            issues.append({
                "level": "error", "type": "parse", "stage": stage["name"],
                "message": f"{stage['name']} 的 SQL 无法解析：{type(exc).__name__}: {str(exc)[:200]}",
                "tables": [], "hint": "先用 `python -m lineage.cli <sql文件>` 单文件解析定位语法问题",
            })
            stage_reports.append({
                "name": stage["name"], "target_table": stage["target_table"],
                "output_tables": [], "statement_count": 0, "column_count": 0,
                "parsed": False, "sql_len": len(stage["sql"]),
                "depends_on": stage["depends_on"],
            })
            continue
        if not isinstance(parsed, list):
            parsed = [parsed]
        parsed = [p for p in parsed if isinstance(p, dict)]
        outputs: List[str] = []
        stmt_count = 0
        for stmt in parsed:
            stmt_count += len(stmt.get("statements") or []) or 1
            statements.append(stmt)
            outputs.extend(stmt.get("output_table_names") or [])
            for edge in stmt.get("table_lineage") or []:
                src, tgt = edge.get("source"), edge.get("target")
                if not src or not tgt:
                    continue
                columns_count = sum(1 for c in stmt.get("column_lineage") or []
                                    if c.get("target_table") == tgt and c.get("source_table") == src)
                existing = next((e for e in edges if e["source"] == src and e["target"] == tgt), None)
                if existing:
                    existing["column_mappings"] += columns_count
                    existing["stages"].append(stage["name"])
                else:
                    edges.append({"source": src, "target": tgt, "column_mappings": columns_count,
                                  "stages": [stage["name"]]})
            for col in stmt.get("column_lineage") or []:
                if len(columns) < MAX_COLUMNS:
                    columns.append(col)
        outputs = sorted(set(outputs))
        for table in outputs:
            produced.setdefault(table, stage["name"])
            if table not in produced_order:
                produced_order.append(table)
        ok = bool(outputs)
        if not ok:
            issues.append({
                "level": "error", "type": "parse", "stage": stage["name"],
                "message": f"第 {stages.index(stage) + 1} 段 SQL 未能解析出输出表（语句/方言/语法问题）",
                "tables": [], "hint": "用 `python -m lineage.cli <sql文件>` 单文件解析看看报错",
            })
        stage_reports.append({
            "name": stage["name"], "target_table": stage["target_table"] or (outputs[0] if outputs else ""),
            "output_tables": outputs, "statement_count": stmt_count,
            "column_count": sum(1 for c in columns if c.get("target_table") in outputs),
            "parsed": ok, "sql_len": len(stage["sql"]),
            "depends_on": stage["depends_on"],
        })

    input_tables: List[str] = []
    for edge in edges:
        if edge["source"] not in input_tables:
            input_tables.append(edge["source"])

    # ------------------------------------------------------------------ #
    # 1) 环路
    # ------------------------------------------------------------------ #
    graph = build_graph(statements, dialect=dialect)
    cycles = graph.detect_cycles()
    for cycle in cycles:
        issues.append({"level": "error", "type": "cycle",
                       "message": "链路成环：" + " → ".join(cycle.get("tables") or []),
                       "tables": cycle.get("tables") or []})

    # ------------------------------------------------------------------ #
    # 2) 分层规则（不得跨层直连）
    # ------------------------------------------------------------------ #
    layer_rules: List[Dict[str, Any]] = []
    transitions: Dict[str, int] = {}
    for edge in edges:
        s_layer, t_layer = layer_of(edge["source"]), layer_of(edge["target"])
        key = f"{s_layer} → {t_layer}"
        transitions[key] = transitions.get(key, 0) + 1
        if step_ok(s_layer, t_layer):
            continue
        level = "error" if s_layer not in ("dim",) and t_layer not in ("dim",) else "warning"
        item = {
            "level": level, "type": "layer_rule",
            "source": edge["source"], "target": edge["target"],
            "source_layer": s_layer, "target_layer": t_layer,
            "message": (f"跨层直连：{edge['source']}（{layer_cn(s_layer)}）→ "
                        f"{edge['target']}（{layer_cn(t_layer)}），跳过中间层"),
            "hint": "数仓规范要求逐层加工（src→ods→dwd→dws→ads）；如需直连请在评审记录里说明原因",
        }
        layer_rules.append(item)
        issues.append(item)

    # ------------------------------------------------------------------ #
    # 3) 断链 / 孤岛 / 链路外依赖
    # ------------------------------------------------------------------ #
    consumed: Set[str] = {e["source"] for e in edges}
    dangling: List[Dict[str, Any]] = []
    for table in produced_order:
        if table in consumed or layer_of(table) in TERMINAL_LAYERS:
            continue
        downstream = global_graph.downstream_tables(table) if global_graph.available else []
        item = {
            "level": "warning", "type": "dangling", "table": table,
            "layer": layer_of(table), "produced_by": [produced[table]],
            "cross_workflow": bool(downstream), "global_downstream": downstream[:5],
            "message": (f"产出表 {table} 在本链路内没有下游消费者"
                        + (f"；全局血缘显示下游在别处：{', '.join(downstream[:3])}" if downstream else "")),
        }
        dangling.append(item)
        issues.append(item)

    external_declared: Set[str] = set()
    for stage in stages:
        external_declared.update(stage.get("external_inputs") or [])
    orphans: List[Dict[str, Any]] = []
    for table in input_tables:
        if table in produced or layer_of(table) in SOURCE_LAYERS:
            continue
        if table in external_declared:
            orphans.append({"level": "info", "type": "external", "table": table,
                            "layer": layer_of(table),
                            "message": f"输入表 {table} 是链路外依赖（stage 已声明它由既有调度产出）"})
            continue
        upstream = global_graph.upstream_tables(table) if global_graph.available else []
        orphans.append({
            "level": "info" if upstream else "warning", "type": "orphan", "table": table,
            "layer": layer_of(table), "global_upstream": upstream[:5],
            "message": (f"输入表 {table} 在本链路内没有上游"
                        + (f"；全局血缘显示上游是：{', '.join(upstream[:3])}" if upstream
                           else "；血缘图里也查不到上游，可能是外部系统直灌")),
        })
    issues.extend(orphans)

    # ------------------------------------------------------------------ #
    # 4) 口径一致性（与知识库逐条比对）
    # ------------------------------------------------------------------ #
    kb_available = False
    knowledge: Dict[str, Any] = knowledge_unavailable("本次未做知识库匹配")
    metric_issues: List[Dict[str, Any]] = []
    metric_check: List[Dict[str, Any]] = []
    store = None
    try:
        store = open_store(payload.get("db"))
        counts = store.counts()
        kb_available = bool(counts.get("kb_metrics"))
    except FileNotFoundError as exc:
        knowledge = knowledge_unavailable(str(exc))
    if store is not None and kb_available:
        try:
            knowledge = match_knowledge(store, collect_target_fields(columns), list(produced),
                                        limit_metrics=int(payload.get("limit_metrics") or 20),
                                        limit_rules=5)
            for table in produced_order:
                metrics = store.metrics(table)
                generated = {str(c.get("target_column")): c for c in columns
                             if c.get("target_table") == table}
                if not metrics:
                    metric_issues.append({
                        "level": "info", "type": "kb_metric", "table": table,
                        "message": f"产出表 {table} 在知识库里没有登记口径（新表或尚未提炼）",
                    })
                    continue
                missing, matched, mismatch = [], [], []
                for metric in metrics:
                    col = str(metric.get("metric_name") or "")
                    want_raw = str(metric.get("formula_full") or metric.get("formula") or "")
                    want = _expected_expr(metric)
                    target_col = col
                    if not target_col or target_col not in generated:
                        # 口径列可能被改名：按归一化后的表达式内容反查
                        for name, item in generated.items():
                            got = _norm_expr(item.get("expression") or "")
                            if want and got and (want in got or got in want):
                                target_col = name
                                break
                    if target_col not in generated:
                        missing.append({"metric": metric.get("chinese_name") or col,
                                        "column": col, "formula": metric.get("formula")})
                        continue
                    got = _norm_expr(generated[target_col].get("expression") or "")
                    body = want.split("=")[-1]
                    row = {"metric": metric.get("chinese_name") or col, "column": target_col,
                           "kb_formula": metric.get("formula"),
                           "expected_columns": want_raw,
                           "generated": generated[target_col].get("expression")}
                    if body and got and (body in got or got in body):
                        matched.append(row)
                    else:
                        mismatch.append(row)
                        metric_issues.append({
                            "level": "warning", "type": "kb_metric", "table": table,
                            "message": f"{table}.{target_col} 的生成表达式与知识库登记口径写法不同，请复核",
                            "kb_formula": metric.get("formula"),
                            "generated": generated[target_col].get("expression")})
                metric_check.append({
                    "table": table, "metric_count": len(metrics),
                    "matched": len(matched), "mismatch": len(mismatch),
                    "missing_columns": missing[:10],
                })
                for miss in missing[:6]:
                    metric_issues.append({
                        "level": "warning", "type": "kb_metric", "table": table,
                        "message": (f"生成 SQL 缺少知识库已登记口径列 "
                                    f"{table}.{miss['column']}（{miss['metric']}：{miss['formula']}）"),
                    })
        except Exception as exc:  # noqa: BLE001 — 口径比对失败不能影响主流程
            metric_issues.append({"level": "info", "type": "kb_metric",
                                  "message": f"知识库比对跳过：{type(exc).__name__}: {exc}"})
        finally:
            store.close()
    issues.extend(metric_issues)

    # ------------------------------------------------------------------ #
    # 5) 与 warehouse_graph.json 逐边对比
    # ------------------------------------------------------------------ #
    graph_diff: Dict[str, Any] = {"available": global_graph.available, "graph_file": global_graph.name}
    if global_graph.available:
        known, new_edges, missing_edges = [], [], []
        for edge in edges:
            src, tgt = edge["source"], edge["target"]
            if global_graph.edge(src, tgt):
                known.append({"source": src, "target": tgt})
            else:
                new_edges.append({"source": src, "target": tgt})
        chain_tables = set(produced_order) | set(input_tables)
        for edge in global_graph.data.get("edges") or []:
            src, tgt = edge.get("source"), edge.get("target")
            if src in chain_tables and tgt in chain_tables and \
                    not any(e["source"] == src and e["target"] == tgt for e in edges):
                missing_edges.append({"source": src, "target": tgt})
        graph_diff.update({
            "known_edges": known, "new_edges": new_edges, "missing_edges": missing_edges[:20],
            "known_count": len(known), "new_count": len(new_edges),
            "missing_count": len(missing_edges),
        })
        if new_edges:
            issues.append({"level": "info", "type": "graph_diff",
                           "message": f"{len(new_edges)} 条边是血缘图里没有的新链路："
                                      + "、".join(f"{e['source']}→{e['target']}" for e in new_edges[:5]),
                           "tables": [e["target"] for e in new_edges[:5]]})
        if missing_edges:
            issues.append({"level": "warning", "type": "graph_diff",
                           "message": f"{len(missing_edges)} 条全局血缘里的边没出现在本次链路中："
                                      + "、".join(f"{e['source']}→{e['target']}" for e in missing_edges[:5]),
                           "tables": [e["target"] for e in missing_edges[:5]]})

    errors = [i for i in issues if i.get("level") == "error"]
    warnings_ = [i for i in issues if i.get("level") == "warning"]
    passed = not errors

    merged = {
        "node_count": len(set(produced_order) | set(input_tables)),
        "edge_count": len(edges),
        "input_tables": sorted(input_tables),
        "output_tables": produced_order,
        "table_lineage": edges,
        "column_lineage": columns,
        "column_lineage_count": sum(1 for row in statements for _ in (row.get("column_lineage") or [])),
        "layer_transitions": transitions,
        "chain": produced_order,
        "statements": [],
    }
    result: Dict[str, Any] = {
        "success": True,
        "mode": "L4",
        "passed": passed,
        "dialect": dialect,
        "stage_count": len(stages),
        "stages": stage_reports,
        "issues": issues[:MAX_ISSUES],
        "issue_count": {"error": len(errors), "warning": len(warnings_),
                        "info": len(issues) - len(errors) - len(warnings_)},
        "layer_rules": layer_rules,
        "metric_check": metric_check,
        "graph_diff": graph_diff,
        "merged": merged,
        "knowledge_available": kb_available,
        "graph_file": global_graph.name,
        "summary": ("✅ 校验通过：" if passed else "❌ 校验未通过：")
                   + f"{len(stages)} 段 SQL / {len(edges)} 条表级边 / {merged['column_lineage_count']} 条字段血缘；"
                   + f"error {len(errors)} / warning {len(warnings_)} / info "
                   f"{len(issues) - len(errors) - len(warnings_)}",
    }

    # ------------------------------------------------------------------ #
    # 6) 体检报告
    # ------------------------------------------------------------------ #
    if payload.get("make_report", True) not in (False, "false", "False", 0, "0"):
        info, err = _attach_report(payload, result, statements, columns, edges, knowledge,
                                   produced_order, kb_available, started)
        if info:
            result.update({"report_id": info["report_id"], "url": info["url"],
                           "internal_url": info["internal_url"], "report_path": info["path"]})
        else:
            result["report_error"] = err

    result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return result


def _attach_report(payload: Dict[str, Any], result: Dict[str, Any], statements: Sequence[Dict[str, Any]],
                   columns: Sequence[Dict[str, Any]], edges: Sequence[Dict[str, Any]],
                   knowledge: Dict[str, Any], produced_order: Sequence[str], kb_available: bool,
                   started: float) -> Tuple[Optional[Dict[str, Any]], str]:
    """落一份「生成链路体检报告」（复用工作流级报告模板 → 零额外前端代码）。"""
    try:
        parsed = {
            "dialect": result["dialect"],
            "statement_count": len(statements),
            "input_tables": result["merged"]["input_tables"],
            "output_tables": list(produced_order),
            "table_lineage": list(edges),
            "column_lineage": list(columns),
            "column_lineage_count": result["merged"]["column_lineage_count"],
            "statements": [],
            "knowledge": knowledge,
        }
        public_base, _internal = report_bases()
        quality = {
            "dangling_outputs": [i for i in result["issues"] if i.get("type") == "dangling"],
            "orphan_inputs": [i for i in result["issues"] if i.get("type") == "orphan"],
            "cycles": [i for i in result["issues"] if i.get("type") == "cycle"],
            "missing_knowledge": [i for i in result["issues"]
                                  if i.get("type") == "kb_metric" and "没有登记口径" in str(i.get("message"))],
            "summary": {
                "dangling_output_count": sum(1 for i in result["issues"] if i.get("type") == "dangling"),
                "orphan_input_count": sum(1 for i in result["issues"] if i.get("type") == "orphan"),
                "cycle_count": sum(1 for i in result["issues"] if i.get("type") == "cycle"),
                "missing_knowledge_count": sum(1 for i in result["issues"]
                                               if i.get("type") == "kb_metric"
                                               and "没有登记口径" in str(i.get("message"))),
                "checked": True,
            },
        }
        workflow = {
            "name": str(payload.get("name") or "生成链路反向校验（L4）"),
            "project_code": str(payload.get("project_code") or ""),
            "code": str(payload.get("workflow_name") or ""),
            "task_count": result["stage_count"],
            "parsed_task_count": sum(1 for s in result["stages"] if s.get("parsed")),
            "chain": list(result["merged"]["chain"]),
            "tasks": [
                {
                    "name": s["name"], "type": "SQL(生成)",
                    "script_len": s["sql_len"], "statement_count": s["statement_count"],
                    "output_tables": s["output_tables"], "parsed": s.get("parsed"),
                    "errors": [i["message"] for i in result["issues"]
                               if i.get("stage") == s["name"]][:2],
                    "metric_count": next((m["metric_count"] for m in result["metric_check"]
                                          if m["table"] == (s["target_table"] or "")), 0),
                }
                for s in result["stages"]
            ],
            "quality": quality,
            "layer_rules": result.get("layer_rules") or [],
            "issue_summary": result.get("issue_count") or {},
        }
        meta = {
            "task_name": workflow["name"],
            "mode": "generate-validate",
            "dialect": result["dialect"],
            "cost_ms": int((time.perf_counter() - started) * 1000),
            "statement_count": len(statements),
            "service_url": f"{public_base}/generate/validate",
            "sql": "",
            "workflow": workflow,
        }
        info = save_report(parsed, meta, directory=reports_dir(payload),
                           report_id=make_report_id(
                               f"generate-validate:{payload.get('name') or ''}:"
                               f"{','.join(produced_order[:3])}"))
        return info, ""
    except Exception as exc:  # noqa: BLE001 — 报告是附加产物，失败不影响校验结果
        return None, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# 终端输出
# --------------------------------------------------------------------------- #
def format_validate_text(result: Dict[str, Any]) -> str:
    if not result.get("success"):
        return "校验失败：" + str(result.get("error") or "未知错误")
    lines = ["=" * 72, "L4 反向校验（生成 SQL 回炉 → 血缘合并 → 体检）", "=" * 72,
             result.get("summary") or "",
             f"校验段数：{result.get('stage_count')}；"
             f"合并链路：{result['merged']['edge_count']} 条表级边 / "
             f"{result['merged']['column_lineage_count']} 条字段血缘"]
    transitions = result["merged"].get("layer_transitions") or {}
    if transitions:
        lines.append("分层流向：" + "  ".join(f"{k}×{v}" for k, v in sorted(transitions.items())))
    lines.append("-" * 72)
    if result.get("layer_rules"):
        lines.append(f"分层规则违规（{len(result['layer_rules'])} 条）：")
        for item in result["layer_rules"]:
            lines.append(f"  ❌ {item['message']}")
    else:
        lines.append("分层规则：✅ 无跨层直连（src→ods→dwd→dws→ads 逐层加工）")
    if result.get("metric_check"):
        lines.append("知识库口径一致性：")
        for item in result["metric_check"]:
            lines.append(f"  - {item['table']}：口径 {item['metric_count']} 条，"
                         f"一致 {item['matched']} / 不一致 {item['mismatch']} / "
                         f"缺失列 {len(item['missing_columns'])}")
    diff = result.get("graph_diff") or {}
    if diff.get("available"):
        lines.append(f"与 {diff.get('graph_file')} 对比：已知边 {diff.get('known_count')} / "
                     f"新链路 {diff.get('new_count')} / 缺失边 {diff.get('missing_count')}")
    lines.append("-" * 72)
    counts = result.get("issue_count") or {}
    lines.append(f"问题清单（error {counts.get('error', 0)} / warning {counts.get('warning', 0)} / "
                 f"info {counts.get('info', 0)}）：")
    for idx, issue in enumerate(result.get("issues") or [], start=1):
        mark = {"error": "❌", "warning": "⚠", "info": "·"}.get(issue.get("level"), "·")
        lines.append(f"  {mark} [{issue.get('type')}] {issue.get('message')}")
    if result.get("url"):
        lines.append("-" * 72)
        lines.append(f"体检报告：{result['url']}")
        lines.append(f"（容器/任务内用：{result.get('internal_url')}）")
    lines.append("=" * 72)
    return "\n".join(lines)
