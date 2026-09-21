# -*- coding: utf-8 -*-
"""单元①（apps/web）与单元②（apps/lineage-api）之间的**边界契约**测试。

这个文件存在的意义：前端是独立部署单元，它与后端之间只有 HTTP 这一层耦合。
所以这里不测「前端好不好看」，只测三件必须成立的事：

1. **托管可用**：``GET /app/`` 与静态资源能正常下发（前端可以选择独立部署，也可以蹭后端托管）；
2. **跨源可用**：CORS 头与 OPTIONS 预检（前端从 :5173 等别的源调用时浏览器才放行）；
3. **契约对齐**：``apps/web/app.js`` 里依赖的响应字段，后端真实返回里**必须真的存在**
   （前端字段名写错时立刻失败，而不是等到浏览器里白屏）。
"""
from __future__ import annotations

import http.client
import json
import re
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "apps" / "web"
APP_JS = WEB_DIR / "app.js"


@pytest.fixture(scope="module")
def server():
    from lineage.serve.api_server import Handler

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _req(port: int, method: str, path: str, body: dict | None = None, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    hdr = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdr["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=hdr)
    r = conn.getresponse()
    payload = r.read().decode("utf-8", "replace")
    conn.close()
    return r.status, dict(r.getheaders()), payload


# --------------------------------------------------------------------------- 1. 托管可用
def test_frontend_module_exists() -> None:
    for f in ("index.html", "app.js", "style.css", "vendor/vue.global.prod.js"):
        assert (WEB_DIR / f).is_file(), f"前端模块缺文件: apps/web/{f}"


def test_serve_index(server) -> None:
    for path, needle, ctype in [
        ("/app/", "血缘工作台", "text/html"),
        ("/app/app.js", "createApp", "application/javascript"),
        ("/app/style.css", "--accent", "text/css"),
        ("/app/vendor/vue.global.prod.js", "vue v3.", "application/javascript"),
    ]:
        status, headers, payload = _req(server, "GET", path)
        assert status == 200, f"{path} -> {status}"
        assert ctype in headers.get("Content-Type", ""), f"{path} Content-Type={headers.get('Content-Type')}"
        assert needle in payload, f"{path} 内容里没有 {needle!r}"


def test_static_path_traversal_is_blocked(server) -> None:
    """``/app/../xx`` 不能读到前端目录之外的文件。"""
    status, _, payload = _req(server, "GET", "/app/../README.md")
    assert status == 404, f"穿越请求不该成功，实际 {status}"
    assert "SQL 血缘解析 MVP" not in payload, "越权读到了仓库根 README"


def test_vue_vendor_is_complete() -> None:
    """本地内置的 Vue 必须是一份完整的正式构建（防止 CDN 只下到重定向页/被截断）。"""
    raw = (WEB_DIR / "vendor" / "vue.global.prod.js").read_bytes()
    assert len(raw) > 100_000, f"vue.global.prod.js 只有 {len(raw)} 字节，疑似没下全"
    assert b"vue v3" in raw[:200], "不是 Vue 3 全局构建"
    assert b"createApp" in raw, "缺少 createApp，构建不完整"


# --------------------------------------------------------------------------- 2. 跨源可用
def test_cors_headers_on_json(server) -> None:
    status, headers, _ = _req(server, "GET", "/health", headers={"Origin": "http://localhost:5173"})
    assert status == 200
    assert headers.get("Access-Control-Allow-Origin"), "缺少 CORS 头，独立源的前端会被浏览器拦下"


def test_cors_preflight_options(server) -> None:
    status, headers, _ = _req(
        server, "OPTIONS", "/analyze",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert status == 204, f"预检应返回 204，实际 {status}"
    assert "POST" in headers.get("Access-Control-Allow-Methods", "")
    assert "Content-Type" in headers.get("Access-Control-Allow-Headers", "")


# --------------------------------------------------------------------------- 3. 契约对齐
ANALYZE_FIELDS = [
    "statement_count", "input_tables", "output_tables", "table_lineage",
    "column_lineage", "column_lineage_count", "knowledge", "report_id", "report_url",
]
TABLE_EDGE_FIELDS = ["source", "target"]
COLUMN_EDGE_FIELDS = ["target_table", "target_column", "source_table", "source_column", "expression"]
GRAPH_FIELDS = ["found", "direct", "levels", "tables", "paths", "edge_count"]
REPORT_FIELDS = ["report_id", "size_bytes", "generated_at", "url"]


def test_app_js_declares_only_real_endpoints() -> None:
    """app.js 里出现的 ``/xxx`` 端点路径必须是后端真实注册过的（防止前端调不存在的接口）。"""
    src = APP_JS.read_text(encoding="utf-8")
    from lineage.serve.api_server import GET_ROUTES, ROUTES

    known = set(ROUTES) | set(GET_ROUTES) | {"/health", "/reports", "/report/", "/app/"}
    used = set(re.findall(r"this\.call\('(/[A-Za-z0-9_/-]+)'", src))
    # 动态拼接的调用（this.call('/' + direction)）正则抓不到，按代码显式列出
    if "this.call('/' + direction" in src:
        used |= {"/upstream", "/impact"}
    assert used, "没从 app.js 里解析到任何端点调用，测试本身失效了"
    unknown = {u for u in used if u not in known}
    assert not unknown, f"app.js 调了后端没有的端点: {sorted(unknown)}"


def test_analyze_response_matches_frontend_contract(server) -> None:
    sql = ("insert overwrite table ads.ads_web_contract_probe\n"
           "select a.brand_name, sum(a.output_qty) as output_qty\n"
           "from cdw.dws_产销存汇总 a group by a.brand_name")
    status, _, payload = _req(server, "POST", "/analyze", body={"sql": sql, "dialect": "hive"})
    assert status == 200, payload[:200]
    d = json.loads(payload)
    for k in ANALYZE_FIELDS:
        assert k in d, f"/analyze 响应缺字段 {k}（前端依赖它）"
    assert d["table_lineage"], "示例 SQL 应产生表级血缘"
    for k in TABLE_EDGE_FIELDS:
        assert k in d["table_lineage"][0], f"table_lineage 条目缺字段 {k}"
    assert d["column_lineage"], "示例 SQL 应产生字段级血缘"
    for k in COLUMN_EDGE_FIELDS:
        assert k in d["column_lineage"][0], f"column_lineage 条目缺字段 {k}"
    assert isinstance(d["knowledge"], dict) and "metric_count" in d["knowledge"]


def test_graph_and_reports_match_frontend_contract(server) -> None:
    graph_file = str(REPO_ROOT / "warehouse_graph.json")
    if not Path(graph_file).is_file():
        pytest.skip("仓库根没有 warehouse_graph.json")
    status, _, payload = _req(server, "POST", "/upstream", body={"table": "ads.ads_产销存月报", "graph": graph_file})
    assert status == 200, payload[:200]
    up = json.loads(payload)
    for k in GRAPH_FIELDS:
        assert k in up, f"/upstream 响应缺字段 {k}（前端依赖它）"
    assert "upstream_count" in up
    if up.get("levels"):
        assert {"level", "tables"} <= set(up["levels"][0]), "levels 条目应是 {level, tables}"

    status, _, payload = _req(server, "GET", "/reports")
    assert status == 200, payload[:200]
    rep = json.loads(payload)
    assert "reports" in rep and "count" in rep
    if rep["reports"]:
        for k in REPORT_FIELDS:
            assert k in rep["reports"][0], f"/reports 条目缺字段 {k}"
