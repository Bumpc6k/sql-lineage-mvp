#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""格式化 HTML 血缘报告渲染（单文件、零外部依赖、内联 CSS/JS/SVG）。

为什么要有它：DolphinScheduler 的任务日志是纯文本，字段级血缘一多就「读不完、看不清」。
本模块把同一个 ``/analyze`` 结果渲染成**一个可离线打开的 HTML 文件**，
任务日志末尾只留一行 URL 指过来即可。

设计约束：

* **单文件**：CSS / JS / 图表（内联 SVG）全部写进同一个 ``.html``，
  不引任何外网 CDN、不引字体文件 —— 断网、内网、容器里都能打开；
* **零新增依赖**：只用标准库（``html`` / ``hashlib`` / ``datetime`` / ``pathlib``）；
* **中文正常显示**：只依赖系统字体栈（Windows 微软雅黑 / macOS 苹方 / Linux Noto）。

对外入口：

* :func:`render_report` —— 纯函数，``dict`` 进 ``str`` 出（方便单测）；
* :func:`save_report`   —— 落盘到 ``reports/<report_id>.html``，返回 id / URL / 大小；
* :func:`prune_reports` —— 只保留最近 N 份，避免磁盘无限增长。

URL 里出现的两个 host 是有意区分开的：

* ``url``（默认 ``http://localhost:18080/report/<id>``）：**宿主机 / Windows 浏览器**直接打开；
* ``internal_url``（默认 ``http://172.17.0.1:18080/report/<id>``）：**容器内**（如 ds-standalone）访问宿主机。

可用环境变量覆盖：``LINEAGE_PUBLIC_BASE`` / ``LINEAGE_INTERNAL_BASE`` / ``LINEAGE_REPORTS_DIR``。
"""

from __future__ import annotations

import hashlib
import html
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "DEFAULT_REPORTS_DIR",
    "KEEP_REPORTS",
    "make_report_id",
    "render_report",
    "save_report",
    "prune_reports",
    "reports_dir",
    "report_bases",
    "report_path",
    "rank_metrics",
    "list_reports",
    "safe_report_id",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_PUBLIC_BASE = "http://localhost:18080"
DEFAULT_INTERNAL_BASE = "http://172.17.0.1:18080"

#: 只保留最近多少份报告（旧的自动删）
KEEP_REPORTS = 200

#: 字段映射表格最多渲染多少行（日志里是 15 行内，报告里给足）
MAX_COLUMN_ROWS = 500
#: 上游链路图最多画多少个节点
MAX_GRAPH_NODES = 24


# --------------------------------------------------------------------------- #
# 路径 / URL / 文件名
# --------------------------------------------------------------------------- #
def reports_dir(payload: Optional[Dict[str, Any]] = None) -> Path:
    """报告落盘目录：请求体 ``reports_dir`` > 环境变量 ``LINEAGE_REPORTS_DIR`` > ``reports/``。"""
    raw = ""
    if payload:
        raw = str(payload.get("reports_dir") or "").strip()
    raw = raw or os.environ.get("LINEAGE_REPORTS_DIR", "").strip()
    path = Path(raw) if raw else DEFAULT_REPORTS_DIR
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def report_bases() -> Tuple[str, str]:
    """返回 ``(公网基址, 容器内基址)``：前者给 Windows 浏览器，后者给容器里的任务。"""
    public = (os.environ.get("LINEAGE_PUBLIC_BASE") or DEFAULT_PUBLIC_BASE).rstrip("/")
    internal = (os.environ.get("LINEAGE_INTERNAL_BASE") or DEFAULT_INTERNAL_BASE).rstrip("/")
    return public, internal


def report_url(report_id: str) -> str:
    return f"{report_bases()[0]}/report/{report_id}"


def internal_report_url(report_id: str) -> str:
    return f"{report_bases()[1]}/report/{report_id}"


def report_path(report_id: str, base: Optional[Path] = None) -> Path:
    return (base or reports_dir()) / f"{report_id}.html"


def make_report_id(sql: str = "", now: Optional[datetime] = None) -> str:
    """``rpt_20260920_213045_<sql 短 hash>``：时间戳让人排序，hash 让同秒请求不撞车。"""
    moment = now or datetime.now()
    digest = hashlib.sha1((sql or "").encode("utf-8")).hexdigest()[:8]
    return f"rpt_{moment:%Y%m%d_%H%M%S}_{digest}"


def safe_report_id(report_id: str) -> str:
    """清洗 report_id：只留字母数字与 ``-_.``（防路径穿越 / 非法文件名）。"""
    return "".join(c for c in (report_id or "") if c.isalnum() or c in "-_.")


#: 兼容旧内部调用名
_safe_report_id = safe_report_id


def prune_reports(base: Optional[Path] = None, keep: int = KEEP_REPORTS) -> int:
    """只保留最近 ``keep`` 份报告（按文件名倒序，文件名带时间戳所以=时间倒序）。"""
    directory = base or reports_dir()
    try:
        files = sorted(directory.glob("rpt_*.html"))
    except OSError:
        return 0
    removed = 0
    for path in files[:-keep] if keep > 0 else files:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def list_reports(base: Optional[Path] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """最近的报告清单（给 ``GET /reports`` 用）。"""
    directory = base or reports_dir()
    out: List[Dict[str, Any]] = []
    try:
        files = sorted(directory.glob("rpt_*.html"), reverse=True)[: max(0, limit)]
    except OSError:
        return out
    for path in files:
        stat = path.stat()
        out.append({
            "report_id": path.stem,
            "size_bytes": stat.st_size,
            "generated_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "url": report_url(path.stem),
        })
    return out


# --------------------------------------------------------------------------- #
# 口径排序（插件日志与 HTML 报告共用同一套「什么最重要」的判据）
# --------------------------------------------------------------------------- #
#: 度量类型优先级：越像「核心产出指标」越靠前
_TYPE_RANK = {"聚合": 0, "比率": 1, "算术计算": 2, "条件分支": 3, "函数转换": 4, "窗口函数": 5}


def _confidence(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def rank_metrics(metrics: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按「关键程度」排序：类型（聚合/比率优先）→ 置信度 → 依赖字段数 → 目标字段级命中。"""
    def key(metric: Dict[str, Any]) -> Tuple[int, int, float, int, int]:
        first = metric.get("matched_by") == "目标字段"
        return (
            _TYPE_RANK.get(str(metric.get("metric_type") or ""), 9),
            0 if first else 1,
            -_confidence(metric.get("confidence")),
            -len(metric.get("depends_on") or []),
            0,
        )

    indexed = list(enumerate(metrics or []))
    indexed.sort(key=lambda pair: (key(pair[1]), pair[0]))
    return [metric for _, metric in indexed]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _text(value: Any, default: str = "-") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _display_width(value: str) -> int:
    """粗略显示宽度：CJK 全角算 2 列（仅用于 SVG 文本裁剪判断）。"""
    return sum(2 if ord(ch) > 0x2000 else 1 for ch in value)


