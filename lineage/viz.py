"""可视化输出（P2）：Mermaid 文本图 + 自包含交互式 HTML。

两个导出器
----------
* :func:`to_mermaid` —— 生成 Mermaid ``flowchart`` 文本，直接贴进 README / Markdown /
  GitLab / 语雀就能渲染成图。支持分层 subgraph 分组、上下游高亮、边上的字段映射条数。
* :func:`to_html` —— 生成 **单文件 HTML**：内联 CSS + 原生 JS（无第三方库、无 CDN、
  无外网请求），双击即可离线打开。用 canvas 力导向布局展示血缘图，支持：

  - 点击节点 → 高亮其上游（蓝）/ 下游（橙），右侧面板展示该表的上游、下游、
    出边入边明细与血缘链路；
  - 搜索表名（回车定位并居中）；
  - 分层筛选（ods / dwd / dws / ads ... 勾选显示）；
  - 拖拽节点、拖拽画布平移、滚轮缩放、双击适配画布、一键导出 PNG。

实现上刻意 **不依赖任何外部 JS 库**：演示环境可能没有外网，内联 d3/vis.js 又会把
HTML 撑到几百 KB，自己写一个 300 行的力导向渲染器反而更稳、更小、更好讲。

用法::

    from lineage.viz import to_mermaid, to_html

    print(to_mermaid(graph))                       # 全图
    print(to_mermaid(graph, highlight="ads.ads_卷烟产销月报", depth=2))
    Path("lineage.html").write_text(to_html(graph, title="烟草数仓血缘图"), encoding="utf-8")
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .graph import LAYERS, LineageGraph

__all__ = ["LAYER_LABELS", "LAYER_COLORS", "graph_to_viz_data", "to_html", "to_mermaid"]

#: 分层中文名（用于 Mermaid subgraph 标题与 HTML 图例）
LAYER_LABELS: Dict[str, str] = {
    "src": "源系统层",
    "stg": "贴源缓冲层",
    "ods": "ODS 贴源层",
    "dim": "维度层",
    "dwd": "DWD 明细层",
    "dws": "DWS 汇总层",
    "ads": "ADS 应用层",
    "app": "应用输出层",
    "other": "其他",
}

#: 分层配色（Mermaid classDef 与 HTML 节点共用）
LAYER_COLORS: Dict[str, str] = {
    "src": "#64748b",
    "stg": "#0ea5e9",
    "ods": "#22c55e",
    "dim": "#a855f7",
    "dwd": "#3b82f6",
    "dws": "#f59e0b",
    "ads": "#ef4444",
    "app": "#ec4899",
    "other": "#94a3b8",
}

_LAYER_ORDER = {name: i for i, name in enumerate(LAYERS)}


def _sort_key(table: str, graph: LineageGraph) -> Tuple[int, str]:
    layer = graph.nodes[table].layer if table in graph.nodes else "other"
    return (_LAYER_ORDER.get(layer, len(LAYERS)), table)


def _escape_mermaid_label(text: str) -> str:
    """Mermaid 节点标签转义（引号 / 尖括号 / 竖线等会破坏语法）。"""
    return (
        text.replace("&", "&amp;")
        .replace('"', "#quot;")
        .replace("<", "#lt;")
        .replace(">", "#gt;")
        .replace("|", "#124;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
    )


# --------------------------------------------------------------------------- #
# Mermaid
# --------------------------------------------------------------------------- #
def to_mermaid(
    graph: LineageGraph,
    direction: str = "LR",
    highlight: Optional[str] = None,
    depth: Optional[int] = None,
    group_layers: bool = True,
    show_edge_labels: bool = True,
    only_highlight: bool = False,
    title: Optional[str] = None,
) -> str:
    """把血缘图导出成 Mermaid flowchart 文本。

    参数：
        direction: ``LR``（左到右，适合链路图）或 ``TD``（上到下）。
        highlight: 要高亮的中心表名（支持只写表名不带库名）；其上游标蓝、下游标橙、
            中心表标黄。为 ``None`` 时不打高亮类。
        depth: 与 ``highlight`` 搭配，限制高亮子图的上下游深度。
        group_layers: 是否按 ods/dwd/dws/ads 分层用 ``subgraph`` 分组。
        show_edge_labels: 边上显示字段映射条数。
        only_highlight: 只画 ``highlight`` 的上下游子图（裁剪大图时用）。
        title: 图顶部注释。
    """
    if direction.upper() not in ("LR", "RL", "TD", "TB", "BT"):
        raise ValueError(f"不支持的 direction：{direction}（可选 LR/RL/TD/TB/BT）")

    focus: Dict[str, Any] = {}
    tables = list(graph.nodes)
    if highlight:
        focus = graph.focus(highlight, depth)
        if not focus.get("found"):
            raise ValueError(f"图中找不到表：{highlight}（候选：{graph.candidates(highlight)}）")
        if only_highlight:
            tables = [t for t in focus["nodes"] if t in graph.nodes]

    tables = sorted(tables, key=lambda t: _sort_key(t, graph))
    node_id = {t: f"n{i}" for i, t in enumerate(tables)}

    lines: List[str] = []
    if title:
        lines.append(f"%% {title}")
    lines.append(f"flowchart {direction.upper()}")

    up = set(focus.get("upstream") or [])
    down = set(focus.get("downstream") or [])
    center = focus.get("center")

    # 节点定义（按分层分组）
    if group_layers:
        by_layer: Dict[str, List[str]] = {}
        for t in tables:
            layer = graph.nodes[t].layer if t in graph.nodes else "other"
            by_layer.setdefault(layer, []).append(t)
        for layer in sorted(by_layer, key=lambda l: _LAYER_ORDER.get(l, len(LAYERS))):
            lines.append(f'  subgraph sg_{layer}["{LAYER_LABELS.get(layer, layer)}"]')
            lines.append("    direction LR")
            for t in by_layer[layer]:
                lines.append(f'    {node_id[t]}["{_escape_mermaid_label(t)}"]')
            lines.append("  end")
    else:
        for t in tables:
            lines.append(f'  {node_id[t]}["{_escape_mermaid_label(t)}"]')

    # 边
    edge_lines: List[str] = []
    in_highlight_edges = 0
    for (s, t) in sorted(graph.edges, key=lambda k: (_sort_key(k[0], graph), _sort_key(k[1], graph))):
        if s not in node_id or t not in node_id:
            continue
        edge = graph.edges[(s, t)]
        label = ""
        if show_edge_labels:
            bits = []
            if edge.column_mappings:
                bits.append(f"{edge.column_mappings} 字段")
            if edge.unresolved_mappings:
                bits.append(f"未解析 {edge.unresolved_mappings}")
            if bits:
                label = " · ".join(bits)
        if label:
            edge_lines.append(f'  {node_id[s]} -- "{label}" --> {node_id[t]}')
        else:
            edge_lines.append(f"  {node_id[s]} --> {node_id[t]}")
    for s, t in graph.edges:
        if s in node_id and t in node_id and center and (s in up or t in down):
            in_highlight_edges += 1

    lines.extend(edge_lines)

    # 高亮 classDef
    lines.append("  classDef hlc fill:#fde047,stroke:#a16207,stroke-width:2px,color:#1f2937")
    lines.append("  classDef hlu fill:#bfdbfe,stroke:#1d4ed8,color:#1e3a8a")
    lines.append("  classDef hld fill:#fed7aa,stroke:#c2410c,color:#7c2d12")
    if center:
        for t in tables:
            if t == center:
                lines.append(f"  class {node_id[t]} hlc")
            elif t in up:
                lines.append(f"  class {node_id[t]} hlu")
            elif t in down:
                lines.append(f"  class {node_id[t]} hld")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 可视化数据（HTML 用）
# --------------------------------------------------------------------------- #
def graph_to_viz_data(
    graph: LineageGraph,
    title: str = "数仓血缘图谱",
    center: Optional[str] = None,
) -> Dict[str, Any]:
    """把血缘图转成前端渲染用的紧凑结构（节点 / 邻接表 / 边明细）。"""
    tables = sorted(graph.nodes)
    index = {t: i for i, t in enumerate(tables)}
    stats = graph.stats()
    layers_present = sorted(
        {graph.nodes[t].layer for t in tables}, key=lambda l: _LAYER_ORDER.get(l, len(LAYERS))
    )

    nodes: List[Dict[str, Any]] = []
    for t in tables:
        node = graph.nodes[t]
        nodes.append(
            {
                "i": index[t],
                "name": t,
                "table": node.table,
                "schema": node.schema,
                "layer": node.layer,
                "in": len(graph.predecessors(t)),
                "out": len(graph.successors(t)),
                "prod": sorted({s.get("file") or "" for s in node.produced_by} - {""}),
                "cons": sorted({s.get("file") or "" for s in node.consumed_by} - {""}),
                "cols": sum(e.column_mappings for e in graph.edges.values() if e.target == t),
            }
        )

    edges: List[Dict[str, Any]] = []
    out_adj: List[List[int]] = [[] for _ in tables]
    in_adj: List[List[int]] = [[] for _ in tables]
    for (s, t) in sorted(graph.edges):
        if s not in index or t not in index:
            continue
        e = graph.edges[(s, t)]
        edges.append(
            {
                "s": index[s],
                "t": index[t],
                "c": e.column_mappings,
                "u": e.unresolved_mappings,
                "k": e.constant_mappings,
                "files": e.files,
                "pf": e.partition_filters,
            }
        )
        out_adj[index[s]].append(index[t])
        in_adj[index[t]].append(index[s])

    cycle_nodes: Set[int] = set()
    for c in graph.detect_cycles():
        for name in c["nodes"]:
            if name in index:
                cycle_nodes.add(index[name])

    return {
        "title": title,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dialect": graph.dialect,
        "root": graph.root,
        "stats": {
            "node_count": stats["node_count"],
            "edge_count": stats["edge_count"],
            "column_mapping_count": stats["column_mapping_count"],
            "max_depth": stats["max_depth"],
            "root_count": stats["root_count"],
            "leaf_count": stats["leaf_count"],
            "cycle_count": stats["cycle_count"],
            "file_count": stats["file_count"],
        },
        "layers": layers_present,
        "layer_labels": LAYER_LABELS,
        "layer_colors": LAYER_COLORS,
        "nodes": nodes,
        "edges": edges,
        "out_adj": out_adj,
        "in_adj": in_adj,
        "cycle_nodes": sorted(cycle_nodes),
        "center": index.get(graph.resolve_table(center) or "", None) if center else None,
    }


# --------------------------------------------------------------------------- #
# 自包含交互式 HTML
# --------------------------------------------------------------------------- #
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{
    --bg:#0f172a; --panel:#111c33; --panel2:#16233d; --line:#24365a;
    --fg:#e6edf7; --dim:#93a4c0; --accent:#38bdf8;
    --up:#60a5fa; --down:#fb923c; --sel:#fde047;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
    font-family:"Microsoft YaHei","PingFang SC","Helvetica Neue",Arial,sans-serif;font-size:13px}
  #app{display:flex;height:100vh;overflow:hidden}
  #left{width:250px;flex:0 0 250px;background:var(--panel);border-right:1px solid var(--line);
    padding:14px;overflow:auto}
  #right{width:320px;flex:0 0 320px;background:var(--panel);border-left:1px solid var(--line);
    padding:14px;overflow:auto}
  #center{flex:1;position:relative;min-width:0}
  canvas{display:block;width:100%;height:100%;cursor:grab}
  canvas.dragging{cursor:grabbing}
  h1{font-size:15px;margin:0 0 4px}
  h2{font-size:12px;margin:16px 0 6px;color:var(--accent);letter-spacing:.5px;
    text-transform:uppercase;border-bottom:1px solid var(--line);padding-bottom:4px}
  .sub{color:var(--dim);font-size:11px;line-height:1.6;word-break:break-all}
  .stat{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px dashed #1e2c49}
  .stat b{color:var(--accent);font-weight:600}
  input[type=search]{width:100%;padding:7px 9px;border-radius:6px;border:1px solid var(--line);
    background:#0b1428;color:var(--fg);outline:none}
  input[type=search]:focus{border-color:var(--accent)}
  .legend{display:flex;align-items:center;gap:7px;padding:3px 0;cursor:pointer;user-select:none}
  .legend input{accent-color:var(--accent)}
  .dot{width:10px;height:10px;border-radius:50%;flex:0 0 10px}
  button{background:var(--panel2);color:var(--fg);border:1px solid var(--line);border-radius:6px;
    padding:6px 9px;cursor:pointer;font-size:12px;margin:4px 4px 0 0}
  button:hover{border-color:var(--accent);color:var(--accent)}
  #hint{position:absolute;left:12px;bottom:10px;color:var(--dim);font-size:11px;
    background:rgba(15,23,42,.75);padding:5px 9px;border-radius:6px;border:1px solid var(--line)}
  #tip{position:absolute;pointer-events:none;background:#0b1428;border:1px solid var(--accent);
    border-radius:5px;padding:4px 8px;font-size:12px;display:none;white-space:nowrap;z-index:5}
  .row{padding:5px 7px;border-radius:5px;cursor:pointer;border:1px solid transparent;
    margin-bottom:3px;word-break:break-all}
  .row:hover{background:var(--panel2);border-color:var(--line)}
  .tag{display:inline-block;font-size:10px;padding:1px 5px;border-radius:4px;margin-right:4px;
    background:#1e2c49;color:var(--dim)}
  .up{color:var(--up)} .down{color:var(--down)} .cyc{color:#f87171}
  .kv{color:var(--dim);font-size:11px}
  code{background:#0b1428;padding:1px 4px;border-radius:3px;font-size:11px}
  .pathline{font-size:11px;color:var(--dim);padding:2px 0;word-break:break-all;line-height:1.5}
  .empty{color:var(--dim);font-size:12px;padding:8px 0}
</style>
</head>
<body>
<div id="app">
  <aside id="left">
    <h1 id="title"></h1>
    <div class="sub" id="meta"></div>
    <h2>统计</h2>
    <div id="stats"></div>
    <h2>搜索表名</h2>
    <input type="search" id="search" placeholder="输入表名，回车定位（支持不带库名）">
    <div id="results"></div>
    <h2>分层筛选</h2>
    <div id="layers"></div>
    <h2>操作</h2>
    <button id="btn-fit">适配画布</button>
    <button id="btn-reset">重置视图</button>
    <button id="btn-png">导出 PNG</button>
    <button id="btn-clear">清除选中</button>
    <div class="sub" style="margin-top:10px">
      蓝=上游 · 橙=下游 · 黄=选中<br>拖节点可移动，拖空白平移，滚轮缩放，双击适配。
    </div>
  </aside>
  <main id="center">
    <canvas id="cv"></canvas>
    <div id="tip"></div>
    <div id="hint">点击节点查看它的上游溯源与下游影响</div>
  </main>
  <aside id="right">
    <h2>表详情</h2>
    <div id="detail"><div class="empty">尚未选中表。点击左侧画布中的任一节点。</div></div>
  </aside>
</div>
<script>
"use strict";
const DATA = __DATA__;
const N = DATA.nodes.length;
const cv = document.getElementById("cv");
const ctx = cv.getContext("2d");
const tip = document.getElementById("tip");

/* ---------- 布局：按分层分列 + 力导向迭代 ---------- */
const layerIndex = {};
DATA.layers.forEach((l, i) => layerIndex[l] = i);
const LH = 130;                       /* 每层列宽 */
const positions = DATA.nodes.map((n, i) => {
  const col = layerIndex[n.layer] !== undefined ? layerIndex[n.layer] : 0;
  const ys = DATA.nodes.filter(m => m.layer === n.layer).map(m => m.i);
  const rank = ys.indexOf(i);
  const total = Math.max(ys.length, 1);
  return { x: col * LH + 90, y: (rank + 0.5) * 90 - (total * 90) / 2, vx: 0, vy: 0, fixed: false };
});
const degree = DATA.nodes.map((n, i) => DATA.out_adj[i].length + DATA.in_adj[i].length);

function simulate(iterations) {
  const k = 120;                       /* 理想边距 */
  for (let it = 0; it < iterations; it++) {
    /* 斥力（O(n^2)，几千节点内够用；示例图仅数十节点） */
    for (let i = 0; i < N; i++) {
      for (let j = i + 1; j < N; j++) {
        let dx = positions[i].x - positions[j].x, dy = positions[i].y - positions[j].y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) { dx = Math.random() - .5; dy = Math.random() - .5; d2 = 1; }
        const d = Math.sqrt(d2);
        const f = (k * k) / d2 * (1 + degree[i] * 0.05 + degree[j] * 0.05);
        const fx = (dx / d) * f, fy = (dy / d) * f;
        positions[i].vx += fx; positions[i].vy += fy;
        positions[j].vx -= fx; positions[j].vy -= fy;
      }
    }
    /* 边上的弹簧力 */
    DATA.edges.forEach(e => {
      const s = positions[e.s], t = positions[e.t];
      let dx = t.x - s.x, dy = t.y - s.y;
      const d = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const f = (d * d) / k * 0.06;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      s.vx += fx; s.vy += fy; t.vx -= fx; t.vy -= fy;
    });
    /* 分层列约束：把节点拉回自己所属层的列 */
    DATA.nodes.forEach((n, i) => {
      const col = layerIndex[n.layer] !== undefined ? layerIndex[n.layer] : 0;
      positions[i].vx += (col * LH + 90 - positions[i].x) * 0.25;
    });
    /* 应用速度 + 阻尼 */
    for (let i = 0; i < N; i++) {
      if (positions[i].fixed) { positions[i].vx = 0; positions[i].vy = 0; continue; }
      positions[i].x += Math.max(-40, Math.min(40, positions[i].vx * 0.02));
      positions[i].y += Math.max(-40, Math.min(40, positions[i].vy * 0.02));
      positions[i].vx *= 0.55; positions[i].vy *= 0.55;
    }
  }
}
simulate(N <= 200 ? 320 : 120);

/* ---------- 视图变换 ---------- */
const view = { x: 0, y: 0, k: 1 };
function fitView() {
  let minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9;
  positions.forEach(p => { minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
                           minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y); });
  const pad = 90;
  const w = cv.clientWidth, h = cv.clientHeight;
  view.k = Math.min((w - pad * 2) / Math.max(maxX - minX, 1), (h - pad * 2) / Math.max(maxY - minY, 1), 2);
  view.k = Math.max(view.k, 0.15);
  view.x = w / 2 - ((minX + maxX) / 2) * view.k;
  view.y = h / 2 - ((minY + maxY) / 2) * view.k;
  draw();
}

/* ---------- 高亮：BFS 上下游 ---------- */
let sel = DATA.center === null || DATA.center === undefined ? -1 : DATA.center;
let upSet = new Set(), downSet = new Set(), upDepth = {}, downDepth = {};
const hidden = new Set();

function bfs(start, adj, set, depths) {
  set.add(start); depths[start] = 0;
  let frontier = [start], d = 0;
  while (frontier.length && d < 40) {
    d++;
    const next = [];
    frontier.forEach(i => adj[i].forEach(j => {
      if (!set.has(j)) { set.add(j); depths[j] = d; next.push(j); }
    }));
    frontier = next;
  }
}
function recompute() {
  upSet = new Set(); downSet = new Set(); upDepth = {}; downDepth = {};
  if (sel >= 0) { bfs(sel, DATA.in_adj, upSet, upDepth); bfs(sel, DATA.out_adj, downSet, downDepth); }
}
function pathsFrom(start, adj, limit) {
  const out = [];
  (function dfs(node, acc) {
    if (out.length >= limit) return;
    const nxt = adj[node].filter(x => acc.indexOf(x) < 0);
    if (!nxt.length || acc.length > 8) { out.push(acc.slice()); return; }
    nxt.forEach(x => { acc.push(x); dfs(x, acc); acc.pop(); });
  })(start, [start]);
  return out;
}

/* ---------- 绘制 ---------- */
function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function radius(n) { return 8 + Math.min(14, (DATA.nodes[n].in + DATA.nodes[n].out) * 1.6); }

function draw() {
  const dpr = window.devicePixelRatio || 1;
  if (cv.width !== cv.clientWidth * dpr || cv.height !== cv.clientHeight * dpr) {
    cv.width = cv.clientWidth * dpr; cv.height = cv.clientHeight * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#0f172a"; ctx.fillRect(0, 0, cv.clientWidth, cv.clientHeight);
  ctx.save(); ctx.translate(view.x, view.y); ctx.scale(view.k, view.k);

  /* 边 */
  DATA.edges.forEach(e => {
    if (hidden.has(DATA.nodes[e.s].layer) || hidden.has(DATA.nodes[e.t].layer)) return;
    const s = positions[e.s], t = positions[e.t];
    const inUp = sel >= 0 && (upSet.has(e.s) || upSet.has(e.t)) && (upSet.has(e.t));
    const inDown = sel >= 0 && downSet.has(e.s) && downSet.has(e.t);
    const selEdge = sel >= 0 && (e.s === sel || e.t === sel);
    let color = "#31456b", width = 1.2, alpha = 1;
    if (sel >= 0) {
      if (inDown) { color = "#fb923c"; width = 2.2; }
      else if (inUp) { color = "#60a5fa"; width = 2.2; }
      else if (selEdge) { color = "#fde047"; width = 2.4; }
      else { alpha = 0.18; }
    }
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = color; ctx.lineWidth = width;
    ctx.beginPath();
    const mx = (s.x + t.x) / 2, my = (s.y + t.y) / 2 - Math.abs(t.x - s.x) * 0.06;
    ctx.moveTo(s.x, s.y); ctx.quadraticCurveTo(mx, my, t.x, t.y); ctx.stroke();
    /* 箭头 */
    const ang = Math.atan2(t.y - my, t.x - mx);
    const rr = radius(e.t);
    const ex = t.x - Math.cos(ang) * rr, ey = t.y - Math.sin(ang) * rr;
    ctx.beginPath();
    ctx.moveTo(ex, ey);
    ctx.lineTo(ex - Math.cos(ang - 0.4) * 9, ey - Math.sin(ang - 0.4) * 9);
    ctx.lineTo(ex - Math.cos(ang + 0.4) * 9, ey - Math.sin(ang + 0.4) * 9);
    ctx.closePath(); ctx.fillStyle = color; ctx.fill();
    /* 边标签：字段映射条数（缩放足够大时才显示） */
    if (view.k > 0.75 && e.c) {
      ctx.globalAlpha = alpha * 0.9;
      ctx.fillStyle = "#8fa6c9"; ctx.font = "10px sans-serif"; ctx.textAlign = "center";
      ctx.fillText(e.c + " 字段", mx, my - 2);
    }
    ctx.globalAlpha = 1;
  });

  /* 节点 */
  DATA.nodes.forEach((n, i) => {
    if (hidden.has(n.layer)) return;
    const p = positions[i], r = radius(i);
    let fill = DATA.layer_colors[n.layer] || "#94a3b8";
    let stroke = "#0b1428", alpha = 1, lw = 1.5;
    if (sel >= 0) {
      if (i === sel) { fill = "#fde047"; stroke = "#a16207"; lw = 3; }
      else if (downSet.has(i)) { fill = "#fb923c"; stroke = "#9a3412"; lw = 2.5; }
      else if (upSet.has(i)) { fill = "#60a5fa"; stroke = "#1d4ed8"; lw = 2.5; }
      else { alpha = 0.22; }
    }
    if (DATA.cycle_nodes.indexOf(i) >= 0) { stroke = "#f87171"; lw = 3; }
    ctx.globalAlpha = alpha;
    ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fillStyle = fill; ctx.fill();
    ctx.lineWidth = lw; ctx.strokeStyle = stroke; ctx.stroke();
    /* 表名：按缩放决定字号，避免糊成一团 */
    const fs = Math.max(9, Math.min(13, 11 / Math.max(view.k, 0.5)));
    ctx.font = (i === sel ? "bold " : "") + fs + "px sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillStyle = (sel >= 0 && i !== sel && !upSet.has(i) && !downSet.has(i)) ? "#64748b" : "#eaf1fb";
    ctx.fillText(n.table, p.x, p.y + r + 11);
    ctx.globalAlpha = 1;
  });
  ctx.restore();
}

/* ---------- 命中测试 / 交互 ---------- */
function toWorld(mx, my) { return { x: (mx - view.x) / view.k, y: (my - view.y) / view.k }; }
function hit(mx, my) {
  const p = toWorld(mx, my);
  for (let i = N - 1; i >= 0; i--) {
    if (hidden.has(DATA.nodes[i].layer)) continue;
    const q = positions[i];
    if ((p.x - q.x) ** 2 + (p.y - q.y) ** 2 <= (radius(i) + 3) ** 2) return i;
  }
  return -1;
}
let drag = null;
cv.addEventListener("mousedown", ev => {
  const i = hit(ev.offsetX, ev.offsetY);
  if (i >= 0) { drag = { type: "node", i, moved: 0 }; positions[i].fixed = true; }
  else { drag = { type: "pan", x: ev.clientX, y: ev.clientY, moved: 0 }; cv.classList.add("dragging"); }
});
window.addEventListener("mousemove", ev => {
  const rect = cv.getBoundingClientRect();
  if (drag && drag.type === "node") {
    const p = toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
    positions[drag.i].x = p.x; positions[drag.i].y = p.y; drag.moved++;
    draw();
  } else if (drag && drag.type === "pan") {
    view.x += ev.clientX - drag.x; view.y += ev.clientY - drag.y;
    drag.x = ev.clientX; drag.y = ev.clientY; drag.moved++; draw();
  } else {
    const i = hit(ev.clientX - rect.left, ev.clientY - rect.top);
    if (i >= 0) {
      tip.style.display = "block";
      tip.style.left = (ev.clientX - rect.left + 14) + "px";
      tip.style.top = (ev.clientY - rect.top + 12) + "px";
      tip.innerHTML = esc(DATA.nodes[i].name) + ' <span class="tag">' + esc(DATA.nodes[i].layer) +
        '</span><br><span class="kv">上游 ' + DATA.nodes[i].in + ' · 下游 ' + DATA.nodes[i].out + '</span>';
      cv.style.cursor = "pointer";
    } else { tip.style.display = "none"; cv.style.cursor = drag ? "grabbing" : "grab"; }
  }
});
window.addEventListener("mouseup", ev => {
  if (drag && drag.type === "node" && drag.moved <= 2) { select(drag.i); }
  if (drag && drag.type === "node") positions[drag.i].fixed = false;
  if (drag && drag.type === "pan" && drag.moved <= 2) { select(-1); }
  drag = null; cv.classList.remove("dragging");
});
cv.addEventListener("wheel", ev => {
  ev.preventDefault();
  const rect = cv.getBoundingClientRect();
  const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
  const before = toWorld(mx, my);
  view.k = Math.max(0.15, Math.min(4, view.k * (ev.deltaY < 0 ? 1.12 : 0.89)));
  const after = toWorld(mx, my);
  view.x += (after.x - before.x) * view.k;
  view.y += (after.y - before.y) * view.k;
  draw();
}, { passive: false });
cv.addEventListener("dblclick", fitView);
window.addEventListener("resize", draw);

/* ---------- 选中 → 右侧详情 ---------- */
function select(i) {
  sel = i; recompute(); draw(); renderDetail();
}
function renderDetail() {
  const box = document.getElementById("detail");
  if (sel < 0) { box.innerHTML = '<div class="empty">尚未选中表。点击左侧画布中的任一节点。</div>'; return; }
  const n = DATA.nodes[sel];
  const upList = Array.from(upSet).filter(x => x !== sel).sort((a, b) => upDepth[a] - upDepth[b]);
  const downList = Array.from(downSet).filter(x => x !== sel).sort((a, b) => downDepth[a] - downDepth[b]);
  const outs = DATA.edges.filter(e => e.s === sel), ins = DATA.edges.filter(e => e.t === sel);
  const paths = pathsFrom(sel, DATA.in_adj, 12).map(p => p.map(x => DATA.nodes[x].name).reverse());
  const dpaths = pathsFrom(sel, DATA.out_adj, 12).map(p => p.map(x => DATA.nodes[x].name));
  let html = '<h1>' + esc(n.name) + '</h1>';
  html += '<div class="sub">分层：<span class="tag" style="background:' + (DATA.layer_colors[n.layer] || "#94a3b8") +
    ';color:#0b1428">' + esc(DATA.layer_labels[n.layer] || n.layer) + '</span>' +
    (DATA.cycle_nodes.indexOf(sel) >= 0 ? '<span class="cyc">⚠ 处于循环依赖中</span>' : '') + '</div>';
  html += '<div class="sub">直接上下游：上游 ' + n.in + ' 张 / 下游 ' + n.out + ' 张 · 字段映射 ' + n.cols + ' 条</div>';
  if (n.prod.length) html += '<div class="sub">产出文件：<br>' + n.prod.map(f => '<code>' + esc(f) + '</code>').join('<br>') + '</div>';
  if (n.cons.length) html += '<div class="sub">被引用文件：<br>' + n.cons.map(f => '<code>' + esc(f) + '</code>').join('<br>') + '</div>';

  html += '<h2 class="up">上游溯源（' + upList.length + ' 张）</h2>';
  if (!upList.length) html += '<div class="empty">无上游：这是一张源表。</div>';
  upList.slice(0, 60).forEach(x => {
    html += '<div class="row" data-i="' + x + '"><span class="tag up">L' + upDepth[x] + '</span>' +
      esc(DATA.nodes[x].name) + '</div>';
  });
  html += '<h2 class="down">下游影响（' + downList.length + ' 张）</h2>';
  if (!downList.length) html += '<div class="empty">无下游：这是一张叶子表（报表/接口输出）。</div>';
  downList.slice(0, 60).forEach(x => {
    html += '<div class="row" data-i="' + x + '"><span class="tag down">L' + downDepth[x] + '</span>' +
      esc(DATA.nodes[x].name) + '</div>';
  });

  html += '<h2>直接上游边（' + ins.length + '）</h2>';
  ins.forEach(e => {
    const d = DATA.nodes[e.s];
    html += '<div class="row" data-i="' + e.s + '">' + esc(d.name) + ' <span class="tag">' + e.c + ' 字段</span>' +
      (e.u ? '<span class="tag">未解析 ' + e.u + '</span>' : '') +
      '<div class="kv">' + (e.pf && Object.keys(e.pf).length ? '分区 ' + esc(JSON.stringify(e.pf)) + ' · ' : '') +
      esc((e.files || []).join(", ")) + '</div></div>';
  });
  html += '<h2>直接下游边（' + outs.length + '）</h2>';
  outs.forEach(e => {
    const d = DATA.nodes[e.t];
    html += '<div class="row" data-i="' + e.t + '">' + esc(d.name) + ' <span class="tag">' + e.c + ' 字段</span>' +
      (e.u ? '<span class="tag">未解析 ' + e.u + '</span>' : '') +
      '<div class="kv">' + esc((e.files || []).join(", ")) + '</div></div>';
  });

  html += '<h2>血缘链路</h2>';
  html += '<div class="kv">上游链路（' + paths.length + ' 条，最多展示 12 条）：</div>';
  if (!paths.length) html += '<div class="empty">无</div>';
  paths.forEach(p => {
    html += '<div class="pathline">' + p.map(x => esc(x)).join(' → ') + ' → <b>' + esc(n.name) + '</b></div>';
  });
  html += '<div class="kv" style="margin-top:6px">下游链路（' + dpaths.length + ' 条）：</div>';
  if (!dpaths.length) html += '<div class="empty">无</div>';
  dpaths.forEach(p => { html += '<div class="pathline"><b>' + esc(n.name) + '</b> → ' + p.slice(1).map(x => esc(x)).join(' → ') + '</div>'; });

  box.innerHTML = html;
  box.querySelectorAll(".row").forEach(el => el.addEventListener("click", () => {
    const j = parseInt(el.dataset.i, 10); select(j); centerOn(j);
  }));
}
function centerOn(i) {
  const p = positions[i];
  view.x = cv.clientWidth / 2 - p.x * view.k;
  view.y = cv.clientHeight / 2 - p.y * view.k;
  draw();
}

/* ---------- 左侧面板 ---------- */
document.getElementById("title").textContent = DATA.title;
document.getElementById("meta").innerHTML =
  '生成时间 ' + DATA.generated_at + ' · 方言 ' + DATA.dialect +
  (DATA.root ? '<br>扫描根目录 <code>' + esc(DATA.root) + '</code>' : '');
const statDefs = [
  ["表（节点）数", DATA.stats.node_count], ["血缘边数", DATA.stats.edge_count],
  ["字段级映射", DATA.stats.column_mapping_count], ["最大血缘深度", DATA.stats.max_depth],
  ["源表 / 叶子表", DATA.stats.root_count + " / " + DATA.stats.leaf_count],
  ["循环依赖", DATA.stats.cycle_count], ["涉及文件", DATA.stats.file_count]
];
document.getElementById("stats").innerHTML = statDefs.map(d =>
  '<div class="stat"><span>' + d[0] + '</span><b>' + d[1] + '</b></div>').join("");

document.getElementById("layers").innerHTML = DATA.layers.map(l =>
  '<label class="legend"><input type="checkbox" checked data-layer="' + l + '">' +
  '<span class="dot" style="background:' + (DATA.layer_colors[l] || "#94a3b8") + '"></span>' +
  esc(DATA.layer_labels[l] || l) + ' (' + DATA.nodes.filter(n => n.layer === l).length + ')</label>').join("");
document.querySelectorAll("#layers input").forEach(cb => cb.addEventListener("change", () => {
  const l = cb.dataset.layer;
  if (cb.checked) hidden.delete(l); else hidden.add(l);
  draw();
}));

const searchBox = document.getElementById("search");
function runSearch() {
  const q = searchBox.value.trim().toLowerCase();
  const box = document.getElementById("results");
  if (!q) { box.innerHTML = ""; return; }
  const hits = DATA.nodes.filter(n => n.name.toLowerCase().indexOf(q) >= 0).slice(0, 12);
  box.innerHTML = hits.length
    ? hits.map(n => '<div class="row" data-i="' + n.i + '">' + esc(n.name) +
        '<div class="kv">上游 ' + n.in + ' · 下游 ' + n.out + '</div></div>').join("")
    : '<div class="empty">没有匹配的表</div>';
  box.querySelectorAll(".row").forEach(el => el.addEventListener("click", () => {
    const i = parseInt(el.dataset.i, 10); select(i); centerOn(i);
  }));
}
searchBox.addEventListener("input", runSearch);
searchBox.addEventListener("keydown", ev => {
  if (ev.key === "Enter") {
    const q = searchBox.value.trim().toLowerCase();
    const hit = DATA.nodes.find(n => n.name.toLowerCase().indexOf(q) >= 0);
    if (hit) { select(hit.i); centerOn(hit.i); }
  }
});
document.getElementById("btn-fit").addEventListener("click", fitView);
document.getElementById("btn-reset").addEventListener("click", () => {
  view.x = 0; view.y = 0; view.k = 1; draw();
});
document.getElementById("btn-clear").addEventListener("click", () => select(-1));
document.getElementById("btn-png").addEventListener("click", () => {
  const prev = sel; sel = -1; recompute(); draw();
  const a = document.createElement("a");
  a.download = "lineage.png";
  a.href = cv.toDataURL("image/png");
  a.click();
  sel = prev; recompute(); draw();
});

/* ---------- 启动 ---------- */
recompute();
fitView();
renderDetail();
if (DATA.stats.cycle_count) {
  document.getElementById("hint").textContent =
    "⚠ 检测到 " + DATA.stats.cycle_count + " 处循环依赖（红圈节点），建议排查";
}
</script>
</body>
</html>
"""


def to_html(
    graph: LineageGraph,
    title: str = "数仓血缘图谱",
    center: Optional[str] = None,
) -> str:
    """生成自包含的单文件交互式血缘图 HTML（无外链、无 CDN，离线双击即开）。"""
    data = graph_to_viz_data(graph, title=title, center=center)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = _HTML_TEMPLATE.replace("__DATA__", payload)
    html = html.replace("__TITLE__", title)
    return html


def write_html(graph: LineageGraph, path: Any, title: str = "数仓血缘图谱",
               center: Optional[str] = None) -> Path:
    p = Path(path)
    p.write_text(to_html(graph, title=title, center=center), encoding="utf-8")
    return p


def write_mermaid(graph: LineageGraph, path: Any, **kwargs: Any) -> Path:
    p = Path(path)
    p.write_text(to_mermaid(graph, **kwargs), encoding="utf-8")
    return p
