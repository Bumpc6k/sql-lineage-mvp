"""目录批量扫描（P2）：把一个数仓 SQL 脚本目录整体解析成全局血缘图。

干什么
------
1. 递归扫描目录下所有 ``.sql`` 文件（默认忽略 ``__pycache__`` / ``.venv`` / ``.git`` /
   ``node_modules`` 等无关目录）；
2. 逐个文件交给 :class:`lineage.parser.SqlLineageParser` 解析；
3. 把所有语句汇总进一张全局血缘图（同一张表在多个文件里被加工会自动合并，
   跨文件的血缘链条自然贯通：a.sql 产出 dwd.x、b.sql 读 dwd.x 写 dws.y）；
4. 产出扫描报告：文件数 / 语句数 / 表数 / 血缘边数 / 解析失败清单。

用法::

    from lineage.scan import scan_directory

    result = scan_directory("examples/warehouse", dialect="hive")
    print(result.report_text())
    result.graph.save("warehouse_graph.json")
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .graph import LineageGraph, LAYERS
from .parser import DEFAULT_DIALECT, SqlLineageParser

__all__ = ["DEFAULT_IGNORE_DIRS", "ScanResult", "discover_sql_files", "scan_directory"]

#: 默认忽略的目录名（不区分大小写）
DEFAULT_IGNORE_DIRS: Set[str] = {
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
    "target",
    "build",
    "dist",
    "site-packages",
    ".ipynb_checkpoints",
}

#: 默认忽略的文件名
DEFAULT_IGNORE_FILES: Set[str] = {"__init__.py"}


def discover_sql_files(
    root: Path,
    ignore_dirs: Optional[Iterable[str]] = None,
    suffix: str = ".sql",
) -> Tuple[List[Path], List[str]]:
    """递归收集 ``.sql`` 文件。

    返回 ``(sql 文件列表（已排序）, 被忽略的目录列表)``。
    """
    ignore = {d.lower() for d in (ignore_dirs if ignore_dirs is not None else DEFAULT_IGNORE_DIRS)}
    found: List[Path] = []
    skipped: List[str] = []

    stack: List[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except (PermissionError, OSError):
            skipped.append(str(current))
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name.lower() in ignore or entry.name.startswith("."):
                    skipped.append(str(entry))
                    continue
                stack.append(entry)
            elif entry.is_file() and entry.suffix.lower() == suffix:
                if entry.name in DEFAULT_IGNORE_FILES:
                    continue
                found.append(entry)
    found.sort(key=lambda p: str(p))
    skipped.sort()
    return found, skipped


@dataclass
class ScanResult:
    """目录扫描结果。"""

    root: str
    dialect: str = DEFAULT_DIALECT
    graph: LineageGraph = field(default_factory=LineageGraph)
    statements: List[Dict[str, Any]] = field(default_factory=list)
    #: 相对路径（相对扫描根目录），便于报告里显示
    files: List[str] = field(default_factory=list)
    #: 解析失败清单：[{file, error}]
    failures: List[Dict[str, str]] = field(default_factory=list)
    #: 文件存在但没有解析出语句（空文件 / 全是注释）
    empty_files: List[str] = field(default_factory=list)
    #: 被忽略的目录（用于报告透明化）
    ignored_dirs: List[str] = field(default_factory=list)
    #: 语句数 > 1 的文件（多语句文件）
    multi_statement_files: List[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    # -- 汇总数字 -------------------------------------------------------- #
    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def statement_count(self) -> int:
        return len(self.statements)

    @property
    def table_count(self) -> int:
        return len(self.graph.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.graph.edges)

    @property
    def no_output_statement_count(self) -> int:
        """没有输出表的语句数（纯查询 SQL，只贡献节点不贡献边）。"""
        return sum(1 for s in self.statements if not s.get("output_table_names"))

    def summary_dict(self) -> Dict[str, Any]:
        stats = self.graph.stats()
        return {
            "root": self.root,
            "dialect": self.dialect,
            "file_count": self.file_count,
            "statement_count": self.statement_count,
            "table_count": self.table_count,
            "edge_count": self.edge_count,
            "column_mapping_count": stats["column_mapping_count"],
            "max_depth": stats["max_depth"],
            "layer_counts": stats["layer_counts"],
            "root_table_count": stats["root_count"],
            "leaf_table_count": stats["leaf_count"],
            "isolated_table_count": stats["isolated_count"],
            "cycle_count": stats["cycle_count"],
            "parse_failure_count": len(self.failures),
            "empty_file_count": len(self.empty_files),
            "no_output_statement_count": self.no_output_statement_count,
            "multi_statement_file_count": len(self.multi_statement_files),
            "ignored_dir_count": len(self.ignored_dirs),
            "elapsed_seconds": self.elapsed_seconds,
        }

    def to_dict(self, with_graph: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "summary": self.summary_dict(),
            "files": self.files,
            "multi_statement_files": self.multi_statement_files,
            "empty_files": self.empty_files,
            "failures": self.failures,
            "ignored_dirs": self.ignored_dirs,
        }
        if with_graph:
            data["graph"] = self.graph.to_dict()
        return data

    def report_text(self, max_list: int = 20) -> str:
        """终端可读的扫描报告（中文）。"""
        s = self.summary_dict()
        lines = ["=" * 72]
        lines.append(f"数仓脚本目录扫描报告  dialect={s['dialect']}")
        lines.append("=" * 72)
        lines.append(f"扫描根目录：{s['root']}")
        lines.append(
            f"SQL 文件数：{s['file_count']}    语句数：{s['statement_count']}"
            f"（多语句文件 {s['multi_statement_file_count']} 个，无输出表语句 {s['no_output_statement_count']} 条）"
        )
        lines.append(
            f"表（节点）数：{s['table_count']}    血缘边数：{s['edge_count']}"
            f"    字段级映射：{s['column_mapping_count']} 条"
        )
        lines.append(
            f"源表 {s['root_table_count']} 张 / 叶子表 {s['leaf_table_count']} 张 / "
            f"孤立表 {s['isolated_table_count']} 张 / 最大血缘深度 {s['max_depth']} 层"
        )
        layers = s.get("layer_counts") or {}
        order = [l for l in LAYERS if l in layers]
        lines.append("分层分布：" + "  ".join(f"{l}={layers[l]}" for l in order))
        lines.append(f"循环依赖：{'有 ' + str(s['cycle_count']) + ' 个 !!' if s['cycle_count'] else '无 ✔'}")
        lines.append(f"解析失败：{s['parse_failure_count']} 个文件")
        if self.failures:
            for f in self.failures[:max_list]:
                lines.append(f"  !! {f['file']}: {f.get('error')}")
        if self.empty_files:
            lines.append(f"空文件（无语句）：{len(self.empty_files)} 个 -> "
                         + ", ".join(self.empty_files[:max_list]))
        lines.append(f"忽略目录：{s['ignored_dir_count']} 个（__pycache__ / .venv / .git 等）")

        lines.append("-" * 72)
        lines.append(f"扫描到的文件（{self.file_count} 个）：")
        for f in self.files[:max_list]:
            lines.append(f"  - {f}")
        if len(self.files) > max_list:
            lines.append(f"  ...（其余 {len(self.files) - max_list} 个省略）")

        lines.append("-" * 72)
        lines.append("全局血缘边（上游 -> 下游）：")
        edges = sorted(self.graph.edges.values(), key=lambda e: (e.source, e.target))
        for e in edges[:max_list]:
            lines.append(
                f"  {e.source}  ->  {e.target}"
                f"    [字段映射 {e.column_mappings} 条"
                + (f"，未解析 {e.unresolved_mappings} 条" if e.unresolved_mappings else "")
                + f"，来源 {len(e.files)} 个文件]"
            )
        if len(edges) > max_list:
            lines.append(f"  ...（其余 {len(edges) - max_list} 条省略，完整图见 graph JSON）")

        lines.append("=" * 72)
        lines.append(f"耗时 {s['elapsed_seconds']} 秒")
        lines.append("=" * 72)
        return "\n".join(lines)


def scan_directory(
    root: Any,
    dialect: str = DEFAULT_DIALECT,
    ignore_dirs: Optional[Iterable[str]] = None,
    max_files: Optional[int] = None,
) -> ScanResult:
    """递归扫描目录下的 ``.sql`` 文件并汇总成全局血缘图。

    参数：
        root: 目录（支持 str / Path）。
        dialect: sqlglot 方言名，数仓默认 ``hive``。
        ignore_dirs: 额外忽略的目录名（会与 :data:`DEFAULT_IGNORE_DIRS` 合并）。
        max_files: 只扫描前 N 个文件（默认全部）。

    单个文件解析失败不会中断整体扫描，会记进 ``result.failures``。
    """
    start = time.perf_counter()
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists():
        raise FileNotFoundError(f"目录不存在：{root_path}")
    if not root_path.is_dir():
        raise NotADirectoryError(f"不是目录：{root_path}")

    ignore = set(DEFAULT_IGNORE_DIRS)
    if ignore_dirs:
        ignore |= {d.lower() for d in ignore_dirs}

    files, ignored = discover_sql_files(root_path, ignore)
    if max_files is not None:
        files = files[:max_files]

    parser = SqlLineageParser(dialect=dialect)
    statements: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    empty_files: List[str] = []
    multi_files: List[str] = []
    rel_files: List[str] = []

    for path in files:
        rel = str(path.relative_to(root_path)) if path.is_relative_to(root_path) else str(path)
        rel_files.append(rel)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:  # pragma: no cover - 文件系统异常
                failures.append({"file": rel, "error": f"读取失败：{exc}"})
                continue
        except OSError as exc:  # pragma: no cover - 文件系统异常
            failures.append({"file": rel, "error": f"读取失败：{exc}"})
            continue

        if not text.strip():
            empty_files.append(rel)
            continue
        try:
            parsed = parser.parse_sql(text, source=rel)
        except Exception as exc:  # noqa: BLE001 - 单文件失败不影响整体扫描
            failures.append({"file": rel, "error": f"{type(exc).__name__}: {exc}"})
            continue

        if not parsed:
            empty_files.append(rel)
            continue
        if len(parsed) > 1:
            multi_files.append(rel)
        statements.extend(parsed)

    graph = LineageGraph(dialect=dialect, root=str(root_path))
    for stmt in statements:
        graph.add_statement(stmt)
    graph.failures = failures
    graph.scan_meta = {"root": str(root_path), "scanned_files": rel_files}

    result = ScanResult(
        root=str(root_path),
        dialect=dialect,
        graph=graph,
        statements=statements,
        files=rel_files,
        failures=failures,
        empty_files=empty_files,
        ignored_dirs=ignored,
        multi_statement_files=multi_files,
        elapsed_seconds=round(time.perf_counter() - start, 3),
    )
    return result
