#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""脚本内嵌 SQL 提取与解析（``lineage.script_parser``）测试。

覆盖：

* :func:`detect_script_kind` —— SQL / Shell / Python / unknown 判定，含海豚任务类型兜底、
  内容特征优先于任务类型（SHELL 任务里被剥壳的纯 SQL 要判成 sql）；
* Shell 提取 —— ``hive -e``（单条 / 多语句）、``beeline -e``、``beeline -f``（含本地文件解析）、
  ``spark-sql -e``、``impala-shell -q``、``mysql -e``、``psql -c``、heredoc（三种 tag 写法）、
  ``SQL="..."`` 变量（单独用 / 被命令引用 / 引用未定义变量）；
* Python 提取 —— ``spark.sql`` 内联 / 三引号 / f-string / 变量、``pd.read_sql``、
  ``sqlalchemy.text``、模块级三引号常量；**文档字符串与注释里的 SQL 不算**；
* 降级与边界 —— 空脚本、只有 echo 的脚本、注释里的 ``hive <<EOF``、``-f`` 外部文件、
  ``spark.sql(表达式)`` 无法静态求值、f-string 占位符、解析失败记 errors 而不抛异常；
* :func:`parse_script` —— 表级 / 字段级血缘聚合、``source_hint`` 溯源、``unresolved_hints`` 去重；
* 工作流集成 —— ``lineage.workflow._parse_tasks`` 对 SHELL / PYTHON 任务按类型分发，
  回填 ``script_kind`` / ``sql_count`` / ``unresolved_hints``；
* CLI —— ``python -m lineage.cli parse-script`` 的 text / json 输出；
* 示例脚本 —— ``examples/scripts/`` 两个真实风格脚本必须能被完整解析。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lineage_core.parser import SqlLineageParser
from lineage_core.script_parser import (
    detect_script_kind,
    extract_sqls,
    parse_script,
    parse_script_file,
    script_summary_text,
)
from lineage.ds.workflow import _parse_tasks

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_SCRIPTS = PROJECT_ROOT / "examples" / "scripts"
SHELL_EXAMPLE = EXAMPLES_SCRIPTS / "etl_ods_每日抽取.sh"
PYTHON_EXAMPLE = EXAMPLES_SCRIPTS / "etl_dws_pyspark.py"


def pairs(result: dict) -> set:
    """表级血缘压缩成 (source, target) 集合。"""
    return {(p["source"], p["target"]) for p in result["table_lineage"]}


def cols(result: dict) -> set:
    """字段级血缘压缩成 (目标表, 目标列, 来源表, 来源列) 集合。"""
    return {
        (c["target_table"], c["target_column"], c["source_table"], c["source_column"])
        for c in result["column_lineage"]
    }


# --------------------------------------------------------------------------- #
# 1) detect_script_kind
# --------------------------------------------------------------------------- #
def test_kind_shell_by_command():
    assert detect_script_kind('hive -e "SELECT 1 FROM t"') == "shell"


def test_kind_shell_by_shebang_and_echo():
    text = "#!/bin/bash\nset -e\necho hi\nDT=$(date +%F)\n"
    assert detect_script_kind(text) == "shell"


def test_kind_shell_by_beeline():
    assert detect_script_kind("beeline -f etl/a.sql") == "shell"


def test_kind_python_by_shebang():
    assert detect_script_kind("#!/usr/bin/env python3\nprint('hi')\n") == "python"


def test_kind_python_by_import_and_spark():
    text = "from pyspark.sql import SparkSession\nspark = SparkSession.builder.getOrCreate()\n"
    assert detect_script_kind(text) == "python"


def test_kind_python_by_spark_sql_call():
    assert detect_script_kind('spark.sql("SELECT 1")') == "python"


def test_kind_sql_pure_insert():
    sql = "INSERT OVERWRITE TABLE a SELECT x FROM b"
    assert detect_script_kind(sql) == "sql"


def test_kind_sql_with_leading_comment():
    sql = "-- 注释\n/* 块注释 */\nSELECT a FROM b"
    assert detect_script_kind(sql) == "sql"


