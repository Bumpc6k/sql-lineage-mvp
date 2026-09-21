# -*- coding: utf-8 -*-
"""DolphinScheduler 子命令：ds sync / workflows / tables / task / upstream。"""

from __future__ import annotations

import argparse
import json
import sys

from lineage_core.parser import DEFAULT_DIALECT
from lineage.ds.client import DsApiError, DsAuthError, DsClient, DsConnectionError
from lineage.ds.lineage import DsLineage, DsLineageBuilder, format_ds_summary_text, format_ds_upstream_text, format_table_tasks_text, format_workflow_detail_text, format_workflows_text
from pathlib import Path
from typing import Sequence

from lineage.cli.common import _add_common, _emit
from lineage.cli.graph_cmds import _SUBCOMMAND_RUNNERS

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
            print("警告：海豚里没有拉到任何工作流（项目为空？先用 demos/ds_setup_demo.py 建演示数据）",
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

__all__ = ['DS_SUBCOMMANDS', 'DS_HELP', 'DEFAULT_DS_GRAPH', '_load_ds_lineage', '_ds_client', 'cmd_ds_sync', 'cmd_ds_workflows', 'cmd_ds_tables', 'cmd_ds_task', 'cmd_ds_upstream', '_DS_RUNNERS', 'cmd_ds']  # 本模块自有名字（供 lineage/cli/__init__.py 兼容导出）
