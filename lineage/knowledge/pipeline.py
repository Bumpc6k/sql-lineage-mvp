"""建库流水线：扫描 SQL 目录 → 提炼口径 → 写入 SQLite → （可选）导出 Markdown。

一条命令搞定：
``python -m lineage.cli kb build examples/warehouse``

流程：
1. 用 P2 的 :func:`lineage.scan.scan_directory` 递归扫描目录，拿到血缘图 + 语句；
2. 逐个文件读回 SQL 原文，挖注释（文件头 / 行内 / 表级 COMMENT）；
3. :class:`~lineage.knowledge.extractor.KnowledgeExtractor` 两阶段提炼（先登记全量
   字段建立中文名映射，再提炼口径 / 规则 / 术语）；
4. 写入 SQLite（``rebuild`` 全量幂等重建 / ``upsert`` 脚本级增量）；
5. 可选导出 Markdown 知识文档。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..graph import LineageGraph
from ..scan import scan_directory
from .comments import parse_sql_comments
from .extractor import ExtractionResult, KnowledgeExtractor
from .store import KnowledgeStore, default_db_path
from .textutil import Glossary

__all__ = [
    "BuildReport",
    "build_knowledge_base",
    "format_build_text",
    "DEFAULT_SCAN_DIRS",
    "resolve_dirs",
    "project_root",
]

#: 默认扫描目录（相对项目根目录）：真实示例数仓 + 码段产量口径演示
DEFAULT_SCAN_DIRS: Tuple[str, ...] = ("examples/warehouse", "examples/knowledge_demo")


def project_root() -> Path:
    """项目根目录（``lineage/knowledge/pipeline.py`` 往上三级）。"""
    return Path(__file__).resolve().parents[2]


def resolve_dirs(dirs: Optional[Sequence[str]] = None) -> List[Path]:
    """把目录参数解析成真实路径：先按当前工作目录找，再按项目根目录找。"""
    names = list(dirs) if dirs else list(DEFAULT_SCAN_DIRS)
    root = project_root()
    out: List[Path] = []
    for name in names:
        p = Path(name).expanduser()
        if p.exists():
            out.append(p.resolve())
            continue
        candidate = (root / name).resolve()
        if candidate.exists():
            out.append(candidate)
            continue
        raise FileNotFoundError(f"目录不存在：{name}（也试过 {candidate}）")
    return out


@dataclass
class BuildReport:
    """建库报告。"""

    db_path: str = ""
    mode: str = "rebuild"
    dirs: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    statement_count: int = 0
    counts: Dict[str, int] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)
    knowledge_hash: str = ""
    glossary_version: str = ""
    glossary_size: int = 0
    doc_path: str = ""
    doc_bytes: int = 0
    failures: List[Dict[str, str]] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "db_path": self.db_path,
            "mode": self.mode,
            "dirs": self.dirs,
            "file_count": len(self.files),
            "files": self.files,
            "statement_count": self.statement_count,
            "counts": self.counts,
            "stats": self.stats,
            "knowledge_hash": self.knowledge_hash,
            "glossary": {"version": self.glossary_version, "size": self.glossary_size},
            "doc": {"path": self.doc_path, "bytes": self.doc_bytes} if self.doc_path else {},
            "failures": self.failures,
            "elapsed_seconds": self.elapsed_seconds,
        }


def _label_for(path: Path, root: Path) -> str:
    """统一用「相对项目根」的路径做脚本标识，跨目录扫描也不会重名。"""
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path.resolve())


def build_knowledge_base(
    dirs: Optional[Sequence[str]] = None,
    db_path: Optional[Any] = None,
    dialect: str = "hive",
    glossary_path: Optional[Any] = None,
    mode: str = "rebuild",
    doc_path: Optional[Any] = None,
    doc_title: str = "业务口径知识库",
) -> BuildReport:
    """扫描目录 → 提炼业务口径 → 建库（可选导出 Markdown）。"""
    started = time.perf_counter()
    root = project_root()
    targets = resolve_dirs(dirs)

    glossary = Glossary.load_default()
    if glossary_path:
        glossary = glossary.merged_with(Glossary.load(glossary_path))

    extractor = KnowledgeExtractor(glossary=glossary, dialect=dialect)
    graph = LineageGraph(dialect=dialect)
    failures: List[Dict[str, str]] = []
    all_files: List[str] = []
    statement_count = 0

    for target in targets:
        scan = scan_directory(target, dialect=dialect)
        failures.extend(scan.failures)
        by_label: Dict[str, List[Dict[str, Any]]] = {}
        for stmt in scan.statements:
            by_label.setdefault(stmt.get("source") or "", []).append(stmt)
            graph.add_statement(stmt)
        for rel in scan.files:
            path = (target / rel) if not str(rel).startswith("/") else Path(rel)
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - IO 异常
                failures.append({"file": str(rel), "error": f"读取失败：{exc}"})
                continue
            label = _label_for(path, root)
            all_files.append(label)
            statements = by_label.get(str(rel), [])
            if not statements:
                continue
            statement_count += len(statements)
            extractor.add_file(label, text, statements, parse_comments(text))

    extractor.add_graph(graph)
    extraction: ExtractionResult = extractor.finish()
    extraction.source_dirs = [str(t) for t in targets]
    extraction.files = sorted(set(all_files))

    store_path = Path(db_path) if db_path else default_db_path()
    with KnowledgeStore(store_path) as store:
        counts = store.rebuild(extraction) if mode == "rebuild" else store.upsert(extraction)
        store._set_meta("dialect", dialect)
        store._set_meta("dirs", ", ".join(str(t) for t in targets))
        store._set_meta("glossary_version", glossary.version)
        store._set_meta("file_count", str(len(all_files)))
        store._set_meta("statement_count", str(statement_count))
        store.conn.commit()
        knowledge_hash = store.knowledge_hash()
        doc_info: Dict[str, Any] = {}
        if doc_path:
            from .markdown import export_markdown

            doc_info = export_markdown(store, doc_path, title=doc_title)

    stats = extraction.stats()
    stats["file_count"] = len(all_files)
    stats["statement_count"] = statement_count
    stats["glossary_size"] = glossary.size
    return BuildReport(
        db_path=str(store_path.resolve()),
        mode=mode,
        dirs=[str(t) for t in targets],
        files=sorted(set(all_files)),
        statement_count=statement_count,
        counts=counts,
        stats=stats,
        knowledge_hash=knowledge_hash,
        glossary_version=glossary.version,
        glossary_size=glossary.size,
        doc_path=doc_info.get("path", ""),
        doc_bytes=int(doc_info.get("bytes") or 0),
        failures=failures,
        elapsed_seconds=round(time.perf_counter() - started, 3),
    )


def parse_comments(text: str):
    """小包装（便于测试打桩）。"""
    return parse_sql_comments(text)


def format_build_text(report: BuildReport) -> str:
    """终端可读的建库报告。"""
    c = report.counts
    s = report.stats
    lines: List[str] = []
    rule = "=" * 72
    lines.append(rule)
    lines.append(f"业务口径知识库构建报告（{report.mode}）")
    lines.append(rule)
    lines.append(f"扫描目录：{', '.join(report.dirs)}")
    lines.append(f"SQL 文件：{len(report.files)} 个    语句：{report.statement_count} 条    "
                 f"耗时 {report.elapsed_seconds} 秒")
    lines.append(f"内置词典：v{report.glossary_version}（{report.glossary_size} 条词条）")
    lines.append("-" * 72)
    lines.append(f"指标口径（metrics）  : {c.get('kb_metrics', 0)} 条"
                 f"   按类型 " + " ".join(f"{k}={v}" for k, v in (s.get('metric_by_type') or {}).items()))
    lines.append(f"按分层分布           : " + " ".join(
        f"{k}={v}" for k, v in (s.get("metric_by_layer") or {}).items()))
    lines.append(f"字段（fields）       : {c.get('kb_fields', 0)} 个"
                 f"（其中 {c.get('fields_with_chinese', 0)} 个有中文业务名）")
    lines.append(f"表（tables）         : {c.get('kb_tables', 0)} 张")
    lines.append(f"业务术语（terms）    : {c.get('kb_terms', 0)} 条"
                 f"   来源 " + " ".join(f"{k}={v}" for k, v in (s.get("term_by_source") or {}).items()))
    lines.append(f"待确认术语           : {c.get('pending_terms', 0)} 个（需人工补充词典）")
    lines.append(f"业务规则（rules）    : {c.get('kb_rules', 0)} 条")
    lines.append(f"表级血缘（edges）    : {c.get('kb_table_lineage', 0)} 条")
    lines.append(f"脚本档案（scripts）  : {c.get('kb_scripts', 0)} 个")
    lines.append("-" * 72)
    lines.append(f"知识库文件：{report.db_path}")
    lines.append(f"内容指纹  ：{report.knowledge_hash}（同样输入 rebuild 后指纹不变 = 幂等）")
    if report.doc_path:
        lines.append(f"Markdown 文档：{report.doc_path}（{report.doc_bytes / 1024:.1f} KB）")
    if report.failures:
        lines.append(f"解析失败：{len(report.failures)} 个文件")
        for item in report.failures[:10]:
            lines.append(f"  !! {item.get('file')}: {item.get('error')}")
    lines.append(rule)
    lines.append("下一步：kb summary / kb search 产量 / kb show 产量 / kb ask \"产量怎么算的\"")
    lines.append(rule)
    return "\n".join(lines)
