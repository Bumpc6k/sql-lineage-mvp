"""P4「业务口径知识提炼 + 知识库」测试。

覆盖：
* 文本层：表达式归一化 / 聚合剥离 / 中文化 / 中文业务名推断优先级 / 待确认；
* 注释层：文件头、行内列注释、表级 COMMENT、WHERE 行注释；
* 提炼层：指标口径（公式 / 类型 / 依赖 / 来源）、直取不产出口径、业务规则；
* 存储层：rebuild 幂等（内容指纹）、增量 upsert 等价、清理孤立记录、导出 JSON；
* 检索 / 问答：关键词打分、模糊命中、意图识别、无 LLM 时降级为规则模式；
* 交付层：CLI 子命令、HTTP 处理函数、Markdown 导出；
* 真实示例目录：``examples/warehouse`` 至少提炼出 20 条口径 / 50 个字段。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lineage.knowledge import (
    ChineseNameResolver,
    Glossary,
    KnowledgeExtractor,
    KnowledgeStore,
    answer,
    build_knowledge_base,
    classify_expression,
    default_db_path,
    detect_intent,
    export_markdown,
    extract_entity,
    localize_expression,
    normalize_expression,
    parse_sql_comments,
    search,
    strip_uniform_aggregates,
)
from lineage.knowledge.comments import parse_sql_comments as parse_comments
from lineage.knowledge.textutil import split_comment_unit, split_identifier
from lineage.parser import SqlLineageParser

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = PROJECT_ROOT / "examples" / "warehouse"
KNOWLEDGE_DEMO = PROJECT_ROOT / "examples" / "knowledge_demo"


# --------------------------------------------------------------------------- #
# 测试夹具
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def glossary() -> Glossary:
    return Glossary.load_default()


@pytest.fixture(scope="module")
def resolver(glossary: Glossary) -> ChineseNameResolver:
    return ChineseNameResolver(glossary)


DEMO_SQL = """
-- =============================================================
-- 测试用：码段产量口径
-- 上游：ods.ods_码段流水
-- =============================================================
INSERT OVERWRITE TABLE cdw.dwd_码段产量 PARTITION (dt = '2026-01-01')
SELECT
    b.batch_no                                     AS batch_no,          -- 批次号
    SUM(b.dama_qty)                                AS dama_qty_total,    -- 打码量合计（箱）
    SUM(b.dama_qty) + SUM(b.tiaoma_qty) - SUM(b.chongma_qty) AS chanliang_qty, -- 产量（箱）
    ROUND(SUM(b.chongma_qty) / SUM(b.dama_qty), 6) AS chongma_rate,      -- 重码率
    CASE WHEN SUM(b.dama_qty) > 0 THEN 1 ELSE 0 END AS has_dama_flag,    -- 是否有打码
    COUNT(1)                                       AS batch_cnt,        -- 批次数
    b.batch_no                                     AS bz               -- 历史遗留字段，业务含义待确认
FROM ods.ods_码段流水 b
WHERE b.dt = '2026-01-01'
  AND b.batch_no IS NOT NULL                        -- 批次号为空视为脏数据
