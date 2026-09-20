#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作流级血缘分析（DolphinScheduler 任务类型 ``LINEAGE_DAG`` 的服务端实现）。

为什么要有它
------------
单脚本任务（``LINEAGE`` / ``POST /analyze``）只看**一条 SQL**：知道「这张表从哪来」，
但不知道「这条链路在工作流里是怎么串起来的」。而数仓里真正的问题几乎都在**工作流级别**：
``wf_dwd_清洗`` 产出 4 张 DWD 表 → ``wf_dws_汇总`` 消费其中 3 张又要自己产出 4 张……
一条链路断在哪、哪张表没人消费、哪张表没有口径登记，只有把**整个工作流的脚本一次性拉下来
批量解析**才看得出来。

``LINEAGE_DAG`` 的用法与价值：**历史工作流零改造** —— 原有 N 个任务一行不改，
只在尾部挂 1 个 ``LINEAGE_DAG`` 节点，它自己通过海豚 OpenAPI 拉取本工作流的全部任务脚本。

流程（本模块）
--------------
1. 登录海豚 OpenAPI（``DS_API_BASE`` / ``DS_API_USER`` / ``DS_API_PASSWORD``，默认
   ``http://localhost:12345/dolphinscheduler`` + ``admin`` / ``dolphinscheduler123``）；
2. 拉工作流定义 → 遍历 ``taskDefinitionList``，按 ``task_types`` 过滤并抽取脚本
   （SQL 任务取 ``taskParams.sql``，SHELL / PYTHON 取 ``rawScript``，顺带 pre/post 语句）；
3. 逐个脚本走本地解析器 :class:`lineage.parser.SqlLineageParser`（不走 HTTP，省一轮往返）
   + 知识库口径匹配（复用 :func:`lineage.knowledge.match_knowledge`）；
4. **合并**成工作流级血缘：按表名把各任务的产出 / 消费串起来，得到跨任务链路；
5. **质量体检**：断链（产出无人消费）/ 孤岛（输入无上游）/ 环路 / 未登记口径；
6. 返回结构化结果，并（默认）落一份 HTML 报告供浏览器点开。

设计约束
--------
* 零新增第三方依赖：复用 ``lineage.ds_client``（urllib）与 ``lineage.parser``（sqlglot）；
* **任何单点失败都降级**：某个任务脚本解析失败只记 ``errors``，不影响其它任务；
  知识库 / 报告失败也只是少一段内容，血缘结果照常返回；
* 表名前缀即分层（``ods.`` / ``cdw.`` / ``ads.``），不需要额外的元数据表。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from lineage.ds_client import (
    DsClient,
    DsError,
    extract_scripts,
    referenced_definition_codes,
)
from lineage.knowledge import (
    KnowledgeStore,
    collect_target_fields,
    default_db_path,
    knowledge_unavailable,
    match_knowledge,
)
from lineage.parser import DEFAULT_DIALECT, SqlLineageParser
from lineage.report import make_report_id, report_bases, reports_dir, save_report

__all__ = [
    "DEFAULT_TASK_TYPES",
    "analyze_workflow",
    "workflow_definition",
]

#: 默认解析哪些任务类型的脚本（海豚任务类型名，大写）
DEFAULT_TASK_TYPES: Tuple[str, ...] = ("SQL", "SHELL", "PYTHON")

#: 视为「贴源层」的表前缀：它们本来就没有上游，不算孤岛输入
SOURCE_LAYERS: Tuple[str, ...] = ("src", "ods", "dim", "ext", "stage")

#: 视为「应用层」的表前缀：应用层是链路终点，产出无人消费属正常，不算断链
TERMINAL_LAYERS: Tuple[str, ...] = ("ads", "app", "rpt", "report")

