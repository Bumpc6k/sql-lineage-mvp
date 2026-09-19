"""口径提炼器：从字段级血缘里提炼「业务口径 / 字段术语 / 业务规则」。

输入：``lineage.parser`` 的字段级血缘（``column_lineage``）+ 全局血缘图 + SQL 注释；
输出：可落库的结构化知识（指标口径 / 字段术语 / 表术语 / 业务规则 / 脚本档案）。

提炼规则（可解释、可复现，不依赖 LLM）：

1. **指标口径**：一条输出字段如果只是 ``a.col AS col`` 的直取，不产生新口径；
   只要有聚合 / 算术 / 比率 / 条件 / 函数 / 窗口加工，就提炼成一条口径，记录
   「公式（中文可读）」「忠实表达式」「依赖字段」「来源脚本」「指标类型」。
2. **公式归一化**：去 ``AS`` 别名、去表别名前缀 → 英文归一化表达式；再把字段名
   换成中文业务名 → ``产量 = 打码量 + 跳码量 - 重码量``。所有聚合函数一致时，
   额外剥掉聚合外壳，得到「口径本体」，聚合类型单独记为 ``聚合(SUM)``。
3. **业务术语**：字段名 → 中文业务名，优先级为
   行内注释 > 内置词典 > 命名规则组合（前缀/词根/后缀）> 待确认。
4. **业务规则**：WHERE / JOIN ON / 分区 / 行内注释里带规则关键词的说明。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .comments import SqlComments, parse_sql_comments
from .textutil import (
    AGGREGATE_FUNCS,
    ChineseNameResolver,
    Glossary,
    NameHit,
    localize_expression,
    normalize_expression,
    split_comment_unit,
    strip_uniform_aggregates,
)

__all__ = [
    "KnowledgeExtractor",
    "ExtractionResult",
    "TableKnowledge",
    "FieldKnowledge",
    "MetricKnowledge",
    "RuleKnowledge",
    "TermKnowledge",
    "ScriptKnowledge",
    "classify_expression",
    "METRIC_TYPES",
]

#: 指标类型枚举
METRIC_TYPES = (
    "窗口函数",
    "条件分支",
    "比率",
    "聚合",
    "算术计算",
    "函数转换",
    "直取",
)

#: 常量来源标记（parser 用 ``(常量)`` 表示没有上游字段）
CONSTANT_MARKER = "(常量)"

#: 行内注释里出现这些词，说明是业务规则说明而不是字段名
RULE_KEYWORDS = (
    "仅统计", "只统计", "仅计", "剔除", "排除", "不计入", "不含", "过滤",
    "口径", "规则", "注意", "必须", "按.*统计", "视为", "算作", "以.*为准",
)

_DIV_RE = re.compile(r"(?<![/*])/(?![/*])")
_ARITH_RE = re.compile(r"(?<=[\w\)\]])\s*[+\-*]\s*(?=[\w\(\[])|(?<=[\w\)\]])\s*[+\-*/]\s*(?=[\w\(\[])")
_FUNC_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z_0-9]*)\s*\(")
_OVER_RE = re.compile(r"\bOVER\s*\(", re.IGNORECASE)


def _clip(text: str, limit: int = 300) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _hash_key(*parts: str) -> str:
    raw = "\u0001".join(p or "" for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def detect_functions(expr: str) -> List[str]:
    """列出表达式里出现的函数名（去重、保序、大写）。"""
    out: List[str] = []
    for m in _FUNC_CALL_RE.finditer(expr or ""):
        name = m.group(1).upper()
        if name in {"AS", "AND", "OR", "NOT", "IN", "THEN", "WHEN", "ELSE", "END"}:
            continue
        if name not in out:
            out.append(name)
    return out


def classify_expression(expr: str) -> Tuple[str, Optional[str]]:
    """判断指标类型，返回 ``(指标类型, 聚合函数)``。"""
    if not expr:
        return "直取", None
    upper = expr.upper()
    funcs = detect_functions(expr)
    agg = next((f for f in funcs if f in AGGREGATE_FUNCS), None)

    if _OVER_RE.search(upper) or "OVER(" in upper.replace(" ", ""):
        return "窗口函数", agg
    if "CASE" in upper and "WHEN" in upper:
        return "条件分支", agg
    if _DIV_RE.search(expr):
        return "比率", agg
    if agg:
        return "聚合", agg
    if _ARITH_RE.search(expr):
        return "算术计算", None
    if funcs:
        return "函数转换", None
    return "直取", None


# --------------------------------------------------------------------------- #
# 知识数据结构
# --------------------------------------------------------------------------- #
@dataclass
class TableKnowledge:
    table_name: str
    short_name: str = ""
    layer: str = ""
    chinese_name: Optional[str] = None
    chinese_source: str = "pending"
    business_desc: str = ""
    column_count: int = 0
    source_files: List[str] = field(default_factory=list)
    is_root: bool = False
    is_leaf: bool = False

    def to_row(self) -> Dict[str, Any]:
        return {
            "table_name": self.table_name,
            "short_name": self.short_name,
            "layer": self.layer,
            "chinese_name": self.chinese_name,
            "chinese_source": self.chinese_source,
            "business_desc": self.business_desc,
            "column_count": self.column_count,
            "source_files": sorted(set(self.source_files)),
            "is_root": int(self.is_root),
            "is_leaf": int(self.is_leaf),
        }


@dataclass
class FieldKnowledge:
    table_name: str
    column_name: str
    chinese_name: Optional[str] = None
    chinese_source: str = "pending"
    confidence: float = 0.0
    unit: Optional[str] = None
    layer: str = ""
    role: str = "source"          # source / target / both
    business_desc: str = ""
    sample_expression: str = ""
    source_files: List[str] = field(default_factory=list)

    @property
    def key(self) -> Tuple[str, str]:
        return self.table_name, self.column_name

    def to_row(self) -> Dict[str, Any]:
        return {
            "table_name": self.table_name,
            "column_name": self.column_name,
            "chinese_name": self.chinese_name,
            "chinese_source": self.chinese_source,
            "confidence": self.confidence,
            "unit": self.unit,
            "layer": self.layer,
            "role": self.role,
            "business_desc": self.business_desc,
            "sample_expression": _clip(self.sample_expression, 200),
            "source_files": sorted(set(self.source_files)),
        }


@dataclass
class MetricKnowledge:
    metric_name: str
    table_name: str
    chinese_name: Optional[str] = None
    chinese_source: str = "pending"
    layer: str = ""
    metric_type: str = "直取"
    aggregate_func: Optional[str] = None
    functions: List[str] = field(default_factory=list)
    expression_raw: str = ""
    expression_normalized: str = ""
    formula: str = ""
    formula_full: str = ""
    depends_on: List[Dict[str, Any]] = field(default_factory=list)
    source_file: str = ""
    source_stmt: int = 1
    source_task_type: str = ""
    input_tables: List[str] = field(default_factory=list)
    owner: str = ""
    version: str = "v1"
    confidence: float = 0.0
    notes: str = ""
    unit: Optional[str] = None

    @property
    def metric_type_label(self) -> str:
        return f"{self.metric_type}({self.aggregate_func})" if self.aggregate_func else self.metric_type

    def to_row(self) -> Dict[str, Any]:
        return {
            "metric_name": self.metric_name,
            "table_name": self.table_name,
            "chinese_name": self.chinese_name,
            "chinese_source": self.chinese_source,
            "layer": self.layer,
            "metric_type": self.metric_type,
            "aggregate_func": self.aggregate_func,
            "functions": sorted(set(self.functions)),
            "expression_raw": self.expression_raw,
            "expression_normalized": self.expression_normalized,
            "formula": self.formula,
            "formula_full": self.formula_full,
            "depends_on": self.depends_on,
            "source_file": self.source_file,
            "source_stmt": self.source_stmt,
            "source_task_type": self.source_task_type,
            "input_tables": sorted(set(self.input_tables)),
            "owner": self.owner,
            "version": self.version,
            "confidence": self.confidence,
            "notes": self.notes,
            "unit": self.unit,
        }


@dataclass
class RuleKnowledge:
    rule_key: str
    rule_type: str
    description: str
    expression: str = ""
    table_name: str = ""
    layer: str = ""
    source_file: str = ""
    source_stmt: int = 1
    source_comment: str = ""

    def to_row(self) -> Dict[str, Any]:
        return {
            "rule_key": self.rule_key,
            "rule_type": self.rule_type,
            "description": self.description,
            "expression": self.expression,
            "table_name": self.table_name,
            "layer": self.layer,
            "source_file": self.source_file,
            "source_stmt": self.source_stmt,
            "source_comment": self.source_comment,
        }


@dataclass
class TermKnowledge:
    term: str
    chinese_name: Optional[str] = None
    category: str = "field"       # field / table / keyword
    source: str = "pending"       # builtin / comment / rule / rule_partial / pending
    confidence: float = 0.0
    domain: str = ""
    aliases: List[str] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    occurrences: int = 0

    @property
    def key(self) -> str:
        return self.term

    def to_row(self) -> Dict[str, Any]:
        return {
            "term": self.term,
            "chinese_name": self.chinese_name,
            "category": self.category,
            "source": self.source,
            "confidence": self.confidence,
            "domain": self.domain,
            "aliases": sorted(set(self.aliases)),
            "tables": sorted(set(self.tables)),
            "occurrences": self.occurrences,
        }


@dataclass
class ScriptKnowledge:
    source_file: str
    header_comment: str = ""
    statement_count: int = 0
    layers: List[str] = field(default_factory=list)
    output_tables: List[str] = field(default_factory=list)
    input_tables: List[str] = field(default_factory=list)
    task_types: List[str] = field(default_factory=list)

    def to_row(self) -> Dict[str, Any]:
        return {
            "source_file": self.source_file,
            "header_comment": _clip(self.header_comment, 800),
            "statement_count": self.statement_count,
            "layers": sorted(set(self.layers)),
            "output_tables": sorted(set(self.output_tables)),
            "input_tables": sorted(set(self.input_tables)),
            "task_types": sorted(set(self.task_types)),
        }


@dataclass
class ExtractionResult:
    tables: List[TableKnowledge] = field(default_factory=list)
    fields: List[FieldKnowledge] = field(default_factory=list)
    metrics: List[MetricKnowledge] = field(default_factory=list)
    rules: List[RuleKnowledge] = field(default_factory=list)
    terms: List[TermKnowledge] = field(default_factory=list)
    scripts: List[ScriptKnowledge] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)
    dialect: str = "hive"
    source_dirs: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    statement_count: int = 0
    failures: List[Dict[str, str]] = field(default_factory=list)

    # -- 统计 -------------------------------------------------------------- #
    @property
    def pending_terms(self) -> List[TermKnowledge]:
        return [t for t in self.terms if t.source == "pending"]

    @property
    def confirmed_terms(self) -> List[TermKnowledge]:
        return [t for t in self.terms if t.source != "pending"]

    def stats(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        for m in self.metrics:
            by_type[m.metric_type] = by_type.get(m.metric_type, 0) + 1
        by_layer: Dict[str, int] = {}
        for m in self.metrics:
            by_layer[m.layer or "?"] = by_layer.get(m.layer or "?", 0) + 1
        term_sources: Dict[str, int] = {}
        for t in self.terms:
            term_sources[t.source] = term_sources.get(t.source, 0) + 1
        with_name = sum(1 for f in self.fields if f.chinese_name)
        return {
            "dialect": self.dialect,
            "dirs": self.source_dirs,
            "file_count": len(self.files),
            "statement_count": self.statement_count,
            "table_count": len(self.tables),
            "field_count": len(self.fields),
            "field_with_chinese": with_name,
            "metric_count": len(self.metrics),
            "metric_by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
            "metric_by_layer": dict(sorted(by_layer.items())),
            "rule_count": len(self.rules),
            "term_count": len(self.terms),
            "term_by_source": dict(sorted(term_sources.items(), key=lambda kv: -kv[1])),
            "script_count": len(self.scripts),
            "edge_count": len(self.edges),
            "failure_count": len(self.failures),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stats": self.stats(),
            "tables": [t.to_row() for t in self.tables],
            "fields": [f.to_row() for f in self.fields],
            "metrics": [m.to_row() for m in self.metrics],
            "rules": [r.to_row() for r in self.rules],
            "terms": [t.to_row() for t in self.terms],
            "scripts": [s.to_row() for s in self.scripts],
            "edges": self.edges,
            "failures": self.failures,
        }


# --------------------------------------------------------------------------- #
# 提炼器
# --------------------------------------------------------------------------- #
class KnowledgeExtractor:
    """把「解析结果 + SQL 注释」提炼成业务口径知识。"""

    def __init__(self, glossary: Optional[Glossary] = None, dialect: str = "hive") -> None:
        self.glossary = glossary or Glossary.load_default()
        self.resolver = ChineseNameResolver(self.glossary)
        self.dialect = dialect
        self._tables: Dict[str, TableKnowledge] = {}
        self._fields: Dict[Tuple[str, str], FieldKnowledge] = {}
        #: 全局字段中文名映射（英文列名 -> 中文），用于公式中文化
        self._name_map: Dict[str, str] = {}
        self._metrics: List[MetricKnowledge] = []
        self._rules: Dict[str, RuleKnowledge] = {}
        self._terms: Dict[str, TermKnowledge] = {}
        self._scripts: List[ScriptKnowledge] = []
        self._edges: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._pending: List[Tuple[str, List[Dict[str, Any]], SqlComments]] = []
        self._root_tables: Set[str] = set()
        self._leaf_tables: Set[str] = set()
        self._dirs: List[str] = []
        self._files: List[str] = []
        self._statement_count = 0
        self._failures: List[Dict[str, str]] = []

    # -- 装载全局信息 ------------------------------------------------------ #
    def add_graph(self, graph: Any) -> None:
        """吸收全局血缘图（提供表分层 / 上下游 / 边）。"""
        data = graph.to_dict() if hasattr(graph, "to_dict") else dict(graph or {})
        for node in data.get("nodes", []):
            name = node.get("name")
            if not name:
                continue
            tbl = self._ensure_table(name, node.get("layer") or "")
            produced = node.get("produced_by") or []
            consumed = node.get("consumed_by") or []
            if not produced:
                self._root_tables.add(name)
            if not consumed:
                self._leaf_tables.add(name)
        for edge in data.get("edges", []):
            src, dst = edge.get("source"), edge.get("target")
            if not src or not dst:
                continue
            key = (src, dst)
            self._edges[key] = {
                "source_table": src,
                "target_table": dst,
                "source_files": sorted(set(edge.get("files") or [])),
                "column_mappings": int(edge.get("column_mappings") or 0),
            }
        for name in data.get("scan_meta", {}).get("scanned_files", []) or []:
            if name not in self._files:
                self._files.append(name)
        root = data.get("root")
        if root:
            self._dirs.append(root)

    # -- 装载一个文件 ------------------------------------------------------ #
    def add_file(
        self,
        source_label: str,
        sql_text: str,
        statements: Sequence[Dict[str, Any]],
        comments: Optional[SqlComments] = None,
    ) -> None:
        """暂存一个 SQL 文件的解析结果 + 注释（真正的提炼在 :meth:`finish`）。

        为什么要分两阶段：口径公式里要把依赖字段换成中文名，而依赖可能指向
        后面才扫描到的表（ads 依赖 dws、dws 依赖 dwd），必须先全量登记字段。
        """
        comments = comments or parse_sql_comments(sql_text)
        if source_label not in self._files:
            self._files.append(source_label)

        script = ScriptKnowledge(source_file=source_label, header_comment=comments.header)
        for stmt in statements:
            self._statement_count += 1
            script.statement_count += 1
            script.task_types.append(stmt.get("task_type") or "")
            for t in stmt.get("output_table_names") or []:
                script.output_tables.append(t)
                script.layers.append(_layer_of(t))
            for t in stmt.get("input_table_names") or []:
                script.input_tables.append(t)
        self._scripts.append(script)
        self._pending.append((source_label, list(statements), comments))

    def finish(self) -> ExtractionResult:
        """两阶段提炼：先全量登记字段（阶段一），再提炼口径与规则（阶段二）。"""
        # 阶段一：登记所有表 / 字段，建立全局中文名映射
        for source_label, statements, comments in self._pending:
            for stmt in statements:
                output_names = stmt.get("output_table_names") or []
                target_name = output_names[0] if output_names else "(query_result)"
                if output_names:
                    self._ensure_table(target_name, _layer_of(target_name)).source_files.append(source_label)
                for name in stmt.get("input_table_names") or []:
                    self._ensure_table(name, _layer_of(name))
                for row in stmt.get("column_lineage") or []:
                    column = row.get("target_column")
                    if column:
                        self._register_field(target_name, column, "target",
                                             row.get("expression") or "", comments, source_label)
                    src_table, src_col = row.get("source_table"), row.get("source_column")
                    if src_col and src_col != CONSTANT_MARKER:
                        self._register_field(src_table or "(未知来源)", src_col, "source",
                                             row.get("expression") or "", comments, source_label)

        # 阶段二：口径 + 规则
        for source_label, statements, comments in self._pending:
            script_table = ""
            for stmt in statements:
                output_names = stmt.get("output_table_names") or []
                if output_names and not script_table:
                    script_table = output_names[0]
                self._extract_statement(source_label, stmt, comments)
            self.add_comment_rules(source_label, comments, script_table)

        for tbl in self._tables.values():
            tbl.is_root = tbl.table_name in self._root_tables or not tbl.source_files
            tbl.is_leaf = tbl.table_name in self._leaf_tables
            tbl.column_count = sum(1 for f in self._fields.values() if f.table_name == tbl.table_name)
            if not tbl.chinese_name:
                hit = self.resolver.resolve_table(tbl.table_name)
                if hit.ok:
                    tbl.chinese_name, tbl.chinese_source = hit.chinese_name, hit.source

        # 术语词典：内置 + 注释 + 规则推断 + 待确认
        self._build_terms()

        metrics = sorted(self._metrics, key=lambda m: (m.table_name, m.metric_name))
        fields = sorted(self._fields.values(), key=lambda f: (f.table_name, f.column_name))
        tables = sorted(self._tables.values(), key=lambda t: t.table_name)
        rules = sorted(self._rules.values(), key=lambda r: (r.source_file, r.rule_type, r.expression))
        terms = sorted(self._terms.values(), key=lambda t: (t.source == "pending", t.term))
        scripts = sorted(self._scripts, key=lambda s: s.source_file)
        return ExtractionResult(
            tables=tables,
            fields=fields,
            metrics=metrics,
            rules=rules,
            terms=terms,
            scripts=scripts,
            edges=sorted(self._edges.values(), key=lambda e: (e["source_table"], e["target_table"])),
            dialect=self.dialect,
            source_dirs=sorted(set(self._dirs)),
            files=sorted(set(self._files)),
            statement_count=self._statement_count,
            failures=list(self._failures),
        )

    # ------------------------------------------------------------------ #
    # 单条语句
    # ------------------------------------------------------------------ #
    def _extract_statement(
        self, source_label: str, stmt: Dict[str, Any], comments: SqlComments
    ) -> None:
        output_names = stmt.get("output_table_names") or []
        target_name = output_names[0] if output_names else "(query_result)"
        if output_names:
            self._ensure_table(target_name, _layer_of(target_name)).source_files.append(source_label)

        input_names = list(stmt.get("input_table_names") or [])
        for name in input_names:
            self._ensure_table(name, _layer_of(name))

        # 1) 字段 + 口径
        by_target: Dict[str, List[Dict[str, Any]]] = {}
        for row in stmt.get("column_lineage") or []:
            by_target.setdefault(row.get("target_column") or "", []).append(row)
        for column, rows in by_target.items():
            if not column:
                continue
            self._register_field(target_name, column, "target", rows[0].get("expression") or "",
                                 comments, source_label)
            depends: List[Tuple[str, str]] = []
            for row in rows:
                src_table = row.get("source_table")
                src_col = row.get("source_column")
                if not src_col or src_col == CONSTANT_MARKER:
                    continue
                if src_table:
                    self._register_field(src_table, src_col, "source", row.get("expression") or "",
                                         comments, source_label)
                depends.append((src_table or "", src_col))
            self._maybe_metric(source_label, stmt, target_name, column, rows, depends, comments)

        # 2) 业务规则：过滤 / 分区 / JOIN
        for cond in stmt.get("filters") or []:
            self._add_rule(source_label, stmt, cond, target_name, rule_type="过滤规则",
                           comment=self._match_where_comment(cond, comments))
        for key, value in (stmt.get("partition_filters") or {}).items():
            self._add_rule(source_label, stmt, f"{key} = '{value}'", target_name,
                           rule_type="分区规则",
                           description=f"分区/时点条件：{key} = {value}")
        for join in stmt.get("joins") or []:
            on = join.get("on")
            if not on:
                continue
            self._add_rule(source_label, stmt, on, target_name, rule_type="关联规则",
                           description=f"{join.get('left')} {join.get('type')} {join.get('right')} ON {on}")

    # -- 字段登记 ---------------------------------------------------------- #
    def _register_field(
        self,
        table_name: str,
        column: str,
        role: str,
        expression: str,
        comments: SqlComments,
        source_label: str,
    ) -> FieldKnowledge:
        key = (table_name, column)
        layer = _layer_of(table_name)
        comment = comments.column_comments.get(column.lower())
        hit = self.resolver.resolve(column, comment)
        _clean, unit = split_comment_unit(comment)
        existing = self._fields.get(key)
        if existing is None:
            existing = FieldKnowledge(
                table_name=table_name,
                column_name=column,
                chinese_name=hit.chinese_name,
                chinese_source=hit.source,
                confidence=hit.confidence,
                unit=unit,
                layer=layer,
                role=role,
                business_desc=_clip(comment or "", 200),
                sample_expression=normalize_expression(expression),
                source_files=[source_label],
            )
            self._fields[key] = existing
        else:
            # 置信度更高的来源胜出（注释 > 词典 > 规则 > 待确认）
            if hit.confidence > existing.confidence or (
                existing.chinese_name is None and hit.chinese_name
            ):
                existing.chinese_name = hit.chinese_name
                existing.chinese_source = hit.source
                existing.confidence = hit.confidence
            if existing.unit is None:
                existing.unit = unit
            if not existing.business_desc and comment:
                existing.business_desc = _clip(comment, 200)
            if not existing.sample_expression:
                existing.sample_expression = normalize_expression(expression)
            if source_label not in existing.source_files:
                existing.source_files.append(source_label)
            existing.role = "both" if existing.role != role else role
        if hit.chinese_name and self._name_map.get(column.lower()) is None:
            self._name_map[column.lower()] = hit.chinese_name
        return existing

    # -- 口径提炼 ---------------------------------------------------------- #
    def _maybe_metric(
        self,
        source_label: str,
        stmt: Dict[str, Any],
        target_name: str,
        column: str,
        rows: Sequence[Dict[str, Any]],
        depends: Sequence[Tuple[str, str]],
        comments: SqlComments,
    ) -> None:
        expression = (rows[0].get("expression") or "").strip()
        if not expression:
            return
        normalized = normalize_expression(expression)
        if not normalized:
            return
        metric_type, agg = classify_expression(normalized)
        if metric_type == "直取":
            return  # 直取字段不构成新口径

        field = self._fields.get((target_name, column))
        chinese = field.chinese_name if field else None
        chinese_source = field.chinese_source if field else "pending"
        unit = field.unit if field else None

        # 依赖字段的中文名
        dep_list: List[Dict[str, Any]] = []
        unresolved = 0
        for src_table, src_col in depends:
            dep_field = self._fields.get((src_table, src_col))
            dep_cn = dep_field.chinese_name if dep_field else self._name_map.get(src_col.lower())
            if not dep_cn:
                unresolved += 1
            dep_list.append({
                "table": src_table or None,
                "column": src_col,
                "chinese_name": dep_cn,
            })
        if not dep_list:
            dep_list = [{"table": None, "column": CONSTANT_MARKER, "chinese_name": "常量"}]

        name_map = dict(self._name_map)
        name_map[column.lower()] = chinese or column
        for dep in dep_list:
            if dep["chinese_name"] and dep["column"] != CONSTANT_MARKER:
                name_map.setdefault(str(dep["column"]).lower(), dep["chinese_name"])

        body = strip_uniform_aggregates(normalized)
        # 只有当剥离聚合后「还保留了计算关系」（含有运算符）才用口径本体，
        # 否则 SUM(x) / AVG(x) 这类会退化成 `产量 = 产量`，宁可显示 `SUM(产量)`。
        if not body or not re.search(r"[+\-*/<>]", body):
            body = normalized
        readable_en = localize_expression(body, name_map)
        full_en = localize_expression(normalized, name_map)
        label = chinese or column
        formula = f"{label} = {readable_en}"
        formula_full = f"{label} = {full_en}"

        comment = comments.column_comments.get(column.lower())
        note_bits: List[str] = []
        if comment and comment != chinese:
            note_bits.append(f"脚本注释：{comment}")
        if body != normalized and agg:
            note_bits.append(f"口径本体已剥离一致的聚合函数 {agg}()（共 {len(depends)} 个依赖字段）")
        if unresolved:
            note_bits.append(f"{unresolved} 个依赖字段未能确定中文名/来源表")

        confidence = 0.9
        if chinese_source == "pending":
            confidence -= 0.15
        if unresolved:
            confidence -= min(0.2, 0.1 * unresolved)
        if metric_type in ("窗口函数", "条件分支"):
            confidence -= 0.05
        confidence = round(max(0.3, min(0.97, confidence)), 2)

        metric = MetricKnowledge(
            metric_name=column,
            table_name=target_name,
            chinese_name=chinese,
            chinese_source=chinese_source,
            layer=_layer_of(target_name),
            metric_type=metric_type,
            aggregate_func=agg,
            functions=detect_functions(normalized),
            expression_raw=expression,
            expression_normalized=normalized,
            formula=formula,
            formula_full=formula_full,
            depends_on=dep_list,
            source_file=source_label,
            source_stmt=int(stmt.get("statement_index") or 1),
            source_task_type=stmt.get("task_type") or "",
            input_tables=list(stmt.get("input_table_names") or []),
            confidence=confidence,
            notes="；".join(note_bits),
            unit=unit,
        )
        self._metrics.append(metric)

    # -- 规则 -------------------------------------------------------------- #
    def _match_where_comment(self, cond: str, comments: SqlComments) -> str:
        for code, comment in comments.inline_notes:
            if code and cond and cond.strip()[:20] in code:
                return comment
        return ""

    def _add_rule(
        self,
        source_label: str,
        stmt: Dict[str, Any],
        expression: str,
        table_name: str,
        rule_type: str = "过滤规则",
        description: str = "",
        comment: str = "",
    ) -> None:
        expression = (expression or "").strip()
        if not expression and not description:
            return
        desc = description or _describe_filter(expression)
        key = _hash_key(source_label, rule_type, table_name, expression, desc)
        if key in self._rules:
            return
        self._rules[key] = RuleKnowledge(
            rule_key=key,
            rule_type=rule_type,
            description=desc,
            expression=_clip(expression, 400),
            table_name=table_name,
            layer=_layer_of(table_name),
            source_file=source_label,
            source_stmt=int(stmt.get("statement_index") or 1),
            source_comment=_clip(comment, 200),
        )

    def add_comment_rules(self, source_label: str, comments: SqlComments,
                          table_name: str = "") -> None:
        """从带规则关键词的行内注释里提炼业务规则（如「仅统计有效扫码」）。"""
        for code, comment in comments.inline_notes:
            if not comment:
                continue
            if any(re.search(kw, comment) for kw in RULE_KEYWORDS):
                self._add_rule(source_label, {}, code, table_name, rule_type="业务规则（注释）",
                               description=f"{comment}（脚本注释）", comment=comment)

    # -- 术语 -------------------------------------------------------------- #
    def _ensure_table(self, name: str, layer: str = "") -> TableKnowledge:
        tbl = self._tables.get(name)
        if tbl is None:
            tbl = TableKnowledge(table_name=name, short_name=name.split(".")[-1], layer=layer or _layer_of(name))
            self._tables[name] = tbl
        elif layer and not tbl.layer:
            tbl.layer = layer
        return tbl

    def _build_terms(self) -> None:
        """汇总业务术语：内置词典 → 注释 → 规则推断 → 待确认。"""
        for term, chinese in self.glossary.columns.items():
            self._terms[term] = TermKnowledge(
                term=term, chinese_name=chinese, category="field", source="builtin",
                confidence=1.0, domain=", ".join(self.glossary.domains[:1]),
            )
        for term, chinese in self.glossary.tables.items():
            self._terms.setdefault(term, TermKnowledge(
                term=term, chinese_name=chinese, category="table", source="builtin",
                confidence=1.0, domain=", ".join(self.glossary.domains[:1]),
            ))

        for field in self._fields.values():
            key = field.column_name.lower()
            term = self._terms.get(key)
            if term is None:
                term = TermKnowledge(term=key, category="field")
                self._terms[key] = term
            if field.chinese_name:
                if term.source in ("pending", "") or field.confidence > term.confidence:
                    term.chinese_name = field.chinese_name
                    term.source = field.chinese_source
                    term.confidence = field.confidence
            if field.table_name not in term.tables:
                term.tables.append(field.table_name)
            term.occurrences += 1

        for table in self._tables.values():
            if not table.chinese_name:
                continue
            key = table.short_name.lower()
            term = self._terms.get(key)
            if term is None:
                term = TermKnowledge(term=key, category="table", source=table.chinese_source,
                                     chinese_name=table.chinese_name, confidence=0.9)
                self._terms[key] = term
            for alias in (table.table_name, table.short_name):
                if alias not in term.aliases:
                    term.aliases.append(alias)


def _layer_of(table_name: str) -> str:
    """从表名推断数仓分层（复用 P2 的 ``graph.table_layer``：dwd_/dws_ 前缀也认）。"""
    from ..graph import table_layer

    return table_layer(table_name or "")


def _describe_filter(expression: str) -> str:
    """把过滤条件翻译成一句人话。"""
    expr = (expression or "").strip()
    upper = expr.upper()
    if not expr:
        return "过滤条件"
    if "IS NOT NULL" in upper:
        return f"空值过滤：{_shorten(expr)}（该字段为空视为脏数据）"
    if re.search(r"(DT|PT|DS|DATE|MONTH)\s*=", upper):
        return f"时点/分区过滤：{_shorten(expr)}"
    if re.search(r"(DT|PT|DS|DATE)\s*>", upper) or re.search(r"(DT|PT|DS|DATE)\s*<", upper):
        return f"时间范围过滤：{_shorten(expr)}"
    if re.search(r"\bIN\s*\(", upper):
        return f"枚举过滤：{_shorten(expr)}"
    if re.search(r"<>|!=", expr):
        return f"取值排除：{_shorten(expr)}"
    if re.search(r"(STATUS|TYPE|FLAG|LEVEL)\s*=", upper):
        return f"状态过滤：{_shorten(expr)}"
    return f"过滤条件：{_shorten(expr)}"


def _shorten(text: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