def test_kind_unknown_for_comment_only():
    assert detect_script_kind("# 只有注释，什么也没有") == "unknown"


def test_kind_unknown_for_empty():
    assert detect_script_kind("   \n  ") == "unknown"


def test_kind_falls_back_to_task_type():
    assert detect_script_kind("some random text", "SQL") == "sql"
    assert detect_script_kind("some random text", "PYTHON") == "python"
    assert detect_script_kind("some random text", "SHELL") == "shell"


def test_kind_task_type_case_insensitive():
    assert detect_script_kind("random", "shell") == "shell"
    assert detect_script_kind("random", "py") == "python"
    assert detect_script_kind("random", "hive") == "sql"


def test_kind_content_wins_over_task_type():
    """海豚 SHELL 任务的 rawScript 被剥壳后就是纯 SQL —— 应判成 sql 而不是 shell。"""
    sql = "INSERT OVERWRITE TABLE ods.a PARTITION (dt='1') SELECT x AS x FROM src.b"
    assert detect_script_kind(sql, "SHELL") == "sql"


# --------------------------------------------------------------------------- #
# 2) Shell 提取
# --------------------------------------------------------------------------- #
def test_shell_hive_e_single():
    entries, hints = extract_sqls('hive -e "SELECT a FROM ods.t1"', "shell")
    assert len(entries) == 1
    assert entries[0]["sql"] == "SELECT a FROM ods.t1"
    assert entries[0]["line"] == 1
    assert hints == []


def test_shell_hive_e_multi_statement():
    text = (
        'hive -e "\n'
        "INSERT OVERWRITE TABLE ods.a PARTITION (dt='1') SELECT x AS x FROM src.b;\n"
        "INSERT OVERWRITE TABLE ods.c PARTITION (dt='1') SELECT y AS y FROM src.d;\n"
        '"'
    )
    result = parse_script(text, "shell")
    assert result["sql_count"] == 1                  # 一条内嵌 SQL（含两条语句）
    assert result["statement_count"] == 2
    assert pairs(result) == {("src.b", "ods.a"), ("src.d", "ods.c")}


def test_shell_hive_e_single_quotes():
    entries, _ = extract_sqls("hive -e 'SELECT a FROM ods.t1'", "shell")
    assert entries[0]["sql"] == "SELECT a FROM ods.t1"


def test_shell_hive_e_with_database_flag():
    entries, _ = extract_sqls('hive --database dw -e "SELECT a FROM ods.t1"', "shell")
    assert len(entries) == 1 and entries[0]["sql"] == "SELECT a FROM ods.t1"


def test_shell_beeline_e():
    text = 'beeline -u "jdbc:hive2://h:10000" -e "INSERT INTO TABLE c SELECT x FROM a"'
    result = parse_script(text, "shell")
    assert pairs(result) == {("a", "c")}


def test_shell_beeline_f_unresolved():
    entries, hints = extract_sqls("beeline -f etl/dws_效率.sql", "shell")
    assert len(entries) == 1
    assert entries[0]["sql"] is None
    assert entries[0]["resolved"] is False
    assert "etl/dws_效率.sql" in entries[0]["source_hint"]
    assert any("外部文件" in h for h in hints)


def test_shell_beeline_f_resolve_local_file(tmp_path):
    sql_file = tmp_path / "etl" / "a.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text("INSERT OVERWRITE TABLE o.a PARTITION (dt='1') SELECT x AS x FROM s.b;",
                        encoding="utf-8")
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/bash\nbeeline -f etl/a.sql\n", encoding="utf-8")
    result = parse_script_file(script, "shell", resolve_files=True)
    assert result["sql_count"] == 1
    assert pairs(result) == {("s.b", "o.a")}


def test_shell_spark_sql_e():
    result = parse_script('spark-sql -e "INSERT INTO TABLE c SELECT x FROM a"', "shell")
    assert pairs(result) == {("a", "c")}


def test_shell_impala_shell_q():
    result = parse_script('impala-shell -q "INSERT INTO TABLE c SELECT x FROM a"', "shell")
    assert pairs(result) == {("a", "c")}


