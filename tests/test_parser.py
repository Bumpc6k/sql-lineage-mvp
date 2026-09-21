"""SQL 血缘解析单元测试（pytest）。

覆盖 README 中声明的 8 种 SQL 形态，外加 CLI / JSON 结构 / 多语句 / 边界场景。
运行：pytest -v   （在项目根目录执行）
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lineage_core.parser import (
    CONSTANT_MARKER,
    SqlLineageParser,
    dumps,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = PROJECT_ROOT / "examples"


@pytest.fixture(scope="module")
def parser() -> SqlLineageParser:
    return SqlLineageParser(dialect="hive")


def only(parser: SqlLineageParser, sql: str) -> dict:
    """解析单条语句，断言只产出一条结果并返回它。"""
    results = parser.parse_sql(sql)
    assert len(results) == 1
    return results[0]


def col_pairs(result: dict) -> set:
    """把字段级血缘压成 (目标表, 目标字段, 来源表, 来源字段) 集合，便于断言。"""
    return {
        (c["target_table"], c["target_column"], c["source_table"], c["source_column"])
        for c in result["column_lineage"]
    }


# --------------------------------------------------------------------------- #
# 形态 1：INSERT INTO / INSERT OVERWRITE TABLE ... SELECT
# --------------------------------------------------------------------------- #
def test_insert_select_table_lineage(parser: SqlLineageParser) -> None:
    sql = """
    INSERT INTO TABLE dws_order_agg
    SELECT org_id, SUM(qty) AS qty
    FROM dwd_order
    WHERE dt = '2026-01-01'
    GROUP BY org_id
    """
    result = only(parser, sql)
    assert result["task_type"] == "INSERT_SELECT"
    assert result["output_table_names"] == ["dws_order_agg"]
    assert result["input_table_names"] == ["dwd_order"]
    assert result["table_lineage"] == [{"source": "dwd_order", "target": "dws_order_agg"}]


def test_insert_overwrite_partition_filter(parser: SqlLineageParser) -> None:
    sql = """
    INSERT OVERWRITE TABLE dws.dws_卷烟产量汇总 PARTITION (dt = '2026-01-01')
    SELECT plant_code, SUM(output_qty) AS total_qty
    FROM dwd.dwd_卷烟产量明细
    WHERE prod_date >= '2026-01-01'
    GROUP BY plant_code
    """
    result = only(parser, sql)
    assert result["task_type"] == "INSERT_SELECT"
    assert result["output_tables"] == [{"name": "dws_卷烟产量汇总", "schema": "dws", "catalog": None}]
    assert result["output_table_names"] == ["dws.dws_卷烟产量汇总"]
    # PARTITION (dt='...') 与 WHERE 中的分区字段都要被提取
    assert result["partition_filters"] == {"dt": "2026-01-01"}
    assert any("prod_date" in f for f in result["filters"])


# --------------------------------------------------------------------------- #
# 形态 2：CREATE TABLE ... AS SELECT（CTAS）
# --------------------------------------------------------------------------- #
def test_ctas(parser: SqlLineageParser) -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS dwd.dwd_cigarette_output AS
    SELECT p.plant_code, p.prod_date, p.output_qty
    FROM ods.ods_卷烟产量 p
    WHERE p.dt = '2026-01-01'
    """
    result = only(parser, sql)
    assert result["task_type"] == "CTAS"
    assert result["output_table_names"] == ["dwd.dwd_cigarette_output"]
    assert result["input_table_names"] == ["ods.ods_卷烟产量"]
    assert result["table_lineage"] == [
        {"source": "ods.ods_卷烟产量", "target": "dwd.dwd_cigarette_output"}
    ]
    assert result["partition_filters"] == {"dt": "2026-01-01"}
    assert ("dwd.dwd_cigarette_output", "output_qty", "ods.ods_卷烟产量", "output_qty") in col_pairs(result)


# --------------------------------------------------------------------------- #
# 形态 3 + 4：多表 JOIN（含 LEFT JOIN）+ 带别名的表
# --------------------------------------------------------------------------- #
def test_multi_join_with_alias(parser: SqlLineageParser) -> None:
    sql = """
    INSERT OVERWRITE TABLE dws_order_org
    SELECT a.order_id, b.org_name, a.qty
    FROM dwd_order a
    LEFT JOIN dim_org b ON a.org_id = b.org_id
    INNER JOIN dim_channel c ON a.channel_id = c.channel_id
    WHERE a.dt = '2026-01-01'
    """
    result = only(parser, sql)
    assert result["input_table_names"] == ["dwd_order", "dim_org", "dim_channel"]
    assert len(result["table_lineage"]) == 3
    assert {p["source"] for p in result["table_lineage"]} == {"dwd_order", "dim_org", "dim_channel"}
    # 别名必须被解析成真实表名
    assert ("dws_order_org", "order_id", "dwd_order", "order_id") in col_pairs(result)
    assert ("dws_order_org", "org_name", "dim_org", "org_name") in col_pairs(result)
    assert ("dws_order_org", "qty", "dwd_order", "qty") in col_pairs(result)
    # JOIN 类型与 ON 条件
    joins = {j["type"] for j in result["joins"]}
    assert joins == {"LEFT JOIN", "INNER JOIN"}
    assert all(j["on"] for j in result["joins"])


