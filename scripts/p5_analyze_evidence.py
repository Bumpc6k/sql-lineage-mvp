#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P5 证据采集：#1 打真实 HTTP POST /analyze，打印血缘 + 知识库口径匹配结果。

用法：
    .venv/bin/python scripts/p5_analyze_evidence.py [SQL 文件] [--url http://127.0.0.1:18080/analyze]

为什么要用 Python 而不是直接 curl：SQL 里有中文、单引号、换行、注释，
    手写 JSON 极易转义出错 —— 这里用文件读入 + json.dumps 由解释器负责转义。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQL = PROJECT_ROOT / "examples" / "knowledge_demo" / "cdw" / "dwd_卷烟产量码段明细.sql"


def post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:800]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sql_file", nargs="?", default=str(DEFAULT_SQL))
    ap.add_argument("--url", default="http://127.0.0.1:18080/analyze")
    ap.add_argument("--raw", action="store_true", help="额外打印完整 JSON 的 knowledge 段")
    args = ap.parse_args()

    sql_path = Path(args.sql_file)
    sql = sql_path.read_text(encoding="utf-8")
    rel = sql_path.resolve().relative_to(PROJECT_ROOT) if sql_path.is_absolute() else sql_path

    print("=" * 78)
    print(f"$ curl -s -X POST {args.url} -H 'Content-Type: application/json' -d @<(python 读 {rel})")
    print("=" * 78)
    print(f"SQL 文件 : {rel}   行数: {len(sql.splitlines())}  字符数: {len(sql)}")

    data = post_json(args.url, {"sql": sql, "dialect": "hive", "with_knowledge": True})

    print(f"success            : {data.get('success')}")
    print(f"statement_count    : {data.get('statement_count')}")
    print(f"input_tables       : {data.get('input_tables')}")
    print(f"output_tables      : {data.get('output_tables')}")
    print(f"table_lineage      : {data.get('table_lineage')}")
    print(f"column_lineage_count: {data.get('column_lineage_count')}")

    kb = data.get("knowledge") or {}
    print("-" * 78)
    print(f"knowledge.kb_available : {kb.get('kb_available')}")
    print(f"knowledge.metric_count : {kb.get('metric_count')}")
    print(f"knowledge.matched_fields: {kb.get('matched_fields')}")
    for metric in kb.get("metrics") or []:
        print(f"  • {metric['chinese_name']}（{metric['target_column']}） @ {metric['target_table']}")
        print(f"      formula      : {metric['formula']}")
        print(f"      formula_full : {metric['formula_full']}")
        print(f"      metric_type  : {metric['metric_type']}   confidence: {metric['confidence']}"
              f"   matched_by: {metric['matched_by']}")
        print(f"      source       : {metric['source_script']} 第 {metric['source_statement']} 条语句")
        print(f"      depends_text : {metric['depends_text']}")
        print(f"      lineage_path : {' → '.join(metric['lineage_path'])}")
    print(f"knowledge.terms ({len(kb.get('terms') or [])}) : "
          + ", ".join(f"{t['field']}→{t['chinese_name']}" for t in (kb.get("terms") or [])))
    print(f"knowledge.rules ({len(kb.get('rules') or [])}) :")
    for r in kb.get("rules") or []:
        print(f"  • [{r['rule_type']}] {r['description']}  （{r['source_script']}）")

    sample = next((m for m in (kb.get("metrics") or []) if m["target_column"] == "chanliang_qty"), None)
    if sample:
        print("-" * 78)
        print("【证据】knowledge.metrics[chanliang_qty] 原文 JSON：")
        print(json.dumps(sample, ensure_ascii=False, indent=2))
    if args.raw:
        print("-" * 78)
        print(json.dumps(kb, ensure_ascii=False, indent=2))
    return 0 if data.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