def _clip(value: str, max_width: int) -> str:
    if _display_width(value) <= max_width:
        return value
    out, width = "", 0
    for ch in value:
        step = 2 if ord(ch) > 0x2000 else 1
        if width + step > max_width - 1:
            break
        out += ch
        width += step
    return out + "…"


def _join(items: Iterable[Any], sep: str = ", ") -> str:
    return sep.join(str(i) for i in items if i not in (None, ""))


def _unique(items: Iterable[Any]) -> List[Any]:
    out: List[Any] = []
    for item in items:
        if item not in (None, "") and item not in out:
            out.append(item)
    return out


# --------------------------------------------------------------------------- #
# CSS（内联，无外部字体/图标依赖）
# --------------------------------------------------------------------------- #
_CSS = """
:root{
  --bg:#0d1117; --panel:#161b22; --panel-2:#1b2330; --line:#2a3441;
  --fg:#e6edf3; --dim:#8b949e; --accent:#58a6ff; --accent-2:#1f6feb;
  --green:#3fb950; --amber:#d29922; --purple:#bc8cff; --red:#f85149;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--fg);
  font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",
       "Hiragino Sans GB","Noto Sans CJK SC",sans-serif;
  -webkit-font-smoothing:antialiased;
}
code,pre,.mono{font-family:"JetBrains Mono","Cascadia Mono",Consolas,"Courier New",monospace}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1200px;margin:0 auto;padding:0 20px 56px}
.hero{
  background:linear-gradient(135deg,#12263f 0%,#161b22 45%,#1b2330 100%);
  border-bottom:1px solid var(--line); padding:28px 0 22px; margin-bottom:26px;
}
.hero .wrap{padding-bottom:0}
.hero h1{margin:0 0 6px;font-size:26px;letter-spacing:.5px}
.hero h1 small{font-size:13px;color:var(--dim);font-weight:400;margin-left:10px;letter-spacing:2px}
.hero p.sub{margin:0;color:var(--dim);font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.chip{
  background:rgba(88,166,255,.10);border:1px solid rgba(88,166,255,.35);
  border-radius:999px;padding:4px 12px;font-size:12.5px;color:#cfe3ff;
}
.chip b{color:#fff;font-weight:600}
.chip.green{background:rgba(63,185,80,.10);border-color:rgba(63,185,80,.35);color:#b7f0c0}
.chip.amber{background:rgba(210,153,34,.12);border-color:rgba(210,153,34,.38);color:#f2d08a}
.chip.purple{background:rgba(188,140,255,.12);border-color:rgba(188,140,255,.35);color:#e2ccff}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-bottom:22px}
section h2{margin:0 0 16px;font-size:17px;display:flex;align-items:center;gap:10px}
section h2 .n{
  display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;
  border-radius:8px;background:var(--accent-2);color:#fff;font-size:13px;font-weight:700;
}
section h2 .count{margin-left:auto;font-size:12.5px;color:var(--dim);font-weight:400}
.hint{color:var(--dim);font-size:12.5px;margin:10px 0 0}
.empty{color:var(--dim);font-size:13px;padding:6px 0}
/* 表级血缘流向 */
.flow{display:flex;flex-direction:column;gap:12px}
.flow-row{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.node{
  border:1px solid var(--line);background:var(--panel-2);border-radius:9px;
  padding:9px 14px;font-size:13px;font-family:"JetBrains Mono",Consolas,monospace;
  max-width:100%;word-break:break-all;
}
.node.src{border-left:4px solid var(--amber)}
.node.tgt{border-left:4px solid var(--green)}
.arrow{color:var(--accent);font-size:15px;letter-spacing:-1px}
.legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--dim);font-size:12.5px;margin-top:14px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle}
/* 字段级血缘真表格 */
.toolbar{display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap}
.toolbar input{
  flex:1;min-width:220px;background:#0d1117;border:1px solid var(--line);border-radius:8px;
  color:var(--fg);padding:8px 12px;font-size:13px;outline:none;
}
.toolbar input:focus{border-color:var(--accent)}
.table-scroll{overflow:auto;max-height:620px;border:1px solid var(--line);border-radius:10px}
table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px}
thead th{
  position:sticky;top:0;z-index:1;background:#1b2330;color:#cfe3ff;text-align:left;
  padding:10px 12px;font-weight:600;white-space:nowrap;border-bottom:1px solid var(--line);
}
tbody td{padding:9px 12px;border-bottom:1px solid rgba(42,52,65,.65);vertical-align:top}
tbody tr:nth-child(odd){background:rgba(255,255,255,.018)}
tbody tr:nth-child(even){background:rgba(88,166,255,.055)}
tbody tr:hover{background:rgba(88,166,255,.13)}
td.idx{color:var(--dim);width:44px;text-align:right}
td.tgt{color:#b7f0c0;font-weight:600}
td.col,td.expr{font-family:"JetBrains Mono",Consolas,monospace;font-size:12.5px;word-break:break-all}
td.expr{color:#cfe3ff}
td.tbl{color:var(--dim);font-size:12.5px}
.badge{display:inline-block;border-radius:6px;padding:1px 7px;font-size:11.5px;border:1px solid var(--line);color:var(--dim)}
.badge.ok{color:#b7f0c0;border-color:rgba(63,185,80,.4)}
.badge.warn{color:#f2d08a;border-color:rgba(210,153,34,.4)}
/* 业务口径卡片 */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(430px,1fr));gap:16px}
.card{background:var(--panel-2);border:1px solid var(--line);border-radius:11px;padding:16px 18px}
.card h3{margin:0 0 10px;font-size:15px;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.card h3 .cn{color:#fff}
.card h3 .col{color:var(--dim);font-size:12.5px;font-family:"JetBrains Mono",Consolas,monospace}
.card .tags{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.formula{
  background:#0d1117;border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:8px;padding:10px 12px;font-size:12.5px;color:#cfe3ff;
  white-space:pre-wrap;word-break:break-word;margin-bottom:12px;
}
.kv{display:grid;grid-template-columns:76px 1fr;gap:4px 10px;font-size:12.5px}
.kv dt{color:var(--dim)}
.kv dd{margin:0;word-break:break-all}
.path{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-family:"JetBrains Mono",Consolas,monospace;font-size:12px}
.path .pnode{background:#0d1117;border:1px solid var(--line);border-radius:6px;padding:2px 8px}
.path .sep{color:var(--accent)}
.deplist{display:flex;flex-wrap:wrap;gap:6px}
.dep{border:1px dashed var(--line);border-radius:6px;padding:2px 8px;font-size:12px;
     font-family:"JetBrains Mono",Consolas,monospace;color:#cfe3ff}
.dep i{color:var(--dim);font-style:normal}
/* SVG 图 */
.graph{overflow:auto;background:#0d1117;border:1px solid var(--line);border-radius:10px;padding:8px}
svg text{font-family:"JetBrains Mono",Consolas,"Microsoft YaHei",sans-serif}
/* SQL */
pre.sql{
  background:#0d1117;border:1px solid var(--line);border-radius:10px;padding:14px;
  overflow:auto;font-size:12.5px;color:#cfe3ff;margin:0;white-space:pre;
}
/* 页脚 */
footer{color:var(--dim);font-size:12.5px;text-align:center;padding:8px 0 0;border-top:1px solid var(--line);margin-top:8px}
"""

