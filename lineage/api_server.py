#!/usr/bin/env python3
"""
血缘解析 + 业务口径知识库 HTTP 服务（标准库实现，零额外依赖）

用途：让运行在容器里的 DolphinScheduler 任务插件（Java）能够调用本血缘解析引擎；
     知识库端点则把 P4 提炼出的业务口径暴露给前端 / 其他系统做检索与问答。

启动：.venv/bin/python -m lineage.api_server [--host 0.0.0.0] [--port 18080]
端点：
  GET  /health                    健康检查
  POST /parse                     {"sql": "...", "dialect": "hive"} → 表级/字段级血缘
  POST /analyze                   {"sql": "...", "dialect": "hive", "with_knowledge": true}
                                  → 血缘 + 业务口径知识库一体化（/parse 的超集）
  POST /impact                    {"table": "...", "graph": "path.json", "depth": 3} → 下游影响
  POST /upstream                  {"table": "...", "graph": "path.json"} → 上游溯源
  GET  /kb/summary                业务口径知识库概览
  POST /kb/search                 {"query": "产量", "kinds": ["metrics"], "limit": 20} → 知识检索
  POST /kb/ask                    {"question": "产量怎么算的", "use_llm": "auto"} → 问数（口径/血缘/术语）
  GET  /kb/metric?name=产量        指标口径详情（公式 + 依赖 + 血缘链路）
"""
import argparse
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lineage.parser import SqlLineageParser  # noqa: E402
from lineage.graph import LineageGraph  # noqa: E402
from lineage.knowledge import (  # noqa: E402
    KnowledgeStore,
    answer,
    collect_target_fields,
    default_db_path,
    format_metric,
    match_knowledge,
    search,
    knowledge_unavailable,
)

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


# --------------------------------------------------------------------------- #
# P5：血缘 × 业务口径一体化端点
# --------------------------------------------------------------------------- #
def _kb_db_path(payload: dict) -> str:
    """请求体里的 db > 环境变量 KB_DB > 默认 data/knowledge.db（统一成绝对路径）。"""
    db = (payload.get("db") or "").strip() or os.environ.get("KB_DB") or str(default_db_path())
    if not os.path.isabs(db):
        db = os.path.join(PROJECT_ROOT, db)
    return db


def build_knowledge_section(payload: dict, parsed: dict) -> dict:
    """在解析结果之上叠加知识库口径匹配；任何异常都降级成 kb_available=false。"""
    if payload.get("with_knowledge") in (False, "false", "False", 0, "0", "off"):
        out = knowledge_unavailable("本次请求 with_knowledge=false，已跳过知识库匹配")
        out["hint"] = "如需口径匹配，请传 with_knowledge=true"
        return out

    db_path = _kb_db_path(payload)
    if not os.path.exists(db_path):
        return knowledge_unavailable(f"知识库文件不存在: {db_path}")

    try:
        store = KnowledgeStore(db_path, create=False)
    except Exception as e:  # noqa: BLE001
        return knowledge_unavailable(f"打开知识库失败: {type(e).__name__}: {e}")

    try:
        counts = store.counts()
        if not counts.get("kb_metrics"):
            return knowledge_unavailable(f"知识库为空: {db_path}")
        return match_knowledge(
            store,
            collect_target_fields(parsed.get("column_lineage") or []),
            parsed.get("output_tables") or [],
            limit_metrics=int(payload.get("limit_metrics") or 12),
            limit_rules=int(payload.get("limit_rules") or 5),
        )
    except Exception as e:  # noqa: BLE001
        return knowledge_unavailable(f"知识库匹配失败: {type(e).__name__}: {e}")
    finally:
        store.close()


def handle_analyze(payload: dict) -> dict:
    """``/parse`` 的超集：血缘解析结果原样返回，另加 knowledge 段。"""
    parsed = handle_parse(payload)
    if not parsed.get("success"):
        return parsed
    parsed["knowledge"] = build_knowledge_section(payload, parsed)
    return parsed


# --------------------------------------------------------------------------- #
# P4：业务口径知识库端点
# --------------------------------------------------------------------------- #
def _open_kb(payload: dict) -> KnowledgeStore:
    """打开知识库：请求体里的 db > 环境变量 KB_DB > 默认 data/knowledge.db。"""
    return KnowledgeStore(_kb_db_path(payload))