GROUP BY b.batch_no;
"""


@pytest.fixture(scope="module")
def demo_extraction():
    """用一段内联 SQL 跑完整的提炼流程。"""
    parser = SqlLineageParser(dialect="hive")
    statements = parser.parse_sql(DEMO_SQL, source="demo.sql")
    extractor = KnowledgeExtractor(dialect="hive")
    extractor.add_file("demo.sql", DEMO_SQL, statements, parse_comments(DEMO_SQL))
    return extractor.finish()


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("kb") / "knowledge.db"
    build_knowledge_base([str(WAREHOUSE), str(KNOWLEDGE_DEMO)], db_path=path)
    return path


@pytest.fixture()
def broken_db(tmp_path: Path) -> Path:
    """真实示例目录建出来的库（每个用例一份，互不干扰）。"""
    path = tmp_path / "knowledge.db"
    build_knowledge_base([str(WAREHOUSE)], db_path=path)
    return path


# --------------------------------------------------------------------------- #
# 1. 文本层
# --------------------------------------------------------------------------- #
def test_normalize_expression_strips_alias_and_prefix() -> None:
    assert normalize_expression("SUM(i.output_qty) AS output_qty") == "SUM(output_qty)"
    assert normalize_expression("p.output_qty * 250 AS output_qty_cig") == "output_qty * 250"


def test_normalize_expression_handles_db_table_column() -> None:
    assert normalize_expression("db.t.col AS col") == "col"


def test_normalize_expression_keeps_cast_semantics() -> None:
    text = normalize_expression("CAST(a.output_qty AS DECIMAL(18, 4)) AS output_qty")
    assert text == "CAST(output_qty AS DECIMAL(18, 4))"


def test_normalize_expression_spaces_operators() -> None:
    assert normalize_expression("(SUM(a.x)+SUM(b.y)) AS z") == "(SUM(x) + SUM(y))"


def test_strip_uniform_aggregates_only_when_same_func() -> None:
    assert strip_uniform_aggregates(
        "SUM(dama_qty) + SUM(tiaoma_qty) - SUM(chongma_qty)"
    ) == "dama_qty + tiaoma_qty - chongma_qty"
    # 不同聚合函数：不剥（避免给出误导性的口径）
    assert strip_uniform_aggregates("SUM(a) / MAX(b)") is None
    # COUNT(1) 没有业务字段：不剥
    assert strip_uniform_aggregates("COUNT(1)") is None
    assert strip_uniform_aggregates("ROUND(x, 4)") is None


def test_localize_expression_substitutes_chinese() -> None:
    name_map = {"dama_qty": "打码量", "tiaoma_qty": "跳码量", "chongma_qty": "重码量"}
    assert localize_expression(
        "dama_qty + tiaoma_qty - chongma_qty", name_map
    ) == "打码量 + 跳码量 - 重码量"
    # 函数名不能被替换
    assert localize_expression("ROUND(dama_qty, 4)", name_map) == "ROUND(打码量, 4)"


def test_split_identifier_handles_snake_and_camel() -> None:
    assert split_identifier("total_output_qty") == ["total", "output", "qty"]
    assert split_identifier("totalOutputQty") == ["total", "output", "qty"]


def test_split_comment_unit_extracts_unit() -> None:
    assert split_comment_unit("产量（箱）") == ("产量", "箱")
    assert split_comment_unit("税种（消费税/增值税/附加税）") == ("税种（消费税/增值税/附加税）", None)
    assert split_comment_unit(None) == (None, None)


def test_split_comment_unit_ignores_explanatory_parentheses() -> None:
    # 「箱转条」是换算说明而不是单位 → 不拆分
    assert split_comment_unit("产量折条（箱转条）") == ("产量折条（箱转条）", None)
    assert split_comment_unit("库存量（箱）") == ("库存量", "箱")


def test_split_comment_unit_treats_unknown_hint_as_unresolved() -> None:
    assert split_comment_unit("历史遗留字段，业务含义待确认") == (None, None)
    assert split_comment_unit("TODO 待补充") == (None, None)


def test_resolver_prefers_comment_when_consistent(resolver: ChineseNameResolver) -> None:
    hit = resolver.resolve("output_qty", "产量（箱）")
    assert hit.chinese_name == "产量" and hit.source == "comment"


def test_resolver_prefers_glossary_when_comment_is_a_note(resolver: ChineseNameResolver) -> None:
    # 「箱转条」是口径说明而不是字段名 → 用词典里的正式名
    hit = resolver.resolve("output_qty_cig", "箱转条")
    assert hit.chinese_name == "产量（条）" and hit.source == "exact_glossary"


def test_resolver_falls_back_to_exact_glossary(resolver: ChineseNameResolver) -> None:
    hit = resolver.resolve("defect_rate")
    assert hit.chinese_name == "不良品率" and hit.source == "exact_glossary"
    assert hit.confidence == 1.0


def test_resolver_composes_from_naming_rules(resolver: ChineseNameResolver) -> None:
    hit = resolver.resolve("total_stock_amt")
    assert hit.chinese_name and "库存" in hit.chinese_name and "金额" in hit.chinese_name


def test_resolver_marks_unknown_as_pending(resolver: ChineseNameResolver) -> None:
    hit = resolver.resolve("zzzz_unknown_thing")
    assert hit.chinese_name is None and hit.source == "pending"
    assert not hit.ok


# --------------------------------------------------------------------------- #
# 2. 注释层
# --------------------------------------------------------------------------- #
def test_parse_sql_comments_header_and_columns() -> None:
    comments = parse_sql_comments(DEMO_SQL)
    assert "码段产量口径" in comments.header
    assert "上游：ods.ods_码段流水" in comments.header
    assert comments.column_comments["batch_no"] == "批次号"
    assert comments.column_comments["chanliang_qty"] == "产量（箱）"


def test_parse_sql_comments_table_comment() -> None:
    sql = "CREATE TABLE t COMMENT '测试表' AS SELECT a AS a FROM s.a;"
    assert parse_sql_comments(sql).table_comments == {"t": "测试表"}


def test_parse_sql_comments_block_comment() -> None:
    sql = "/* 块注释 */\nSELECT a.x AS x FROM s.a a;\n"
    comments = parse_sql_comments(sql)
    assert comments.header == "块注释"
    assert comments.column_comments == {}


def test_parse_sql_comments_where_note() -> None:
    comments = parse_sql_comments("SELECT a.x AS x FROM s.a a WHERE a.dt = '1' -- 只取当天\n")
    assert comments.where_comments == ["只取当天"]


# --------------------------------------------------------------------------- #
# 3. 提炼层
# --------------------------------------------------------------------------- #
def test_classify_expression_types() -> None:
    assert classify_expression("SUM(a)")[0] == "聚合"
    assert classify_expression("a + b - c")[0] == "算术计算"
    assert classify_expression("ROUND(a / NULLIF(b, 0), 4)")[0] == "比率"
    assert classify_expression("CASE WHEN a > 0 THEN 1 ELSE 0 END")[0] == "条件分支"
    assert classify_expression("ROW_NUMBER() OVER(ORDER BY a)")[0] == "窗口函数"
    assert classify_expression("COALESCE(a, 0)")[0] == "函数转换"
    assert classify_expression("a")[0] == "直取"


def test_extract_chongma_formula(demo_extraction) -> None:
    """核心场景：产量 = 打码量 + 跳码量 - 重码量。"""
    metrics = {m.metric_name: m for m in demo_extraction.metrics}
    metric = metrics["chanliang_qty"]
    assert metric.formula == "产量 = 打码量 + 跳码量 - 重码量"
    assert metric.formula_full == "产量 = SUM(打码量) + SUM(跳码量) - SUM(重码量)"
    assert metric.metric_type == "聚合" and metric.aggregate_func == "SUM"
    assert metric.table_name == "cdw.dwd_码段产量" and metric.layer == "dwd"
    assert metric.source_file == "demo.sql" and metric.source_stmt == 1
    deps = {d["column"]: d["chinese_name"] for d in metric.depends_on}
    assert deps == {"dama_qty": "打码量", "tiaoma_qty": "跳码量", "chongma_qty": "重码量"}


def test_extract_passthrough_is_not_a_metric(demo_extraction) -> None:
    names = {m.metric_name for m in demo_extraction.metrics}
    assert "batch_no" not in names          # a.col AS col 不算口径
    assert "chanliang_qty" in names


def test_extract_ratio_and_case_metrics(demo_extraction) -> None:
    metrics = {m.metric_name: m for m in demo_extraction.metrics}
    assert metrics["chongma_rate"].metric_type == "比率"
    assert metrics["chongma_rate"].formula.startswith("重码率 = ")
    assert metrics["has_dama_flag"].metric_type == "条件分支"
    assert "批次数" in metrics["batch_cnt"].formula


def test_extract_metric_confidence_and_notes(demo_extraction) -> None:
    metric = {m.metric_name: m for m in demo_extraction.metrics}["chanliang_qty"]
    assert 0.3 <= metric.confidence <= 0.97
    assert "SUM" in metric.notes  # 剥离聚合的说明


def test_extract_fields_and_pending(demo_extraction) -> None:
    fields = {(f.table_name, f.column_name): f for f in demo_extraction.fields}
    assert fields[("cdw.dwd_码段产量", "dama_qty_total")].chinese_name == "打码量合计"
    assert fields[("cdw.dwd_码段产量", "dama_qty_total")].unit == "箱"
    bz = fields[("cdw.dwd_码段产量", "bz")]
    assert bz.chinese_name is None and bz.chinese_source == "pending"
    assert "待确认" in (bz.business_desc or "")


def test_extract_rules(demo_extraction) -> None:
    rules = demo_extraction.rules
    kinds = {r.rule_type for r in rules}
    assert {"过滤规则", "分区规则", "业务规则（注释）"} <= kinds
    desc = " ".join(r.description for r in rules)
    assert "脏数据" in desc or "空值过滤" in desc


def test_extract_stats_and_tables(demo_extraction) -> None:
    stats = demo_extraction.stats()
    assert stats["metric_count"] >= 4 and stats["field_count"] >= 5
    assert stats["table_count"] >= 2 and stats["rule_count"] >= 2
    tables = {t.table_name for t in demo_extraction.tables}
    assert {"cdw.dwd_码段产量", "ods.ods_码段流水"} <= tables


# --------------------------------------------------------------------------- #
# 4. 存储层
# --------------------------------------------------------------------------- #
def test_store_rebuild_is_idempotent(broken_db: Path) -> None:
    first = build_knowledge_base([str(WAREHOUSE)], db_path=broken_db)
    second = build_knowledge_base([str(WAREHOUSE)], db_path=broken_db)
    assert first.knowledge_hash == second.knowledge_hash
    assert first.counts == second.counts


def test_store_incremental_equals_rebuild(broken_db: Path) -> None:
    full = build_knowledge_base([str(WAREHOUSE)], db_path=broken_db)
    again = build_knowledge_base([str(WAREHOUSE)], db_path=broken_db, mode="incremental")
    assert again.knowledge_hash == full.knowledge_hash


def test_store_counts_and_summary(demo_db: Path) -> None:
    with KnowledgeStore(demo_db) as store:
        counts = store.counts()
        assert counts["kb_metrics"] >= 20
        assert counts["kb_fields"] >= 50
        assert counts["kb_terms"] >= 50
        summary = store.summary()
        assert summary["counts"] == counts
        assert summary["metric_by_layer"]
        assert summary["metric_by_type"]


def test_store_get_metric_by_name_and_chinese(demo_db: Path) -> None:
    with KnowledgeStore(demo_db) as store:
        by_name = store.get_metric("chanliang_qty")
        assert by_name and by_name[0]["formula"] == "产量 = 打码量 + 跳码量 - 重码量"
        by_cn = store.get_metric("产量")
        assert by_cn and all(m["chinese_name"] == "产量" for m in by_cn)
        assert store.get_metric("不存在的指标xyz") == []


def test_store_upstream_paths(demo_db: Path) -> None:
    with KnowledgeStore(demo_db) as store:
        paths = store.upstream_paths("cdw.dwd_卷烟产量码段明细")
        assert paths
        assert paths[0][0] == "cdw.dwd_卷烟产量码段明细"       # 链路从本表开始
        assert paths[0][-1] == "src.mes_码段采集接口"          # 一直到源系统接口表
        # 只写表名也能解析
        assert store.resolve_table_name("dwd_卷烟产量明细") == "cdw.dwd_卷烟产量明细"


def test_store_export_json_and_tables(demo_db: Path) -> None:
    with KnowledgeStore(demo_db) as store:
        data = store.export_json()
        for key in ("meta", "summary", "tables", "fields", "metrics", "rules", "terms", "table_lineage"):
            assert key in data
        assert json.dumps(data, ensure_ascii=False)  # 可 JSON 序列化
        table = store.get_table("dwd_卷烟产量明细")
        assert table and table["chinese_name"]


def test_store_incremental_prunes_orphans(tmp_path: Path) -> None:
    db = tmp_path / "kb.db"
    only = tmp_path / "sql"
    only.mkdir()
    (only / "a.sql").write_text(
        "INSERT OVERWRITE TABLE db.t1 SELECT SUM(x.a) AS s FROM src.s x;", encoding="utf-8"
    )
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "b.sql").write_text(
        "INSERT OVERWRITE TABLE db.t2 SELECT SUM(x.b) AS s FROM src.x x;", encoding="utf-8"
    )
    build_knowledge_base([str(only), str(extra)], db_path=db)
    with KnowledgeStore(db) as store:
        assert {m["table_name"] for m in store.metrics()} == {"db.t1", "db.t2"}
    # 把 b.sql 改成不再产出 t2（改产出 t3），增量扫描 extra 目录
    (extra / "b.sql").write_text(
        "INSERT OVERWRITE TABLE db.t3 SELECT SUM(x.b) AS s FROM src.x x;", encoding="utf-8"
    )
    build_knowledge_base([str(extra)], db_path=db, mode="incremental")
    with KnowledgeStore(db) as store:
        tables = {m["table_name"] for m in store.metrics()}
        assert "db.t3" in tables and "db.t2" not in tables   # 旧记录被清理
        assert "db.t1" in tables                             # 未扫描的脚本不受影响


def test_store_meta_and_default_path() -> None:
    assert str(default_db_path()).endswith("knowledge.db")


# --------------------------------------------------------------------------- #
# 5. 检索 / 问答
# --------------------------------------------------------------------------- #
def test_search_finds_metric_by_chinese(demo_db: Path) -> None:
    with KnowledgeStore(demo_db) as store:
        result = search(store, "产量")
    assert result["total"] > 0
    top = result["groups"]["metrics"][0]
    assert top["formula"] == "产量 = 打码量 + 跳码量 - 重码量"
    assert top["score"] >= 100 and top["match_reason"] == "完全相等"


def test_search_fuzzy_chinese_ordering(demo_db: Path) -> None:
    """「不良率」应按中文字序命中「不良品率」。"""
    with KnowledgeStore(demo_db) as store:
        result = search(store, "不良率", kinds=("metrics",))
    assert result["total"] >= 1
    assert any("不良品率" == m["chinese_name"] for m in result["groups"]["metrics"])


def test_search_kind_filter_and_empty(demo_db: Path) -> None:
    with KnowledgeStore(demo_db) as store:
        only_tables = search(store, "产量", kinds=("tables",))
        assert set(only_tables["groups"]) == {"tables"}
        assert search(store, "")["total"] == 0
        assert search(store, "完全不存在的词xyz")["total"] == 0


def test_search_matches_source_file_and_table(demo_db: Path) -> None:
    with KnowledgeStore(demo_db) as store:
        result = search(store, "dws_税利汇总")
    assert result["total"] > 0
    assert any(item["table_name"] == "cdw.dws_税利汇总"
               for item in result["groups"].get("metrics", []) + result["groups"].get("tables", []))


def test_detect_intent_and_entity() -> None:
    assert detect_intent("产量怎么算的") == "metric_formula"
    assert detect_intent("哪些表用到了打码量") == "metric_usage"
    assert detect_intent("卷烟产量流水从哪来") == "upstream"
    assert detect_intent("有哪些指标") == "list_metrics"
    assert detect_intent("dwd_卷烟产量明细有哪些字段") == "table_fields"
    assert detect_intent("") == "empty"
    assert extract_entity("产量怎么算的") == "产量"
    assert extract_entity("哪些表用到了打码量") == "打码量"
    assert extract_entity("不良品率是什么") == "不良品率"


def test_answer_metric_formula_rule_mode(demo_db: Path) -> None:
    with KnowledgeStore(demo_db) as store:
        result = answer(store, "产量怎么算的", use_llm=False)
    assert result["intent"] == "metric_formula"
    assert result["mode"] == "rule"
    assert "产量 = 打码量 + 跳码量 - 重码量" in result["answer"]
    assert "来源脚本" in result["answer"]
    assert result["llm"]["enabled"] is False  # 测试环境没有 LLM_API_KEY


def test_answer_usage_and_lineage(demo_db: Path) -> None:
    with KnowledgeStore(demo_db) as store:
        usage = answer(store, "哪些表用到了打码量", use_llm=False)
        assert usage["intent"] == "metric_usage"
        assert "dama_qty" in usage["answer"]
        lineage = answer(store, "卷烟产量明细从哪来", use_llm=False)
        assert lineage["intent"] == "upstream"
        assert "ods.ods_卷烟产量流水" in lineage["answer"]


def test_answer_list_and_unknown(demo_db: Path) -> None:
    with KnowledgeStore(demo_db) as store:
        listing = answer(store, "有哪些指标", use_llm=False)
        assert listing["intent"] == "list_metrics"
        assert "指标口径" in listing["answer"]
        unknown = answer(store, "一个不存在的问题xyz", use_llm=False)
        assert unknown["intent"] in ("search", "term_meaning")


def test_answer_llm_falls_back_when_unavailable(demo_db: Path, monkeypatch) -> None:
    """配了 key 但接口不可达 / 没有 key 时，必须降级为规则模式而不是报错。"""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with KnowledgeStore(demo_db) as store:
        result = answer(store, "产量怎么算的", use_llm=True)
    assert "产量 = 打码量 + 跳码量 - 重码量" in result["answer"]
    assert result["mode"].startswith("rule")


# --------------------------------------------------------------------------- #
# 6. 交付层：Markdown / CLI / HTTP
# --------------------------------------------------------------------------- #
def test_export_markdown(demo_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "业务口径知识库.md"
    with KnowledgeStore(demo_db) as store:
        info = export_markdown(store, out)
    assert info["path"] == str(out.resolve()) and info["bytes"] > 2000
    text = out.read_text(encoding="utf-8")
    for section in ("## 0. 概览", "## 1. 指标口径总览", "## 2. 指标口径明细",
                    "## 3. 业务术语词典", "## 4. 字段清单", "## 5. 业务规则", "## 8. 已知限制"):
        assert section in text
    assert "产量 = 打码量 + 跳码量 - 重码量" in text
    assert "`cdw.dwd_卷烟产量码段明细`" in text


def _run_kb(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "lineage.cli", "kb", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def test_cli_kb_build_search_show_ask(tmp_path: Path) -> None:
    db = tmp_path / "kb.db"
    built = _run_kb("build", str(WAREHOUSE), "--db", str(db))
    assert built.returncode == 0, built.stderr
    assert "指标口径（metrics）" in built.stdout
    assert "内容指纹" in built.stdout
    assert db.exists()

    found = _run_kb("search", "产量", "--db", str(db))
    assert found.returncode == 0
    assert "产量 = 打码量 + 跳码量 - 重码量" not in found.stdout  # warehouse 里没有码段口径
    assert "指标口径" in found.stdout

    shown = _run_kb("show", "defect_rate", "--db", str(db))
    assert shown.returncode == 0
    assert "不良品率" in shown.stdout and "依赖字段" in shown.stdout

    asked = _run_kb("ask", "不良品率怎么算的", "--db", str(db))
    assert asked.returncode == 0
    assert "规则模式" in asked.stdout and "不良品率" in asked.stdout

    missing = _run_kb("show", "没有这个指标", "--db", str(db))
    assert missing.returncode == 1


def test_cli_kb_demo_dirs_gives_chongma_formula(tmp_path: Path) -> None:
    db = tmp_path / "kb.db"
    built = _run_kb("build", str(WAREHOUSE), str(KNOWLEDGE_DEMO), "--db", str(db), "--quiet")
    assert built.returncode == 0, built.stderr
    asked = _run_kb("ask", "产量怎么算的", "--db", str(db))
    assert "产量 = 打码量 + 跳码量 - 重码量" in asked.stdout
    exported = _run_kb("export", "--db", str(db), "--md", str(tmp_path / "kb.md"))
    assert exported.returncode == 0 and (tmp_path / "kb.md").exists()


def test_cli_kb_db_missing(tmp_path: Path) -> None:
    proc = _run_kb("summary", "--db", str(tmp_path / "nope.db"))
    assert proc.returncode == 2
    assert "知识库不存在" in proc.stderr


# --------------------------------------------------------------------------- #
# 7. 真实示例目录基线
# --------------------------------------------------------------------------- #
def test_warehouse_kb_baseline(tmp_path: Path) -> None:
    report = build_knowledge_base([str(WAREHOUSE)], db_path=tmp_path / "kb.db")
    counts = report.counts
    assert counts["kb_metrics"] >= 20, counts
    assert counts["kb_fields"] >= 50, counts
    assert counts["kb_terms"] >= 50, counts
    assert counts["kb_rules"] >= 10, counts
    assert report.failures == []
    with KnowledgeStore(tmp_path / "kb.db") as store:
        assert store.get_metric("defect_rate")[0]["formula"].startswith("不良品率 = ")
        assert store.get_metric("sale_output_ratio")[0]["metric_type"] == "比率"
        assert store.get_table("dws_产销存汇总")["layer"] == "dws"


def test_knowledge_demo_dir_is_scannable() -> None:
    files = sorted(KNOWLEDGE_DEMO.rglob("*.sql"))
    assert len(files) == 3
    parser = SqlLineageParser()
    for path in files:
        assert parser.parse_file(path), f"{path.name} 未解析出语句"


def test_http_kb_handlers(demo_db: Path) -> None:
    """直接调用 HTTP 处理函数（不起服务），验证三个 kb 端点。"""
    from lineage.api_server import handle_kb_ask, handle_kb_metric, handle_kb_search, handle_kb_summary

    payload = {"db": str(demo_db)}
    summary = handle_kb_summary(dict(payload))
    assert summary["success"] and summary["counts"]["kb_metrics"] >= 20

    found = handle_kb_search({**payload, "query": "产量", "kinds": "metrics", "limit": 3})
    assert found["success"] and found["total"] == 3
    assert found["groups"]["metrics"][0]["formula"] == "产量 = 打码量 + 跳码量 - 重码量"

    asked = handle_kb_ask({**payload, "question": "产量怎么算的", "use_llm": "off"})
    assert asked["success"] and "打码量" in asked["answer"]

    metric = handle_kb_metric({**payload, "name": "chanliang_qty"})
    assert metric["success"] and metric["count"] == 1
    assert "口径：产量 = 打码量 + 跳码量 - 重码量" in metric["details"][0]["detail"]

    assert handle_kb_search({**payload, "query": ""})["success"] is False
    assert handle_kb_ask({**payload, "question": ""})["success"] is False


def test_existing_parser_still_works_after_kb_changes() -> None:
    """回归：P1 解析、P2 扫描不受 KB 模块影响。"""
    parser = SqlLineageParser(dialect="hive")
    statements = parser.parse_sql("CREATE TABLE t AS SELECT a.id AS id FROM s.a a;")
    assert statements[0]["output_table_names"] == ["t"]
    assert statements[0]["column_lineage"][0]["source_column"] == "id"