def test_shell_mysql_e():
    result = parse_script('mysql -h db -e "INSERT INTO TABLE c SELECT x FROM a"', "shell")
    assert pairs(result) == {("a", "c")}


def test_shell_psql_c():
    result = parse_script('psql -c "SELECT x FROM a"', "shell")
    assert result["statement_count"] == 1


def test_shell_heredoc_basic():
    text = (
        "hive <<EOF\n"
        "INSERT OVERWRITE TABLE o.a PARTITION (dt='1')\n"
        "SELECT x AS x FROM s.b;\n"
        "EOF\n"
    )
    result = parse_script(text, "shell")
    assert pairs(result) == {("s.b", "o.a")}
    assert result["sqls"][0]["source_hint"].startswith("hive heredoc <<EOF")


def test_shell_heredoc_quoted_tag():
    text = "beeline <<'SQL'\nSELECT x FROM s.b;\nSQL\n"
    entries, _ = extract_sqls(text, "shell")
    assert len(entries) == 1 and entries[0]["sql"].strip().startswith("SELECT x FROM s.b")


def test_shell_heredoc_dash_tag_with_tabs():
    text = "hive <<-EOF\n\tSELECT x FROM s.b;\n\tEOF\n"
    entries, _ = extract_sqls(text, "shell")
    assert len(entries) == 1
    assert "SELECT x FROM s.b" in entries[0]["sql"]


def test_shell_heredoc_unterminated_ignored():
    entries, _ = extract_sqls("hive <<EOF\nSELECT x FROM s.b;\n", "shell")
    assert entries == []


def test_shell_comment_heredoc_is_not_heredoc():
    """注释里的 ``hive <<EOF`` 不能当 heredoc（否则会把整段脚本吞进去）。"""
    text = (
        "#!/bin/bash\n"
        "# 用法示例：hive <<EOF heredoc\n"
        "hive -e \"INSERT OVERWRITE TABLE o.a PARTITION (dt='1') SELECT x AS x FROM s.b;\"\n"
        "echo done\n"
    )
    result = parse_script(text, "shell")
    assert result["sql_count"] == 1
    assert pairs(result) == {("s.b", "o.a")}


def test_shell_variable_referenced_by_command():
    text = (
        'SQL="INSERT OVERWRITE TABLE o.a PARTITION (dt=\'1\') SELECT x AS x FROM s.b"\n'
        'hive -e "$SQL"\n'
    )
    result = parse_script(text, "shell")
    assert result["sql_count"] == 1
    assert pairs(result) == {("s.b", "o.a")}
    assert "变量" in result["sqls"][0]["source_hint"]
    assert result["sqls"][0]["note"] == "SQL 来自 shell 变量展开"


def test_shell_multiline_variable_referenced():
    text = (
        'SQL="INSERT OVERWRITE TABLE o.a PARTITION (dt=\'1\')\n'
        'SELECT x AS x FROM s.b\n'
        'GROUP BY x"\n'
        'beeline -e "$SQL"\n'
    )
    result = parse_script(text, "shell")
    assert pairs(result) == {("s.b", "o.a")}


def test_shell_variable_standalone_when_never_used():
    text = 'SQL="INSERT OVERWRITE TABLE o.a SELECT x AS x FROM s.b"\necho $SQL\n'
    result = parse_script(text, "shell")
    assert result["sql_count"] == 1
    assert pairs(result) == {("s.b", "o.a")}
    assert "变量内嵌 SQL" in result["sqls"][0]["note"]


def test_shell_undefined_variable_records_hint():
    result = parse_script('beeline -e "$NOT_DEFINED"', "shell")
    assert result["sql_count"] == 0
    assert any("未在脚本内定义" in h for h in result["unresolved_hints"])
    assert any("未从该 shell 脚本中提取到任何内嵌 SQL" in h for h in result["unresolved_hints"])


def test_shell_echo_not_sql():
    result = parse_script('#!/bin/bash\necho "INSERT OVERWRITE TABLE x SELECT a FROM b"\n', "shell")
    assert result["sql_count"] == 0


