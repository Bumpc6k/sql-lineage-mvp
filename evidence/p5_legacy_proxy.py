#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「旧版血缘服务」代理：只为了验证插件在 /analyze 缺失时的降级行为。

行为（监听 18099）：
  POST /analyze  -> 404 {"success": false, "error": "unknown endpoint /analyze"}（模拟老版本服务）
  其它路径        -> 原样转发给 http://127.0.0.1:18080（真正的血缘服务）

用法：.venv/bin/python evidence/p5_legacy_proxy.py [--port 18099] [--upstream http://127.0.0.1:18080]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "http://127.0.0.1:18080"


class Handler(BaseHTTPRequestHandler):
    server_version = "LegacyLineageProxy/1.0"

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._proxy("GET")

    def do_POST(self):  # noqa: N802
        self._proxy("POST")

    def _proxy(self, method: str) -> None:
        if self.path.split("?")[0].rstrip("/") == "/analyze":
            sys.stderr.write("[legacy-proxy] /analyze -> 404（模拟旧版服务）\n")
            self._send(404, {"success": False, "error": "unknown endpoint /analyze（旧版服务）"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else None
        req = urllib.request.Request(UPSTREAM + self.path, data=payload, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw, code = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            raw, code = e.read(), e.code
        except Exception as e:  # noqa: BLE001
            self._send(502, {"success": False, "error": f"upstream failed: {e}"})
            return
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        sys.stderr.write("[legacy-proxy] " + fmt % args + "\n")


def main() -> int:
    global UPSTREAM
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18099)
    ap.add_argument("--upstream", default=UPSTREAM)
    args = ap.parse_args()
    UPSTREAM = args.upstream.rstrip("/")
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"旧版血缘服务代理已启动: http://0.0.0.0:{args.port} → /analyze 404，其它转发 {UPSTREAM}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
