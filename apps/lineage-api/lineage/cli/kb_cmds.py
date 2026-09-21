# -*- coding: utf-8 -*-
"""知识库子命令：kb build / summary / search / show / ask / export / terms / fields。"""

from __future__ import annotations

import argparse
import sys

from lineage_core.parser import DEFAULT_DIALECT, dumps
from pathlib import Path
from typing import Any, Dict, List, Sequence

from lineage.cli.common import _add_common, _emit
from lineage.cli.graph_cmds import _SUBCOMMAND_RUNNERS

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
    from lineage.knowledge import KnowledgeStore, default_db_path

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

    from lineage.knowledge import build_knowledge_base, format_build_text

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

    from lineage.knowledge import format_search_text, search

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

    from lineage.knowledge import format_metric

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

    from lineage.knowledge import answer

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

    from lineage.knowledge import export_markdown

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

__all__ = ['KB_SUBCOMMANDS', 'KB_HELP', 'DEFAULT_KB_DOC', '_kb_store', 'cmd_kb_build', 'cmd_kb_summary', 'cmd_kb_search', 'cmd_kb_show', 'cmd_kb_ask', 'cmd_kb_export', 'cmd_kb_terms', 'cmd_kb_fields', '_KB_RUNNERS', 'cmd_kb']  # 本模块自有名字（供 lineage/cli/__init__.py 兼容导出）