def test_shell_commented_command_not_extracted():
    text = '# hive -e "INSERT OVERWRITE TABLE o.a SELECT x FROM s.b"\necho skip\n'
    result = parse_script(text, "shell")
    assert result["sql_count"] == 0


def test_shell_non_sql_command_argument_skipped():
    result = parse_script('hive -e "show databases"', "shell")
    assert result["sql_count"] == 0
    assert any("不像 SQL" in h for h in result["unresolved_hints"])


def test_shell_multiple_patterns_together():
    text = (
        "#!/bin/bash\n"
        'DT="2026-01-01"\n'
        'hive -e "INSERT OVERWRITE TABLE o.a PARTITION (dt=\'2026-01-01\') SELECT x AS x FROM s.b;"\n'
        'SQL_STOCK="INSERT OVERWRITE TABLE o.c PARTITION (dt=\'2026-01-01\') SELECT y AS y FROM s.d"\n'
        'beeline -e "$SQL_STOCK"\n'
        "hive <<EOF\n"
        "INSERT OVERWRITE TABLE o.e PARTITION (dt='2026-01-01') SELECT z AS z FROM s.f;\n"
        "EOF\n"
        "spark-sql -f etl/missing.sql\n"
    )
    result = parse_script(text, "shell")
    assert result["sql_count"] == 3
    assert result["statement_count"] == 3
    assert pairs(result) == {("s.b", "o.a"), ("s.d", "o.c"), ("s.f", "o.e")}
    assert any("外部文件" in h for h in result["unresolved_hints"])


def test_shell_column_lineage_carries_source_hint():
    text = (
        "hive <<EOF\n"
        "INSERT OVERWRITE TABLE o.a PARTITION (dt='1') SELECT b.x AS x FROM s.b b;\n"
        "EOF\n"
    )
    result = parse_script(text, "shell")
    assert cols(result) == {("o.a", "x", "s.b", "x")}
    assert result["column_lineage"][0]["source_hint"].startswith("hive heredoc")


# --------------------------------------------------------------------------- #
# 3) Python 提取
# --------------------------------------------------------------------------- #
def test_python_spark_sql_inline():
    result = parse_script('spark.sql("INSERT INTO TABLE c SELECT x FROM a")')
    assert result["kind"] == "python"
    assert pairs(result) == {("a", "c")}


def test_python_spark_sql_triple_quoted():
    text = 'spark.sql("""\nINSERT OVERWRITE TABLE c PARTITION (dt=\'1\')\nSELECT x AS x FROM a\n""")\n'
    result = parse_script(text)
    assert pairs(result) == {("a", "c")}
    assert result["sqls"][0]["source_hint"].startswith("spark.sql() 第 1 行")


def test_python_spark_sql_variable():
    text = (
        'SQL_A = """\nINSERT INTO TABLE c SELECT x AS x FROM a\n"""\n'
        "spark.sql(SQL_A)\n"
    )
    result = parse_script(text)
    assert pairs(result) == {("a", "c")}
    assert "变量定义于第 1 行" in result["sqls"][0]["source_hint"]


def test_python_spark_sql_fstring_placeholder():
    text = 'DT = "2026-01-01"\nspark.sql(f"SELECT x FROM a WHERE dt = \'{DT}\'")\n'
    result = parse_script(text)
    assert result["sql_count"] == 1
    assert result["input_tables"] == ["a"]                 # 占位符替换成 0 后仍能解析
    assert result["sqls"][0]["placeholders"] == ["DT"]
    assert any("f-string 占位符 {DT}" in h for h in result["unresolved_hints"])


def test_python_spark_sql_unknown_variable():
    result = parse_script("spark.sql(some_expr_undefined)")
    assert result["sql_count"] == 0
    assert any("静态层面无法求值" in h for h in result["unresolved_hints"])


def test_python_spark_sql_empty_argument():
    result = parse_script("spark.sql()")
    assert result["sql_count"] == 0
    assert any("参数为空" in h for h in result["unresolved_hints"])


def test_python_pandas_read_sql():
    text = 'df = pd.read_sql("SELECT x FROM a WHERE flag = 1", conn)\n'
    result = parse_script(text)
    assert pairs(result) == set()          # 纯查询没有输出表
    assert result["input_tables"] == ["a"]
    assert result["sqls"][0]["source_hint"].startswith("pd.read_sql()")