_JS = """
(function () {
  var box = document.getElementById('col-filter');
  if (!box) { return; }
  var rows = Array.prototype.slice.call(document.querySelectorAll('#col-table tbody tr'));
  var counter = document.getElementById('col-count');
  function apply() {
    var q = (box.value || '').trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (tr) {
      var hit = !q || (tr.getAttribute('data-key') || '').indexOf(q) >= 0;
      tr.style.display = hit ? '' : 'none';
      if (hit) { shown += 1; }
    });
    if (counter) {
      counter.textContent = q ? ('过滤出 ' + shown + ' / ' + rows.length + ' 行')
                              : (rows.length + ' 行字段映射');
    }
  }
  box.addEventListener('input', apply);
  apply();
})();
"""


# --------------------------------------------------------------------------- #
# 各段落渲染
# --------------------------------------------------------------------------- #
def _hero(meta: Dict[str, Any], stats: Dict[str, Any]) -> str:
    task = _text(meta.get("task_name"), "LINEAGE 血缘分析任务")
    workflow = meta.get("workflow") or {}
    chips = [
        ("任务", task, ""),
        ("生成时间", _text(meta.get("generated_at")), ""),
        ("解析耗时", f"{_text(meta.get('cost_ms'), '?')} ms", ""),
        ("方言", _text(meta.get("dialect"), "-"), ""),
        ("字段映射", f"{stats['column_count']} 个", ""),
        ("表级流向", f"{stats['input_count']} 源表 → {stats['output_count']} 目标表", ""),
    ]
    if workflow:
        # 工作流级报告：把「这是哪个工作流 / 几个任务 / 全链路多长」顶到最前面
        chips = [
            ("工作流", _text(workflow.get("name"), "-"), ""),
            ("生成时间", _text(meta.get("generated_at")), ""),
            ("任务", f"{_text(workflow.get('task_count'), '?')} 个"
                     f"（解析 {_text(workflow.get('parsed_task_count'), '?')} 个）", ""),
            ("全链路", f"{len(workflow.get('chain') or [])} 级", ""),
            ("解析耗时", f"{_text(meta.get('cost_ms'), '?')} ms", ""),
            ("方言", _text(meta.get("dialect"), "-"), ""),
            ("字段映射", f"{stats['column_count']} 个", ""),
            ("表级流向", f"{stats['input_count']} 源表 → {stats['output_count']} 目标表", ""),
        ]
    if stats["kb_available"]:
        chips.append(("业务口径", f"命中 {stats['metric_count']} 条", "green"))
    else:
        chips.append(("业务口径", "知识库未命中 / 不可用", "amber"))
    if meta.get("report_id"):
        chips.append(("报告 ID", _text(meta.get("report_id")), "purple"))
    chip_html = "".join(
        f'<span class="chip {cls}">{_esc(label)} <b>{_esc(value)}</b></span>' for label, value, cls in chips
    )
    if workflow:
        title, sub = "工作流血缘分析报告", "WORKFLOW LINEAGE REPORT"
        footer = ("由 DolphinScheduler LINEAGE_DAG 任务插件生成（一次解析整个工作流）"
                  f" · 血缘服务 {_esc(_text(meta.get('service_url')))}")
        id_html = f"<h1>{_esc(title)}<small>{sub}</small></h1>"
    else:
        id_html = "<h1>血缘分析报告<small>LINEAGE REPORT</small></h1>"
        footer = (f"由 DolphinScheduler LINEAGE 任务插件生成 · "
                  f"血缘服务 {_esc(_text(meta.get('service_url')))}")
    return (
        '<header class="hero"><div class="wrap">'
        + id_html
        + f'<p class="sub">{footer}</p>'
        + f'<div class="chips">{chip_html}</div>'
        "</div></header>"
    )


