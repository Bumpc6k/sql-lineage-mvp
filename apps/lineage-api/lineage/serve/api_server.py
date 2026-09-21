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
                                  默认顺带生成一份 HTML 报告（body 传 with_report=false 可关掉）
  POST /analyze-workflow          {"project_code": 123, "process_define_code": 456, "scope": "current",
                                   "task_types": ["SQL","SHELL","PYTHON"], "include_sub_process": false,
                                   "with_knowledge": true, "with_report": true}
                                  → 工作流级血缘（LINEAGE_DAG 任务类型）：一次拉取整个工作流的
                                    全部任务脚本批量解析，返回 任务清单 / 合并表级血缘 / 跨任务字段血缘 /
                                    全链路 / 业务口径汇总 / 链路质量体检（断链·孤岛·环路·未登记口径）
  POST /report                    {"sql": "...", "dialect": "hive", "with_knowledge": true, "task_name": "..."}
                                  → 生成单文件 HTML 报告（零外部依赖），返回 report_id 与可点击 URL
  GET  /report/<report_id>        取回该 HTML 报告（text/html; charset=utf-8）
  GET  /reports                   最近生成的报告清单（JSON，便于排查 / 做导航页）
  POST /impact                    {"table": "...", "graph": "path.json", "depth": 3} → 下游影响
  POST /upstream                  {"table": "...", "graph": "path.json"} → 上游溯源
  GET  /kb/summary                业务口径知识库概览
  POST /kb/search                 {"query": "产量", "kinds": ["metrics"], "limit": 20} → 知识检索
  POST /kb/ask                    {"question": "产量怎么算的", "use_llm": "auto"} → 问数（口径/血缘/术语）
  GET  /kb/metric?name=产量        指标口径详情（公式 + 依赖 + 血缘链路）
  POST /generate/sql              {"source_tables": ["ods.ods_卷烟产量流水"],
                                   "target_table": "cdw.dwd_卷烟产量明细",
                                   "metrics": ["产量"], "group_by": ["plant_code"],
                                   "partition_field": "dt", "dialect": "hive"}
                                  → L1 单表加工 SQL 生成（知识库口径 → INSERT OVERWRITE ... SELECT），
                                    每一列都带 explain 依据，无法确认的部分进 warnings
  POST /generate/pipeline         {"requirement": "生成产销存月报", "target_layer": "ads",
                                   "max_stages": 4, "dialect": "hive"}
                                  → L2 分层链路生成（ods→dwd→dws→ads 多段 SQL + 链路图）
  POST /generate/apply            {"pipeline": {...L2 返回...}, "project_code": 123,
                                   "workflow_name": "wf_gen_产销存月报", "create_workflow": false,
                                   "env": "hive"}
                                  → L3 转成 DolphinScheduler 工作流定义；create_workflow=true 时
                                    真实调海豚 API 创建（同名先 OFFLINE 再删除）并回读校验
  POST /generate/validate         {"pipeline": {...} 或 "stages": [...] 或 "sql_list": [...]}
                                  → L4 反向校验：生成的 SQL 过血缘引擎 → 合并链路 → 体检
                                    （断链/孤岛/环路/跨层直连/口径一致性/与血缘图对比）+ HTML 报告
"""
import argparse
import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # 迁移后位于 lineage/serve/，多一层

from lineage_core.parser import SqlLineageParser  # noqa: E402
from lineage_core.graph import LineageGraph  # noqa: E402
from lineage.render.report import (  # noqa: E402
    list_reports,
    report_bases,
    report_path,
    reports_dir,
    safe_report_id,
    save_report,
)
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
from lineage.ds.workflow import analyze_workflow  # noqa: E402
from lineage.generate import (  # noqa: E402
    apply_pipeline,
    generate_pipeline,
    generate_sql,
    validate_generation,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))  # 仓库根
DEFAULT_GRAPH = os.path.join(PROJECT_ROOT, "warehouse_graph.json")

#: 独立前端模块（apps/web）的目录 —— 服务端可以顺手托管它，也可以完全不起（前端独立部署时用）
WEB_DIR = os.path.join(PROJECT_ROOT, "apps", "web")
#: CORS：前端是独立单元，可能从 :5173 等别的源调本服务。默认放开，可用环境变量收紧。
CORS_ORIGIN = os.environ.get("LINEAGE_CORS_ORIGIN", "*")
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".map": "application/json; charset=utf-8",
}



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


def _flag(value, default: bool = False) -> bool:
    """宽松布尔解析（JSON true / "true" / 1 / "on" 都认）。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