def test_python_read_sql_query():
    text = 'df = pd.read_sql_query("SELECT x FROM a", conn)\n'
    result = parse_script(text)
    assert result["input_tables"] == ["a"]
    assert result["sqls"][0]["source_hint"].startswith("pd.read_sql_query()")


def test_python_sqlalchemy_text():
    text = 'from sqlalchemy import text\nq = conn.execute(text("SELECT x FROM a"))\n'
    result = parse_script(text)
    assert result["input_tables"] == ["a"]
    assert result["sqls"][0]["source_hint"].startswith("text()")


def test_python_plain_text_call_not_sql_ignored():
    text = "from mylib import text\nx = text('hello world')\n"
    result = parse_script(text)
    assert result["sql_count"] == 0


def test_python_module_level_triple_quoted_constant():
    text = 'SQL = """\nINSERT INTO TABLE c SELECT x AS x FROM a\n"""\nprint(SQL)\n'
    result = parse_script(text)
    assert pairs(result) == {("a", "c")}
    assert "python 字符串字面量" in result["sqls"][0]["source_hint"]


def test_python_docstring_not_extracted():
    """文档字符串里提到的 spark.sql("SELECT ...") 只是文字，不是代码。"""
    text = (
        'def f():\n'
        '    """示例：spark.sql("INSERT INTO TABLE x SELECT a FROM y")"""\n'
        "    pass\n"
    )
    result = parse_script(text)
    assert result["sql_count"] == 0


def test_python_comment_not_extracted():
    text = '# 参见 spark.sql("INSERT INTO TABLE x SELECT a FROM y")\npass\n'
    result = parse_script(text)
    assert result["sql_count"] == 0


def test_python_realistic_spark_sql_chain():
    text = (
        "import pandas as pd\n"
        "from pyspark.sql import SparkSession\n"
        "spark = SparkSession.builder.getOrCreate()\n"
        "DT = '2026-01-01'\n"
        'SQL_A = """\nINSERT OVERWRITE TABLE cdw.a PARTITION (dt=\'2026-01-01\')\n'
        'SELECT p.x AS x FROM ods.p\n"""\n'
        "spark.sql(SQL_A)\n"
        'spark.sql("INSERT OVERWRITE TABLE ads.b PARTITION (dt=\'2026-01-01\') '
        'SELECT a.x AS x FROM cdw.a a")\n'
    )
    result = parse_script(text)
    assert result["sql_count"] == 2
    assert pairs(result) == {("ods.p", "cdw.a"), ("cdw.a", "ads.b")}
    assert cols(result) == {("cdw.a", "x", "ods.p", "x"), ("ads.b", "x", "cdw.a", "x")}


# --------------------------------------------------------------------------- #
# 4) 降级与边界
# --------------------------------------------------------------------------- #
def test_parse_script_empty_text():
    result = parse_script("", None)
    assert result["kind"] == "unknown"
    assert result["sql_count"] == 0
    assert result["table_lineage"] == []
    assert result["unresolved_hints"]


def test_parse_script_whitespace_only():
    result = parse_script("   \n\t\n", "sql")
    assert result["sql_count"] == 0


def test_parse_script_sql_kind_whole_text():
    sql = "INSERT OVERWRITE TABLE o.a PARTITION (dt='1') SELECT b.x AS x FROM s.b b"
    result = parse_script(sql, "sql")
    assert result["sql_count"] == 1
    assert result["sqls"][0]["source_hint"] == "(整段脚本按 SQL 解析)"
    assert pairs(result) == {("s.b", "o.a")}


def test_parse_script_undetectable_kind_returns_empty():
    """传 kind=unknown 时按内容判定；判定不出 SQL 就返回空结果 + 提示。"""
    result = parse_script("echo nothing", "unknown")
    assert result["sql_count"] == 0
    assert result["table_lineage"] == []
    assert result["unresolved_hints"]


