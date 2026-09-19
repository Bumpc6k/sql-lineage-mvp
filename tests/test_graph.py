"""血缘图引擎单元测试（P2）。

覆盖：构图 / 边元信息 / 上下游溯源与影响分析 / 深度限制 / 表名模糊解析 /
路径查询 / 环路检测 / 统计 / 拓扑排序 / JSON 序列化往返 / 文本渲染。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import pytest

from lineage.graph import (
    LAYERS,
    LineageGraph,
    build_graph,
    build_graph_from_report,
    format_analysis_text,
    format_cycles_text,
    format_path_text,
    format_stats_text,
    table_layer,
)
from lineage.parser import SqlLineageParser

# ---------------------------------------------------------------- 测试数据 --
CHAIN_SQL = """
-- 四层链路：src.raw_a -> ods.a -> dwd.b -> dws.c -> ads.d
CREATE TABLE ods.a AS
SELECT id, v FROM src.raw_a;

CREATE TABLE dwd.b AS
SELECT id, v * 2 AS v2 FROM ods.a;

INSERT OVERWRITE TABLE dws.c
SELECT id, SUM(v2) AS total FROM dwd.b GROUP BY id;

CREATE TABLE ads.d AS
SELECT id, total FROM dws.c;
"""

DIAMOND_SQL = """
-- 菱形：src.raw 有两条路径到达 ads.d
CREATE TABLE ods.a AS SELECT id FROM src.raw;
CREATE TABLE ods.a2 AS SELECT id FROM src.raw;
CREATE TABLE ads.d AS
SELECT a.id AS id, b.id AS id2
FROM ods.a a JOIN ods.a2 b ON a.id = b.id;
"""

CYCLE_SQL = """
CREATE TABLE x.a AS SELECT k FROM y.b;
CREATE TABLE y.b AS SELECT k FROM x.a;
"""

SELF_LOOP_SQL = """
CREATE TABLE z.t AS SELECT k + 1 AS k FROM z.t;
"""

ISOLATED_SQL = """
SELECT id FROM ods_只读表;
"""


def parse(sql: str, source: str = "test.sql") -> List[Dict]:
    return SqlLineageParser(dialect="hive").parse_sql(sql, source=source)


@pytest.fixture()
def chain_graph() -> LineageGraph:
    return build_graph(parse(CHAIN_SQL, "chain.sql"), dialect="hive")


@pytest.fixture()
def diamond_graph() -> LineageGraph:
    return build_graph(parse(DIAMOND_SQL, "diamond.sql"), dialect="hive")


# ---------------------------------------------------------------- 构图 --
def test_build_graph_nodes_and_edges(chain_graph: LineageGraph) -> None:
    assert set(chain_graph.nodes) == {"src.raw_a", "ods.a", "dwd.b", "dws.c", "ads.d"}
    assert set(chain_graph.edges) == {
        ("src.raw_a", "ods.a"),
        ("ods.a", "dwd.b"),
        ("dwd.b", "dws.c"),
        ("dws.c", "ads.d"),
    }
    # 邻接表方向：source -> target
    assert chain_graph.successors("ods.a") == ["dwd.b"]
    assert chain_graph.predecessors("dwd.b") == ["ods.a"]
    assert chain_graph.successors("ads.d") == []
    assert chain_graph.predecessors("src.raw_a") == []


def test_edge_metadata_carries_columns_files_and_statements(chain_graph: LineageGraph) -> None:
    edge = chain_graph.edge("ods.a", "dwd.b")
    assert edge is not None
    assert edge.column_mappings == 2          # id <- id, v2 <- v * 2
    assert edge.files == ["chain.sql"]
    assert edge.statements == [
        {"file": "chain.sql", "statement_index": 2, "task_type": "CTAS"}
    ]
    assert {c["target_column"] for c in edge.columns} == {"id", "v2"}
    assert all(c["resolved"] for c in edge.columns)


def test_node_layer_inference(chain_graph: LineageGraph) -> None:
    assert chain_graph.nodes["src.raw_a"].layer == "src"
    assert chain_graph.nodes["ods.a"].layer == "ods"
    assert chain_graph.nodes["dwd.b"].layer == "dwd"
    assert chain_graph.nodes["dws.c"].layer == "dws"
    assert chain_graph.nodes["ads.d"].layer == "ads"


def test_node_produced_by_and_consumed_by(chain_graph: LineageGraph) -> None:
    node = chain_graph.nodes["ods.a"]
    assert node.produced_by == [
        {"file": "chain.sql", "statement_index": 1, "task_type": "CTAS"}
    ]
    assert node.consumed_by[0]["statement_index"] == 2
    # 源表：只被读、不被写
    assert chain_graph.nodes["src.raw_a"].produced_by == []
    assert chain_graph.nodes["src.raw_a"].ref_count == 1


def test_build_graph_merges_same_table_across_files() -> None:
    stmts = parse("CREATE TABLE ods.a AS SELECT id FROM src.raw_a;", "f1.sql")
    stmts += parse("INSERT OVERWRITE TABLE ods.a SELECT id FROM src.raw_b;", "f2.sql")
    g = build_graph(stmts)
    assert set(g.predecessors("ods.a")) == {"src.raw_a", "src.raw_b"}
    assert sorted(g.nodes["ods.a"].produced_by[0].keys()) == ["file", "statement_index", "task_type"]
    assert len(g.nodes["ods.a"].produced_by) == 2
    # 两条边的来源文件各自独立
    assert g.edge("src.raw_a", "ods.a").files == ["f1.sql"]
    assert g.edge("src.raw_b", "ods.a").files == ["f2.sql"]


def test_build_graph_from_report() -> None:
    parser = SqlLineageParser("hive")
    stmts = parse(CHAIN_SQL)
    report = parser.aggregate(stmts)
    g = build_graph_from_report(report)
    assert len(g.nodes) == 5
    assert len(g.edges) == 4
    assert g.scan_meta["statement_count"] == 4


def test_select_only_statement_creates_node_without_edge() -> None:
    g = build_graph(parse(ISOLATED_SQL))
    assert "ods_只读表" in g.nodes
    assert g.edges == {}
    assert g.isolated() == ["ods_只读表"]


# ---------------------------------------------------------------- 表名解析 --
def test_resolve_table_fuzzy(chain_graph: LineageGraph) -> None:
    assert chain_graph.resolve_table("ods.a") == "ods.a"
    assert chain_graph.resolve_table("ODS.A") == "ods.a"
    assert chain_graph.resolve_table("a") == "ods.a"        # 只写表名
    assert chain_graph.resolve_table("不存在的表") is None
    assert chain_graph.resolve_table("ads.d") in chain_graph
    assert chain_graph.resolve_table("") is None


def test_candidates_lists_fuzzy_hits(diamond_graph: LineageGraph) -> None:
    assert diamond_graph.candidates("a") == ["ods.a"]
    assert diamond_graph.candidates("raw") == ["src.raw"]
    assert diamond_graph.candidates("nope") == []


# ---------------------------------------------------------------- 上下游 --
def test_upstream_levels_and_tables(chain_graph: LineageGraph) -> None:
    res = chain_graph.upstream("ads.d")
    assert res["found"] is True
    assert res["direct"] == ["dws.c"]
    assert [lvl["tables"] for lvl in res["levels"]] == [["dws.c"], ["dwd.b"], ["ods.a"], ["src.raw_a"]]
    assert res["table_count"] == 4
    assert res["roots"] == ["src.raw_a"]
    assert res["paths"] == [["ads.d", "dws.c", "dwd.b", "ods.a", "src.raw_a"]]
    assert len(res["edges"]) == 4


def test_upstream_depth_limit(chain_graph: LineageGraph) -> None:
    res = chain_graph.upstream("ads.d", depth=2)
    assert res["table_count"] == 2
    assert res["tables"] == ["dws.c", "dwd.b"]
    assert res["levels"][-1]["level"] == 2


def test_downstream_impact(chain_graph: LineageGraph) -> None:
    res = chain_graph.downstream("src.raw_a")
    assert res["direct"] == ["ods.a"]
    assert res["table_count"] == 4
    assert res["levels"][0]["tables"] == ["ods.a"]
    assert res["leaves"] == ["ads.d"]
    assert res["paths"] == [["src.raw_a", "ods.a", "dwd.b", "dws.c", "ads.d"]]

    limited = chain_graph.downstream("src.raw_a", depth=1)
    assert limited["tables"] == ["ods.a"]
    assert limited["paths"] == [["src.raw_a", "ods.a"]]


def test_downstream_of_leaf_is_empty(chain_graph: LineageGraph) -> None:
    res = chain_graph.downstream("ads.d")
    assert res["table_count"] == 0
    assert res["direct"] == []
    assert res["paths"] == [["ads.d"]]      # 只含起点自身


def test_upstream_table_not_found(chain_graph: LineageGraph) -> None:
    res = chain_graph.upstream("不存在")
    assert res["found"] is False
    assert res["candidates"] == []
    assert "找不到" in format_analysis_text(res)


def test_ancestors_descendants_sets(chain_graph: LineageGraph) -> None:
    assert chain_graph.ancestors("ads.d") == {"dws.c", "dwd.b", "ods.a", "src.raw_a"}
    assert chain_graph.descendants("ods.a") == {"dwd.b", "dws.c", "ads.d"}
    assert chain_graph.ancestors("ads.d", depth=1) == {"dws.c"}


# ---------------------------------------------------------------- 路径 --
def test_path_between_returns_all_paths(diamond_graph: LineageGraph) -> None:
    res = diamond_graph.path_between("src.raw", "ads.d")
    assert res["found"] is True
    assert res["path_count"] == 2
    assert res["paths"] == [
        ["src.raw", "ods.a", "ads.d"],
        ["src.raw", "ods.a2", "ads.d"],
    ]
    assert res["shortest"] == ["src.raw", "ods.a", "ads.d"]
    assert res["shortest_length"] == 3
    assert res["direct"] is False


def test_path_between_direct_edge(chain_graph: LineageGraph) -> None:
    res = chain_graph.path_between("src.raw_a", "ods.a")
    assert res["direct"] is True
    assert res["shortest_length"] == 2
    text = format_path_text(res)
    assert "直接相连" in text


def test_path_between_no_route(chain_graph: LineageGraph) -> None:
    res = chain_graph.path_between("ads.d", "src.raw_a")   # 反向不存在
    assert res["found"] is False
    assert res["path_count"] == 0
    assert "没有血缘链路" in format_path_text(res)


def test_path_between_unknown_table(chain_graph: LineageGraph) -> None:
    res = chain_graph.path_between("src.raw_a", "没有这张表")
    assert res["found"] is False
    assert res["missing"] == ["没有这张表"]
    assert "找不到表" in format_path_text(res)


def test_path_max_paths_truncates(diamond_graph: LineageGraph) -> None:
    res = diamond_graph.path_between("src.raw", "ads.d", max_paths=1)
    assert res["path_count"] == 1
    assert res["truncated"] is True


# ---------------------------------------------------------------- 环路 --
def test_detect_cycles_none_for_dag(chain_graph: LineageGraph) -> None:
    assert chain_graph.detect_cycles() == []
    assert chain_graph.has_cycle() is False
    assert "未发现循环依赖" in format_cycles_text([])


def test_detect_cycles_two_node_loop() -> None:
    g = build_graph(parse(CYCLE_SQL))
    cycles = g.detect_cycles()
    assert len(cycles) == 1
    assert cycles[0]["nodes"] == ["x.a", "y.b"]
    assert cycles[0]["length"] == 2
    assert cycles[0]["example"] == "x.a -> y.b -> x.a"
    assert g.has_cycle() is True
    assert "循环依赖" in format_cycles_text(cycles)


def test_detect_cycles_self_loop() -> None:
    g = build_graph(parse(SELF_LOOP_SQL))
    cycles = g.detect_cycles()
    assert len(cycles) == 1
    assert cycles[0]["nodes"] == ["z.t"]
    assert cycles[0]["path"] == ["z.t", "z.t"]


def test_topological_order_dag_and_cycle(chain_graph: LineageGraph) -> None:
    order = chain_graph.topological_order()
    assert order is not None
    assert order.index("src.raw_a") < order.index("ods.a") < order.index("ads.d")
    cyclic = build_graph(parse(CYCLE_SQL))
    assert cyclic.topological_order() is None


# ---------------------------------------------------------------- 统计 --
def test_stats_for_chain(chain_graph: LineageGraph) -> None:
    stats = chain_graph.stats()
    assert stats["node_count"] == 5
    assert stats["edge_count"] == 4
    assert stats["root_count"] == 1
    assert stats["roots"] == ["src.raw_a"]
    assert stats["leaf_count"] == 1
    assert stats["leaves"] == ["ads.d"]
    assert stats["isolated_count"] == 0
    assert stats["max_depth"] == 4
    assert stats["deepest_tables"] == ["ads.d"]
    assert stats["has_cycle"] is False
    assert stats["cycle_count"] == 0
    assert stats["file_count"] == 1
    assert stats["layer_counts"] == {"src": 1, "ods": 1, "dwd": 1, "dws": 1, "ads": 1}
    assert stats["column_mapping_count"] == 8       # 2 + 2 + 2 + 2
    assert stats["unresolved_column_mapping_count"] == 0
    assert stats["avg_out_degree"] == 0.8


def test_stats_counts_isolated_tables(chain_graph: LineageGraph) -> None:
    g = build_graph(parse(CHAIN_SQL) + parse(ISOLATED_SQL))
    stats = g.stats()
    assert stats["isolated"] == ["ods_只读表"]
    assert stats["isolated_count"] == 1
    assert stats["node_count"] == 6


def test_stats_for_cyclic_graph_does_not_crash() -> None:
    g = build_graph(parse(CYCLE_SQL))
    stats = g.stats()
    assert stats["cycle_count"] == 1
    assert stats["has_cycle"] is True
    assert stats["max_depth"] == 0        # 整个图是一个环，压缩后层级为 0


def test_tables_by_layer(chain_graph: LineageGraph) -> None:
    by_layer = chain_graph.tables_by_layer()
    assert by_layer["ods"] == ["ods.a"]
    assert set(by_layer) <= set(LAYERS)


# ---------------------------------------------------------------- 序列化 --
def test_to_dict_from_dict_roundtrip(chain_graph: LineageGraph) -> None:
    data = chain_graph.to_dict()
    assert json.loads(json.dumps(data))["schema_version"] == 1
    restored = LineageGraph.from_dict(data)
    assert restored.to_dict() == data
    assert set(restored.nodes) == set(chain_graph.nodes)
    assert set(restored.edges) == set(chain_graph.edges)
    # 读回后分析能力一致
    assert restored.upstream("ads.d")["table_count"] == chain_graph.upstream("ads.d")["table_count"]
    assert restored.stats() == chain_graph.stats()


def test_dumps_loads_and_file_roundtrip(chain_graph: LineageGraph, tmp_path: Path) -> None:
    text = chain_graph.dumps()
    assert "src.raw_a" in text
    again = LineageGraph.loads(text)
    assert again.stats()["edge_count"] == 4

    out = tmp_path / "graph.json"
    chain_graph.save(out)
    assert out.exists()
    loaded = LineageGraph.load(out)
    assert loaded.to_dict() == chain_graph.to_dict()


def test_roundtrip_keeps_chinese_readable(chain_graph: LineageGraph) -> None:
    n = chain_graph.nodes["ads.d"]
    assert n.name == "ads.d"
    text = json.dumps(chain_graph.to_dict(), ensure_ascii=False)
    assert "ads.d" in text


# ---------------------------------------------------------------- 子图 --
def test_subgraph_and_focus(chain_graph: LineageGraph) -> None:
    sub = chain_graph.subgraph(["ods.a", "dwd.b"])
    assert set(sub.nodes) == {"ods.a", "dwd.b"}
    assert set(sub.edges) == {("ods.a", "dwd.b")}

    focus = chain_graph.focus("dwd.b")
    assert focus["center"] == "dwd.b"
    assert focus["upstream"] == ["ods.a", "src.raw_a"]
    assert focus["downstream"] == ["ads.d", "dws.c"]
    assert focus["nodes"] == ["ads.d", "dwd.b", "dws.c", "ods.a", "src.raw_a"]
    assert len(focus["edges"]) == 4


def test_focus_depth_limit(chain_graph: LineageGraph) -> None:
    focus = chain_graph.focus("dws.c", depth=1)
    assert focus["upstream"] == ["dwd.b"]
    assert focus["downstream"] == ["ads.d"]


# ---------------------------------------------------------------- 分层判定 --
@pytest.mark.parametrize(
    "name,expected",
    [
        ("src.erp_产量", "src"),
        ("stg.中间表", "stg"),
        ("ods.ods_卷烟产量", "ods"),
        ("dim.dim_plant", "dim"),
        ("cdw.dwd_卷烟产量明细", "dwd"),
        ("cdw.dws_产量汇总", "dws"),
        ("ads.ads_产销存月报", "ads"),
        ("app.某接口输出", "app"),
        ("whatever.随便一张表", "other"),
        ("", "other"),
    ],
)
def test_table_layer_inference(name: str, expected: str) -> None:
    assert table_layer(name) == expected


# ---------------------------------------------------------------- 文本输出 --
def test_format_stats_text_contains_key_metrics(chain_graph: LineageGraph) -> None:
    text = format_stats_text(chain_graph.stats())
    assert "表（节点）数：5" in text
    assert "血缘边数：4" in text
    assert "最大血缘深度：4 层" in text


def test_format_analysis_text_suggests_candidates(chain_graph: LineageGraph) -> None:
    res = chain_graph.upstream("c")     # 模糊表名能自动命中 dws.c
    assert res["found"] is True
    assert res["table"] == "dws.c"
    text = format_analysis_text(res)
    assert "上游溯源" in text
    assert "最上游源表" in text
