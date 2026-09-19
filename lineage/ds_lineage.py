"""DolphinScheduler 任务级血缘（P3）。

把海豚工作流里的任务节点还原成多层血缘图谱::

    项目(工程) -> 工作流 -> 任务节点 -> 表

数据来源全是海豚 OpenAPI 的**只读**接口（不改海豚一行源码，见 :mod:`lineage.ds_client`）：

* ``GET /projects``                                              —— 项目（工程）
* ``GET /projects/{pc}/process-definition``                      —— 项目下的工作流
* ``GET /projects/{pc}/process-definition/{code}``               —— 工作流 + 任务定义 + 任务关系
* 任务参数里的 ``taskParams.sql``（SQL 任务）/ ``taskParams.rawScript``（SHELL / PYTHON 任务）

每个任务的脚本文本交给 :class:`lineage.parser.SqlLineageParser` 解析，得到表级 / 字段级血缘；
再叠加两类「工作流间依赖」：

* **表依赖（推导）**：A 工作流读了 B 工作流产出的表 => ``B -> A``
* **原生依赖（声明式）**：任务参数里 ``SUB_PROCESS.processDefinitionCode`` /
  ``DEPENDENT.dependence.dependTaskList[].dependItemList[].definitionCode`` => ``B -> A``

支持的查询：某张表被哪些工作流 / 任务加工（反向）、某工作流的上下游依赖、
某工作流下每个任务读 / 写了哪些表、以及复用 P2 图引擎做表级上下游溯源。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .ds_client import (
    DEFAULT_DIALECT,
    DsClient,
    DsError,
    extract_scripts,
    referenced_definition_codes,
    task_params_of,
)
from .graph import LineageGraph, build_graph, format_analysis_text, table_layer
from .parser import SqlLineageParser, dumps

#: ds_lineage.json 的结构版本
DS_LINEAGE_SCHEMA_VERSION = 1
#: 落盘时每个任务保留的语句明细里，字段映射最多保留多少条（防止 JSON 爆炸）
MAX_COLUMNS_PER_EDGE_IN_JSON = 0
#: 依赖边默认最多列出的表
MAX_VIA_TABLES = 12

_LAYER_ORDER = {"src": 0, "stg": 1, "ods": 2, "dim": 2, "dwd": 3, "dws": 4, "ads": 5, "app": 6, "other": 9}


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class DsTask:
    """一个调度任务节点（= 海豚的一个 task definition）。"""

    code: str
    name: str
    task_type: str
    workflow: str
    workflow_code: str
    project: str
    project_code: str
    version: int = 0
    description: str = ""
    #: [{"key": "taskParams.sql", "text": "...", "statement_count": 1}]
    scripts: List[Dict[str, Any]] = field(default_factory=list)
    #: 解析出的语句（紧凑摘要，见 _compact_statement / build 的 full_statements 开关）
    statements: List[Dict[str, Any]] = field(default_factory=list)
    read_tables: List[str] = field(default_factory=list)
    write_tables: List[str] = field(default_factory=list)
    #: 同工作流内的上游 / 下游任务名（来自 processTaskRelationList）
    upstream_tasks: List[str] = field(default_factory=list)
    downstream_tasks: List[str] = field(default_factory=list)
    #: 任务参数里引用的其它工作流：[{target, target_code, kind, key}]
    native_refs: List[Dict[str, str]] = field(default_factory=list)
    #: 该任务涉及的数据源（SQL 任务）
    datasource_id: Optional[str] = None
    #: 解析报错（不中断整体构建）：[{workflow, task, key, error}]
    errors: List[Dict[str, Any]] = field(default_factory=list)
    #: 完整解析结果（含字段级血缘，仅构建期使用，不落盘）
    statements_full: List[Dict[str, Any]] = field(default_factory=list, repr=False)

    @property
    def key(self) -> str:
        """任务唯一键：``工作流/任务名``。"""
        return f"{self.workflow}/{self.name}"

    @property
    def has_sql(self) -> bool:
        return bool(self.statements)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "task_type": self.task_type,
            "workflow": self.workflow,
            "workflow_code": self.workflow_code,
            "project": self.project,
            "project_code": self.project_code,
            "version": self.version,
            "description": self.description,
            "scripts": self.scripts,
            "statements": self.statements,
            "read_tables": sorted(self.read_tables),
            "write_tables": sorted(self.write_tables),
            "upstream_tasks": sorted(self.upstream_tasks),
            "downstream_tasks": sorted(self.downstream_tasks),
            "native_refs": self.native_refs,
            "datasource_id": self.datasource_id,
            "errors": self.errors,
            "has_sql": self.has_sql,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DsTask":
        return cls(
            code=str(data.get("code") or ""),
            name=data["name"],
            task_type=str(data.get("task_type") or "SQL"),
            workflow=data.get("workflow") or "",
            workflow_code=str(data.get("workflow_code") or ""),
            project=data.get("project") or "",
            project_code=str(data.get("project_code") or ""),
            version=int(data.get("version") or 0),
            description=data.get("description") or "",
            scripts=list(data.get("scripts") or []),
            statements=list(data.get("statements") or []),
            read_tables=list(data.get("read_tables") or []),
            write_tables=list(data.get("write_tables") or []),
            upstream_tasks=list(data.get("upstream_tasks") or []),
            downstream_tasks=list(data.get("downstream_tasks") or []),
            native_refs=list(data.get("native_refs") or []),
            datasource_id=data.get("datasource_id"),
            errors=list(data.get("errors") or []),
        )


@dataclass
class DsWorkflow:
    """一个工作流（海豚 process definition）。"""

    code: str
    name: str
    project: str
    project_code: str
    description: str = ""
    version: int = 0
    release_state: str = ""
    execution_type: str = ""
    task_keys: List[str] = field(default_factory=list)
    reads: List[str] = field(default_factory=list)
    writes: List[str] = field(default_factory=list)
    #: 本工作流内部既产出又被消费的表（不产生跨流依赖）
    internal_tables: List[str] = field(default_factory=list)
    #: 表依赖推导出的上下游：[{workflow, via_tables, kinds}]
    upstream_workflows: List[Dict[str, Any]] = field(default_factory=list)
    downstream_workflows: List[Dict[str, Any]] = field(default_factory=list)
    #: 任务里声明的海豚原生依赖：[{workflow, workflow_code, via_task, kind, key}]
    native_dependencies: List[Dict[str, str]] = field(default_factory=list)

    @property
    def task_count(self) -> int:
        return len(self.task_keys)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "project": self.project,
            "project_code": self.project_code,
            "description": self.description,
            "version": self.version,
            "release_state": self.release_state,
            "execution_type": self.execution_type,
            "task_keys": sorted(self.task_keys),
            "task_count": self.task_count,
            "reads": sorted(self.reads),
            "writes": sorted(self.writes),
            "internal_tables": sorted(self.internal_tables),
            "upstream_workflows": self.upstream_workflows,
            "downstream_workflows": self.downstream_workflows,
            "native_dependencies": self.native_dependencies,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DsWorkflow":
        return cls(
            code=str(data.get("code") or ""),
            name=data["name"],
            project=data.get("project") or "",
            project_code=str(data.get("project_code") or ""),
            description=data.get("description") or "",
            version=int(data.get("version") or 0),
            release_state=data.get("release_state") or "",
            execution_type=data.get("execution_type") or "",
            task_keys=list(data.get("task_keys") or []),
            reads=list(data.get("reads") or []),
            writes=list(data.get("writes") or []),
            internal_tables=list(data.get("internal_tables") or []),
            upstream_workflows=list(data.get("upstream_workflows") or []),
            downstream_workflows=list(data.get("downstream_workflows") or []),
            native_dependencies=list(data.get("native_dependencies") or []),
        )


@dataclass
class DsProject:
    """一个项目（= 海豚 project / 工程）。"""

    code: str
    name: str
    description: str = ""
    workflow_names: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "workflow_count": len(self.workflow_names),
            "workflow_names": sorted(self.workflow_names),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DsProject":
        return cls(
            code=str(data.get("code") or ""),
            name=data["name"],
            description=data.get("description") or "",
            workflow_names=list(data.get("workflow_names") or []),
        )


# --------------------------------------------------------------------------- #
# 语句摘要
# --------------------------------------------------------------------------- #
def _compact_statement(stmt: Dict[str, Any], full: bool = False) -> Dict[str, Any]:
    """把解析结果压成可落盘的摘要（字段级明细默认只留条数）。"""
    cols = stmt.get("column_lineage") or []
    out: Dict[str, Any] = {
        "statement_index": stmt.get("statement_index"),
        "task_type": stmt.get("task_type"),
        "output_table_names": sorted(stmt.get("output_table_names") or []),
        "input_table_names": sorted(stmt.get("input_table_names") or []),
        "table_lineage": sorted(
            [{"source": p.get("source"), "target": p.get("target")} for p in (stmt.get("table_lineage") or [])],
            key=lambda x: (x["source"] or "", x["target"] or ""),
        ),
        "filter_count": len(stmt.get("filters") or []),
        "join_count": len(stmt.get("joins") or []),
        "column_mapping_count": len(cols),
        "partition_filters": stmt.get("partition_filters") or {},
    }
    if full:
        out["column_lineage"] = cols
    return out


# --------------------------------------------------------------------------- #
# 血缘模型
# --------------------------------------------------------------------------- #
class DsLineage:
    """「项目 -> 工作流 -> 任务 -> 表」多层血缘模型（可从 JSON 无损读回）。"""

    def __init__(self, dialect: str = DEFAULT_DIALECT, base_url: str = "", user: str = "",
                 fetched_at: str = "", generated_at: Optional[str] = None) -> None:
        self.dialect = dialect
        self.base_url = base_url
        self.user = user
        self.fetched_at = fetched_at
        self.generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.projects: Dict[str, DsProject] = {}
        self.workflows: Dict[str, DsWorkflow] = {}
        self.tasks: Dict[str, DsTask] = {}
        #: 表索引：表全名 -> {name, layer, produced_by, consumed_by, workflows, ...}
        self.tables: Dict[str, Dict[str, Any]] = {}
        #: 工作流间依赖边：[{source, target, kind, via_tables, via_task}]
        self.workflow_dependencies: List[Dict[str, Any]] = []
        #: 表级血缘图（复用 P2 引擎）
        self.table_graph: LineageGraph = LineageGraph(dialect=dialect)
        #: 解析失败记录
        self.failures: List[Dict[str, Any]] = []

    # ---------------------------------------------------------------- #
    # 基本访问
    # ---------------------------------------------------------------- #
    def workflow_names(self) -> List[str]:
        return sorted(self.workflows)

    def task_names(self, workflow: Optional[str] = None) -> List[str]:
        if workflow is None:
            return sorted(self.tasks)
        wf = self.workflows.get(workflow)
        if wf is None:
            return []
        return sorted(k for k in self.tasks if wf.name == self.tasks[k].workflow)

    def resolve_workflow(self, name: str) -> Optional[str]:
        """宽松解析工作流名（精确 -> 忽略大小写 -> 唯一子串）。"""
        if not name:
            return None
        if name in self.workflows:
            return name
        low = name.strip().lower()
        for key in self.workflows:
            if key.lower() == low:
                return key
        hits = [k for k in self.workflows if low in k.lower()]
        return hits[0] if len(hits) == 1 else None

    def workflow_candidates(self, name: str) -> List[str]:
        low = (name or "").strip().lower()
        return sorted(k for k in self.workflows if low in k.lower())

    def resolve_table(self, name: str) -> Optional[str]:
        """宽松解析表名（复用图引擎的：精确 -> 忽略大小写 -> 裸表名唯一匹配）。"""
        if not name:
            return None
        resolved = self.table_graph.resolve_table(name)
        if resolved:
            return resolved
        low = name.strip().lower()
        bare = low.split(".")[-1]
        hits = [t for t in self.tables if t.lower().split(".")[-1] == bare]
        return hits[0] if len(hits) == 1 else None

    def table_candidates(self, name: str) -> List[str]:
        cands = set(self.table_graph.candidates(name) or [])
        bare = (name or "").strip().lower().split(".")[-1]
        cands.update(t for t in self.tables if t.lower().split(".")[-1] == bare)
        return sorted(cands)

    # ---------------------------------------------------------------- #
    # 查询 ①：某张表被哪些工作流 / 任务加工（含反向消费方）
    # ---------------------------------------------------------------- #
    def table_tasks(self, table: str) -> Dict[str, Any]:
        resolved = self.resolve_table(table)
        if resolved is None:
            return {
                "found": False,
                "table": table,
                "candidates": self.table_candidates(table),
            }
        entry = self.tables.get(resolved) or {"name": resolved, "layer": table_layer(resolved)}
        produced = [self.tasks[k] for k in entry.get("produced_by_tasks") or [] if k in self.tasks]
        consumed = [self.tasks[k] for k in entry.get("consumed_by_tasks") or [] if k in self.tasks]

        def brief(task: DsTask, direction: str) -> Dict[str, Any]:
            return {
                "task": task.name,
                "task_code": task.code,
                "task_type": task.task_type,
                "workflow": task.workflow,
                "workflow_code": task.workflow_code,
                "project": task.project,
                "direction": direction,
                "statement_count": len(task.statements),
            }

        upstream = self.table_graph.upstream(resolved, depth=None, max_paths=0)
        downstream = self.table_graph.downstream(resolved, depth=None, max_paths=0)
        chains = [
            {
                "project": t.project,
                "workflow": t.workflow,
                "task": t.name,
                "task_type": t.task_type,
                "table": resolved,
                "role": "写入",
            }
            for t in sorted(produced, key=lambda x: (x.workflow, x.name))
        ] + [
            {
                "project": t.project,
                "workflow": t.workflow,
                "task": t.name,
                "task_type": t.task_type,
                "table": resolved,
                "role": "读取",
            }
            for t in sorted(consumed, key=lambda x: (x.workflow, x.name))
        ]

        return {
            "found": True,
            "table": resolved,
            "layer": entry.get("layer") or table_layer(resolved),
            "produced_by": [brief(t, "write") for t in sorted(produced, key=lambda x: (x.workflow, x.name))],
            "consumed_by": [brief(t, "read") for t in sorted(consumed, key=lambda x: (x.workflow, x.name))],
            "workflows": sorted({t.workflow for t in produced} | {t.workflow for t in consumed}),
            "producer_workflows": sorted({t.workflow for t in produced}),
            "consumer_workflows": sorted({t.workflow for t in consumed}),
            "chains": chains,
            "upstream_tables": upstream.get("tables") or [],
            "downstream_tables": downstream.get("tables") or [],
            "is_source": not (upstream.get("tables") or []),
            "is_leaf": not (downstream.get("tables") or []),
        }

    # ---------------------------------------------------------------- #
    # 查询 ②：某个工作流的详情（任务读 / 写表 + 上下游依赖）
    # ---------------------------------------------------------------- #
    def workflow_detail(self, name: str) -> Dict[str, Any]:
        resolved = self.resolve_workflow(name)
        if resolved is None:
            return {"found": False, "workflow": name,
                    "candidates": self.workflow_candidates(name)}
        wf = self.workflows[resolved]
        tasks = [self.tasks[k] for k in wf.task_keys if k in self.tasks]
        return {
            "found": True,
            "workflow": wf.name,
            "code": wf.code,
            "project": wf.project,
            "project_code": wf.project_code,
            "description": wf.description,
            "version": wf.version,
            "release_state": wf.release_state,
            "execution_type": wf.execution_type,
            "task_count": len(tasks),
            "tasks": [
                {
                    "name": t.name,
                    "code": t.code,
                    "task_type": t.task_type,
                    "upstream_tasks": t.upstream_tasks,
                    "downstream_tasks": t.downstream_tasks,
                    "read_tables": t.read_tables,
                    "write_tables": t.write_tables,
                    "script_count": len(t.scripts),
                    "statement_count": len(t.statements),
                    "datasource_id": t.datasource_id,
                    "errors": t.errors,
                }
                for t in sorted(tasks, key=lambda x: x.name)
            ],
            "reads": wf.reads,
            "writes": wf.writes,
            "internal_tables": wf.internal_tables,
            "upstream_workflows": wf.upstream_workflows,
            "downstream_workflows": wf.downstream_workflows,
            "native_dependencies": wf.native_dependencies,
        }

    # ---------------------------------------------------------------- #
    # 查询 ③：全体工作流概览 + 拓扑分层
    # ---------------------------------------------------------------- #
    def workflow_list(self) -> Dict[str, Any]:
        """工作流清单（带项目、任务数、读 / 写表数、上下游工作流），按依赖拓扑分层。"""
        # 只保留工作流之间的依赖图，做层次划分
        deps = [d for d in self.workflow_dependencies]
        succ: Dict[str, Set[str]] = {}
        pred: Dict[str, Set[str]] = {}
        for d in deps:
            succ.setdefault(d["source"], set()).add(d["target"])
            pred.setdefault(d["target"], set()).add(d["source"])

        # 拓扑分层（Kahn）：第 1 层 = 没有任何上游工作流的工作流
        remaining = {name: set(pred.get(name, set())) for name in self.workflows}
        levels: List[List[str]] = []
        done: Set[str] = set()
        while len(done) < len(remaining):
            layer = sorted(n for n, p in remaining.items() if n not in done and not (p - done))
            if not layer:                       # 有环 -> 剩下的按名字兜底
                layer = sorted(n for n in remaining if n not in done)
            levels.append(layer)
            done.update(layer)

        items = []
        for wf in sorted(self.workflows.values(), key=lambda w: (w.project, w.name)):
            items.append({
                "workflow": wf.name,
                "code": wf.code,
                "project": wf.project,
                "task_count": wf.task_count,
                "read_table_count": len(wf.reads),
                "write_table_count": len(wf.writes),
                "writes": wf.writes,
                "upstream_workflows": sorted({u["workflow"] for u in wf.upstream_workflows}),
                "downstream_workflows": sorted({u["workflow"] for u in wf.downstream_workflows}),
                "native_dependencies": sorted({u["workflow"] for u in wf.native_dependencies}),
                "release_state": wf.release_state,
            })
        return {
            "found": bool(items),
            "workflow_count": len(items),
            "levels": levels,
            "workflows": items,
            "dependencies": deps,
        }

    # ---------------------------------------------------------------- #
    # 查询 ④：表级上下游（复用 P2 图引擎）
    # ---------------------------------------------------------------- #
    def upstream_tables(self, table: str, depth: Optional[int] = None, max_paths: int = 0) -> Dict[str, Any]:
        """表级上游溯源，并给每张上游表标注是哪个工作流 / 任务产出的。"""
        result = self.table_graph.upstream(table, depth=depth, max_paths=max_paths)
        if result.get("found"):
            result["producers"] = self._annotate_producers(result.get("tables") or [])
        return result

    def downstream_tables(self, table: str, depth: Optional[int] = None, max_paths: int = 0) -> Dict[str, Any]:
        result = self.table_graph.downstream(table, depth=depth, max_paths=max_paths)
        if result.get("found"):
            result["consumers"] = self._annotate_producers(
                self.table_graph.descendants(table, depth) if result.get("table") else []
            )
        return result

    def _annotate_producers(self, tables: Iterable[str]) -> List[Dict[str, Any]]:
        out = []
        for name in sorted(set(tables)):
            entry = self.tables.get(name) or {}
            producers = [self.tasks[k] for k in entry.get("produced_by_tasks") or [] if k in self.tasks]
            consumers = [self.tasks[k] for k in entry.get("consumed_by_tasks") or [] if k in self.tasks]
            out.append({
                "table": name,
                "layer": entry.get("layer") or table_layer(name),
                "produced_by": [{"workflow": t.workflow, "task": t.name} for t in sorted(producers, key=lambda x: x.key)],
                "consumed_by": [{"workflow": t.workflow, "task": t.name} for t in sorted(consumers, key=lambda x: x.key)],
                "is_source": not producers,
            })
        return out

    def project_detail(self, name: str) -> Dict[str, Any]:
        proj = self.projects.get(name)
        if proj is None:
            return {"found": False, "project": name, "candidates": sorted(self.projects)}
        return {
            "found": True,
            "project": proj.name,
            "code": proj.code,
            "description": proj.description,
            "workflow_count": len(proj.workflow_names),
            "workflows": [
                {
                    "workflow": wf_name,
                    "task_count": self.workflows[wf_name].task_count if wf_name in self.workflows else 0,
                    "writes": self.workflows[wf_name].writes if wf_name in self.workflows else [],
                }
                for wf_name in sorted(proj.workflow_names)
            ],
        }

    # ---------------------------------------------------------------- #
    # 统计
    # ---------------------------------------------------------------- #
    def stats(self) -> Dict[str, Any]:
        tasks = list(self.tasks.values())
        with_sql = [t for t in tasks if t.has_sql]
        no_sql = [t for t in tasks if not t.has_sql]
        stmt_count = sum(len(t.statements) for t in tasks)
        col_count = sum(int(s.get("column_mapping_count") or 0) for t in tasks for s in t.statements)
        prod_workflows = {t.workflow for t in tasks if t.write_tables}
        cons_workflows = {t.workflow for t in tasks if t.read_tables}
        layer_count: Dict[str, int] = {}
        for name in self.tables:
            layer = (self.tables[name].get("layer") or table_layer(name))
            layer_count[layer] = layer_count.get(layer, 0) + 1
        sources = [n for n, e in self.tables.items() if not (e.get("produced_by_tasks") or [])]
        leaves = [n for n, e in self.tables.items() if not (e.get("consumed_by_tasks") or [])]
        native_edges = [d for d in self.workflow_dependencies if d.get("kind") == "native"]
        table_edges = [d for d in self.workflow_dependencies if d.get("kind") == "table"]
        return {
            "project_count": len(self.projects),
            "workflow_count": len(self.workflows),
            "task_count": len(tasks),
            "task_with_sql_count": len(with_sql),
            "task_without_sql_count": len(no_sql),
            "workflow_with_sql_count": len({t.workflow for t in with_sql}),
            "workflow_without_sql_count": len({t.workflow for t in no_sql if t.workflow} - {t.workflow for t in with_sql}),
            "script_count": sum(len(t.scripts) for t in tasks),
            "statement_count": stmt_count,
            "column_mapping_count": col_count,
            "table_count": len(self.tables),
            "source_table_count": len(sources),
            "leaf_table_count": len(leaves),
            "tables_by_layer": {k: layer_count[k] for k in sorted(layer_count, key=lambda x: _LAYER_ORDER.get(x, 9))},
            "workflow_dependency_count": len(self.workflow_dependencies),
            "table_dependency_count": len(table_edges),
            "native_dependency_count": len(native_edges),
            "workflow_level_count": len(self.workflow_list()["levels"]),
            "producer_workflow_count": len(prod_workflows),
            "consumer_workflow_count": len(cons_workflows),
            "failed_task_count": len({f["task"] for f in self.failures if f.get("task")}),
            "error_count": len(self.failures),
        }

    # ---------------------------------------------------------------- #
    # 序列化
    # ---------------------------------------------------------------- #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": DS_LINEAGE_SCHEMA_VERSION,
            "kind": "dolphinscheduler_lineage",
            "dialect": self.dialect,
            "generated_at": self.generated_at,
            "fetched_at": self.fetched_at,
            "source": {"base_url": self.base_url, "user": self.user},
            "stats": self.stats(),
            "projects": [self.projects[k].to_dict() for k in sorted(self.projects)],
            "workflows": [self.workflows[k].to_dict() for k in sorted(self.workflows)],
            "tasks": [self.tasks[k].to_dict() for k in sorted(self.tasks)],
            "tables": [self.tables[k] for k in sorted(self.tables)],
            "workflow_dependencies": self.workflow_dependencies,
            "failures": self.failures,
            "table_graph": self.table_graph.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DsLineage":
        source = data.get("source") or {}
        obj = cls(
            dialect=data.get("dialect", DEFAULT_DIALECT),
            base_url=source.get("base_url", ""),
            user=source.get("user", ""),
            fetched_at=data.get("fetched_at", ""),
            generated_at=data.get("generated_at"),
        )
        for p in data.get("projects") or []:
            proj = DsProject.from_dict(p)
            obj.projects[proj.name] = proj
        for w in data.get("workflows") or []:
            wf = DsWorkflow.from_dict(w)
            obj.workflows[wf.name] = wf
        for t in data.get("tasks") or []:
            task = DsTask.from_dict(t)
            obj.tasks[task.key] = task
        obj.tables = {e["name"]: e for e in (data.get("tables") or []) if e.get("name")}
        obj.workflow_dependencies = list(data.get("workflow_dependencies") or [])
        obj.failures = list(data.get("failures") or [])
        if data.get("table_graph"):
            obj.table_graph = LineageGraph.from_dict(data["table_graph"])
        else:
            obj.table_graph = LineageGraph(dialect=obj.dialect)
        return obj

    def dumps(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def loads(cls, text: str) -> "DsLineage":
        return cls.from_dict(json.loads(text))

    def save(self, path: Any) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(self.dumps() + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Any) -> "DsLineage":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "workflows" not in data:
            raise ValueError(f"不是 ds sync 产出的血缘文件：{path}")
        return cls.from_dict(data)

    # ---------------------------------------------------------------- #
    # 中文摘要
    # ---------------------------------------------------------------- #
    def summary_text(self) -> str:
        return format_ds_summary_text(self)


# --------------------------------------------------------------------------- #
# 构建器
# --------------------------------------------------------------------------- #
class DsLineageBuilder:
    """把海豚 API 的快照（或 csv / JSON 样本）构建成 :class:`DsLineage`。"""

    def __init__(self, dialect: str = DEFAULT_DIALECT, parser: Optional[SqlLineageParser] = None,
                 full_statements: bool = False) -> None:
        self.dialect = dialect
        self.parser = parser or SqlLineageParser(dialect=dialect)
        self.full_statements = full_statements

    # ---------------------------------------------------------------- #
    def build(self, fetched: Dict[str, Any], base_url: str = "", user: str = "") -> DsLineage:
        """输入 :meth:`DsClient.fetch_all` 的快照，输出多层血缘模型。"""
        lineage = DsLineage(
            dialect=fetched.get("dialect") or self.dialect,
            base_url=base_url or fetched.get("base_url", ""),
            user=user or fetched.get("user", ""),
            fetched_at=fetched.get("fetched_at", ""),
        )
        self.parser = self.parser or SqlLineageParser(dialect=lineage.dialect)
        all_statements: List[Dict[str, Any]] = []
        #: definition_code -> 工作流名（用于把原生依赖翻译成工作流名）
        code_to_workflow: Dict[str, str] = {}

        for bundle in fetched.get("projects") or []:
            project = bundle.get("project") or {}
            pname = project.get("name") or str(project.get("code") or "未知项目")
            pcode = str(project.get("code") or "")
            proj = DsProject(code=pcode, name=pname, description=project.get("description") or "")
            lineage.projects[pname] = proj

            for wf_bundle in bundle.get("workflows") or []:
                definition = wf_bundle.get("processDefinition") or {}
                wname = definition.get("name") or str(definition.get("code") or "未命名工作流")
                wcode = str(definition.get("code") or "")
                code_to_workflow[wcode] = wname
                proj.workflow_names.append(wname)
                lineage.workflows[wname] = DsWorkflow(
                    code=wcode,
                    name=wname,
                    project=pname,
                    project_code=pcode,
                    description=definition.get("description") or "",
                    version=int(definition.get("version") or 0),
                    release_state=definition.get("releaseState") or "",
                    execution_type=definition.get("executionType") or "",
                )

        # 第二遍：任务（需要先有 code->workflow 映射，才能把原生依赖翻译成工作流名）
        for bundle in fetched.get("projects") or []:
            project = bundle.get("project") or {}
            pname = project.get("name") or str(project.get("code") or "未知项目")
            pcode = str(project.get("code") or "")
            for wf_bundle in bundle.get("workflows") or []:
                definition = wf_bundle.get("processDefinition") or {}
                wname = definition.get("name") or str(definition.get("code") or "未命名工作流")
                task_defs = {str(t.get("code")): t for t in (wf_bundle.get("taskDefinitionList") or [])}
                relations = wf_bundle.get("processTaskRelationList") or []
                name_by_code = {str(t.get("code")): (t.get("name") or str(t.get("code"))) for t in task_defs.values()}

                for tcode, tdef in task_defs.items():
                    task = self._build_task(
                        tdef, wname, str(definition.get("code") or ""), pname, pcode,
                        name_by_code, code_to_workflow,
                    )
                    lineage.tasks[task.key] = task
                    lineage.workflows[wname].task_keys.append(task.key)
                    all_statements.extend(task.statements_full)
                    if task.errors:
                        lineage.failures.extend(task.errors)

                # 任务间关系（工作流内部 DAG）
                for rel in relations:
                    pre = str(rel.get("preTaskCode") or "0")
                    post = str(rel.get("postTaskCode") or "0")
                    if post not in name_by_code:
                        continue
                    post_task = lineage.tasks.get(f"{wname}/{name_by_code[post]}")
                    if post_task is None:
                        continue
                    if pre not in ("0", "", "None") and pre in name_by_code:
                        up_name = name_by_code[pre]
                        post_task.upstream_tasks.append(up_name)
                        up_task = lineage.tasks.get(f"{wname}/{up_name}")
                        if up_task and post_task.name not in up_task.downstream_tasks:
                            up_task.downstream_tasks.append(post_task.name)

        # 第三遍：表索引 / 工作流依赖 / 表级图
        lineage.table_graph = build_graph(all_statements, dialect=lineage.dialect,
                                          root=f"dolphinscheduler:{lineage.base_url}")
        self._build_table_index(lineage)
        self._build_workflow_dependencies(lineage)
        return lineage

    # ---------------------------------------------------------------- #
    def _build_task(
        self,
        tdef: Dict[str, Any],
        workflow: str,
        workflow_code: str,
        project: str,
        project_code: str,
        name_by_code: Dict[str, str],
        code_to_workflow: Dict[str, str],
    ) -> DsTask:
        tname = tdef.get("name") or str(tdef.get("code") or "未命名任务")
        params = task_params_of(tdef)
        task = DsTask(
            code=str(tdef.get("code") or ""),
            name=tname,
            task_type=str(tdef.get("taskType") or ""),
            workflow=workflow,
            workflow_code=workflow_code,
            project=project,
            project_code=project_code,
            version=int(tdef.get("version") or 0),
            description=tdef.get("description") or "",
        )
        ds = params.get("datasource")
        if ds not in (None, "", 0, "0"):
            task.datasource_id = str(ds)

        # 原生依赖 -> 工作流
        for ref in referenced_definition_codes(tdef):
            target = code_to_workflow.get(ref["definition_code"])
            task.native_refs.append({
                "target_workflow": target or "",
                "target_code": ref["definition_code"],
                "kind": ref["kind"],
                "key": ref["key"],
            })

        # 脚本 -> 解析
        statements: List[Dict[str, Any]] = []
        for script in extract_scripts(tdef):
            label = f"{project}/{workflow}/{tname} ({script['key']})"
            try:
                parsed = self.parser.parse_sql(script["text"], source=label)
            except Exception as exc:                       # pragma: no cover - 防御
                task.errors.append({"workflow": workflow, "task": tname, "key": script["key"],
                                    "error": f"{type(exc).__name__}: {exc}"})
                task.scripts.append({"key": script["key"], "text": script["text"],
                                     "statement_count": 0, "error": str(exc)})
                continue
            for stmt in parsed:
                stmt["workflow"] = workflow
                stmt["task"] = tname
                stmt["project"] = project
            statements.extend(parsed)
            task.scripts.append({
                "key": script["key"],
                "text": script["text"],
                "statement_count": len(parsed),
                "table_count": len({n for s in parsed for n in (s.get("input_table_names") or [])}
                                   | {n for s in parsed for n in (s.get("output_table_names") or [])}),
            })

        task.statements_full = statements
        task.statements = [_compact_statement(s, full=self.full_statements) for s in statements]
        task.read_tables = sorted({n for s in statements for n in (s.get("input_table_names") or [])})
        task.write_tables = sorted({n for s in statements for n in (s.get("output_table_names") or [])})
        return task

    # ---------------------------------------------------------------- #
    def _build_table_index(self, lineage: DsLineage) -> None:
        index: Dict[str, Dict[str, Any]] = {}

        def entry(name: str) -> Dict[str, Any]:
            if name not in index:
                index[name] = {
                    "name": name,
                    "layer": table_layer(name),
                    "produced_by_tasks": [],
                    "consumed_by_tasks": [],
                    "producer_workflows": [],
                    "consumer_workflows": [],
                    "producer_projects": [],
                    "in_degree": 0,
                    "out_degree": 0,
                }
            return index[name]

        for task in lineage.tasks.values():
            for name in task.write_tables:
                e = entry(name)
                if task.key not in e["produced_by_tasks"]:
                    e["produced_by_tasks"].append(task.key)
                if task.workflow not in e["producer_workflows"]:
                    e["producer_workflows"].append(task.workflow)
                if task.project not in e["producer_projects"]:
                    e["producer_projects"].append(task.project)
            for name in task.read_tables:
                e = entry(name)
                if task.key not in e["consumed_by_tasks"]:
                    e["consumed_by_tasks"].append(task.key)
                if task.workflow not in e["consumer_workflows"]:
                    e["consumer_workflows"].append(task.workflow)

        for name, e in index.items():
            e["produced_by_tasks"].sort()
            e["consumed_by_tasks"].sort()
            e["producer_workflows"].sort()
            e["consumer_workflows"].sort()
            e["producer_projects"].sort()
            e["in_degree"] = len(e["produced_by_tasks"])
            e["out_degree"] = len(e["consumed_by_tasks"])
            e["is_source"] = not e["produced_by_tasks"]
            e["is_leaf"] = not e["consumed_by_tasks"]
        lineage.tables = index

    # ---------------------------------------------------------------- #
    def _build_workflow_dependencies(self, lineage: DsLineage) -> None:
        """推导工作流间依赖：表依赖（A 读 B 产出的表）+ 原生依赖（DEPENDENT / SUB_PROCESS）。"""
        # 先按任务汇总出每个工作流的读表 / 写表
        for task in lineage.tasks.values():
            wf = lineage.workflows.get(task.workflow)
            if wf is None:
                continue
            wf.reads.extend(t for t in task.read_tables if t not in wf.reads)
            wf.writes.extend(t for t in task.write_tables if t not in wf.writes)

        writer_of: Dict[str, List[str]] = {}
        for wf in lineage.workflows.values():
            for t in wf.writes:
                writer_of.setdefault(t, []).append(wf.name)

        merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

        def add(source: str, target: str, kind: str, table: Optional[str] = None,
                via_task: Optional[str] = None, relation: Optional[str] = None) -> None:
            if not source or not target or source == target:
                return
            key = (source, target, kind)
            edge = merged.get(key)
            if edge is None:
                edge = {"source": source, "target": target, "kind": kind,
                        "via_tables": [], "via_task": via_task or "", "relation": relation or ""}
                merged[key] = edge
            if table and table not in edge["via_tables"]:
                edge["via_tables"].append(table)
            if via_task and via_task not in edge["via_task"]:
                edge["via_task"] = f"{edge['via_task']},{via_task}" if edge["via_task"] else via_task

        for wf in lineage.workflows.values():
            own = set(wf.writes)
            for table in wf.reads:
                if table in own:
                    continue                       # 本工作流自产自用 -> 内部表，不算跨流依赖
                for producer in writer_of.get(table, []):
                    add(producer, wf.name, "table", table=table)
            # 原生依赖（任务声明）
            for task in lineage.tasks.values():
                if task.workflow != wf.name:
                    continue
                for ref in task.native_refs:
                    target_wf = ref.get("target_workflow") or ""
                    if not target_wf:
                        continue
                    add(target_wf, wf.name, "native", via_task=task.name, relation=ref.get("kind"))

        deps = sorted(merged.values(), key=lambda d: (d["source"], d["target"], d["kind"]))
        for d in deps:
            d["via_tables"] = sorted(d["via_tables"])
            d["via_table_count"] = len(d["via_tables"])
            d["via_tables_truncated"] = False
            if len(d["via_tables"]) > MAX_VIA_TABLES:
                d["via_tables"] = d["via_tables"][:MAX_VIA_TABLES]
                d["via_tables_truncated"] = True

        # 回填到工作流对象
        for name, wf in lineage.workflows.items():
            wf.internal_tables = sorted(set(wf.writes) & set(wf.reads))
            up, down = [], []
            for d in deps:
                if d["target"] == name:
                    up.append({"workflow": d["source"], "kind": d["kind"],
                               "via_tables": d["via_tables"], "via_task": d.get("via_task", "")})
                if d["source"] == name:
                    down.append({"workflow": d["target"], "kind": d["kind"],
                               "via_tables": d["via_tables"], "via_task": d.get("via_task", "")})
            wf.upstream_workflows = sorted(up, key=lambda x: (x["workflow"], x["kind"]))
            wf.downstream_workflows = sorted(down, key=lambda x: (x["workflow"], x["kind"]))
            native = []
            for task in sorted(lineage.tasks.values(), key=lambda x: x.key):
                if task.workflow != name:
                    continue
                for ref in task.native_refs:
                    native.append({"workflow": ref.get("target_workflow") or ref["target_code"],
                                   "workflow_code": ref["target_code"],
                                   "via_task": task.name, "kind": ref["kind"], "key": ref["key"]})
            wf.native_dependencies = native
            wf.reads = sorted(wf.reads)
            wf.writes = sorted(wf.writes)

        lineage.workflow_dependencies = deps

    # ---------------------------------------------------------------- #
    def build_from_client(self, client: DsClient, project_names: Optional[Sequence[str]] = None,
                          project_codes: Optional[Sequence[Any]] = None) -> DsLineage:
        """直接从海豚拉取并构建（``ds sync`` 走这条路）。"""
        fetched = client.fetch_all(project_names=project_names, project_codes=project_codes,
                                   dialect=self.dialect)
        return self.build(fetched, base_url=client.base_url, user=client.user)


def build_ds_lineage(fetched: Dict[str, Any], dialect: str = DEFAULT_DIALECT,
                     full_statements: bool = False) -> DsLineage:
    """便捷函数：快照 -> 血缘模型。"""
    return DsLineageBuilder(dialect=dialect, full_statements=full_statements).build(fetched)


# --------------------------------------------------------------------------- #
# 中文文本渲染
# --------------------------------------------------------------------------- #
def _line(char: str = "-", width: int = 78) -> str:
    return char * width


def _short_kind(kind: str) -> str:
    return "表" if kind == "table" else "原生"


def _merge_workflow_links(items: Sequence[Dict[str, Any]]) -> str:
    """把「同一工作流的两类依赖（表 / 原生）」合并成一个名字，避免重复显示。"""
    kinds: Dict[str, List[str]] = {}
    for item in items:
        kinds.setdefault(item["workflow"], [])
        k = _short_kind(item.get("kind") or "")
        if k not in kinds[item["workflow"]]:
            kinds[item["workflow"]].append(k)
    return "、".join(f"{name}（{'/'.join(kinds[name])}依赖）" for name in sorted(kinds)) or "无"


def format_ds_summary_text(lineage: DsLineage, max_workflows: int = 20, max_tables: int = 15) -> str:
    """``ds sync`` 的中文摘要。"""
    st = lineage.stats()
    out: List[str] = []
    out.append(_line("="))
    out.append("DolphinScheduler 血缘同步报告（旁路集成：只读 OpenAPI，不改海豚源码）")
    out.append(_line("="))
    out.append(f"接口地址：{lineage.base_url or '(未知)'}    登录用户：{lineage.user or '(未知)'}"
               f"    快照时间：{lineage.fetched_at or '(未知)'}")
    out.append(f"SQL 方言：{lineage.dialect}")
    out.append("")
    out.append("【总览】")
    out.append(f"  项目（工程）    : {st['project_count']} 个")
    out.append(f"  工作流          : {st['workflow_count']} 个"
               f"（含 SQL 的 {st['workflow_with_sql_count']} 个）")
    out.append(f"  任务节点        : {st['task_count']} 个"
               f"（可解析 SQL 的 {st['task_with_sql_count']} 个 / 无 SQL 的 {st['task_without_sql_count']} 个）")
    out.append(f"  SQL 脚本        : {st['script_count']} 份，解析出语句 {st['statement_count']} 条"
               f"，字段映射 {st['column_mapping_count']} 条")
    out.append(f"  涉及表          : {st['table_count']} 张"
               f"（源系统表 {st['source_table_count']} 张 / 末端表 {st['leaf_table_count']} 张）")
    if st["tables_by_layer"]:
        layers = "、".join(f"{k} {v} 张" for k, v in st["tables_by_layer"].items())
        out.append(f"  分层分布        : {layers}")
    out.append(f"  工作流间依赖    : {st['workflow_dependency_count']} 条"
               f"（表血缘推导 {st['table_dependency_count']} 条 + 海豚原生依赖 {st['native_dependency_count']} 条）"
               f"，依赖层级 {st['workflow_level_count']} 层")
    if st["error_count"]:
        out.append(f"  解析失败        : {st['error_count']} 处（详见 JSON 的 failures 字段）")
    out.append("")

    out.append("【项目 -> 工作流 -> 任务 骨架】")
    for pname in sorted(lineage.projects):
        proj = lineage.projects[pname]
        out.append(f"  ● 项目 {proj.name}（code={proj.code}，{len(proj.workflow_names)} 个工作流）")
        for wname in sorted(proj.workflow_names):
            wf = lineage.workflows.get(wname)
            if wf is None:
                continue
            ups = _merge_workflow_links(wf.upstream_workflows)
            downs = _merge_workflow_links(wf.downstream_workflows)
            out.append(f"      └─ {wname}  任务 {wf.task_count} 个，读表 {len(wf.reads)} 张，写表 {len(wf.writes)} 张")
            out.append(f"          上游工作流：{ups}")
            out.append(f"          下游工作流：{downs}")
            tasks = [lineage.tasks[k] for k in sorted(wf.task_keys) if k in lineage.tasks]
            for t in tasks[:max_workflows]:
                pieces = [f"{t.name} [{t.task_type or '?'}]"]
                if t.upstream_tasks:
                    pieces.append(f"<- {', '.join(t.upstream_tasks)}")
                if not t.has_sql:
                    pieces.append("(无 SQL)")
                out.append(f"          · {' '.join(pieces)}")
                if t.write_tables:
                    out.append(f"              写：{', '.join(t.write_tables)}")
                if t.read_tables:
                    out.append(f"              读：{', '.join(t.read_tables)}")
    out.append("")

    out.append("【工作流依赖拓扑】")
    wl = lineage.workflow_list()
    for i, level in enumerate(wl["levels"], 1):
        out.append(f"  第 {i} 层：{', '.join(level)}")
    if wl["dependencies"]:
        out.append("  依赖明细：")
        for d in wl["dependencies"]:
            kind = "表血缘" if d["kind"] == "table" else "海豚原生"
            via = ("，经表 " + "、".join(d.get("via_tables") or []) if d.get("via_tables") else
                   ("，由任务 " + d.get("via_task", "") if d.get("via_task") else ""))
            out.append(f"      {d['source']} -> {d['target']}（{kind}{via}）")
    out.append("")

    leaves = sorted(t for t, e in lineage.tables.items() if not e.get("consumed_by_tasks"))
    if leaves:
        out.append(f"【末端表（无人消费，前 {min(len(leaves), max_tables)} 张）】")
        for t in leaves[:max_tables]:
            prods = lineage.tables[t].get("producer_workflows") or []
            out.append(f"  {t}（{'、'.join(prods) if prods else '源系统表'}）")
        out.append("")
    out.append("下一步：python -m lineage.cli ds workflows / ds tables <表名> / ds task <工作流名> / ds upstream <表名>")
    return "\n".join(out)


def format_workflows_text(result: Dict[str, Any]) -> str:
    """``ds workflows`` 的中文输出。"""
    if not result.get("workflow_count"):
        return "没有从血缘文件里读到任何工作流（是不是 ds sync 还没跑？）"
    out: List[str] = []
    out.append(_line("="))
    out.append(f"工作流清单（共 {result['workflow_count']} 个，依赖分层 {len(result.get('levels') or [])} 层）")
    out.append(_line("="))
    for i, level in enumerate(result.get("levels") or [], 1):
        out.append(f"第 {i} 层（{len(level)} 个，无更上游的表依赖）：")
        for wname in level:
            wf = next((w for w in result["workflows"] if w["workflow"] == wname), None)
            if wf is None:
                continue
            out.append(f"  ● {wf['workflow']}  [{wf['project']}]")
            out.append(f"      任务 {wf['task_count']} 个    读表 {wf['read_table_count']} 张"
                       f"    写表 {wf['write_table_count']} 张    状态 {wf['release_state'] or '-'}")
            out.append(f"      上游工作流：{'、'.join(wf['upstream_workflows']) or '无'}")
            out.append(f"      下游工作流：{'、'.join(wf['downstream_workflows']) or '无'}")
            if wf["native_dependencies"]:
                out.append(f"      海豚原生依赖：{'、'.join(wf['native_dependencies'])}")
    out.append("")
    out.append("依赖边明细：")
    for d in result.get("dependencies") or []:
        kind = "表血缘" if d["kind"] == "table" else "海豚原生"
        detail = "、".join(d.get("via_tables") or []) or d.get("via_task") or ""
        out.append(f"  {d['source']} -> {d['target']}（{kind}：{detail}）")
    return "\n".join(out)


def format_table_tasks_text(result: Dict[str, Any], max_list: int = 20) -> str:
    """``ds tables`` 的中文输出。"""
    if not result.get("found"):
        lines = [f"!! 血缘文件里找不到表：{result.get('table')}"]
        cands = result.get("candidates") or []
        lines.append(f"   你是不是想找：{', '.join(cands)}" if cands
                     else "   先用 `ds workflows` 或 `ds sync` 确认表名（支持只写表名不带库名）")
        return "\n".join(lines)

    out: List[str] = []
    out.append(_line("="))
    out.append(f"表「{result['table']}」的加工链路（层级：{result.get('layer')}）")
    out.append(_line("="))
    if result["produced_by"]:
        out.append(f"被 {len(result['producer_workflows'])} 个工作流 / {len(result['produced_by'])} 个任务【加工产出】：")
        for item in result["produced_by"][:max_list]:
            out.append(f"  ● {item['workflow']} / {item['task']} [{item['task_type']}]"
                       f"    语句 {item['statement_count']} 条    工作流 code={item['workflow_code']}")
    else:
        out.append("【源系统表】没有任何调度任务产出它（来自外部系统 / 手工导入）")
    out.append("")
    if result["consumed_by"]:
        out.append(f"被 {len(result['consumer_workflows'])} 个工作流 / {len(result['consumed_by'])} 个任务【读取消费】：")
        for item in result["consumed_by"][:max_list]:
            out.append(f"  ○ {item['workflow']} / {item['task']} [{item['task_type']}]"
                       f"    语句 {item['statement_count']} 条")
    else:
        out.append("【末端表】没有调度任务读它（报表 / 应用直接取数）")
    out.append("")
    out.append("多层血缘链路（项目 -> 工作流 -> 任务 -> 表）：")
    for chain in result.get("chains") or []:
        out.append(f"  {chain['project']} -> {chain['workflow']} -> {chain['task']} -> {chain['table']}"
                   f"（{chain['role']}）")
    up = result.get("upstream_tables") or []
    down = result.get("downstream_tables") or []
    out.append("")
    out.append(f"表级上游（{len(up)} 张）：{', '.join(up) if up else '无（源系统表）'}")
    out.append(f"表级下游（{len(down)} 张）：{', '.join(down) if down else '无（末端表）'}")
    return "\n".join(out)


def format_workflow_detail_text(result: Dict[str, Any], max_tasks: int = 30) -> str:
    """``ds task`` 的中文输出（某工作流下每个任务读 / 写了哪些表）。"""
    if not result.get("found"):
        lines = [f"!! 血缘文件里找不到工作流：{result.get('workflow')}"]
        cands = result.get("candidates") or []
        lines.append(f"   你是不是想找：{', '.join(cands)}" if cands
                     else "   先用 `ds workflows` 列出全部工作流")
        return "\n".join(lines)

    out: List[str] = []
    out.append(_line("="))
    out.append(f"工作流「{result['workflow']}」（项目：{result['project']}）")
    out.append(_line("="))
    out.append(f"定义 code：{result['code']}    版本：{result['version']}    "
               f"发布状态：{result['release_state'] or '-'}    执行策略：{result['execution_type'] or '-'}")
    if result.get("description"):
        out.append(f"描述：{result['description']}")
    out.append(f"任务节点 {result['task_count']} 个    读表 {len(result['reads'])} 张    写表 {len(result['writes'])} 张")
    out.append("")
    out.append("任务节点清单（含读 / 写表）：")
    for i, t in enumerate(result["tasks"][:max_tasks], 1):
        flag = "" if t["statement_count"] else " / 无 SQL"
        out.append(f"  [{i}] {t['name']}  [{t['task_type'] or '?'}{flag}]  code={t['code']}")
        if t["upstream_tasks"]:
            out.append(f"      依赖前置任务：{', '.join(t['upstream_tasks'])}")
        if t["downstream_tasks"]:
            out.append(f"      触发后置任务：{', '.join(t['downstream_tasks'])}")
        out.append(f"      读：{', '.join(t['read_tables']) if t['read_tables'] else '(无)'}")
        out.append(f"      写：{', '.join(t['write_tables']) if t['write_tables'] else '(无)'}")
        if t.get("datasource_id"):
            out.append(f"      数据源 id：{t['datasource_id']}    脚本 {t['script_count']} 份 / 语句 {t['statement_count']} 条")
        if t["errors"]:
            out.append(f"      !! 解析失败 {len(t['errors'])} 处")
    if len(result["tasks"]) > max_tasks:
        out.append(f"  ...（还有 {len(result['tasks']) - max_tasks} 个任务，见 JSON 输出）")
    out.append("")
    out.append(f"本工作流写出的表（{len(result['writes'])}）：{', '.join(result['writes']) if result['writes'] else '无'}")
    out.append(f"本工作流读取的表（{len(result['reads'])}）：{', '.join(result['reads']) if result['reads'] else '无'}")
    if result.get("internal_tables"):
        out.append(f"内部表（自产自用，不产生跨流依赖）：{', '.join(result['internal_tables'])}")
    out.append("")
    if result["upstream_workflows"]:
        out.append("上游工作流（表依赖 / 原生依赖，均表示「它先跑，我再跑」）：")
        for u in result["upstream_workflows"]:
            via = "、".join(u.get("via_tables") or []) or u.get("via_task") or ""
            out.append(f"  <- {u['workflow']}（{'表依赖' if u['kind'] == 'table' else '原生依赖'}：{via}）")
    else:
        out.append("上游工作流：无（它是最上游的工作流）")
    if result["downstream_workflows"]:
        out.append("下游工作流（我跑完才轮到它们）：")
        for u in result["downstream_workflows"]:
            via = "、".join(u.get("via_tables") or []) or u.get("via_task") or ""
            out.append(f"  -> {u['workflow']}（{'表依赖' if u['kind'] == 'table' else '原生依赖'}：{via}）")
    else:
        out.append("下游工作流：无（它是最末端的工作流）")
    if result.get("native_dependencies"):
        out.append("海豚原生依赖声明（DEPENDENT / SUB_PROCESS 任务参数里读到的）：")
        for n in result["native_dependencies"]:
            out.append(f"  · 任务 {n['via_task']} 依赖工作流 {n['workflow']}"
                       f"（{n['kind']}，code={n['workflow_code']}）")
    return "\n".join(out)


def format_ds_upstream_text(result: Dict[str, Any], max_paths: int = 5) -> str:
    """``ds upstream`` 的中文输出：P2 图引擎结果 + 每张表的调度产出方。"""
    if not result.get("found"):
        return format_analysis_text(result, max_paths=max_paths)

    base = format_analysis_text({k: v for k, v in result.items() if k not in ("producers", "consumers")},
                                max_paths=max_paths)
    producers = result.get("producers") or []
    extra: List[str] = []
    if producers:
        extra.append("")
        extra.append("各上游表的调度产出方（DolphinScheduler 侧）：")
        for item in producers:
            if item["produced_by"]:
                who = "、".join(f"{p['workflow']}/{p['task']}" for p in item["produced_by"])
                extra.append(f"  {item['table']}（{item['layer']}）<- {who}")
            else:
                extra.append(f"  {item['table']}（{item['layer']}）<- 源系统表（无调度任务产出）")
    return base + "\n".join(extra)


__all__ = [
    "DS_LINEAGE_SCHEMA_VERSION",
    "DsTask",
    "DsWorkflow",
    "DsProject",
    "DsLineage",
    "DsLineageBuilder",
    "build_ds_lineage",
    "format_ds_summary_text",
    "format_workflows_text",
    "format_table_tasks_text",
    "format_workflow_detail_text",
    "format_ds_upstream_text",
]