def test_parse_script_broken_sql_records_error_not_raise():
    text = 'hive -e "INSERT OVERWRITE TABLE a PARTITION SELECT FROM"'
    result = parse_script(text, "shell")
    assert result["sql_count"] == 1
    assert result["statement_count"] == 0
    assert result["errors"]
    assert any("解析失败" in h for h in result["unresolved_hints"])


def test_parse_script_hints_are_deduplicated():
    text = (
        'spark.sql(f"SELECT {DT} FROM a")\n'
        'spark.sql(f"SELECT {DT} FROM b")\n'
    )
    result = parse_script(text)
    hints = [h for h in result["unresolved_hints"] if "f-string 占位符 {DT}" in h]
    assert len(hints) == 2          # 两条不同的 source_hint，各提示一次
    assert len(set(result["unresolved_hints"])) == len(result["unresolved_hints"])


def test_parse_script_aggregates_multiple_sqls():
    text = (
        'hive -e "INSERT OVERWRITE TABLE o.a PARTITION (dt=\'1\') SELECT x AS x FROM s.b;"\n'
        'hive -e "INSERT OVERWRITE TABLE o.c PARTITION (dt=\'1\') SELECT y AS y FROM s.d;"\n'
    )
    result = parse_script(text, "shell")
    assert result["sql_count"] == 2
    assert result["statement_count"] == 2
    assert result["input_tables"] == ["s.b", "s.d"]
    assert result["output_tables"] == ["o.a", "o.c"]
    assert len(result["table_lineage"]) == 2
    assert result["column_lineage_count"] == 2
    assert all("via" in p for p in result["table_lineage"])


def test_parse_script_source_and_task_type_recorded():
    result = parse_script('hive -e "SELECT x FROM a"', "shell", task_type="SHELL", source="t1")
    assert result["source"] == "t1"
    assert result["task_type"] == "SHELL"
    assert result["detected_kind"] == "shell"


def test_script_summary_text_contains_key_sections():
    result = parse_script(
        'hive -e "INSERT OVERWRITE TABLE o.a PARTITION (dt=\'1\') SELECT x AS x FROM s.b;"\n'
        "spark-sql -f etl/missing.sql\n",
        "shell",
    )
    text = script_summary_text(result)
    assert "脚本内嵌 SQL 血缘报告" in text
    assert "s.b  -->  o.a" in text
    assert "未解析 / 需人工确认" in text


# --------------------------------------------------------------------------- #
# 5) 示例脚本（真实风格，端到端）
# --------------------------------------------------------------------------- #
def test_example_shell_script():
    result = parse_script_file(SHELL_EXAMPLE)
    assert result["kind"] == "shell"
    assert result["sql_count"] == 3
    assert result["statement_count"] == 4
    assert pairs(result) == {
        ("src.erp_生产工单明细", "ods.ods_卷烟产量流水"),
        ("src.wms_库存快照", "ods.ods_成品库存快照"),
        ("src.wms_库存快照", "ods.ods_库存基线"),
        ("src.erp_税利上缴", "ods.ods_税利上缴流水"),
    }
    # 外部 SQL 文件引用如实记入 unresolved_hints
    assert any("etl/ods_设备运行工况.sql" in h for h in result["unresolved_hints"])
    # 变量里的 SQL 被解析出来
    assert any("变量" in (s["source_hint"] or "") for s in result["sqls"])


def test_example_python_script():
    result = parse_script_file(PYTHON_EXAMPLE)
    assert result["kind"] == "python"
    assert result["sql_count"] == 5
    assert pairs(result) == {
        ("ods.ods_卷烟产量流水", "cdw.dws_产销存汇总"),
        ("ods.ods_卷烟销量流水", "cdw.dws_产销存汇总"),
        ("ods.ods_卷烟产量流水", "cdw.dws_产量日汇总"),
        ("cdw.dws_产销存汇总", "ads.ads_经营指标明细"),
        ("dim.dim_plant", "ads.ads_经营指标明细"),
    }
    assert "ods.ods_税利上缴流水" in result["input_tables"]      # sqlalchemy.text 里的 SQL
    # f-string 占位符提示
    assert any("f-string 占位符 {DT}" in h for h in result["unresolved_hints"])
    # 全部字段级血缘都能解析（示例脚本里没有 SELECT * / 多表同名歧义）
    assert result["column_lineage_count"] > 0
    assert all(c["resolved"] for c in result["column_lineage"])


