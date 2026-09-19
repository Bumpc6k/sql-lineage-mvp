"""SQL 血缘解析命令行入口（P1 单文件解析 + P2 图谱/影响分析/可视化）。

用法示例::

    # ===== P1：单文件 / 多文件解析（原有行为，保持不变）=====
    python -m lineage.cli examples/02_dwd_to_dws_join.sql
    python -m lineage.cli examples/02_dwd_to_dws_join.sql --output json
    python -m lineage.cli examples/*.sql --output json --save /tmp/lineage.json
    echo "CREATE TABLE t AS SELECT id FROM ods_a" | python -m lineage.cli - --output json

    # ===== P2：目录扫描 → 全局血缘图 =====
    python -m lineage.cli scan examples/warehouse --dialect hive \
        --graph-out warehouse_graph.json --report-out scan_report.txt

    # ===== P2：上下游分析与排查 =====
    python -m lineage.cli upstream ads.ads_卷烟产销月报 --graph warehouse_graph.json
    python -m lineage.cli impact   ods.ods_卷烟产量流水 --graph warehouse_graph.json --depth 3
    python -m lineage.cli path     src.erp_产量接口 ads.ads_卷烟产销月报 --graph warehouse_graph.json
    python -m lineage.cli cycle    --graph warehouse_graph.json
    python -m lineage.cli stats    --graph warehouse_graph.json

    # ===== P2：可视化导出 =====
    python -m lineage.cli viz warehouse_graph.json --format mermaid --out lineage.mmd
    python -m lineage.cli viz warehouse_graph.json --format html    --out lineage.html \
        --highlight ads.ads_卷烟产销月报 --depth 3

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

公共开关：``--output json`` 输出结构化 JSON；``--save PATH`` 把 JSON 落盘；
``--quiet`` 抑制 stderr 提示。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .ds_client import DsApiError, DsAuthError, DsClient, DsConnectionError
from .ds_lineage import (
    DsLineage,
    DsLineageBuilder,
    format_ds_summary_text,
    format_ds_upstream_text,
    format_table_tasks_text,
    format_workflow_detail_text,
    format_workflows_text,
)
from .graph import (
    LineageGraph,
    build_graph,
    format_analysis_text,
    format_cycles_text,
    format_path_text,
    format_stats_text,
)
from .parser import DEFAULT_DIALECT, SqlLineageParser, dumps
from .scan import scan_directory
from .viz import to_html, to_mermaid

STDIN_SENTINEL = "-"

#: P2/P3/P4 子命令清单（argv[0] 命中即走子命令分支，否则走 P1 的旧解析流程）
SUBCOMMANDS = ("scan", "upstream", "impact", "path", "cycle", "stats", "viz", "ds", "kb")


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


def _load_sources(file_args: Sequence[str]) -> List[tuple]:
    """读取输入：返回 [(来源标签, SQL 文本)]；'-' 或空参数表示读 stdin。"""
    if not file_args:
        file_args = [STDIN_SENTINEL]

    out: List[tuple] = []
    for item in file_args:
        if item == STDIN_SENTINEL:
            text = sys.stdin.read()
            if not text.strip():
                raise SystemExit("错误：标准输入为空，请通过管道或重定向传入 SQL")
            out.append(("<stdin>", text))
        else:
            path = Path(item)
            if not path.exists():
                raise SystemExit(f"错误：文件不存在 -> {item}")
            out.append((str(path), path.read_text(encoding="utf-8")))
    return out


def run_parse(argv: Sequence[str]) -> int:
    """P1 流程：解析给定 SQL 文件（或 stdin）并输出文本 / JSON。"""
    args = build_arg_parser().parse_args(list(argv))
    parser = SqlLineageParser(dialect=args.dialect)

    try:
        sources = _load_sources(args.sql_files)
    except SystemExit as exc:
        print(exc.code, file=sys.stderr)
        return 2

    statements = []
    for label, text in sources:
        if not text.strip():
            continue
        statements.extend(parser.parse_sql(text, source=label))

    report = parser.aggregate(statements)

    if args.output in ("text", "both"):
        print(parser.summary_text(statements, report, color=not args.no_color))
        if args.output == "both":
            print()

    if args.output in ("json", "both"):
        print(dumps(report, indent=args.indent))

    if args.save:
        Path(args.save).write_text(dumps(report, indent=args.indent) + "\n", encoding="utf-8")
        if not args.quiet:
            print(f"[已保存] JSON 报告 -> {Path(args.save).resolve()}", file=sys.stderr)

    if not statements:
        if not args.quiet:
            print("警告：没有解析到任何 SQL 语句", file=sys.stderr)
        return 1
    return 0


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


def cmd_scan(argv: Sequence[str]) -> int:
    """scan：扫描目录 → 全局血缘图。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli scan",
        description="递归扫描数仓 SQL 目录，构建全局血缘图（跨文件链路自动贯通）",
    )
    ap.add_argument("directory", help="SQL 脚本目录（递归扫描 .sql）")
    ap.add_argument("-d", "--dialect", default=DEFAULT_DIALECT, help=f"sqlglot 方言，默认 {DEFAULT_DIALECT}")
    ap.add_argument("--graph-out", metavar="PATH", default=None, help="全局血缘图 JSON 输出路径")
    ap.add_argument("--report-out", metavar="PATH", default=None, help="扫描报告文本输出路径")
    ap.add_argument("--ignore-dir", action="append", default=[], metavar="NAME",
                    help="额外忽略的目录名（可重复；默认已忽略 __pycache__/.venv/.git 等）")
    ap.add_argument("--max-files", type=int, default=None, help="最多扫描前 N 个文件（调试用）")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    try:
        result = scan_directory(args.directory, dialect=args.dialect, ignore_dirs=args.ignore_dir,
                                max_files=args.max_files)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    text = result.report_text()
    payload = result.to_dict(with_graph=True)

    if args.graph_out:
        Path(args.graph_out).parent.mkdir(parents=True, exist_ok=True)
        result.graph.save(args.graph_out)
        if not args.quiet:
            print(f"[已保存] 全局血缘图 -> {Path(args.graph_out).resolve()} "
                  f"（{result.table_count} 个节点 / {result.edge_count} 条边）", file=sys.stderr)
    if args.report_out:
        Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_out).write_text(text + "\n", encoding="utf-8")
        if not args.quiet:
            print(f"[已保存] 扫描报告 -> {Path(args.report_out).resolve()}", file=sys.stderr)

    code = _emit(payload, args, text)

    if result.failures:
        if not args.quiet:
            print(f"\n警告：{len(result.failures)} 个文件解析失败，详见报告 / JSON 的 failures 字段",
                  file=sys.stderr)
        return 1
    if result.statement_count == 0:
        if not args.quiet:
            print(f"\n警告：目录中没有解析到任何 SQL 语句 -> {args.directory}", file=sys.stderr)
        return 1
    return code


