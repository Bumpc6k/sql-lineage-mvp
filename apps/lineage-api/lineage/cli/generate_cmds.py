# -*- coding: utf-8 -*-
"""生成引擎子命令：generate sql / pipeline / apply / validate。"""

from __future__ import annotations

import argparse
import json
import sys

from lineage_core.parser import DEFAULT_DIALECT, dumps
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from lineage.cli.common import _add_common, _emit
from lineage.cli.graph_cmds import _SUBCOMMAND_RUNNERS

# --------------------------------------------------------------------------- #
# P7：生成引擎子命令（generate sql / pipeline / apply / validate）
# --------------------------------------------------------------------------- #
GENERATE_HELP = """python -m lineage.cli generate <子命令> [参数]

生成引擎（P7）：从业务需求生成加工 SQL / 分层链路 / 海豚工作流，并自校验。
「宁少勿假」：每一列都能追溯到知识库口径或存量脚本字段血缘，推导出来的部分一律进 warnings。

子命令：
  sql        L1 单表加工 SQL 生成（知识库口径 → INSERT OVERWRITE ... SELECT + 语法自检）
  pipeline   L2 分层链路生成（需求关键词 → ods→dwd→dws→ads 多段 SQL + 链路图）
  apply      L3 一键落地 DolphinScheduler（默认只出工作流 JSON；--apply 才真调海豚 API）
  validate   L4 反向校验（生成 SQL 过血缘引擎 → 断链/孤岛/环路/跨层直连/口径一致性 + HTML 报告）

示例：
  python -m lineage.cli generate sql \\
      --source ods.ods_卷烟产量流水 --target cdw.dwd_卷烟产量明细 \\
      --metric 产量 --group-by plant_code --partition dt --json

  python -m lineage.cli generate pipeline --requirement "生成产销存月报" \\
      --target-layer ads --max-stages 4 --json --save pipeline.json

  python -m lineage.cli generate apply --pipeline-file pipeline.json \\
      --workflow-name wf_gen_产销存月报 --project-code 123 --json            # 只出 JSON
  python -m lineage.cli generate apply --pipeline-file pipeline.json \\
      --workflow-name wf_gen_产销存月报 --project-code 123 --apply --json     # 真创建

  python -m lineage.cli generate validate --pipeline-file pipeline.json --json

公共开关：--json（等价 --output json）/ --save PATH / --db PATH / --graph PATH / --no-llm
"""

GENERATE_SUBCOMMANDS = ("sql", "pipeline", "apply", "validate")

def _add_generate_common(ap: argparse.ArgumentParser) -> None:
    """生成引擎子命令的公共参数。"""
    ap.add_argument("--db", default=None, metavar="PATH", help="知识库文件路径（默认 data/knowledge.db）")
    ap.add_argument("--graph", default=None, metavar="PATH",
                    help="血缘图 JSON（默认 warehouse_graph.json；用于复用存量字段血缘）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（等价 --output json）")
    ap.add_argument("--no-llm", action="store_true",
                    help="禁用可插拔 LLM（纯模板，离线可用；不配 LLM_API_KEY 时本来就走模板）")
    _add_common(ap)

def _finish_generate_args(args: argparse.Namespace) -> argparse.Namespace:
    if getattr(args, "json", False):
        args.output = "json"
    return args

