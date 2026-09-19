#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 ``ds_lineage.json`` 里的调度侧表级血缘图（``table_graph``）单独导出。

导出的文件与 P2 的 ``scan --graph-out`` 产物**完全同构**，因此可以直接喂给 P2 的全部子命令
（``stats`` / ``upstream`` / ``impact`` / ``path`` / ``cycle`` / ``viz``），
不用改 P1/P2 一行代码。

用法::

    .venv/bin/python scripts/ds_export_table_graph.py ds_lineage.json /tmp/ds_table_graph.json
    .venv/bin/python -m lineage.cli impact cdw.dws_产销存汇总 --graph /tmp/ds_table_graph.json --depth 3
    .venv/bin/python -m lineage.cli viz    /tmp/ds_table_graph.json --format html \
        --out docs/ds_lineage.html --highlight ads.ads_经营指标驾驶舱 --depth 4
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lineage.graph import LineageGraph  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python scripts/ds_export_table_graph.py",
        description="从 ds sync 产物里导出调度侧表级血缘图（P2 同构图），供 P2 子命令继续分析",
    )
    ap.add_argument("lineage_json", help="ds sync 产出的 ds_lineage.json")
    ap.add_argument("out", nargs="?", default="/tmp/ds_table_graph.json", help="输出的图 JSON 路径")
    args = ap.parse_args(argv if argv is not None else None)

    source = Path(args.lineage_json)
    if not source.exists():
        print(f"错误：找不到 {source}（先跑 `python -m lineage.cli ds sync`）", file=sys.stderr)
        return 2
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "table_graph" not in data:
        print(f"错误：{source} 里没有 table_graph 字段（不是 ds sync 的产物？）", file=sys.stderr)
        return 2

    graph = LineageGraph.from_dict(data["table_graph"])
    graph.save(args.out)
    stats = graph.stats()
    print(f"[已导出] 调度侧表级血缘图 -> {Path(args.out).resolve()}"
          f"（{stats['node_count']} 张表 / {stats['edge_count']} 条边，schema_version={graph.to_dict()['schema_version']}）")
    print("         下一步：python -m lineage.cli stats --graph "
          f"{args.out} / impact <表名> --graph {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