def cmd_upstream(argv: Sequence[str]) -> int:
    """upstream：上游溯源。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli upstream",
        description="上游溯源：这张表的数据从哪来（可限定深度）",
    )
    ap.add_argument("table", help="表名（支持只写表名不带库名）")
    ap.add_argument("--graph", required=True, metavar="PATH", help="血缘图 JSON（scan 产出）")
    ap.add_argument("-n", "--depth", type=int, default=None, help="最大溯源层级，默认不限")
    ap.add_argument("--max-paths", type=int, default=50, help="最多列出的血缘链路条数，默认 50")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    graph = _load_graph(args.graph)
    result = graph.upstream(args.table, depth=args.depth, max_paths=args.max_paths)
    text = format_analysis_text(result, max_paths=args.max_paths)
    code = _emit(result, args, text)
    return code if result.get("found") else 1


def cmd_impact(argv: Sequence[str]) -> int:
    """impact：下游影响分析。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli impact",
        description="下游影响分析：改这张表 / 字段会波及哪些下游表（可限定深度）",
    )
    ap.add_argument("table", help="表名（支持只写表名不带库名）")
    ap.add_argument("--graph", required=True, metavar="PATH", help="血缘图 JSON（scan 产出）")
    ap.add_argument("-n", "--depth", type=int, default=None, help="最大影响层级，默认不限")
    ap.add_argument("--max-paths", type=int, default=50, help="最多列出的血缘链路条数，默认 50")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    graph = _load_graph(args.graph)
    result = graph.downstream(args.table, depth=args.depth, max_paths=args.max_paths)
    text = format_analysis_text(result, max_paths=args.max_paths)
    code = _emit(result, args, text)
    return code if result.get("found") else 1


def cmd_path(argv: Sequence[str]) -> int:
    """path：两表之间的血缘链路。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli path",
        description="查询两张表之间的所有血缘链路（含最短链路）",
    )
    ap.add_argument("source", help="起点表（上游）")
    ap.add_argument("target", help="终点表（下游）")
    ap.add_argument("--graph", required=True, metavar="PATH", help="血缘图 JSON（scan 产出）")
    ap.add_argument("--max-paths", type=int, default=10, help="最多列出的链路条数，默认 10")
    ap.add_argument("--max-length", type=int, default=12, help="单条链路最大跳数，默认 12")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    graph = _load_graph(args.graph)
    result = graph.path_between(args.source, args.target, max_paths=args.max_paths,
                                max_length=args.max_length)
    text = format_path_text(result, max_paths=args.max_paths)
    code = _emit(result, args, text)
    return code if result.get("found") else 1


def cmd_cycle(argv: Sequence[str]) -> int:
    """cycle：环路检测。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli cycle",
        description="循环依赖检测：血缘图里是否存在 A->B->C->A 这类环路",
    )
    ap.add_argument("--graph", required=True, metavar="PATH", help="血缘图 JSON（scan 产出）")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    graph = _load_graph(args.graph)
    cycles = graph.detect_cycles()
    text = format_cycles_text(cycles)
    payload = {"cycle_count": len(cycles), "has_cycle": bool(cycles), "cycles": cycles}
    code = _emit(payload, args, text)
    return 1 if cycles else code