# --------------------------------------------------------------------------- #
# 6) 工作流集成（_parse_tasks 按脚本类型分发）
# --------------------------------------------------------------------------- #
def _task(name: str, ttype: str, params: dict, code: int = 1) -> dict:
    return {"task": {"name": name, "code": code, "taskType": ttype, "taskParams": params},
            "from_workflow": "wf_test"}


SH_SHELL = (
    "#!/bin/bash\n"
    'echo "start"\n'
    'hive -e "INSERT OVERWRITE TABLE cdw.dws_a PARTITION (dt=\'1\') SELECT x AS x FROM ods.p;"\n'
    'SQL_B="INSERT OVERWRITE TABLE cdw.dws_b PARTITION (dt=\'1\') SELECT y AS y FROM ods.q"\n'
    'beeline -e "$SQL_B"\n'
    "hive <<EOF\n"
    "INSERT OVERWRITE TABLE cdw.dws_c PARTITION (dt='1') SELECT z AS z FROM ods.r;\n"
    "EOF\n"
)

PY_PYSPARK = (
    "from pyspark.sql import SparkSession\n"
    "spark = SparkSession.builder.getOrCreate()\n"
    'spark.sql("""INSERT OVERWRITE TABLE ads.x PARTITION (dt=\'1\')\n'
    'SELECT s AS s FROM cdw.dws_a""")\n'
)


def test_workflow_shell_task_extracts_embedded_sql():
    tasks = _parse_tasks([_task("t_shell", "SHELL",
                               {"localParams": [], "resourceList": [], "rawScript": SH_SHELL})],
                         "hive", ["SQL", "SHELL", "PYTHON"])
    t = tasks[0]
    assert t["type"] == "SHELL"
    assert t["script_kind"] == "shell"
    assert t["sql_count"] == 3
    assert t["statement_count"] == 3
    assert t["parsed"] is True
    assert t["output_tables"] == ["cdw.dws_a", "cdw.dws_b", "cdw.dws_c"]
    assert t["input_tables"] == ["ods.p", "ods.q", "ods.r"]
    assert t["column_count"] == 3


def test_workflow_python_task_extracts_embedded_sql():
    tasks = _parse_tasks([_task("t_py", "PYTHON",
                               {"localParams": [], "resourceList": [], "rawScript": PY_PYSPARK})],
                         "hive", ["SQL", "SHELL", "PYTHON"])
    t = tasks[0]
    assert t["script_kind"] == "python"
    assert t["sql_count"] == 1
    assert t["output_tables"] == ["ads.x"]
    assert t["input_tables"] == ["cdw.dws_a"]


def test_workflow_shell_task_records_unresolved_hints():
    shell = '#!/bin/bash\nbeeline -f etl/missing.sql\n'
    tasks = _parse_tasks([_task("t_shell", "SHELL",
                               {"localParams": [], "resourceList": [], "rawScript": shell})],
                         "hive", ["SHELL"])
    t = tasks[0]
    assert t["sql_count"] == 0
    assert t["parsed"] is False
    assert any("外部文件" in h for h in t["unresolved_hints"])


def test_workflow_sql_task_still_direct_parsed():
    sql = "INSERT OVERWRITE TABLE ods.a PARTITION (dt='1') SELECT x AS x FROM src.b"
    tasks = _parse_tasks([_task("t_sql", "SQL",
                               {"type": "HIVE", "sql": sql, "localParams": [], "resourceList": []})],
                         "hive", ["SQL"])
    t = tasks[0]
    assert t["script_kind"] == "sql"
    assert t["sql_count"] == 1
    assert t["statement_count"] == 1
    assert t["output_tables"] == ["ods.a"]


