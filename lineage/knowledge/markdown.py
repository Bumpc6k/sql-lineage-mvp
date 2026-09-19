"""Markdown 知识文档导出：《业务口径知识库.md》。

组织结构（按「主题 / 分层」而不是按脚本）：
0. 概览（规模 + 分布）
1. 指标口径总览（按 数仓分层 → 表 组织成表格）
2. 指标口径明细（每条口径：公式 / 忠实表达式 / 依赖字段 / 来源 / 血缘链路）
3. 业务术语词典（字段与表的中文业务名）
4. 字段清单（按表）
5. 业务规则（过滤 / 分区 / 关联 / 注释规则）
6. 待确认术语（人工补充清单）
7. 脚本档案
8. 已知限制
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .store import KnowledgeStore

__all__ = ["to_markdown", "export_markdown", "DEFAULT_DOC_NAME"]

DEFAULT_DOC_NAME = "业务口径知识库.md"

_LAYER_ORDER = ("src", "ods", "dim", "dwd", "dws", "ads")
_LAYER_LABEL = {
    "src": "源系统接口层",
    "ods": "ODS 贴源层",
    "dim": "维表层",
    "dwd": "DWD 明细层",
    "dws": "DWS 汇总层",
    "ads": "ADS 应用层",
}
_LIMITATIONS = """\
1. **语法级推导，不是语义级推导**：口径来自 SQL 静态解析，不连数据库、不读元数据，
   `SELECT *` 与同名字段无法消歧（会标为「未解析」）。
2. **公式是「去别名 + 中文替换」的结果**：函数参数（如 `ROUND(x, 4)` 的精度、
   `NULLIF(x, 0)` 的防除零）会被保留在「忠实表达式」里；只有全部聚合函数一致时，
   才额外给出剥掉聚合壳的「口径本体」（聚合类型记录在指标类型里）。
3. **中文业务名来源分级**：脚本行内注释 > 内置词典 > 命名规则组合 > 待确认。
   规则组合出来的名字（来源 `rule` / `rule_partial`）可能有歧义，需人工复核。
4. **缺失的部分**：
   - 动态分区、`LATERAL VIEW` / `explode`、复杂 UDTF 的加工语义未展开；
   - 指标之间的「层级关系」（如 dws 汇总口径 vs dwd 明细口径是否一致）未做校验；
   - 环比 / 同比、时间窗口（`ROWS BETWEEN`）等窗口语义未纳入口径本体；
   - 责任人 / 版本号是占位字段，需要人工在库里补（当前为空 → 文档显示「待指定」）。
5. **增量 upsert 的边界**：增量以「脚本」为最小粒度；跨脚本聚合出来的字段 / 表 /
   术语会做并集合并，因此**改动脚本后建议跑一次全量 rebuild** 保证完全一致。
