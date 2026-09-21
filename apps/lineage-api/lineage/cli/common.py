# -*- coding: utf-8 -*-
"""CLI 共享基础设施：子命令注册表、主参数解析器、公共参数/图加载/输出格式。"""

from __future__ import annotations

import argparse
import json
import sys

from lineage_core.graph import LineageGraph
from lineage_core.parser import DEFAULT_DIALECT, dumps
from pathlib import Path
from typing import Any, Dict

STDIN_SENTINEL = "-"

#: P2/P3/P4 子命令清单（argv[0] 命中即走子命令分支，否则走 P1 的旧解析流程）
SUBCOMMANDS = ("scan", "upstream", "impact", "path", "cycle", "stats", "viz", "ds", "kb",
               "generate", "parse-script")

# --------------------------------------------------------------------------- #
# P1：原有命令行（行为保持不变）
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    """构建 P1 命令行参数解析器（单文件 / 多文件解析）。"""
    parser = argparse.ArgumentParser(
        prog="python -m lineage.cli",
        description="SQL 血缘解析（Hive / Spark SQL 方言）：表级血缘 + 字段级血缘 + 过滤条件提取",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "sql_files",
        nargs="*",
        metavar="SQL_FILE",
        help="待解析的 SQL 文件路径，可传多个；传 '-' 或省略则从标准输入读取",
    )
    parser.add_argument(
        "-d",
        "--dialect",
        default=DEFAULT_DIALECT,
        help=f"sqlglot 方言名，默认 {DEFAULT_DIALECT}（可选 spark / postgres / doris / starrocks 等）",
    )
    parser.add_argument(
        "-o",
        "--output",
        choices=("text", "json", "both"),
        default="text",
        help="输出格式：text=终端摘要（默认），json=JSON，both=两者都输出",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON 缩进空格数，默认 2",
    )
    parser.add_argument(
        "--save",
        metavar="PATH",
        default=None,
        help="把 JSON 报告额外写入指定文件",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="终端摘要不使用 ANSI 颜色",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="不输出 stderr 提示信息（保存/警告等）",
    )
    return parser

# --------------------------------------------------------------------------- #
# P2：子命令实现
# --------------------------------------------------------------------------- #
def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-o",
        "--output",
        choices=("text", "json"),
        default="text",
        help="输出格式：text=中文摘要（默认），json=结构化 JSON",
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON 缩进，默认 2")
    parser.add_argument("--save", metavar="PATH", default=None, help="把 JSON 结果写入文件")
    parser.add_argument("--quiet", action="store_true", help="不输出 stderr 提示")

def _load_graph(path: str) -> LineageGraph:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"错误：血缘图文件不存在 -> {path}\n"
                         f"提示：先用 `python -m lineage.cli scan <目录> --graph-out {path}` 生成")
    try:
        return LineageGraph.load(p)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SystemExit(f"错误：血缘图文件格式不正确 -> {path}（{exc}）")

def _emit(payload: Dict[str, Any], args: argparse.Namespace, text: str) -> int:
    """按 --output 输出文本或 JSON，并按 --save 落盘。"""
    if args.output == "json":
        print(dumps(payload, indent=args.indent))
    else:
        print(text)
    if getattr(args, "save", None):
        Path(args.save).write_text(dumps(payload, indent=args.indent) + "\n", encoding="utf-8")
        if not getattr(args, "quiet", False):
            print(f"[已保存] JSON -> {Path(args.save).resolve()}", file=sys.stderr)
    return 0

__all__ = ['STDIN_SENTINEL', 'SUBCOMMANDS', 'build_arg_parser', '_add_common', '_load_graph', '_emit']  # 本模块自有名字（供 lineage/cli/__init__.py 兼容导出）