def test_workflow_shell_task_with_raw_sql_fallback():
    """SHELL 任务里塞的是纯 SQL（海豚 rawScript 常见形态）：走兜底直解。"""
    sql = "INSERT OVERWRITE TABLE ods.a PARTITION (dt='1') SELECT x AS x FROM src.b;"
    tasks = _parse_tasks([_task("t_shell", "SHELL",
                               {"localParams": [], "resourceList": [], "rawScript": sql})],
                         "hive", ["SHELL"])
    t = tasks[0]
    assert t["statement_count"] == 1
    assert t["output_tables"] == ["ods.a"]
    assert t["script_kind"] == "sql"       # 内容判定：这就是纯 SQL


def test_workflow_mixed_tasks_script_kind_summary():
    tasks = _parse_tasks([
        _task("t_sql", "SQL", {"type": "HIVE",
                               "sql": "INSERT OVERWRITE TABLE cdw.d PARTITION (dt='1') SELECT x AS x FROM ods.p",
                               "localParams": [], "resourceList": []}, 1),
        _task("t_shell", "SHELL", {"localParams": [], "resourceList": [], "rawScript": SH_SHELL}, 2),
        _task("t_py", "PYTHON", {"localParams": [], "resourceList": [], "rawScript": PY_PYSPARK}, 3),
    ], "hive", ["SQL", "SHELL", "PYTHON"])
    kinds = [t["script_kind"] for t in tasks]
    assert kinds == ["sql", "shell", "python"]
    assert sum(t["sql_count"] for t in tasks) == 5


def test_workflow_skipped_task_type():
    tasks = _parse_tasks([_task("t_datax", "DATAX", {"localParams": []})], "hive", ["SQL"])
    assert tasks[0]["parsed"] is False
    assert any("不在本次解析范围" in e for e in tasks[0]["errors"])


def test_workflow_task_column_lineage_carries_task_and_hint():
    tasks = _parse_tasks([_task("t_shell", "SHELL",
                               {"localParams": [], "resourceList": [], "rawScript": SH_SHELL})],
                         "hive", ["SHELL"])
    column = tasks[0]["_columns"][0]
    assert column["task"] == "t_shell"
    assert column["task_type"] == "SHELL"
    assert column["source_hint"]


# --------------------------------------------------------------------------- #
# 7) CLI：python -m lineage.cli parse-script
# --------------------------------------------------------------------------- #
def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "lineage.cli", *args],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True,
    )


def test_cli_parse_script_text():
    proc = _run_cli("parse-script", str(SHELL_EXAMPLE))
    assert proc.returncode == 0, proc.stderr
    assert "脚本内嵌 SQL 血缘报告" in proc.stdout
    assert "提取到内嵌 SQL: 3 条" in proc.stdout
    assert "src.erp_生产工单明细  -->  ods.ods_卷烟产量流水" in proc.stdout


def test_cli_parse_script_json():
    proc = _run_cli("parse-script", str(PYTHON_EXAMPLE), "--output", "json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["kind"] == "python"
    assert payload["sql_count"] == 5
    assert "cdw.dws_产销存汇总" in payload["output_tables"]


def test_cli_parse_script_force_kind():
    proc = _run_cli("parse-script", str(SHELL_EXAMPLE), "--kind", "sql", "--output", "json")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["kind"] == "sql"


def test_cli_parse_script_task_type():
    proc = _run_cli("parse-script", str(PYTHON_EXAMPLE), "--task-type", "PYTHON", "--output", "json")
    payload = json.loads(proc.stdout)
    assert payload["task_type"] == "PYTHON"


def test_cli_parse_script_multiple_files():
    proc = _run_cli("parse-script", str(SHELL_EXAMPLE), str(PYTHON_EXAMPLE), "--output", "json")
    payload = json.loads(proc.stdout)
    assert payload["file_count"] == 2
    assert len(payload["scripts"]) == 2


def test_cli_parse_script_missing_file():
    proc = _run_cli("parse-script", "examples/scripts/不存在.sh")
    assert proc.returncode == 2
    assert "文件不存在" in proc.stderr


def test_cli_subcommand_registered():
    from lineage import cli

    assert "parse-script" in cli.SUBCOMMANDS
    assert "parse-script" in cli._SUBCOMMAND_RUNNERS
    assert cli._SUBCOMMAND_RUNNERS["parse-script"] is cli.cmd_parse_script
