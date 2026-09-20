"""血缘 × 知识库一体化：把「本条 SQL 解析出的目标字段」对上「知识库里的业务口径」。

``/parse`` 只回答「数据从哪来到哪去」；本模块在它之上再回答一个业务问题：
**这条加工 SQL 产出的指标，业务口径是什么、依赖哪些上游字段？**

匹配策略（只用库内索引，零第三方依赖、结果可解释）：

1. **目标字段级**：``column_lineage`` 的 ``(target_table, target_column)`` 精确命中 ``kb_metrics``
   —— 这是最强证据，说明「本任务亲手产出了这个指标」；
2. **输出表级**：目标表上的其余口径（同表兄弟指标，如 ``产量`` / ``产量（条）``），
   说明「本任务产出的表里还有哪些口径」；
3. **字段术语**：命中口径的目标字段 + ``depends_on`` 依赖字段 → ``kb_fields`` 的中文业务名；
4. **业务规则**：命中口径所在表 / 来源脚本上的规则（最多 ``limit_rules`` 条）。

全部匹配失败也不会报错，只是 ``metrics`` 为空 —— 纯血缘报告照常可用。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "collect_target_fields",
    "match_knowledge",
    "unavailable",
    "KB_BUILD_HINT",
]

#: 知识库不可用时的统一提示（对外文案，HTTP 与插件都读它）
KB_BUILD_HINT = "知识库不存在或为空，请先执行 `.venv/bin/python -m lineage.cli kb build` 建库"

_METRIC_MAX = 12
_TERM_MAX = 30
_RULE_MAX = 5


def unavailable(reason: str = "") -> Dict[str, Any]:
    """构造「知识库不可用」的降级结果（不抛异常，调用方照常返回血缘）。"""
    out: Dict[str, Any] = {
        "kb_available": False,
        "metric_count": 0,
        "metrics": [],
        "terms": [],
        "rules": [],
        "matched_fields": [],
        "hint": KB_BUILD_HINT,
    }
    if reason:
        out["reason"] = reason
    return out


def _low(value: Any) -> str:
    return str(value or "").strip().lower()


def collect_target_fields(column_lineage: Optional[Iterable[Dict[str, Any]]],
                          limit: int = 500) -> List[Dict[str, str]]:
    """从 ``column_lineage`` 抽取去重后的目标字段（保序）。

    一个目标字段可能由多个源字段算出（如 ``产量`` 依赖 3 个字段），
    这里只保留第一条作为「代表来源」，匹配口径只关心字段本身。
    """
    out: List[Dict[str, str]] = []
    seen = set()
    for item in column_lineage or []:
        if not isinstance(item, dict):
            continue
        table = str(item.get("target_table") or "").strip()
        column = str(item.get("target_column") or "").strip()
        if not table or not column:
            continue
        key = (_low(table), _low(column))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "table": table,
            "column": column,
            "source_table": str(item.get("source_table") or "").strip(),
            "source_column": str(item.get("source_column") or "").strip(),
            "expression": str(item.get("expression") or "").strip(),
        })
        if len(out) >= limit:
            break
    return out


def _depends_on_text(dep: Dict[str, Any]) -> str:
    table = str(dep.get("table") or "").strip()
    column = str(dep.get("column") or "").strip()
    cn = str(dep.get("chinese_name") or "").strip()
    full = ".".join(p for p in (table, column) if p)
    return f"{full}({cn})" if cn else full


def _metric_payload(metric: Dict[str, Any], matched_by: str, path: Optional[List[str]]) -> Dict[str, Any]:
    """把 kb_metrics 的一行整形成对外的 metric 对象（字段名与 /kb/metric 保持一致）。"""
    deps = [d for d in (metric.get("depends_on") or []) if isinstance(d, dict)]
    return {
        "target_table": metric.get("table_name") or "",
        "target_column": metric.get("metric_name") or "",
        "chinese_name": metric.get("chinese_name") or "",
        "formula": metric.get("formula") or "",
        "formula_full": metric.get("formula_full") or "",
        "metric_type": metric.get("metric_type") or "",
        "confidence": metric.get("confidence"),
        "unit": metric.get("unit") or "",
        "layer": metric.get("layer") or "",
        "aggregate_func": metric.get("aggregate_func") or "",
        "expression_normalized": metric.get("expression_normalized") or "",
        "depends_on": [{"table": d.get("table") or "", "column": d.get("column") or "",
                        "chinese_name": d.get("chinese_name") or ""} for d in deps],
        "depends_text": ", ".join(_depends_on_text(d) for d in deps),
        "source_script": metric.get("source_file") or "",
        "source_statement": metric.get("source_stmt"),
        "source_task_type": metric.get("source_task_type") or "",
        "input_tables": metric.get("input_tables") or [],
        "notes": metric.get("notes") or "",
        "matched_by": matched_by,
        "lineage_path": list(path or []),
    }


def match_knowledge(
    store: Any,
    targets: Sequence[Dict[str, str]],
    output_tables: Sequence[str] = (),
    limit_metrics: int = _METRIC_MAX,
    limit_terms: int = _TERM_MAX,
    limit_rules: int = _RULE_MAX,
) -> Dict[str, Any]:
    """在知识库里匹配本任务产出的口径 / 术语 / 规则。

    参数 ``store`` 需已打开（调用方负责 close）；``targets`` 是
    :func:`collect_target_fields` 的结果；``output_tables`` 是本次解析出的目标表。
    """
    metrics = store.metrics()
    by_field: Dict[Tuple[str, str], Dict[str, Any]] = {}
    by_table: Dict[str, List[Dict[str, Any]]] = {}
    for metric in metrics:
        table_key = _low(metric.get("table_name"))
        by_field.setdefault((table_key, _low(metric.get("metric_name"))), metric)
        by_table.setdefault(table_key, []).append(metric)

    picked: List[Tuple[Dict[str, Any], str]] = []
    picked_keys = set()
    matched_fields: List[str] = []

    # 1) 目标字段级：最直接的证据
    direct: List[Tuple[Dict[str, Any], str]] = []
    for target in targets:
        key = (_low(target.get("table")), _low(target.get("column")))
        metric = by_field.get(key)
        if metric is None:
            continue
        if target["column"] not in matched_fields:
            matched_fields.append(target["column"])
        if key in picked_keys:
            continue
        picked_keys.add(key)
        direct.append((metric, "目标字段"))

    # 2) 输出表级：同表产出的兄弟口径
    output_keys = []
    for table in output_tables or []:
        table_key = _low(table)
        if table_key and table_key not in output_keys:
            output_keys.append(table_key)
    table_level: List[Tuple[Dict[str, Any], str]] = []
    for table_key in output_keys:
        for metric in by_table.get(table_key, []):
            key = (table_key, _low(metric.get("metric_name")))
            if key in picked_keys:
                continue
            picked_keys.add(key)
            table_level.append((metric, "输出表"))
            column = metric.get("metric_name") or ""
            if column and column not in matched_fields:
                matched_fields.append(column)

    # 公式里依赖字段越多的口径信息量越大（如 产量 依赖 3 个字段），排前面
    richness = lambda item: -len(item[0].get("depends_on") or [])  # noqa: E731
    picked = sorted(direct, key=richness) + sorted(table_level, key=richness)
    picked = picked[:max(0, limit_metrics)]

    # 3) 依赖字段（含目标字段本身）→ 字段中文业务名
    term_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for field in store.fields():
        term_index[(_low(field.get("table_name")), _low(field.get("column_name")))] = field

    terms: List[Dict[str, Any]] = []
    seen_terms = set()

    def _add_term(table: str, column: str) -> None:
        key = (_low(table), _low(column))
        if not column or key in seen_terms:
            return
        field = term_index.get(key)
        if not field or not field.get("chinese_name"):
            return
        seen_terms.add(key)
        terms.append({
            "field": column,
            "table": field.get("table_name") or table,
            "chinese_name": field.get("chinese_name") or "",
            "source": field.get("chinese_source") or "",
            "confidence": field.get("confidence"),
            "unit": field.get("unit") or "",
            "role": field.get("role") or "",
            "business_desc": field.get("business_desc") or "",
        })

    metrics_out: List[Dict[str, Any]] = []
    sources: List[str] = []
    metric_tables: List[str] = []
    for metric, matched_by in picked:
        table_name = metric.get("table_name") or ""
        paths = []
        try:
            paths = store.upstream_paths(table_name, depth=8, max_paths=1) or []
        except Exception:  # noqa: BLE001 — 血缘链路是锦上添花，失败不影响口径
            paths = []
        path = paths[0] if paths else ([table_name] if table_name else [])
        metrics_out.append(_metric_payload(metric, matched_by, path))

        if table_name and table_name not in metric_tables:
            metric_tables.append(table_name)
        source = metric.get("source_file") or ""
        if source and source not in sources:
            sources.append(source)
        for dep in metric.get("depends_on") or []:
            if isinstance(dep, dict):
                _add_term(str(dep.get("table") or ""), str(dep.get("column") or ""))
        _add_term(table_name, metric.get("metric_name") or "")

    # 目标字段自身的中文名（即使没有口径也要给出），放在依赖字段之后
    for target in targets:
        _add_term(target.get("table") or "", target.get("column") or "")

    terms = terms[:max(0, limit_terms)]

    # 4) 业务规则：优先命中口径所在表
    rules: List[Dict[str, Any]] = []
    table_set = set(metric_tables) | {t for t in (output_tables or []) if t}
    for rule in store.rules():
        if len(rules) >= max(0, limit_rules):
            break
        same_table = (rule.get("table_name") or "") in table_set
        same_script = (rule.get("source_file") or "") in sources
        if not (same_table or same_script):
            continue
        rules.append({
            "rule_type": rule.get("rule_type") or "",
            "description": rule.get("description") or "",
            "expression": rule.get("expression") or "",
            "table": rule.get("table_name") or "",
            "source_script": rule.get("source_file") or "",
            "source_statement": rule.get("source_stmt"),
            "source_comment": rule.get("source_comment") or "",
        })

    return {
        "kb_available": True,
        "db": str(getattr(store, "path", "") or ""),
        "metric_count": len(metrics_out),
        "metrics": metrics_out,
        "terms": terms,
        "rules": rules,
        "matched_fields": matched_fields,
        "target_field_count": len(targets),
    }