def cmd_generate_sql(argv: Sequence[str]) -> int:
    """generate sql：L1 单表加工 SQL 生成。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli generate sql",
        description="L1 单表加工 SQL 生成：知识库口径 + 字段中文名 → 可直接跑的 INSERT OVERWRITE ... SELECT",
    )
    ap.add_argument("--source", action="append", required=True, metavar="TABLE",
                    help="源表（可重复；支持只写表名）")
    ap.add_argument("--target", required=True, metavar="TABLE", help="目标表")
    ap.add_argument("--metric", action="append", default=[], metavar="NAME",
                    help="指标（中文业务名或列名，可重复；从知识库取口径公式）")
    ap.add_argument("--group-by", action="append", default=[], metavar="COL",
                    help="分组维度列（可重复；给了就按维度聚合）")
    ap.add_argument("--partition", default=None, metavar="COL", help="分区字段（如 dt）")
    ap.add_argument("--date", default=None, help="分区值，默认 ${bizdate}（调度参数占位）")
    ap.add_argument("--where", default=None, help="额外过滤条件（原样拼进 WHERE）")
    ap.add_argument("--all-columns", action="store_true",
                    help="按存量血缘补齐目标表其余列（Hive 按位置写入时列数要对齐）")
    ap.add_argument("--dialect", default=DEFAULT_DIALECT, help=f"sqlglot 方言，默认 {DEFAULT_DIALECT}")
    _add_generate_common(ap)
    args = _finish_generate_args(ap.parse_args(list(argv)))

    from lineage.generate import format_sql_text, generate_sql

    payload = {
        "source_tables": args.source, "target_table": args.target, "metrics": args.metric,
        "group_by": args.group_by, "partition_field": args.partition, "dialect": args.dialect,
        "db": args.db, "graph": args.graph, "where": args.where, "all_columns": args.all_columns,
        "use_llm": "off" if args.no_llm else "auto",
    }
    if args.date:
        payload["date"] = args.date
    result = generate_sql(payload)
    text = format_sql_text(result) if result.get("success") else \
        f"生成失败：{result.get('error')}" + (f"\n提示：{result.get('hint')}" if result.get("hint") else "")
    code = _emit(result, args, text)
    return code if result.get("success") else 1

def cmd_generate_pipeline(argv: Sequence[str]) -> int:
    """generate pipeline：L2 分层链路生成。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli generate pipeline",
        description="L2 分层链路生成：需求关键词 → 目标层表 → 逐层多段 INSERT SQL + 链路图",
    )
    ap.add_argument("--requirement", "-r", default="", help="业务需求，如 生成产销存月报")
    ap.add_argument("--target-table", default=None, metavar="TABLE", help="直接指定目标表（跳过需求匹配）")
    ap.add_argument("--target-layer", default="ads", help="目标分层，默认 ads")
    ap.add_argument("--max-stages", type=int, default=4, help="最多生成几段，默认 4")
    ap.add_argument("--date", default=None, help="分区值，默认 ${bizdate}")
    ap.add_argument("--dialect", default=DEFAULT_DIALECT, help=f"sqlglot 方言，默认 {DEFAULT_DIALECT}")
    _add_generate_common(ap)
    args = _finish_generate_args(ap.parse_args(list(argv)))

    from lineage.generate import format_pipeline_text, generate_pipeline

    payload = {
        "requirement": args.requirement, "target_table": args.target_table,
        "target_layer": args.target_layer, "max_stages": args.max_stages,
        "dialect": args.dialect, "db": args.db, "graph": args.graph,
        "use_llm": "off" if args.no_llm else "auto",
    }
    if args.date:
        payload["date"] = args.date
    result = generate_pipeline(payload)
    text = format_pipeline_text(result) if result.get("success") else \
        f"生成失败：{result.get('error')}" + (f"\n提示：{result.get('hint')}" if result.get("hint") else "")
    code = _emit(result, args, text)
    return code if result.get("success") else 1

def _load_pipeline_arg(args: argparse.Namespace) -> Tuple[Optional[dict], str]:
    """从 --pipeline-file 读 L2 结果 JSON（也接受直接给 stages 数组 / 只有 sql 的对象）。"""
    if not args.pipeline_file:
        return None, ""
    path = Path(args.pipeline_file)
    if not path.exists():
        return None, f"错误：文件不存在 -> {args.pipeline_file}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"错误：读取 {args.pipeline_file} 失败（{exc}）"
    if isinstance(data, list):
        return {"stages": data}, ""
    if isinstance(data, dict) and (data.get("stages") or data.get("pipeline")):
        return data, ""
    return None, f"错误：{args.pipeline_file} 里既没有 stages 也没有 pipeline 字段"

