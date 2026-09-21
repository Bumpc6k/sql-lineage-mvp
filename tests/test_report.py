#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""格式化 HTML 报告（``lineage.report``）+ ``POST /report`` / ``GET /report/<id>`` 测试。

覆盖：
* 报告 ID 生成（时间戳 + SQL 短 hash）与文件名清洗（防路径穿越）；
* ``render_report`` 的结构：标题栏 / 表级流向 / 字段级真表格 / 口径卡片 / 内联 SVG 链路图 /
  SQL 段 / 页脚，且**零外部依赖**（没有外链 script/link/img/字体）；
* 转义安全：SQL 与表达式里的 ``<script>`` 不会跑成标签；
* 降级：知识库不可用、无字段血缘、无链路时都要出正常页面，不抛异常；
* ``rank_metrics``：聚合/比率类口径排在条件分支前面、置信度高的靠前；
* ``save_report`` / ``prune_reports``：落盘、URL 拼装、只保留最近 N 份；
* HTTP 层：真实起一个 ``ThreadingHTTPServer``，验证 ``POST /report`` → ``GET /report/<id>``
  （200 + ``text/html; charset=utf-8``）与 404 分支。
"""

from __future__ import annotations

import http.client
import json
import re
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from lineage.render import report as report_mod
from lineage.serve.api_server import Handler, ROUTES, build_report_meta, handle_report
from lineage_core.parser import SqlLineageParser
from lineage.render.report import (
    make_report_id,
    prune_reports,
    rank_metrics,
    render_report,
    safe_report_id,
    save_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_SQL_PATH = PROJECT_ROOT / "examples" / "knowledge_demo" / "cdw" / "dwd_卷烟产量码段明细.sql"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def demo_sql() -> str:
    return DEMO_SQL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parsed(demo_sql: str) -> dict:
    """一条真实的 ``/analyze`` 风格结果（含 knowledge 段）。"""
    from lineage.serve.api_server import handle_analyze
    from lineage.knowledge import default_db_path

    out = handle_analyze({"sql": demo_sql, "dialect": "hive", "with_knowledge": True})
    assert out["success"] is True
    assert Path(str(default_db_path())).exists(), "本测试依赖已建好的默认知识库 data/knowledge.db"
    return out


# --------------------------------------------------------------------------- #
# 1. report_id / 文件名
# --------------------------------------------------------------------------- #
def test_make_report_id_has_timestamp_and_hash() -> None:
    rid = make_report_id("SELECT 1", now=datetime(2026, 9, 20, 21, 30, 45))
    assert rid == "rpt_20260920_213045_" + make_report_id("SELECT 1").split("_")[-1]
    assert re.fullmatch(r"rpt_\d{8}_\d{6}_[0-9a-f]{8}", rid), rid


def test_make_report_id_is_stable_for_same_sql() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5)
    assert make_report_id("SELECT 1", moment) == make_report_id("SELECT 1", moment)
    assert make_report_id("SELECT 1", moment) != make_report_id("SELECT 2", moment)


def test_safe_report_id_strips_path_traversal() -> None:
    assert safe_report_id("../../etc/passwd") == "....etcpasswd"
    assert "/" not in safe_report_id("rpt_2026/../../../x")
    assert safe_report_id("") == ""


# --------------------------------------------------------------------------- #
# 2. 渲染结构
# --------------------------------------------------------------------------- #
def test_render_report_sections_and_stats(parsed: dict) -> None:
    html = render_report(parsed, {"task_name": "t_lineage_产量口径", "cost_ms": 118,
                                  "report_id": "rpt_test_0001"})
    for needle in ("<!DOCTYPE html>", 'lang="zh-CN"', "血缘分析报告", "LINEAGE REPORT",
                   "表级血缘", "字段级血缘", "业务口径（知识库匹配）", "上游链路图",
                   "加工条件 / SQL 原文", "t_lineage_产量口径", "118 ms"):
        assert needle in html, needle
    # 表级流向：源表 → 目标表
    assert "ods.ods_卷烟码段流水" in html and "cdw.dwd_卷烟产量码段明细" in html
    # 字段级血缘是真表格（thead/tbody + 每行一个 tr）
    assert "<table id=\"col-table\">" in html
    assert html.count('<tr data-key=') == len(parsed["column_lineage"])
    assert "chanliang_qty" in html
    # 口径卡片：公式 / 来源脚本 / 依赖 / 链路
    assert html.count('<div class="card">') >= 1
    assert "SUM(打码量) + SUM(跳码量) - SUM(重码量)" in html
    assert "dwd_卷烟产量码段明细.sql" in html
    assert "src.mes_码段采集接口" in html
    # 内联 SVG 链路图（节点 + 箭头 marker）
    assert "<svg" in html and "marker-end=\"url(#arrow)\"" in html
    # 页脚
    assert "由 sql-lineage-mvp 生成" in html


def test_render_report_has_no_external_dependency(parsed: dict) -> None:
    """单文件零外部依赖：没有外链 script/link/img/@import/字体。"""
    html = render_report(parsed, {"report_id": "rpt_test_0002"})
    assert "<link" not in html
    assert "<img" not in html
    assert "@import" not in html
    assert "url(http" not in html
    # 唯一的 <script> 必须是内联的（没有 src）
    scripts = re.findall(r"<script[^>]*>", html)
    assert scripts and all("src=" not in tag for tag in scripts), scripts
    # 除了 SVG 的 xmlns 命名空间（不是网络请求），不应出现任何外网地址
    assert "cdn." not in html and "googleapis" not in html
    assert not re.search(r'(src|href)="https?://', html), "不允许外链资源"


def test_render_report_escapes_user_content() -> None:
    """SQL 里的尖括号/引号必须转义成一个安全的文本节点。"""
    evil_sql = 'INSERT INTO t.a SELECT 1 AS x FROM s.b WHERE note = \'<script>alert(1)</script>\''
    parsed = {
        "success": True,
        "dialect": "hive",
        "statement_count": 1,
        "input_tables": ["s.b"],
        "output_tables": ["t.a"],
        "table_lineage": [{"source": "s.b", "target": "t.a"}],
        "column_lineage_count": 1,
        "column_lineage": [{"target_table": "t.a", "target_column": "x", "source_table": "s.b",
                            "source_column": "y", "expression": "<img src=x onerror=alert(1)>"}],
        "statements": [{"statement_index": 1, "sql": evil_sql, "filters": ["a = '<b>'"],
                        "partition_filters": {"dt": "2026-01-01"}}],
        "knowledge": {"kb_available": False, "metrics": [], "reason": "空库"},
    }
    html = render_report(parsed, {"report_id": "rpt_test_0003"})
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "onerror=alert(1)" not in html.replace("&lt;img src=x onerror=alert(1)&gt;", "")
    assert "&lt;b&gt;" in html
    # 降级文案
    assert "未匹配到业务口径" in html and "空库" in html


def test_render_report_without_lineage_is_still_valid() -> None:
    html = render_report({"success": True, "knowledge": {"kb_available": True, "metrics": []}},
                         {"report_id": "rpt_test_0004", "task_name": "空任务"})
    assert "未解析出表级血缘" in html
    assert "未解析出字段级血缘" in html
    assert "知识库已就绪，但本任务产出字段未匹配到已登记指标口径" in html
    assert "无可绘制的上游链路" in html
    assert html.rstrip().endswith("</html>")


def test_svg_falls_back_to_table_level_edges() -> None:
    """没有口径链路时，用表级血缘画图（源表 → 目标表）。"""
    html = render_report({
        "success": True,
        "input_tables": ["ods.a"],
        "output_tables": ["dwd.b"],
        "table_lineage": [{"source": "ods.a", "target": "dwd.b"}],
        "column_lineage": [],
        "statements": [],
        "knowledge": {"kb_available": True, "metrics": []},
    }, {"report_id": "rpt_test_0005"})
    assert "<svg" in html
    assert "ods.a" in html and "dwd.b" in html
    assert "共 2 个节点 / 1 条边" in html


# --------------------------------------------------------------------------- #
# 3. 口径排序
# --------------------------------------------------------------------------- #
def test_rank_metrics_prefers_aggregate_and_confidence() -> None:
    metrics = [
        {"chinese_name": "条件口径", "metric_type": "条件分支", "confidence": 0.99, "depends_on": []},
        {"chinese_name": "函数口径", "metric_type": "函数转换", "confidence": 0.9, "depends_on": []},
        {"chinese_name": "低置信聚合", "metric_type": "聚合", "confidence": 0.5, "depends_on": []},
        {"chinese_name": "高置信聚合", "metric_type": "聚合", "confidence": 0.9, "depends_on": []},
        {"chinese_name": "比率口径", "metric_type": "比率", "confidence": 0.95, "depends_on": []},
    ]
    order = [m["chinese_name"] for m in rank_metrics(metrics)]
    # 类型优先（聚合 0 > 比率 1 > 条件分支 3 > 函数转换 4），同类型内置信度高的靠前
    assert order == ["高置信聚合", "低置信聚合", "比率口径", "条件口径", "函数口径"]


def test_rank_metrics_is_stable_and_tolerates_junk() -> None:
    metrics = [{"chinese_name": "a", "confidence": "oops"}, {"chinese_name": "b"}]
    assert [m["chinese_name"] for m in rank_metrics(metrics)] == ["a", "b"]
    assert rank_metrics([]) == []


# --------------------------------------------------------------------------- #
# 4. save_report / prune_reports
# --------------------------------------------------------------------------- #
def test_save_report_writes_file_and_urls(parsed: dict, tmp_path: Path) -> None:
    info = save_report(parsed, {"task_name": "本地任务", "cost_ms": 42}, directory=tmp_path)
    target = tmp_path / f"{info['report_id']}.html"
    assert target.is_file()
    assert info["size_bytes"] == target.stat().st_size > 5000
    assert info["url"] == f"http://localhost:18080/report/{info['report_id']}"
    assert info["internal_url"] == f"http://172.17.0.1:18080/report/{info['report_id']}"
    assert info["generated_at"] and info["task_name"] == "本地任务"
    assert "本地任务" in target.read_text(encoding="utf-8")


def test_save_report_respects_env_bases(parsed: dict, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LINEAGE_PUBLIC_BASE", "http://10.0.0.9:18080/")
    monkeypatch.setenv("LINEAGE_INTERNAL_BASE", "http://172.17.0.1:18081")
    info = save_report(parsed, {}, directory=tmp_path, report_id="rpt_env_0001")
    assert info["url"] == "http://10.0.0.9:18080/report/rpt_env_0001"
    assert info["internal_url"] == "http://172.17.0.1:18081/report/rpt_env_0001"


def test_save_report_custom_id_is_sanitized(parsed: dict, tmp_path: Path) -> None:
    info = save_report(parsed, {}, directory=tmp_path, report_id="../../evil")
    assert "/" not in info["file"] and ".." not in info["file"].replace("..", "")
    assert info["file"].startswith("....evil")


def test_prune_reports_keeps_newest(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"rpt_20260920_00000{i}_aaaaaaaa.html").write_text("x", encoding="utf-8")
    assert prune_reports(tmp_path, keep=2) == 3
    left = sorted(p.name for p in tmp_path.glob("rpt_*.html"))
    assert left == ["rpt_20260920_000003_aaaaaaaa.html", "rpt_20260920_000004_aaaaaaaa.html"]


def test_reports_dir_precedence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LINEAGE_REPORTS_DIR", str(tmp_path))
    assert report_mod.reports_dir() == tmp_path
    assert report_mod.reports_dir({}) == tmp_path
    assert report_mod.reports_dir({"reports_dir": str(tmp_path / "sub")}) == tmp_path / "sub"
    # 相对路径按项目根解析
    monkeypatch.setenv("LINEAGE_REPORTS_DIR", "reports_tmp")
    assert report_mod.reports_dir() == PROJECT_ROOT / "reports_tmp"


# --------------------------------------------------------------------------- #
# 5. POST /report 处理函数
# --------------------------------------------------------------------------- #
def test_handle_report_returns_url_and_stats(demo_sql: str, tmp_path: Path) -> None:
    out = handle_report({"sql": demo_sql, "dialect": "hive", "with_knowledge": True,
                         "task_name": "t_lineage_产量口径", "cost_ms": 118,
                         "reports_dir": str(tmp_path)})
    assert out["success"] is True
    assert re.fullmatch(r"rpt_\d{8}_\d{6}_[0-9a-f]{8}", out["report_id"])
    assert out["url"].endswith(f"/report/{out['report_id']}")
    assert (tmp_path / out["file"]).is_file()
    stats = out["stats"]
    assert stats["source_table_count"] == 1 and stats["target_table_count"] == 1
    assert stats["column_lineage_count"] == 17
    assert stats["kb_available"] is True and stats["metric_count"] >= 1
    assert "产量" in stats["metric_names"]
    assert stats["html_bytes"] > 5000


def test_handle_report_bad_sql(tmp_path: Path) -> None:
    out = handle_report({"sql": "   ", "reports_dir": str(tmp_path)})
    assert out["success"] is False


def test_report_in_routes() -> None:
    assert "/report" in ROUTES


def test_analyze_attaches_report_only_when_asked(parsed: dict, tmp_path: Path) -> None:
    """handle_analyze 默认不落盘（单测友好）；with_report=true 才返回 report 段。"""
    from lineage.serve.api_server import handle_analyze

    off = handle_analyze({"sql": "SELECT 1", "reports_dir": str(tmp_path)})
    assert off["success"] is True
    assert "report" not in off
    on = handle_analyze({"sql": DEMO_SQL_PATH.read_text(encoding="utf-8"), "with_knowledge": True,
                         "with_report": True, "reports_dir": str(tmp_path)})
    assert on["report"]["report_id"] == on["report_id"]
    assert on["report_url"].startswith("http://localhost:18080/report/")


def test_build_report_meta_defaults(demo_sql: str) -> None:
    parsed = SqlLineageParser(dialect="hive").parse_sql(demo_sql)
    parsed = parsed if isinstance(parsed, list) else [parsed]
    meta = build_report_meta({"sql": demo_sql, "mode": "sql"}, parsed[0])
    assert meta["task_name"] == "LINEAGE 血缘分析任务"
    assert meta["mode"] == "sql"
    assert meta["sql"] == demo_sql


# --------------------------------------------------------------------------- #
# 6. HTTP 端到端（真起服务，真发请求）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def report_server(tmp_path: Path, demo_sql: str, monkeypatch):
    """起一个真实的 HTTP 服务（随机端口），把报告目录指到 tmp_path。"""
    monkeypatch.setenv("LINEAGE_REPORTS_DIR", str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], tmp_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(port: int, method: str, path: str, body: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    payload = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), raw


def test_http_report_roundtrip(report_server, demo_sql: str) -> None:
    port, reports_dir_path = report_server
    status, _, raw = _request(port, "POST", "/report",
                              {"sql": demo_sql, "dialect": "hive", "with_knowledge": True,
                               "task_name": "http 端到端"})
    assert status == 200
    payload = json.loads(raw.decode("utf-8"))
    assert payload["success"] is True
    report_id = payload["report_id"]

    status, headers, raw = _request(port, "GET", f"/report/{report_id}")
    assert status == 200
    assert headers.get("Content-Type") == "text/html; charset=utf-8"
    body = raw.decode("utf-8")
    assert body.startswith("<!DOCTYPE html>") and body.rstrip().endswith("</html>")
    assert "http 端到端" in body and "chanliang_qty" in body
    assert len(body.encode("utf-8")) == payload["size_bytes"]

    # 报告清单
    status, _, raw = _request(port, "GET", "/reports")
    assert status == 200
    listing = json.loads(raw.decode("utf-8"))
    assert listing["count"] >= 1
    assert any(item["report_id"] == report_id for item in listing["reports"])

    # 不存在的报告 → 404 HTML
    status, headers, raw = _request(port, "GET", "/report/rpt_19700101_000000_deadbeef")
    assert status == 404
    assert headers.get("Content-Type") == "text/html; charset=utf-8"
    assert "报告不存在" in raw.decode("utf-8")


def test_http_analyze_returns_report_url(report_server, demo_sql: str) -> None:
    """插件走的路径：POST /analyze 一次调用同时拿到分析结果 + 报告地址。"""
    port, _ = report_server
    status, _, raw = _request(port, "POST", "/analyze",
                              {"sql": demo_sql, "dialect": "hive", "with_knowledge": True})
    assert status == 200
    payload = json.loads(raw.decode("utf-8"))
    assert payload["success"] is True
    assert payload["knowledge"]["metric_count"] >= 1
    report = payload["report"]
    assert report["report_id"] == payload["report_id"]
    assert payload["report_url"] == f"http://localhost:18080/report/{report['report_id']}"
    assert payload["report_internal_url"] == f"http://172.17.0.1:18080/report/{report['report_id']}"

    # with_report=false 时不落盘
    status, _, raw = _request(port, "POST", "/analyze",
                              {"sql": demo_sql, "with_knowledge": True, "with_report": False})
    assert status == 200
    assert "report" not in json.loads(raw.decode("utf-8"))


def test_http_report_url_is_openable_host(report_server, demo_sql: str) -> None:
    """URL 的 host:port 能被 urlparse 拆出来（浏览器可点）。"""
    port, _ = report_server
    _, _, raw = _request(port, "POST", "/report", {"sql": demo_sql, "with_knowledge": True})
    url = json.loads(raw.decode("utf-8"))["url"]
    parsed_url = urlparse(url)
    assert parsed_url.scheme == "http" and parsed_url.hostname == "localhost"
    assert parsed_url.path.startswith("/report/rpt_")