# --------------------------------------------------------------------------- #
# 形态 5：子查询（FROM (SELECT ...) t）
# --------------------------------------------------------------------------- #
def test_derived_subquery(parser: SqlLineageParser) -> None:
    sql = """
    INSERT INTO TABLE ads_order
    SELECT t.order_id, t.qty
    FROM (
        SELECT o.order_id, o.qty
        FROM ods_order o
        WHERE o.dt = '2026-01-01' AND o.qty > 0
    ) t
    """
    result = only(parser, sql)
    assert result["input_table_names"] == ["ods_order"]
    assert ("ads_order", "order_id", "ods_order", "order_id") in col_pairs(result)
    assert ("ads_order", "qty", "ods_order", "qty") in col_pairs(result)


# --------------------------------------------------------------------------- #
# 形态 6：CTE（WITH x AS (SELECT ...) SELECT ...）
# --------------------------------------------------------------------------- #
def test_cte_pushdown_to_physical_table(parser: SqlLineageParser) -> None:
    sql = """
    WITH base AS (
        SELECT order_id, org_id, qty FROM dwd_order WHERE dt = '2026-01-01'
    ),
    org AS (
        SELECT org_id, org_name FROM dim_org
    )
    INSERT INTO TABLE dws_order_org
    SELECT b.order_id, b.qty, o.org_name
    FROM base b
    JOIN org o ON b.org_id = o.org_id
    """
    result = only(parser, sql)
    # CTE 名不能出现在输入表里，必须下推到真实物理表
    assert result["input_table_names"] == ["dwd_order", "dim_org"]
    assert "base" not in result["input_table_names"]
    assert ("dws_order_org", "order_id", "dwd_order", "order_id") in col_pairs(result)
    assert ("dws_order_org", "qty", "dwd_order", "qty") in col_pairs(result)
    assert ("dws_order_org", "org_name", "dim_org", "org_name") in col_pairs(result)


# --------------------------------------------------------------------------- #
# 形态 7：UNION / UNION ALL
# --------------------------------------------------------------------------- #
def test_union_all_both_branches(parser: SqlLineageParser) -> None:
    sql = """
    INSERT OVERWRITE TABLE ads_union
    SELECT id, amt FROM ods_a WHERE dt = '2026-01-01'
    UNION ALL
    SELECT id, amt FROM ods_b WHERE dt = '2026-01-01'
    """
    result = only(parser, sql)
    assert result["input_table_names"] == ["ods_a", "ods_b"]
    assert len(result["table_lineage"]) == 2
    # 两个分支都要产出字段级血缘，且目标列名按位置对齐
    assert ("ads_union", "id", "ods_a", "id") in col_pairs(result)
    assert ("ads_union", "id", "ods_b", "id") in col_pairs(result)
    assert ("ads_union", "amt", "ods_a", "amt") in col_pairs(result)
    assert ("ads_union", "amt", "ods_b", "amt") in col_pairs(result)


def test_union_inside_derived_table(parser: SqlLineageParser) -> None:
    sql = """
    INSERT OVERWRITE TABLE ads_total
    SELECT src_type, SUM(qty) AS qty
    FROM (
        SELECT '自产' AS src_type, output_qty AS qty FROM dwd_prod
        UNION ALL
        SELECT '外购' AS src_type, purchase_qty AS qty FROM ods_buy
    ) u
    GROUP BY src_type
    """
    result = only(parser, sql)
    assert result["input_table_names"] == ["dwd_prod", "ods_buy"]
    assert ("ads_total", "qty", "dwd_prod", "output_qty") in col_pairs(result)
    assert ("ads_total", "qty", "ods_buy", "purchase_qty") in col_pairs(result)
    # 常量来源（'自产'/'外购'）如实标注为常量，而不是编造来源表
    src_type_rows = [c for c in result["column_lineage"] if c["target_column"] == "src_type"]
    assert src_type_rows
    assert all(c["source_column"] == CONSTANT_MARKER for c in src_type_rows)
    assert all(c["source_table"] is None for c in src_type_rows)


