"""``lineage.ds_lineage``（DolphinScheduler 任务级多层血缘）测试。

输入是固定 JSON 样本（结构同 ``DsClient.fetch_all`` 的真实返回），
覆盖：多层血缘构建、任务 / 工作流 / 表三个维度的查询、工作流间依赖推导
（表血缘 + 海豚原生）、序列化往返、中文渲染、异常 SQL 降级，
以及 ``ds`` 四个只读子命令 + ``ds sync`` 的端到端（mock 服务）行为。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

from lineage import cli
from lineage.ds_lineage import (
    DsLineage,
    DsLineageBuilder,
    build_ds_lineage,
    format_ds_summary_text,
    format_ds_upstream_text,
    format_table_tasks_text,
    format_workflow_detail_text,
    format_workflows_text,
)
from tests.ds_mock import MockDsServer, dependent_task, relation, shell_task, sql_task

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PROJECT_CODE = 100001
WF_ODS, WF_DWD, WF_ADS = 200001, 200002, 200003

SQL_ODS = ("INSERT OVERWRITE TABLE ods.ods_卷烟产量流水 PARTITION (dt = '2026-01-01')\n"
           "SELECT a.work_order_no AS work_order_no, a.plant_code AS plant_code\n"
           "FROM src.erp_生产工单明细 a\nWHERE a.dt = '2026-01-01';")
SQL_SHELL = ('# 库存贴源（SHELL 任务）\nhive -e "\n'
             "INSERT OVERWRITE TABLE ods.ods_成品库存快照 PARTITION (dt = '2026-01-01')\n"
             "SELECT c.warehouse_code AS warehouse_code FROM src.wms_库存快照 c "
             "WHERE c.dt = '2026-01-01';\n\"")
SQL_DWD = ("INSERT OVERWRITE TABLE cdw.dwd_卷烟产量明细 PARTITION (dt = '2026-01-01')\n"
           "SELECT p.work_order_no AS work_order_no, pl.plant_name AS plant_name\n"
           "FROM ods.ods_卷烟产量流水 p\nLEFT JOIN dim.dim_plant pl ON p.plant_code = pl.plant_code\n"
           "WHERE p.dt = '2026-01-01';")
SQL_BROKEN = "INSERT OVERWRITE TABLE cdw.dwd_坏表 SELECT FROM WHERE GROUP BY;"
SQL_ADS = ("-- [1/2] 中间表\nCREATE TABLE ads.ads_产销存中间 AS\n"
           "SELECT m.plant_code AS plant_code, m.output_qty AS output_qty\n"
           "FROM cdw.dws_产销存汇总 m\n"
           "LEFT JOIN cdw.dwd_卷烟产量明细 d ON m.plant_code = d.plant_code\n"
           "WHERE m.dt = '2026-01-01';\n"
           "-- [2/2] 月报\n"
           "INSERT OVERWRITE TABLE ads.ads_产销存月报 PARTITION (dt = '2026-01-01')\n"
           "SELECT i.plant_code AS plant_code, SUM(i.output_qty) AS output_qty\n"
           "FROM ads.ads_产销存中间 i GROUP BY i.plant_code;")
SQL_ADS2 = ("INSERT OVERWRITE TABLE ads.ads_设备看板 PARTITION (dt = '2026-01-01')\n"
            "SELECT f.device_code AS device_code, f.run_minutes AS run_minutes\n"
            "FROM src.iot_设备工况采集 f WHERE f.dt = '2026-01-01';")


def sample_fetched() -> Dict[str, Any]:
    """构造 ``DsClient.fetch_all`` 形态的固定样本（3 个工作流 / 7 个任务）。"""
    return {
        "fetched_at": "2026-01-01 00:00:00",
        "base_url": "http://mock/dolphinscheduler",
        "user": "admin",
        "dialect": "hive",
        "projects": [{
            "project": {"code": PROJECT_CODE, "name": "演示项目", "description": "P3 测试项目"},
            "workflows": [
                {
                    "processDefinition": {"code": WF_ODS, "name": "wf_ods_采集", "version": 1,
                                          "releaseState": "OFFLINE", "description": "ODS 采集",
                                          "executionType": "PARALLEL"},
                    "taskDefinitionList": [
                        sql_task(300001, "t_ods_产量流水", SQL_ODS),
                        shell_task(300002, "t_ods_库存快照", SQL_SHELL),
                    ],
                    "processTaskRelationList": [relation(0, 300001), relation(0, 300002)],
                },
                {
                    "processDefinition": {"code": WF_DWD, "name": "wf_dwd_清洗", "version": 1,
                                          "releaseState": "OFFLINE", "description": "DWD 清洗",
                                          "executionType": "PARALLEL"},
                    "taskDefinitionList": [
                        sql_task(300003, "t_dwd_产量明细", SQL_DWD),
                        sql_task(300004, "t_dwd_坏脚本", SQL_BROKEN),
                    ],
                    "processTaskRelationList": [relation(0, 300003), relation(0, 300004)],
                },
                {
                    "processDefinition": {"code": WF_ADS, "name": "wf_ads_报表", "version": 1,
                                          "releaseState": "OFFLINE", "description": "ADS 报表",
                                          "executionType": "PARALLEL"},
                    "taskDefinitionList": [
                        dependent_task(300005, "t_check_上游就绪", WF_DWD),
                        sql_task(300006, "t_ads_产销存月报", SQL_ADS),
                        sql_task(300007, "t_ads_设备看板", SQL_ADS2),
                    ],
                    "processTaskRelationList": [relation(0, 300005), relation(300005, 300006),
                                                relation(300005, 300007)],
                },
            ],
        }],
    }


@pytest.fixture(scope="module")
def lineage() -> DsLineage:
    return build_ds_lineage(sample_fetched(), dialect="hive")


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "lineage.cli", *args],
                          cwd=PROJECT_ROOT, capture_output=True, text=True)


# --------------------------------------------------------------------------- #
# 构建：多层骨架
# --------------------------------------------------------------------------- #
def test_build_multilayer_skeleton(lineage: DsLineage) -> None:
    assert list(lineage.projects) == ["演示项目"]
    assert sorted(lineage.workflows) == ["wf_ads_报表", "wf_dwd_清洗", "wf_ods_采集"]
    assert len(lineage.tasks) == 7
    assert lineage.workflows["wf_ods_采集"].task_count == 2
    assert lineage.workflows["wf_ads_报表"].task_count == 3
    # 项目 -> 工作流 归属
    proj = lineage.projects["演示项目"]
    assert sorted(proj.workflow_names) == ["wf_ads_报表", "wf_dwd_清洗", "wf_ods_采集"]


def test_build_stats(lineage: DsLineage) -> None:
    st = lineage.stats()
    assert st["project_count"] == 1
    assert st["workflow_count"] == 3
    assert st["task_count"] == 7
    assert st["task_with_sql_count"] == 5              # 2 个：DEPENDENT + 坏脚本
    assert st["task_without_sql_count"] == 2
    assert st["statement_count"] == 6                  # SQL_ADS 是 2 条语句
    assert st["table_count"] == 11
    assert st["error_count"] == 1                      # 坏脚本被记录、没崩
    assert st["failed_task_count"] == 1
    assert st["workflow_dependency_count"] == 3
    assert st["table_dependency_count"] == 2
    assert st["native_dependency_count"] == 1
    assert st["workflow_level_count"] == 3
    assert st["script_count"] == 6                     # 5 份能解析 + 1 份坏脚本（仍被记录）
    assert st["column_mapping_count"] > 0


def test_task_reads_and_writes_tables(lineage: DsLineage) -> None:
    task = lineage.tasks["wf_ods_采集/t_ods_产量流水"]
    assert task.read_tables == ["src.erp_生产工单明细"]
    assert task.write_tables == ["ods.ods_卷烟产量流水"]
    assert task.task_type == "SQL"
    assert task.datasource_id == "1"
    assert task.scripts and task.scripts[0]["key"] == "taskParams.sql"

    shell = lineage.tasks["wf_ods_采集/t_ods_库存快照"]
    assert shell.task_type == "SHELL"
    assert shell.read_tables == ["src.wms_库存快照"]
    assert shell.write_tables == ["ods.ods_成品库存快照"]
    assert "hive -e" not in shell.scripts[0]["text"]


def test_task_level_relations_inside_workflow(lineage: DsLineage) -> None:
    check = lineage.tasks["wf_ads_报表/t_check_上游就绪"]
    assert check.upstream_tasks == []
    assert sorted(check.downstream_tasks) == ["t_ads_产销存月报", "t_ads_设备看板"]

    monthly = lineage.tasks["wf_ads_报表/t_ads_产销存月报"]
    assert monthly.upstream_tasks == ["t_check_上游就绪"]
    assert monthly.downstream_tasks == []


def test_broken_sql_recorded_as_failure(lineage: DsLineage) -> None:
    bad = lineage.tasks["wf_dwd_清洗/t_dwd_坏脚本"]
    assert bad.has_sql is False
    assert bad.errors and "SQL 解析失败" in bad.errors[0]["error"]
    assert len(lineage.failures) == 1
    assert lineage.failures[0]["task"] == "t_dwd_坏脚本"
    # 坏脚本不影响同一工作流的其它任务
    assert lineage.tasks["wf_dwd_清洗/t_dwd_产量明细"].write_tables == ["cdw.dwd_卷烟产量明细"]


def test_statement_summary_shape(lineage: DsLineage) -> None:
    task = lineage.tasks["wf_ads_报表/t_ads_产销存月报"]
    assert [s["statement_index"] for s in task.statements] == [1, 2]
    assert task.statements[0]["output_table_names"] == ["ads.ads_产销存中间"]
    assert task.statements[1]["output_table_names"] == ["ads.ads_产销存月报"]
    assert "column_lineage" not in task.statements[0]          # 默认只留条数
    assert task.statements[0]["column_mapping_count"] > 0


def test_tables_index_layer_and_degree(lineage: DsLineage) -> None:
    ods = lineage.tables["ods.ods_卷烟产量流水"]
    assert ods["layer"] == "ods"
    assert ods["produced_by_tasks"] == ["wf_ods_采集/t_ods_产量流水"]
    assert ods["producer_workflows"] == ["wf_ods_采集"]
    assert ods["consumer_workflows"] == ["wf_dwd_清洗"]
    src = lineage.tables["src.erp_生产工单明细"]
    assert src["is_source"] is True
    assert src["produced_by_tasks"] == []
    assert lineage.tables["ads.ads_产销存月报"]["is_leaf"] is True


# --------------------------------------------------------------------------- #
# 工作流间依赖推导
# --------------------------------------------------------------------------- #
def test_workflow_table_dependencies(lineage: DsLineage) -> None:
    deps = {(d["source"], d["target"], d["kind"]): d for d in lineage.workflow_dependencies}
    assert ("wf_ods_采集", "wf_dwd_清洗", "table") in deps
    assert ("wf_dwd_清洗", "wf_ads_报表", "table") in deps
    assert deps[("wf_ods_采集", "wf_dwd_清洗", "table")]["via_tables"] == ["ods.ods_卷烟产量流水"]
    assert deps[("wf_dwd_清洗", "wf_ads_报表", "table")]["via_tables"] == ["cdw.dwd_卷烟产量明细"]


def test_workflow_native_dependency(lineage: DsLineage) -> None:
    wf = lineage.workflows["wf_ads_报表"]
    assert wf.native_dependencies == [{"workflow": "wf_dwd_清洗", "workflow_code": str(WF_DWD),
                                       "via_task": "t_check_上游就绪", "kind": "dependent",
                                       "key": "dependence.dependTaskList[0].dependItemList[0].definitionCode"}]
    kinds = {u["kind"] for u in wf.upstream_workflows}
    assert kinds == {"native", "table"}
    assert {u["workflow"] for u in wf.upstream_workflows} == {"wf_dwd_清洗"}


def test_internal_table_excluded_from_cross_workflow_dependency(lineage: DsLineage) -> None:
    wf = lineage.workflows["wf_ads_报表"]
    assert wf.internal_tables == ["ads.ads_产销存中间"]
    # ads 工作流自产自用该表，不应出现 ads -> ads 的自依赖
    assert all(d["source"] != "wf_ads_报表" for d in lineage.workflow_dependencies)


def test_workflow_reads_writes_aggregation(lineage: DsLineage) -> None:
    wf = lineage.workflows["wf_ods_采集"]
    # 列表按表名排序（中文字符串比较），保证 JSON 落盘可 diff
    assert wf.writes == ["ods.ods_卷烟产量流水", "ods.ods_成品库存快照"]
    assert wf.reads == ["src.erp_生产工单明细", "src.wms_库存快照"]


# --------------------------------------------------------------------------- #
# 查询 ①②③④
# --------------------------------------------------------------------------- #
def test_query_table_tasks_reverse(lineage: DsLineage) -> None:
    result = lineage.table_tasks("ods.ods_卷烟产量流水")
    assert result["found"] is True
    assert result["layer"] == "ods"
    assert [p["task"] for p in result["produced_by"]] == ["t_ods_产量流水"]
    assert [c["task"] for c in result["consumed_by"]] == ["t_dwd_产量明细"]
    assert result["producer_workflows"] == ["wf_ods_采集"]
    assert result["consumer_workflows"] == ["wf_dwd_清洗"]
    assert result["is_source"] is False and result["is_leaf"] is False
    assert "src.erp_生产工单明细" in result["upstream_tables"]
    assert "ads.ads_产销存月报" in result["downstream_tables"]
    # 多层链路：项目 -> 工作流 -> 任务 -> 表
    chain = next(c for c in result["chains"] if c["role"] == "读取")
    assert chain == {"project": "演示项目", "workflow": "wf_dwd_清洗", "task": "t_dwd_产量明细",
                     "task_type": "SQL", "table": "ods.ods_卷烟产量流水", "role": "读取"}


def test_query_table_tasks_source_and_leaf(lineage: DsLineage) -> None:
    src = lineage.table_tasks("src.erp_生产工单明细")
    assert src["produced_by"] == [] and src["is_source"] is True
    leaf = lineage.table_tasks("ads.ads_产销存月报")
    assert leaf["consumed_by"] == [] and leaf["is_leaf"] is True
    assert leaf["producer_workflows"] == ["wf_ads_报表"]


def test_query_table_tasks_lenient_name(lineage: DsLineage) -> None:
    assert lineage.table_tasks("ods_卷烟产量流水")["table"] == "ods.ods_卷烟产量流水"
    assert lineage.table_tasks("ODS.ODS_卷烟产量流水")["table"] == "ods.ods_卷烟产量流水"


def test_query_table_tasks_not_found(lineage: DsLineage) -> None:
    result = lineage.table_tasks("不存在的表")
    assert result["found"] is False
    assert result["candidates"] == []
    assert "找不到" in format_table_tasks_text(result)


def test_query_workflow_detail(lineage: DsLineage) -> None:
    detail = lineage.workflow_detail("wf_dwd_清洗")
    assert detail["found"] is True
    assert detail["code"] == str(WF_DWD)
    assert detail["task_count"] == 2
    names = [t["name"] for t in detail["tasks"]]
    assert names == ["t_dwd_产量明细", "t_dwd_坏脚本"]
    good = next(t for t in detail["tasks"] if t["name"] == "t_dwd_产量明细")
    assert good["write_tables"] == ["cdw.dwd_卷烟产量明细"]
    assert "ods.ods_卷烟产量流水" in good["read_tables"]
    assert [u["workflow"] for u in detail["upstream_workflows"]] == ["wf_ods_采集"]
    # 下游同时被「表血缘」和「原生依赖」两种关系指向，工作流名会被列两次（各自带 kind）
    assert {u["workflow"] for u in detail["downstream_workflows"]} == {"wf_ads_报表"}
    assert {u["kind"] for u in detail["downstream_workflows"]} == {"table", "native"}


def test_query_workflow_detail_lenient_and_not_found(lineage: DsLineage) -> None:
    assert lineage.workflow_detail("wf_dwd_清洗")["workflow"] == "wf_dwd_清洗"
    assert lineage.workflow_detail("dwd")["workflow"] == "wf_dwd_清洗"       # 唯一子串
    missing = lineage.workflow_detail("不存在的工作流")
    assert missing["found"] is False
    assert "找不到" in format_workflow_detail_text(missing)


def test_query_workflow_list_levels(lineage: DsLineage) -> None:
    result = lineage.workflow_list()
    assert result["levels"] == [["wf_ods_采集"], ["wf_dwd_清洗"], ["wf_ads_报表"]]
    ods = next(w for w in result["workflows"] if w["workflow"] == "wf_ods_采集")
    assert ods["task_count"] == 2
    assert ods["write_table_count"] == 2
    assert ods["downstream_workflows"] == ["wf_dwd_清洗"]


def test_query_upstream_tables_with_producers(lineage: DsLineage) -> None:
    result = lineage.upstream_tables("ads.ads_产销存月报")
    assert result["found"] is True
    assert result["table"] == "ads.ads_产销存月报"
    assert "src.erp_生产工单明细" in result["tables"]
    producers = {p["table"]: p for p in result["producers"]}
    assert producers["ods.ods_卷烟产量流水"]["produced_by"] == [
        {"workflow": "wf_ods_采集", "task": "t_ods_产量流水"}]
    assert producers["src.erp_生产工单明细"]["is_source"] is True
    text = format_ds_upstream_text(result)
    assert "各上游表的调度产出方" in text


def test_query_upstream_depth_limit(lineage: DsLineage) -> None:
    shallow = lineage.upstream_tables("ads.ads_产销存月报", depth=1)
    assert shallow["table_count"] == 1
    assert shallow["tables"] == ["ads.ads_产销存中间"]


def test_query_downstream_tables(lineage: DsLineage) -> None:
    result = lineage.downstream_tables("ods.ods_卷烟产量流水")
    assert "cdw.dwd_卷烟产量明细" in result["tables"]
    assert "ads.ads_产销存月报" in result["tables"]


def test_query_project_detail(lineage: DsLineage) -> None:
    detail = lineage.project_detail("演示项目")
    assert detail["workflow_count"] == 3
    assert {w["workflow"] for w in detail["workflows"]} == {"wf_ods_采集", "wf_dwd_清洗", "wf_ads_报表"}
    assert lineage.project_detail("无此项目")["found"] is False


def test_table_graph_is_reusable_p2_engine(lineage: DsLineage) -> None:
    graph = lineage.table_graph
    assert graph.resolve_table("ods_卷烟产量流水") == "ods.ods_卷烟产量流水"
    stats = graph.stats()
    assert stats["node_count"] >= 11
    paths = graph.path_between("src.erp_生产工单明细", "ads.ads_产销存月报")
    assert paths["found"] is True


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def test_to_dict_from_dict_round_trip(lineage: DsLineage) -> None:
    payload = lineage.to_dict()
    assert payload["schema_version"] == 1
    assert payload["kind"] == "dolphinscheduler_lineage"
    again = DsLineage.from_dict(payload)
    assert again.to_dict() == payload
    assert again.stats() == lineage.stats()
    assert again.table_tasks("ods.ods_卷烟产量流水") == lineage.table_tasks("ods.ods_卷烟产量流水")


def test_save_and_load(lineage: DsLineage, tmp_path: Path) -> None:
    path = tmp_path / "sub" / "ds_lineage.json"
    lineage.save(path)
    assert path.exists()
    loaded = DsLineage.load(path)
    assert loaded.stats() == lineage.stats()
    assert loaded.base_url == "http://mock/dolphinscheduler"


def test_load_rejects_foreign_json(tmp_path: Path) -> None:
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"nodes": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        DsLineage.load(path)


# --------------------------------------------------------------------------- #
# 中文渲染
# --------------------------------------------------------------------------- #
def test_summary_text_contains_multilayer_view(lineage: DsLineage) -> None:
    text = format_ds_summary_text(lineage)
    assert "DolphinScheduler 血缘同步报告" in text
    assert "项目 -> 工作流 -> 任务 骨架" in text
    assert "wf_ods_采集" in text and "wf_ads_报表" in text
    assert "第 1 层" in text
    assert "wf_ods_采集（表依赖）" in text          # 上游工作流展示
    assert "解析失败" in text                        # 坏脚本被提示


def test_table_tasks_text_shows_chains(lineage: DsLineage) -> None:
    text = format_table_tasks_text(lineage.table_tasks("ods.ods_卷烟产量流水"))
    assert "【加工产出】" in text and "【读取消费】" in text
    assert "项目 -> 工作流 -> 任务 -> 表" in text
    assert "演示项目 -> wf_ods_采集 -> t_ods_产量流水 -> ods.ods_卷烟产量流水（写入）" in text


def test_workflow_detail_text_shows_tasks_and_deps(lineage: DsLineage) -> None:
    text = format_workflow_detail_text(lineage.workflow_detail("wf_ads_报表"))
    assert "任务节点清单" in text
    assert "t_check_上游就绪" in text and "无 SQL" in text
    assert "上游工作流" in text and "原生依赖" in text
    assert "海豚原生依赖声明" in text


def test_workflows_text_shows_levels(lineage: DsLineage) -> None:
    text = format_workflows_text(lineage.workflow_list())
    assert "依赖分层 3 层" in text
    assert "第 1 层（1 个" in text
    assert "依赖边明细" in text


def test_workflows_text_empty() -> None:
    assert "没有从血缘文件里读到任何工作流" in format_workflows_text({"workflow_count": 0})


# --------------------------------------------------------------------------- #
# 端到端：ds sync（打 mock 服务）
# --------------------------------------------------------------------------- #
def test_sync_against_mock_server(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    with MockDsServer() as srv:
        out = tmp_path / "ds_lineage.json"
        code = cli.main(["ds", "sync", "--base-url", srv.url, "--graph-out", str(out)])
        captured = capsys.readouterr()
    assert code == 0
    assert out.exists()
    assert "DolphinScheduler 血缘同步报告" in captured.out
    lineage = DsLineage.load(out)
    assert lineage.stats()["workflow_count"] == 2
    assert lineage.stats()["task_count"] == 4
    assert lineage.workflow_dependencies            # 有依赖边
    # sync 只读：写操作一个都不该发生
    assert not srv.find("/process-definition", "POST")
    assert not srv.find("/task-definition", "POST")
    assert srv.find("/logout", "POST")


def test_sync_json_output_and_project_filter(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    with MockDsServer() as srv:
        out = tmp_path / "ds.json"
        code = cli.main(["ds", "sync", "--base-url", srv.url, "--project", "不存在的项目",
                         "--graph-out", str(out), "--output", "json", "--quiet"])
        captured = capsys.readouterr()
    assert code == 1                      # 没拉到工作流 -> 非 0
    payload = json.loads(captured.out)
    assert payload["projects"] == []
    assert payload["stats"]["workflow_count"] == 0


def test_sync_connection_error_is_friendly(capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    code = cli.main(["ds", "sync", "--base-url", "http://127.0.0.1:1/dolphinscheduler",
                     "--graph-out", str(tmp_path / "none.json"), "--quiet"])
    captured = capsys.readouterr()
    assert code == 2
    assert "连不上 DolphinScheduler" in captured.err
    assert "Traceback" not in captured.err


# --------------------------------------------------------------------------- #
# 端到端：ds 只读子命令（先落一份样本血缘文件）
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def graph_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("ds") / "ds_lineage.json"
    build_ds_lineage(sample_fetched()).save(path)
    return path


def test_cli_ds_help() -> None:
    proc = _cli("ds", "--help")
    assert proc.returncode == 0
    assert "sync" in proc.stdout and "upstream" in proc.stdout


def test_cli_ds_unknown_subcommand() -> None:
    proc = _cli("ds", "nope")
    assert proc.returncode == 2
    assert "未知的 ds 子命令" in proc.stderr


def test_cli_ds_workflows(graph_file: Path) -> None:
    proc = _cli("ds", "workflows", "--graph", str(graph_file), "--output", "json")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["workflow_count"] == 3
    assert payload["levels"][0] == ["wf_ods_采集"]


def test_cli_ds_tables(graph_file: Path) -> None:
    proc = _cli("ds", "tables", "ods.ods_卷烟产量流水", "--graph", str(graph_file))
    assert proc.returncode == 0
    assert "t_ods_产量流水" in proc.stdout and "t_dwd_产量明细" in proc.stdout
    json_proc = _cli("ds", "tables", "ods.ods_卷烟产量流水", "--graph", str(graph_file),
                     "--output", "json")
    payload = json.loads(json_proc.stdout)
    assert payload["producer_workflows"] == ["wf_ods_采集"]


def test_cli_ds_tables_not_found(graph_file: Path) -> None:
    proc = _cli("ds", "tables", "没有这张表", "--graph", str(graph_file))
    assert proc.returncode == 1
    assert "找不到表" in proc.stdout
    assert "Traceback" not in proc.stderr


def test_cli_ds_task(graph_file: Path) -> None:
    proc = _cli("ds", "task", "wf_ads_报表", "--graph", str(graph_file))
    assert proc.returncode == 0
    assert "任务节点清单" in proc.stdout
    assert "t_ads_产销存月报" in proc.stdout
    assert "原生依赖" in proc.stdout


def test_cli_ds_task_not_found(graph_file: Path) -> None:
    proc = _cli("ds", "task", "没有这个工作流", "--graph", str(graph_file))
    assert proc.returncode == 1
    assert "找不到工作流" in proc.stdout


def test_cli_ds_upstream(graph_file: Path) -> None:
    proc = _cli("ds", "upstream", "ads.ads_产销存月报", "--graph", str(graph_file))
    assert proc.returncode == 0
    assert "上游溯源" in proc.stdout
    assert "各上游表的调度产出方" in proc.stdout


def test_cli_ds_missing_graph_file(tmp_path: Path) -> None:
    proc = _cli("ds", "workflows", "--graph", str(tmp_path / "nope.json"))
    assert proc.returncode == 2
    assert "血缘文件不存在" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_ds_broken_graph_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    proc = _cli("ds", "workflows", "--graph", str(bad))
    assert proc.returncode == 2
    assert "格式不正确" in proc.stderr


def test_existing_subcommands_still_registered() -> None:
    """P3 不能破坏 P1/P2 的命令面。"""
    for name in ("scan", "upstream", "impact", "path", "cycle", "stats", "viz"):
        assert name in cli.SUBCOMMANDS
    assert "ds" in cli.SUBCOMMANDS