def handle_kb_summary(payload: dict) -> dict:
    store = _open_kb(payload)
    try:
        summary = store.summary(limit_tables=int(payload.get("top") or 15))
    finally:
        store.close()
    return {"success": True, **summary}


def handle_kb_search(payload: dict) -> dict:
    query = (payload.get("query") or payload.get("q") or "").strip()
    if not query:
        return {"success": False, "error": "query 不能为空"}
    kinds = payload.get("kinds") or payload.get("kind")
    if isinstance(kinds, str):
        kinds = [k.strip() for k in kinds.split(",") if k.strip()]
    store = _open_kb(payload)
    try:
        result = search(store, query, kinds=kinds, limit=int(payload.get("limit") or 20))
    finally:
        store.close()
    return {"success": True, **result}


def handle_kb_ask(payload: dict) -> dict:
    question = (payload.get("question") or payload.get("q") or "").strip()
    if not question:
        return {"success": False, "error": "question 不能为空"}
    use_llm = payload.get("use_llm", "auto")
    if use_llm in ("on", "true", "True", True):
        use_llm = True
    elif use_llm in ("off", "false", "False", False):
        use_llm = False
    store = _open_kb(payload)
    try:
        result = answer(store, question, use_llm=use_llm)
    finally:
        store.close()
    return {"success": True, **result}


def handle_kb_metric(payload: dict) -> dict:
    name = (payload.get("name") or payload.get("metric") or "").strip()
    if not name:
        return {"success": False, "error": "name 不能为空（指标名或中文业务名）"}
    store = _open_kb(payload)
    try:
        rows = store.get_metric(name)
        details = [{"metric": row, "detail": format_metric(store, row)} for row in rows]
    finally:
        store.close()
    return {"success": True, "name": name, "count": len(rows),
            "metrics": rows, "details": details}


ROUTES = {
    "/parse": handle_parse,
    "/analyze": handle_analyze,
    "/impact": handle_impact,
    "/upstream": handle_upstream,
    "/kb/summary": handle_kb_summary,
    "/kb/search": handle_kb_search,
    "/kb/ask": handle_kb_ask,
    "/kb/metric": handle_kb_metric,
}

#: 支持 GET 的端点（其余为 POST）
GET_ROUTES = {"/health", "/kb/summary", "/kb/metric"}


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
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in ("/health", ""):
            kb_db = str(default_db_path())
            kb_exists = os.path.exists(kb_db)
            kb_metrics = 0
            if kb_exists:
                try:
                    with KnowledgeStore(kb_db, create=False) as store:
                        kb_metrics = int(store.counts().get("kb_metrics") or 0)
                except Exception:  # noqa: BLE001 — /health 不能因为知识库坏而失败
                    kb_metrics = -1
            self._send(200, {
                "success": True,
                "service": "lineage-api",
                "endpoints": sorted(ROUTES.keys()),
                "get_endpoints": sorted(GET_ROUTES),
                "default_graph": os.path.basename(DEFAULT_GRAPH),
                "graph_exists": os.path.exists(DEFAULT_GRAPH),
                "kb_db": kb_db,
                "kb_db_exists": kb_exists,
                "kb_metrics": kb_metrics,
            })
            return
        fn = ROUTES.get(path)
        if fn and path in GET_ROUTES:
            payload = {k: (v[0] if len(v) == 1 else v)
                       for k, v in parse_qs(parsed.query).items()}
            try:
                self._send(200, fn(payload))
            except Exception as e:
                self._send(500, {
                    "success": False,
                    "error": f"{type(e).__name__}: {e}",
                    "traceback": traceback.format_exc()[-1500:],
                })
            return
        self._send(404, {
            "success": False,
            "error": "未知路径，请用 GET " + ", ".join(sorted(GET_ROUTES))
                     + "；POST " + ", ".join(sorted(ROUTES)),
        })

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
    print(f"   端点: {', '.join(sorted(ROUTES))}   GET: {', '.join(sorted(GET_ROUTES))}", flush=True)
    print(f"   默认血缘图: {DEFAULT_GRAPH}（存在={os.path.exists(DEFAULT_GRAPH)}）", flush=True)
    print(f"   知识库: {default_db_path()}（存在={os.path.exists(str(default_db_path()))}）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)


if __name__ == "__main__":
    main()
