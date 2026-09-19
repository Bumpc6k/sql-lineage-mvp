#!/usr/bin/env python3
"""
血缘解析 HTTP 服务（标准库实现，零额外依赖）

用途：让运行在容器里的 DolphinScheduler 任务插件（Java）能够调用本血缘解析引擎。
     插件 → HTTP → 本服务 → lineage 解析引擎 → 返回血缘 JSON

启动：.venv/bin/python -m lineage.api_server [--host 0.0.0.0] [--port 18080]
端点：
  GET  /health                    健康检查
  POST /parse                     {"sql": "...", "dialect": "hive"} → 表级/字段级血缘
  POST /impact                    {"table": "...", "graph": "path.json", "depth": 3} → 下游影响
  POST /upstream                  {"table": "...", "graph": "path.json"} → 上游溯源
"""
import argparse
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lineage.parser import SqlLineageParser  # noqa: E402
from lineage.graph import LineageGraph  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_GRAPH = os.path.join(PROJECT_ROOT, "warehouse_graph.json")


def handle_parse(payload: dict) -> dict:
    sql = payload.get("sql") or ""
    dialect = payload.get("dialect") or "hive"
    if not sql.strip():
        return {"success": False, "error": "sql 不能为空"}

    parser = SqlLineageParser(dialect=dialect)
    stmts = parser.parse_sql(sql, source=payload.get("source_name") or "<inline>")
    if not isinstance(stmts, list):
        stmts = [stmts]

    inputs, outputs, tlineage, clineage = set(), set(), [], []
    for st in stmts:
        if not isinstance(st, dict):
            continue
        inputs.update(st.get("input_table_names") or [])
        outputs.update(st.get("output_table_names") or [])
        tlineage.extend(st.get("table_lineage") or [])
        clineage.extend(st.get("column_lineage") or [])

    return {
        "success": True,
        "dialect": dialect,
        "statement_count": len(stmts),
        "input_tables": sorted(inputs),
        "output_tables": sorted(outputs),
        "table_lineage": tlineage,
        "column_lineage_count": len(clineage),
        "column_lineage": clineage[:200],
        "statements": stmts,
    }


def _load_graph(graph_path: str):
    path = graph_path or DEFAULT_GRAPH
    if not os.path.isabs(path):
        path = os.path.join(PROJECT_ROOT, path)
    with open(path, encoding="utf-8") as f:
        return LineageGraph.from_dict(json.load(f)), path


def _impact_or_upstream(payload: dict, direction: str) -> dict:
    table = payload.get("table") or ""
    if not table:
        return {"success": False, "error": "table 不能为空"}
    graph, path = _load_graph(payload.get("graph") or "")
    fn = graph.downstream if direction == "downstream" else graph.upstream
    res = fn(table, depth=payload.get("depth")) or {}
    count_key = "downstream_count" if direction == "downstream" else "upstream_count"
    return {
        "success": True,
        "direction": direction,
        "start_table": table,
        "graph_file": os.path.basename(path),
        "found": res.get("found", False),
        "direct": res.get("direct") or [],
        "levels": res.get("levels") or [],
        "tables": res.get("tables") or [],
        count_key: res.get("table_count", 0),
        "edge_count": res.get("edge_count", 0),
        "paths": res.get("paths") or [],
    }


def handle_impact(payload: dict) -> dict:
    return _impact_or_upstream(payload, "downstream")


def handle_upstream(payload: dict) -> dict:
    return _impact_or_upstream(payload, "upstream")


ROUTES = {
    "/parse": handle_parse,
    "/impact": handle_impact,
    "/upstream": handle_upstream,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "LineageAPI/1.0"

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", ""):
            self._send(200, {
                "success": True,
                "service": "lineage-api",
                "endpoints": sorted(ROUTES.keys()),
                "default_graph": os.path.basename(DEFAULT_GRAPH),
                "graph_exists": os.path.exists(DEFAULT_GRAPH),
            })
        else:
            self._send(404, {"success": False, "error": "未知路径，请用 " + ", ".join(sorted(ROUTES))})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        fn = ROUTES.get(path)
        if not fn:
            self._send(404, {"success": False, "error": f"未知端点 {path}"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            self._send(400, {"success": False, "error": f"请求体不是合法 JSON: {e}"})
            return
        try:
            self._send(200, fn(payload))
        except Exception as e:
            self._send(500, {
                "success": False,
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()[-1500:],
            })

    def log_message(self, fmt, *args):
        sys.stderr.write("[lineage-api] " + fmt % args + "\n")


def main():
    ap = argparse.ArgumentParser(description="血缘解析 HTTP 服务（供 DolphinScheduler 插件调用）")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=18080)
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"✅ 血缘服务已启动: http://{args.host}:{args.port}", flush=True)
    print(f"   端点: {', '.join(sorted(ROUTES))}   GET /health", flush=True)
    print(f"   默认血缘图: {DEFAULT_GRAPH}（存在={os.path.exists(DEFAULT_GRAPH)}）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)


if __name__ == "__main__":
    main()
