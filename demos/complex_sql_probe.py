#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复杂 SQL 解析能力实测探针（任务一）：逐个样本跑真实解析器并落证据。

用法::

    .venv/bin/python demos/complex_sql_probe.py                  # 全部样本
    .venv/bin/python demos/complex_sql_probe.py 01 02            # 只看编号前缀命中的样本
    .venv/bin/python demos/complex_sql_probe.py --detail 12      # 打印字段级血缘明细

产出：

* ``reports/complex_sql_probe.json`` —— 结构化实测结果（供 docs 引用）
* 终端摘要 —— 每个样本 表级/字段级 血缘条数与 resolved 比例
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from lineage_core.parser import SqlLineageParser  # noqa: E402

SAMPLES_DIR = PROJECT_ROOT / "examples" / "complex_sql"
OUT_JSON = PROJECT_ROOT / "reports" / "complex_sql_probe.json"


def probe_one(parser: SqlLineageParser, path: Path) -> dict:
    """解析单个样本文件，返回结构化实测结果。"""
    text = path.read_text(encoding="utf-8")
    entry: dict = {
        "sample": path.name,
        "path": str(path.relative_to(PROJECT_ROOT)),
        "chars": len(text),
        "parse_error": None,
        "statements": [],
        "totals": {},
    }
    try:
        statements = parser.parse_sql(text, source=path.name)
    except Exception as e:  # noqa: BLE001 — 解析失败本身就是要记录的证据
        entry["parse_error"] = f"{type(e).__name__}: {e}"
        return entry

    in_tables, out_tables = [], []
    col_total = col_resolved = 0
    for st in statements:
        cols = st.get("column_lineage") or []
        resolved = sum(1 for c in cols if c.get("resolved"))
        col_total += len(cols)
        col_resolved += resolved
        for t in st.get("input_table_names") or []:
            if t not in in_tables:
                in_tables.append(t)
        for t in st.get("output_table_names") or []:
            if t not in out_tables:
                out_tables.append(t)
        entry["statements"].append(
            {
                "index": st["statement_index"],
                "task_type": st["task_type"],
                "input_tables": st.get("input_table_names") or [],
                "output_tables": st.get("output_table_names") or [],
                "table_lineage": [
                    f"{e['source']} -> {e['target']}" for e in st.get("table_lineage") or []
                ],
                "column_count": len(cols),
                "column_resolved": resolved,
                "column_unresolved": len(cols) - resolved,
                "unresolved_detail": [
                    {
                        "target": f"{c['target_table']}.{c['target_column']}",
                        "source_table": c["source_table"],
                        "source_column": c["source_column"],
                        "expression": c["expression"],
                    }
                    for c in cols
                    if not c.get("resolved")
                ],
                "column_sample": [
                    {
                        "target": f"{c['target_table']}.{c['target_column']}",
                        "source": (
                            f"{c['source_table']}.{c['source_column']}"
                            if c["source_table"]
                            else c["source_column"]
                        ),
                        "expression": c["expression"],
                        "resolved": bool(c.get("resolved")),
                    }
                    for c in cols[:12]
                ],
                "partitions": st.get("partition_filters") or {},
                "filters": st.get("filters") or [],
                "joins": [f"{j['type']} {j['left']}->{j['right']}" for j in st.get("joins") or []],
            }
        )
    entry["totals"] = {
        "statement_count": len(statements),
        "input_tables": in_tables,
        "output_tables": out_tables,
        "column_count": col_total,
        "column_resolved": col_resolved,
        "column_unresolved": col_total - col_resolved,
        "resolve_rate": round(col_resolved / col_total, 4) if col_total else None,
    }
    return entry


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="复杂 SQL 解析能力实测探针")
    ap.add_argument("prefixes", nargs="*", help="样本编号前缀过滤（如 01 07）")
    ap.add_argument("--dialect", default="hive")
    ap.add_argument("--detail", nargs="*", default=[], help="打印这些编号样本的字段级血缘明细")
    ap.add_argument("--no-json", action="store_true", help="不写 reports/complex_sql_probe.json")
    args = ap.parse_args(argv)

    parser = SqlLineageParser(dialect=args.dialect)
    paths = sorted(SAMPLES_DIR.glob("*.sql"))
    if args.prefixes:
        paths = [p for p in paths if any(p.name.startswith(x) for x in args.prefixes)]

    results = []
    for path in paths:
        entry = probe_one(parser, path)
        results.append(entry)
        print("=" * 78)
        print(f"[{entry['sample']}]  ({entry['chars']} 字符)")
        if entry["parse_error"]:
            print(f"  !! 解析异常: {entry['parse_error']}")
            continue
        t = entry["totals"]
        print(f"  语句数={t['statement_count']}")
        print(f"  输入表: {', '.join(t['input_tables']) or '(无)'}")
        print(f"  输出表: {', '.join(t['output_tables']) or '(无)'}")
        print(
            f"  字段级血缘: {t['column_count']} 条 (resolved={t['column_resolved']}, "
            f"unresolved={t['column_unresolved']}, 比例={t['resolve_rate']})"
        )
        for st in entry["statements"]:
            print(
                f"    - stmt#{st['index']} {st['task_type']}: "
                f"{len(st['table_lineage'])} 条表级 / {st['column_count']} 条字段级 "
                f"(resolved {st['column_resolved']})"
            )
            if st["unresolved_detail"]:
                names = sorted({d["target"] for d in st["unresolved_detail"]})
                print(f"        未解析列: {', '.join(names[:8])}{' ...' if len(names) > 8 else ''}")

        if any(entry["sample"].startswith(x) for x in args.detail):
            print("  -- 字段级血缘明细 --")
            for st in entry["statements"]:
                for c in st["column_sample"]:
                    flag = "" if c["resolved"] else "  <<UNRESOLVED>>"
                    print(f"    {c['target']}  <-  {c['source']}   [{c['expression']}]{flag}")

    if not args.no_json:
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(
            json.dumps({"dialect": args.dialect, "samples": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("=" * 78)
        print(f"JSON 实测结果已写入: {OUT_JSON.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
