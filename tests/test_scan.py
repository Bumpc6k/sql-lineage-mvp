"""目录批量扫描 + P2 子命令端到端测试。

覆盖：真实示例目录的扫描结果、跨文件链路贯通、忽略目录、单文件失败不影响整体、
空文件识别、循环依赖样例，以及 scan/upstream/impact/path/cycle/stats/viz 子命令。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lineage.graph import LineageGraph
from lineage.scan import DEFAULT_IGNORE_DIRS, ScanResult, discover_sql_files, scan_directory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = PROJECT_ROOT / "examples" / "warehouse"
ISSUES = PROJECT_ROOT / "examples" / "warehouse_issues"

#: 示例数仓目录的基线数字（跑 scan 得到，改动示例目录时需同步）
WAREHOUSE_FILES = 21
WAREHOUSE_STATEMENTS = 24
WAREHOUSE_TABLES = 34
WAREHOUSE_EDGES = 41
WAREHOUSE_MAX_DEPTH = 7


@pytest.fixture(scope="module")
def warehouse_scan() -> ScanResult:
    return scan_directory(WAREHOUSE, dialect="hive")


# ---------------------------------------------------------------- 目录发现 --
def test_discover_sql_files_finds_all() -> None:
    files, _ = discover_sql_files(WAREHOUSE)
    assert len(files) == WAREHOUSE_FILES
    assert all(f.suffix == ".sql" for f in files)
    # 结果稳定排序（跨平台一致）
    assert files == sorted(files, key=str)


def test_discover_sql_files_ignores_noise_dirs(tmp_path: Path) -> None:
    (tmp_path / "ods").mkdir()
    (tmp_path / "ods" / "a.sql").write_text("CREATE TABLE t AS SELECT id FROM s.a;", encoding="utf-8")
    for noisy in (".venv", "__pycache__", ".git", "node_modules"):
        (tmp_path / noisy).mkdir()
        (tmp_path / noisy / "should_be_ignored.sql").write_text(
            "CREATE TABLE z AS SELECT id FROM src.q;", encoding="utf-8"
        )
    files, ignored = discover_sql_files(tmp_path)
    assert [f.name for f in files] == ["a.sql"]
    assert len(ignored) == 4
    assert {"__pycache__", ".venv", ".git", "node_modules"} <= DEFAULT_IGNORE_DIRS


def test_scan_ignores_noise_dirs_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "ods").mkdir()
    (tmp_path / "ods" / "a.sql").write_text("CREATE TABLE t AS SELECT id FROM s.a;", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "skip.sql").write_text("CREATE TABLE q AS SELECT id FROM src.z;", encoding="utf-8")
    result = scan_directory(tmp_path)
    assert result.file_count == 1
    assert result.table_count == 2
    assert result.edge_count == 1
    assert "q" not in result.graph.nodes


# ---------------------------------------------------------------- 真实示例 --
def test_scan_warehouse_baseline(warehouse_scan: ScanResult) -> None:
    s = warehouse_scan.summary_dict()
    assert s["file_count"] == WAREHOUSE_FILES
    assert s["statement_count"] == WAREHOUSE_STATEMENTS
    assert s["table_count"] == WAREHOUSE_TABLES
    assert s["edge_count"] == WAREHOUSE_EDGES
    assert s["max_depth"] == WAREHOUSE_MAX_DEPTH
    assert s["parse_failure_count"] == 0
    assert s["empty_file_count"] == 0
    assert s["cycle_count"] == 0
    assert s["multi_statement_file_count"] == 2
    assert s["no_output_statement_count"] == 0
    assert s["layer_counts"] == {"src": 6, "ods": 6, "dim": 4, "dwd": 6, "dws": 6, "ads": 6}
    assert warehouse_scan.failures == []


def test_scan_warehouse_files_are_relative(warehouse_scan: ScanResult) -> None:
    assert len(warehouse_scan.files) == WAREHOUSE_FILES
    assert all(not f.startswith("/") for f in warehouse_scan.files)
    assert "ods/ods_卷烟产量流水.sql" in warehouse_scan.files
    assert "cdw/dws_产销存汇总.sql" in warehouse_scan.files


def test_scan_report_text_is_readable(warehouse_scan: ScanResult) -> None:
    text = warehouse_scan.report_text()
    assert "数仓脚本目录扫描报告" in text
    assert f"SQL 文件数：{WAREHOUSE_FILES}" in text
    assert f"表（节点）数：{WAREHOUSE_TABLES}" in text
    assert "解析失败：0 个文件" in text


def test_scan_detects_multi_statement_files(warehouse_scan: ScanResult) -> None:
    assert warehouse_scan.multi_statement_files == [
        "ads/ads_经营指标驾驶舱.sql",
        "cdw/dws_产销存汇总.sql",
    ]


def test_cross_file_chain_is_connected(warehouse_scan: ScanResult) -> None:
    """跨文件链路必须贯通：src 层 -> ods -> dwd -> dws -> ads -> ads 驾驶舱。"""
    g = warehouse_scan.graph
    chain = [
        "src.erp_生产工单明细",
        "ods.ods_卷烟产量流水",
        "cdw.dwd_卷烟产量明细",
        "cdw.dws_产量汇总",
        "cdw.dws_产销存汇总",
        "ads.ads_产销存月报",
        "ads.ads_经营指标明细",
        "ads.ads_经营指标驾驶舱",
    ]
    for up, down in zip(chain, chain[1:]):
        assert g.edge(up, down) is not None, f"跨文件血缘断开：{up} -> {down}"

    res = g.path_between(chain[0], chain[-1])
    assert res["found"] is True
    assert res["shortest"] == chain
    assert res["shortest_length"] == 8

    # 每一条边都应能追溯到具体文件（跨文件证据）
    for up, down in zip(chain, chain[1:]):
        assert g.edge(up, down).files, f"边 {up}->{down} 缺少来源文件"


def test_cross_file_merge_same_table_two_files(warehouse_scan: ScanResult) -> None:
    """cdw.dws_产销存汇总 同时被 ads_产销存月报 与 ads_税利分析 消费（跨文件多下游）。"""
    g = warehouse_scan.graph
    assert g.successors("cdw.dws_产销存汇总") == ["ads.ads_产销存月报", "ads.ads_税利分析"]
    assert warehouse_scan.graph.edge("cdw.dws_产销存汇总", "ads.ads_产销存月报").files == [
        "ads/ads_产销存月报.sql"
    ]


def test_scan_max_depth_six_plus_layers(warehouse_scan: ScanResult) -> None:
    """需求：示例目录至少 5 层血缘深度。"""
    assert warehouse_scan.graph.stats()["max_depth"] >= 5
    depths = warehouse_scan.graph.depths()
    assert depths["ads.ads_经营指标驾驶舱"] == WAREHOUSE_MAX_DEPTH
    assert depths["src.erp_生产工单明细"] == 0


def test_scan_graph_is_serializable(warehouse_scan: ScanResult, tmp_path: Path) -> None:
    out = tmp_path / "warehouse_graph.json"
    warehouse_scan.graph.save(out)
    text = out.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["schema_version"] == 1
    assert len(payload["nodes"]) == WAREHOUSE_TABLES
    assert len(payload["edges"]) == WAREHOUSE_EDGES
    assert "卷烟" in text          # 中文未被转义成 \uXXXX
    loaded = LineageGraph.load(out)
    assert loaded.stats() == warehouse_scan.graph.stats()


def test_scan_result_to_dict(warehouse_scan: ScanResult) -> None:
    data = warehouse_scan.to_dict(with_graph=True)
    assert data["summary"]["table_count"] == WAREHOUSE_TABLES
    assert len(data["graph"]["nodes"]) == WAREHOUSE_TABLES
    assert isinstance(data["files"], list)
    assert data["failures"] == []
    light = warehouse_scan.to_dict(with_graph=False)
    assert "graph" not in light


# ---------------------------------------------------------------- 异常场景 --
def test_scan_broken_file_recorded_and_others_survive(tmp_path: Path) -> None:
    (tmp_path / "good.sql").write_text("CREATE TABLE ods.a AS SELECT id FROM src.raw;", encoding="utf-8")
    (tmp_path / "bad.sql").write_text(
        "INSERT OVERWRITE TABLE t SELECT a.b AS c, SUM(x AS y FROM ods.t x\n", encoding="utf-8"
    )
    result = scan_directory(tmp_path)
    assert result.file_count == 2
    assert len(result.failures) == 1
    assert result.failures[0]["file"] == "bad.sql"
    assert "错误" in result.failures[0]["error"] or "Error" in result.failures[0]["error"]
    # 好文件照常入图
    assert ("src.raw", "ods.a") in result.graph.edges
    assert "解析失败：1 个文件" in result.report_text()


def test_scan_empty_and_comment_only_files(tmp_path: Path) -> None:
    (tmp_path / "empty.sql").write_text("", encoding="utf-8")
    (tmp_path / "comments.sql").write_text("-- 只有注释\n-- 没有语句\n", encoding="utf-8")
    (tmp_path / "real.sql").write_text("CREATE TABLE ods.a AS SELECT id FROM src.raw;", encoding="utf-8")
    result = scan_directory(tmp_path)
    assert result.file_count == 3
    assert sorted(result.empty_files) == ["comments.sql", "empty.sql"]
    assert result.statement_count == 1
    assert result.failures == []


def test_scan_empty_directory(tmp_path: Path) -> None:
    result = scan_directory(tmp_path)
    assert result.file_count == 0
    assert result.table_count == 0
    assert result.edge_count == 0
    assert result.statement_count == 0
    assert "SQL 文件数：0" in result.report_text()


def test_scan_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        scan_directory(tmp_path / "不存在")


def test_scan_issues_directory_detects_cycle_and_failure() -> None:
    result = scan_directory(ISSUES)
    assert result.file_count == 2
    assert len(result.failures) == 1
    cycles = result.graph.detect_cycles()
    assert len(cycles) == 1
    assert cycles[0]["nodes"] == ["ads.ads_销量修正结果", "dwd.dwd_销量回写池"]
    assert result.summary_dict()["cycle_count"] == 1


def test_scan_max_files_option(warehouse_scan: ScanResult) -> None:
    partial = scan_directory(WAREHOUSE, max_files=3)
    assert partial.file_count == 3
    assert partial.table_count < warehouse_scan.table_count


# ---------------------------------------------------------------- CLI 端到端 --
def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "lineage.cli", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def test_cli_scan_upstream_impact_end_to_end(tmp_path: Path) -> None:
    graph_path = tmp_path / "graph.json"
    report_path = tmp_path / "report.txt"
    proc = _cli(
        "scan", str(WAREHOUSE),
        "--dialect", "hive",
        "--graph-out", str(graph_path),
        "--report-out", str(report_path),
    )
    assert proc.returncode == 0, proc.stderr
    assert graph_path.exists() and report_path.exists()
    assert "SQL 文件数：21" in proc.stdout
    assert "血缘边数：41" in proc.stdout
    assert "血缘边数：41" in report_path.read_text(encoding="utf-8")

    up = _cli("upstream", "ads.ads_经营指标驾驶舱", "--graph", str(graph_path))
    assert up.returncode == 0, up.stderr
    assert "上游溯源" in up.stdout
    assert "src.erp_生产工单明细" in up.stdout

    up_json = _cli("upstream", "ads_经营指标驾驶舱", "--graph", str(graph_path), "--output", "json")
    payload = json.loads(up_json.stdout)
    assert payload["found"] is True
    assert payload["table"] == "ads.ads_经营指标驾驶舱"
    assert payload["table_count"] == 33

    imp = _cli("impact", "ods.ods_卷烟产量流水", "--graph", str(graph_path), "--depth", "2")
    assert imp.returncode == 0, imp.stderr
    assert "下游影响分析" in imp.stdout
    assert "cdw.dws_产量汇总" in imp.stdout
    assert "ads.ads_产销存月报" not in imp.stdout      # 深度 2 之外

    miss = _cli("upstream", "根本不存在的表", "--graph", str(graph_path))
    assert miss.returncode == 1
    assert "找不到表" in miss.stdout


def test_cli_path_cycle_stats(tmp_path: Path) -> None:
    graph_path = tmp_path / "graph.json"
    _cli("scan", str(WAREHOUSE), "--graph-out", str(graph_path), "--quiet")

    p = _cli("path", "src.erp_生产工单明细", "ads.ads_经营指标驾驶舱", "--graph", str(graph_path))
    assert p.returncode == 0, p.stderr
    assert "最短长度：8" in p.stdout

    c = _cli("cycle", "--graph", str(graph_path))
    assert c.returncode == 0
    assert "未发现循环依赖" in c.stdout

    issues_graph = tmp_path / "issues_graph.json"
    _cli("scan", str(ISSUES), "--graph-out", str(issues_graph), "--quiet")
    c2 = _cli("cycle", "--graph", str(issues_graph))
    assert c2.returncode == 1
    assert "发现 1 个循环依赖" in c2.stdout

    s = _cli("stats", "--graph", str(graph_path), "--output", "json")
    stats = json.loads(s.stdout)
    assert stats["node_count"] == 34
    assert stats["edge_count"] == 41
    assert stats["max_depth"] == 7


def test_cli_viz_mermaid_and_html(tmp_path: Path) -> None:
    graph_path = tmp_path / "graph.json"
    _cli("scan", str(WAREHOUSE), "--graph-out", str(graph_path), "--quiet")

    mmd = tmp_path / "lineage.mmd"
    m = _cli("viz", str(graph_path), "--format", "mermaid", "--out", str(mmd))
    assert m.returncode == 0, m.stderr
    text = mmd.read_text(encoding="utf-8")
    assert text.startswith("flowchart LR")
    assert 'n0["src.' in text
    assert 'subgraph sg_ods' in text
    assert "-->" in text
    assert "ads.ads_经营指标驾驶舱" in text

    html_path = tmp_path / "lineage.html"
    h = _cli("viz", str(graph_path), "--format", "html", "--out", str(html_path),
             "--highlight", "ads.ads_经营指标驾驶舱")
    assert h.returncode == 0, h.stderr
    html = html_path.read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html
    assert html.strip().endswith("</html>")
    assert '"ads.ads_经营指标驾驶舱"' in html

    bad = _cli("viz", str(graph_path), "--format", "mermaid", "--highlight", "没有这张表")
    assert bad.returncode == 2
    assert "找不到表" in bad.stderr


def test_cli_graph_file_missing_gives_hint(tmp_path: Path) -> None:
    proc = _cli("stats", "--graph", str(tmp_path / "nope.json"))
    assert proc.returncode == 2
    assert "血缘图文件不存在" in proc.stderr
    assert "scan" in proc.stderr


def test_cli_legacy_mode_still_works() -> None:
    """P2 改动不能破坏 P1 的旧用法（文件路径直接解析）。"""
    proc = _cli(str(PROJECT_ROOT / "examples" / "01_ods_to_dwd_ctas.sql"), "--no-color")
    assert proc.returncode == 0, proc.stderr
    assert "SQL 血缘解析报告" in proc.stdout
