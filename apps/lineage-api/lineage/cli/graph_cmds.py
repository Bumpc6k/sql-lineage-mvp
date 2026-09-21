# -*- coding: utf-8 -*-
"""图相关子命令：scan / upstream / impact / path / cycle / stats / viz / parse-script。"""

from __future__ import annotations

import argparse
import sys

from lineage.collect.scan import scan_directory
from lineage_core.graph import format_analysis_text, format_cycles_text, format_path_text, format_stats_text
from lineage_core.parser import DEFAULT_DIALECT, SqlLineageParser, dumps
from lineage_core.script_parser import parse_script_file, script_summary_text
from lineage.render.viz import to_html, to_mermaid
from pathlib import Path
from typing import Any, Dict, List, Sequence

from lineage.cli.common import STDIN_SENTINEL, _add_common, _emit, _load_graph, build_arg_parser

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

def cmd_parse_script(argv: Sequence[str]) -> int:
    """parse-script：从 Shell / Python 脚本里提取内嵌 SQL 并解析血缘。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli parse-script",
        description="从 Shell（hive -e/-f、beeline、spark-sql、impala-shell、heredoc、SQL=\"...\"）"
                    "与 Python/PySpark（spark.sql、pd.read_sql、sqlalchemy.text）脚本中"
                    "提取内嵌 SQL 并解析表级 / 字段级血缘",
    )
    ap.add_argument("files", nargs="+", metavar="FILE", help="脚本文件（.sh / .py / .sql）")
    ap.add_argument("-d", "--dialect", default=DEFAULT_DIALECT,
                    help=f"sqlglot 方言，默认 {DEFAULT_DIALECT}")
    ap.add_argument("--kind", choices=("auto", "sql", "shell", "python"), default="auto",
                    help="强制脚本类型；默认 auto=按内容自动判定")
    ap.add_argument("--task-type", default=None, metavar="TYPE",
                    help="海豚任务类型（SQL / SHELL / PYTHON），作为类型判定的兜底依据")
    ap.add_argument("--resolve-files", action="store_true",
                    help="尝试读取 `hive -f xxx.sql` 引用的本地 SQL 文件（默认只记未解析提示）")
    _add_common(ap)
    args = ap.parse_args(list(argv))

    kind = None if args.kind == "auto" else args.kind
    results: List[Dict[str, Any]] = []
    texts: List[str] = []
    for name in args.files:
        p = Path(name)
        if not p.exists():
            print(f"错误：文件不存在 -> {name}", file=sys.stderr)
            return 2
        res = parse_script_file(p, kind, args.dialect, task_type=args.task_type,
                                resolve_files=args.resolve_files)
        results.append(res)
        texts.append(script_summary_text(res))

    payload: Dict[str, Any] = results[0] if len(results) == 1 else {
        "file_count": len(results), "scripts": results,
    }
    return _emit(payload, args, "\n".join(texts))

_SUBCOMMAND_RUNNERS = {
    "scan": cmd_scan,
    "upstream": cmd_upstream,
    "impact": cmd_impact,
    "path": cmd_path,
    "cycle": cmd_cycle,
    "stats": cmd_stats,
    "viz": cmd_viz,
    "parse-script": cmd_parse_script,
}

__all__ = ['_load_sources', 'run_parse', 'cmd_scan', 'cmd_upstream', 'cmd_impact', 'cmd_path', 'cmd_cycle', 'cmd_stats', 'cmd_viz', 'cmd_parse_script', '_SUBCOMMAND_RUNNERS']  # 本模块自有名字（供 lineage/cli/__init__.py 兼容导出）