def _section_workflow(workflow: Dict[str, Any], parsed: Dict[str, Any]) -> str:
    """工作流模式的第一屏：任务清单 + 全链路 + 链路质量体检。

    只有 ``meta.workflow`` 存在时才渲染 —— 单任务报告一个字节都不变。
    """
    tasks = list(workflow.get("tasks") or [])
    quality = workflow.get("quality") or {}
    summary = quality.get("summary") or {}
    chain = list(workflow.get("chain") or [])

    rows: List[str] = []
    for i, task in enumerate(tasks, start=1):
        outs = list(task.get("output_tables") or [])
        out_html = "".join(f'<span class="dep">{_esc(o)}</span>' for o in outs[:4])
        if len(outs) > 4:
            out_html += f'<span class="badge">+{len(outs) - 4}</span>'
        if not out_html:
            out_html = '<span class="badge">无产出</span>'
        errors = list(task.get("errors") or [])
        status = ('<span class="badge ok">已解析</span>' if task.get("parsed")
                  else '<span class="badge warn">跳过/无脚本</span>')
        rows.append(
            "<tr>"
            f'<td class="idx">{i}</td>'
            f'<td class="tgt">{_esc(_text(task.get("name")))}</td>'
            f'<td><span class="badge">{_esc(_text(task.get("type")))}</span></td>'
            f'<td class="mono">{_esc(_text(task.get("script_len"), "0"))}</td>'
            f'<td class="mono">{_esc(_text(task.get("statement_count"), "0"))}</td>'
            f"<td>{out_html}</td>"
            f'<td class="mono">{_esc(_text(task.get("metric_count"), "0"))}</td>'
            f"<td>{status}</td>"
            f'<td class="tbl">{_esc("；".join(errors[:2])) if errors else "-"}</td>'
            "</tr>"
        )
    task_table = (
        '<div class="table-scroll" style="max-height:420px"><table><thead><tr>'
        "<th>#</th><th>任务名</th><th>类型</th><th>脚本字符</th><th>语句</th>"
        "<th>产出表</th><th>口径</th><th>状态</th><th>备注</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        if rows else '<div class="empty">工作流里没有可解析的任务</div>'
    )

    def _badge(count: int, ok_text: str, warn_text: str) -> str:
        if count:
            return f'<span class="badge warn">{_esc(warn_text)}</span>'
        return f'<span class="badge ok">{_esc(ok_text)}</span>'

    def _items(key: str, extra: str = "", arrow: str = "") -> str:
        items = quality.get(key) or []
        if not items:
            return ""
        chips = []
        for item in items[:8]:
            note = item.get(extra) if extra else None
            suffix = ""
            if note and arrow:
                suffix = f'<i> {arrow} {_esc(", ".join(str(n) for n in note[:3]))}</i>'
            elif item.get("cross_workflow"):
                suffix = "<i> 跨工作流</i>"
            chips.append(f'<span class="dep">{_esc(_text(item.get("table")))}{suffix}</span>')
        more = f'<span class="badge">共 {len(items)} 张</span>' if len(items) > 8 else ""
        return f'<div class="deplist" style="margin-top:8px">{"".join(chips)}{more}</div>'

    dangling = summary.get("dangling_output_count") or 0
    orphan = summary.get("orphan_input_count") or 0
    cycles = summary.get("cycle_count") or 0
    missing = summary.get("missing_knowledge_count") or 0
    quality_html = (
        '<div class="deplist">'
        + _badge(cycles, "✅ 无环路", f"⚠ {cycles} 个环路")
        + _badge(dangling, "✅ 无断链（产出表都有下游）",
                 f"⚠ {dangling} 张产出表本工作流内无人消费"
                 f"（其中 {summary.get('dangling_cross_workflow_count') or 0} 张下游在别的工作流）")
        + _badge(orphan, "✅ 无孤岛输入（输入表都有上游）",
                 f"⚠ {orphan} 张输入表本工作流内无上游"
                 f"（其中 {summary.get('orphan_cross_workflow_count') or 0} 张上游在别的工作流）")
        + _badge(missing, "✅ 产出表口径已全部登记",
                 f"⚠ {missing} 张产出表未登记口径")
        + "</div>"
    )
    quality_html += _items("dangling_outputs", "global_downstream", "→")
    quality_html += _items("orphan_inputs", "global_upstream", "←")
    quality_html += _items("missing_knowledge")
    # 生成引擎（L4 反向校验）专用：分层规则违规（跨层直连）逐条列出
    layer_rules = list(workflow.get("layer_rules") or [])
    if layer_rules:
        chips = "".join(
            f'<span class="dep">{_esc(_text(item.get("source")))} → {_esc(_text(item.get("target")))}</span>'
            for item in layer_rules[:8]
        )
        quality_html += (
            '<div class="deplist" style="margin-top:10px">'
            f'<span class="badge warn">⚠ 分层规则违规 {len(layer_rules)} 条'
            f'（跨层直连，应逐层加工）</span>{chips}</div>'
        )

    chain_html = (
        '<div class="path" style="margin-top:6px">' + _path_html(chain) + "</div>"
        if chain else '<div class="empty">未解析出跨任务链路（脚本可能都是单表加工）</div>'
    )
    external = workflow.get("chain_external") or {}
    tail = ""
    if external.get("downstream"):
        tail = ('<p class="hint">本工作流产出其后还会流向：'
                + _esc(", ".join(external["downstream"])) + "（全局血缘，可能在别的工作流）</p>")

    return (
        '<section id="workflow"><h2><span class="n">W</span>工作流概览'
        f'<span class="count">{_esc(_text(workflow.get("project_code")))} / '
        f'{_esc(_text(workflow.get("code")))} · {len(tasks)} 个任务</span></h2>'
        '<h3 style="margin:0 0 10px;font-size:14px">任务清单</h3>'
        f"{task_table}"
        '<h3 style="margin:22px 0 10px;font-size:14px">全链路（跨任务）</h3>'
        f"{chain_html}{tail}"
        '<h3 style="margin:22px 0 10px;font-size:14px">链路质量体检</h3>'
        f"{quality_html}"
        "</section>"
    )