#: 递归展开子流程（``include_sub_process``）的最大深度
MAX_SUB_PROCESS_DEPTH = 3
#: 环路检测扫描的最大表数（防御性上限，正常数仓不会触顶）
MAX_CYCLE_SCAN_NODES = 400
#: 一次最多返回多少个环路
MAX_CYCLES = 5
#: 工作流级口径匹配条数上限（单脚本是 12，工作流面更宽，给到 24）
MAX_METRICS = 24


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _flag(value: Any, default: bool = False) -> bool:
    """宽松布尔解析（JSON true / ``"true"`` / 1 / ``"on"`` 都认）。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value in (None, "", 0, "0"):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _layer(table: str) -> str:
    """表名前缀当成分层名（``cdw.dwd_产量明细`` → ``cdw``）。"""
    name = (table or "").strip()
    return name.split(".")[0].lower() if "." in name else ""


def _in_layer(table: str, layers: Sequence[str]) -> bool:
    return _layer(table) in {lay.lower() for lay in layers}


def _unique(items: Iterable[Any]) -> List[Any]:
    out: List[Any] = []
    for item in items:
        if item not in (None, "") and item not in out:
            out.append(item)
    return out


def _task_types(payload: Dict[str, Any]) -> List[str]:
    """解析 ``task_types``：支持 ``["SQL","SHELL"]`` 或 ``"SQL,SHELL"``。"""
    raw = payload.get("task_types")
    if raw is None:
        raw = payload.get("taskTypes")
    if raw is None:
        return list(DEFAULT_TASK_TYPES)
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw]
    else:
        parts = [str(raw).strip()]
    out = [p.upper() for p in parts if p]
    return out or list(DEFAULT_TASK_TYPES)


# --------------------------------------------------------------------------- #
# 1) 拉取工作流定义
# --------------------------------------------------------------------------- #
def _ds_client(payload: Dict[str, Any]) -> DsClient:
    """按请求体 > 环境变量 > 默认值 的顺序构造海豚客户端。"""
    base = (payload.get("ds_base") or payload.get("ds_api_base")
            or os.environ.get("DS_API_BASE") or os.environ.get("DS_BASE_URL") or "")
    user = (payload.get("ds_user") or payload.get("ds_api_user")
            or os.environ.get("DS_API_USER") or os.environ.get("DS_USER") or "")
    password = (payload.get("ds_password") or payload.get("ds_api_password")
                or os.environ.get("DS_API_PASSWORD") or os.environ.get("DS_PASSWORD") or "")
    kwargs: Dict[str, Any] = {}
    if str(base).strip():
        kwargs["base_url"] = str(base).strip()
    if str(user).strip():
        kwargs["user"] = str(user).strip()
    if str(password).strip():
        kwargs["password"] = str(password)
    timeout = payload.get("ds_timeout")
    if timeout:
        kwargs["timeout"] = float(timeout)
    return DsClient(**kwargs)


def workflow_definition(
    client: DsClient,
    project_code: Any,
    define_code: Any,
    include_sub_process: bool = False,
    _depth: int = 0,
    _seen: Optional[Set[int]] = None,
) -> Dict[str, Any]:
    """拉一个工作流定义，可选递归展开子流程任务（``SUB_PROCESS`` / ``DEPENDENT``）。"""
    seen = _seen if _seen is not None else set()
    detail = client.get_process_definition(project_code, define_code)
    definition = detail.get("processDefinition") or {}
    tasks: List[Dict[str, Any]] = []
    for task in detail.get("taskDefinitionList") or []:
        if isinstance(task, dict):
            tasks.append({"task": task, "from_workflow": definition.get("name") or ""})
    if include_sub_process and _depth < MAX_SUB_PROCESS_DEPTH:
        for item in list(tasks):
            for ref in referenced_definition_codes(item["task"]):
                sub_code = _int_or_none(ref.get("definition_code"))
                if not sub_code or sub_code in seen:
                    continue
                seen.add(sub_code)
                try:
                    sub = workflow_definition(client, project_code, sub_code,
                                              include_sub_process=True,
                                              _depth=_depth + 1, _seen=seen)
                except DsError:
                    continue
                tasks.extend(sub["tasks"])
    return {
        "name": definition.get("name") or "",
        "code": definition.get("code") or _int_or_none(define_code),
        "version": definition.get("version"),
        "release_state": definition.get("releaseState"),
        "project_code": _int_or_none(project_code),
        "tasks": tasks,
    }


def _resolve_workflows(client: DsClient, payload: Dict[str, Any], scope: str) -> List[Dict[str, Any]]:
    """定位要分析的工作流：``project_code`` + ``process_define_code`` / ``workflow_name``。

    ``scope=project`` 时把项目下**所有**工作流一起分析（跨工作流链路）；
    没给 ``project_code`` 时按 ``workflow_name`` 在所有项目里找。
    """
    project_code = _int_or_none(payload.get("project_code") or payload.get("projectCode"))
    define_code = _int_or_none(payload.get("process_define_code") or payload.get("processDefinitionCode"))
    name = str(payload.get("workflow_name") or payload.get("name") or "").strip()
    include_sub = _flag(payload.get("include_sub_process") or payload.get("includeSubProcess"))

    if scope == "project" and project_code:
        out = []
        for definition in client.list_process_definitions(project_code):
            code = _int_or_none(definition.get("code"))
            if not code:
                continue
            out.append(workflow_definition(client, project_code, code, include_sub))
        return out

    if project_code and define_code:
        return [workflow_definition(client, project_code, define_code, include_sub)]
    if project_code and name:
        for definition in client.list_process_definitions(project_code):
            if definition.get("name") == name:
                code = _int_or_none(definition.get("code"))
                if code:
                    return [workflow_definition(client, project_code, code, include_sub)]
        raise DsError(f"项目 {project_code} 下找不到工作流「{name}」")
    if name:
        for project in client.list_projects():
            code = _int_or_none(project.get("code"))
            if not code:
                continue
            for definition in client.list_process_definitions(code):
                if definition.get("name") == name:
                    sub = _int_or_none(definition.get("code"))
                    if sub:
                        return [workflow_definition(client, code, sub, include_sub)]
        raise DsError(f"所有项目里都找不到工作流「{name}」")
    raise DsError("缺少 project_code + process_define_code（或 workflow_name）")


# --------------------------------------------------------------------------- #
# 2) 批量解析
# --------------------------------------------------------------------------- #
def _parse_tasks(task_items: Sequence[Dict[str, Any]], dialect: str,
                 task_types: Sequence[str]) -> List[Dict[str, Any]]:
    """逐个任务抽取脚本并解析（单任务失败只记 ``errors``，不打断整体）。"""
    parser = SqlLineageParser(dialect=dialect)
    wanted = {t.upper() for t in task_types}
    results: List[Dict[str, Any]] = []

    for item in task_items:
        task = item.get("task") or {}
        entry: Dict[str, Any] = {
            "name": task.get("name") or "",
            "code": task.get("code"),
            "type": str(task.get("taskType") or "").upper(),
            "from_workflow": item.get("from_workflow") or "",
            "parsed": False,
            "script_keys": [],
            "script_len": 0,
            "statement_count": 0,
            "column_count": 0,
            "metric_count": 0,
            "input_tables": [],
            "output_tables": [],
            "errors": [],
            "_scripts": [],
            "_columns": [],
            "_statements": [],
        }
        if entry["type"] not in wanted:
            entry["errors"].append(
                f"跳过：任务类型 {entry['type']} 不在本次解析范围（{','.join(sorted(wanted))}）")
            results.append(entry)
            continue

        for script in extract_scripts(task):
            text = script.get("text") or ""
            entry["_scripts"].append(script)
            entry["script_keys"].append(script.get("key") or "")
            entry["script_len"] += len(text)
            try:
                statements = parser.parse_sql(text, source=entry["name"]) or []
            except Exception as e:  # noqa: BLE001 — 单条脚本失败不能拖垮整个工作流
                entry["errors"].append(f"{script.get('key')}: {type(e).__name__}: {e}")
                continue
            for st in statements:
                if not isinstance(st, dict):
                    continue
                entry["statement_count"] += 1
                entry["input_tables"].extend(st.get("input_table_names") or [])
                entry["output_tables"].extend(st.get("output_table_names") or [])
                columns = []
                for col in st.get("column_lineage") or []:
                    if not isinstance(col, dict):
                        continue
                    enriched = dict(col)
                    enriched["task"] = entry["name"]
                    enriched["task_type"] = entry["type"]
                    columns.append(enriched)
                entry["_columns"].extend(columns)
                statement = dict(st)
                statement["task"] = entry["name"]
                entry["_statements"].append(statement)

        entry["input_tables"] = sorted(set(entry["input_tables"]))
        entry["output_tables"] = sorted(set(entry["output_tables"]))
        entry["column_count"] = len(entry["_columns"])
        entry["parsed"] = entry["statement_count"] > 0
        results.append(entry)
    return results


# --------------------------------------------------------------------------- #
# 3) 合并 + 链路
# --------------------------------------------------------------------------- #
def _merge(tasks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把各任务的血缘合并成工作流级：表级边、字段级、任务级依赖。"""
    edge_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    column_lineage: List[Dict[str, Any]] = []
    statements: List[Dict[str, Any]] = []
    producers: Dict[str, List[str]] = {}
    consumers: Dict[str, List[str]] = {}

    for task in tasks:
        for table in task["output_tables"]:
            producers.setdefault(table, []).append(task["name"])
        for table in task["input_tables"]:
            consumers.setdefault(table, []).append(task["name"])
        for st in task["_statements"]:
            statements.append(st)
            for edge in st.get("table_lineage") or []:
                src, tgt = edge.get("source"), edge.get("target")
                if not src or not tgt:
                    continue
                record = edge_map.setdefault((src, tgt), {"source": src, "target": tgt, "tasks": []})
                if task["name"] not in record["tasks"]:
                    record["tasks"].append(task["name"])
        column_lineage.extend(task["_columns"])

    # 任务级依赖：A 产出表 t，B 消费表 t ⇒ A -> B
    task_edges: Dict[Tuple[str, str], List[str]] = {}
    for table, producer_list in producers.items():
        for consumer in consumers.get(table, []):
            for producer in producer_list:
                if producer == consumer:
                    continue
                key = (producer, consumer)
                via = task_edges.setdefault(key, [])
                if table not in via:
                    via.append(table)

    nodes = sorted({t for pair in edge_map for t in pair})
    edges = [edge_map[key] for key in sorted(edge_map)]
    return {
        "table_lineage": edges,
        "nodes": nodes,
        "edges": [{"source": e["source"], "target": e["target"]} for e in edges],
        "column_lineage": column_lineage,
        "statements": statements,
        "producers": producers,
        "consumers": consumers,
        "task_lineage": [
            {"source_task": src, "target_task": tgt, "via_tables": vias}
            for (src, tgt), vias in sorted(task_edges.items())
        ],
    }