def cmd_generate_apply(argv: Sequence[str]) -> int:
    """generate apply：L3 落地 DolphinScheduler。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli generate apply",
        description="L3 一键落地 DolphinScheduler：链路 → 任务定义 + 依赖 + locations"
                    "（默认只出 JSON；--apply 才真调海豚 API 创建）",
    )
    ap.add_argument("--pipeline-file", default=None, metavar="PATH",
                    help="L2 的 JSON 输出（generate pipeline --save 的文件）")
    ap.add_argument("--requirement", "-r", default="", help="也可以直接给需求，内部先跑一遍 L2")
    ap.add_argument("--target-layer", default="ads", help="需求模式下的目标分层，默认 ads")
    ap.add_argument("--max-stages", type=int, default=4, help="需求模式下的最大段数，默认 4")
    ap.add_argument("--workflow-name", default=None, help="工作流名，默认 wf_gen_<需求>")
    ap.add_argument("--project-code", type=int, default=0, help="海豚项目 code（--apply 时用）")
    ap.add_argument("--project-name", default=None, help="海豚项目名（替代 project-code）")
    ap.add_argument("--datasource-id", type=int, default=None, help="SQL 任务绑定的数据源 id")
    ap.add_argument("--env", default="hive", help="SQL 任务的数据源类型，默认 hive")
    ap.add_argument("--description", default=None, help="工作流描述")
    ap.add_argument("--apply", dest="do_apply", action="store_true",
                    help="真的调海豚 OpenAPI 创建（默认不创建，只返回 JSON 供人工评审）")
    ap.add_argument("--base-url", default=None, help="海豚地址，默认 DS_BASE_URL 或 localhost:12345")
    ap.add_argument("--user", default=None, help="海豚用户名，默认 DS_USER（admin）")
    ap.add_argument("--password", default=None, help="海豚密码，默认 DS_PASSWORD")
    _add_generate_common(ap)
    args = _finish_generate_args(ap.parse_args(list(argv)))

    from lineage.generate import apply_pipeline, format_apply_text, generate_pipeline

    pipeline, err = _load_pipeline_arg(args)
    if err:
        print(err, file=sys.stderr)
        return 2
    if pipeline is None:
        if not args.requirement:
            print("错误：请用 --pipeline-file 给 L2 结果，或用 --requirement 直接描述需求",
                  file=sys.stderr)
            return 2
        pipeline = generate_pipeline({
            "requirement": args.requirement, "target_layer": args.target_layer,
            "max_stages": args.max_stages, "db": args.db, "graph": args.graph,
            "use_llm": "off" if args.no_llm else "auto",
        })
        if not pipeline.get("success"):
            print(f"错误：先生成链路失败 -> {pipeline.get('error')}", file=sys.stderr)
            return 1
        if not args.quiet:
            print(f"[提示] 已先按需求生成 {len(pipeline.get('stages') or [])} 段链路",
                  file=sys.stderr)

    payload = {
        "pipeline": pipeline, "workflow_name": args.workflow_name,
        "project_code": args.project_code, "project_name": args.project_name,
        "create_workflow": bool(args.do_apply), "env": args.env,
        "datasource_id": args.datasource_id, "description": args.description,
        "base_url": args.base_url, "user": args.user, "password": args.password,
    }
    result = apply_pipeline(payload)
    text = format_apply_text(result)
    code = _emit(result, args, text)
    return code if result.get("success") else 1

def cmd_generate_validate(argv: Sequence[str]) -> int:
    """generate validate：L4 反向校验。"""
    ap = argparse.ArgumentParser(
        prog="python -m lineage.cli generate validate",
        description="L4 反向校验：生成的 SQL 过血缘引擎 → 合并链路 → 体检（断链/孤岛/环路/"
                    "跨层直连/口径一致性/与血缘图对比）+ HTML 报告",
    )
    ap.add_argument("--pipeline-file", default=None, metavar="PATH", help="L2 的 JSON 输出")
    ap.add_argument("--sql-file", default=None, metavar="PATH", help="单个 SQL 文件（或 stages JSON）")
    ap.add_argument("--sql", action="append", default=[], metavar="SQL", help="直接给 SQL 文本（可重复）")
    ap.add_argument("--name", default=None, help="报告标题里的链路名")
    ap.add_argument("--workflow-name", default=None, help="已在海豚创建的工作流名（写进报告）")
    ap.add_argument("--project-code", type=int, default=0, help="海豚项目 code（写进报告）")
    ap.add_argument("--no-report", action="store_true", help="不落 HTML 报告")
    ap.add_argument("--reports-dir", default=None, metavar="PATH", help="报告落盘目录（测试用）")
    ap.add_argument("--dialect", default=DEFAULT_DIALECT, help=f"sqlglot 方言，默认 {DEFAULT_DIALECT}")
    _add_generate_common(ap)
    args = _finish_generate_args(ap.parse_args(list(argv)))

    from lineage.generate import format_validate_text, validate_generation

    payload: Dict[str, Any] = {
        "db": args.db, "graph": args.graph, "dialect": args.dialect,
        "make_report": not args.no_report, "reports_dir": args.reports_dir,
        "name": args.name, "workflow_name": args.workflow_name,
        "project_code": args.project_code, "sql_list": args.sql,
    }
    if args.pipeline_file:
        data, err = _load_pipeline_arg(argparse.Namespace(pipeline_file=args.pipeline_file))
        if err:
            print(err, file=sys.stderr)
            return 2
        payload["stages"] = data.get("stages") or (data.get("pipeline") or {}).get("stages") or []
        payload["pipeline"] = data.get("pipeline") or data
    if args.sql_file:
        payload["sql_file"] = args.sql_file
    result = validate_generation(payload)
    text = format_validate_text(result)
    ok = bool(result.get("success") and result.get("passed"))
    if args.output == "json":
        _emit(result, args, text)
        return 0 if ok else 1
    print(text)
    if args.save:
        Path(args.save).write_text(dumps(result, indent=args.indent) + "\n", encoding="utf-8")
    return 0 if ok else 1

_GENERATE_RUNNERS = {
    "sql": cmd_generate_sql,
    "pipeline": cmd_generate_pipeline,
    "apply": cmd_generate_apply,
    "validate": cmd_generate_validate,
}

def cmd_generate(argv: Sequence[str]) -> int:
    """generate 子命令分发（``python -m lineage.cli generate <子命令>``）。"""
    args = list(argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(GENERATE_HELP)
        return 0
    sub = args[0]
    if sub not in _GENERATE_RUNNERS:
        print(f"错误：未知的 generate 子命令 `{sub}`\n", file=sys.stderr)
        print(GENERATE_HELP, file=sys.stderr)
        return 2
    return _GENERATE_RUNNERS[sub](args[1:])

_SUBCOMMAND_RUNNERS["generate"] = cmd_generate

__all__ = ['GENERATE_HELP', 'GENERATE_SUBCOMMANDS', '_add_generate_common', '_finish_generate_args', 'cmd_generate_sql', 'cmd_generate_pipeline', '_load_pipeline_arg', 'cmd_generate_apply', 'cmd_generate_validate', '_GENERATE_RUNNERS', 'cmd_generate']  # 本模块自有名字（供 lineage/cli/__init__.py 兼容导出）