# --------------------------------------------------------------------------- #
# 形态 8：分区过滤条件提取
# --------------------------------------------------------------------------- #
def test_partition_filters_from_where_and_partition_clause(parser: SqlLineageParser) -> None:
    sql = """
    INSERT OVERWRITE TABLE t_target PARTITION (dt = '2026-03-01', hr = '08')
    SELECT a.x
    FROM src_a a
    WHERE a.dt = '2026-03-01' AND a.plant_code = 'P001' AND a.status IN ('1', '2')
    """
    result = only(parser, sql)
    # PARTITION 子句优先
    assert result["partition_filters"] == {"dt": "2026-03-01", "hr": "08"}
    assert len(result["filters"]) == 1
    assert "a.dt = '2026-03-01'" in result["filters"][0]
    # 非分区字段不会混进 partition_filters
    assert "plant_code" not in result["partition_filters"]
    assert "status" not in result["partition_filters"]


def test_partition_filter_in_where_only(parser: SqlLineageParser) -> None:
    sql = "SELECT id FROM ods_t WHERE dt = '2026-01-01' AND pt = 'cn' AND id > 0"
    result = only(parser, sql)
    assert result["partition_filters"] == {"dt": "2026-01-01", "pt": "cn"}


# --------------------------------------------------------------------------- #
# 纯 SELECT（无输出表）
# --------------------------------------------------------------------------- #
def test_plain_select_has_no_output_table(parser: SqlLineageParser) -> None:
    sql = """
    SELECT d.prod_date, SUM(d.qty) AS total_qty
    FROM dwd_detail d
    LEFT JOIN dim_plant p ON d.plant_code = p.plant_code
    WHERE d.prod_date BETWEEN '2026-01-01' AND '2026-01-31'
    GROUP BY d.prod_date
    ORDER BY total_qty DESC
    LIMIT 10
    """
    result = only(parser, sql)
    assert result["task_type"] == "SELECT"
    assert result["output_tables"] == []
    assert result["table_lineage"] == []
    assert result["input_table_names"] == ["dwd_detail", "dim_plant"]
    assert ("(query_result)", "total_qty", "dwd_detail", "qty") in col_pairs(result)


# --------------------------------------------------------------------------- #
# 边界场景：SELECT * / 未消歧同名字段 / 常量列 / 多语句
# --------------------------------------------------------------------------- #
def test_select_star_marked_unresolved(parser: SqlLineageParser) -> None:
    sql = "CREATE TABLE x AS SELECT * FROM ods_a a JOIN ods_b b ON a.id = b.id"
    result = only(parser, sql)
    assert result["input_table_names"] == ["ods_a", "ods_b"]
    star_rows = [c for c in result["column_lineage"] if c["source_column"] == "*"]
    # 没有元数据，SELECT * 无法展开列：必须如实标注 resolved=False
    assert len(star_rows) == 2
    assert all(c["resolved"] is False for c in star_rows)


def test_ambiguous_unqualified_column(parser: SqlLineageParser) -> None:
    sql = "INSERT INTO t2 SELECT id FROM a JOIN b ON a.id = b.id"
    result = only(parser, sql)
    ambiguous = [c for c in result["column_lineage"] if c["target_column"] == "id"]
    assert ambiguous
    assert all(c["resolved"] is False and c["source_table"] is None for c in ambiguous)


def test_unknown_alias_kept_as_is(parser: SqlLineageParser) -> None:
    sql = "INSERT INTO t2 SELECT zz.col1 FROM a"
    result = only(parser, sql)
    row = result["column_lineage"][0]
    assert row["source_table"] == "zz"
    assert row["resolved"] is False


def test_constant_output_column(parser: SqlLineageParser) -> None:
    sql = "INSERT INTO t2 SELECT COUNT(1) AS cnt, '常量' AS tag, a.id FROM a"
    result = only(parser, sql)
    by_target = {c["target_column"]: c for c in result["column_lineage"]}
    assert by_target["cnt"]["source_column"] == CONSTANT_MARKER
    assert by_target["cnt"]["source_table"] is None
    assert by_target["tag"]["source_column"] == CONSTANT_MARKER
    assert by_target["id"]["source_column"] == "id"
    assert by_target["id"]["source_table"] == "a"


def test_multi_statement_file(parser: SqlLineageParser) -> None:
    sql = """
    CREATE TABLE dwd.t1 AS SELECT id FROM ods.t0 WHERE dt = '2026-01-01';
    INSERT OVERWRITE TABLE dws.t2 SELECT id FROM dwd.t1;
    SELECT COUNT(1) AS cnt FROM dws.t2;
    """
    results = parser.parse_sql(sql)
    assert [r["task_type"] for r in results] == ["CTAS", "INSERT_SELECT", "SELECT"]
    assert [r["statement_index"] for r in results] == [1, 2, 3]