def _section_tables(edges: List[Dict[str, Any]], tables_in: List[str], tables_out: List[str]) -> str:
    rows: List[str] = []
    if edges:
        for edge in edges:
            rows.append(
                '<div class="flow-row">'
                f'<div class="node src">{_esc(_text(edge.get("source")))}</div>'
                '<div class="arrow">──►</div>'
                f'<div class="node tgt">{_esc(_text(edge.get("target")))}</div>'
                "</div>"
            )
    elif tables_in and tables_out:
        for src in tables_in:
            for tgt in tables_out:
                rows.append(
                    '<div class="flow-row">'
                    f'<div class="node src">{_esc(src)}</div><div class="arrow">──►</div>'
                    f'<div class="node tgt">{_esc(tgt)}</div></div>'
                )
    body = "".join(rows) or '<div class="empty">未解析出表级血缘</div>'
    legend = (
        '<div class="legend">'
        '<span><span class="dot" style="background:#d29922"></span>源表（输入）</span>'
        '<span><span class="dot" style="background:#3fb950"></span>目标表（输出）</span>'
        "</div>"
    )
    return (
        '<section id="tables"><h2><span class="n">1</span>表级血缘'
        f'<span class="count">{len(tables_in)} 张源表 → {len(tables_out)} 张目标表</span></h2>'
        f'<div class="flow">{body}</div>'
        f"{legend}"
        "</section>"
    )


def _section_columns(columns: List[Dict[str, Any]], show_task: bool = False) -> str:
    """字段级血缘真表格；工作流模式下多一列「来源任务」（跨任务字段血缘的关键）。"""
    rows: List[str] = []
    span = 8 if show_task else 7
    for i, col in enumerate(columns[:MAX_COLUMN_ROWS], start=1):
        tgt_table = _text(col.get("target_table"))
        tgt_col = _text(col.get("target_column"))
        src_table = _text(col.get("source_table"))
        src_col = _text(col.get("source_column"))
        expr = _text(col.get("expression"))
        resolved = col.get("resolved")
        badge = ('<span class="badge ok">已解析</span>' if resolved
                 else '<span class="badge warn">未解析</span>')
        key = f"{tgt_table} {tgt_col} {src_table} {src_col} {expr} {col.get('task') or ''}".lower()
        task_cell = (f'<td class="tbl">{_esc(_text(col.get("task"), "-"))}</td>' if show_task else "")
        rows.append(
            f'<tr data-key="{_esc(key)}">'
            f'<td class="idx">{i}</td>'
            f"{task_cell}"
            f'<td class="tbl">{_esc(tgt_table)}</td>'
            f'<td class="tgt">{_esc(tgt_col)}</td>'
            f'<td class="tbl">{_esc(src_table)}</td>'
            f'<td class="col">{_esc(src_col)}</td>'
            f'<td class="expr">{_esc(expr)}</td>'
            f"<td>{badge}</td>"
            "</tr>"
        )
    if not rows:
        return (
            '<section id="columns"><h2><span class="n">2</span>字段级血缘'
            '<span class="count">0 个字段映射</span></h2>'
            '<div class="empty">未解析出字段级血缘</div></section>'
        )
    more = ""
    if len(columns) > MAX_COLUMN_ROWS:
        more = f'<p class="hint">仅展示前 {MAX_COLUMN_ROWS} 行，共 {len(columns)} 行。</p>'
    task_head = "<th>来源任务</th>" if show_task else ""
    return (
        '<section id="columns"><h2><span class="n">2</span>字段级血缘'
        + (f'<span class="badge" style="margin-left:10px">跨任务 · {span - 1} 列</span>' if show_task else "")
        + '<span class="count" id="col-count">'
        f"{len(columns)} 行字段映射</span></h2>"
        '<div class="toolbar">'
        '<input id="col-filter" type="text" placeholder="输入字段名 / 表名 / 表达式关键字过滤，例如 chanliang_qty">'
        "</div>"
        '<div class="table-scroll"><table id="col-table"><thead><tr>'
        f"<th>#</th>{task_head}<th>目标表</th><th>目标字段</th><th>来源表</th><th>来源字段</th><th>加工表达式</th><th>状态</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
        + more
        + "</section>"
    )


def _path_html(path: Sequence[str]) -> str:
    if not path:
        return '<span class="badge">未登记链路</span>'
    nodes = []
    for i, name in enumerate(path):
        if i:
            nodes.append('<span class="sep">→</span>')
        nodes.append(f'<span class="pnode">{_esc(name)}</span>')
    return "".join(nodes)


