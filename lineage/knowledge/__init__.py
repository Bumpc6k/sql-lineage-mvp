"""P4：业务口径知识提炼 + 知识库。

一句话：**把数仓 SQL 脚本里蕴含的业务知识（指标口径 / 字段术语 / 业务规则）
自动提炼出来，落进可检索的 SQLite 知识库，并提供检索、问答与 Markdown 导出。**

模块地图
--------
============  ==================================================================
``textutil``   中文业务名推断（词典 + 命名规则）与表达式归一化 / 中文化
``comments``   SQL 注释挖掘（文件头 / 行内 / 表级 COMMENT）
``extractor``  口径提炼器：字段级血缘 → 指标口径 / 字段术语 / 业务规则
``store``      SQLite 知识库（幂等 rebuild、脚本级增量 upsert、导出 JSON）
``search``     关键词 / 模糊检索（多字段加权打分，可解释）
``qa``         问数骨架（规则模式 + 可插拔 LLM，无 key 自动降级）
``markdown``   《业务口径知识库.md》导出
``pipeline``   建库流水线（扫描 → 提炼 → 落库 → 导出）
============  ==================================================================

快速上手::

    from lineage.knowledge import build_knowledge_base, KnowledgeStore, search

    report = build_knowledge_base(["examples/warehouse"], db_path="data/knowledge.db")
    print(report.counts)

    store = KnowledgeStore("data/knowledge.db")
    print(store.summary())
    print(search(store, "产量")["counts"])
"""

from __future__ import annotations

from .comments import SqlComments, parse_sql_comments
from .extractor import (
    ExtractionResult,
    FieldKnowledge,
    KnowledgeExtractor,
    MetricKnowledge,
    RuleKnowledge,
    ScriptKnowledge,
    TableKnowledge,
    TermKnowledge,
    classify_expression,
)
from .markdown import export_markdown, to_markdown
from .pipeline import (
    DEFAULT_SCAN_DIRS,
    BuildReport,
    build_knowledge_base,
    format_build_text,
)
from .qa import LLMClient, LLMSettings, answer, detect_intent, extract_entity, format_metric
from .search import format_search_text, search
from .store import DEFAULT_DB_PATH, KnowledgeStore, default_db_path
from .textutil import (
    ChineseNameResolver,
    Glossary,
    localize_expression,
    normalize_expression,
    split_comment_unit,
    split_identifier,
    strip_uniform_aggregates,
)

__all__ = [
    # 提炼
    "KnowledgeExtractor",
    "ExtractionResult",
    "TableKnowledge",
    "FieldKnowledge",
    "MetricKnowledge",
    "RuleKnowledge",
    "TermKnowledge",
    "ScriptKnowledge",
    "classify_expression",
    # 文本
    "Glossary",
    "ChineseNameResolver",
    "normalize_expression",
    "localize_expression",
    "strip_uniform_aggregates",
    "split_identifier",
    "split_comment_unit",
    "parse_sql_comments",
    "SqlComments",
    # 存储
    "KnowledgeStore",
    "DEFAULT_DB_PATH",
    "default_db_path",
    # 检索 / 问答
    "search",
    "format_search_text",
    "answer",
    "format_metric",
    "detect_intent",
    "extract_entity",
    "LLMClient",
    "LLMSettings",
    # 文档 / 流水线
    "to_markdown",
    "export_markdown",
    "build_knowledge_base",
    "format_build_text",
    "BuildReport",
    "DEFAULT_SCAN_DIRS",
]

__version__ = "0.1.0"