def test_aggregate_dedup_and_json_serializable(parser: SqlLineageParser) -> None:
    statements = parser.parse_sql(
        "SELECT id FROM ods_a WHERE dt = '2026-01-01';\n"
        "SELECT id FROM ods_a WHERE dt = '2026-01-01';"
    )
    report = parser.aggregate(statements)
    assert report["statement_count"] == 2
    assert report["input_table_names"] == ["ods_a"]
    assert len(report["column_lineage"]) == 1  # 完全相同的字段级血缘被去重
    text = dumps(report)
    assert json.loads(text)["dialect"] == "hive"
    assert "ods_a" in text  # ensure_ascii=False 保留中文/原样


def test_summary_text_renders(parser: SqlLineageParser) -> None:
    statements = parser.parse_sql("CREATE TABLE t AS SELECT a FROM s WHERE dt = '2026-01-01'")
    text = parser.summary_text(statements)
    assert "task_type = CTAS" in text
    assert "s  -->  t" in text
    assert "分区过滤" in text


def test_invalid_sql_raises_valueerror(parser: SqlLineageParser) -> None:
    with pytest.raises(ValueError):
        parser.parse_sql("SELECT FROM WHERE (((")


# --------------------------------------------------------------------------- #
# 示例文件 & CLI 集成测试
# --------------------------------------------------------------------------- #
def test_all_examples_parse(parser: SqlLineageParser) -> None:
    files = sorted(EXAMPLES_DIR.glob("*.sql"))
    assert len(files) >= 5
    total_statements = 0
    for path in files:
        results = parser.parse_file(path)
        assert results, f"{path.name} 未解析出任何语句"
        for r in results:
            assert r["task_type"] in {"INSERT_SELECT", "CTAS", "SELECT"}
            assert r["input_table_names"], f"{path.name} 未解析出输入表"
            assert r["table_lineage"] or r["task_type"] == "SELECT"
        total_statements += len(results)
    assert total_statements >= 6


def _run_cli(args, stdin_text=None):
    return subprocess.run(
        [sys.executable, "-m", "lineage.cli", *args],
        cwd=PROJECT_ROOT,
        input=stdin_text,
        capture_output=True,
        text=True,
    )


def test_cli_json_output() -> None:
    proc = _run_cli(
        [str(EXAMPLES_DIR / "02_dwd_to_dws_join.sql"), "--output", "json", "--no-color"]
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["dialect"] == "hive"
    assert report["statement_count"] == 1
    stmt = report["statements"][0]
    for key in (
        "task_type",
        "output_tables",
        "input_tables",
        "table_lineage",
        "column_lineage",
        "filters",
    ):
        assert key in stmt, f"JSON 缺少字段 {key}"
    assert stmt["task_type"] == "INSERT_SELECT"
    assert "dws.dws_卷烟产量汇总" in report["output_table_names"]


def test_cli_text_and_dialect_option() -> None:
    proc = _run_cli([str(EXAMPLES_DIR / "06_adhoc_select.sql"), "--no-color", "-d", "spark"])
    assert proc.returncode == 0, proc.stderr
    assert "task_type = SELECT" in proc.stdout
    assert "dialect=spark" in proc.stdout


def test_cli_stdin() -> None:
    sql = "CREATE TABLE t AS SELECT a.x FROM ods_a a WHERE a.dt = '2026-01-01'"
    proc = _run_cli(["-", "--output", "json"], stdin_text=sql)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    stmt = report["statements"][0]
    assert stmt["task_type"] == "CTAS"
    assert stmt["input_table_names"] == ["ods_a"]
    assert stmt["partition_filters"] == {"dt": "2026-01-01"}


def test_cli_multiple_files_and_save(tmp_path: Path) -> None:
    out_file = tmp_path / "report.json"
    proc = _run_cli(
        [
            str(EXAMPLES_DIR / "01_ods_to_dwd_ctas.sql"),
            str(EXAMPLES_DIR / "05_pipeline_multi_statement.sql"),
            "--output",
            "json",
            "--save",
            str(out_file),
            "--quiet",
        ]
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["file_count"] == 2
    assert report["statement_count"] == 4
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert saved["statement_count"] == 4


def test_cli_missing_file_returns_2() -> None:
    proc = _run_cli(["/tmp/definitely-not-here.sql"])
    assert proc.returncode == 2
    assert "文件不存在" in proc.stderr


def test_cli_help() -> None:
    proc = _run_cli(["--help"])
    assert proc.returncode == 0
    assert "SQL 血缘解析" in proc.stdout