def _section_knowledge(metrics: List[Dict[str, Any]], kb: Dict[str, Any], kb_available: bool) -> str:
    head = (
        '<section id="knowledge"><h2><span class="n">3</span>业务口径（知识库匹配）'
        f'<span class="count">命中 {len(metrics)} 条</span></h2>'
    )
    if not kb_available:
        reason = _esc(_text(kb.get("reason"), "知识库未命中 / 不可用"))
        return head + f'<div class="empty">未匹配到业务口径：{reason}</div>' + _meta_extras(kb) + "</section>"
    if not metrics:
        return head + '<div class="empty">知识库已就绪，但本任务产出字段未匹配到已登记指标口径</div>' + _meta_extras(kb) + "</section>"

    cards = []
    for metric in rank_metrics(metrics):
        cn = _text(metric.get("chinese_name"), "")
        column = _text(metric.get("target_column"), "")
        formula = metric.get("formula_full") or metric.get("formula") or ""
        deps = metric.get("depends_on") or []
        dep_html = "".join(
            f'<span class="dep">{_esc(d.get("table"))}.{_esc(d.get("column"))}'
            f'{"<i>（" + _esc(d.get("chinese_name")) + "）</i>" if d.get("chinese_name") else ""}</span>'
            for d in deps
        ) or '<span class="badge">未登记依赖字段</span>'
        tags = [
            f'<span class="chip purple">{_esc(_text(metric.get("metric_type"), "未知类型"))}</span>',
            f'<span class="chip">置信度 <b>{_esc(_text(metric.get("confidence")))}</b></span>',
            f'<span class="chip">{_esc(_text(metric.get("matched_by"), "-"))}命中</span>',
        ]
        if metric.get("unit"):
            tags.append(f'<span class="chip green">单位 {_esc(metric["unit"])}</span>')
        if metric.get("layer"):
            tags.append(f'<span class="chip">{_esc(metric["layer"])} 层</span>')
        cards.append(
            '<div class="card">'
            f'<h3><span class="cn">{_esc(cn or column)}</span>'
            f'<span class="col">{_esc(column)}</span></h3>'
            f'<div class="tags">{"".join(tags)}</div>'
            f'<div class="formula">{_esc(_text(formula, "(无公式)"))}</div>'
            "<dl class=\"kv\">"
            f'<dt>目标表</dt><dd class="mono">{_esc(_text(metric.get("target_table")))}</dd>'
            f'<dt>来源脚本</dt><dd>{_esc(_text(metric.get("source_script")))}'
            f'（第 {_esc(_text(metric.get("source_statement")))} 条语句）</dd>'
            f'<dt>依赖字段</dt><dd><div class="deplist">{dep_html}</div></dd>'
            f'<dt>上游链路</dt><dd><div class="path">{_path_html(metric.get("lineage_path") or [])}</div></dd>'
            "</dl></div>"
        )
    return head + f'<div class="cards">{"".join(cards)}</div>' + _meta_extras(kb) + "</section>"


def _meta_extras(kb: Dict[str, Any]) -> str:
    """术语 + 业务规则：卡片区之外的补充信息（有才渲染）。"""
    parts: List[str] = []
    terms = kb.get("terms") or []
    if terms:
        chips = "".join(
            f'<span class="dep">{_esc(t.get("field"))}<i> → {_esc(t.get("chinese_name"))}</i></span>'
            for t in terms[:24]
        )
        more = f'<span class="badge">共 {len(terms)} 项</span>' if len(terms) > 24 else ""
        parts.append(
            '<h3 style="margin:22px 0 10px;font-size:14px">涉及字段的中文业务名</h3>'
            f'<div class="deplist">{chips}{more}</div>'
        )
    rules = kb.get("rules") or []
    if rules:
        items = "".join(
            f'<li><span class="badge">{_esc(_text(r.get("rule_type")))}</span> '
            f'{_esc(_text(r.get("description")))} '
            f'<span class="badge">{_esc(_text(r.get("source_script")))}</span></li>'
            for r in rules
        )
        parts.append(
            '<h3 style="margin:22px 0 10px;font-size:14px">业务规则</h3>'
            f'<ul class="rules" style="margin:0;padding-left:20px;font-size:13px;line-height:2">{items}</ul>'
        )
    if parts:
        parts.append('<p class="hint">口径由 <code>kb build</code> 从加工脚本自动提炼（语法级，未做语义校验）。</p>')
    return "".join(parts)


def _svg_graph(metrics: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> str:
    """上游链路图：内联 SVG，左=最上游，右=本任务目标表。"""
    paths: List[List[str]] = []
    for metric in metrics:
        path = [p for p in (metric.get("lineage_path") or []) if p]
        if len(path) >= 2:
            paths.append(path)
    if not paths:
        # 没有口径链路就退回表级血缘（源表 → 目标表）
        for edge in edges:
            src, tgt = edge.get("source"), edge.get("target")
            if src and tgt:
                paths.append([tgt, src])
    if not paths:
        return '<div class="empty">无可绘制的上游链路（未匹配到口径或表级血缘）</div>'

    max_depth = max(len(p) - 1 for p in paths)
    depth: Dict[str, int] = {}
    order: Dict[str, int] = {}
    layer_count: Dict[int, int] = {}
    for path in paths:
        for index, node in enumerate(path):
            # index 0 = 目标表（最右）；越靠后越上游（越靠左）
            depth[node] = max(depth.get(node, 0), index)
    for path in paths:
        for index, node in enumerate(path):
            if node in order or depth[node] != index:
                continue
            level = max_depth - index
            order[node] = layer_count.get(level, 0)
            layer_count[level] = order[node] + 1

    nodes = sorted(depth.keys(), key=lambda n: (-depth[n], order.get(n, 0)))[:MAX_GRAPH_NODES]
    box_w, box_h, pad_x, pad_y, gap_y = 216, 42, 26, 26, 18
    col_w = box_w + 78
    width = pad_x * 2 + box_w + col_w * max_depth
    height = pad_y * 2 + max(layer_count.values() or [1]) * (box_h + gap_y) - gap_y

    def pos(node: str) -> Tuple[int, int]:
        index = depth[node]
        level = max_depth - index
        x = pad_x + level * col_w
        y = pad_y + order.get(node, 0) * (box_h + gap_y)
        return x, y

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="上游链路图">',
        "<defs><marker id=\"arrow\" viewBox=\"0 0 10 10\" refX=\"9\" refY=\"5\" markerWidth=\"7\" "
        "markerHeight=\"7\" orient=\"auto-start-reverse\">"
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#58a6ff"/></marker></defs>',
    ]

    seen_edges = set()
    for path in paths:
        for i in range(len(path) - 1):
            downstream, upstream = path[i], path[i + 1]
            if (upstream, downstream) in seen_edges:
                continue
            seen_edges.add((upstream, downstream))
            if upstream not in nodes or downstream not in nodes:
                continue
            x1, y1 = pos(upstream)
            x2, y2 = pos(downstream)
            parts.append(
                f'<line x1="{x1 + box_w}" y1="{y1 + box_h // 2}" x2="{x2 - 6}" y2="{y2 + box_h // 2}" '
                'stroke="#58a6ff" stroke-width="1.6" marker-end="url(#arrow)" opacity="0.85"/>'
            )

    for node in nodes:
        x, y = pos(node)
        is_target = any((metric.get("target_table") or "") == node for metric in metrics)
        stroke = "#3fb950" if is_target else "#2a3441"
        fill = "#12261a" if is_target else "#1b2330"
        parts.append(
            f'<rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" rx="9" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="1.4"/>'
            f'<text x="{x + 12}" y="{y + 26}" fill="#e6edf3" font-size="12.5">'
            f'{_esc(_clip(node, 30))}</text>'
        )
    parts.append("</svg>")
    note = (
        '<p class="hint">节点 = 表，箭头 = 数据流向（左：最上游 → 右：本任务目标表，绿框）。'
        f"共 {len(nodes)} 个节点 / {len(seen_edges)} 条边。</p>"
    )
    return f'<div class="graph">{"".join(parts)}</div>{note}'


