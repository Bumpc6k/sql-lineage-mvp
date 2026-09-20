"""P5「血缘 × 业务口径一体化」测试。

覆盖：
* ``collect_target_fields``：从 column_lineage 抽目标字段（去重 / 保序 / 容错）；
* ``match_knowledge``：目标字段级命中、输出表级兜底、术语与规则、血缘链路；
* ``POST /analyze`` 处理函数：/parse 超集（血缘字段一字不差）+ knowledge 段；
* 降级：知识库不存在 / 空库 / ``with_knowledge=false`` 都不报错、血缘照常返回。

真实样例：``examples/knowledge_demo/cdw/dwd_卷烟产量码段明细.sql``
（口径 ``产量 = 打码量 + 跳码量 - 重码量``）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lineage.api_server import build_knowledge_section, handle_analyze, handle_parse
from lineage.knowledge import KnowledgeStore, build_knowledge_base, collect_target_fields, match_knowledge
from lineage.parser import SqlLineageParser

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DEMO = PROJECT_ROOT / "examples" / "knowledge_demo"
DEMO_SQL_PATH = KNOWLEDGE_DEMO / "cdw" / "dwd_卷烟产量码段明细.sql"


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("kb_integrate") / "knowledge.db"
    build_knowledge_base([str(KNOWLEDGE_DEMO)], db_path=path)
    return path


@pytest.fixture(scope="module")
def demo_sql() -> str:
    return DEMO_SQL_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. 目标字段抽取
# --------------------------------------------------------------------------- #
def test_collect_target_fields_dedup_and_order() -> None:
    lineage = [
        {"target_table": "cdw.t", "target_column": "chanliang_qty", "source_table": "ods.s", "source_column": "dama_qty"},
        {"target_table": "cdw.t", "target_column": "chanliang_qty", "source_table": "ods.s", "source_column": "tiaoma_qty"},
        {"target_table": "cdw.t", "target_column": "batch_no", "source_table": "ods.s", "source_column": "batch_no"},
        {"target_table": "cdw.t", "target_column": "", "source_table": "ods.s", "source_column": "x"},
        "not-a-dict",
    ]
    fields = collect_target_fields(lineage)
    assert [f["column"] for f in fields] == ["chanliang_qty", "batch_no"]
    assert fields[0]["source_column"] == "dama_qty"
    assert fields[0]["table"] == "cdw.t"


def test_collect_target_fields_tolerates_empty() -> None:
    assert collect_target_fields(None) == []
    assert collect_target_fields([]) == []


# --------------------------------------------------------------------------- #
# 2. 知识库匹配
# --------------------------------------------------------------------------- #
def test_match_knowledge_hits_metric_formula(demo_db: Path, demo_sql: str) -> None:
    statements = SqlLineageParser(dialect="hive").parse_sql(demo_sql)
    statements = statements if isinstance(statements, list) else [statements]
    targets = collect_target_fields(statements[0]["column_lineage"])

    with KnowledgeStore(demo_db) as store:
        result = match_knowledge(store, targets, statements[0]["output_table_names"])

    assert result["kb_available"] is True
    assert result["metric_count"] >= 1
    by_column = {m["target_column"]: m for m in result["metrics"]}
    assert "chanliang_qty" in by_column, by_column.keys()
    chanliang = by_column["chanliang_qty"]
    assert chanliang["chinese_name"] == "产量"
    assert chanliang["formula"] == "产量 = 打码量 + 跳码量 - 重码量"
    assert chanliang["metric_type"] == "聚合"
    assert chanliang["confidence"] == 0.9
    assert chanliang["matched_by"] == "目标字段"
    assert chanliang["source_statement"] == 1
    assert chanliang["source_script"].endswith("dwd_卷烟产量码段明细.sql")
    # 依赖字段带中文名 + 上游链路
    assert {d["column"] for d in chanliang["depends_on"]} == {"dama_qty", "tiaoma_qty", "chongma_qty"}
    assert chanliang["lineage_path"][0] == "cdw.dwd_卷烟产量码段明细"
    assert "ods.ods_卷烟码段流水" in chanliang["lineage_path"]
    # 依赖 3 个字段的口径排在只有 1 个依赖的口径前面
    assert result["metrics"][0]["target_column"] == "chanliang_qty"


def test_match_knowledge_terms_and_rules(demo_db: Path, demo_sql: str) -> None:
    statements = SqlLineageParser(dialect="hive").parse_sql(demo_sql)
    statements = statements if isinstance(statements, list) else [statements]
    targets = collect_target_fields(statements[0]["column_lineage"])

    with KnowledgeStore(demo_db) as store:
        result = match_knowledge(store, targets, statements[0]["output_table_names"])

    terms = {t["field"]: t["chinese_name"] for t in result["terms"]}
    assert terms["dama_qty"] == "打码量"
    assert terms["tiaoma_qty"] == "跳码量"
    assert terms["chongma_qty"] == "重码量"
    assert terms["chanliang_qty"] == "产量"

    assert result["rules"], "应提炼出产量口径相关业务规则"
    assert len(result["rules"]) <= 5
    assert any("产量口径" in r["description"] for r in result["rules"])
    assert all(r["rule_type"] and r["source_script"] for r in result["rules"])
    assert "chanliang_qty" in result["matched_fields"]


def test_match_knowledge_no_hit_is_silent(demo_db: Path) -> None:
    """匹配不到任何口径：空列表，不抛异常。"""
    with KnowledgeStore(demo_db) as store:
        result = match_knowledge(store, [{"table": "no.such_table", "column": "no_such_col"}], ["no.such_table"])
    assert result["kb_available"] is True
    assert result["metric_count"] == 0
    assert result["metrics"] == []
    assert result["matched_fields"] == []


# --------------------------------------------------------------------------- #
# 3. POST /analyze 处理函数
# --------------------------------------------------------------------------- #
def test_handle_analyze_is_parse_superset(demo_db: Path, demo_sql: str) -> None:
    payload = {"sql": demo_sql, "dialect": "hive", "with_knowledge": True, "db": str(demo_db)}
    parsed = handle_parse(dict(payload))
    analyzed = handle_analyze(dict(payload))

    for key in ("statement_count", "input_tables", "output_tables", "table_lineage",
                "column_lineage", "column_lineage_count", "statements", "dialect", "success"):
        assert analyzed[key] == parsed[key], key

    knowledge = analyzed["knowledge"]
    assert knowledge["kb_available"] is True
    assert knowledge["metric_count"] >= 1
    assert any(m["formula"] == "产量 = 打码量 + 跳码量 - 重码量" for m in knowledge["metrics"])


def test_handle_analyze_without_knowledge_flag(demo_db: Path, demo_sql: str) -> None:
    out = handle_analyze({"sql": demo_sql, "with_knowledge": False, "db": str(demo_db)})
    assert out["success"] is True
    assert out["knowledge"]["kb_available"] is False
    assert "with_knowledge" in out["knowledge"]["reason"]


def test_handle_analyze_missing_kb_degrades(demo_sql: str, tmp_path: Path) -> None:
    """知识库文件不存在：血缘照常，knowledge 降级并提示先建库。"""
    out = handle_analyze({"sql": demo_sql, "db": str(tmp_path / "not_there.db")})
    assert out["success"] is True
    assert out["output_tables"] == ["cdw.dwd_卷烟产量码段明细"]
    assert out["knowledge"]["kb_available"] is False
    assert "kb build" in out["knowledge"]["hint"]
    assert not (tmp_path / "not_there.db").exists(), "不应顺手创建空库"


def test_handle_analyze_empty_kb_degrades(demo_sql: str, tmp_path: Path) -> None:
    """空库（有文件无内容）：不报错，kb_available=False。"""
    import sqlite3

    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()
    out = handle_analyze({"sql": demo_sql, "db": str(empty)})
    assert out["success"] is True
    assert out["knowledge"]["kb_available"] is False


def test_handle_analyze_empty_sql() -> None:
    out = handle_analyze({"sql": "  "})
    assert out["success"] is False


def test_build_knowledge_section_api_shape() -> None:
    """/health 的 endpoints 列表里必须出现 /analyze。"""
    from lineage.api_server import ROUTES

    assert "/analyze" in ROUTES
    assert sorted(ROUTES) == sorted([
        "/parse", "/analyze", "/impact", "/upstream",
        "/kb/summary", "/kb/search", "/kb/ask", "/kb/metric",
    ])


def test_build_knowledge_section_ignores_bad_kb(demo_sql: str, tmp_path: Path) -> None:
    """知识库文件被写成非 SQLite 内容时也不能抛异常。"""
    bad = tmp_path / "bad.db"
    bad.write_text("this is not a sqlite database", encoding="utf-8")
    parsed = handle_parse({"sql": demo_sql})
    section = build_knowledge_section({"db": str(bad)}, parsed)
    assert section["kb_available"] is False
    assert section["metrics"] == []