def cmd_stats(argv: Sequence[str]) -> int:
    """stats：图统计。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli stats",
        description="血缘图统计：节点 / 边 / 源表 / 叶子表 / 最大深度 / 分层分布 / 环路",
    )
    ap.add_argument("--graph", required=True, metavar="PATH", help="血缘图 JSON（scan 产出）")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    graph = _load_graph(args.graph)
    stats = graph.stats()
    text = format_stats_text(stats)
    stats_with_nodes = dict(stats)
    stats_with_nodes["nodes"] = graph.to_dict()["nodes"]
    stats_with_nodes["edges"] = [
        {"source": e.source, "target": e.target, "column_mappings": e.column_mappings}
        for e in sorted(graph.edges.values(), key=lambda x: (x.source, x.target))
    ]
    return _emit(stats_with_nodes, args, text)


def cmd_viz(argv: Sequence[str]) -> int:
    """viz：导出 Mermaid / HTML 可视化。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli viz",
        description="血缘图可视化导出：Mermaid 文本（贴 Markdown）或自包含交互式 HTML（离线可开）",
    )
    ap.add_argument("graph_file", help="血缘图 JSON（scan --graph-out 产出）")
    ap.add_argument("-f", "--format", choices=("mermaid", "html"), default="mermaid",
                    help="导出格式：mermaid=文本图（默认），html=交互式单文件 HTML")
    ap.add_argument("--out", metavar="PATH", default=None, help="输出文件路径；省略则打印到标准输出")
    ap.add_argument("--direction", default="LR", help="Mermaid 方向：LR / TD，默认 LR")
    ap.add_argument("--highlight", default=None, metavar="TABLE", help="高亮的中心表（其上游标蓝、下游标橙）")
    ap.add_argument("--depth", type=int, default=None, help="高亮子图的上下游深度限制")
    ap.add_argument("--only-highlight", action="store_true", help="Mermaid 只画高亮子图（裁剪大图）")
    ap.add_argument("--no-group", action="store_true", help="Mermaid 不做分层的 subgraph 分组")
    ap.add_argument("--no-edge-labels", action="store_true", help="Mermaid 边上不显示字段映射条数")
    ap.add_argument("--title", default="数仓血缘图谱", help="HTML 标题")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    graph = _load_graph(args.graph_file)
    try:
        if args.format == "mermaid":
            content = to_mermaid(
                graph,
                direction=args.direction,
                highlight=args.highlight,
                depth=args.depth,
                group_layers=not args.no_group,
                show_edge_labels=not args.no_edge_labels,
                only_highlight=args.only_highlight,
            )
        else:
            content = to_html(graph, title=args.title, center=args.highlight)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        size_kb = out.stat().st_size / 1024
        if args.output == "json":
            print(dumps({"format": args.format, "out": str(out.resolve()),
                         "bytes": out.stat().st_size, "highlight": args.highlight}, indent=args.indent))
        else:
            print(f"[已导出] {args.format} 可视化 -> {out.resolve()}（{size_kb:.1f} KB）")
            if args.format == "html":
                print("        自包含单文件（内联 CSS/JS，无外网依赖），浏览器直接打开即可交互查看")
            else:
                print("        直接把内容贴进 Markdown 的 ```mermaid 代码块即可渲染")
    else:
        print(content)
    if args.save:
        Path(args.save).write_text(dumps({"format": args.format, "bytes": len(content.encode("utf-8"))},
                                         indent=args.indent) + "\n", encoding="utf-8")
    return 0


_SUBCOMMAND_RUNNERS = {
    "scan": cmd_scan,
    "upstream": cmd_upstream,
    "impact": cmd_impact,
    "path": cmd_path,
    "cycle": cmd_cycle,
    "stats": cmd_stats,
    "viz": cmd_viz,
}


# --------------------------------------------------------------------------- #
# P3：DolphinScheduler 子命令
# --------------------------------------------------------------------------- #
DS_SUBCOMMANDS = ("sync", "workflows", "tables", "task", "upstream")

DS_HELP = """python -m lineage.cli ds <子命令> [参数]

DolphinScheduler 旁路集成（只读海豚 OpenAPI，不改海豚一行源码）：
  项目(工程) -> 工作流 -> 任务节点 -> 表 的多层血缘 + 分析。

子命令：
  sync       从海豚 OpenAPI 拉取项目 / 工作流 / 任务定义，解析任务里的 SQL 脚本，
             构建多层血缘并落盘为 ds_lineage.json
  workflows  列出工作流及其依赖分层（读 ds_lineage.json，不需要连海豚）
  tables     某张表被哪些工作流 / 任务加工（反向），并给出「项目->工作流->任务->表」链路
  task       某个工作流下的任务分别读了哪些表、写了哪些表，以及该工作流的上下游依赖
  upstream   某张表的表级上游溯源（复用 P2 图引擎），并标注每张表的调度产出方

示例：
  python -m lineage.cli ds sync --graph-out ds_lineage.json
  python -m lineage.cli ds sync --base-url http://localhost:12345/dolphinscheduler --project 烟草数仓演示
  python -m lineage.cli ds workflows --graph ds_lineage.json
  python -m lineage.cli ds tables ods.ods_卷烟产量流水 --graph ds_lineage.json
  python -m lineage.cli ds task wf_dws_汇总 --graph ds_lineage.json
  python -m lineage.cli ds upstream ads.ads_经营指标驾驶舱 --graph ds_lineage.json --depth 3

环境变量：DS_BASE_URL / DS_USER / DS_PASSWORD / DS_DIALECT
"""

#: ds 子命令默认读取 / 写入的血缘文件
DEFAULT_DS_GRAPH = "ds_lineage.json"