def _table_layer(table: str) -> str:
    """表名前缀当分层名（``cdw.dwd_x`` → ``cdw``），用于给链路节点上色。"""
    name = str(table or "")
    return name.split(".")[0].lower() if "." in name else ""


def _svg_workflow_graph(edges: List[Dict[str, Any]], max_nodes: int = MAX_GRAPH_NODES) -> str:
    """工作流级全链路图：按「最长路径层级」把表排成左→右若干层（内联 SVG）。

    与 :func:`_svg_graph`（单任务口径上游链路）的区别：这里画的是**整张表级 DAG**，
    所以能一眼看出 ``src → ods → cdw.dwd → cdw.dws → ads`` 这种跨任务主干，
    以及哪张表挂了多条下游（分叉）。
    """
    nodes = sorted({t for edge in edges for t in (edge.get("source"), edge.get("target")) if t})
    if not nodes:
        return '<div class="empty">未解析出表级血缘</div>'
    adj: Dict[str, List[str]] = {n: [] for n in nodes}
    for edge in edges:
        src, tgt = edge.get("source"), edge.get("target")
        if src in adj and tgt in adj and tgt not in adj[src]:
            adj[src].append(tgt)

    depth: Dict[str, int] = {n: 0 for n in nodes}
    for _ in range(len(nodes)):  # 松弛法算子图最长路径（有环时也会停下来）
        changed = False
        for node in nodes:
            for nxt in adj[node]:
                if depth[nxt] < depth[node] + 1:
                    depth[nxt] = depth[node] + 1
                    changed = True
        if not changed:
            break

    kept = sorted(sorted(nodes, key=lambda n: (-depth[n], n))[:max_nodes], key=lambda n: (depth[n], n))
    allow = set(kept)
    layers = sorted({depth[n] for n in kept})
    order: Dict[int, int] = {}
    positions: Dict[str, Tuple[int, int, int]] = {}
    box_w, box_h, pad_x, pad_y, gap_y, col_w = 228, 42, 24, 24, 16, 300
    for node in kept:
        level = layers.index(depth[node])
        row = order.get(level, 0)
        order[level] = row + 1
        positions[node] = (pad_x + level * col_w, pad_y + row * (box_h + gap_y), level)

    width = pad_x * 2 + box_w + col_w * max(len(layers) - 1, 0)
    height = pad_y * 2 + max(order.values() or [1]) * (box_h + gap_y) - gap_y
    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="工作流全链路图">',
        "<defs><marker id=\"warrow\" viewBox=\"0 0 10 10\" refX=\"9\" refY=\"5\" markerWidth=\"7\" "
        "markerHeight=\"7\" orient=\"auto-start-reverse\">"
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#58a6ff"/></marker></defs>',
    ]
    for edge in edges:
        src, tgt = edge.get("source"), edge.get("target")
        if src not in allow or tgt not in allow or src == tgt:
            continue
        x1, y1, lv1 = positions[src]
        x2, y2, lv2 = positions[tgt]
        if lv2 <= lv1:  # 同层 / 回边：画条小弧线意思一下，别穿过节点
            parts.append(
                f'<path d="M {x1 + box_w} {y1 + box_h // 2} C {x1 + box_w + 40} {y1 + box_h // 2},'
                f' {x1 + box_w + 40} {y2 + box_h // 2}, {x2 + box_w} {y2 + box_h // 2}" fill="none" '
                'stroke="#f85149" stroke-width="1.6" marker-end="url(#warrow)" opacity="0.9"/>'
            )
            continue
        parts.append(
            f'<line x1="{x1 + box_w}" y1="{y1 + box_h // 2}" x2="{x2 - 6}" y2="{y2 + box_h // 2}" '
            'stroke="#58a6ff" stroke-width="1.6" marker-end="url(#warrow)" opacity="0.85"/>'
        )
    for node in kept:
        x, y, _lv = positions[node]
        layer = _table_layer(node)
        stroke = {"src": "#d29922", "ods": "#d29922", "ads": "#bc8cff"}.get(layer, "#2a3441")
        fill = {"ads": "#241a33"}.get(layer, "#1b2330")
        parts.append(
            f'<rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" rx="9" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="1.4"/>'
            f'<text x="{x + 12}" y="{y + 26}" fill="#e6edf3" font-size="12.5">'
            f'{_esc(_clip(node, 32))}</text>'
        )
    parts.append("</svg>")
    return (
        f'<div class="graph">{"".join(parts)}</div>'
        f'<p class="hint">节点 = 表，箭头 = 数据流向（左：贴源 → 右：应用层）。'
        f"共 {len(kept)} 个节点 / {len(edges)} 条边"
        + (f"，另有 {len(nodes) - len(kept)} 个节点未画出。" if len(nodes) > len(kept) else "。")
        + "</p>"
    )


