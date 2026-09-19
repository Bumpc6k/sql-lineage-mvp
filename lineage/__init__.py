"""sql-lineage-mvp: 基于 sqlglot 的 SQL 静态血缘解析（表级 + 字段级 + 图谱分析）。

能力分层：

* **P1 解析层**（``lineage.parser``）：Hive / Spark SQL 方言的静态解析，
  输出表级血缘、字段级血缘、过滤条件；不连数据库、不做执行计划分析。
* **P2 图谱层**（``lineage.graph`` / ``lineage.scan`` / ``lineage.viz``）：
  目录批量扫描 → 全局血缘图 → 上游溯源 / 下游影响分析 / 环路检测 / 统计，
  导出 Mermaid 与自包含交互式 HTML。
* **P3 调度集成层**（``lineage.ds_client`` / ``lineage.ds_lineage``）：
  旁路对接 DolphinScheduler OpenAPI（只读、不改海豚源码），把工作流里的任务 SQL
  还原成「项目(工程) → 工作流 → 任务节点 → 表」的多层血缘，支持反向查询与工作流依赖推导。
"""

from .ds_client import DsApiError, DsAuthError, DsClient, DsConnectionError, extract_scripts
from .ds_lineage import DsLineage, DsLineageBuilder, build_ds_lineage
from .graph import LineageGraph, build_graph, build_graph_from_report
from .parser import SqlLineageParser, __version__
from .scan import ScanResult, scan_directory
from .viz import to_html, to_mermaid

__all__ = [
    "DsApiError",
    "DsAuthError",
    "DsClient",
    "DsConnectionError",
    "DsLineage",
    "DsLineageBuilder",
    "LineageGraph",
    "ScanResult",
    "SqlLineageParser",
    "__version__",
    "build_ds_lineage",
    "build_graph",
    "build_graph_from_report",
    "extract_scripts",
    "scan_directory",
    "to_html",
    "to_mermaid",
]
