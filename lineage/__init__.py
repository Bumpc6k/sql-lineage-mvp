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
* **P6 工作流级血缘**（``lineage.workflow``）：给 DolphinScheduler 的 ``LINEAGE_DAG`` 任务类型
  提供 ``POST /analyze-workflow`` —— 拉取**一整个工作流**的全部任务脚本批量解析，合并成
  工作流级血缘（跨任务表级链路 / 任务级依赖 / 跨任务字段血缘），再做断链·孤岛·环路·未登记口径
  四类链路质量体检，并落一份工作流级 HTML 报告（``lineage.report`` 的「工作流模式」）。
* **P7 生成引擎**（``lineage.generate``，**按需导入**、不在本文件里 eager import）：
  反过来从业务需求**生成**加工 SQL 与数据链路 —— L1 单表加工 SQL 生成（知识库口径 + 存量字段
  血缘 → ``INSERT OVERWRITE ... SELECT``，逐列 explain 依据）、L2 分层链路生成（ods→dwd→dws→ads）、
  L3 一键落地 DolphinScheduler、L4 反向校验（生成 SQL 过血缘引擎 → 断链/孤岛/环路/跨层直连/
  口径一致性体检 + HTML 报告）。CLI：``generate sql|pipeline|apply|validate``；
  HTTP：``/generate/sql|pipeline|apply|validate``。
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