def _load_ds_lineage(path: str) -> DsLineage:
    p = Path(path)
    if not p.exists():
        raise SystemExit(
            f"错误：DolphinScheduler 血缘文件不存在 -> {path}\n"
            f"提示：先跑 `python -m lineage.cli ds sync --graph-out {path}`"
        )
    try:
        return DsLineage.load(p)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"错误：血缘文件格式不正确 -> {path}（{exc}）")


def _ds_client(args: argparse.Namespace) -> DsClient:
    return DsClient(
        base_url=getattr(args, "base_url", None),
        user=getattr(args, "user", None),
        password=getattr(args, "password", None),
        dialect=getattr(args, "dialect", None),
        timeout=getattr(args, "timeout", 30.0),
    )


def cmd_ds_sync(argv: Sequence[str]) -> int:
    """ds sync：从海豚拉取定义 → 解析任务 SQL → 构建多层血缘 → 落盘。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli ds sync",
        description="从 DolphinScheduler OpenAPI 拉取工作流/任务定义，解析任务 SQL 脚本，构建多层血缘",
    )
    ap.add_argument("--base-url", default=None, metavar="URL",
                    help=f"海豚服务地址，默认取 DS_BASE_URL 或 {DsClient().base_url}")
    ap.add_argument("--user", default=None, help="登录用户，默认取 DS_USER（admin）")
    ap.add_argument("--password", default=None, help="登录密码，默认取 DS_PASSWORD")
    ap.add_argument("--project", action="append", default=[], metavar="NAME",
                    help="只同步指定项目名（可重复；默认同步全部项目）")
    ap.add_argument("--project-code", action="append", default=[], metavar="CODE",
                    help="只同步指定项目 code（可重复）")
    ap.add_argument("-d", "--dialect", default=None, help=f"sqlglot 方言，默认 {DEFAULT_DIALECT}")
    ap.add_argument("--graph-out", default=DEFAULT_DS_GRAPH, metavar="PATH",
                    help=f"血缘 JSON 输出路径，默认 {DEFAULT_DS_GRAPH}")
    ap.add_argument("--no-save", action="store_true", help="只在终端输出，不落盘血缘 JSON")
    ap.add_argument("--full-statements", action="store_true",
                    help="JSON 里保留完整字段级血缘（文件会大很多，默认只留条数）")
    ap.add_argument("--timeout", type=float, default=30.0, help="HTTP 超时秒数，默认 30")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    client = _ds_client(args)
    try:
        client.login()
    except DsConnectionError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except DsAuthError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    try:
        builder = DsLineageBuilder(dialect=client.dialect, full_statements=args.full_statements)
        lineage = builder.build_from_client(client, project_names=args.project,
                                            project_codes=args.project_code)
    except DsConnectionError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except DsApiError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    finally:
        client.close()

    text = format_ds_summary_text(lineage)
    payload = lineage.to_dict()

    if not args.no_save and args.graph_out:
        Path(args.graph_out).parent.mkdir(parents=True, exist_ok=True)
        lineage.save(args.graph_out)
        if not args.quiet:
            size_kb = Path(args.graph_out).stat().st_size / 1024
            st = lineage.stats()
            print(f"[已保存] DS 血缘 JSON -> {Path(args.graph_out).resolve()} "
                  f"（{size_kb:.1f} KB：{st['project_count']} 项目 / {st['workflow_count']} 工作流 / "
                  f"{st['task_count']} 任务 / {st['table_count']} 表）", file=sys.stderr)
            print(f"         下一步：python -m lineage.cli ds workflows --graph {args.graph_out}",
                  file=sys.stderr)

    code = _emit(payload, args, text)
    st = lineage.stats()
    if st["workflow_count"] == 0:
        if not args.quiet:
            print("警告：海豚里没有拉到任何工作流（项目为空？先用 scripts/ds_setup_demo.py 建演示数据）",
                  file=sys.stderr)
        return 1
    if st["error_count"]:
        if not args.quiet:
            print(f"\n警告：{st['error_count']} 处任务脚本解析失败，详见 JSON 的 failures 字段",
                  file=sys.stderr)
        return 1
    return code


def cmd_ds_workflows(argv: Sequence[str]) -> int:
    """ds workflows：工作流清单 + 依赖分层。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli ds workflows",
        description="列出 DolphinScheduler 工作流及其表级依赖分层（读 ds_lineage.json，不需要连海豚）",
    )
    ap.add_argument("--graph", default=DEFAULT_DS_GRAPH, metavar="PATH",
                    help=f"ds sync 产出的血缘 JSON，默认 {DEFAULT_DS_GRAPH}")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    lineage = _load_ds_lineage(args.graph)
    result = lineage.workflow_list()
    text = format_workflows_text(result)
    code = _emit(result, args, text)
    return code if result.get("workflow_count") else 1