def _topo_order(nodes: Sequence[str], edges: Sequence[Dict[str, Any]]) -> List[str]:
    """Kahn 拓扑排序；有环时只返回能排出来的部分（剩下的节点就是环路候选）。"""
    adj: Dict[str, List[str]] = {n: [] for n in nodes}
    indeg: Dict[str, int] = {n: 0 for n in nodes}
    for edge in edges:
        src, tgt = edge.get("source"), edge.get("target")
        if not src or not tgt or src == tgt:
            continue
        adj.setdefault(src, []).append(tgt)
        indeg.setdefault(tgt, 0)
        indeg[tgt] = indeg.get(tgt, 0) + 1
        indeg.setdefault(src, indeg.get(src, 0))
    queue = sorted(n for n in adj if indeg.get(n, 0) == 0)
    order: List[str] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for nxt in adj.get(node, []):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    return order


def _longest_chain(merged: Dict[str, Any], limit: int = 40) -> List[str]:
    """最长表级链路（拓扑顺序上的 DP），例如 ``src.a → ods.b → cdw.dwd_c``。

    长度相同时优先从贴源层（``src`` / ``ods``）起始，其次是非 ``dim`` 层 ——
    维表只是 JOIN 的旁支，不代表主链路。
    """
    nodes = merged["nodes"]
    edges = merged["edges"]
    if not nodes:
        return []
    best: Dict[str, List[str]] = {n: [n] for n in nodes}
    adj: Dict[str, List[str]] = {n: [] for n in nodes}
    for edge in edges:
        if edge["source"] in adj and edge["target"] not in adj[edge["source"]]:
            adj[edge["source"]].append(edge["target"])

    def score(path: List[str]) -> Tuple[int, int]:
        start_layer = _layer(path[0]) if path else ""
        rank = 0 if start_layer in ("src", "ods") else (2 if start_layer == "dim" else 1)
        return (len(path), -rank)

    chain: List[str] = []
    for node in _topo_order(nodes, edges):
        if score(best.get(node, [])) > score(chain):
            chain = best[node]
        for nxt in adj.get(node, []):
            candidate = best[node] + [nxt]
            if score(candidate) > score(best.get(nxt, [])):
                best[nxt] = candidate
    for node in nodes:  # 环路里的节点不会进拓扑序，单独兜一次底
        if score(best.get(node, [])) > score(chain):
            chain = best[node]
    return chain[:limit]