def build_report_meta(payload: dict, parsed: dict, cost_ms: Optional[int] = None) -> dict:
    """给 HTML 报告标题栏用的元信息（任务名 / 耗时 / 服务地址 / SQL 原文）。

    ``cost_ms`` 优先用调用方（插件）传的，没传就用本函数所在请求里实测的解析耗时。
    """
    return {
        "task_name": payload.get("task_name") or payload.get("source_name") or "LINEAGE 血缘分析任务",
        "mode": payload.get("mode") or "sql",
        "dialect": parsed.get("dialect"),
        "cost_ms": payload.get("cost_ms") or cost_ms,
        "statement_count": parsed.get("statement_count"),
        "service_url": report_bases()[0] + "/analyze",
        "sql": payload.get("sql") or "",
    }


def attach_report(payload: dict, parsed: dict, cost_ms: Optional[int] = None) -> Tuple[Optional[dict], str]:
    """生成并落盘 HTML 报告。返回 ``(报告信息, 错误说明)``；失败只降级，绝不打断血缘结果。"""
    try:
        meta = build_report_meta(payload, parsed, cost_ms)
        info = save_report(parsed, meta, directory=reports_dir(payload))
        return info, ""
    except Exception as e:  # noqa: BLE001 — 报告是附加产物，失败不能影响血缘接口
        err = f"{type(e).__name__}: {e}"
        sys.stderr.write(f"[lineage-api] 生成 HTML 报告失败: {err}\n")
        return None, err


def _analyze_core(payload: dict) -> dict:
    """血缘解析 + 知识库口径匹配（不落报告，供 /analyze 与 /report 复用）。"""
    parsed = handle_parse(payload)
    if not parsed.get("success"):
        return parsed
    parsed["knowledge"] = build_knowledge_section(payload, parsed)
    return parsed


def _analyze_timed(payload: dict) -> Tuple[dict, int]:
    """跑一次分析并实测服务端耗时（毫秒）—— 报告标题栏的「解析耗时」用它。"""
    started = time.perf_counter()
    parsed = _analyze_core(payload)
    return parsed, int((time.perf_counter() - started) * 1000)


def handle_analyze(payload: dict) -> dict:
    """``/parse`` 的超集：血缘解析结果原样返回，另加 knowledge 段。

    payload 里 ``with_report=true`` 时顺带生成 HTML 报告并返回 ``report`` 段
    （HTTP 层的 ``POST /analyze`` 默认就是 true —— 任务插件一次调用即可拿到
    分析结果 + 可点击的报告地址；直接调本函数的单测不会写盘）。
    """
    parsed, cost_ms = _analyze_timed(payload)
    if not parsed.get("success"):
        return parsed
    if _flag(payload.get("with_report"), default=False):
        info, err = attach_report(payload, parsed, cost_ms)
        if info:
            parsed["report"] = info
            parsed["report_id"] = info["report_id"]
            parsed["report_url"] = info["url"]
            parsed["report_internal_url"] = info["internal_url"]
        else:
            parsed["report_error"] = err
    return parsed


def handle_report(payload: dict) -> dict:
    """``POST /report``：跑一遍分析 → 渲染单文件 HTML 报告 → 返回可点击 URL。

    请求体与 ``/analyze`` 完全一致（``sql`` / ``dialect`` / ``with_knowledge`` / ``db``），
    额外可传 ``task_name``（报告标题里的任务名）、``report_id``（自定义 ID）、
    ``reports_dir``（落盘目录，测试用）。
    """
    parsed, cost_ms = _analyze_timed(payload)
    if not parsed.get("success"):
        return parsed

    info, err = attach_report(payload, parsed, cost_ms)
    if not info:
        return {"success": False, "error": f"生成 HTML 报告失败: {err}"}

    knowledge = parsed.get("knowledge") or {}
    metrics = knowledge.get("metrics") or []
    input_tables = parsed.get("input_tables") or []
    output_tables = parsed.get("output_tables") or []
    return {
        "success": True,
        **info,
        "hint": "浏览器直接打开 url；容器/任务里请用 internal_url",
        "stats": {
            "cost_ms": cost_ms,
            "dialect": parsed.get("dialect"),
            "statement_count": parsed.get("statement_count"),
            "source_table_count": len(input_tables),
            "target_table_count": len(output_tables),
            "input_tables": input_tables,
            "output_tables": output_tables,
            "table_lineage_count": len(parsed.get("table_lineage") or []),
            "column_lineage_count": parsed.get("column_lineage_count") or len(parsed.get("column_lineage") or []),
            "column_lineage_shown": len(parsed.get("column_lineage") or []),
            "kb_available": bool(knowledge.get("kb_available")),
            "metric_count": len(metrics),
            "metric_names": [m.get("chinese_name") or m.get("target_column") for m in metrics],
            "html_bytes": info["size_bytes"],
        },
    }


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


