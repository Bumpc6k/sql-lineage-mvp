# -*- coding: utf-8 -*-
"""SQL 血缘解析命令行入口（P1 单文件解析 + P2 图谱/影响分析/可视化）。

用法示例::

    # ===== P1：单文件 / 多文件解析（原有行为，保持不变）=====
    python -m lineage.cli examples/02_dwd_to_dws_join.sql
    python -m lineage.cli examples/02_dwd_to_dws_join.sql --output json
    python -m lineage.cli examples/*.sql --output json --save /tmp/lineage.json
    echo "CREATE TABLE t AS SELECT id FROM ods_a" | python -m lineage.cli - --output json

    # ===== P2：目录扫描 → 全局血缘图 =====
    python -m lineage.cli scan examples/warehouse --dialect hive         --graph-out warehouse_graph.json --report-out scan_report.txt

    # ===== P2：上下游分析与排查 =====
    python -m lineage.cli upstream ads.ads_卷烟产销月报 --graph warehouse_graph.json
    python -m lineage.cli impact   ods.ods_卷烟产量流水 --graph warehouse_graph.json --depth 3
    python -m lineage.cli path     src.erp_产量接口 ads.ads_卷烟产销月报 --graph warehouse_graph.json
    python -m lineage.cli cycle    --graph warehouse_graph.json
    python -m lineage.cli stats    --graph warehouse_graph.json

    # ===== P2：可视化导出 =====
    python -m lineage.cli viz warehouse_graph.json --format mermaid --out lineage.mmd
    python -m lineage.cli viz warehouse_graph.json --format html    --out lineage.html         --highlight ads.ads_卷烟产销月报 --depth 3

    # ===== P3：对接 DolphinScheduler（旁路集成，只读海豚 OpenAPI）=====
    python -m lineage.cli ds sync --graph-out ds_lineage.json
    python -m lineage.cli ds workflows --graph ds_lineage.json
    python -m lineage.cli ds tables ods.ods_卷烟产量流水 --graph ds_lineage.json
    python -m lineage.cli ds task wf_dws_汇总 --graph ds_lineage.json
    python -m lineage.cli ds upstream ads.ads_经营指标驾驶舱 --graph ds_lineage.json

    # ===== P4：业务口径知识提炼 + 知识库（SQLite 检索 / 问答 / Markdown 导出）=====
    python -m lineage.cli kb build
    python -m lineage.cli kb summary
    python -m lineage.cli kb search 产量
    python -m lineage.cli kb show chanliang_qty
    python -m lineage.cli kb ask "产量怎么算的"
    python -m lineage.cli kb export --md docs/业务口径知识库.md

    # ===== P7：生成引擎（需求 → 加工 SQL / 分层链路 / 海豚工作流 / 反向校验）=====
    python -m lineage.cli generate sql --source ods.ods_卷烟产量流水         --target cdw.dwd_卷烟产量明细 --metric 产量 --group-by plant_code --partition dt
    python -m lineage.cli generate pipeline --requirement "生成产销存月报"         --target-layer ads --max-stages 4 --json --save pipeline.json
    python -m lineage.cli generate apply --pipeline-file pipeline.json         --workflow-name wf_gen_产销存月报 --project-code 123 --apply --json
    python -m lineage.cli generate validate --pipeline-file pipeline.json --json

    # ===== P8：脚本内嵌 SQL 解析（Shell / Python-PySpark）=====
    python -m lineage.cli parse-script examples/scripts/etl_ods_每日抽取.sh
    python -m lineage.cli parse-script examples/scripts/etl_dws_pyspark.py --output json
    python -m lineage.cli parse-script run.sh --task-type SHELL --kind auto --resolve-files

公共开关：``--output json`` 输出结构化 JSON；``--save PATH`` 把 JSON 落盘；
``--quiet`` 抑制 stderr 提示。"""

from __future__ import annotations

import argparse
import sys

from typing import Optional, Sequence

from lineage.cli.common import SUBCOMMANDS
from lineage.cli.graph_cmds import _SUBCOMMAND_RUNNERS, run_parse

def build_subcommand_parser() -> argparse.ArgumentParser:
    """子命令的帮助入口（``python -m lineage.cli help`` 用不到，这里仅暴露清单）。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli",
        description="P2 子命令：scan / upstream / impact / path / cycle / stats / viz；"
                    "P3 子命令：ds（sync / workflows / tables / task / upstream）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("subcommand", choices=SUBCOMMANDS, help="要执行的子命令")
    return ap

def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 主函数，返回进程退出码。

    ``argv[0]`` 命中 :data:`SUBCOMMANDS` 时走 P2 子命令，否则按 P1 的
    「文件路径 / stdin」流程解析（保证原有用法完全不变）。
    """
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in SUBCOMMANDS:
        try:
            return _SUBCOMMAND_RUNNERS[args[0]](args[1:])
        except SystemExit as exc:      # 子命令内部用 SystemExit 抛用户级错误
            if isinstance(exc.code, str):
                print(exc.code, file=sys.stderr)
                return 2
            return int(exc.code or 0)
        except BrokenPipeError:        # pragma: no cover - 管道提前关闭
            return 0
    if args and args[0] == "help":
        print(__doc__)
        return 0
    return run_parse(args)

__all__ = ['build_subcommand_parser', 'main']  # 本模块自有名字（供 lineage/cli/__init__.py 兼容导出）
