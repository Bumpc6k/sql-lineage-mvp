"""生成引擎（P7）：从业务需求生成加工 SQL / 分层链路 / 调度工作流，并自校验。

四层能力
--------
============  ============================================================  ==========================
层            能力                                                          CLI / HTTP
============  ============================================================  ==========================
``L1``        单表加工 SQL 生成（知识库口径 → INSERT OVERWRITE ... SELECT）  ``generate sql`` / ``/generate/sql``
``L2``        分层链路生成（ods → dwd → dws → ads 多段 SQL + 图）            ``generate pipeline`` / ``/generate/pipeline``
``L3``        一键落地 DolphinScheduler（任务定义 + 依赖 + locations）        ``generate apply`` / ``/generate/apply``
``L4``        反向校验（生成 SQL 过血缘引擎 → 断链/孤岛/环路/分层规则）      ``generate validate`` / ``/generate/validate``
============  ============================================================  ==========================

模块地图
--------
* :mod:`~lineage.generate.spec` —— 公共底座：知识库只读视图、血缘图索引、分层规则、证据收集；
* :mod:`~lineage.generate.sql_builder` —— L1 单表加工 SQL 生成（模板引擎核心）；
* :mod:`~lineage.generate.pipeline` —— L2 分层链路生成；
* :mod:`~lineage.generate.apply` —— L3 转 DolphinScheduler 工作流并（可选）真实创建；
* :mod:`~lineage.generate.validate` —— L4 反向校验 + 体检报告；
* :mod:`~lineage.generate.llm` —— 可插拔 LLM（无 key 自动跳过，模板为主）。

快速上手::

    from lineage.generate import generate_sql, generate_pipeline

    generate_sql({"source_tables": ["ods.ods_卷烟产量流水"],
                  "target_table": "cdw.dwd_卷烟产量明细",
                  "metrics": ["产量"], "group_by": ["plant_code"]})["sql"]
"""

from __future__ import annotations

from lineage.generate.apply import apply_pipeline, build_workflow_json, format_apply_text
from lineage.generate.llm import GenerateLLM
from lineage.generate.pipeline import format_pipeline_text, generate_pipeline
from lineage.generate.spec import (
    DEFAULT_GRAPH,
    LAYER_CN,
    LAYER_INDEX,
    SIDE_LAYERS,
    TERMINAL_LAYERS,
    Evidence,
    GraphIndex,
    KbView,
    layer_cn,
    layer_of,
    open_store,
    parse_check,
    step_ok,
)
from lineage.generate.sql_builder import ColumnSpec, format_sql_text, generate_sql, render_insert
from lineage.generate.validate import format_validate_text, validate_generation

__all__ = [
    # L1
    "generate_sql",
    "format_sql_text",
    "render_insert",
    "ColumnSpec",
    # L2
    "generate_pipeline",
    "format_pipeline_text",
    # L3
    "apply_pipeline",
    "build_workflow_json",
    "format_apply_text",
    # L4
    "validate_generation",
    "format_validate_text",
    # 底座
    "GraphIndex",
    "KbView",
    "Evidence",
    "open_store",
    "parse_check",
    "layer_of",
    "layer_cn",
    "step_ok",
    "GenerateLLM",
    "DEFAULT_GRAPH",
    "LAYER_INDEX",
    "LAYER_CN",
    "SIDE_LAYERS",
    "TERMINAL_LAYERS",
]

__version__ = "1.0.0"