def handle_analyze_workflow(payload: dict) -> dict:
    """``POST /analyze-workflow``：工作流级血缘（LINEAGE_DAG 任务类型的服务端入口）。

    真正的逻辑在 :func:`lineage.workflow.analyze_workflow`：登录海豚 OpenAPI → 拉工作流定义
    → 批量解析所有任务脚本 → 合并成工作流级血缘 → 链路质量体检 → 落一份 HTML 报告。
    这里只负责把「内部异常」也包成业务错误（HTTP 一律 200，插件按 ``success`` 判断）。
    """
    try:
        return analyze_workflow(payload)
    except Exception as e:  # noqa: BLE001 — 任何意外都降级成可读的错误信息
        return {"success": False, "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()[-1500:]}


# --------------------------------------------------------------------------- #
# P7：生成引擎端点（L1 单表加工 SQL / L2 分层链路 / L3 落地海豚 / L4 反向校验）
# --------------------------------------------------------------------------- #
def _generate(payload: dict, fn) -> dict:
    """生成引擎端点的统一包装：内部异常也返回可读的业务错误（HTTP 一律 200）。"""
    try:
        return fn(payload)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()[-1500:]}


def handle_generate_sql(payload: dict) -> dict:
    """``POST /generate/sql``：L1 单表加工 SQL 生成。"""
    return _generate(payload, generate_sql)


def handle_generate_pipeline(payload: dict) -> dict:
    """``POST /generate/pipeline``：L2 分层链路生成。"""
    return _generate(payload, generate_pipeline)


def handle_generate_apply(payload: dict) -> dict:
    """``POST /generate/apply``：L3 链路转 DolphinScheduler 工作流（默认只出 JSON）。"""
    return _generate(payload, apply_pipeline)


def handle_generate_validate(payload: dict) -> dict:
    """``POST /generate/validate``：L4 反向校验 + 体检报告。"""
    return _generate(payload, validate_generation)


ROUTES = {
    "/parse": handle_parse,
    "/analyze": handle_analyze,
    "/analyze-workflow": handle_analyze_workflow,
    "/report": handle_report,
    "/impact": handle_impact,
    "/upstream": handle_upstream,
    "/kb/summary": handle_kb_summary,
    "/kb/search": handle_kb_search,
    "/kb/ask": handle_kb_ask,
    "/kb/metric": handle_kb_metric,
    "/generate/sql": handle_generate_sql,
    "/generate/pipeline": handle_generate_pipeline,
    "/generate/apply": handle_generate_apply,
    "/generate/validate": handle_generate_validate,
}

#: 支持 GET 的端点（其余为 POST）
#: 另有动态 GET：``/report/<report_id>``（取回 HTML 报告）与 ``/reports``（最近报告清单）
GET_ROUTES = {"/health", "/kb/summary", "/kb/metric", "/reports"}

#: HTTP 层为 ``POST /analyze`` 注入的默认值 —— 插件一次调用就能拿到报告地址
POST_DEFAULTS = {"/analyze": {"with_report": True}, "/analyze-workflow": {"with_report": True}}


