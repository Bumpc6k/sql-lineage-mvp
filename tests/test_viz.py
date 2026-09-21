"""可视化输出测试（P2）：Mermaid 文本 + 自包含交互式 HTML。

除结构检查外，本文件用 **QuickJS 真正执行** HTML 里的内联 JS（配 DOM 桩），
验证力导向布局、点击节点后的上下游高亮、搜索、分层筛选等交互逻辑确实能跑通，
而不是"只生成了一个 HTML 文件"。若环境没装 quickjs，该用例自动跳过。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

import pytest

from lineage_core.graph import LineageGraph, build_graph
from lineage_core.parser import SqlLineageParser
from lineage.collect.scan import scan_directory
from lineage.render.viz import (
    LAYER_COLORS,
    LAYER_LABELS,
    graph_to_viz_data,
    to_html,
    to_mermaid,
    write_html,
    write_mermaid,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = PROJECT_ROOT / "examples" / "warehouse"

CHAIN_SQL = """
CREATE TABLE ods.a AS SELECT id, v FROM src.raw_a;
CREATE TABLE cdw.dwd_b AS SELECT id, v * 2 AS v2 FROM ods.a;
INSERT OVERWRITE TABLE cdw.dws_c SELECT id, SUM(v2) AS total FROM cdw.dwd_b GROUP BY id;
CREATE TABLE ads.d AS SELECT id, total FROM cdw.dws_c;
"""


@pytest.fixture(scope="module")
def warehouse_graph() -> LineageGraph:
    return scan_directory(WAREHOUSE).graph


@pytest.fixture(scope="module")
def chain_graph() -> LineageGraph:
    stmts = SqlLineageParser("hive").parse_sql(CHAIN_SQL, source="chain.sql")
    return build_graph(stmts)


# ---------------------------------------------------------------- Mermaid --
def test_mermaid_basic_structure(chain_graph: LineageGraph) -> None:
    text = to_mermaid(chain_graph)
    assert text.startswith("flowchart LR")
    assert "subgraph sg_src" in text
    assert "subgraph sg_ods" in text
    assert '["src.raw_a"]' in text
    assert '["ads.d"]' in text
    # 4 条边
    assert text.count("-->") == 4
    # 分层中文名
    assert "源系统层" in text and "ADS 应用层" in text


def test_mermaid_direction_and_grouping_options(chain_graph: LineageGraph) -> None:
    assert to_mermaid(chain_graph, direction="TD").startswith("flowchart TD")
    assert "subgraph" not in to_mermaid(chain_graph, group_layers=False)
    assert "字段" not in to_mermaid(chain_graph, show_edge_labels=False)
    with pytest.raises(ValueError):
        to_mermaid(chain_graph, direction="SIDEWAYS")


def test_mermaid_edge_labels_show_column_counts(chain_graph: LineageGraph) -> None:
    text = to_mermaid(chain_graph)
    assert '"2 字段"' in text


def test_mermaid_highlight_classes(warehouse_graph: LineageGraph) -> None:
    # 选一张既有上游又有下游的中间表，上游标蓝、下游标橙、自己标黄
    text = to_mermaid(warehouse_graph, highlight="cdw.dws_产销存汇总", depth=2)
    assert "classDef hlc" in text and "classDef hlu" in text and "classDef hld" in text
    classes = re.findall(r"class (\w+) (hlc|hlu|hld)", text)
    kinds = {k for _, k in classes}
    assert kinds == {"hlc", "hlu", "hld"}
    assert sum(1 for _, k in classes if k == "hlc") == 1


def test_mermaid_only_highlight_crops_graph(warehouse_graph: LineageGraph) -> None:
    full = to_mermaid(warehouse_graph)
    cropped = to_mermaid(warehouse_graph, highlight="ods.ods_卷烟产量流水", depth=2, only_highlight=True)
    assert len(cropped.splitlines()) < len(full.splitlines())
    assert '["ods.ods_卷烟产量流水"]' in cropped
    assert "ads.ads_经营指标驾驶舱" not in cropped


def test_mermaid_unknown_highlight_raises(warehouse_graph: LineageGraph) -> None:
    with pytest.raises(ValueError) as exc:
        to_mermaid(warehouse_graph, highlight="根本不存在")
    assert "找不到表" in str(exc.value)


def test_mermaid_escaping_of_special_chars() -> None:
    stmts = SqlLineageParser("hive").parse_sql(
        'CREATE TABLE `db.t|x` AS SELECT id FROM ods.a;', source="x.sql"
    )
    text = to_mermaid(build_graph(stmts))
    assert "#124;" in text


def test_write_mermaid_file(chain_graph: LineageGraph, tmp_path: Path) -> None:
    out = write_mermaid(chain_graph, tmp_path / "lineage.mmd")
    assert out.exists()
    assert out.read_text(encoding="utf-8").startswith("flowchart LR")


# ---------------------------------------------------------------- 可视化数据 --
def test_graph_to_viz_data_structure(warehouse_graph: LineageGraph) -> None:
    data = graph_to_viz_data(warehouse_graph, title="烟草数仓血缘图")
    assert data["title"] == "烟草数仓血缘图"
    assert len(data["nodes"]) == len(warehouse_graph.nodes)
    assert len(data["edges"]) == len(warehouse_graph.edges)
    assert len(data["out_adj"]) == len(data["nodes"])
    assert data["layers"] == ["src", "ods", "dim", "dwd", "dws", "ads"]
    assert data["stats"]["max_depth"] == 7
    assert data["layer_labels"] == LAYER_LABELS
    assert set(data["layer_colors"]) == set(LAYER_COLORS)
    # 邻接表与节点索引一致
    idx = {n["name"]: n["i"] for n in data["nodes"]}
    for s, t in list(warehouse_graph.edges)[:5]:
        assert idx[t] in data["out_adj"][idx[s]]
        assert idx[s] in data["in_adj"][idx[t]]


def test_graph_to_viz_data_marks_cycles() -> None:
    stmts = SqlLineageParser("hive").parse_sql(
        "CREATE TABLE x.a AS SELECT k FROM y.b;\nCREATE TABLE y.b AS SELECT k FROM x.a;",
        source="cycle.sql",
    )
    data = graph_to_viz_data(build_graph(stmts))
    assert len(data["cycle_nodes"]) == 2
    assert data["stats"]["cycle_count"] == 1


def test_graph_to_viz_data_center(warehouse_graph: LineageGraph) -> None:
    data = graph_to_viz_data(warehouse_graph, center="ads_经营指标驾驶舱")
    assert data["center"] is not None
    assert data["nodes"][data["center"]]["name"] == "ads.ads_经营指标驾驶舱"
    assert data["nodes"][data["center"]]["out"] == 0


# ---------------------------------------------------------------- HTML 结构 --
def test_html_is_self_contained(warehouse_graph: LineageGraph) -> None:
    html = to_html(warehouse_graph)
    assert html.startswith("<!DOCTYPE html>")
    assert html.strip().endswith("</html>")
    # 无任何外链依赖：没有 http(s)、没有 <link>、没有 src=
    assert "http://" not in html
    assert "https://" not in html
    assert "<link" not in html
    assert "src=" not in html
    assert "cdn" not in html.lower()
    assert "unpkg" not in html and "jsdelivr" not in html
    # 内联 CSS / JS
    assert "<style>" in html and "</style>" in html
    assert html.count("<script>") == 1
    assert "getContext" in html and "addEventListener" in html


def test_html_embeds_expected_interactions(warehouse_graph: LineageGraph) -> None:
    html = to_html(warehouse_graph, title="烟草数仓血缘图")
    for feature in ("id=\"search\"", "id=\"layers\"", "btn-fit", "btn-reset", "btn-png",
                    "wheel", "mousedown", "dblclick", "toDataURL"):
        assert feature in html, feature
    assert "烟草数仓血缘图" in html


def test_html_payload_matches_graph(warehouse_graph: LineageGraph) -> None:
    html = to_html(warehouse_graph)
    payload = re.search(r"const DATA = (\{.*?\});\n", html, re.S).group(1)
    data = json.loads(payload)
    assert len(data["nodes"]) == 34
    assert len(data["edges"]) == 41
    assert "ads.ads_经营指标驾驶舱" in [n["name"] for n in data["nodes"]]


def test_write_html_file(warehouse_graph: LineageGraph, tmp_path: Path) -> None:
    out = write_html(warehouse_graph, tmp_path / "lineage.html", title="血缘图")
    assert out.exists()
    assert out.stat().st_size > 20000          # 内联了 JS/CSS，不可能只有几 KB


def test_html_size_is_reasonable(warehouse_graph: LineageGraph) -> None:
    size_kb = len(to_html(warehouse_graph).encode("utf-8")) / 1024
    assert 20 < size_kb < 500, f"HTML 体积异常：{size_kb:.1f} KB"


# ---------------------------------------------------------------- 真跑 JS --
DOM_STUB = r"""
var __calls = {};
var __ctx = new Proxy({}, {
  get: function(t, p) { if (p in t) return t[p]; return function() { __calls[p] = (__calls[p]||0)+1; }; },
  set: function(t, p, v) { t[p] = v; return true; }
});
function __el(id) {
  return {
    id: id, textContent: "", innerHTML: "", value: "", checked: true, style: {}, dataset: {},
    classList: {add: function(){}, remove: function(){}},
    addEventListener: function(){}, dispatchEvent: function(){},
    querySelectorAll: function(){ return []; }, querySelector: function(){ return null; },
    getContext: function(){ return __ctx; }, toDataURL: function(){ return "data:image/png;base64,AA"; },
    click: function(){}, width: 0, height: 0, clientWidth: 1200, clientHeight: 800,
    getBoundingClientRect: function(){ return {left:0, top:0, width:1200, height:800}; },
    offsetX: 0, offsetY: 0
  };
}
var __elements = {};
var document = {
  getElementById: function(id){ if (!__elements[id]) __elements[id] = __el(id); return __elements[id]; },
  querySelectorAll: function(){ return []; }, querySelector: function(){ return null; },
  createElement: function(tag){ return __el(tag); }
};
var window = { addEventListener: function(){}, devicePixelRatio: 1, innerWidth: 1200, innerHeight: 800 };
"""

JS_DRIVER = r"""
var __out = {ok: true};
try {
  __out.dataNodes = DATA.nodes.length;
  __out.dataEdges = DATA.edges.length;
  __out.positions = positions.length;
  __out.initialCenter = sel;
  __out.arcs = __calls["arc"] || 0;
  __out.arrows = __calls["quadraticCurveTo"] || 0;
  __out.labelDraws = __calls["fillText"] || 0;
  var target = DATA.nodes.findIndex(function(n){ return n.name === "__TARGET__"; });
  select(target);
  __out.selName = DATA.nodes[sel].name;
  __out.up = Array.from(upSet).filter(function(x){ return x !== sel; }).sort(function(a,b){return a-b;});
  __out.down = Array.from(downSet).filter(function(x){ return x !== sel; }).sort(function(a,b){return a-b;});
  __out.upNames = __out.up.map(function(i){ return DATA.nodes[i].name; });
  __out.maxUpDepth = Math.max.apply(null, __out.up.map(function(i){ return upDepth[i]; }));
  __out.maxDownDepth = Math.max.apply(null, __out.down.map(function(i){ return downDepth[i]; }));
  var detail = document.getElementById("detail").innerHTML;
  __out.detailHasName = detail.indexOf(DATA.nodes[sel].name) >= 0;
  __out.detailHasUpstream = detail.indexOf("上游溯源") >= 0;
  __out.detailHasDownstream = detail.indexOf("下游影响") >= 0;
  __out.detailHasPaths = detail.indexOf("血缘链路") >= 0;
  __out.detailLen = detail.length;
  __out.pathsUp = pathsFrom(sel, DATA.in_adj, 12).length;
  __out.pathsDown = pathsFrom(sel, DATA.out_adj, 12).length;
  var sb = document.getElementById("search");
  sb.value = "产销存";
  runSearch();
  __out.searchHtmlLen = document.getElementById("results").innerHTML.length;
  __out.searchHitCount = (document.getElementById("results").innerHTML.match(/class="row"/g) || []).length;
  hidden.add("src");
  __out.hiddenWorks = hidden.has("src");
  hidden.delete("src");
  fitView();
  __out.viewK = view.k;
  centerOn(1);
  __out.centerOnChangedView = (view.x !== 0 || view.y !== 0);
} catch (e) {
  __out.ok = false; __out.error = String(e);
  __out.stack = e.stack ? String(e.stack).slice(0, 300) : "";
}
JSON.stringify(__out);
"""


def _run_viz_js(html: str, target_table: str) -> Dict:
    quickjs = pytest.importorskip("quickjs", reason="需要可选依赖 quickjs 才能执行内联 JS")
    script = re.search(r"<script>(.*)</script>", html, re.S).group(1)
    code = DOM_STUB + script + "\n" + JS_DRIVER.replace("__TARGET__", target_table)
    return json.loads(quickjs.Context().eval(code))


def _bfs_distances(graph: LineageGraph, start: str, direction: str) -> Dict[str, int]:
    neighbors = graph.predecessors if direction == "up" else graph.successors
    dist = {start: 0}
    frontier = [start]
    while frontier:
        nxt: List[str] = []
        for node in frontier:
            for nb in neighbors(node):
                if nb not in dist:
                    dist[nb] = dist[node] + 1
                    nxt.append(nb)
        frontier = nxt
    return dist


def test_html_js_runs_and_highlights_correctly(warehouse_graph: LineageGraph) -> None:
    """在 QuickJS 里真跑一遍 HTML 的内联 JS，比对 JS 算出的上下游与 Python 引擎结果。"""
    target = "cdw.dws_产销存汇总"
    out = _run_viz_js(to_html(warehouse_graph), target)
    assert out["ok"] is True, f"JS 执行报错：{out.get('error')} @ {out.get('stack')}"
    assert out["dataNodes"] == 34
    assert out["dataEdges"] == 41
    assert out["positions"] == 34
    assert out["selName"] == target

    # JS 的上下游集合必须与 Python 血缘图引擎一致
    idx = {t: i for i, t in enumerate(sorted(warehouse_graph.nodes))}
    exp_up = sorted(idx[t] for t in warehouse_graph.upstream(target, max_paths=0)["tables"])
    exp_down = sorted(idx[t] for t in warehouse_graph.downstream(target, max_paths=0)["tables"])
    assert out["up"] == exp_up
    assert out["down"] == exp_down
    # 层级（BFS 距离）也要一致
    up_dist = _bfs_distances(warehouse_graph, target, "up")
    down_dist = _bfs_distances(warehouse_graph, target, "down")
    assert out["maxUpDepth"] == max(up_dist.values())
    assert out["maxDownDepth"] == max(down_dist.values())

    # 渲染确实发生了：每个节点画了圆、每条边画了曲线
    assert out["arcs"] == 34
    assert out["arrows"] == 41

    # 右侧详情面板渲染了上下游与链路
    assert out["detailHasName"] and out["detailHasUpstream"] and out["detailHasDownstream"]
    assert out["detailHasPaths"] and out["detailLen"] > 500
    assert out["pathsUp"] >= 1 and out["pathsDown"] >= 1

    # 搜索与分层筛选
    assert out["searchHitCount"] >= 2
    assert out["hiddenWorks"] is True
    # 视图操作
    assert out["viewK"] > 0
    assert out["centerOnChangedView"] is True


def test_html_js_handles_leaf_and_source_tables(warehouse_graph: LineageGraph) -> None:
    leaf = _run_viz_js(to_html(warehouse_graph), "ads.ads_经营指标驾驶舱")
    assert leaf["ok"] is True
    assert leaf["down"] == []                      # 叶子表没有下游
    assert leaf["maxUpDepth"] == 7                 # 最深溯源 7 跳

    src = _run_viz_js(to_html(warehouse_graph), "src.erp_生产工单明细")
    assert src["ok"] is True
    assert src["up"] == []                         # 源表没有上游
    assert src["maxDownDepth"] >= 5