# --------------------------------------------------------------------------- #
# 3b) 全局血缘图（warehouse_graph.json）：把工作流内的链路接到 src / ads 两端
# --------------------------------------------------------------------------- #
def _global_graph() -> Any:
    """加载仓库级血缘图（拿不到就返回 None，所有拼链逻辑自动降级）。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "warehouse_graph.json")
    if not os.path.exists(path):
        return None
    try:
        from lineage.graph import LineageGraph
        with open(path, encoding="utf-8") as f:
            return LineageGraph.from_dict(json.load(f))
    except Exception:  # noqa: BLE001 — 全局图只是锦上添花
        return None


def _pick_path(paths: Sequence[Sequence[str]], prefer_src: bool = True) -> List[str]:
    """从多条路径里挑最「值钱」的一条：越长越好，同长优先含 ``src`` 层。"""
    best: List[str] = []
    best_key: Tuple[int, int] = (-1, -1)
    for path in paths or []:
        if not isinstance(path, (list, tuple)) or len(path) < 2:
            continue
        nodes = [str(p) for p in path if p]
        key = (len(nodes), 1 if (prefer_src and any(_layer(n) == "src" for n in nodes)) else 0)
        if key > best_key:
            best, best_key = nodes, key
    return best


def _global_upstream(graph: Any, table: str) -> List[str]:
    if graph is None or not table:
        return []
    try:
        res = graph.upstream(table, depth=8) or {}
    except Exception:  # noqa: BLE001
        return []
    return _pick_path(res.get("paths") or []) or []


def _global_downstream(graph: Any, table: str) -> List[str]:
    if graph is None or not table:
        return []
    try:
        res = graph.downstream(table, depth=8) or {}
    except Exception:  # noqa: BLE001
        return []
    return _pick_path(res.get("paths") or []) or []


def _global_neighbours(graph: Any, table: str, direction: str, limit: int = 5) -> List[str]:
    if graph is None or not table:
        return []
    try:
        fn = graph.downstream if direction == "downstream" else graph.upstream
        res = fn(table, depth=6) or {}
    except Exception:  # noqa: BLE001
        return []
    return [str(t) for t in (res.get("tables") or [])][:limit]


def _stitch_chain(inner: Sequence[str], graph: Any, limit: int = 12) -> Dict[str, Any]:
    """把「工作流内链路」接到全局图上：向上补 ``src`` / ``ods``。

    这样 ``wf_dwd_清洗`` 的链路不会再从 ``ods.`` 开始，而是
    ``src.erp_生产工单明细 → ods.ods_卷烟产量流水 → cdw.dwd_卷烟产量明细``。
    另外还顺手记下「本工作流产出之后流向哪里（别的表 / 别的工作流）」，
    放在 ``downstream`` 里供报告与日志参考，但**不并进主链路** ——
    主链路的终点必须是本工作流的产出。
    """
    inner = [t for t in inner if t]
    if not inner or graph is None:
        return {"chain": list(inner), "upstream": [], "downstream": []}

    upstream_path = _global_upstream(graph, inner[0])
    downstream_path = _global_downstream(graph, inner[-1])
    # 全局图里可能同时有 src / dim 两条上游：[表, ods, src] 这种才是主干
    upstream = [t for t in reversed(upstream_path[1:]) if _layer(t) != "dim"]
    downstream = [t for t in downstream_path[1:] if _layer(t) != "dim"][:5]
    full = (upstream + list(inner))[:limit]
    return {"chain": full, "upstream": upstream, "downstream": downstream}


def _find_cycles(nodes: Sequence[str], edges: Sequence[Dict[str, Any]], limit: int = MAX_CYCLES) -> List[List[str]]:
    """DFS 找出表级依赖里的环路（每个环只报一次，起点任意）。"""
    if len(nodes) > MAX_CYCLE_SCAN_NODES:
        nodes = list(nodes)[:MAX_CYCLE_SCAN_NODES]
    allowed = set(nodes)
    adj: Dict[str, List[str]] = {n: [] for n in nodes}
    for edge in edges:
        src, tgt = edge.get("source"), edge.get("target")
        if src in allowed and tgt in allowed and src != tgt and tgt not in adj[src]:
            adj[src].append(tgt)

    color: Dict[str, int] = {n: 0 for n in adj}
    stack: List[str] = []
    on_stack: Set[str] = set()
    cycles: List[List[str]] = []

    def walk(node: str) -> None:
        if len(cycles) >= limit:
            return
        color[node] = 1
        stack.append(node)
        on_stack.add(node)
        for nxt in adj.get(node, []):
            if len(cycles) >= limit:
                break
            if color.get(nxt, 0) == 1 and nxt in on_stack:
                cycles.append(stack[stack.index(nxt):] + [nxt])
            elif color.get(nxt, 0) == 0:
                walk(nxt)
        stack.pop()
        on_stack.discard(node)
        color[node] = 2

    for node in nodes:
        if len(cycles) >= limit:
            break
        if color.get(node, 0) == 0:
            walk(node)
    return cycles


# --------------------------------------------------------------------------- #
# 4) 质量体检
# --------------------------------------------------------------------------- #
def _quality(merged: Dict[str, Any], kb_tables: Set[str], kb_available: bool,
             graph: Any = None) -> Dict[str, Any]:
    """断链 / 孤岛 / 环路 / 未登记口径 —— 工作流级的「体检报告」。

    断链与孤岛都再做一次**全局血缘**核对：表在 ``warehouse_graph.json`` 里若有下游 /
    上游，说明它只是「跨工作流」而不是真的断了，条目里用 ``cross_workflow`` 标出来。
    """
    producers = merged["producers"]
    consumers = merged["consumers"]

    dangling = []
    for table in sorted(producers):
        if consumers.get(table) or _in_layer(table, TERMINAL_LAYERS):
            continue
        neighbours = _global_neighbours(graph, table, "downstream")
        dangling.append({
            "table": table,
            "layer": _layer(table),
            "produced_by": _unique(producers.get(table, [])),
            "cross_workflow": bool(neighbours),
            "global_downstream": neighbours,
            "hint": ("本工作流内无人消费；全局血缘显示下游在其它工作流："
                     + ", ".join(neighbours)) if neighbours
                    else "本工作流内无人消费；若是链路终点请确认是否漏挂下游任务",
        })

    orphans = []
    for table in sorted(consumers):
        if producers.get(table) or _in_layer(table, SOURCE_LAYERS):
            continue
        neighbours = _global_neighbours(graph, table, "upstream")
        orphans.append({
            "table": table,
            "layer": _layer(table),
            "consumed_by": _unique(consumers.get(table, [])),
            "cross_workflow": bool(neighbours),
            "global_upstream": neighbours,
            "hint": ("本工作流内无上游；全局血缘显示上游在其它工作流："
                     + ", ".join(neighbours)) if neighbours
                    else "本工作流内无上游；可能是漏挂上游任务或外部系统直灌",
        })

    cycles = _find_cycles(merged["nodes"], merged["edges"])
    missing = [
        {"table": table, "layer": _layer(table), "produced_by": _unique(producers.get(table, []))}
        for table in sorted(producers)
        if kb_available and table not in kb_tables
    ]
    return {
        "dangling_outputs": dangling,
        "orphan_inputs": orphans,
        "cycles": cycles,
        "missing_knowledge": missing,
        "summary": {
            "dangling_output_count": len(dangling),
            "dangling_cross_workflow_count": sum(1 for d in dangling if d.get("cross_workflow")),
            "orphan_input_count": len(orphans),
            "orphan_cross_workflow_count": sum(1 for o in orphans if o.get("cross_workflow")),
            "cycle_count": len(cycles),
            "missing_knowledge_count": len(missing),
            "checked": True,
        },
    }


# --------------------------------------------------------------------------- #
# 5) 知识库
# --------------------------------------------------------------------------- #
def _kb_tables() -> Set[str]:
    """知识库里「已登记过口径」的表名集合（读不到就给空集，调用方按 unavailable 处理）。"""
    path = str(default_db_path())
    if not os.path.exists(path):
        return set()
    try:
        with KnowledgeStore(path, create=False) as store:
            return {str(m.get("table_name") or "") for m in store.metrics() if m.get("table_name")}
    except Exception:  # noqa: BLE001 — 知识库坏掉不能影响血缘
        return set()


def _knowledge(payload: Dict[str, Any], merged: Dict[str, Any],
               output_tables: Sequence[str]) -> Dict[str, Any]:
    """工作流级口径匹配：把所有任务的字段血缘 + 所有产出表一起丢给知识库。"""
    if not _flag(payload.get("with_knowledge"), default=True):
        out = knowledge_unavailable("本次请求 with_knowledge=false，已跳过知识库匹配")
        out["hint"] = "如需口径匹配，请传 with_knowledge=true"
        return out

    db_path = str(payload.get("db") or os.environ.get("KB_DB") or default_db_path())
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), db_path)
    if not os.path.exists(db_path):
        return knowledge_unavailable(f"知识库文件不存在: {db_path}")
    try:
        store = KnowledgeStore(db_path, create=False)
    except Exception as e:  # noqa: BLE001
        return knowledge_unavailable(f"打开知识库失败: {type(e).__name__}: {e}")
    try:
        counts = store.counts()
        if not counts.get("kb_metrics"):
            return knowledge_unavailable(f"知识库为空: {db_path}")
        return match_knowledge(
            store,
            collect_target_fields(merged["column_lineage"]),
            list(output_tables),
            limit_metrics=int(payload.get("limit_metrics") or MAX_METRICS),
            limit_rules=int(payload.get("limit_rules") or 5),
        )
    except Exception as e:  # noqa: BLE001
        return knowledge_unavailable(f"知识库匹配失败: {type(e).__name__}: {e}")
    finally:
        store.close()


def _attribute_metrics(tasks: Sequence[Dict[str, Any]], knowledge: Dict[str, Any]) -> int:
    """把命中的口径按目标表归到各任务上（``metric_count``），返回总命中数。"""
    metrics = knowledge.get("metrics") or []
    for task in tasks:
        tables = set(task["output_tables"])
        columns = {(c.get("target_table"), c.get("target_column")) for c in task["_columns"]}
        count = 0
        for metric in metrics:
            if metric.get("target_table") in tables:
                count += 1
            elif (metric.get("target_table"), metric.get("target_column")) in columns:
                count += 1
        task["metric_count"] = count
    return len(metrics)


# --------------------------------------------------------------------------- #
# 6) 主入口
# --------------------------------------------------------------------------- #
def analyze_workflow(payload: Dict[str, Any]) -> Dict[str, Any]:
    """``POST /analyze-workflow`` 的实现：拉工作流 → 批量解析 → 合并 → 体检 → 报告。"""
    started = time.perf_counter()
    scope = str(payload.get("scope") or "current").strip().lower()
    if scope not in ("current", "project"):
        scope = "current"
    task_types = _task_types(payload)
    dialect = str(payload.get("dialect") or DEFAULT_DIALECT)

    try:
        client = _ds_client(payload)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"构造海豚客户端失败: {type(e).__name__}: {e}"}

    try:
        client.ensure_login()
        workflows = _resolve_workflows(client, payload, scope)
    except DsError as e:
        return {"success": False, "error": f"读取 DolphinScheduler 工作流失败: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 — 登出失败无关紧要
            pass

    task_items = [item for wf in workflows for item in wf["tasks"]]
    tasks = _parse_tasks(task_items, dialect, task_types)
    merged = _merge(tasks)

    input_tables = sorted({t for task in tasks for t in task["input_tables"]})
    output_tables = sorted({t for task in tasks for t in task["output_tables"]})
    graph = _global_graph()
    chain_in_workflow = _longest_chain(merged)
    stitched = _stitch_chain(chain_in_workflow, graph)
    chain = stitched["chain"]

    kb_tables = _kb_tables()
    knowledge = _knowledge(payload, merged, output_tables)
    metric_count = _attribute_metrics(tasks, knowledge)
    quality = _quality(merged, kb_tables, bool(knowledge.get("kb_available")), graph)

    primary = workflows[0] if workflows else {}
    workflow_info = {
        "name": primary.get("name") or str(payload.get("workflow_name") or ""),
        "code": primary.get("code"),
        "project_code": primary.get("project_code"),
        "release_state": primary.get("release_state"),
        "scope": scope,
        "task_count": len(tasks),
        "parsed_task_count": sum(1 for t in tasks if t["parsed"]),
        "workflow_count": len(workflows),
        "workflows": [{"name": wf.get("name"), "code": wf.get("code"),
                       "task_count": len(wf["tasks"])} for wf in workflows],
        "statement_count": sum(t["statement_count"] for t in tasks),
        "chain": chain,
        "chain_in_workflow": chain_in_workflow,
        "chain_external": {"upstream": stitched["upstream"], "downstream": stitched["downstream"]},
        "layers": _unique(_layer(t) for t in merged["nodes"]),
    }

    cost_ms = int((time.perf_counter() - started) * 1000)

    result: Dict[str, Any] = {
        "success": True,
        "workflow": workflow_info,
        "tasks": [
            {
                "name": task["name"],
                "type": task["type"],
                "code": task["code"],
                "parsed": task["parsed"],
                "script_len": task["script_len"],
                "script_keys": task["script_keys"],
                "statement_count": task["statement_count"],
                "input_tables": task["input_tables"],
                "output_tables": task["output_tables"],
                "column_count": task["column_count"],
                "metric_count": task["metric_count"],
                "errors": task["errors"],
            }
            for task in tasks
        ],
        "merged": {
            "table_lineage": merged["table_lineage"],
            "nodes": merged["nodes"],
            "edges": merged["edges"],
            "task_lineage": merged["task_lineage"],
            "input_tables": input_tables,
            "output_tables": output_tables,
            "column_lineage": merged["column_lineage"],
            "column_lineage_count": len(merged["column_lineage"]),
        },
        "chain": chain,
        "chain_in_workflow": chain_in_workflow,
        "chain_external": {"upstream": stitched["upstream"], "downstream": stitched["downstream"]},
        "knowledge": knowledge,
        "metric_count": metric_count,
        "quality": quality,
        "cost_ms": cost_ms,
        "dialect": dialect,
        "ds_base": client.base_url,
    }

    if _flag(payload.get("with_report"), default=True):
        info, err = _attach_report(payload, result, merged, dialect, cost_ms)
        if info:
            result["report"] = info
            result["report_id"] = info["report_id"]
            result["url"] = info["url"]
            result["internal_url"] = info["internal_url"]
        else:
            result["report_error"] = err

    result["cost_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def _attach_report(payload: Dict[str, Any], result: Dict[str, Any], merged: Dict[str, Any],
                   dialect: str, cost_ms: int) -> Tuple[Optional[Dict[str, Any]], str]:
    """生成工作流级 HTML 报告（复用 report.save_report，通过 ``meta.workflow`` 打开工作流模式）。"""
    try:
        workflow = result["workflow"]
        parsed = {
            "dialect": dialect,
            "statement_count": workflow["statement_count"],
            "input_tables": result["merged"]["input_tables"],
            "output_tables": result["merged"]["output_tables"],
            "table_lineage": merged["table_lineage"],
            "column_lineage": merged["column_lineage"],
            "column_lineage_count": len(merged["column_lineage"]),
            "statements": merged["statements"],
            "knowledge": result["knowledge"],
        }
        public_base, _internal = report_bases()
        meta = {
            "task_name": f"工作流 {workflow['name']}",
            "mode": "workflow",
            "dialect": dialect,
            "cost_ms": cost_ms,
            "statement_count": workflow["statement_count"],
            "service_url": f"{public_base}/analyze-workflow",
            "sql": "",
            "workflow": dict(workflow, quality=result["quality"], tasks=result["tasks"]),
        }
        info = save_report(
            parsed, meta,
            directory=reports_dir(payload),
            report_id=make_report_id(f"workflow:{workflow.get('name')}:{workflow.get('code')}"),
        )
        return info, ""
    except Exception as e:  # noqa: BLE001 — 报告是附加产物，失败不能影响血缘结果
        import traceback
        traceback.print_exc()
        return None, f"{type(e).__name__}: {e}"
