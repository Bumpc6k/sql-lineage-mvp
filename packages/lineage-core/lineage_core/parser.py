"""SQL 血缘解析核心模块（P1：SQL 静态解析 + 血缘提取）。

设计说明
--------
本模块只做 **静态解析**：把 SQL 文本交给 sqlglot 解析成 AST，然后自底向上
遍历 AST 提取血缘，全过程不连接数据库、不读取元数据、不执行 SQL。

支持的 SQL 形态（见 README「支持的 SQL 形态」）：
1. ``INSERT [OVERWRITE|INTO] TABLE t [PARTITION (...)] SELECT ...``
2. ``CREATE TABLE t AS SELECT ...``（CTAS，含 IF NOT EXISTS）
3. 多表 JOIN（INNER / LEFT / RIGHT / FULL / CROSS / SEMI / ANTI）
4. 带别名 / 带库名（``FROM dwd_order a JOIN dim_org b ON ...``）
5. 派生表子查询（``FROM (SELECT ...) t``）
6. CTE（``WITH x AS (SELECT ...) SELECT ... FROM x``），支持多层嵌套
7. ``UNION`` / ``UNION ALL``（含多分支、嵌套 set operation）
8. 分区过滤条件提取（``PARTITION (dt='2026-01-01')`` 与 ``WHERE dt='...'``）

已知限制（如实说明，不做粉饰）：
* 字段级血缘是 **语法级推导**，不是语义级推导，不依赖元数据：
  - 无法展开 ``SELECT *``（没有表结构），此时会输出 ``source_column='*'`` 且
    ``resolved=False`` 的记录；
  - 无法处理 ``INSERT INTO t SELECT * FROM a`` 之外的列位置插入语义；
  - 输出表达式中嵌套的标量子查询内部的列 **不** 参与该字段的解析（会跳过），
    避免把子查询的列错误归到外层；
  - ``LATERAL VIEW`` / ``explode`` / UDTF、``WINDOW`` 语句、动态分区
    （``PARTITION (dt)``）只做尽力而为的解析；
  - 同名字段（如多表都有 ``id`` 且未加别名）无法在语法层面消歧，会输出
    ``source_table=None`` + ``resolved=False``；
  - 仅做语法去重，不会做函数语义展开（``SUM(a.qty)`` 只记到 ``a.qty``，
    不会下推到更细粒度）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple, Union as TypingUnion

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

__version__ = "0.1.0"

#: 默认方言（Hive SQL / Spark SQL 语法高度兼容）
DEFAULT_DIALECT = "hive"

#: 任务类型枚举
TASK_INSERT_SELECT = "INSERT_SELECT"
TASK_CTAS = "CTAS"
TASK_SELECT = "SELECT"
TASK_INSERT_VALUES = "INSERT_VALUES"
TASK_OTHER = "OTHER"

#: 被识别成分区字段的列名（小写），用于把 WHERE 谓词归类成分区过滤
PARTITION_KEY_HINTS: Set[str] = {
    "dt",
    "pt",
    "ds",
    "day",
    "date",
    "log_date",
    "stat_date",
    "stat_dt",
    "statis_date",
    "month",
    "mon",
    "year",
    "hour",
    "hh",
    "分区",
}

#: 递归解析的最大深度（防止自引用 CTE / 异常 SQL 造成死循环）
MAX_DEPTH = 8

#: 常量来源标记：输出字段由字面量/无上游列的函数（如 COUNT(1)、'自产'）产生
CONSTANT_MARKER = "(常量)"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TableRef:
    """一张物理表的引用：库名 / 表名（catalog 可选）。"""

    name: str
    schema: Optional[str] = None
    catalog: Optional[str] = None

    @property
    def full_name(self) -> str:
        return ".".join(p for p in (self.catalog, self.schema, self.name) if p)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "schema": self.schema, "catalog": self.catalog}

    @classmethod
    def from_ast(cls, node: exp.Table) -> "TableRef":
        """从 sqlglot 的 ``exp.Table`` 节点构造 TableRef。"""
        return cls(
            name=node.name,
            schema=node.db or None,
            catalog=node.catalog or None,
        )


@dataclass
class Source:
    """作用域里的一个数据来源：物理表，或者派生表（子查询 / CTE）。"""

    key: str                                   # 别名或表名（小写，用作索引）
    raw_name: str                              # 原始标识符（保留大小写）
    kind: str                                  # "table" | "derived"
    table_ref: Optional[TableRef] = None       # kind == "table"
    query: Optional[exp.Expression] = None     # kind == "derived"：内部查询
    cte: bool = False                          # 是否来自 CTE
    alias: Optional[str] = None

    @property
    def display_name(self) -> str:
        if self.kind == "table" and self.table_ref is not None:
            return self.table_ref.full_name
        return self.alias or self.raw_name


def _is_set_op(node: exp.Expression) -> bool:
    """判断是否是 UNION / UNION ALL / INTERSECT / EXCEPT 这类集合运算。"""
    types: List[type] = [exp.Union]
    for name in ("Intersect", "Except", "SetOperation"):
        t = getattr(exp, name, None)
        if isinstance(t, type):
            types.append(t)
    return isinstance(node, tuple(types))


def _flatten_set_op(node: exp.Expression) -> List[exp.Expression]:
    """把 UNION/UNION ALL 展平成按顺序排列的分支列表（递归处理嵌套 set op）。"""
    if not _is_set_op(node):
        return [node]
    out: List[exp.Expression] = []
    for side in (node.this, node.expression):
        if side is None:
            continue
        out.extend(_flatten_set_op(side))
    return out


# --------------------------------------------------------------------------- #
# 作用域（FROM 子句解析结果）
# --------------------------------------------------------------------------- #
class Scope:
    """一个 SELECT 的 FROM/JOIN 作用域：别名 -> Source 的映射。

    CTE 名称会被解析成 ``kind="derived"`` 的 Source（内部查询即 CTE 的 SELECT），
    因此对 CTE 的字段引用可以被继续下推到真实物理表。
    """

    def __init__(
        self,
        select: exp.Expression,
        cte_map: Dict[str, exp.Expression],
        depth: int = 0,
    ) -> None:
        self.select = select
        self.cte_map = cte_map
        self.depth = depth
        self.sources: Dict[str, Source] = {}
        self._build()

    # -- 构建 -------------------------------------------------------------- #
    def _build(self) -> None:
        for node in self._from_and_join_nodes(self.select):
            self._add_source(node)

    @staticmethod
    def _from_and_join_nodes(select: exp.Expression) -> List[exp.Expression]:
        nodes: List[exp.Expression] = []
        if not isinstance(select, exp.Expression):
            return nodes
        from_ = select.args.get("from_") if hasattr(select, "args") else None
        if from_ is not None and from_.this is not None:
            nodes.append(from_.this)
        for join in select.args.get("joins") or []:
            if join.this is not None:
                nodes.append(join.this)
        return nodes

    def _add_source(self, node: exp.Expression) -> None:
        node = _unwrap(node)
        if node is None:
            return
        alias = node.alias or None

        if isinstance(node, exp.Subquery):
            inner = node.this
            key = (alias or "").lower()
            if key:
                self.sources[key] = Source(
                    key=key, raw_name=alias or "", kind="derived", query=inner, alias=alias
                )
            return

        if isinstance(node, exp.Table):
            table_ref = TableRef.from_ast(node)
            name = table_ref.name
            cte_select = self.cte_map.get(name.lower())
            if cte_select is not None:
                # CTE：当作派生表处理，字段可以继续往真实表下推
                key = (alias or name).lower()
                self.sources[key] = Source(
                    key=key,
                    raw_name=name,
                    kind="derived",
                    query=cte_select,
                    cte=True,
                    alias=alias or name,
                )
                # 未加别名时，表名本身也可作为限定符使用
                if alias:
                    self.sources.setdefault(name.lower(), self.sources[key])
                return

            key = (alias or name).lower()
            self.sources[key] = Source(
                key=key,
                raw_name=alias or name,
                kind="table",
                table_ref=table_ref,
                alias=alias,
            )
            if alias:
                self.sources.setdefault(name.lower(), self.sources[key])
            return

        # 其他形态（如 UNNEST / LATERAL VIEW / VALUES）尽力而为，直接忽略
        return

    # -- 查询 -------------------------------------------------------------- #
    def get(self, qualifier: str) -> Optional[Source]:
        return self.sources.get((qualifier or "").lower())

    def all_sources(self) -> List[Source]:
        """去重后的来源列表（同一物理表可能因别名注册两次）。"""
        seen: Set[int] = set()
        out: List[Source] = []
        for src in self.sources.values():
            if id(src) in seen:
                continue
            seen.add(id(src))
            out.append(src)
        return out


def _unwrap(node: Optional[exp.Expression]) -> Optional[exp.Expression]:
    """剥掉 Paren / Alias / Lateral 等包装，拿到真正的表或子查询节点。"""
    guard = 0
    while isinstance(node, exp.Expression) and guard < 10:
        guard += 1
        if isinstance(node, exp.Subquery):
            return node
        if isinstance(node, (exp.Table,)) or _is_set_op(node):
            return node
        for attr in ("this",):
            inner = node.args.get(attr)
            if (
                isinstance(node, (exp.Paren, exp.Lateral, exp.Alias, exp.TableAlias, exp.Subquery))
                and isinstance(inner, exp.Expression)
            ):
                node = inner
                break
        else:
            break
    return node


# --------------------------------------------------------------------------- #
# 主解析器
# --------------------------------------------------------------------------- #
class SqlLineageParser:
    """SQL 血缘解析器。

    用法::

        parser = SqlLineageParser(dialect="hive")
        results = parser.parse_sql(sql_text)      # 每条语句一个 dict
        report = parser.summary_text(results)     # 终端可读文本摘要

    参数：
        dialect: sqlglot 方言名，Hive SQL 用 ``hive``，Spark SQL 用 ``spark``，
            也支持 ``postgres`` / ``mysql`` / ``doris`` / ``starrocks`` 等。
        default_schema: 预留：当 SQL 中未写库名时补充的默认库（当前仅用于
            输出提示，不做改写）。
    """

    def __init__(self, dialect: str = DEFAULT_DIALECT, default_schema: Optional[str] = None) -> None:
        self.dialect = dialect
        self.default_schema = default_schema

    # ------------------------------------------------------------------ #
    # 对外 API
    # ------------------------------------------------------------------ #
    def parse_sql(self, sql: str, source: Optional[str] = None) -> List[Dict[str, Any]]:
        """解析一段可能包含多条语句的 SQL，返回每条语句的血缘结果。"""
        if sql is None or not sql.strip():
            return []
        try:
            statements = [s for s in sqlglot.parse(sql, read=self.dialect) if s is not None]
        except ParseError as exc:  # pragma: no cover - 依赖具体方言
            raise ValueError(f"SQL 解析失败（dialect={self.dialect}）：{exc}") from exc

        results: List[Dict[str, Any]] = []
        for idx, statement in enumerate(statements, start=1):
            result = self.analyze_statement(statement, statement_index=idx)
            result["source"] = source
            results.append(result)
        return results

    def parse_file(self, path: TypingUnion[str, os.PathLike]) -> List[Dict[str, Any]]:
        """解析单个 SQL 文件。"""
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        return self.parse_sql(text, source=str(p))

    def parse_files(self, paths: Sequence[TypingUnion[str, os.PathLike]]) -> Dict[str, Any]:
        """解析多个 SQL 文件并聚合成一份报告（供 CLI 使用）。

        返回结构::

            {
              "dialect": "hive",
              "file_count": 2,
              "statement_count": 5,
              "input_tables": [...],           # 全量去重
              "output_tables": [...],
              "table_lineage": [...],          # 全量去重
              "column_lineage": [...],
              "statements": [ {单条语句结果}, ... ]
            }
        """
        statements: List[Dict[str, Any]] = []
        for path in paths:
            statements.extend(self.parse_file(path))
        return self.aggregate(statements)

    def aggregate(self, statements: List[Dict[str, Any]]) -> Dict[str, Any]:
        """把多条语句结果聚合成一份报告（去重表级血缘 / 表清单）。"""
        inputs: Dict[str, Dict[str, Any]] = {}
        outputs: Dict[str, Dict[str, Any]] = {}
        pairs: Dict[Tuple[str, str], Dict[str, str]] = {}
        columns: List[Dict[str, Any]] = []
        seen_col: Set[Tuple[Any, ...]] = set()

        def _full_name(t: Dict[str, Any]) -> str:
            return ".".join(p for p in (t.get("catalog"), t.get("schema"), t.get("name")) if p)

        for stmt in statements:
            for t in stmt.get("input_tables", []):
                inputs.setdefault(_full_name(t), t)
            for t in stmt.get("output_tables", []):
                outputs.setdefault(_full_name(t), t)
            for pair in stmt.get("table_lineage", []):
                pairs.setdefault((pair["source"], pair["target"]), pair)
            for col in stmt.get("column_lineage", []):
                key = (
                    col.get("target_table"),
                    col.get("target_column"),
                    col.get("source_table"),
                    col.get("source_column"),
                    col.get("expression"),
                )
                if key in seen_col:
                    continue
                seen_col.add(key)
                columns.append(col)

        files = sorted({s["source"] for s in statements if s.get("source")})
        return {
            "dialect": self.dialect,
            "file_count": len(files),
            "files": files,
            "statement_count": len(statements),
            "input_tables": list(inputs.values()),
            "input_table_names": list(inputs.keys()),
            "output_tables": list(outputs.values()),
            "output_table_names": list(outputs.keys()),
            "table_lineage": [pairs[k] for k in sorted(pairs)],
            "column_lineage": columns,
            "statements": statements,
        }

    # ------------------------------------------------------------------ #
    # 单条语句分析
    # ------------------------------------------------------------------ #
    def analyze_statement(self, statement: exp.Expression, statement_index: int = 1) -> Dict[str, Any]:
        """分析一条语句，返回血缘结果 dict（结构见 README）。"""
        cte_map = self._collect_ctes(statement)
        task_type, output_table, query = self._classify(statement)

        input_refs: List[TableRef] = []
        if query is not None:
            input_refs = self._collect_physical_tables(query, cte_map)

        output_refs: List[TableRef] = [output_table] if output_table is not None else []

        table_lineage: List[Dict[str, str]] = []
        if output_table is not None:
            for ref in input_refs:
                table_lineage.append(
                    {"source": ref.full_name, "target": output_table.full_name}
                )

        column_lineage = self._collect_column_lineage(query, output_table, cte_map) if query is not None else []

        return {
            "statement_index": statement_index,
            "task_type": task_type,
            "dialect": self.dialect,
            "output_tables": [t.to_dict() for t in output_refs],
            "output_table_names": [t.full_name for t in output_refs],
            "input_tables": [t.to_dict() for t in input_refs],
            "input_table_names": [t.full_name for t in input_refs],
            "table_lineage": table_lineage,
            "column_lineage": column_lineage,
            "filters": self._collect_filters(statement),
            "partition_filters": self._collect_partition_filters(statement),
            "joins": self._collect_joins(query) if query is not None else [],
            "sql": statement.sql(dialect=self.dialect, comments=False),
        }

    # -- 语句分类 ---------------------------------------------------------- #
    def _classify(
        self, statement: exp.Expression
    ) -> Tuple[str, Optional[TableRef], Optional[exp.Expression]]:
        """返回 (task_type, 输出表, 供血缘分析的查询节点)。"""
        if isinstance(statement, exp.Insert):
            target = statement.this
            ref = TableRef.from_ast(target) if isinstance(target, exp.Table) else None
            body = statement.expression
            if body is None or isinstance(body, exp.Values):
                return TASK_INSERT_VALUES, ref, None
            return TASK_INSERT_SELECT, ref, body

        if isinstance(statement, exp.Create):
            kind = getattr(statement.args.get("kind"), "value", statement.args.get("kind"))
            kind = str(kind).upper() if kind else ""
            body = statement.expression
            if kind == "TABLE" and body is not None and self._is_query(body):
                target = statement.this
                ref = TableRef.from_ast(target) if isinstance(target, exp.Table) else None
                return TASK_CTAS, ref, body
            return TASK_OTHER, None, None

        if self._is_query(statement):
            return TASK_SELECT, None, statement

        return TASK_OTHER, None, None

    @staticmethod
    def _is_query(node: Optional[exp.Expression]) -> bool:
        if node is None:
            return False
        if isinstance(node, exp.Subquery):
            node = node.this
        return isinstance(node, exp.Select) or _is_set_op(node)

    # -- CTE --------------------------------------------------------------- #
    @staticmethod
    def _collect_ctes(statement: exp.Expression) -> Dict[str, exp.Expression]:
        """收集语句中所有 CTE 定义：名称(小写) -> SELECT 节点。

        会同时收集嵌套在内层的 WITH（``SELECT * FROM (WITH x AS ...) t``），
        同名 CTE 以先出现的为准。
        """
        cte_map: Dict[str, exp.Expression] = {}
        for with_node in statement.find_all(exp.With):
            for cte in with_node.expressions:
                name = cte.alias
                if not name:
                    continue
                cte_map.setdefault(name.lower(), cte.this)
        return cte_map

    # -- 物理表收集 -------------------------------------------------------- #
    def _collect_physical_tables(
        self,
        query: exp.Expression,
        cte_map: Dict[str, exp.Expression],
        depth: int = 0,
        acc: Optional[List[TableRef]] = None,
    ) -> List[TableRef]:
        """递归收集查询用到的所有物理表（CTE 会被展开到真实表）。"""
        if acc is None:
            acc = []
        if depth > MAX_DEPTH or query is None:
            return acc

        if _is_set_op(query):
            for branch in _flatten_set_op(query):
                self._collect_physical_tables(branch, cte_map, depth + 1, acc)
            return acc

        scope = Scope(query, cte_map, depth=depth)
        for src in scope.all_sources():
            if src.kind == "table" and src.table_ref is not None:
                if src.table_ref.full_name not in [t.full_name for t in acc]:
                    acc.append(src.table_ref)
            elif src.kind == "derived" and src.query is not None:
                self._collect_physical_tables(src.query, cte_map, depth + 1, acc)
        return acc

    # -- 字段级血缘 -------------------------------------------------------- #
    def _collect_column_lineage(
        self,
        query: exp.Expression,
        output_table: Optional[TableRef],
        cte_map: Dict[str, exp.Expression],
    ) -> List[Dict[str, Any]]:
        """构建字段级血缘：[{target_table,target_column,source_table,source_column,expression}]。"""
        target_name = output_table.full_name if output_table else "(query_result)"
        rows: List[Dict[str, Any]] = []
        seen: Set[Tuple[Any, ...]] = set()

        branches = _flatten_set_op(query)
        # 目标列名以第一个分支为准（UNION 各分支按位置对齐）
        first = branches[0] if branches else None
        target_names: List[str] = []
        if isinstance(first, exp.Select):
            target_names = [e.alias_or_name or f"col_{i+1}" for i, e in enumerate(first.expressions)]

        for branch_index, branch in enumerate(branches, start=1):
            if not isinstance(branch, exp.Select):
                continue
            scope = Scope(branch, cte_map)
            for pos, expr in enumerate(branch.expressions):
                if isinstance(first, exp.Select) and branch_index > 1 and pos < len(target_names):
                    col_name = target_names[pos]
                else:
                    col_name = expr.alias_or_name or f"col_{pos+1}"

                expression_sql = expr.sql(dialect=self.dialect, comments=False)
                sources = self._resolve_expression(expr, scope, cte_map)
                if not sources and not self._own_columns(expr):
                    # 常量表达式（COUNT(1) / '自产' / 纯字面量）没有上游字段
                    sources = [(None, CONSTANT_MARKER, True)]
                for src_table, src_col, resolved in sources:
                    key = (target_name, col_name, src_table, src_col, expression_sql)
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(
                        {
                            "target_table": target_name,
                            "target_column": col_name,
                            "source_table": src_table,
                            "source_column": src_col,
                            "expression": expression_sql,
                            "resolved": resolved,
                        }
                    )
        return rows

    def _resolve_expression(
        self,
        expr: exp.Expression,
        scope: Scope,
        cte_map: Dict[str, exp.Expression],
        depth: int = 0,
    ) -> List[Tuple[Optional[str], str, bool]]:
        """解析一个输出表达式里引用的所有源字段。

        返回 [(source_table, source_column, resolved)]，``resolved=False`` 表示
        语法层面无法确定来源（例如 ``SELECT *``、同名字段、未知别名）。
        """
        if depth > MAX_DEPTH:
            return []
        # Star：SELECT *
        if isinstance(expr, exp.Star):
            return [
                (src.display_name, "*", False) for src in scope.all_sources()
            ] or [(None, "*", False)]
        if isinstance(expr, exp.Column) and isinstance(expr.this, exp.Star):
            src = scope.get(expr.table)
            return [(src.display_name if src else expr.table, "*", False)]

        out: List[Tuple[Optional[str], str, bool]] = []
        for column in self._own_columns(expr):
            out.extend(self._resolve_column(column, scope, cte_map, depth))
        return out

    @staticmethod
    def _own_columns(expr: exp.Expression) -> List[exp.Column]:
        """取出表达式自身（不含嵌套子查询）引用的 Column 节点。"""
        if isinstance(expr, exp.Column):
            return [expr]

        def _prune(node: exp.Expression) -> bool:
            # 标量子查询有自己的作用域，不参与外层字段解析
            return isinstance(node, exp.Subquery) or _is_set_op(node)

        cols: List[exp.Column] = []
        for node in expr.walk(prune=_prune):
            if node is expr:
                continue
            if isinstance(node, exp.Column):
                cols.append(node)
        return cols

    def _resolve_column(
        self,
        column: exp.Column,
        scope: Scope,
        cte_map: Dict[str, exp.Expression],
        depth: int = 0,
    ) -> List[Tuple[Optional[str], str, bool]]:
        """把一个 Column 解析成 [(物理表, 物理字段, resolved)]。"""
        qualifier = column.table
        col_name = column.name

        if qualifier:
            src = scope.get(qualifier)
            if src is None:
                # 未知别名：保留原始限定符，标记未解析
                return [(qualifier, col_name, False)]
            return self._resolve_from_source(src, col_name, cte_map, depth)

        sources = scope.all_sources()
        if len(sources) == 1:
            return self._resolve_from_source(sources[0], col_name, cte_map, depth)

        # 未限定且多来源：尝试在派生表中找到唯一命中
        hits: List[Tuple[Optional[str], str, bool]] = []
        for src in sources:
            if src.kind == "derived" and src.query is not None:
                inner = self._lookup_output_column(src.query, col_name)
                if inner:
                    hits.extend(
                        self._resolve_from_source(src, col_name, cte_map, depth)
                    )
        distinct = {h[0] for h in hits}
        if len(distinct) == 1:
            return hits
        # 语法层面无法消歧（例如多张表都有同名字段）
        return [(None, col_name, False)]

    def _resolve_from_source(
        self,
        src: Source,
        col_name: str,
        cte_map: Dict[str, exp.Expression],
        depth: int,
    ) -> List[Tuple[Optional[str], str, bool]]:
        """从某个来源（物理表 / 派生表）解析字段，派生表会继续下推。"""
        if src.kind == "table" and src.table_ref is not None:
            return [(src.table_ref.full_name, col_name, True)]

        if src.kind == "derived" and src.query is not None:
            if col_name == "*":
                return [(src.display_name, "*", False)]
            return self._resolve_output_column(
                src.query, col_name, cte_map, src.display_name, depth + 1
            )

        return [(src.display_name, col_name, False)]

    def _resolve_output_column(
        self,
        query: exp.Expression,
        col_name: str,
        cte_map: Dict[str, exp.Expression],
        unknown_qualifier: Optional[str] = None,
        depth: int = 0,
    ) -> List[Tuple[Optional[str], str, bool]]:
        """在（子查询 / CTE 的）输出列里找到 col_name 并继续向下解析。"""
        if depth > MAX_DEPTH or query is None:
            return [(unknown_qualifier, col_name, False)]

        branches = _flatten_set_op(query)
        out: List[Tuple[Optional[str], str, bool]] = []
        for branch in branches:
            if not isinstance(branch, exp.Select):
                continue
            scope = Scope(branch, cte_map)
            matched = False
            for e in branch.expressions:
                if isinstance(e, exp.Star) or (
                    isinstance(e, exp.Column) and isinstance(e.this, exp.Star)
                ):
                    continue
                if (e.alias_or_name or "") == col_name:
                    matched = True
                    sub = self._resolve_expression(e, scope, cte_map, depth + 1)
                    if sub:
                        out.extend(sub)
                    elif not self._own_columns(e):
                        out.append((None, CONSTANT_MARKER, True))
            if not matched and self._has_star(branch):
                # 子查询用 SELECT *，语法层面无法确定列的来源
                for src in scope.all_sources():
                    out.append((src.display_name, col_name, False))
        if not out:
            out = [(unknown_qualifier, col_name, False)]
        return out

    @staticmethod
    def _has_star(select: exp.Expression) -> bool:
        for e in select.expressions:
            if isinstance(e, exp.Star):
                return True
            if isinstance(e, exp.Column) and isinstance(e.this, exp.Star):
                return True
        return False

    @staticmethod
    def _lookup_output_column(query: exp.Expression, col_name: str) -> bool:
        """判断某个查询的输出列里是否存在 col_name。"""
        for branch in _flatten_set_op(query):
            if not isinstance(branch, exp.Select):
                continue
            for e in branch.expressions:
                if (e.alias_or_name or "") == col_name:
                    return True
        return False

    # -- 过滤条件 ---------------------------------------------------------- #
    def _collect_filters(self, statement: exp.Expression) -> List[str]:
        """提取所有 WHERE / HAVING / QUALIFY 条件文本（去重、保序、含子查询内条件）。

        JOIN 的 ON 条件不算过滤条件，单独由 ``joins`` 字段给出。
        """
        out: List[str] = []
        clause_types = (exp.Where, exp.Having, exp.Qualify)
        for node in statement.find_all(*clause_types):
            cond = node.this
            if cond is None:
                continue
            text = cond.sql(dialect=self.dialect, comments=False)
            if text not in out:
                out.append(text)
        return out

    def _collect_partition_filters(self, statement: exp.Expression) -> Dict[str, Any]:
        """提取分区过滤：PARTITION 子句 + WHERE 中的分区字段等值/IN 谓词。"""
        filters: Dict[str, Any] = {}

        # 1) INSERT OVERWRITE TABLE t PARTITION (dt='...')
        for table in statement.find_all(exp.Table):
            partition = table.args.get("partition")
            if partition is None:
                continue
            for pred in partition.expressions:
                key, value = self._literal_predicate(pred)
                if key:
                    filters[key] = value

        # 2) WHERE 里的分区字段谓词
        for where in statement.find_all(exp.Where):
            if where.this is None:
                continue
            for pred in self._iter_predicates(where.this):
                key, value = self._literal_predicate(pred)
                if key and key.lower() in PARTITION_KEY_HINTS:
                    filters.setdefault(key, value)
        return filters

    @staticmethod
    def _iter_predicates(node: exp.Expression) -> Iterator[exp.Expression]:
        """展开 AND 连接，取出每个谓词。"""
        if isinstance(node, exp.And):
            yield from SqlLineageParser._iter_predicates(node.this)
            yield from SqlLineageParser._iter_predicates(node.expression)
        else:
            yield node

    def _literal_predicate(self, pred: exp.Expression) -> Tuple[Optional[str], Any]:
        """把 ``col = 'x'`` / ``col IN ('x','y')`` 解析成 (列名, 值)。"""
        if isinstance(pred, exp.EQ):
            left, right = pred.this, pred.expression
            if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
                return left.name, right.this
            if isinstance(right, exp.Column) and isinstance(left, exp.Literal):
                return right.name, left.this
        if isinstance(pred, exp.In):
            col = pred.this
            if isinstance(col, exp.Column):
                values = []
                for e in pred.expressions:
                    if isinstance(e, exp.Literal):
                        values.append(e.this)
                if values:
                    return col.name, values
        return None, None

    # -- JOIN 信息 --------------------------------------------------------- #
    def _collect_joins(self, query: exp.Expression) -> List[Dict[str, Any]]:
        """提取 JOIN 关系（类型 / 左表 / 右表 / ON 条件），用于终端摘要展示。"""
        out: List[Dict[str, Any]] = []
        for branch in _flatten_set_op(query):
            if not isinstance(branch, exp.Select):
                continue
            from_node = branch.args.get("from_")
            left = _unwrap(from_node.this) if from_node is not None else None
            left_name = self._node_name(left)
            for join in branch.args.get("joins") or []:
                right = _unwrap(join.this)
                side = (join.side or "").upper()
                kind = (join.args.get("kind") or "").upper()
                label = " ".join(p for p in (side, kind, "JOIN") if p)
                on = join.args.get("on")
                out.append(
                    {
                        "type": label,
                        "left": left_name,
                        "right": self._node_name(right),
                        "on": on.sql(dialect=self.dialect, comments=False)
                        if on is not None
                        else None,
                    }
                )
        return out

    @staticmethod
    def _node_name(node: Optional[exp.Expression]) -> Optional[str]:
        if node is None:
            return None
        if isinstance(node, exp.Subquery):
            return node.alias or "(subquery)"
        if isinstance(node, exp.Table):
            return node.alias or TableRef.from_ast(node).full_name
        return node.sql()

    # ------------------------------------------------------------------ #
    # 终端文本摘要
    # ------------------------------------------------------------------ #
    def summary_text(self, statements: List[Dict[str, Any]], report: Optional[Dict[str, Any]] = None,
                     color: bool = False) -> str:
        """把解析结果渲染成终端可读的中文摘要。"""
        report = report or self.aggregate(statements)
        bold = "\033[1m" if color else ""
        dim = "\033[2m" if color else ""
        reset = "\033[0m" if color else ""
        lines: List[str] = []
        rule = "=" * 72
        lines.append(rule)
        lines.append(
            f"{bold}SQL 血缘解析报告{reset}  dialect={report['dialect']}  "
            f"文件数={report.get('file_count', 0)}  语句数={report['statement_count']}"
        )
        lines.append(rule)

        if not statements:
            lines.append("(没有解析到任何 SQL 语句)")
            return "\n".join(lines)

        for stmt in statements:
            lines.append("")
            lines.append(
                f"{bold}[语句 {stmt['statement_index']}] task_type = {stmt['task_type']}{reset}"
            )
            if stmt.get("source"):
                lines.append(f"  来源文件: {stmt['source']}")
            outs = stmt.get("output_table_names") or []
            ins = stmt.get("input_table_names") or []
            lines.append(f"  输出表  : {', '.join(outs) if outs else '(无，查询结果集)'}")
            lines.append(f"  输入表  : {', '.join(ins) if ins else '(无)'}")

            if stmt["table_lineage"]:
                lines.append("  表级血缘:")
                width = max(len(p["source"]) for p in stmt["table_lineage"])
                for pair in stmt["table_lineage"]:
                    lines.append(f"    {pair['source']:<{width}}  -->  {pair['target']}")

            if stmt["column_lineage"]:
                lines.append(f"  字段级血缘 ({len(stmt['column_lineage'])} 条):")
                for col in stmt["column_lineage"]:
                    src = f"{col['source_table']}.{col['source_column']}" if col["source_table"] else col["source_column"]
                    flag = "" if col.get("resolved", True) else f" {dim}(未能解析){reset}"
                    lines.append(
                        f"    {col['target_table']}.{col['target_column']}  <-  {src}"
                        f"   [{col['expression']}]{flag}"
                    )
            else:
                lines.append("  字段级血缘: (无)")

            if stmt.get("joins"):
                lines.append("  关联关系:")
                for j in stmt["joins"]:
                    cond = j["on"] or "(无 ON 条件)"
                    lines.append(f"    {j['left']}  {j['type']}  {j['right']}  ON {cond}")

            if stmt.get("filters"):
                lines.append("  过滤条件:")
                for f in stmt["filters"]:
                    lines.append(f"    - {f}")

            if stmt.get("partition_filters"):
                pf = ", ".join(f"{k}={v}" for k, v in stmt["partition_filters"].items())
                lines.append(f"  分区过滤: {pf}")

        lines.append("")
        lines.append(rule)
        lines.append(
            f"{bold}汇总{reset}: 输入表 {len(report.get('input_table_names', []))} 张 / "
            f"输出表 {len(report.get('output_table_names', []))} 张 / "
            f"表级血缘 {len(report.get('table_lineage', []))} 对 / "
            f"字段级血缘 {len(report.get('column_lineage', []))} 条"
        )
        lines.append(rule)
        return "\n".join(lines)


def parse_sql(sql: str, dialect: str = DEFAULT_DIALECT) -> List[Dict[str, Any]]:
    """便捷函数：解析一段 SQL 并返回血缘结果列表。"""
    return SqlLineageParser(dialect=dialect).parse_sql(sql)


def dumps(report: Any, indent: int = 2) -> str:
    """带 ensure_ascii=False 的 JSON 序列化（中文表名/别名可读）。"""
    return json.dumps(report, ensure_ascii=False, indent=indent)