def cmd_ds_tables(argv: Sequence[str]) -> int:
    """ds tables：某张表被哪些工作流 / 任务加工（反向）。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli ds tables",
        description="反向查询：这张表被哪些调度工作流 / 任务节点加工（含多层血缘链路）",
    )
    ap.add_argument("table", help="表名（支持只写表名不带库名）")
    ap.add_argument("--graph", default=DEFAULT_DS_GRAPH, metavar="PATH", help="ds sync 产出的血缘 JSON")
    ap.add_argument("--max-list", type=int, default=20, help="最多列出的任务条数，默认 20")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    lineage = _load_ds_lineage(args.graph)
    result = lineage.table_tasks(args.table)
    text = format_table_tasks_text(result, max_list=args.max_list)
    code = _emit(result, args, text)
    return code if result.get("found") else 1


def cmd_ds_task(argv: Sequence[str]) -> int:
    """ds task：某个工作流下任务的读 / 写表，以及该工作流的上下游依赖。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli ds task",
        description="查看某个调度工作流下每个任务节点读了哪些表、写了哪些表，以及工作流的上下游依赖",
    )
    ap.add_argument("workflow", help="工作流名（支持唯一子串匹配，如 dws）")
    ap.add_argument("--graph", default=DEFAULT_DS_GRAPH, metavar="PATH", help="ds sync 产出的血缘 JSON")
    ap.add_argument("--max-tasks", type=int, default=30, help="最多列出的任务条数，默认 30")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    lineage = _load_ds_lineage(args.graph)
    result = lineage.workflow_detail(args.workflow)
    text = format_workflow_detail_text(result, max_tasks=args.max_tasks)
    code = _emit(result, args, text)
    return code if result.get("found") else 1


def cmd_ds_upstream(argv: Sequence[str]) -> int:
    """ds upstream：表级上游溯源（复用 P2 图引擎）+ 调度产出方标注。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli ds upstream",
        description="表级上游溯源：这张表的数据在调度侧是怎么一步步加工出来的（复用 P2 图引擎）",
    )
    ap.add_argument("table", help="表名（支持只写表名不带库名）")
    ap.add_argument("--graph", default=DEFAULT_DS_GRAPH, metavar="PATH", help="ds sync 产出的血缘 JSON")
    ap.add_argument("-n", "--depth", type=int, default=None, help="最大溯源层级，默认不限")
    ap.add_argument("--max-paths", type=int, default=5, help="最多列出的链路条数，默认 5")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    lineage = _load_ds_lineage(args.graph)
    result = lineage.upstream_tables(args.table, depth=args.depth, max_paths=args.max_paths)
    text = format_ds_upstream_text(result, max_paths=args.max_paths)
    code = _emit(result, args, text)
    return code if result.get("found") else 1


_DS_RUNNERS = {
    "sync": cmd_ds_sync,
    "workflows": cmd_ds_workflows,
    "tables": cmd_ds_tables,
    "task": cmd_ds_task,
    "upstream": cmd_ds_upstream,
}


def cmd_ds(argv: Sequence[str]) -> int:
    """ds 子命令分发（``python -m lineage.cli ds <子命令>``）。"""
    args = list(argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(DS_HELP)
        return 0
    sub = args[0]
    if sub not in _DS_RUNNERS:
        print(f"错误：未知的 ds 子命令 `{sub}`\n", file=sys.stderr)
        print(DS_HELP, file=sys.stderr)
        return 2
    return _DS_RUNNERS[sub](args[1:])


#: ds 子命令在 :data:`_SUBCOMMAND_RUNNERS` 里注册（函数定义顺序所限，这里回填）
_SUBCOMMAND_RUNNERS["ds"] = cmd_ds


# --------------------------------------------------------------------------- #
# P4：业务口径知识库子命令
# --------------------------------------------------------------------------- #
KB_SUBCOMMANDS = ("build", "summary", "search", "show", "ask", "export", "terms", "fields")

KB_HELP = """python -m lineage.cli kb <子命令> [参数]

业务口径知识库（P4）：把数仓 SQL 脚本里的业务知识提炼成可检索的知识库（SQLite）。

子命令：
  build      扫描 SQL 目录 → 提炼指标口径 / 字段术语 / 业务规则 → 写入 SQLite
  summary    知识库概览（口径条数、分层/类型分布、口径最多的表）
  search     关键词 / 模糊检索（按表名、字段名、中文名、口径关键词）
  show       某个指标口径详情：公式 + 依赖字段 + 来源脚本 + 血缘链路
  ask        自然语言问数：口径怎么算 / 哪些表用到某字段 / 某表从哪来 / 有哪些指标
  export     导出《业务口径知识库.md》（也可导出整库 JSON）
  terms      业务术语词典（--pending 只看待确认）
  fields     某张表的字段清单（含中文业务名与名来源）

示例：
  python -m lineage.cli kb build
  python -m lineage.cli kb build examples/warehouse --db data/knowledge.db --md-out docs/业务口径知识库.md
  python -m lineage.cli kb summary
  python -m lineage.cli kb search 产量
  python -m lineage.cli kb show chanliang_qty
  python -m lineage.cli kb ask "产量怎么算的"
  python -m lineage.cli kb ask "哪些表用到了打码量"
  python -m lineage.cli kb export --md docs/业务口径知识库.md

