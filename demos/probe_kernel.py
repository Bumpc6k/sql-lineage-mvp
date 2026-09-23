#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旧内核探针：把 B2 接口设计所依赖的真实响应抓下来（评审证据，可复跑）。

用法：
    bash /usr/local/bin/start-lineage-api.sh          # 先起内核服务（:18080）
    .venv/bin/python demos/probe_kernel.py [输出路径]

默认输出 docs/platform-vision/raw/2026-09-23-kernel-probe.json。
注意：脚本**只调用只读端点**；/analyze 会顺带落一份 HTML 报告（内核行为，无法关闭）。
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.request

BASE = os.environ.get("LINEAGE_BASE", "http://127.0.0.1:18080")
DEFAULT_OUT = "docs/platform-vision/raw/2026-09-23-kernel-probe.json"
DEMO_SQL = "docs/ds_demo_workflows/sql/wf_ads_报表/t_ads_产销存月报.sql"

PROBES: list[tuple[str, str, dict]] = [
    ("analyze", "/analyze", {"mode": "sql", "sql": None, "dialect": "hive", "depth": 3}),  # sql 占位，运行时填入
    ("kb_search", "/kb/search", {"query": "产量"}),
    ("kb_ask", "/kb/ask", {"question": "ads.ads_产销存月报 的产量怎么来的"}),
    ("upstream", "/upstream", {"table": "ads.ads_产销存月报", "depth": 5}),
    ("impact", "/impact", {"table": "cdw.dws_产销存汇总"}),
    ("kb_summary", "/kb/summary", {}),
]


def call(path: str, body: dict, timeout: int = 60) -> tuple[int | str, dict]:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # 网络/超时/解析失败都记下来，不吞
        return "ERR", {"__error__": str(exc)[:200]}


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    sql = open(DEMO_SQL, encoding="utf-8").read()
    probe: dict = {}
    for name, path, body in PROBES:
        if body.get("sql", "x") is None:
            body = {**body, "sql": sql}
        status, resp = call(path, body)
        ok = resp.get("success") if isinstance(resp, dict) else None
        print("%-12s HTTP %-4s success=%s" % (name, status, ok))
        probe[name] = {"request": {"path": path, "body": body}, "http_status": status, "response": resp}

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "probed_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "base_url": BASE,
                "note": "旧内核真实响应快照，用于 B2 接口设计评审（未加工）",
                "endpoints": probe,
            },
            fh,
            ensure_ascii=False,
            indent=1,
        )
    print("\n✓ 已写 %s（%d KB）" % (out, os.path.getsize(out) // 1024))
    # 失败即非零退出，便于门禁使用
    bad = [k for k, v in probe.items() if not (isinstance(v["response"], dict) and v["response"].get("success"))]
    if bad:
        print("!! 以下探针未成功：%s" % ", ".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
