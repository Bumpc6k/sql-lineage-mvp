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

#: P2/P3 子命令清单（argv[0] 命中即走子命令分支，否则走 P1 的旧解析流程）
SUBCOMMANDS = ("scan", "upstream", "impact", "path", "cycle", "stats", "viz", "ds")


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