"""


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _esc(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _layer_sort_key(layer: str) -> int:
    try:
        return _LAYER_ORDER.index(layer)
    except ValueError:
        return len(_LAYER_ORDER)


def _fmt_deps(deps: Sequence[Dict[str, Any]]) -> str:
    bits: List[str] = []
    for dep in deps or []:
        if not isinstance(dep, dict):
            continue
        table = dep.get("table")
        column = dep.get("column") or ""
        chinese = dep.get("chinese_name") or "未定名"
        bits.append(f"`{table}.{column}`（{chinese}）" if table else f"`{column}`（{chinese}）")
    return "、".join(bits) or "—"


def to_markdown(store: KnowledgeStore, title: str = "业务口径知识库", with_detail: bool = True) -> str:
    """把整库渲染成 Markdown 文本。"""
    summary = store.summary()
    counts = summary["counts"]
    metrics = store.metrics()
    fields = store.fields()
    tables = store.tables()
    terms = store.terms()
    rules = store.rules()
    scripts = store.scripts()
    meta = summary
    lines: List[str] = []

    # ---------------- 头 ----------------
    lines.append(f"# {title}")
    lines.append("")
    lines.append("> 由 sql-lineage-mvp `kb export --md` 自动生成：从数仓 SQL 脚本里提炼业务口径、")
    lines.append("> 字段术语与业务规则。**改动脚本后建议重跑 `kb build`**，本文档随库一起刷新。")
    lines.append("")
    lines.append(f"- 生成时间：{_now()}")
    lines.append(f"- 知识库文件：`{meta['db']}`（结构版本 {meta['schema_version']}，"
                 f"最近建库 {meta.get('built_at') or '—'}）")
    lines.append(f"- 规模：**{counts['kb_metrics']} 条指标口径 / {counts['kb_fields']} 个字段 / "
                 f"{counts['kb_tables']} 张表 / {counts['kb_terms']} 条业务术语 / "
                 f"{counts['kb_rules']} 条业务规则 / {counts['kb_scripts']} 个脚本**")
    lines.append(f"- 字段中文化覆盖：{counts['fields_with_chinese']}/{counts['kb_fields']}，"
                 f"待确认术语 {counts['pending_terms']} 个")
    lines.append("")

    # ---------------- 0. 概览 ----------------
    lines.append("## 0. 概览")
    lines.append("")
    lines.append("### 0.1 指标口径按分层分布")
    lines.append("")
    lines.append("| 分层 | 含义 | 口径条数 |")
    lines.append("| --- | --- | --- |")
    by_layer = summary["metric_by_layer"]
    for layer in sorted(by_layer, key=_layer_sort_key):
        lines.append(f"| {layer} | {_LAYER_LABEL.get(layer, '—')} | {by_layer[layer]} |")
    lines.append(f"| **合计** | — | **{counts['kb_metrics']}** |")
    lines.append("")
    lines.append("### 0.2 指标口径按类型分布")
    lines.append("")
    lines.append("| 口径类型 | 条数 | 说明 |")
    lines.append("| --- | --- | --- |")
    type_desc = {
        "聚合": "SUM / COUNT / AVG / MAX / MIN 等聚合",
        "算术计算": "加减乘除组合（a+b-c）",
        "比率": "除法口径（不良率 / 产销率 / 开动率）",
        "条件分支": "CASE WHEN 条件判定（含条件计数）",
        "窗口函数": "OVER 窗口（排名 / 累计）",
        "函数转换": "COALESCE / CAST / ROUND 等函数包装",
    }
    for k, v in summary["metric_by_type"].items():
        lines.append(f"| {k} | {v} | {type_desc.get(k, '—')} |")
    lines.append("")
    lines.append("### 0.3 口径最多的表（Top 15）")
    lines.append("")
    lines.append("| 表 | 中文名 | 分层 | 口径条数 |")
    lines.append("| --- | --- | --- | --- |")
    for row in summary["top_tables"][:15]:
        if not row["metric_count"]:
            continue
        lines.append(f"| `{row['name']}` | {row['chinese'] or '待确认'} | {row['layer']} | {row['metric_count']} |")
    lines.append("")

    # ---------------- 1. 口径总览 ----------------
    lines.append("## 1. 指标口径总览")
    lines.append("")
    by_layer_table: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for metric in metrics:
        by_layer_table.setdefault(metric.get("layer") or "其他", {}).setdefault(
            metric["table_name"], []
        ).append(metric)
    for layer in sorted(by_layer_table, key=_layer_sort_key):
        lines.append(f"### {layer} 层（{_LAYER_LABEL.get(layer, '—')}）")
        lines.append("")
        for table_name in sorted(by_layer_table[layer]):
            table = next((t for t in tables if t["table_name"] == table_name), {})
            lines.append(f"#### `{table_name}`（{table.get('chinese_name') or '待确认'}）")
            lines.append("")
            lines.append("| 指标（中文） | 字段 | 口径公式 | 类型 | 来源脚本 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for metric in sorted(by_layer_table[layer][table_name],
                                 key=lambda m: m["metric_name"]):
                lines.append(
                    f"| {_esc(metric.get('chinese_name') or '待确认')} "
                    f"| `{metric['metric_name']}` "
                    f"| {_esc(metric.get('formula'))} "
                    f"| {metric.get('metric_type')}"
                    + (f"({metric.get('aggregate_func')})" if metric.get("aggregate_func") else "")
                    + f" | `{metric.get('source_file')}` 第{metric.get('source_stmt')}条 |"
                )
            lines.append("")

    if not with_detail:
        return "\n".join(lines) + "\n"

    # ---------------- 2. 口径明细 ----------------
    lines.append("## 2. 指标口径明细")
    lines.append("")
    lines.append("> 每条口径给出：口径公式（中文可读）、忠实表达式（原始语义）、依赖字段、")
    lines.append("> 来源脚本与血缘链路。`置信度` 由「中文名来源 + 依赖字段是否都能定名」推导。")
    lines.append("")
    for idx, metric in enumerate(metrics, start=1):
        title_cn = metric.get("chinese_name") or "待确认"
        lines.append(f"### 2.{idx} {title_cn}"
                     f"（`{metric['table_name']}.{metric['metric_name']}`）")
        lines.append("")
        lines.append(f"- **口径**：`{metric.get('formula')}`")
        lines.append(f"- **忠实表达式**：`{metric.get('formula_full')}`")
        lines.append(f"- **口径类型**：{metric.get('metric_type')}"
                     + (f"（聚合函数 `{metric.get('aggregate_func')}`）"
                        if metric.get("aggregate_func") else "")
                     + (f"；涉及函数：{'、'.join(metric.get('functions') or [])}"
                        if metric.get("functions") else ""))
        lines.append(f"- **依赖字段**：{_fmt_deps(metric.get('depends_on') or [])}")
        lines.append(f"- **上游表**：{'、'.join('`%s`' % t for t in (metric.get('input_tables') or [])) or '—'}")
        lines.append(f"- **来源**：`{metric.get('source_file')}` 第 {metric.get('source_stmt')} 条语句"
                     f"（{metric.get('source_task_type')}）；责任人：{metric.get('owner') or '待指定'}；"
                     f"版本：{metric.get('version')}；置信度：{metric.get('confidence')}")
        if metric.get("notes"):
            lines.append(f"- **备注**：{metric['notes']}")
        if metric.get("expression_raw"):
            lines.append(f"- **实现片段**：`{metric['expression_raw']}`")
        paths = store.upstream_paths(metric["table_name"], depth=8, max_paths=1)
        if paths:
            lines.append("- **血缘链路（示例）**：")
            for path in paths[:1]:
                lines.append(f"  ```text\n  {'  →  '.join(path)}\n  ```")
        lines.append(f"- 检索：`kb show {metric['metric_name']}` / `kb ask \""
                     f"{title_cn}怎么算的\"`")
        lines.append("")

    # ---------------- 3. 业务术语词典 ----------------
    lines.append("## 3. 业务术语词典")
    lines.append("")
    lines.append("| 术语（英文/拼音） | 中文业务名 | 类别 | 来源 | 置信度 | 出现次数 | 涉及表 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for term in terms:
        if term.get("source") == "pending" and not term.get("tables"):
            continue
        lines.append(
            f"| `{term['term']}` | {term.get('chinese_name') or '**待确认**'} "
            f"| {term.get('category')} | {term.get('source')} | {term.get('confidence')} "
            f"| {term.get('occurrences')} "
            f"| {_esc('、'.join((term.get('tables') or [])[:3])) or '—'} |"
        )
    lines.append("")

    # ---------------- 4. 字段清单 ----------------
    lines.append("## 4. 字段清单（按表）")
    lines.append("")
    for table in tables:
        table_fields = [f for f in fields if f["table_name"] == table["table_name"]]
        if not table_fields:
            continue
        lines.append(f"### `{table['table_name']}`（{table.get('chinese_name') or '待确认'}，"
                     f"{table.get('layer')} 层，{len(table_fields)} 个字段）")
        lines.append("")
        if table.get("business_desc"):
            lines.append(f"> 脚本说明：{table['business_desc']}")
            lines.append("")
        lines.append("| 字段 | 中文业务名 | 单位 | 名来源 | 置信度 | 角色 | 出现脚本 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for field in sorted(table_fields, key=lambda f: f["column_name"]):
            lines.append(
                f"| `{field['column_name']}` | {field.get('chinese_name') or '**待确认**'} "
                f"| {field.get('unit') or '—'} | {field.get('chinese_source')} "
                f"| {field.get('confidence')} | {field.get('role')} "
                f"| {_esc('、'.join((field.get('source_files') or [])[:2])) or '—'} |"
            )
        lines.append("")

    # ---------------- 5. 业务规则 ----------------
    lines.append("## 5. 业务规则")
    lines.append("")
    if not rules:
        lines.append("（本次扫描没有提取到业务规则）")
        lines.append("")
    else:
        lines.append("| 类型 | 规则说明 | 表达式 | 作用表 | 来源脚本 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for rule in rules:
            lines.append(
                f"| {rule.get('rule_type')} | {_esc(rule.get('description'))} "
                f"| `{_esc(rule.get('expression'))}` | `{rule.get('table_name')}` "
                f"| `{rule.get('source_file')}` 第{rule.get('source_stmt')}条 |"
            )
        lines.append("")

    # ---------------- 6. 待确认 ----------------
    no_name = [t for t in terms if not t.get("chinese_name")]
    low_conf = [t for t in terms if t.get("chinese_name")
                and (t.get("source") in ("rule_partial",) or float(t.get("confidence") or 0) < 0.5)]
    lines.append("## 6. 待确认 / 需复核术语")
    lines.append("")
    lines.append(f"- 未能自动推断中文名的字段：**{len(no_name)}** 个")
    lines.append(f"- 由命名规则拼接、置信度较低建议复核的字段：**{len(low_conf)}** 个")
    lines.append("")
    if no_name:
        lines.append("### 6.1 未能推断（需人工补充）")
        lines.append("")
        lines.append("| 字段 | 涉及表 |")
        lines.append("| --- | --- |")
        for term in no_name:
            lines.append(f"| `{term['term']}` | {_esc('、'.join((term.get('tables') or [])[:4])) or '—'} |")
        lines.append("")
    if low_conf:
        lines.append("### 6.2 低置信度（建议复核命名规则推断结果）")
        lines.append("")
        lines.append("| 字段 | 推断中文名 | 来源 | 置信度 | 涉及表 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for term in low_conf[:60]:
            lines.append(
                f"| `{term['term']}` | {term.get('chinese_name')} | {term.get('source')} "
                f"| {term.get('confidence')} | {_esc('、'.join((term.get('tables') or [])[:3])) or '—'} |"
            )
        lines.append("")
    if not no_name and not low_conf:
        lines.append("全部术语都有可信的中文业务名 ✔")
        lines.append("")
    lines.append("补充方式：在 `lineage/knowledge/glossary.json` 的 `columns` 里加一条，"
                 "或直接在 SQL 里给字段加行内注释（注释优先级最高）。")
    lines.append("")

    # ---------------- 7. 脚本档案 ----------------
    lines.append("## 7. 脚本档案")
    lines.append("")
    lines.append("| 脚本 | 语句数 | 分层 | 产出表 | 读取表 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for script in scripts:
        lines.append(
            f"| `{script['source_file']}` | {script.get('statement_count')} "
            f"| {_esc('、'.join(script.get('layers') or []))} "
            f"| {_esc('、'.join(script.get('output_tables') or []))} "
            f"| {_esc('、'.join((script.get('input_tables') or [])[:5]))} |"
        )
    lines.append("")

    # ---------------- 8. 限制 ----------------
    lines.append("## 8. 已知限制")
    lines.append("")
    lines.append(_LIMITATIONS)
    lines.append("")
    return "\n".join(lines) + "\n"


def export_markdown(
    store: KnowledgeStore,
    out_path: Any,
    title: str = "业务口径知识库",
    with_detail: bool = True,
) -> Dict[str, Any]:
    """导出 Markdown 文档，返回 ``{"path", "bytes", "lines"}``。"""
    text = to_markdown(store, title=title, with_detail=with_detail)
    path = Path(out_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"path": str(path.resolve()), "bytes": len(text.encode("utf-8")),
            "lines": text.count("\n") + 1}