环境变量：KB_DB（库路径）/ KB_GLOSSARY（自定义词典）/ LLM_API_KEY、LLM_BASE_URL、LLM_MODEL（可选智能问答）
"""

#: 默认导出的 Markdown 知识文档路径（相对项目根）
DEFAULT_KB_DOC = "docs/业务口径知识库.md"


def _kb_store(args: argparse.Namespace):
    """按 --db 打开知识库（默认 data/knowledge.db，环境变量 KB_DB 可覆盖）。"""
    from .knowledge import KnowledgeStore, default_db_path

    path = Path(args.db) if getattr(args, "db", None) else default_db_path()
    if not path.exists() and not getattr(args, "allow_missing", False):
        raise SystemExit(
            f"错误：知识库不存在 -> {path}\n"
            f"提示：先跑 `python -m lineage.cli kb build` 建库"
        )
    return KnowledgeStore(path)


def cmd_kb_build(argv: Sequence[str]) -> int:
    """kb build：扫描 SQL 目录 → 提炼口径 → 建库。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli kb build",
        description="扫描数仓 SQL 目录，提炼业务口径 / 字段术语 / 业务规则，写入 SQLite 知识库",
    )
    ap.add_argument("directories", nargs="*", metavar="DIR",
                    help="SQL 脚本目录（可多个；默认 examples/warehouse + examples/knowledge_demo）")
    ap.add_argument("--db", metavar="PATH", default=None,
                    help="知识库文件路径，默认 data/knowledge.db（或环境变量 KB_DB）")
    ap.add_argument("-d", "--dialect", default=DEFAULT_DIALECT, help=f"sqlglot 方言，默认 {DEFAULT_DIALECT}")
    ap.add_argument("--glossary", metavar="PATH", default=None,
                    help="叠加自定义词典 JSON（字段/词根中文名），可重复维护")
    ap.add_argument("--incremental", action="store_true",
                    help="增量模式：按脚本粒度替换（默认全量 rebuild，幂等）")
    ap.add_argument("--md-out", metavar="PATH", default=None,
                    help=f"建库后顺便导出 Markdown 知识文档，默认不导出（可传 {DEFAULT_KB_DOC}）")
    ap.add_argument("--doc-title", default="业务口径知识库", help="Markdown 文档标题")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    from .knowledge import build_knowledge_base, format_build_text

    try:
        report = build_knowledge_base(
            dirs=args.directories or None,
            db_path=Path(args.db) if args.db else None,
            dialect=args.dialect,
            glossary_path=args.glossary,
            mode="incremental" if args.incremental else "rebuild",
            doc_path=args.md_out,
            doc_title=args.doc_title,
        )
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    text = format_build_text(report)
    code = _emit(report.to_dict(), args, text)
    if report.failures:
        if not args.quiet:
            print(f"警告：{len(report.failures)} 个文件解析失败", file=sys.stderr)
        return 1
    if not report.counts.get("kb_metrics"):
        if not args.quiet:
            print("警告：没有提炼到任何指标口径（目录里没有加工逻辑？）", file=sys.stderr)
        return 1
    return code


def cmd_kb_summary(argv: Sequence[str]) -> int:
    """kb summary：知识库概览。"""
    ap = argparse.ArgumentParser(prog="python -m lineage.cli kb summary",
                                description="业务口径知识库概览")
    ap.add_argument("--db", default=None, metavar="PATH", help="知识库文件路径")
    ap.add_argument("--allow-missing", action="store_true", help="库不存在时也打开（空库）")
    ap.add_argument("--top", type=int, default=12, help="展示口径最多的前 N 张表，默认 12")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    store = _kb_store(args)
    try:
        summary = store.summary(limit_tables=args.top)
    finally:
        store.close()

    c = summary["counts"]
    lines = ["=" * 72, "业务口径知识库概览", "=" * 72]
    lines.append(f"库文件：{summary['db']}（结构版本 {summary['schema_version']}）")
    lines.append(f"最近建库：{summary.get('built_at') or '—'}")
    lines.append("-" * 72)
    lines.append(f"指标口径 {c['kb_metrics']} 条 / 字段 {c['kb_fields']} 个"
                 f"（中文化 {c['fields_with_chinese']}）/ 表 {c['kb_tables']} 张")
    lines.append(f"业务术语 {c['kb_terms']} 条（待确认 {c['pending_terms']}）/ "
                 f"业务规则 {c['kb_rules']} 条 / 脚本 {c['kb_scripts']} 个 / 表级血缘 {c['kb_table_lineage']} 条")
    lines.append("口径类型分布：" + "  ".join(f"{k}={v}" for k, v in summary["metric_by_type"].items()))
    lines.append("口径分层分布：" + "  ".join(f"{k}={v}" for k, v in summary["metric_by_layer"].items()))
    lines.append("术语来源分布：" + "  ".join(f"{k}={v}" for k, v in summary["term_by_source"].items()))
    lines.append("-" * 72)
    lines.append("口径最多的表：")
    for row in summary["top_tables"][: args.top]:
        if not row["metric_count"]:
            continue
        lines.append(f"  {row['name']:<32} {row['chinese'] or '待确认':<16} "
                     f"[{row['layer']}] {row['metric_count']} 条口径")
    lines.append("=" * 72)
    return _emit(summary, args, "\n".join(lines))


def cmd_kb_search(argv: Sequence[str]) -> int:
    """kb search：关键词检索。"""
    ap = argparse.ArgumentParser(prog="python -m lineage.cli kb search",
                                description="知识库检索：表名 / 字段名 / 中文业务名 / 口径关键词")
    ap.add_argument("query", help="检索关键词，如 产量 / 打码量 / 不良率 / dws_税利汇总")
    ap.add_argument("--db", default=None, metavar="PATH", help="知识库文件路径")
    ap.add_argument("--kind", default=None, metavar="KINDS",
                    help="只检索指定类别，逗号分隔：metrics,fields,tables,terms,rules")
    ap.add_argument("--limit", type=int, default=8, help="每类最多返回条数，默认 8")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    from .knowledge import format_search_text, search

    kinds = [k.strip() for k in args.kind.split(",")] if args.kind else None
    store = _kb_store(args)
    try:
        result = search(store, args.query, kinds=kinds, limit=args.limit)
        text = format_search_text(result, limit_per_group=args.limit)
    finally:
        store.close()
    code = _emit(result, args, text)
    return code if result["total"] else 1


