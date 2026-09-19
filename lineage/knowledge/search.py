"""知识库检索：关键词 / 模糊匹配 + 相关性打分。

为什么不用 FTS5：中文没有词边界，FTS 默认分词对中文几乎不可用（需要额外分词器）。
这里用「多字段加权 + 字序包含」的朴素打分，零依赖、结果可解释：
* 精确相等 > 前缀命中 > 子串命中 > 中文字序包含（``不良率`` 能命中 ``不良品率``）；
* 字段权重：中文名 > 英文名 > 公式 / 表达式 > 备注说明；
* 结果按 ``metrics / fields / tables / terms / rules`` 分组返回，并给出得分与命中原因。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .store import KnowledgeStore

__all__ = ["search", "format_search_text", "score_text", "tokenize_query"]

_KINDS = ("metrics", "fields", "tables", "terms", "rules")

#: 各字段的权重（命中越靠前的字段，说明越相关）
_METRIC_FIELDS: Sequence[Tuple[str, float]] = (
    ("chinese_name", 1.3),
    ("metric_name", 1.2),
    ("formula", 1.0),
    ("formula_full", 0.9),
    ("table_name", 0.8),
    ("expression_normalized", 0.6),
    ("notes", 0.4),
    ("depends_text", 0.5),
)
_FIELD_FIELDS: Sequence[Tuple[str, float]] = (
    ("chinese_name", 1.3),
    ("column_name", 1.2),
    ("table_name", 0.7),
    ("business_desc", 0.5),
    ("sample_expression", 0.4),
)
_TABLE_FIELDS: Sequence[Tuple[str, float]] = (
    ("chinese_name", 1.3),
    ("table_name", 1.2),
    ("business_desc", 0.6),
)
_TERM_FIELDS: Sequence[Tuple[str, float]] = (
    ("chinese_name", 1.3),
    ("term", 1.2),
    ("aliases_text", 0.6),
)
_RULE_FIELDS: Sequence[Tuple[str, float]] = (
    ("description", 1.1),
    ("expression", 0.9),
    ("table_name", 0.7),
    ("source_file", 0.6),
)

_CN_RE = re.compile(r"[\u4e00-\u9fff]+")
_STRIP_RE = re.compile(r"[\s　,，。.、;；:：!！?？'\"“”‘’()（）\[\]【】/\\|]+")


def tokenize_query(query: str) -> List[str]:
    """把查询串拆成检索 token（中文整串 + 英文按分词 / 下划线拆）。"""
    if not query:
        return []
    text = _STRIP_RE.sub(" ", query.strip())
    raw = [t for t in text.split(" ") if t]
    out: List[str] = []
    for token in raw:
        if token not in out:
            out.append(token)
        if not _CN_RE.fullmatch(token):
            for piece in re.split(r"[_\-]+", token):
                if piece and piece not in out:
                    out.append(piece)
    return out


def _cjk_in_order(needle: str, haystack: str) -> bool:
    """中文按字序包含判断：``不良率`` 命中 ``不良品率``。"""
    if not needle or not haystack:
        return False
    idx = 0
    for ch in haystack:
        if idx < len(needle) and ch == needle[idx]:
            idx += 1
    return idx == len(needle)


def score_text(text: Optional[str], query: str) -> Tuple[float, str]:
    """给单个字段打分，返回 ``(0~100 的分数, 命中原因)``。"""
    if not text or not query:
        return 0.0, ""
    hay = str(text).lower()
    needle = query.lower()
    if not needle.strip():
        return 0.0, ""
    if hay == needle:
        return 100.0, "完全相等"
    if hay.startswith(needle):
        return 82.0, "前缀命中"
    if needle in hay:
        return 65.0, "子串命中"
    # 多 token：全部命中才算（按覆盖度打折）
    tokens = [t for t in tokenize_query(query) if t]
    if len(tokens) > 1:
        hits = [t for t in tokens if t.lower() in hay]
        if hits:
            ratio = len(hits) / len(tokens)
            return 45.0 * ratio, f"词元命中 {len(hits)}/{len(tokens)}"
    if _CN_RE.search(needle) and _cjk_in_order(needle, hay):
        return 40.0, "中文字序命中"
    return 0.0, ""


def _decorate(rows: Iterable[Dict[str, Any]], fields: Sequence[Tuple[str, float]],
              query: str, kind: str) -> List[Dict[str, Any]]:
    scored: List[Dict[str, Any]] = []
    for row in rows:
        best = 0.0
        reason = ""
        hit_field = ""
        for field, weight in fields:
            value = row.get(field)
            if field.endswith("_text"):
                value = row.get(field[: -len("_text")])
                if isinstance(value, (list, tuple)):
                    value = " ".join(str(v) for v in value)
            if not value:
                continue
            base, why = score_text(str(value), query)
            if base <= 0:
                continue
            weighted = base * weight
            if weighted > best:
                best, reason, hit_field = weighted, why, field
        if best <= 0:
            continue
        item = dict(row)
        item["_kind"] = kind
        item["score"] = round(best, 1)
        item["matched_field"] = hit_field
        item["match_reason"] = reason
        # 口径丰富度：公式里运算符越多，说明这条口径「信息量越大」，同分时排前面
        body = row.get("formula") or ""
        item["_richness"] = len(re.findall(r"[+\-*/]", body)) + len(row.get("depends_on") or []) * 0.1
        scored.append(item)
    scored.sort(key=lambda r: (-r["score"], -r["_richness"],
                               r.get("chinese_name") or r.get("table_name")
                               or r.get("term") or r.get("metric_name") or ""))
    return scored


def search(
    store: KnowledgeStore,
    query: str,
    kinds: Optional[Sequence[str]] = None,
    limit: int = 20,
    min_score: float = 0.0,
) -> Dict[str, Any]:
    """在知识库里检索关键词。

    返回 ``{"query":..., "total":N, "groups": {...}, "counts": {...}}``；
    命中条目带 ``score / matched_field / match_reason``，方便解释「为什么搜出来」。
    """
    query = (query or "").strip()
    wanted = tuple(kinds) if kinds else _KINDS
    groups: Dict[str, List[Dict[str, Any]]] = {k: [] for k in wanted}
    if not query:
        return {"query": query, "total": 0, "groups": groups,
                "counts": {k: 0 for k in wanted}}

    if "metrics" in wanted:
        rows = store.metrics()
        for row in rows:
            deps = row.get("depends_on") or []
            row["depends_text"] = " ".join(
                f"{d.get('column', '')} {d.get('chinese_name') or ''}" for d in deps if isinstance(d, dict)
            )
        groups["metrics"] = _decorate(rows, _METRIC_FIELDS, query, "metrics")[:limit]
    if "fields" in wanted:
        groups["fields"] = _decorate(store.fields(), _FIELD_FIELDS, query, "fields")[:limit]
    if "tables" in wanted:
        groups["tables"] = _decorate(store.tables(), _TABLE_FIELDS, query, "tables")[:limit]
    if "terms" in wanted:
        groups["terms"] = _decorate(store.terms(), _TERM_FIELDS, query, "terms")[:limit]
    if "rules" in wanted:
        groups["rules"] = _decorate(store.rules(), _RULE_FIELDS, query, "rules")[:limit]

    counts = {k: len(v) for k, v in groups.items()}
    return {
        "query": query,
        "total": sum(counts.values()),
        "counts": counts,
        "groups": groups,
        "min_score": min_score,
    }


def format_search_text(result: Dict[str, Any], limit_per_group: int = 6) -> str:
    """把检索结果渲染成终端可读中文文本。"""
    lines: List[str] = []
    q = result.get("query", "")
    counts = result.get("counts") or {}
    lines.append("=" * 72)
    lines.append(f"知识库检索：{q}")
    lines.append("=" * 72)
    if not result.get("total"):
        lines.append("没有命中任何知识条目。")
        lines.append("提示：换个关键词，或先跑 `kb build` 建库（kb summary 可看库里有什么）。")
        return "\n".join(lines)
    lines.append("命中分布：" + "  ".join(
        f"{k}={counts.get(k, 0)}" for k in ("metrics", "fields", "tables", "terms", "rules")
    ))

    group = result["groups"].get("metrics") or []
    if group:
        lines.append("")
        lines.append(f"—— 指标口径（{len(group)} 条）——")
        for item in group[:limit_per_group]:
            cn = item.get("chinese_name") or "-"
            lines.append(
                f"  [{item['score']:.1f}] {cn}（{item['metric_name']}）"
                f"  @ {item['table_name']}  [{item.get('metric_type')}]"
            )
            if item.get("formula"):
                lines.append(f"        口径：{item['formula']}")
            lines.append(f"        来源：{item.get('source_file')} 第{item.get('source_stmt')}条语句"
                         f"（命中 {item.get('matched_field')}·{item.get('match_reason')}）")
    group = result["groups"].get("fields") or []
    if group:
        lines.append("")
        lines.append(f"—— 字段术语（{len(group)} 条）——")
        for item in group[:limit_per_group]:
            lines.append(
                f"  [{item['score']:.1f}] {item.get('chinese_name') or '(待确认)'}"
                f"  = {item['table_name']}.{item['column_name']}"
                f"   来源={item.get('chinese_source')}"
            )
    group = result["groups"].get("tables") or []
    if group:
        lines.append("")
        lines.append(f"—— 表（{len(group)} 张）——")
        for item in group[:limit_per_group]:
            lines.append(
                f"  [{item['score']:.1f}] {item['table_name']}"
                f"（{item.get('chinese_name') or '待确认'}）  分层={item.get('layer')}"
                f"  字段数={item.get('column_count')}"
            )
    group = result["groups"].get("terms") or []
    if group:
        lines.append("")
        lines.append(f"—— 业务术语（{len(group)} 条）——")
        for item in group[:limit_per_group]:
            lines.append(
                f"  [{item['score']:.1f}] {item.get('term')} => {item.get('chinese_name') or '(待确认)'}"
                f"  来源={item.get('source')}  出现={item.get('occurrences')} 次"
            )
    group = result["groups"].get("rules") or []
    if group:
        lines.append("")
        lines.append(f"—— 业务规则（{len(group)} 条）——")
        for item in group[:limit_per_group]:
            lines.append(f"  [{item['score']:.1f}] [{item.get('rule_type')}] {item.get('description')}")
            lines.append(f"        来源：{item.get('source_file')} 第{item.get('source_stmt')}条语句")
    lines.append("=" * 72)
    return "\n".join(lines)
