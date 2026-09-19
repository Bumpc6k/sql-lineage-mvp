"""血缘图引擎（P2）：把解析结果构造成有向图，支持上游溯源 / 下游影响 / 路径 / 环路 / 统计。

设计说明
--------
* **纯内存图**，不依赖图数据库、不依赖 networkx：节点用 dict 存，出边 / 入边用两个
  邻接表（``defaultdict(list)``）维护。这样离线环境零依赖即可运行，演示时不会因为
  装不上包而翻车。
* 节点 = 表（``schema.table`` 全名）；边 = 表级血缘方向 ``source -> target``，
  边上携带元信息：来源文件、语句位置、字段映射条数、未解析字段映射条数、字段映射明细。
* 同一张表在多个文件 / 多条语句里被加工时自动合并节点与边（去重、累加）。
* 支持 ``to_dict()`` / ``from_dict()`` JSON 往返，图可以落盘后由别的进程读回来做分析。

用法::

    from lineage.parser import SqlLineageParser
    from lineage.graph import build_graph

    stmts = SqlLineageParser("hive").parse_sql(sql_text, source="a.sql")
    g = build_graph(stmts)
    g.stats()
    g.upstream("ads.ads_卷烟产销月报", depth=3)
    g.downstream("ods.ods_卷烟产量流水")
    g.path_between("src.erp_产量接口", "ads.ads_卷烟产销月报")
    g.detect_cycles()
"""

from __future__ import annotations

import heapq
import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

__all__ = [
    "GRAPH_SCHEMA_VERSION",
    "LAYERS",
    "LineageEdge",
    "LineageGraph",
    "LineageNode",
    "build_graph",
    "build_graph_from_report",
    "table_layer",
]

#: 落盘 JSON 的结构版本号，方便后续升级时做兼容判断
GRAPH_SCHEMA_VERSION = 1

#: 分层顺序（用于可视化排序与分层统计）
LAYERS: Tuple[str, ...] = ("src", "stg", "ods", "dim", "dwd", "dws", "ads", "app", "other")

#: 每条边最多保留的字段映射明细条数（避免图 JSON 过大）
MAX_COLUMNS_PER_EDGE = 200

#: 表不存在时的伪节点名（纯 SELECT 语句没有输出表）
QUERY_RESULT_NODE = "(query_result)"

_SCHEMA_LAYER_HINTS: Dict[str, str] = {
    "src": "src",
    "source": "src",
    "stg": "stg",
    "stage": "stg",
    "ods": "ods",
    "dim": "dim",
    "dwd": "dwd",
    "cdw": "dwd",  # 数仓层（cdw.dwd_xxx 由表名前缀继续细分）
    "dws": "dws",
    "ads": "ads",
    "app": "app",
}


def table_layer(name: str) -> str:
    """按「库名 + 表名前缀」推断分层，用于分层统计 / 可视化排序。

    规则（命中即返回，保序）::

        src.*  / stg.*                 -> src / stg
        ods.*                          -> ods
        dim.*                          -> dim
        表名含 dwd_                     -> dwd
        表名含 dws_                     -> dws
        ads.* / app.*                  -> ads / app
        其他                           -> other
    """
    if not name:
        return "other"
    schema = name.split(".", 1)[0].lower() if "." in name else ""
    table = name.split(".")[-1].lower()

    if "dwd" in table:
        return "dwd"
    if "dws" in table:
        return "dws"
    if schema in _SCHEMA_LAYER_HINTS:
        return _SCHEMA_LAYER_HINTS[schema]
    for key in ("ods", "dim", "ads", "dwd", "dws"):
        if table.startswith(key + "_") or table == key:
            return key
    return "other"