def cmd_kb_show(argv: Sequence[str]) -> int:
    """kb show：指标口径详情。"""
    ap = argparse.ArgumentParser(prog="python -m lineage.cli kb show",
                                description="指标口径详情：公式 / 依赖字段 / 来源脚本 / 血缘链路")
    ap.add_argument("metric", help="指标名或中文业务名，如 chanliang_qty / 产量 / 不良品率")
    ap.add_argument("--db", default=None, metavar="PATH", help="知识库文件路径")
    ap.add_argument("--limit", type=int, default=5, help="最多展示几条同名口径，默认 5")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    from .knowledge import format_metric

    store = _kb_store(args)
    try:
        rows = store.get_metric(args.metric)
        if not rows:
            store.close()
            print(f"知识库里没有指标「{args.metric}」。\n"
                  f"提示：`python -m lineage.cli kb search {args.metric}` 先模糊找一下；"
                  f"`kb summary` 可以看库里有哪些指标。", file=sys.stderr)
            return 1
        lines = ["=" * 72,
                 f"指标口径：{args.metric}（命中 {len(rows)} 条）",
                 "=" * 72]
        payload = {"metric": args.metric, "count": len(rows), "metrics": rows[: args.limit]}
        for idx, row in enumerate(rows[: args.limit], start=1):
            lines.append(f"[{idx}] " + format_metric(store, row))
            lines.append("-" * 72)
        lines.append("检索：kb search " + (rows[0].get("metric_name") or args.metric)
                     + "  /  kb ask \"" + (rows[0].get("chinese_name") or args.metric) + "怎么算的\"")
        lines.append("=" * 72)
    finally:
        store.close()
    return _emit(payload, args, "\n".join(lines))


def cmd_kb_ask(argv: Sequence[str]) -> int:
    """kb ask：自然语言问数（规则模式 + 可选 LLM）。"""
    ap = argparse.ArgumentParser(prog="python -m lineage.cli kb ask",
                                description="自然语言问数：口径怎么算 / 哪些表用到某字段 / 某表从哪来")
    ap.add_argument("question", help='问题，如 "产量怎么算的"、"哪些表用到了打码量"')
    ap.add_argument("--db", default=None, metavar="PATH", help="知识库文件路径")
    ap.add_argument("--use-llm", choices=("auto", "on", "off"), default="auto",
                    help="是否用 LLM 润色答案：auto=有 LLM_API_KEY 才用（默认），on=强制，off=纯规则")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    from .knowledge import answer

    store = _kb_store(args)
    try:
        result = answer(store, args.question, use_llm={"on": True, "off": False}.get(args.use_llm, "auto"))
    finally:
        store.close()

    lines = ["=" * 72,
             f"问题：{result['question']}",
             f"意图：{result['intent_label']}（{result['intent']}）"
             f"    实体：{result['entity'] or '(未识别)'}"
             f"    模式：{result['mode']}",
             "=" * 72,
             result["answer"],
             "=" * 72]
    if not result["llm"]["enabled"]:
        lines.append("提示：未配置 LLM_API_KEY，当前为规则模式（离线可用）；"
                     "配置 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL 可启用智能润色。")
    return _emit(result, args, "\n".join(lines))


def cmd_kb_export(argv: Sequence[str]) -> int:
    """kb export：导出 Markdown 知识文档 / JSON。"""
    ap = argparse.ArgumentParser(prog="python -m lineage.cli kb export",
                                description="导出《业务口径知识库.md》或整库 JSON")
    ap.add_argument("--db", default=None, metavar="PATH", help="知识库文件路径")
    ap.add_argument("--md", metavar="PATH", nargs="?", const=DEFAULT_KB_DOC, default=None,
                    help=f"导出 Markdown 文档（省略路径则用 {DEFAULT_KB_DOC}）")
    ap.add_argument("--json", dest="json_out", metavar="PATH", default=None,
                    help="导出整库 JSON（供外部系统消费）")
    ap.add_argument("--title", default="业务口径知识库", help="Markdown 文档标题")
    ap.add_argument("--no-detail", action="store_true", help="Markdown 只导出总览，不导出逐条明细")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    from .knowledge import export_markdown

    if not args.md and not args.json_out:
        args.md = DEFAULT_KB_DOC

    store = _kb_store(args)
    payload: Dict[str, Any] = {}
    messages: List[str] = []
    try:
        if args.md:
            info = export_markdown(store, args.md, title=args.title, with_detail=not args.no_detail)
            payload["markdown"] = info
            messages.append(f"[已导出] Markdown 知识文档 -> {info['path']}"
                            f"（{info['bytes'] / 1024:.1f} KB / {info['lines']} 行）")
        if args.json_out:
            data = store.export_json()
            Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_out).write_text(dumps(data, indent=args.indent) + "\n", encoding="utf-8")
            payload["json"] = {"path": str(Path(args.json_out).resolve()),
                               "bytes": Path(args.json_out).stat().st_size}
            messages.append(f"[已导出] 知识库 JSON -> {Path(args.json_out).resolve()}")
        payload["summary"] = store.summary()
    finally:
        store.close()
    if args.output == "json":
        return _emit(payload, args, "")
    print("\n".join(messages))
    c = payload["summary"]["counts"]
    print(f"         内容：{c['kb_metrics']} 条口径 / {c['kb_fields']} 个字段 / "
          f"{c['kb_terms']} 条术语 / {c['kb_rules']} 条规则")
    if args.save:
        Path(args.save).write_text(dumps(payload, indent=args.indent) + "\n", encoding="utf-8")
    return 0


