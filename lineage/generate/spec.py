"""生成引擎公共底座（L1~L4 共用）。

本模块只提供「证据可追溯」的底层能力，不含任何编造逻辑：

* :func:`open_store` —— 只读打开知识库（不存在就报错，绝不悄悄建空库）；
* :class:`GraphIndex` —— ``warehouse_graph.json`` 的只读索引：边（含**真实字段映射表达式**）、
  分层、上下游、分区过滤、字段清单；
* :class:`KbView` —— 知识库只读视图：表 / 字段（含中文名与来源）/ 指标口径 / 表级血缘；
* :class:`Evidence` —— 逐列 explain + warnings 收集器（生成 SQL 的每一列都必须有依据）；
* 别名分配 / 表达式改写 / 关联键推断等纯函数工具。

分层规则（与 :mod:`lineage.graph` 的 ``table_layer`` 保持一致）::

    src / stg  ->  0      ods -> 1      dim -> 侧表      dwd -> 2      dws -> 3      ads -> 4

「不得跨层直连」判定：一条边的 分层差 只能是 0 或 1（相邻层），否则视为跨层直连。
``dim`` 是侧表，允许被 dwd/dws/ads 任意层 JOIN，不参与分层差判定。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..graph import table_layer
from ..knowledge import KnowledgeStore, default_db_path

#: 默认分区值占位（DolphinScheduler 里由定时参数替换）
DATE_PLACEHOLDER = "${bizdate}"

__all__ = [
    "PROJECT_ROOT",
    "DEFAULT_GRAPH",
    "DATE_PLACEHOLDER",
    "LAYERS",
    "LAYER_INDEX",
    "LAYER_CN",
    "SIDE_LAYERS",
    "TERMINAL_LAYERS",
    "layer_index",
    "layer_cn",
    "step_ok",
    "Evidence",
    "GraphIndex",
    "KbView",
    "open_store",
    "resolve_db_path",
    "split_table",
    "short_name",
    "assign_aliases",
    "detect_alias",
    "rewrite_alias",
    "strip_alias_suffix",
    "infer_join_key",
    "parse_check",
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GRAPH = PROJECT_ROOT / "warehouse_graph.json"

#: 全部分层（顺序即自上而下的数仓流向）
LAYERS: Tuple[str, ...] = ("src", "stg", "ods", "dim", "dwd", "dws", "ads", "app", "other")

#: 分层序号（用于「不得跨层直连」判定）；dim 是侧表，见 :data:`SIDE_LAYERS`
LAYER_INDEX: Dict[str, int] = {
    "src": 0,
    "stg": 0,
    "ods": 1,
    "dim": 1,
    "dwd": 2,
    "dws": 3,
    "ads": 4,
    "app": 5,
    "other": 2,
}

#: 中文层名（生成注释 / 报告用）
LAYER_CN: Dict[str, str] = {
    "src": "源系统",
    "stg": "缓冲层",
    "ods": "贴源层",
    "dim": "维表层",
    "dwd": "明细层",
    "dws": "汇总层",
    "ads": "应用层",
    "app": "应用层",
    "other": "未分层",
}

#: 侧表层（维表）：不参与「跨层直连」判定
SIDE_LAYERS: Tuple[str, ...] = ("dim",)

#: 链路终点层（这些层产出后无人消费是正常的）
TERMINAL_LAYERS: Tuple[str, ...] = ("ads", "app")

#: 链路起点层（这些层作为输入但没有上游是正常的）
SOURCE_LAYERS: Tuple[str, ...] = ("src", "stg", "ods")


# --------------------------------------------------------------------------- #
# 分层工具
# --------------------------------------------------------------------------- #
def layer_index(layer: str) -> int:
    """分层 -> 序号（未知层按 ``other`` 处理）。"""
    return LAYER_INDEX.get((layer or "").lower(), LAYER_INDEX["other"])


def layer_cn(layer: str) -> str:
    return LAYER_CN.get((layer or "").lower(), layer or "未分层")


def layer_of(table: str) -> str:
    """表名 -> 分层（复用 :func:`lineage.graph.table_layer`）。"""
    return table_layer(table or "")


def step_ok(source_layer: str, target_layer: str) -> bool:
    """分层规则：允许「同层」与「相邻层下一层」，禁止跨层直连。

    维表（``dim``）是侧表：被任意层 JOIN 都合规。
    """
    s = (source_layer or "").lower()
    t = (target_layer or "").lower()
    if s in SIDE_LAYERS or t in SIDE_LAYERS:
        return True
    if t in ("", "other") or s in ("", "other"):
        return True
    return 0 <= layer_index(t) - layer_index(s) <= 1


# --------------------------------------------------------------------------- #
# 证据收集
# --------------------------------------------------------------------------- #
class Evidence:
    """逐列依据（explain）+ 待人工确认项（warnings）。

    「宁少勿假」：任何**推导**（而非知识库/存量脚本直接给出的）内容都必须落到
    :meth:`warn`，让数据开发人工确认。
    """

    def __init__(self) -> None:
        self.explain: List[str] = []
        self.warnings: List[str] = []

    def say(self, text: str) -> None:
        if text and text not in self.explain:
            self.explain.append(text)

    def warn(self, text: str) -> None:
        if text and text not in self.warnings:
            self.warnings.append(text)

    def merge(self, other: "Evidence", prefix: str = "") -> None:
        for line in other.explain:
            self.say(f"{prefix}{line}" if prefix else line)
        for line in other.warnings:
            self.warn(f"{prefix}{line}" if prefix else line)


# --------------------------------------------------------------------------- #
# 知识库 / 血缘图
# --------------------------------------------------------------------------- #
def resolve_db_path(db: Any = None) -> Path:
    """请求体 db > 环境变量 KB_DB > 项目默认 ``data/knowledge.db``。"""
    raw = str(db or "").strip() or os.environ.get("KB_DB") or str(default_db_path())
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    return path


def open_store(db: Any = None) -> KnowledgeStore:
    """只读打开知识库；不存在直接抛 ``FileNotFoundError``（由调用方转成业务错误）。"""
    path = resolve_db_path(db)
    if not path.exists():
        raise FileNotFoundError(
            f"知识库文件不存在：{path}；先跑 `python -m lineage.cli kb build` 建库，"
            f"或用 --db / KB_DB 指定库文件"
        )
    return KnowledgeStore(path, create=False)


class KbView:
    """知识库只读视图（带一层内存缓存，避免反复查表）。"""

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store
        self._fields: Dict[str, List[Dict[str, Any]]] = {}
        self._field_index: Dict[Tuple[str, str], Dict[str, Any]] = {}

    # -- 表 ---------------------------------------------------------------- #
    def table(self, name: str) -> Optional[Dict[str, Any]]:
        if not name:
            return None
        raw = self.store.get_table(name)
        if raw and _same_table(raw.get("table_name"), name):
            return raw
        if raw:
            # get_table 有模糊兜底：只在「同库或同短名」时认可，避免张冠李戴
            if _short(raw.get("table_name")).lower() == _short(name).lower():
                return raw
            if _schema(raw.get("table_name")) and _schema(name) and \
                    _schema(raw.get("table_name")) == _schema(name):
                return raw
            if _short(raw.get("table_name")) == _short(name):
                return raw
        return raw

    def resolve(self, name: str) -> str:
        """表名归一（支持只写短名）。"""
        return self.store.resolve_table_name(name)

    def has(self, name: str) -> bool:
        return self.table(name) is not None

    def layer(self, name: str) -> str:
        info = self.table(name)
        if info and info.get("layer"):
            return str(info["layer"])
        return layer_of(name)

    # -- 字段 -------------------------------------------------------------- #
    def fields(self, table: str) -> List[Dict[str, Any]]:
        key = self.resolve(table) if table else table
        if key not in self._fields:
            self._fields[key] = self.store.fields(key)
            for row in self._fields[key]:
                self._field_index[(key.lower(), str(row.get("column_name")).lower())] = row
        return self._fields[key]

    def field(self, table: str, column: str) -> Optional[Dict[str, Any]]:
        self.fields(table)
        return self._field_index.get((str(self.resolve(table)).lower(), str(column).lower()))

    def find_field_by_chinese(self, table: str, chinese: str) -> Optional[Dict[str, Any]]:
        """按中文业务名精确匹配字段（找不到给 ``None``，不做猜测）。"""
        key = (chinese or "").strip()
        if not key:
            return None
        for row in self.fields(table):
            if (row.get("chinese_name") or "").strip() == key:
                return row
        return None

    def column_names(self, table: str) -> List[str]:
        return [str(r.get("column_name")) for r in self.fields(table)]

    def tables(self, layer: Optional[str] = None) -> List[Dict[str, Any]]:
        """全部登记表（可按分层筛选）。"""
        rows = self.store.tables()
        for row in rows:
            if not row.get("layer"):
                row["layer"] = layer_of(str(row.get("table_name") or ""))
        if layer:
            return [r for r in rows if str(r.get("layer")) == layer]
        return rows

    def table_candidates(self, layer: str) -> List[Dict[str, Any]]:
        """某分层的候选表（:func:`lineage.generate.pipeline.find_target_table` 用）。"""
        return self.tables(layer)

    # -- 口径 -------------------------------------------------------------- #
    def metrics(self, table: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.store.metrics(table) if table else self.store.metrics()

    def find_metrics(self, name: str) -> List[Dict[str, Any]]:
        return self.store.get_metric(name)

    def edges(self) -> List[Dict[str, Any]]:
        return self.store.edges()

    def counts(self) -> Dict[str, int]:
        return self.store.counts()

    def close(self) -> None:
        self.store.close()


def _schema(name: str) -> str:
    return (name or "").split(".", 1)[0].lower() if "." in (name or "") else ""


def _short(name: str) -> str:
    return (name or "").split(".")[-1]


def _same_table(a: str, b: str) -> bool:
    return (a or "").lower() == (b or "").lower()


class GraphIndex:
    """``warehouse_graph.json`` 的只读索引。

    图里的每条边都带**真实字段映射表达式**（``columns[].expression``，来自存量脚本），
    因此生成器可以把「存量口径」原样复用，而不是凭空编字段。
    """

    def __init__(self, data: Optional[Dict[str, Any]] = None, path: Optional[Any] = None) -> None:
        self.data: Dict[str, Any] = data or {}
        self.path = str(path) if path else ""
        self._edges_by_target: Dict[str, List[Dict[str, Any]]] = {}
        self._edges_by_source: Dict[str, List[Dict[str, Any]]] = {}
        self._nodes: Dict[str, Dict[str, Any]] = {}
        for edge in self.data.get("edges") or []:
            self._edges_by_target.setdefault(edge.get("target") or "", []).append(edge)
            self._edges_by_source.setdefault(edge.get("source") or "", []).append(edge)
        for node in self.data.get("nodes") or []:
            self._nodes[node.get("name") or ""] = node

    # -- 载入 -------------------------------------------------------------- #
    @classmethod
    def load(cls, path: Optional[Any] = None) -> "GraphIndex":
        target = Path(path) if path else DEFAULT_GRAPH
        if not target.is_absolute():
            target = (PROJECT_ROOT / target).resolve()
        if not target.exists():
            return cls({}, path=str(target))
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls({}, path=str(target))
        return cls(data, path=str(target))

    @property
    def available(self) -> bool:
        return bool(self.data.get("edges")) or bool(self.data.get("nodes"))

    @property
    def name(self) -> str:
        return os.path.basename(self.path) if self.path else ""

    # -- 查询 -------------------------------------------------------------- #
    def has(self, table: str) -> bool:
        return table in self._nodes

    def tables(self) -> List[str]:
        return sorted(self._nodes)

    def edges_into(self, table: str) -> List[Dict[str, Any]]:
        return list(self._edges_by_target.get(table or "", []))

    def edges_from(self, table: str) -> List[Dict[str, Any]]:
        return list(self._edges_by_source.get(table or "", []))

    def upstream_tables(self, table: str, layers: Optional[Sequence[str]] = None) -> List[str]:
        out = []
        for edge in self.edges_into(table):
            src = edge.get("source") or ""
            if layers is None or layer_of(src) in layers:
                out.append(src)
        return out

    def downstream_tables(self, table: str) -> List[str]:
        return [e.get("target") or "" for e in self.edges_from(table)]

    def edge(self, source: str, target: str) -> Optional[Dict[str, Any]]:
        for edge in self.edges_into(target):
            if (edge.get("source") or "") == source:
                return edge
        return None

    def expressions_into(self, target: str, sources: Sequence[str]) -> List[Dict[str, Any]]:
        """目标表的字段映射明细（按源表筛选），保留**真实表达式**。"""
        wanted = set(sources)
        out: List[Dict[str, Any]] = []
        for edge in self.edges_into(target):
            if edge.get("source") not in wanted:
                continue
            for col in edge.get("columns") or []:
                out.append({**col, "source_table": edge.get("source"),
                            "files": edge.get("files") or []})
        return out

    def columns_into(self, target: str, sources: Sequence[str]) -> List[str]:
        seen: List[str] = []
        for item in self.expressions_into(target, sources):
            name = item.get("target_column")
            if name and name not in seen:
                seen.append(name)
        return seen

    def column_mapping_count(self, source: str, target: str) -> int:
        edge = self.edge(source, target)
        if not edge:
            return 0
        return len(edge.get("columns") or []) or int(edge.get("column_mappings") or 0)

    def partition_field(self, target: str, sources: Optional[Sequence[str]] = None) -> Optional[str]:
        """目标表分区字段：取存量脚本里真实出现过的分区过滤键（没有就不猜）。"""
        wanted = set(sources or ())
        for edge in self.edges_into(target):
            if wanted and edge.get("source") not in wanted:
                continue
            for key in (edge.get("partition_filters") or {}):
                return str(key)
        return None

    def partition_value(self, target: str, sources: Optional[Sequence[str]] = None) -> str:
        wanted = set(sources or ())
        for edge in self.edges_into(target):
            if wanted and edge.get("source") not in wanted:
                continue
            for _key, value in (edge.get("partition_filters") or {}).items():
                return str(value)
        return ""

    def source_files(self, target: str, sources: Optional[Sequence[str]] = None) -> List[str]:
        wanted = set(sources or ())
        out: List[str] = []
        for edge in self.edges_into(target):
            if wanted and edge.get("source") not in wanted:
                continue
            for f in edge.get("files") or []:
                if f not in out:
                    out.append(f)
        return out

    def layer(self, table: str) -> str:
        node = self._nodes.get(table or "")
        if node and node.get("layer"):
            return str(node["layer"])
        return layer_of(table)

    def main_upstream(self, target: str, layers: Optional[Sequence[str]] = None,
                      exclude: Iterable[str] = ()) -> Optional[str]:
        """目标的「主要上游」：优先分层低于目标、字段映射最多的那张表。"""
        skip = set(exclude)
        best: Optional[str] = None
        best_key: Optional[Tuple[int, int, str]] = None
        t_layer = layer_index(self.layer(target))
        for edge in self.edges_into(target):
            src = edge.get("source") or ""
            if not src or src in skip:
                continue
            if layers is not None and layer_of(src) not in layers:
                continue
            count = len(edge.get("columns") or []) or int(edge.get("column_mappings") or 0)
            below = -1 if layer_index(self.layer(src)) < t_layer else 0
            key = (below, -count, src)
            if best_key is None or key < best_key:
                best_key, best = key, src
        return best


# --------------------------------------------------------------------------- #
# 别名 / 表达式工具
# --------------------------------------------------------------------------- #
def split_table(name: str) -> Tuple[str, str]:
    """``cdw.dwd_卷烟产量明细`` -> ``("cdw", "dwd_卷烟产量明细")``。"""
    if not name:
        return "", ""
    if "." in name:
        parts = name.split(".")
        return ".".join(parts[:-1]), parts[-1]
    return "", name


def short_name(name: str) -> str:
    return split_table(name)[1]


def assign_aliases(tables: Sequence[str]) -> Dict[str, str]:
    """给一组表分配稳定别名：``t1, t2, ...``（顺序即入参顺序）。"""
    out: Dict[str, str] = {}
    for idx, name in enumerate(tables, start=1):
        if name and name not in out:
            out[name] = f"t{idx}"
    return out


_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")
_ALIAS_SUFFIX_RE = re.compile(r"\s+AS\s+[A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]*\s*$", re.I)
_AGG_RE = re.compile(r"\b(SUM|COUNT|AVG|MAX|MIN|COLLECT_SET|COLLECT_LIST|STDDEV|VAR)\s*\(", re.I)


def detect_alias(expression: str) -> Optional[str]:
    """从表达式里推断它用的是哪个表别名（``p.output_qty AS output_qty`` -> ``p``）。"""
    if not expression:
        return None
    aliases: List[str] = []
    for match in _IDENT_RE.finditer(expression):
        alias = match.group(1)
        if alias.lower() in ("sum", "count", "avg", "max", "min", "cast", "round", "nullif",
                             "coalesce", "case", "when", "then", "else", "end", "as", "if",
                             "nvl", "ifnull", "decimal", "int", "bigint", "string", "date",
                             "regexp_replace", "substr", "concat", "abs", "floor", "ceil"):
            continue
        if alias not in aliases:
            aliases.append(alias)
    return aliases[0] if len(aliases) == 1 else (aliases[0] if aliases else None)


def rewrite_alias(expression: str, old: str, new: str) -> str:
    """把表达式里的 ``old.`` 前缀改写成 ``new.``（别名重映射）。"""
    if not expression or not old or not new or old == new:
        return expression or ""
    return re.sub(rf"\b{re.escape(old)}\.", f"{new}.", expression)


def strip_alias_suffix(expression: str) -> str:
    """去掉表达式结尾的 ``AS xxx``（生成器自己再拼 AS）。"""
    return _ALIAS_SUFFIX_RE.sub("", (expression or "").strip()).strip()


def has_aggregate(expression: str) -> bool:
    return bool(_AGG_RE.search(expression or ""))


def infer_join_key(fact_table: str, dim_table: str, kb: Optional[KbView] = None,
                   graph: Optional[GraphIndex] = None) -> Tuple[str, str]:
    """推断两表关联键：只认**有据可依**的同名列或命名规范，返回 ``(列名, 依据说明)``。

    策略（按可靠性排序，找不到返回 ``("", "")``）：

    1. 两张表都登记过的同名列（``*_code`` > ``*_id`` > ``*_no`` > 其它）；
    2. 维表命名规范：``dim.dim_plant`` -> 事实表里的 ``plant_code`` / ``plant_id`` / ``plant_no``；
    3. 都没有 -> 空（调用方必须把它标成「需人工确认」，绝不硬编一个键）。
    """
    fact_cols: List[str] = []
    dim_cols: List[str] = []
    if kb is not None:
        fact_cols = kb.column_names(fact_table)
        dim_cols = kb.column_names(dim_table)
    if graph is not None and (not fact_cols or not dim_cols):
        fact_cols = fact_cols or [_short_of(col) for col in _graph_columns(graph, fact_table)]
        dim_cols = dim_cols or [_short_of(col) for col in _graph_columns(graph, dim_table)]
    common = [c for c in dim_cols if c in set(fact_cols)]
    if common:
        for suffix in ("_code", "_id", "_no", "_key"):
            for col in common:
                if col.lower().endswith(suffix):
                    return col, f"两张表登记过的同名列 {col}"
        return sorted(common)[0], f"两张表登记过的同名列 {sorted(common)[0]}"

    # 命名规范：dim_plant -> plant_code
    short = short_name(dim_table) if dim_table else ""
    entity = re.sub(r"^(dim|维表)[_\-]?", "", short, flags=re.I)
    entity = re.sub(r"(维表|信息|字典|代码表|表)$", "", entity).strip()
    if entity:
        lowered = {c.lower(): c for c in fact_cols}
        for suffix in ("_code", "_id", "_no", "_key"):
            candidate = f"{entity}{suffix}".lower()
            if candidate in lowered:
                return lowered[candidate], (f"命名规范推断：{dim_table} -> {lowered[candidate]}"
                                            f"（**需人工确认**）")
    return "", ""


def _short_of(column: str) -> str:
    return (column or "").split(".")[-1]


def _graph_columns(graph: GraphIndex, table: str) -> List[str]:
    """图里这张表作为「目标」或「来源」出现过的列名（用于兜底推断关联键）。"""
    out: List[str] = []
    for edge in graph.edges_into(table):
        for col in edge.get("columns") or []:
            name = col.get("target_column")
            if name and name not in out:
                out.append(name)
    for edge in graph.edges_from(table):
        for col in edge.get("columns") or []:
            name = col.get("source_column")
            if name and name not in out:
                out.append(name)
    return out


# --------------------------------------------------------------------------- #
# SQL 语法自检
# --------------------------------------------------------------------------- #
def parse_check(sql: str, dialect: str = "hive") -> Dict[str, Any]:
    """把生成的 SQL 丢回血缘解析引擎，确认「语法可解析 + 血缘可提取」。

    返回 ``{parse_ok, dialect, statement_count, input_tables, output_tables,
    table_lineage, column_lineage_count, error}``；解析异常不抛，落到 ``parse_ok=false``。
    """
    from ..parser import SqlLineageParser

    out: Dict[str, Any] = {
        "parse_ok": False,
        "dialect": dialect,
        "statement_count": 0,
        "input_tables": [],
        "output_tables": [],
        "table_lineage": [],
        "column_lineage_count": 0,
        "error": "",
    }
    if not (sql or "").strip():
        out["error"] = "SQL 为空"
        return out
    parser = SqlLineageParser(dialect=dialect)
    try:
        stmts = parser.parse_sql(sql, source="<generate>")
    except Exception as exc:  # noqa: BLE001 — 解析失败要如实返回，不能吞
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    if not isinstance(stmts, list):
        stmts = [stmts]
    inputs: Set[str] = set()
    outputs: Set[str] = set()
    tlineage: List[Dict[str, Any]] = []
    columns = 0
    for stmt in stmts:
        if not isinstance(stmt, dict):
            continue
        inputs.update(stmt.get("input_table_names") or [])
        outputs.update(stmt.get("output_table_names") or [])
        tlineage.extend(x for x in (stmt.get("table_lineage") or []) if x not in tlineage)
        columns += len(stmt.get("column_lineage") or [])
    out.update({
        "parse_ok": bool(stmts) and bool(outputs),
        "statement_count": len(stmts),
        "input_tables": sorted(inputs),
        "output_tables": sorted(outputs),
        "table_lineage": tlineage,
        "column_lineage_count": columns,
    })
    return out
