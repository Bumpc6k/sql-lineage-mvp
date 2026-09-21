#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作流级血缘（``lineage.workflow`` + ``POST /analyze-workflow``）测试。

覆盖：

* 用 :mod:`tests.ds_mock` 起一个模拟海豚，验证「登录 → 拉工作流定义 → 批量解析 → 合并」全链路；
* 任务类型过滤（SQL + SHELL 里的多语句 SQL 都吃下来）、脚本长度 / 语句数 / 产出表统计；
* 合并结果：表级边、任务级依赖、跨任务字段血缘（每条都带 ``task`` 归属）；
* 全链路 ``chain`` 与全局血缘拼接（``chain_in_workflow`` / ``chain_external``）；
* 链路质量体检：断链 / 孤岛 / 环路 / 未登记口径，以及 ``_find_cycles`` 的环路检测；
* 降级：知识库关掉 / 打不开、海豚登录失败、目标工作流不存在、DS 连不上，都必须回
  ``success=false`` 或可用结果，而不是抛异常；
* 报告：``meta.workflow`` 打开工作流模式（工作流概览 / 任务清单 / 链路质量体检 / 跨任务字段血缘
  多一列「来源任务」），且单任务报告**一个字节不变**；
* HTTP 层：真实起 ``ThreadingHTTPServer`` 跑 ``POST /analyze-workflow``。
"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from lineage.serve.api_server import Handler, POST_DEFAULTS, ROUTES, handle_analyze_workflow
from lineage.render.report import render_report
from lineage.ds.workflow import (
    _find_cycles,
    _longest_chain,
    _quality,
    _stitch_chain,
    analyze_workflow,
    workflow_definition,
)
from tests.ds_mock import (
    MOCK_PROJECT_CODE,
    MOCK_WF_DWS_CODE,
    MOCK_WF_ODS_CODE,
    MockDsServer,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KB_DB = PROJECT_ROOT / "data" / "knowledge.db"


@pytest.fixture(scope="module")
def ds():
    with MockDsServer() as server:
        yield server


def _analyze(ds, **overrides):
    payload = {
        "ds_base": ds.url,
        "ds_user": "admin",
        "ds_password": "dolphinscheduler123",
        "project_code": MOCK_PROJECT_CODE,
        "with_report": False,
        "with_knowledge": False,
    }
    payload.update(overrides)
    return analyze_workflow(payload)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def test_analyze_workflow_parses_all_tasks(ds) -> None:
    """SQL 任务的 ``sql`` 与 SHELL 任务 ``rawScript`` 里的多语句 SQL 都要被解析。"""
    result = _analyze(ds, process_define_code=MOCK_WF_ODS_CODE)

    assert result["success"] is True
    wf = result["workflow"]
    assert wf["name"] == "wf_ods_采集"
    assert wf["code"] == MOCK_WF_ODS_CODE
    assert wf["project_code"] == MOCK_PROJECT_CODE
    assert wf["task_count"] == 2
    assert wf["parsed_task_count"] == 2
    assert wf["statement_count"] == 3          # 1 条 SQL + SHELL 里 2 条 INSERT

    tasks = {t["name"]: t for t in result["tasks"]}
    assert set(tasks) == {"t_ods_产量流水", "t_ods_库存快照"}
    assert tasks["t_ods_产量流水"]["type"] == "SQL"
    assert tasks["t_ods_产量流水"]["script_len"] > 0
    assert tasks["t_ods_产量流水"]["output_tables"] == ["ods.ods_卷烟产量流水"]
    assert tasks["t_ods_库存快照"]["type"] == "SHELL"
    assert tasks["t_ods_库存快照"]["statement_count"] == 2
    assert sorted(tasks["t_ods_库存快照"]["output_tables"]) == sorted([
        "ods.ods_成品库存快照", "ods.ods_库存基线"])
    assert all(t["errors"] == [] for t in result["tasks"])


def test_analyze_workflow_merges_lineage_and_chain(ds) -> None:
    """合并后的表级血缘 / 节点边数 / 跨任务字段血缘。"""
    result = _analyze(ds, process_define_code=MOCK_WF_ODS_CODE)

    merged = result["merged"]
    edges = {(e["source"], e["target"]) for e in merged["table_lineage"]}
    assert ("src.erp_生产工单明细", "ods.ods_卷烟产量流水") in edges
    assert ("src.wms_库存快照", "ods.ods_成品库存快照") in edges
    assert merged["column_lineage_count"] == len(merged["column_lineage"]) > 0
    # 每条字段血缘都必须带「来源任务」，否则跨任务展示就无从谈起
    assert {c["task"] for c in merged["column_lineage"]} <= {"t_ods_产量流水", "t_ods_库存快照"}

    assert result["chain"] == ["src.erp_生产工单明细", "ods.ods_卷烟产量流水"] or \
        result["chain"][-1].startswith("ods.")
    assert result["workflow"]["chain_in_workflow"]
    assert result["cost_ms"] >= 0
    assert "sessionId" not in result                     # 不能把会话泄漏到结果里


def test_analyze_workflow_task_types_filter(ds) -> None:
    """``task_types`` 只留 SQL 时，SHELL 任务被跳过（但仍列在任务清单里）。"""
    result = _analyze(ds, process_define_code=MOCK_WF_ODS_CODE, task_types=["SQL"])
    assert result["success"] is True
    tasks = {t["name"]: t for t in result["tasks"]}
    assert tasks["t_ods_库存快照"]["parsed"] is False
    assert any("不在本次解析范围" in e for e in tasks["t_ods_库存快照"]["errors"])
    assert result["workflow"]["parsed_task_count"] == 1


def test_analyze_workflow_quality_checks(ds) -> None:
    """断链 / 孤岛 / 环路 / 未登记口径四类体检都要有结论。"""
    result = _analyze(ds, process_define_code=MOCK_WF_ODS_CODE)
    quality = result["quality"]

    # ods 产出在本工作流内没人消费，且不是应用层 → 断链
    assert {d["table"] for d in quality["dangling_outputs"]} == {
        "ods.ods_卷烟产量流水", "ods.ods_成品库存快照", "ods.ods_库存基线"}
    # src 层天然无上游 → 不算孤岛输入
    assert quality["orphan_inputs"] == []
    assert quality["cycles"] == []
    assert quality["missing_knowledge"] == []            # 关掉知识库时不报未登记
    assert quality["summary"]["checked"] is True
    assert quality["summary"]["dangling_output_count"] == 3


def test_analyze_workflow_by_name_and_project_scope(ds) -> None:
    """按 ``workflow_name`` 定位；``scope=project`` 把项目下所有工作流一起分析。"""
    by_name = _analyze(ds, workflow_name="wf_dws_汇总")
    assert by_name["success"] is True
    assert by_name["workflow"]["task_count"] == 2        # DEPENDENT 任务也在清单里

    whole = _analyze(ds, scope="project", project_code=MOCK_PROJECT_CODE)
    assert whole["success"] is True
    assert whole["workflow"]["workflow_count"] == 2
    assert whole["workflow"]["task_count"] == 4
    assert {w["name"] for w in whole["workflow"]["workflows"]} == {"wf_ods_采集", "wf_dws_汇总"}


# --------------------------------------------------------------------------- #
# 降级路径
# --------------------------------------------------------------------------- #
def test_analyze_workflow_missing_target(ds) -> None:
    result = _analyze(ds, project_code=MOCK_PROJECT_CODE)
    assert result["success"] is False
    assert "缺少" in result["error"]


def test_analyze_workflow_unknown_workflow_name(ds) -> None:
    result = _analyze(ds, project_code=MOCK_PROJECT_CODE, workflow_name="不存在的流程")
    assert result["success"] is False
    assert "找不到工作流" in result["error"]


def test_analyze_workflow_bad_password(ds) -> None:
    result = _analyze(ds, ds_password="wrong-password", process_define_code=MOCK_WF_ODS_CODE)
    assert result["success"] is False
    assert "登录" in result["error"] or "失败" in result["error"]


def test_analyze_workflow_ds_unreachable() -> None:
    result = analyze_workflow({
        "ds_base": "http://127.0.0.1:1/dolphinscheduler",
        "project_code": 1, "process_define_code": 2,
        "with_report": False, "ds_timeout": 1,
    })
    assert result["success"] is False
    assert "读取 DolphinScheduler 工作流失败" in result["error"]


def test_analyze_workflow_kb_off_and_on(ds) -> None:
    off = _analyze(ds, process_define_code=MOCK_WF_ODS_CODE, with_knowledge=False)
    assert off["knowledge"]["kb_available"] is False
    assert "with_knowledge" in off["knowledge"]["hint"]

    if not KB_DB.exists():
        pytest.skip("仓库知识库不存在，跳过口径匹配用例")
    on = _analyze(ds, process_define_code=MOCK_WF_DWS_CODE, with_knowledge=True, db=str(KB_DB))
    assert on["success"] is True
    assert on["knowledge"]["kb_available"] is True
    # cdw.dws_产销存汇总 在知识库里登记过口径，工作流级匹配必须命中
    assert on["metric_count"] >= 1
    assert any(t["metric_count"] >= 1 for t in on["tasks"])


# --------------------------------------------------------------------------- #
# 纯函数：链路 / 环路 / 拼链
# --------------------------------------------------------------------------- #
def test_longest_chain_prefers_ods_start() -> None:
    merged = {
        "nodes": ["dim.dim_brand", "ods.ods_a", "cdw.dwd_b", "cdw.dws_c"],
        "edges": [{"source": "dim.dim_brand", "target": "cdw.dwd_b"},
                  {"source": "ods.ods_a", "target": "cdw.dwd_b"},
                  {"source": "cdw.dwd_b", "target": "cdw.dws_c"}],
    }
    chain = _longest_chain(merged)
    assert chain == ["ods.ods_a", "cdw.dwd_b", "cdw.dws_c"]     # 维表旁支不该当主干


def test_find_cycles_detects_loop() -> None:
    nodes = ["a", "b", "c"]
    edges = [{"source": "a", "target": "b"}, {"source": "b", "target": "c"},
             {"source": "c", "target": "a"}]
    cycles = _find_cycles(nodes, edges)
    assert cycles, "自环依赖必须被检出"
    assert cycles[0][0] == cycles[0][-1]                        # 首尾闭合


def test_stitch_chain_uses_global_graph() -> None:
    class _FakeGraph:
        def upstream(self, table, depth=8):
            # 与真实 LineageGraph 一致：paths 的第一项就是被查的表
            return {"paths": [[table, "src.src_a"]]}

        def downstream(self, table, depth=8):
            return {"paths": [[table, "ads.ads_x"]]}

    stitched = _stitch_chain(["ods.ods_a", "cdw.dwd_b"], _FakeGraph())
    assert stitched["chain"] == ["src.src_a", "ods.ods_a", "cdw.dwd_b"]
    assert stitched["upstream"] == ["src.src_a"]
    assert stitched["downstream"] == ["ads.ads_x"]              # 只记不并进主链路
    assert _stitch_chain(["ods.ods_a"], None)["chain"] == ["ods.ods_a"]


def test_quality_cycle_and_missing_knowledge() -> None:
    merged = {
        "producers": {"cdw.a": ["t1"], "cdw.b": ["t2"]},
        "consumers": {"cdw.b": ["t1"], "cdw.a": ["t2"]},
        "nodes": ["cdw.a", "cdw.b"],
        "edges": [{"source": "cdw.a", "target": "cdw.b"},
                  {"source": "cdw.b", "target": "cdw.a"}],
    }
    quality = _quality(merged, kb_tables={"cdw.a"}, kb_available=True)
    assert quality["summary"]["cycle_count"] == 1
    assert [m["table"] for m in quality["missing_knowledge"]] == ["cdw.b"]
    assert quality["dangling_outputs"] == [] and quality["orphan_inputs"] == []


def test_workflow_definition_include_sub_process(ds) -> None:
    """``SUB_PROCESS`` / ``DEPENDENT`` 引用的工作流可被递归展开（同项目内）。"""
    from lineage.ds.client import DsClient

    client = DsClient(base_url=ds.url, user="admin", password="dolphinscheduler123")
    try:
        plain = workflow_definition(client, MOCK_PROJECT_CODE, MOCK_WF_DWS_CODE)
        assert len(plain["tasks"]) == 2
        expanded = workflow_definition(client, MOCK_PROJECT_CODE, MOCK_WF_DWS_CODE,
                                       include_sub_process=True)
        # DWS 工作流依赖 ODS 工作流 → 展开后多出 ODS 的 2 个任务
        names = [t["task"]["name"] for t in expanded["tasks"]]
        assert "t_ods_产量流水" in names
        assert len(expanded["tasks"]) == 4
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# 报告：工作流模式
# --------------------------------------------------------------------------- #
def test_render_report_workflow_mode(ds) -> None:
    result = _analyze(ds, process_define_code=MOCK_WF_ODS_CODE)
    parsed = {
        "dialect": "hive",
        "statement_count": result["workflow"]["statement_count"],
        "input_tables": result["merged"]["input_tables"],
        "output_tables": result["merged"]["output_tables"],
        "table_lineage": result["merged"]["table_lineage"],
        "column_lineage": result["merged"]["column_lineage"],
        "column_lineage_count": result["merged"]["column_lineage_count"],
        "statements": [],
        "knowledge": result["knowledge"],
    }
    html = render_report(parsed, {"task_name": "工作流 wf_ods_采集", "mode": "workflow",
                                  "workflow": dict(result["workflow"], tasks=result["tasks"],
                                                   quality=result["quality"])})
    assert "工作流血缘分析报告" in html and "WORKFLOW LINEAGE REPORT" in html
    assert "工作流概览" in html and "任务清单" in html and "链路质量体检" in html
    assert "来源任务" in html                       # 跨任务字段血缘多一列
    assert "全链路图" in html and "全链路（跨任务）" in html
    assert "src.erp_生产工单明细" in html
    # 仍然零外部依赖
    assert "<script src" not in html and "<link " not in html


def test_render_report_single_task_unchanged() -> None:
    """没有 ``meta.workflow`` 时，报告必须还是原来那份单任务报告。"""
    html = render_report({"dialect": "hive", "input_tables": ["ods.a"], "output_tables": ["dwd.b"],
                          "table_lineage": [{"source": "ods.a", "target": "dwd.b"}],
                          "column_lineage": [], "statements": [], "knowledge": {}},
                         {"task_name": "t1"})
    assert "血缘分析报告" in html
    assert "工作流概览" not in html and "WORKFLOW LINEAGE REPORT" not in html
    assert "上游链路图" in html


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #
def test_workflow_route_registered() -> None:
    assert "/analyze-workflow" in ROUTES
    assert POST_DEFAULTS["/analyze-workflow"]["with_report"] is True
    assert callable(handle_analyze_workflow)


def test_http_analyze_workflow(ds) -> None:
    import urllib.request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        body = json.dumps({
            "ds_base": ds.url, "project_code": MOCK_PROJECT_CODE,
            "process_define_code": MOCK_WF_ODS_CODE,
            "with_knowledge": False, "with_report": False,
        }).encode("utf-8")
        req = urllib.request.Request(f"http://{host}:{port}/analyze-workflow", data=body,
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("application/json")
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["success"] is True
        assert payload["workflow"]["name"] == "wf_ods_采集"
        assert payload["merged"]["column_lineage_count"] > 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_health_lists_workflow_endpoint() -> None:
    import urllib.request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert "/analyze-workflow" in payload["endpoints"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