def cmd_kb_terms(argv: Sequence[str]) -> int:
    """kb terms：业务术语词典。"""
    ap = argparse.ArgumentParser(prog="python -m lineage.cli kb terms",
                                description="业务术语词典：字段/表 中文业务名与来源")
    ap.add_argument("--db", default=None, metavar="PATH", help="知识库文件路径")
    ap.add_argument("--pending", action="store_true", help="只看待确认（无中文名）的术语")
    ap.add_argument("--source", default=None,
                    help="只看指定来源：builtin/comment/rule/rule_partial/pending"
                         "（术语级合并后的来源；字段级来源用 `kb fields <表>` 看）")
    ap.add_argument("--limit", type=int, default=50, help="最多列出条数，默认 50")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    store = _kb_store(args)
    try:
        terms = store.terms(only_pending=args.pending)
        if args.source:
            terms = [t for t in terms if t.get("source") == args.source]
    finally:
        store.close()

    lines = ["=" * 72, f"业务术语词典（{len(terms)} 条）", "=" * 72,
             f"{'术语':<26}{'中文业务名':<18}{'来源':<14}{'置信度':<8}出现次数"]
    lines.append("-" * 72)
    for term in terms[: args.limit]:
        lines.append(f"{term['term']:<26}{(term.get('chinese_name') or '(待确认)'):<18}"
                     f"{term.get('source'):<14}{str(term.get('confidence')):<8}{term.get('occurrences')}")
    if len(terms) > args.limit:
        lines.append(f"...（其余 {len(terms) - args.limit} 条省略，用 --limit 调整）")
    lines.append("=" * 72)
    return _emit({"count": len(terms), "terms": terms}, args, "\n".join(lines))


def cmd_kb_fields(argv: Sequence[str]) -> int:
    """kb fields：某张表的字段清单。"""
    ap = argparse.ArgumentParser(prog="python -m lineage.cli kb fields",
                                description="某张表的字段清单（含中文业务名 / 名来源 / 角色）")
    ap.add_argument("table", help="表名（支持只写表名不带库名）")
    ap.add_argument("--db", default=None, metavar="PATH", help="知识库文件路径")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    store = _kb_store(args)
    try:
        table = store.get_table(args.table)
        if not table:
            store.close()
            print(f"知识库里没有表「{args.table}」（`kb search {args.table}` 试试）", file=sys.stderr)
            return 1
        fields = store.fields(table["table_name"])
        metrics = store.metrics(table["table_name"])
    finally:
        store.close()

    lines = ["=" * 72,
             f"表 {table['table_name']}（{table.get('chinese_name') or '待确认'}，{table.get('layer')} 层）",
             "=" * 72]
    if table.get("business_desc"):
        lines.append(f"脚本说明：{table['business_desc']}")
    lines.append(f"字段 {len(fields)} 个 / 口径 {len(metrics)} 条")
    lines.append("-" * 72)
    lines.append(f"{'字段':<24}{'中文业务名':<20}{'来源':<14}{'角色':<10}单位")
    for field in fields:
        lines.append(f"{field['column_name']:<24}{(field.get('chinese_name') or '(待确认)'):<20}"
                     f"{field.get('chinese_source'):<14}{field.get('role'):<10}{field.get('unit') or '-'}")
    if metrics:
        lines.append("-" * 72)
        lines.append("该表的指标口径：")
        for metric in metrics:
            lines.append(f"  - {metric.get('formula')}   [{metric.get('metric_type')}]")
    lines.append("=" * 72)
    return _emit({"table": table, "field_count": len(fields), "fields": fields,
                  "metrics": metrics}, args, "\n".join(lines))


_KB_RUNNERS = {
    "build": cmd_kb_build,
    "summary": cmd_kb_summary,
    "search": cmd_kb_search,
    "show": cmd_kb_show,
    "ask": cmd_kb_ask,
    "export": cmd_kb_export,
    "terms": cmd_kb_terms,
    "fields": cmd_kb_fields,
}


def cmd_kb(argv: Sequence[str]) -> int:
    """kb 子命令分发（``python -m lineage.cli kb <子命令>``）。"""
    args = list(argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(KB_HELP)
        return 0
    sub = args[0]
    if sub not in _KB_RUNNERS:
        print(f"错误：未知的 kb 子命令 `{sub}`\n", file=sys.stderr)
        print(KB_HELP, file=sys.stderr)
        return 2
    return _KB_RUNNERS[sub](args[1:])


_SUBCOMMAND_RUNNERS["kb"] = cmd_kb


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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