def _section_workflow_graph(edges: List[Dict[str, Any]]) -> str:
    return (
        '<section id="upstream"><h2><span class="n">4</span>全链路图'
        '<span class="count">工作流内表级 DAG（跨任务）</span></h2>'
        + _svg_workflow_graph(edges)
        + "</section>"
    )


def _section_upstream(metrics: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> str:
    return (
        '<section id="upstream"><h2><span class="n">4</span>上游链路图'
        '<span class="count">口径上游链路（跨层）</span></h2>'
        + _svg_graph(metrics, edges)
        + "</section>"
    )


def _section_sql(statements: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    cond_rows: List[str] = []
    sql_texts: List[str] = []
    for st in statements or []:
        index = st.get("statement_index")
        # 工作流模式下每条语句都带上了来源任务，标出来才不会看不出是谁的 SQL
        label = f"任务 {st.get('task')} · 语句 {index}" if st.get("task") else f"语句 {index}"
        for item in st.get("filters") or []:
            cond_rows.append(f'<span class="dep">{_esc(label)} 过滤 <i>{_esc(item)}</i></span>')
        pf = st.get("partition_filters") or {}
        for key, value in pf.items():
            cond_rows.append(f'<span class="dep">{_esc(label)} 分区 <i>{_esc(key)} = {_esc(value)}</i></span>')
        if st.get("sql"):
            sql_texts.append(st["sql"])
    if not sql_texts and meta.get("sql"):
        sql_texts.append(meta["sql"])
    cond_html = (
        f'<div class="deplist">{"".join(cond_rows)}</div>' if cond_rows
        else '<div class="empty">无条件</div>'
    )
    sql_html = (
        "".join(f'<pre class="sql">{_esc(text)}</pre>' for text in sql_texts)
        if sql_texts else '<div class="empty">无 SQL 原文</div>'
    )
    return (
        '<section id="sql"><h2><span class="n">5</span>加工条件 / SQL 原文'
        f'<span class="count">{len(statements)} 条语句</span></h2>'
        f"{cond_html}"
        f'<h3 style="margin:18px 0 10px;font-size:14px">SQL 原文</h3>{sql_html}'
        "</section>"
    )


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def _apply_defaults(meta: Dict[str, Any], parsed: Dict[str, Any]) -> Dict[str, Any]:
    """补齐标题栏/页脚要用的元信息（缺 key 或值为 None 都算缺）。"""
    if not meta.get("generated_at"):
        meta["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not meta.get("dialect"):
        meta["dialect"] = parsed.get("dialect")
    if not meta.get("column_count"):
        meta["column_count"] = parsed.get("column_lineage_count") or len(parsed.get("column_lineage") or [])
    return meta


def render_report(parsed: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> str:
    """把一个 ``/analyze`` 风格的结果 dict 渲染成完整 HTML 文本（单文件、零外部依赖）。"""
    meta = _apply_defaults(dict(meta or {}), parsed)
    knowledge = parsed.get("knowledge") or {}
    kb_available = bool(knowledge.get("kb_available"))
    metrics = list(knowledge.get("metrics") or []) if kb_available else []
    columns = list(parsed.get("column_lineage") or [])
    edges = list(parsed.get("table_lineage") or [])
    tables_in = list(parsed.get("input_tables") or [])
    tables_out = list(parsed.get("output_tables") or [])
    statements = list(parsed.get("statements") or [])

    stats = {
        "input_count": len(tables_in),
        "output_count": len(tables_out),
        "column_count": len(columns),
        "metric_count": len(metrics),
        "kb_available": kb_available,
    }

    title = f"血缘分析报告 {meta.get('report_id') or ''}".strip()
    workflow = meta.get("workflow") or {}
    if workflow:
        title = f"工作流血缘分析报告 {workflow.get('name') or ''} {meta.get('report_id') or ''}".strip()
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n"
        + _hero(meta, stats)
        + '<div class="wrap">'
        + (_section_workflow(workflow, parsed) if workflow else "")
        + _section_tables(edges, tables_in, tables_out)
        + _section_columns(columns, show_task=bool(workflow))
        + _section_knowledge(metrics, knowledge, kb_available)
        + (_section_workflow_graph(edges) if workflow else _section_upstream(metrics, edges))
        + _section_sql(statements, meta)
        + "<footer>"
        + f"生成时间 {_esc(meta.get('generated_at'))} · 报告 ID {_esc(_text(meta.get('report_id')))} · "
        + f"由 sql-lineage-mvp 生成（服务 {_esc(_text(meta.get('service_url')))}）"
        + "</footer></div>\n"
        + f"<script>{_JS}</script>\n</body>\n</html>\n"
    )


def save_report(
    parsed: Dict[str, Any],
    meta: Optional[Dict[str, Any]] = None,
    directory: Optional[Path] = None,
    report_id: Optional[str] = None,
) -> Dict[str, Any]:
    """渲染并落盘，返回 ``{report_id, path, size_bytes, url, internal_url, generated_at}``。"""
    meta = _apply_defaults(dict(meta or {}), parsed)
    sql = str(meta.get("sql") or "")
    rid = report_id or make_report_id(sql)
    base = Path(directory) if directory else reports_dir()
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"{safe_report_id(rid)}.html"
    meta["report_id"] = target.stem
    html_text = render_report(parsed, meta)
    target.write_text(html_text, encoding="utf-8")
    prune_reports(base)
    return {
        "report_id": target.stem,
        "path": str(target),
        "file": target.name,
        "size_bytes": target.stat().st_size,
        "url": report_url(target.stem),
        "internal_url": internal_report_url(target.stem),
        "generated_at": meta["generated_at"],
        "task_name": meta.get("task_name") or "",
    }