def _split_name(name: str) -> Tuple[Optional[str], str]:
    """``ads.ads_卷烟月报`` -> (``ads``, ``ads_卷烟月报``)；无库名时 schema 为 None。"""
    if "." in name:
        parts = name.split(".")
        return ".".join(parts[:-1]), parts[-1]
    return None, name


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class LineageNode:
    """图中的一个表节点。"""

    name: str
    schema: Optional[str] = None
    table: Optional[str] = None
    layer: str = "other"
    #: 生产该表的语句引用：[{file, statement_index, task_type}]
    produced_by: List[Dict[str, Any]] = field(default_factory=list)
    #: 消费该表的语句引用（该表作为上游出现）
    consumed_by: List[Dict[str, Any]] = field(default_factory=list)
    #: 该表被引用的次数（下游语句数）
    ref_count: int = 0

    def __post_init__(self) -> None:
        if self.schema is None and self.table is None:
            self.schema, self.table = _split_name(self.name)
        self.layer = table_layer(self.name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "schema": self.schema,
            "table": self.table,
            "layer": self.layer,
            "produced_by": self.produced_by,
            "consumed_by": self.consumed_by,
            "ref_count": self.ref_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LineageNode":
        return cls(
            name=data["name"],
            schema=data.get("schema"),
            table=data.get("table"),
            layer=data.get("layer") or table_layer(data["name"]),
            produced_by=list(data.get("produced_by") or []),
            consumed_by=list(data.get("consumed_by") or []),
            ref_count=int(data.get("ref_count") or 0),
        )


@dataclass
class LineageEdge:
    """一条表级血缘边 ``source -> target``，携带加工元信息。"""

    source: str
    target: str
    files: List[str] = field(default_factory=list)
    statements: List[Dict[str, Any]] = field(default_factory=list)
    column_mappings: int = 0
    unresolved_mappings: int = 0
    constant_mappings: int = 0
    #: 字段映射明细（去重后最多 MAX_COLUMNS_PER_EDGE 条）
    columns: List[Dict[str, str]] = field(default_factory=list)
    columns_truncated: bool = False
    partition_filters: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> Tuple[str, str]:
        return (self.source, self.target)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "files": self.files,
            "statements": self.statements,
            "column_mappings": self.column_mappings,
            "unresolved_mappings": self.unresolved_mappings,
            "constant_mappings": self.constant_mappings,
            "columns": self.columns,
            "columns_truncated": self.columns_truncated,
            "partition_filters": self.partition_filters,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LineageEdge":
        return cls(
            source=data["source"],
            target=data["target"],
            files=list(data.get("files") or []),
            statements=list(data.get("statements") or []),
            column_mappings=int(data.get("column_mappings") or 0),
            unresolved_mappings=int(data.get("unresolved_mappings") or 0),
            constant_mappings=int(data.get("constant_mappings") or 0),
            columns=list(data.get("columns") or []),
            columns_truncated=bool(data.get("columns_truncated")),
            partition_filters=dict(data.get("partition_filters") or {}),
        )


# --------------------------------------------------------------------------- #
# 图
# --------------------------------------------------------------------------- #
class LineageGraph:
    """血缘有向图（内存实现，零外部依赖）。

    * ``nodes``: 表全名 -> LineageNode
    * ``edges``: (source, target) -> LineageEdge
    * ``_out`` / ``_in``: 邻接表，保证遍历顺序稳定（按插入顺序）
    """

    def __init__(self, dialect: str = "hive", root: Optional[str] = None) -> None:
        self.dialect = dialect
        self.root = root
        self.nodes: Dict[str, LineageNode] = {}
        self.edges: Dict[Tuple[str, str], LineageEdge] = {}
        self._out: Dict[str, List[str]] = defaultdict(list)
        self._in: Dict[str, List[str]] = defaultdict(list)
        #: 解析失败的语句记录（由 scan 填充）
        self.failures: List[Dict[str, Any]] = []
        #: 扫描统计（由 scan 填充）
        self.scan_meta: Dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def add_node(self, name: str) -> LineageNode:
        node = self.nodes.get(name)
        if node is None:
            node = LineageNode(name=name)
            self.nodes[name] = node
        return node

    def add_edge_meta(
        self,
        source: str,
        target: str,
        file: Optional[str] = None,
        statement_index: Optional[int] = None,
        task_type: Optional[str] = None,
    ) -> LineageEdge:
        """新建 / 复用一条边，并登记来源语句元信息（自动去重）。"""
        key = (source, target)
        edge = self.edges.get(key)
        if edge is None:
            edge = LineageEdge(source=source, target=target)
            self.edges[key] = edge
            self._out[source].append(target)
            self._in[target].append(source)

        if file and file not in edge.files:
            edge.files.append(file)
        stmt_ref = {
            "file": file,
            "statement_index": statement_index,
            "task_type": task_type,
        }
        if stmt_ref not in edge.statements:
            edge.statements.append(stmt_ref)
        return edge

    def add_statement(self, stmt: Dict[str, Any]) -> None:
        """把一条语句的解析结果并入图。

        约定（与 parser.analyze_statement 输出一致）：
        * ``table_lineage``：``[{source, target}, ...]`` 决定图的边
        * ``column_lineage``：``[{target_table,target_column,source_table,source_column,...}]``
          决定边上的字段映射条数
        * 只被读取（无输出表）的表也会建节点，但不产生边
        """
        file = stmt.get("source")
        idx = stmt.get("statement_index")
        task_type = stmt.get("task_type")

        for name in stmt.get("input_table_names") or []:
            node = self.add_node(name)
            ref = {"file": file, "statement_index": idx, "task_type": task_type}
            if ref not in node.consumed_by:
                node.consumed_by.append(ref)
            node.ref_count += 1
        for name in stmt.get("output_table_names") or []:
            node = self.add_node(name)
            ref = {"file": file, "statement_index": idx, "task_type": task_type}
            if ref not in node.produced_by:
                node.produced_by.append(ref)

        for pair in stmt.get("table_lineage") or []:
            src, tgt = pair.get("source"), pair.get("target")
            if not src or not tgt:
                continue
            self.add_node(src)
            self.add_node(tgt)
            edge = self.add_edge_meta(src, tgt, file, idx, task_type)
            pf = stmt.get("partition_filters") or {}
            for k, v in pf.items():
                edge.partition_filters.setdefault(k, v)

        # 字段映射归到对应边上
        for col in stmt.get("column_lineage") or []:
            src_t = col.get("source_table")
            src_c = col.get("source_column")
            tgt_t = col.get("target_table")
            tgt_c = col.get("target_column")
            if not tgt_t or not tgt_c:
                continue
            # 常量 / 未解析来源：算到「主要上游边」上（唯一上游时才有意义）
            edge: Optional[LineageEdge] = None
            if src_t is None:
                candidates = [
                    self.edges[(p["source"], p["target"])]
                    for p in (stmt.get("table_lineage") or [])
                    if p.get("target") == tgt_t and (p["source"], p["target"]) in self.edges
                ]
                if len(candidates) == 1:
                    edge = candidates[0]
                    edge.constant_mappings += 1
                else:
                    continue
            else:
                edge = self.edges.get((src_t, tgt_t))
                if edge is None:
                    continue
                if col.get("resolved", True):
                    edge.column_mappings += 1
                else:
                    edge.unresolved_mappings += 1

            entry = {
                "target_column": tgt_c,
                "source_column": src_c,
                "expression": col.get("expression") or "",
                "resolved": bool(col.get("resolved", True)),
            }
            if entry not in edge.columns:
                if len(edge.columns) >= MAX_COLUMNS_PER_EDGE:
                    edge.columns_truncated = True
                else:
                    edge.columns.append(entry)

    # ------------------------------------------------------------------ #
    # 基本查询
    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self.nodes)

    def __contains__(self, table: str) -> bool:
        return self.resolve_table(table) is not None

    @property
    def table_names(self) -> List[str]:
        return sorted(self.nodes)

    def tables_by_layer(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {layer: [] for layer in LAYERS}
        for name in sorted(self.nodes):
            out.setdefault(self.nodes[name].layer, []).append(name)
        return {k: v for k, v in out.items() if v}

    def successors(self, table: str) -> List[str]:
        """下游邻居（该表被谁用）。"""
        return list(self._out.get(table, []))

    def predecessors(self, table: str) -> List[str]:
        """上游邻居（该表用了谁）。"""
        return list(self._in.get(table, []))

    def edge(self, source: str, target: str) -> Optional[LineageEdge]:
        return self.edges.get((source, target))

    def resolve_table(self, name: str) -> Optional[str]:
        """把用户输入解析成图里的标准表名。

        依次尝试：全名精确匹配 -> 忽略大小写 -> 表名部分（不含库名）唯一匹配。
        找不到或匹配到多张表时返回 ``None``（用 :meth:`candidates` 看候选）。
        """
        if not name:
            return None
        if name in self.nodes:
            return name
        low = name.lower().strip().strip('"').strip("`").strip()
        for key in self.nodes:
            if key.lower() == low:
                return key
        hits = self.candidates(name)
        if len(hits) == 1:
            return hits[0]
        return None

    def candidates(self, name: str) -> List[str]:
        """按「表名部分」模糊匹配出候选表（用于 CLI 提示与自动补全库名）。"""
        low = (name or "").lower().strip().strip('"').strip("`").strip()
        hits: List[str] = []
        for key in sorted(self.nodes):
            if key.lower().endswith("." + low) or _split_name(key)[1].lower() == low:
                hits.append(key)
        return hits

    def roots(self) -> List[str]:
        """根表：没有任何上游（只被读、不被写出来）。与 :meth:`source_tables` 同义。"""
        return self.source_tables()

    def leaves(self) -> List[str]:
        """叶子表：没有任何下游（只被写、不再被别人读）。"""
        return sorted(t for t in self.nodes if not self.successors(t))

    def isolated(self) -> List[str]:
        """孤立表：既没有上游也没有下游。"""
        return sorted(
            t for t in self.nodes if not self.successors(t) and not self.predecessors(t)
        )

    def source_tables(self) -> List[str]:
        """真正的源表：无上游、且不是被加工出来的（或者没有生产语句也算源）。"""
        return sorted(t for t in self.nodes if not self.predecessors(t))

    # ------------------------------------------------------------------ #
    # 遍历：上游 / 下游
    # ------------------------------------------------------------------ #
    def _bfs_levels(
        self, start: str, direction: str, depth: Optional[int]
    ) -> Tuple[List[List[str]], List[str]]:
        """按层 BFS。direction='up' 走入边、'down' 走出边。返回 (levels, all_tables)。"""
        neighbors = self.predecessors if direction == "up" else self.successors
        levels: List[List[str]] = []
        seen: Set[str] = {start}
        current = [start]
        level = 0
        while current and (depth is None or level < depth):
            level += 1
            nxt: List[str] = []
            for node in current:
                for nb in neighbors(node):
                    if nb in seen:
                        continue
                    seen.add(nb)
                    nxt.append(nb)
            if not nxt:
                break
            # 同层内按名字排序，输出稳定
            nxt_sorted = sorted(dict.fromkeys(nxt))
            levels.append(nxt_sorted)
            current = nxt_sorted
        all_tables = [start] + [t for lvl in levels for t in lvl]
        return levels, all_tables

    def _enumerate_paths(
        self,
        start: str,
        direction: str,
        depth: Optional[int],
        max_paths: int = 100,
        max_length: int = 12,
    ) -> Tuple[List[List[str]], bool]:
        """枚举从 start 出发、走到「尽头」的链路（DFS，带条数与长度上限）。

        返回 (paths, truncated)。路径首元素是 start，按遍历方向延伸。
        """
        neighbors = self.predecessors if direction == "up" else self.successors
        paths: List[List[str]] = []
        truncated = False
        limit_depth = min(depth, max_length) if depth is not None else max_length

        def dfs(node: str, acc: List[str]) -> None:
            nonlocal truncated
            if len(paths) >= max_paths:
                truncated = True
                return
            nxt = [n for n in neighbors(node) if n not in acc]
            if not nxt or len(acc) - 1 >= limit_depth:
                paths.append(list(acc))
                return
            for nb in nxt:
                acc.append(nb)
                dfs(nb, acc)
                acc.pop()

        dfs(start, [start])
        return paths, truncated

    def upstream(self, table: str, depth: Optional[int] = None, max_paths: int = 100) -> Dict[str, Any]:
        """上游溯源：这张表的数据从哪来。

        返回 dict 结构::

            {
              "table": 标准表名, "direction": "upstream", "depth": N or None,
              "direct": [直接上游], "levels": [{"level":1,"tables":[...]}, ...],
              "tables": [全部上游表], "table_count": n,
              "edges": [{"source","target"}...],
              "paths": [[表, 上游, 更上游...], ...], "paths_truncated": bool,
              "roots": [该子图里的源表]
            }
        """
        resolved = self.resolve_table(table)
        if resolved is None:
            return self._not_found(table)
        levels, all_tables = self._bfs_levels(resolved, "up", depth)
        paths, truncated = self._enumerate_paths(resolved, "up", depth, max_paths)
        edges = [
            {"source": s, "target": t, "column_mappings": self.edges[(s, t)].column_mappings}
            for (s, t) in sorted(self.edges)
            if s in all_tables and t in all_tables
        ]
        upstream_tables = [t for t in all_tables if t != resolved]
        roots = sorted(t for t in all_tables if not self.predecessors(t))
        return {
            "table": resolved,
            "direction": "upstream",
            "depth": depth,
            "direct": levels[0] if levels else [],
            "levels": [{"level": i, "tables": t} for i, t in enumerate(levels, start=1)],
            "tables": upstream_tables,
            "table_count": len(upstream_tables),
            "edges": edges,
            "edge_count": len(edges),
            "paths": paths,
            "paths_truncated": truncated,
            "roots": roots,
            "found": True,
        }

    def downstream(self, table: str, depth: Optional[int] = None, max_paths: int = 100) -> Dict[str, Any]:
        """下游影响分析：改这张表会影响谁。结构同 :meth:`upstream`。"""
        resolved = self.resolve_table(table)
        if resolved is None:
            return self._not_found(table)
        levels, all_tables = self._bfs_levels(resolved, "down", depth)
        paths, truncated = self._enumerate_paths(resolved, "down", depth, max_paths)
        edges = [
            {"source": s, "target": t, "column_mappings": self.edges[(s, t)].column_mappings}
            for (s, t) in sorted(self.edges)
            if s in all_tables and t in all_tables
        ]
        downstream_tables = [t for t in all_tables if t != resolved]
        return {
            "table": resolved,
            "direction": "downstream",
            "depth": depth,
            "direct": levels[0] if levels else [],
            "levels": [{"level": i, "tables": t} for i, t in enumerate(levels, start=1)],
            "tables": downstream_tables,
            "table_count": len(downstream_tables),
            "edges": edges,
            "edge_count": len(edges),
            "paths": paths,
            "paths_truncated": truncated,
            "leaves": sorted(t for t in all_tables if not self.successors(t)),
            "found": True,
        }

    # 便捷别名：只关心表集合时用
    def ancestors(self, table: str, depth: Optional[int] = None) -> Set[str]:
        res = self.upstream(table, depth, max_paths=0)
        return set(res.get("tables", []))

    def descendants(self, table: str, depth: Optional[int] = None) -> Set[str]:
        res = self.downstream(table, depth, max_paths=0)
        return set(res.get("tables", []))

    def _not_found(self, table: str) -> Dict[str, Any]:
        return {
            "table": table,
            "direction": "unknown",
            "found": False,
            "candidates": self.candidates(table),
            "direct": [],
            "levels": [],
            "tables": [],
            "table_count": 0,
            "edges": [],
            "edge_count": 0,
            "paths": [],
            "paths_truncated": False,
        }

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    def path_between(
        self,
        source: str,
        target: str,
        max_paths: int = 20,
        max_length: int = 12,
    ) -> Dict[str, Any]:
        """两表之间的血缘链路。

        * ``paths``：所有链路（DFS 枚举，带条数 / 长度上限，避免组合爆炸）
        * ``shortest``：最短链路（BFS，链路上没有更短的了）

        返回 ``{source, target, found, paths, path_count, shortest, shortest_length}``。
        """
        src = self.resolve_table(source)
        dst = self.resolve_table(target)
        if src is None or dst is None:
            return {
                "source": source,
                "target": target,
                "found": False,
                "missing": [t for t, r in ((source, src), (target, dst)) if r is None],
                "candidates": {source: self.candidates(source), target: self.candidates(target)},
                "paths": [],
                "path_count": 0,
                "shortest": [],
                "shortest_length": None,
            }

        paths: List[List[str]] = []
        truncated = False

        def dfs(node: str, acc: List[str]) -> None:
            nonlocal truncated
            if len(paths) >= max_paths:
                truncated = True
                return
            if node == dst:
                paths.append(list(acc))
                return
            if len(acc) - 1 >= max_length:
                return
            for nb in self.successors(node):
                if nb in acc:
                    continue
                acc.append(nb)
                dfs(nb, acc)
                acc.pop()

        dfs(src, [src])
        paths.sort(key=lambda p: (len(p), p))

        # 最短路径：BFS
        shortest: List[str] = []
        prev: Dict[str, Optional[str]] = {src: None}
        queue = deque([src])
        while queue:
            node = queue.popleft()
            if node == dst:
                break
            for nb in self.successors(node):
                if nb in prev:
                    continue
                prev[nb] = node
                queue.append(nb)
        if dst in prev:
            cur: Optional[str] = dst
            while cur is not None:
                shortest.append(cur)
                cur = prev[cur]
            shortest.reverse()

        return {
            "source": src,
            "target": dst,
            "found": bool(paths),
            "paths": paths,
            "path_count": len(paths),
            "truncated": truncated,
            "shortest": shortest,
            "shortest_length": len(shortest) if shortest else None,
            "direct": bool(self.edges.get((src, dst))),
        }

    # ------------------------------------------------------------------ #
    # 环路
    # ------------------------------------------------------------------ #
    def _sccs(self) -> List[List[str]]:
        """Tarjan 强连通分量（迭代实现，避免深图递归爆栈）。"""
        index_of: Dict[str, int] = {}
        low: Dict[str, int] = {}
        on_stack: Set[str] = set()
        stack: List[str] = []
        result: List[List[str]] = []
        counter = [0]

        for start in self.nodes:
            if start in index_of:
                continue
            work: List[Tuple[str, int]] = [(start, 0)]
            while work:
                node, pi = work.pop()
                if pi == 0:
                    index_of[node] = low[node] = counter[0]
                    counter[0] += 1
                    stack.append(node)
                    on_stack.add(node)
                recurse = False
                neighbors = self.successors(node)
                for i in range(pi, len(neighbors)):
                    nb = neighbors[i]
                    if nb not in index_of:
                        work.append((node, i + 1))
                        work.append((nb, 0))
                        recurse = True
                        break
                    if nb in on_stack:
                        low[node] = min(low[node], index_of[nb])
                if recurse:
                    continue
                if low[node] == index_of[node]:
                    comp: List[str] = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == node:
                            break
                    result.append(sorted(comp))
                if work:
                    parent, _ = work[-1]
                    low[parent] = min(low[parent], low[node])
        return result

    def detect_cycles(self) -> List[Dict[str, Any]]:
        """检测循环依赖（如 A->B->C->A，或表自己写自己）。

        返回 ``[{"nodes": [...], "length": n, "path": [环节的头尾相连链路], "example": "...->..."}]``，
        按环路长度、节点名排序；空列表表示无环。
        """
        cycles: List[Dict[str, Any]] = []
        for comp in self._sccs():
            self_loop = len(comp) == 1 and comp[0] in self.successors(comp[0])
            if len(comp) == 1 and not self_loop:
                continue
            path = self._cycle_path(comp)
            cycles.append(
                {
                    "nodes": comp,
                    "length": len(comp),
                    "path": path,
                    "example": " -> ".join(path) if path else " -> ".join(comp + [comp[0]]),
                }
            )
        cycles.sort(key=lambda c: (c["length"], c["nodes"]))
        return cycles

    def _cycle_path(self, comp: List[str]) -> List[str]:
        """在强连通分量里找一个具体环路（首尾相接，返回时不重复首元素）。"""
        members = set(comp)
        start = comp[0]
        stack: List[str] = [start]
        visited: Set[str] = {start}

        def dfs(node: str) -> Optional[List[str]]:
            for nb in self.successors(node):
                if nb not in members:
                    continue
                if nb == start:
                    return list(stack)
                if nb in visited:
                    continue
                visited.add(nb)
                stack.append(nb)
                found = dfs(nb)
                if found:
                    return found
                stack.pop()
            return None

        found = dfs(start)
        return (found or []) + [start] if found else []

    def has_cycle(self) -> bool:
        return bool(self.detect_cycles())

    def topological_order(self) -> Optional[List[str]]:
        """拓扑排序（Kahn）。有环时返回 ``None``。"""
        indeg = {t: len(self.predecessors(t)) for t in self.nodes}
        heap = [t for t, d in indeg.items() if d == 0]
        heapq.heapify(heap)
        order: List[str] = []
        while heap:
            node = heapq.heappop(heap)
            order.append(node)
            for nb in self.successors(node):
                indeg[nb] -= 1
                if indeg[nb] == 0:
                    heapq.heappush(heap, nb)
        return order if len(order) == len(self.nodes) else None

    # ------------------------------------------------------------------ #
    # 统计
    # ------------------------------------------------------------------ #
    def depths(self) -> Dict[str, int]:
        """每个节点的最长血缘深度（源表=0）。有环时退化为按强连通分量压缩后的层级。"""
        sccs = self._sccs()
        comp_of: Dict[str, int] = {}
        for i, comp in enumerate(sccs):
            for t in comp:
                comp_of[t] = i
        indeg = [0] * len(sccs)
        out: Dict[int, Set[int]] = defaultdict(set)
        for (s, t) in self.edges:
            cs, ct = comp_of[s], comp_of[t]
            if cs == ct:
                continue
            if ct not in out[cs]:
                out[cs].add(ct)
                indeg[ct] += 1
        depth = [0] * len(sccs)
        queue = deque(i for i in range(len(sccs)) if indeg[i] == 0)
        while queue:
            i = queue.popleft()
            for j in sorted(out[i]):
                depth[j] = max(depth[j], depth[i] + 1)
                indeg[j] -= 1
                if indeg[j] == 0:
                    queue.append(j)
        return {t: depth[comp_of[t]] for t in self.nodes}

    def stats(self) -> Dict[str, Any]:
        """图统计：节点 / 边 / 根表 / 叶子 / 孤立 / 最大深度 / 分层分布 / 环路数。"""
        depths = self.depths()
        max_depth = max(depths.values()) if depths else 0
        deepest = sorted(t for t, d in depths.items() if d == max_depth)
        layer_counts: Dict[str, int] = {}
        for t in self.nodes:
            layer_counts[self.nodes[t].layer] = layer_counts.get(self.nodes[t].layer, 0) + 1
        cycles = self.detect_cycles()
        col_total = sum(e.column_mappings for e in self.edges.values())
        unresolved = sum(e.unresolved_mappings for e in self.edges.values())
        constants = sum(e.constant_mappings for e in self.edges.values())
        files = sorted({f for e in self.edges.values() for f in e.files if f})
        return {
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "root_count": len(self.source_tables()),
            "roots": self.source_tables(),
            "leaf_count": len(self.leaves()),
            "leaves": self.leaves(),
            "isolated_count": len(self.isolated()),
            "isolated": self.isolated(),
            "max_depth": max_depth,
            "deepest_tables": deepest,
            "layer_counts": layer_counts,
            "cycle_count": len(cycles),
            "has_cycle": bool(cycles),
            "column_mapping_count": col_total,
            "unresolved_column_mapping_count": unresolved,
            "constant_column_mapping_count": constants,
            "file_count": len(files),
            "files": files,
            "avg_out_degree": round(len(self.edges) / len(self.nodes), 3) if self.nodes else 0.0,
        }

    # ------------------------------------------------------------------ #
    # 子图 / 导出
    # ------------------------------------------------------------------ #
    def subgraph(self, tables: Iterable[str]) -> "LineageGraph":
        """抽取只含指定表（及其相互之间的边）的子图。"""
        keep = set(tables)
        sub = LineageGraph(dialect=self.dialect, root=self.root)
        for name in sorted(keep):
            if name in self.nodes:
                sub.nodes[name] = LineageNode.from_dict(self.nodes[name].to_dict())
        for (s, t), e in self.edges.items():
            if s in keep and t in keep:
                sub.edges[(s, t)] = LineageEdge.from_dict(e.to_dict())
                sub._out[s].append(t)
                sub._in[t].append(s)
        return sub

    def focus(self, table: str, depth: Optional[int] = None) -> Dict[str, Any]:
        """取「某张表 + 其上下游子图」，供可视化高亮使用。

        返回 ``{"center", "upstream": [...], "downstream": [...], "nodes": [...], "edges": [...]}``。
        """
        resolved = self.resolve_table(table)
        if resolved is None:
            return {"center": table, "found": False, "upstream": [], "downstream": [], "nodes": [], "edges": []}
        up = self.upstream(resolved, depth, max_paths=0).get("tables", [])
        down = self.downstream(resolved, depth, max_paths=0).get("tables", [])
        nodes = sorted({resolved, *up, *down})
        edges = [
            {"source": s, "target": t}
            for (s, t) in sorted(self.edges)
            if s in nodes and t in nodes
        ]
        return {
            "center": resolved,
            "found": True,
            "upstream": sorted(up),
            "downstream": sorted(down),
            "nodes": nodes,
            "edges": edges,
        }

    def to_dict(self) -> Dict[str, Any]:
        """序列化成可 JSON 落盘的结构（``from_dict`` 可无损读回）。"""
        return {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "dialect": self.dialect,
            "root": self.root,
            "nodes": [self.nodes[t].to_dict() for t in sorted(self.nodes)],
            "edges": [self.edges[k].to_dict() for k in sorted(self.edges)],
            "failures": self.failures,
            "scan_meta": self.scan_meta,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LineageGraph":
        g = cls(dialect=data.get("dialect", "hive"), root=data.get("root"))
        for nd in data.get("nodes") or []:
            node = LineageNode.from_dict(nd)
            g.nodes[node.name] = node
        for ed in data.get("edges") or []:
            edge = LineageEdge.from_dict(ed)
            g.edges[edge.key] = edge
            g._out[edge.source].append(edge.target)
            g._in[edge.target].append(edge.source)
        g.failures = list(data.get("failures") or [])
        g.scan_meta = dict(data.get("scan_meta") or {})
        return g

    def dumps(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def loads(cls, text: str) -> "LineageGraph":
        return cls.from_dict(json.loads(text))

    def save(self, path: Any) -> None:
        from pathlib import Path

        Path(path).write_text(self.dumps() + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Any) -> "LineageGraph":
        from pathlib import Path

        return cls.loads(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 便捷构造 & 文本渲染
# --------------------------------------------------------------------------- #
def build_graph(statements: Sequence[Dict[str, Any]], dialect: str = "hive",
                root: Optional[str] = None) -> LineageGraph:
    """从语句解析结果列表构建血缘图。"""
    g = LineageGraph(dialect=dialect, root=root)
    for stmt in statements:
        g.add_statement(stmt)
    return g


def build_graph_from_report(report: Dict[str, Any], root: Optional[str] = None) -> LineageGraph:
    """从 ``SqlLineageParser.aggregate()`` / ``parse_files()`` 的报告构建血缘图。"""
    g = build_graph(report.get("statements") or [], dialect=report.get("dialect", "hive"), root=root)
    g.scan_meta.update(
        {
            k: report[k]
            for k in ("file_count", "statement_count")
            if k in report
        }
    )
    return g


def format_analysis_text(result: Dict[str, Any], max_paths: int = 15) -> str:
    """把 upstream / downstream 的分析结果渲染成终端中文摘要。"""
    if not result.get("found"):
        lines = [f"!! 图中找不到表：{result.get('table')}"]
        cands = result.get("candidates") or []
        if cands:
            lines.append(f"   你是不是想找：{', '.join(cands)}")
        else:
            lines.append("   请先用 stats 子命令确认表名（支持只写表名不带库名）")
        return "\n".join(lines)

    is_up = result["direction"] == "upstream"
    title = "上游溯源（这张表的数据从哪来）" if is_up else "下游影响分析（改这张表会波及谁）"
    arrow = "<-" if is_up else "->"
    lines: List[str] = []
    lines.append("=" * 72)
    lines.append(f"{title}")
    lines.append(f"起点表：{result['table']}"
                 f"    深度限制：{'全部' if result.get('depth') is None else result['depth']}"
                 f"    方向：{'上游' if is_up else '下游'}")
    lines.append("=" * 72)
    lines.append(f"{'直接' + ('上游' if is_up else '下游')}（第 1 层，{len(result['direct'])} 张）："
                 + (", ".join(result["direct"]) if result["direct"] else "(无)"))

    if len(result["levels"]) > 1:
        lines.append("逐层展开：")
        for lvl in result["levels"]:
            lines.append(f"  第 {lvl['level']} 层（{len(lvl['tables'])} 张）：{', '.join(lvl['tables'])}")

    total = result["table_count"]
    lines.append(f"合计{'上游' if is_up else '下游'}表数：{total} 张    涉及血缘边：{result['edge_count']} 条")
    if result["levels"]:
        tail = result["roots"] if is_up else result.get("leaves", [])
        label = "最上游源表" if is_up else "最下游叶子表"
        lines.append(f"{label}（{len(tail)} 张）：" + (", ".join(tail) if tail else "(无)"))

    if result["paths"]:
        shown = result["paths"][:max_paths]
        lines.append(f"血缘链路（共 {len(result['paths'])} 条，展示前 {len(shown)} 条，"
                     f"箭头方向为血缘流向）：")
        for p in shown:
            chain = p if not is_up else list(reversed(p))
            lines.append("  " + f"  {arrow}  ".join(chain))
        if result.get("paths_truncated"):
            lines.append("  ...（链路过多，已截断）")
    lines.append("=" * 72)
    return "\n".join(lines)


def format_path_text(result: Dict[str, Any], max_paths: int = 10) -> str:
    """渲染 path_between 的结果。"""
    lines: List[str] = []
    lines.append("=" * 72)
    if not result.get("found"):
        if result.get("missing"):
            lines.append(f"!! 图中找不到表：{', '.join(result['missing'])}")
            for name, cands in (result.get("candidates") or {}).items():
                if cands:
                    lines.append(f"   {name} 的候选：{', '.join(cands)}")
        else:
            lines.append(
                f"两张表之间没有血缘链路：{result['source']}  -/->  {result['target']}"
            )
        lines.append("=" * 72)
        return "\n".join(lines)

    lines.append(f"血缘链路：{result['source']}  ->  {result['target']}")
    lines.append("=" * 72)
    lines.append(f"链路条数：{result['path_count']}    最短长度：{result['shortest_length']}（含首尾表）"
                 f"    {'直接相连' if result.get('direct') else '非直接相连'}")
    if result.get("shortest"):
        lines.append("最短链路：" + "  ->  ".join(result["shortest"]))
    if result["paths"]:
        lines.append(f"全部链路（展示前 {min(max_paths, len(result['paths']))} 条）：")
        for p in result["paths"][:max_paths]:
            lines.append("  " + "  ->  ".join(p) + f"    [{len(p) - 1} 跳]")
    if result.get("truncated"):
        lines.append("  ...（链路过多，已截断）")
    lines.append("=" * 72)
    return "\n".join(lines)


def format_cycles_text(cycles: List[Dict[str, Any]]) -> str:
    lines = ["=" * 72]
    if not cycles:
        lines.append("环路检测：未发现循环依赖 ✔（血缘图是有向无环图 DAG）")
        lines.append("=" * 72)
        return "\n".join(lines)
    lines.append(f"环路检测：发现 {len(cycles)} 个循环依赖 !!")
    lines.append("=" * 72)
    for i, c in enumerate(cycles, start=1):
        lines.append(f"[环路 {i}] 涉及 {c['length']} 张表：{', '.join(c['nodes'])}")
        lines.append(f"  示例环路：{c['example']}")
    lines.append("=" * 72)
    lines.append("提示：循环依赖会让调度无法确定执行顺序，需要人工确认是否 SQL 写错或存在回写。")
    return "\n".join(lines)


def format_stats_text(stats: Dict[str, Any]) -> str:
    lines = ["=" * 72]
    lines.append("血缘图统计")
    lines.append("=" * 72)
    lines.append(f"表（节点）数：{stats['node_count']}        血缘边数：{stats['edge_count']}"
                 f"        平均出度：{stats['avg_out_degree']}")
    lines.append(f"源表（无上游）：{stats['root_count']}    叶子表（无下游）：{stats['leaf_count']}"
                 f"    孤立表：{stats['isolated_count']}")
    lines.append(f"最大血缘深度：{stats['max_depth']} 层     最深表：{', '.join(stats['deepest_tables'][:5]) or '(无)'}")
    lines.append(f"循环依赖：{'有 ' + str(stats['cycle_count']) + ' 个' if stats['has_cycle'] else '无 ✔'}")
    lines.append(f"字段级映射：{stats['column_mapping_count']} 条（未解析 "
                 f"{stats['unresolved_column_mapping_count']} 条 / 常量 "
                 f"{stats['constant_column_mapping_count']} 条）")
    lines.append(f"涉及文件：{stats['file_count']} 个")
    layers = stats.get("layer_counts") or {}
    order = [l for l in LAYERS if l in layers]
    lines.append("分层分布：" + "  ".join(f"{l}={layers[l]}" for l in order))
    if stats["isolated"]:
        lines.append(f"孤立表：{', '.join(stats['isolated'][:10])}")
    lines.append("=" * 72)
    return "\n".join(lines)
