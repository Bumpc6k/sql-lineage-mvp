"""P7「生成引擎」测试：L1 单表加工 SQL / L2 分层链路 / L3 落地海豚 / L4 反向校验。

覆盖点
------
* **L1**：模板拼接（分区 / 聚合 / GROUP BY）、知识库口径复用（中文业务名 -> 别名.字段）、
  字段缺失时的降级与 warnings、explain 逐列有依据、生成 SQL 必须能被血缘引擎解析、
  目标表列覆盖率告警、all_columns 补齐、显式 joins 覆盖；
* **L2**：复用模式（需求关键词 → 目标表 → 逐层链路）、指定目标表、段数上限、新建表模式、
  每段 SQL 可解析、pipeline 节点/边自洽；
* **L3**：工作流 JSON 结构（taskDefinition / taskRelation / locations 链式）、默认不创建、
  mock 海豚真实创建 + 回读、同名工作流先删后建（可重复执行）；
* **L4**：校验通过 / 跨层直连 / 环路 / 断链孤岛 / 口径缺失 / 与血缘图对比 / 体检报告落盘与渲染；
* **HTTP**：``/generate/*`` 处理函数与路由注册、``/health`` 端点清单、报告目录可注入；
* **CLI**：``generate sql|pipeline|apply|validate`` 的 ``--json`` 输出与退出码；
* **LLM**：未配置 key 时纯模板可用；注入假客户端时只做评审与候选筛选（不编表名）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lineage.generate import (
    GenerateLLM,
    GraphIndex,
    apply_pipeline,
    build_workflow_json,
    generate_pipeline,
    generate_sql,
    layer_of,
    parse_check,
    step_ok,
    validate_generation,
)
from lineage.generate.llm import GenerateLLM as _GenerateLLM
from lineage.knowledge import build_knowledge_base
from lineage_core.parser import SqlLineageParser

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = PROJECT_ROOT / "examples" / "warehouse"
KNOWLEDGE_DEMO = PROJECT_ROOT / "examples" / "knowledge_demo"
GRAPH = PROJECT_ROOT / "warehouse_graph.json"
CLI = [sys.executable, "-m", "lineage.cli"]


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def kb_db(tmp_path_factory: pytest.TempPathFactory) -> str:
    """临时知识库（从真实示例目录重建，不依赖仓库里那份 data/knowledge.db）。"""
    db = tmp_path_factory.mktemp("kb") / "knowledge.db"
    report = build_knowledge_base([str(WAREHOUSE), str(KNOWLEDGE_DEMO)], db_path=str(db))
    assert report.counts["kb_metrics"] > 20
    return str(db)


@pytest.fixture(scope="module")
def pipeline(kb_db: str) -> dict:
    """L2 的分层链路结果（多个用例复用）。"""
    result = generate_pipeline({"requirement": "生成产销存月报", "target_layer": "ads",
                                "max_stages": 4, "db": kb_db, "graph": str(GRAPH)})
    assert result["success"], result.get("error")
    return result


def _run(*args: str, expect: int = 0) -> subprocess.CompletedProcess:
    proc = subprocess.run(CLI + list(args), cwd=str(PROJECT_ROOT), capture_output=True, text=True)
    assert proc.returncode == expect, f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc


# --------------------------------------------------------------------------- #
# L1：单表加工 SQL 生成
# --------------------------------------------------------------------------- #
def test_l1_basic_sql_and_explain(kb_db: str) -> None:
    result = generate_sql({
        "source_tables": ["ods.ods_卷烟产量流水"],
        "target_table": "cdw.dwd_卷烟产量明细",
        "metrics": ["产量"],
        "group_by": ["plant_code"],
        "partition_field": "dt",
        "dialect": "hive",
        "db": kb_db,
        "graph": str(GRAPH),
    })
    assert result["success"], result.get("error")
    sql = result["sql"]
    assert "INSERT OVERWRITE TABLE cdw.dwd_卷烟产量明细 PARTITION (dt = '${bizdate}')" in sql
    assert "SUM(t1.output_qty) AS output_qty" in sql
    assert "t1.plant_code AS plant_code" in sql
    assert "GROUP BY t1.plant_code" in sql
    assert "WHERE t1.dt = '${bizdate}'" in sql
    # 逐列都有依据
    for column in result["columns"]:
        assert column["evidence"], f"列 {column['column']} 没有 explain 依据"
        assert column["source"] in ("kb_metric", "graph_expression", "source_field")
    # 关键依据：口径来源脚本 + 字段中文名
    joined = "\n".join(result["explain"])
    assert "口径依据" in joined and "ods_卷烟产量流水.sql" in joined
    assert "生产厂编码" in joined
    assert result["ast_check"]["parse_ok"] is True
    assert result["ast_check"]["output_tables"] == ["cdw.dwd_卷烟产量明细"]


def test_l1_generated_sql_reparses_with_lineage(kb_db: str) -> None:
    result = generate_sql({"source_tables": ["ods.ods_卷烟产量流水"],
                           "target_table": "cdw.dwd_卷烟产量明细",
                           "metrics": ["产量"], "group_by": ["plant_code"],
                           "db": kb_db, "graph": str(GRAPH)})
    stmts = SqlLineageParser(dialect="hive").parse_sql(result["sql"], source="<test>")
    assert stmts and stmts[0]["output_table_names"] == ["cdw.dwd_卷烟产量明细"]
    targets = {c["target_column"] for c in stmts[0]["column_lineage"]}
    assert {"output_qty", "plant_code"} <= targets


def test_l1_reuses_kb_metric_formula(kb_db: str) -> None:
    """码段口径「产量 = 打码量 + 跳码量 - 重码量」应被原样复用（中文名 -> 真实列名）。"""
    result = generate_sql({"source_tables": ["ods.ods_卷烟码段流水"],
                           "target_table": "cdw.dwd_卷烟产量码段明细",
                           "metrics": ["产量"],
                           "db": kb_db, "graph": str(GRAPH)})
    assert result["success"], result.get("error")
    sql = result["sql"]
    assert "SUM(t1.dama_qty) + SUM(t1.tiaoma_qty) - SUM(t1.chongma_qty)" in sql
    kinds = {c["source"] for c in result["columns"]}
    assert "kb_metric" in kinds, "应从知识库口径生成"
    assert result["ast_check"]["parse_ok"] is True


def test_l1_unknown_target_and_source_warn(kb_db: str) -> None:
    """目标表 / 源表都不在知识库：降级为「源字段直取 + warnings」，绝不留空话。"""
    result = generate_sql({"source_tables": ["ods.ods_卷烟产量流水"],
                           "target_table": "cdw.dwd_卷烟产量明细_新表",
                           "metrics": ["产量"], "group_by": ["plant_code"],
                           "db": kb_db, "graph": str(GRAPH)})
    assert result["success"], result.get("error")
    assert result["target_table_known"] is False
    warnings = "\n".join(result["warnings"])
    assert "未在知识库登记" in warnings
    assert result["ast_check"]["parse_ok"] is True


def test_l1_unknown_metric_is_skipped_with_warning(kb_db: str) -> None:
    result = generate_sql({"source_tables": ["ods.ods_卷烟产量流水"],
                           "target_table": "cdw.dwd_卷烟产量明细",
                           "metrics": ["这个指标不存在"],
                           "db": kb_db, "graph": str(GRAPH)})
    assert result["success"] is False
    assert "没有任何列可以被生成" in result["error"]
    assert any("无法" in w or "找不到" in w or "跳过" in w for w in result["warnings"])


def test_l1_unknown_group_column_is_skipped(kb_db: str) -> None:
    result = generate_sql({"source_tables": ["ods.ods_卷烟产量流水"],
                           "target_table": "cdw.dwd_卷烟产量明细",
                           "metrics": ["产量"], "group_by": ["no_such_column"],
                           "db": kb_db, "graph": str(GRAPH)})
    assert result["success"], result.get("error")
    assert all(c["column"] != "no_such_column" for c in result["columns"])
    assert any("no_such_column" in w for w in result["warnings"])


def test_l1_coverage_warning_and_all_columns(kb_db: str) -> None:
    payload = {"source_tables": ["ods.ods_卷烟产量流水"],
               "target_table": "cdw.dwd_卷烟产量明细",
               "metrics": ["产量"], "db": kb_db, "graph": str(GRAPH)}
    partial = generate_sql(payload)
    assert any("只覆盖" in w for w in partial["warnings"])
    full = generate_sql({**payload, "all_columns": True})
    assert len(full["columns"]) > len(partial["columns"])
    assert any("all_columns=true" in w for w in full["warnings"])
    assert full["ast_check"]["parse_ok"] is True


def test_l1_partition_field_unconfirmed_warns(kb_db: str) -> None:
    result = generate_sql({"source_tables": ["ods.ods_卷烟产量流水"],
                           "target_table": "cdw.dwd_卷烟产量明细",
                           "metrics": ["产量"], "partition_field": "biz_dt",
                           "db": kb_db, "graph": str(GRAPH)})
    # 存量脚本里的分区键是 dt：指定 biz_dt 时必须给出提示（不一致 / 未印证 都算）
    assert any("biz_dt" in w and ("印证" in w or "不一致" in w) for w in result["warnings"])


def test_l1_explicit_join_override(kb_db: str) -> None:
    result = generate_sql({"source_tables": ["ods.ods_卷烟产量流水", "dim.dim_plant"],
                           "target_table": "cdw.dwd_卷烟产量明细",
                           "metrics": ["产量"],
                           "joins": [{"table": "dim.dim_plant",
                                      "on": "t1.plant_code = t2.plant_code", "kind": "LEFT"}],
                           "db": kb_db, "graph": str(GRAPH)})
    assert "LEFT JOIN dim.dim_plant t2 ON t1.plant_code = t2.plant_code" in result["sql"]
    assert result["joins"][0]["confirmed"] is True


def test_l1_missing_kb_returns_hint(tmp_path: Path) -> None:
    result = generate_sql({"source_tables": ["ods.x"], "target_table": "cdw.y",
                           "metrics": ["产量"], "db": str(tmp_path / "none.db")})
    assert result["success"] is False
    assert "知识库文件不存在" in result["error"]


def test_l1_requires_inputs() -> None:
    assert generate_sql({})["success"] is False
    assert generate_sql({"target_table": "t"})["success"] is False
    both = generate_sql({"source_tables": ["ods.x"], "target_table": "cdw.y"})
    assert both["success"] is False and "metrics" in both["error"]


def test_l1_no_llm_configured(kb_db: str) -> None:
    result = generate_sql({"source_tables": ["ods.ods_卷烟产量流水"],
                           "target_table": "cdw.dwd_卷烟产量明细", "metrics": ["产量"],
                           "db": kb_db, "graph": str(GRAPH), "use_llm": "off"})
    assert result["llm"]["used"] is False
    assert "LLM_API_KEY" in result["llm"]["hint"]


# --------------------------------------------------------------------------- #
# L2：分层链路生成
# --------------------------------------------------------------------------- #
def test_l2_pipeline_layers_and_sql(pipeline: dict) -> None:
    stages = pipeline["stages"]
    assert [s["layer"] for s in stages] == ["ads", "dws", "dwd", "ods"]
    assert pipeline["target_table"] == "ads.ads_产销存月报"
    assert pipeline["table_mode"] == "reuse"
    for stage in stages:
        assert stage["sql"].startswith("--")
        assert f"INSERT OVERWRITE TABLE {stage['target_table']}" in stage["sql"]
        assert stage["ast_check"]["parse_ok"] is True
        assert stage["columns"], "每段都必须有列映射"
        for column in stage["columns"]:
            assert column["expression"].strip()
    # 段间依赖链
    assert stages[0]["depends_on"] == ["cdw.dws_产销存汇总"]
    assert stages[1]["depends_on"] == ["cdw.dwd_卷烟销量明细"]
    assert stages[2]["depends_on"] == ["ods.ods_卷烟销量流水"]
    assert stages[3]["depends_on"] == []


def test_l2_pipeline_graph_consistency(pipeline: dict) -> None:
    nodes = {n["id"] for n in pipeline["pipeline"]["nodes"]}
    for stage in pipeline["stages"]:
        assert stage["target_table"] in nodes
        for dep in stage["depends_on"]:
            assert dep in nodes
            assert any(e["source"] == dep and e["target"] == stage["target_table"]
                       and e["kind"] == "chain" for e in pipeline["pipeline"]["edges"])
    for ext in [e for s in pipeline["stages"] for e in s["external_inputs"]]:
        assert ext in nodes
        assert any(e["source"] == ext and e["kind"] == "external" for e in pipeline["pipeline"]["edges"])


def test_l2_max_stages_limits(pipeline: dict) -> None:
    assert len(pipeline["stages"]) <= 4
    assert any("max_stages" in w for w in pipeline["warnings"])


def test_l2_target_table_override(kb_db: str) -> None:
    result = generate_pipeline({"target_table": "cdw.dws_产量汇总", "max_stages": 2,
                                "db": kb_db, "graph": str(GRAPH)})
    assert result["success"], result.get("error")
    assert result["target_table"] == "cdw.dws_产量汇总"
    assert len(result["stages"]) == 2
    assert result["stages"][0]["layer"] == "dws"


def test_l2_new_table_mode(kb_db: str) -> None:
    result = generate_pipeline({"requirement": "生成智能制造月度分析", "target_layer": "ads",
                                "max_stages": 2, "db": kb_db, "graph": str(GRAPH)})
    assert result["success"], result.get("error")
    assert result["table_mode"] == "new"
    assert result["stages"][0]["target_table"].startswith("ads.ads_")
    assert any("新建表" in w or "推导" in w for w in result["warnings"])


def test_l2_requires_requirement(kb_db: str) -> None:
    result = generate_pipeline({"db": kb_db, "graph": str(GRAPH)})
    assert result["success"] is False and "requirement" in result["error"]


def test_l2_partition_placeholder(kb_db: str) -> None:
    result = generate_pipeline({"requirement": "生成产销存月报", "db": kb_db, "graph": str(GRAPH)})
    assert result["date_placeholder"] == "${bizdate}"
    assert "${bizdate}" in result["stages"][0]["sql"]


# --------------------------------------------------------------------------- #
# L3：落地 DolphinScheduler
# --------------------------------------------------------------------------- #
def test_l3_workflow_json_structure(pipeline: dict) -> None:
    built = build_workflow_json(pipeline["stages"], project_code=123, workflow_name="wf_test",
                                env="hive", datasource_id=1)
    assert built["ok"]
    tasks = built["tasks"]
    assert len(tasks) == len(pipeline["stages"])
    # 上游先执行
    assert tasks[0]["name"] == built["execution_order"][0]
    for task in tasks:
        assert task["taskType"] == "SQL"
        assert task["taskParams"]["sqlType"] == "1"
        assert task["taskParams"]["type"] == "HIVE"
        assert task["taskParams"]["sql"].strip()
    relations = built["workflow"]["taskRelationJsonObject"]
    chain = [r for r in relations if r["preTaskCode"] != 0]
    assert len(chain) == len(tasks) - 1
    # locations 链式（x 递增）
    xs = [loc["x"] for loc in built["locations"]]
    assert xs == sorted(xs) and len(set(xs)) == len(xs)
    # JSON 文本字段可直接塞给海豚接口
    assert json.loads(built["workflow"]["taskDefinitionJson"])[0]["code"] == tasks[0]["code"]


def test_l3_dry_run_is_default(pipeline: dict) -> None:
    result = apply_pipeline({"pipeline": pipeline, "workflow_name": "wf_gen_测试",
                             "project_code": 123})
    assert result["success"] is True
    assert result["created"] is False
    assert "workflow_json" in result
    assert any("安全默认" in n for n in result["notes"])
    codes = [t["code"] for t in json.loads(result["workflow_json"]["taskDefinitionJson"])]
    assert codes == result["task_codes"]


def test_l3_requires_stages() -> None:
    result = apply_pipeline({})
    assert result["success"] is False and "stages" in result["error"]


def test_l3_create_against_mock_ds(pipeline: dict) -> None:
    from tests.ds_mock import MOCK_PROJECT_CODE, MockDsServer

    with MockDsServer() as srv:
        result = apply_pipeline({"pipeline": pipeline, "workflow_name": "wf_gen_mock",
                                 "project_code": MOCK_PROJECT_CODE, "create_workflow": True,
                                 "base_url": srv.url, "datasource_id": 1})
    assert result["success"], result.get("error")
    assert result["created"] is True
    assert result["workflow_code"]
    assert result["readback"]["task_count"] == len(pipeline["stages"])
    assert result["readback"]["relation_count"] == len(pipeline["stages"])
    # 依赖链：第 2 个任务的上游是第 1 个任务的 code
    readback = result["readback"]["tasks"]
    assert readback[1]["pre"] == [readback[0]["code"]] if "code" in readback[1] else True
    assert len(srv.find("/process-definition", "POST")) == 1


def test_l3_same_name_is_recreated(pipeline: dict) -> None:
    from tests.ds_mock import MOCK_PROJECT_CODE, MockDsServer

    with MockDsServer() as srv:
        first = apply_pipeline({"pipeline": pipeline, "workflow_name": "wf_gen_same",
                                "project_code": MOCK_PROJECT_CODE, "create_workflow": True,
                                "base_url": srv.url, "datasource_id": 1})
        second = apply_pipeline({"pipeline": pipeline, "workflow_name": "wf_gen_same",
                                 "project_code": MOCK_PROJECT_CODE, "create_workflow": True,
                                 "base_url": srv.url, "datasource_id": 1})
    assert first["created"] and second["created"]
    assert first["workflow_code"] != second["workflow_code"]
    assert any("已删除同名旧工作流" in n for n in second["notes"])


def test_l3_unreachable_ds_degrades(pipeline: dict) -> None:
    result = apply_pipeline({"pipeline": pipeline, "workflow_name": "wf_gen_fail",
                             "create_workflow": True,
                             "base_url": "http://127.0.0.1:1/dolphinscheduler"})
    assert result["success"] is False
    assert "连不上" in result["error"] or "DsConnectionError" in result["error"]
    assert "create_workflow=false" in (result.get("hint") or "")


# --------------------------------------------------------------------------- #
# L4：反向校验
# --------------------------------------------------------------------------- #
def test_l4_validate_passed_with_report(pipeline: dict, tmp_path: Path, kb_db: str) -> None:
    result = validate_generation({"pipeline": pipeline, "db": kb_db, "graph": str(GRAPH),
                                  "reports_dir": str(tmp_path), "name": "wf_gen_产销存月报"})
    assert result["success"] and result["passed"]
    assert result["issue_count"]["error"] == 0
    assert result["layer_rules"] == []
    assert result["merged"]["edge_count"] >= 4
    assert result["graph_diff"]["new_count"] == 0, "生成链路应与 warehouse_graph.json 完全一致"
    assert sum(m["matched"] for m in result["metric_check"]) > 0
    assert result["report_id"] and result["url"]
    assert (tmp_path / f"{result['report_id']}.html").is_file()
    assert result["report_path"].endswith(".html")


def test_l4_layer_violation_detected(tmp_path: Path, kb_db: str) -> None:
    sql = ("INSERT OVERWRITE TABLE ads.ads_跨层直连 PARTITION (dt = '2026-01-01')\n"
           "SELECT p.plant_code AS plant_code, p.output_qty AS output_qty\n"
           "FROM ods.ods_卷烟产量流水 p WHERE p.dt = '2026-01-01';")
    result = validate_generation({"sql_list": [sql], "db": kb_db, "graph": str(GRAPH),
                                 "reports_dir": str(tmp_path)})
    assert result["passed"] is False
    assert result["layer_rules"] and result["layer_rules"][0]["type"] == "layer_rule"
    assert "跨层直连" in result["layer_rules"][0]["message"]
    html = (tmp_path / f"{result['report_id']}.html").read_text(encoding="utf-8")
    assert "分层规则违规" in html


def test_l4_cycle_detected(tmp_path: Path, kb_db: str) -> None:
    sqls = [
        "INSERT OVERWRITE TABLE cdw.dwd_a PARTITION (dt='2026-01-01') "
        "SELECT x.id AS id FROM cdw.dwd_b x WHERE x.dt='2026-01-01';",
        "INSERT OVERWRITE TABLE cdw.dwd_b PARTITION (dt='2026-01-01') "
        "SELECT y.id AS id FROM cdw.dwd_a y WHERE y.dt='2026-01-01';",
    ]
    result = validate_generation({"sql_list": sqls, "db": kb_db, "graph": str(GRAPH),
                                 "reports_dir": str(tmp_path), "make_report": False})
    assert result["passed"] is False
    assert any(i["type"] == "cycle" for i in result["issues"])


def test_l4_dangling_and_missing_metric(tmp_path: Path, kb_db: str) -> None:
    sql = ("INSERT OVERWRITE TABLE cdw.dwd_卷烟产量明细 PARTITION (dt='2026-01-01')\n"
           "SELECT p.plant_code AS plant_code FROM ods.ods_卷烟产量流水 p "
           "WHERE p.dt='2026-01-01';")
    result = validate_generation({"sql_list": [sql], "db": kb_db, "graph": str(GRAPH),
                                 "reports_dir": str(tmp_path), "make_report": False})
    types = {i["type"] for i in result["issues"]}
    assert "dangling" in types            # 产出表无人消费
    assert "kb_metric" in types           # 缺少知识库已登记口径列
    assert any("output_qty" in i["message"] for i in result["issues"] if i["type"] == "kb_metric")


def test_l4_orphan_input_marked_external(tmp_path: Path, kb_db: str) -> None:
    result = validate_generation({
        "stages": [{"target_table": "cdw.dwd_x", "layer": "dwd",
                   "sql": "INSERT OVERWRITE TABLE cdw.dwd_x PARTITION (dt='2026-01-01') "
                          "SELECT y.id AS id FROM cdw.dws_y y WHERE y.dt='2026-01-01';",
                   "external_inputs": ["cdw.dws_y"]}],
        "db": kb_db, "graph": str(GRAPH), "reports_dir": str(tmp_path), "make_report": False,
    })
    assert any(i["type"] == "external" for i in result["issues"])


def test_l4_parse_failure_is_error(tmp_path: Path) -> None:
    result = validate_generation({"sql_list": ["SELEC broken FROM"], "graph": str(GRAPH),
                                  "reports_dir": str(tmp_path), "make_report": False})
    assert result["passed"] is False
    assert any(i["type"] == "parse" and i["level"] == "error" for i in result["issues"])


def test_l4_requires_sql() -> None:
    result = validate_generation({})
    assert result["success"] is False and "SQL" in result["error"]


def test_l4_accepts_sql_file(tmp_path: Path, kb_db: str) -> None:
    path = tmp_path / "one.sql"
    path.write_text("INSERT OVERWRITE TABLE cdw.dws_产量汇总 PARTITION (dt='2026-01-01')\n"
                    "SELECT p.plant_code AS plant_code, SUM(p.output_qty) AS total_output_qty\n"
                    "FROM cdw.dwd_卷烟产量明细 p WHERE p.dt='2026-01-01' GROUP BY p.plant_code;",
                    encoding="utf-8")
    result = validate_generation({"sql_file": str(path), "db": kb_db, "graph": str(GRAPH),
                                  "reports_dir": str(tmp_path), "make_report": False})
    assert result["success"] and result["stage_count"] == 1
    assert result["merged"]["output_tables"] == ["cdw.dws_产量汇总"]


# --------------------------------------------------------------------------- #
# HTTP 端点
# --------------------------------------------------------------------------- #
def test_http_generate_handlers(kb_db: str, tmp_path: Path) -> None:
    from lineage.serve import api_server

    for route in ("/generate/sql", "/generate/pipeline", "/generate/apply", "/generate/validate"):
        assert route in api_server.ROUTES, f"{route} 未注册"
    sql = api_server.handle_generate_sql({
        "source_tables": ["ods.ods_卷烟产量流水"], "target_table": "cdw.dwd_卷烟产量明细",
        "metrics": ["产量"], "group_by": ["plant_code"], "db": kb_db, "graph": str(GRAPH)})
    assert sql["success"] and "INSERT OVERWRITE" in sql["sql"]
    pipe = api_server.handle_generate_pipeline({"requirement": "生成产销存月报", "db": kb_db,
                                                "graph": str(GRAPH)})
    assert pipe["success"] and len(pipe["stages"]) >= 2
    applied = api_server.handle_generate_apply({"pipeline": pipe, "workflow_name": "wf_http"})
    assert applied["success"] and applied["created"] is False
    validated = api_server.handle_generate_validate({"pipeline": pipe, "db": kb_db,
                                                     "graph": str(GRAPH),
                                                     "reports_dir": str(tmp_path)})
    assert validated["success"] and validated["url"].endswith(f"/report/{validated['report_id']}")


def test_http_generate_error_is_business_error() -> None:
    from lineage.serve import api_server

    bad = api_server.handle_generate_sql({"target_table": "cdw.x"})
    assert bad["success"] is False and "error" in bad


def test_health_lists_generate_endpoints() -> None:
    from lineage.serve import api_server

    listed = sorted(api_server.ROUTES)
    assert "/generate/sql" in listed and "/generate/validate" in listed


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_generate_sql_json(kb_db: str) -> None:
    proc = _run("generate", "sql", "--source", "ods.ods_卷烟产量流水",
                "--target", "cdw.dwd_卷烟产量明细", "--metric", "产量",
                "--group-by", "plant_code", "--partition", "dt",
                "--db", kb_db, "--graph", str(GRAPH), "--json")
    payload = json.loads(proc.stdout)
    assert payload["success"] and payload["sql"].startswith("--")
    assert payload["ast_check"]["parse_ok"] is True


def test_cli_generate_pipeline_and_validate(kb_db: str, tmp_path: Path) -> None:
    out = tmp_path / "pipeline.json"
    _run("generate", "pipeline", "--requirement", "生成产销存月报", "--max-stages", "4",
         "--db", kb_db, "--graph", str(GRAPH), "--json", "--save", str(out))
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["stages"]) == 4
    report_dir = tmp_path / "reports"
    proc = _run("generate", "validate", "--pipeline-file", str(out), "--db", kb_db,
                "--graph", str(GRAPH), "--json", "--reports-dir", str(report_dir))
    result = json.loads(proc.stdout)
    assert result["success"] and result["passed"]
    assert (report_dir / f"{result['report_id']}.html").is_file()


def test_cli_generate_apply_dry_run(kb_db: str, tmp_path: Path) -> None:
    out = tmp_path / "pipeline.json"
    _run("generate", "pipeline", "-r", "生成产销存月报", "--db", kb_db, "--graph", str(GRAPH),
         "--json", "--save", str(out))
    proc = _run("generate", "apply", "--pipeline-file", str(out), "--workflow-name", "wf_cli_test",
                "--project-code", "123", "--json")
    result = json.loads(proc.stdout)
    assert result["success"] and result["created"] is False
    assert json.loads(result["workflow_json"]["taskRelationJson"])


def test_cli_generate_validate_failure_exit_code(kb_db: str, tmp_path: Path) -> None:
    sql_file = tmp_path / "bad.sql"
    sql_file.write_text("INSERT OVERWRITE TABLE ads.ads_直连测试 PARTITION (dt='2026-01-01') "
                        "SELECT p.plant_code AS plant_code FROM ods.ods_卷烟产量流水 p;",
                        encoding="utf-8")
    proc = _run("generate", "validate", "--sql-file", str(sql_file), "--db", kb_db,
                "--graph", str(GRAPH), "--no-report", "--json", expect=1)
    result = json.loads(proc.stdout)
    assert result["passed"] is False and result["layer_rules"]


def test_cli_generate_unknown_subcommand() -> None:
    proc = _run("generate", "nope", expect=2)
    assert "未知的 generate 子命令" in proc.stderr


def test_cli_generate_help_lists_four_subcommands() -> None:
    proc = _run("generate")
    for sub in ("sql", "pipeline", "apply", "validate"):
        assert sub in proc.stdout


# --------------------------------------------------------------------------- #
# LLM 可插拔（模板为主）
# --------------------------------------------------------------------------- #
class _FakeClient:
    """假的 OpenAI 兼容客户端：只用于验证「LLM 不改写 SQL、不编表名」。"""

    class _Settings:
        model = "fake-model"
        base_url = "http://fake.local/v1"

    def __init__(self, reply: str) -> None:
        self.settings = self._Settings()
        self.reply = reply
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    def complete(self, system: str, user: str):
        self.calls += 1
        return self.reply


def test_llm_review_returns_notes() -> None:
    llm = GenerateLLM(client=_FakeClient("- 分区条件已覆盖\n- 建议确认关联键"))
    notes = llm.review_sql("SELECT 1", "背景")
    assert notes == ["分区条件已覆盖", "建议确认关联键"]
    assert llm.available is True


def test_llm_pick_tables_drops_fabricated_names() -> None:
    client = _FakeClient('{"tables": ["ads.ads_产销存月报", "ads.ads_瞎编的表"], "reason": "相关"}')
    llm = GenerateLLM(client=client)
    picked = llm.pick_tables("产销存月报", ["ads.ads_产销存月报", "ads.ads_税利分析"])
    assert picked["tables"] == ["ads.ads_产销存月报"]


def test_llm_disabled_by_flag() -> None:
    llm = GenerateLLM(enabled="off", client=_FakeClient("- x"))
    assert llm.available is False


def test_llm_generate_sql_reports_review(kb_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import lineage.generate.sql_builder as sql_builder

    fake = _FakeClient("- 生成结果自检通过")
    monkeypatch.setattr(sql_builder, "GenerateLLM", lambda *a, **k: GenerateLLM(client=fake))
    result = generate_sql({"source_tables": ["ods.ods_卷烟产量流水"],
                           "target_table": "cdw.dwd_卷烟产量明细", "metrics": ["产量"],
                           "db": kb_db, "graph": str(GRAPH), "use_llm": "auto"})
    assert result["llm"]["used"] is True
    assert result["llm"]["notes"] == ["生成结果自检通过"]
    assert "INSERT OVERWRITE" in result["sql"], "LLM 不得改写 SQL"


# --------------------------------------------------------------------------- #
# 底座工具
# --------------------------------------------------------------------------- #
def test_layer_rules_helper() -> None:
    assert step_ok("ods", "dwd") and step_ok("dws", "ads") and step_ok("dim", "ads")
    assert step_ok("dwd", "dwd")
    assert not step_ok("ods", "ads") and not step_ok("src", "dwd")
    assert layer_of("cdw.dwd_卷烟产量明细") == "dwd"
    assert layer_of("ads.ads_产销存月报") == "ads"


def test_graph_index_reads_real_expressions() -> None:
    graph = GraphIndex.load(str(GRAPH))
    assert graph.available
    exprs = graph.expressions_into("ads.ads_产销存月报", ["cdw.dws_产销存汇总"])
    assert any(e["target_column"] == "sale_output_ratio" for e in exprs)
    assert graph.partition_field("ads.ads_产销存月报") == "dt"


def test_parse_check_reports_failure() -> None:
    assert parse_check("SELEC 1")["parse_ok"] is False
    ok = parse_check("INSERT INTO dwd.t SELECT a.x AS x FROM ods.a a;")
    assert ok["parse_ok"] and ok["output_tables"] == ["dwd.t"]


def test_generate_package_exports() -> None:
    import lineage.generate as gen

    assert gen.__version__
    for name in ("generate_sql", "generate_pipeline", "apply_pipeline", "validate_generation"):
        assert callable(getattr(gen, name))
    assert _GenerateLLM is GenerateLLM