class Handler(BaseHTTPRequestHandler):
    server_version = "LineageAPI/1.0"

    def _cors(self):
        """前端是独立部署单元，从别的源调本服务时需要放行（可用 LINEAGE_CORS_ORIGIN 收紧）。"""
        if CORS_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self):  # noqa: N802 — BaseHTTPRequestHandler 约定
        """CORS 预检：浏览器在跨源 POST(application/json) 前会先来一次。"""
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code: int, body: bytes, content_type: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel_path: str):
        """``GET /app/<file>``：托管独立前端模块 apps/web（不启动它也不影响服务）。"""
        rel = unquote(rel_path or "").lstrip("/") or "index.html"
        target = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not target.startswith(os.path.normpath(WEB_DIR)) or not os.path.isfile(target):
            self._send_html(404, (
                '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
                "<title>404 · 前端模块不可用</title></head>"
                '<body style="font-family:sans-serif;background:#0d1117;color:#e6edf3;padding:48px">'
                "<h1>404 · 前端模块不可用</h1>"
                f"<p>没找到 <code>{rel}</code>（前端目录 <code>{WEB_DIR}</code>）。</p>"
                "<p>可改用独立部署方式：<code>bash ops/start-web.sh</code>（:5173）。</p>"
                "</body></html>").encode("utf-8"))
            return
        ext = os.path.splitext(target)[1].lower()
        try:
            self._send_html(200, open(target, "rb").read(), STATIC_TYPES.get(ext, "application/octet-stream"))
        except OSError as e:
            sys.stderr.write(f"[lineage-api] 读取前端文件失败 {target}: {e}\n")
            self._send(500, {"success": False, "error": f"读取失败: {e}"})


    def _send_report(self, raw_id: str):
        """``GET /report/<report_id>``：把落盘的 HTML 原样吐回去（找不到给 404 HTML 页）。"""
        rid = safe_report_id(unquote(raw_id or ""))
        if rid:
            target = report_path(rid, reports_dir())
            try:
                if target.is_file():
                    self._send_html(200, target.read_bytes())
                    return
            except OSError as e:
                sys.stderr.write(f"[lineage-api] 读取报告失败 {target}: {e}\n")
        body = (
            '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            "<title>404 · 报告不存在</title></head>"
            '<body style="font-family:sans-serif;background:#0d1117;color:#e6edf3;padding:48px">'
            "<h1>404 · 报告不存在</h1>"
            f"<p>report_id: <code>{rid or '(空)'}</code></p>"
            "<p>可能已被清理（服务端只保留最近若干份）。"
            '可访问 <code>GET /reports</code> 查看最近清单，或 <code>POST /report</code> 重新生成。</p>'
            "</body></html>"
        ).encode("utf-8")
        self._send_html(404, body)

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
            public_base, internal_base = report_bases()
            self._send(200, {
                "success": True,
                "service": "lineage-api",
                "endpoints": sorted(ROUTES.keys()),
                "get_endpoints": sorted(GET_ROUTES) + ["/report/<report_id>"],
                "default_graph": os.path.basename(DEFAULT_GRAPH),
                "graph_exists": os.path.exists(DEFAULT_GRAPH),
                "kb_db": kb_db,
                "kb_db_exists": kb_exists,
                "kb_metrics": kb_metrics,
                "reports_dir": str(reports_dir()),
                "report_url_template": f"{public_base}/report/<report_id>",
                "report_internal_url_template": f"{internal_base}/report/<report_id>",
                "analyze_generates_report": POST_DEFAULTS["/analyze"]["with_report"],
            })
            return
        if path == "/app":
            self._send_static("index.html")
            return
        if path.startswith("/app/"):
            self._send_static(path[len("/app/"):])
            return
        if path.startswith("/report/"):
            self._send_report(path[len("/report/"):])
            return
        if path in ("/report", "/reports"):
            public_base, internal_base = report_bases()
            self._send(200, {
                "success": True,
                "reports_dir": str(reports_dir()),
                "public_base": public_base,
                "internal_base": internal_base,
                "count": len(list_reports(reports_dir(), limit=200)),
                "reports": list_reports(reports_dir(), limit=20),
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
                     + ", /report/<report_id>；POST " + ", ".join(sorted(ROUTES)),
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
        if not isinstance(payload, dict):
            self._send(400, {"success": False, "error": "请求体必须是 JSON 对象"})
            return
        # 插件只发 {"sql","dialect","with_knowledge"}：这里补齐默认值，
        # 让它一次调用就同时拿到「分析结果 + 可点击的报告 URL」
        for key, value in POST_DEFAULTS.get(path, {}).items():
            payload.setdefault(key, value)
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
