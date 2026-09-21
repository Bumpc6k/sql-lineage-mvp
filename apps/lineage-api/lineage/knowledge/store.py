"""知识库存储层：SQLite（标准库 ``sqlite3``，零额外依赖）。

设计要点
--------
* 库文件默认 ``data/knowledge.db``（可用 ``--db`` 或环境变量 ``KB_DB`` 覆盖）；
* 8 张业务表：脚本档案 / 表 / 字段 / 指标口径 / 业务规则 / 业务术语 / 表级血缘 / 元信息；
* **幂等 rebuild**：``rebuild()`` 先清空再全量写入，同样输入必然得到同样的库内容
  （可用 :meth:`KnowledgeStore.knowledge_hash` 取指纹对比）；
* **增量 upsert**：``upsert()`` 按「来源脚本」粒度替换（脚本级幂等），
  并清理只由被替换脚本贡献、且本次扫描后不再存在的孤立记录；
* 字段 / 表 / 术语跨脚本合并（``source_files`` 取并集，中文名取置信度更高的）；
* 导出 JSON 供外部消费。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from lineage.knowledge.extractor import ExtractionResult

__all__ = ["KnowledgeStore", "SCHEMA_VERSION", "DEFAULT_DB_PATH", "default_db_path"]

#: 库结构版本（结构变化时 +1）
SCHEMA_VERSION = "1.0.0"


def default_db_path() -> Path:
    """默认库路径：``<项目根>/data/knowledge.db``，可用环境变量 ``KB_DB`` 覆盖。"""
    custom = os.environ.get("KB_DB")
    if custom:
        return Path(custom).expanduser()
    return Path(__file__).resolve().parents[4] / "data" / "knowledge.db"


DEFAULT_DB_PATH = default_db_path()

#: 业务表清单（用于清空 / 导出 / 指纹）
BIZ_TABLES = (
    "kb_scripts",
    "kb_tables",
    "kb_fields",
    "kb_metrics",
    "kb_rules",
    "kb_terms",
    "kb_table_lineage",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS kb_scripts (
    source_file     TEXT PRIMARY KEY,
    header_comment  TEXT,
    statement_count INTEGER,
    layers          TEXT,
    output_tables   TEXT,
    input_tables    TEXT,
    task_types      TEXT
);
CREATE TABLE IF NOT EXISTS kb_tables (
    table_name     TEXT PRIMARY KEY,
    short_name     TEXT,
    layer          TEXT,
    chinese_name   TEXT,
    chinese_source TEXT,
    business_desc  TEXT,
    column_count   INTEGER,
    is_root        INTEGER,
    is_leaf        INTEGER,
    source_files   TEXT
);
CREATE TABLE IF NOT EXISTS kb_fields (
    table_name       TEXT,
    column_name      TEXT,
    chinese_name     TEXT,
    chinese_source   TEXT,
    confidence       REAL,
    unit             TEXT,
    layer            TEXT,
    role             TEXT,
    business_desc    TEXT,
    sample_expression TEXT,
    source_files     TEXT,
    PRIMARY KEY (table_name, column_name)
);
CREATE TABLE IF NOT EXISTS kb_metrics (
    metric_name         TEXT,
    table_name          TEXT,
    chinese_name        TEXT,
    chinese_source      TEXT,
    layer               TEXT,
    metric_type         TEXT,
    aggregate_func      TEXT,
    functions           TEXT,
    expression_raw      TEXT,
    expression_normalized TEXT,
    formula             TEXT,
    formula_full        TEXT,
    depends_on          TEXT,
    source_file         TEXT,
    source_stmt         INTEGER,
    source_task_type    TEXT,
    input_tables        TEXT,
    owner               TEXT,
    version             TEXT,
    confidence          REAL,
    notes               TEXT,
    unit                TEXT,
    PRIMARY KEY (metric_name, table_name, source_file)
);
CREATE TABLE IF NOT EXISTS kb_rules (
    rule_key       TEXT PRIMARY KEY,
    rule_type      TEXT,
    description    TEXT,
    expression     TEXT,
    table_name     TEXT,
    layer          TEXT,
    source_file    TEXT,
    source_stmt    INTEGER,
    source_comment TEXT
);
CREATE TABLE IF NOT EXISTS kb_terms (
    term        TEXT PRIMARY KEY,
    chinese_name TEXT,
    category    TEXT,
    source      TEXT,
    confidence  REAL,
    domain      TEXT,
    aliases     TEXT,
    tables      TEXT,
    occurrences INTEGER
);
CREATE TABLE IF NOT EXISTS kb_table_lineage (
    source_table    TEXT,
    target_table    TEXT,
    source_files    TEXT,
    column_mappings INTEGER,
    PRIMARY KEY (source_table, target_table)
);
CREATE INDEX IF NOT EXISTS idx_metrics_table ON kb_metrics(table_name);
CREATE INDEX IF NOT EXISTS idx_fields_column ON kb_fields(column_name);
CREATE INDEX IF NOT EXISTS idx_terms_chinese ON kb_terms(chinese_name);
CREATE INDEX IF NOT EXISTS idx_rules_file ON kb_rules(source_file);
"""

#: 存储为 JSON 文本的列
_JSON_COLS = {
    "functions", "depends_on", "input_tables", "source_files", "aliases", "tables",
    "layers", "output_tables", "task_types",
}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


class KnowledgeStore:
    """业务口径知识库（SQLite）。"""

    def __init__(self, path: Any = None, create: bool = True) -> None:
        self.path = Path(path) if path else default_db_path()
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    # ------------------------------------------------------------------ #
    # 结构
    # ------------------------------------------------------------------ #
    def init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(_SCHEMA)
            self._set_meta("schema_version", SCHEMA_VERSION)

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kb_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def meta(self) -> Dict[str, str]:
        rows = self.conn.execute("SELECT key, value FROM kb_meta").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    def __enter__(self) -> "KnowledgeStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        """清空所有业务表（保留 kb_meta）。"""
        with self.conn:
            for table in BIZ_TABLES:
                self.conn.execute(f"DELETE FROM {table}")

    def rebuild(self, result: ExtractionResult, db_label: str = "") -> Dict[str, Any]:
        """幂等全量重建：先清空，再写入本次提炼结果。"""
        self.clear()
        counts = self._write(result, incremental=False)
        with self.conn:
            self._set_meta("built_at", _now_iso())
            self._set_meta("db_label", db_label or "")
            self._set_meta("last_mode", "rebuild")
        return counts

    def upsert(self, result: ExtractionResult, db_label: str = "") -> Dict[str, Any]:
        """增量 upsert：按来源脚本粒度替换 + 清理孤立记录。"""
        counts = self._write(result, incremental=True)
        with self.conn:
            self._set_meta("built_at", _now_iso())
            self._set_meta("db_label", db_label or "")
            self._set_meta("last_mode", "incremental")
        return counts

    def _write(self, result: ExtractionResult, incremental: bool) -> Dict[str, Any]:
        scanned = set(result.files)
        new_field_keys = {(f.table_name, f.column_name) for f in result.fields}
        new_table_keys = {t.table_name for t in result.tables}
        new_term_keys = {t.term for t in result.terms}

        with self.conn:
            if incremental and scanned:
                qmarks = ",".join("?" for _ in scanned)
                files = tuple(sorted(scanned))
                self.conn.execute(f"DELETE FROM kb_metrics WHERE source_file IN ({qmarks})", files)
                self.conn.execute(f"DELETE FROM kb_rules WHERE source_file IN ({qmarks})", files)
                self.conn.execute(f"DELETE FROM kb_scripts WHERE source_file IN ({qmarks})", files)
                self._prune_orphans(scanned, new_field_keys, new_table_keys, new_term_keys)
            elif not incremental:
                self.conn.execute("DELETE FROM kb_metrics")
                self.conn.execute("DELETE FROM kb_rules")
                self.conn.execute("DELETE FROM kb_scripts")

            for row in (t.to_row() for t in result.tables):
                self._merge_table(row)
            for row in (f.to_row() for f in result.fields):
                self._merge_field(row)
            for row in (m.to_row() for m in result.metrics):
                self._replace_row("kb_metrics", row, ("metric_name", "table_name", "source_file"))
            for row in (r.to_row() for r in result.rules):
                self._replace_row("kb_rules", row, ("rule_key",))
            for row in (t.to_row() for t in result.terms):
                self._merge_term(row)
            for row in (s.to_row() for s in result.scripts):
                self._replace_row("kb_scripts", row, ("source_file",))
            for edge in result.edges:
                self._merge_edge(edge)

        return self.counts()

    # -- 合并 / 替换 ------------------------------------------------------- #
    def _merge_table(self, row: Dict[str, Any]) -> None:
        existing = self.conn.execute(
            "SELECT * FROM kb_tables WHERE table_name = ?", (row["table_name"],)
        ).fetchone()
        if existing:
            files = sorted(set(_load(existing["source_files"], [])) | set(row["source_files"]))
            chinese = row["chinese_name"] or existing["chinese_name"]
            source = row["chinese_source"] if row["chinese_name"] else existing["chinese_source"]
            desc = row["business_desc"] or existing["business_desc"]
            row = {**dict(existing), **row, "source_files": files,
                   "chinese_name": chinese, "chinese_source": source, "business_desc": desc}
        self._replace_row("kb_tables", row, ("table_name",))

    def _merge_field(self, row: Dict[str, Any]) -> None:
        existing = self.conn.execute(
            "SELECT * FROM kb_fields WHERE table_name = ? AND column_name = ?",
            (row["table_name"], row["column_name"]),
        ).fetchone()
        if existing:
            files = sorted(set(_load(existing["source_files"], [])) | set(row["source_files"]))
            old_conf = float(existing["confidence"] or 0)
            take_new = bool(row["chinese_name"]) and (
                float(row["confidence"] or 0) > old_conf or not existing["chinese_name"]
            )
            row = {
                **dict(existing),
                **row,
                "source_files": files,
                "chinese_name": row["chinese_name"] if take_new else existing["chinese_name"],
                "chinese_source": row["chinese_source"] if take_new else existing["chinese_source"],
                "confidence": max(float(row["confidence"] or 0), old_conf),
                "unit": row["unit"] or existing["unit"],
                "business_desc": row["business_desc"] or existing["business_desc"],
                "sample_expression": row["sample_expression"] or existing["sample_expression"],
                "role": existing["role"] if existing["role"] != row["role"] else row["role"],
            }
        self._replace_row("kb_fields", row, ("table_name", "column_name"))

    def _merge_term(self, row: Dict[str, Any]) -> None:
        existing = self.conn.execute("SELECT * FROM kb_terms WHERE term = ?", (row["term"],)).fetchone()
        if existing:
            old_conf = float(existing["confidence"] or 0)
            take_new = bool(row["chinese_name"]) and (
                float(row["confidence"] or 0) > old_conf or not existing["chinese_name"]
            )
            row = {
                **dict(existing),
                **row,
                "chinese_name": row["chinese_name"] if take_new else existing["chinese_name"],
                "source": row["source"] if take_new else existing["source"],
                "confidence": max(float(row["confidence"] or 0), old_conf),
                "aliases": sorted(set(_load(existing["aliases"], [])) | set(row["aliases"])),
                "tables": sorted(set(_load(existing["tables"], [])) | set(row["tables"])),
                "occurrences": max(int(existing["occurrences"] or 0), int(row["occurrences"] or 0)),
            }
        self._replace_row("kb_terms", row, ("term",))

    def _merge_edge(self, edge: Dict[str, Any]) -> None:
        existing = self.conn.execute(
            "SELECT * FROM kb_table_lineage WHERE source_table = ? AND target_table = ?",
            (edge["source_table"], edge["target_table"]),
        ).fetchone()
        row = {
            "source_table": edge["source_table"],
            "target_table": edge["target_table"],
            "source_files": sorted(set(_load(existing["source_files"], []) if existing else [])
                                   | set(edge.get("source_files") or [])),
            "column_mappings": max(int(existing["column_mappings"] or 0) if existing else 0,
                                   int(edge.get("column_mappings") or 0)),
        }
        self._replace_row("kb_table_lineage", row, ("source_table", "target_table"))

    def _replace_row(self, table: str, row: Dict[str, Any], pk: Sequence[str]) -> None:
        cols = list(row.keys())
        values = [_dump(row[c]) if c in _JSON_COLS and not isinstance(row[c], str) else row[c]
                  for c in cols]
        placeholders = ", ".join("?" for _ in cols)
        sql = (
            f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        )
        self.conn.execute(sql, values)

    def _prune_orphans(
        self,
        scanned: set,
        new_field_keys: set,
        new_table_keys: set,
        new_term_keys: set,
    ) -> None:
        """删除「来源脚本都在本次扫描范围内、但本次没再产出」的孤立记录。"""
        for table, col, new_keys in (
            ("kb_fields", ("table_name", "column_name"), new_field_keys),
            ("kb_tables", ("table_name",), new_table_keys),
        ):
            for record in self.conn.execute(f"SELECT * FROM {table}").fetchall():
                files = set(_load(record["source_files"], []))
                if files and files <= scanned:
                    key = tuple(record[c] for c in col)
                    if key not in new_keys:
                        where = " AND ".join(f"{c} = ?" for c in col)
                        self.conn.execute(f"DELETE FROM {table} WHERE {where}", key)
        for record in self.conn.execute("SELECT * FROM kb_terms").fetchall():
            tables = set(_load(record["tables"], []))
            table_files = set()
            for name in tables:
                tbl = self.conn.execute(
                    "SELECT source_files FROM kb_tables WHERE table_name = ?", (name,)
                ).fetchone()
                if tbl:
                    table_files |= set(_load(tbl["source_files"], []))
            if table_files and table_files <= scanned and record["term"] not in new_term_keys \
                    and record["source"] not in ("builtin",):
                self.conn.execute("DELETE FROM kb_terms WHERE term = ?", (record["term"],))

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def _rows(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for record in self.conn.execute(sql, tuple(params)).fetchall():
            item = dict(record)
            for col in _JSON_COLS:
                if col in item:
                    default = 0 if col == "column_mappings" else []
                    item[col] = _load(item[col], default)
            out.append(item)
        return out

    def metrics(self, table: Optional[str] = None) -> List[Dict[str, Any]]:
        if table:
            return self._rows("SELECT * FROM kb_metrics WHERE table_name = ? ORDER BY metric_name", (table,))
        return self._rows("SELECT * FROM kb_metrics ORDER BY table_name, metric_name")

    def fields(self, table: Optional[str] = None) -> List[Dict[str, Any]]:
        if table:
            return self._rows("SELECT * FROM kb_fields WHERE table_name = ? ORDER BY column_name", (table,))
        return self._rows("SELECT * FROM kb_fields ORDER BY table_name, column_name")

    def tables(self) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM kb_tables ORDER BY table_name")

    def rules(self) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM kb_rules ORDER BY source_file, rule_type")

    def terms(self, only_pending: bool = False) -> List[Dict[str, Any]]:
        if only_pending:
            return self._rows("SELECT * FROM kb_terms WHERE source = 'pending' ORDER BY term")
        return self._rows("SELECT * FROM kb_terms ORDER BY term")

    def scripts(self) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM kb_scripts ORDER BY source_file")

    def edges(self) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM kb_table_lineage ORDER BY source_table, target_table")

    def get_metric(self, name: str) -> List[Dict[str, Any]]:
        """按指标名 / 中文名精确查口径（大小写不敏感）。"""
        key = (name or "").strip().lower()
        if not key:
            return []
        rows = self._rows(
            "SELECT * FROM kb_metrics WHERE lower(metric_name) = ? OR lower(chinese_name) = ? "
            "ORDER BY table_name",
            (key, key),
        )
        if rows:
            return rows
        return self._rows(
            "SELECT * FROM kb_metrics WHERE lower(metric_name) LIKE ? OR lower(chinese_name) LIKE ? "
            "ORDER BY table_name LIMIT 50",
            (f"%{key}%", f"%{key}%"),
        )

    def get_table(self, name: str) -> Optional[Dict[str, Any]]:
        key = (name or "").strip().lower()
        if not key:
            return None
        rows = self._rows("SELECT * FROM kb_tables WHERE lower(table_name) = ?", (key,))
        if rows:
            return rows[0]
        rows = self._rows(
            "SELECT * FROM kb_tables WHERE lower(table_name) LIKE ? ORDER BY length(table_name) LIMIT 1",
            (f"%{key}%",),
        )
        return rows[0] if rows else None

    # -- 血缘路径 ---------------------------------------------------------- #
    def upstream_paths(self, table: str, depth: int = 8, max_paths: int = 5) -> List[List[str]]:
        """基于库内表级血缘向上溯源（BFS 枚举链路）。"""
        return self._walk(table, up=True, depth=depth, max_paths=max_paths)

    def downstream_paths(self, table: str, depth: int = 8, max_paths: int = 5) -> List[List[str]]:
        """基于库内表级血缘向下追踪。"""
        return self._walk(table, up=False, depth=depth, max_paths=max_paths)

    def _walk(self, table: str, up: bool, depth: int, max_paths: int) -> List[List[str]]:
        edges = self.edges()
        adj: Dict[str, List[str]] = {}
        for e in edges:
            key, val = (e["target_table"], e["source_table"]) if up else (e["source_table"], e["target_table"])
            adj.setdefault(key, []).append(val)
        target = self.resolve_table_name(table)
        paths: List[List[str]] = []
        stack: List[Tuple[str, List[str]]] = [(target, [target])]
        while stack and len(paths) < max_paths * 4:
            node, path = stack.pop(0)
            parents = adj.get(node) or []
            extended = False
            for parent in parents:
                if parent in path:
                    continue
                if len(path) < depth:
                    stack.append((parent, path + [parent]))
                    extended = True
            if not extended:
                paths.append(path)
        paths.sort(key=lambda p: (len(p), p))
        return paths[:max_paths]

    def resolve_table_name(self, table: str) -> str:
        """支持只写表名不带库名。"""
        key = (table or "").strip()
        if not key:
            return key
        row = self.conn.execute(
            "SELECT table_name FROM kb_tables WHERE lower(table_name) = ?", (key.lower(),)
        ).fetchone()
        if row:
            return row["table_name"]
        row = self.conn.execute(
            "SELECT table_name FROM kb_tables WHERE table_name LIKE ? "
            "ORDER BY length(table_name) LIMIT 1",
            (f"%.{key}",),
        ).fetchone()
        if row:
            return row["table_name"]
        row = self.conn.execute(
            "SELECT table_name FROM kb_tables WHERE table_name LIKE ? "
            "ORDER BY length(table_name) LIMIT 1",
            (f"%{key}%",),
        ).fetchone()
        return row["table_name"] if row else key

    # ------------------------------------------------------------------ #
    # 统计 / 导出 / 指纹
    # ------------------------------------------------------------------ #
    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for table in BIZ_TABLES:
            row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            out[table] = int(row["n"])
        row = self.conn.execute("SELECT COUNT(*) AS n FROM kb_terms WHERE source = 'pending'").fetchone()
        out["pending_terms"] = int(row["n"])
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM kb_fields WHERE chinese_name IS NOT NULL AND chinese_name <> ''"
        ).fetchone()
        out["fields_with_chinese"] = int(row["n"])
        return out

    def summary(self, limit_tables: int = 25) -> Dict[str, Any]:
        """知识库概览（HTTP ``/kb/summary`` 与 CLI ``kb summary`` 共用）。"""
        counts = self.counts()
        meta = self.meta()
        type_rows = self.conn.execute(
            "SELECT metric_type AS t, COUNT(*) AS n FROM kb_metrics GROUP BY metric_type ORDER BY n DESC"
        ).fetchall()
        layer_rows = self.conn.execute(
            "SELECT layer AS l, COUNT(*) AS n FROM kb_metrics GROUP BY layer ORDER BY l"
        ).fetchall()
        term_rows = self.conn.execute(
            "SELECT source AS s, COUNT(*) AS n FROM kb_terms GROUP BY source ORDER BY n DESC"
        ).fetchall()
        top_tables = self.conn.execute(
            "SELECT t.table_name AS name, t.chinese_name AS chinese, t.layer AS layer, "
            "COUNT(m.metric_name) AS metric_count FROM kb_tables t "
            "LEFT JOIN kb_metrics m ON m.table_name = t.table_name "
            "GROUP BY t.table_name ORDER BY metric_count DESC, t.table_name LIMIT ?",
            (limit_tables,),
        ).fetchall()
        return {
            "db": str(self.path),
            "schema_version": meta.get("schema_version", SCHEMA_VERSION),
            "built_at": meta.get("built_at", ""),
            "counts": counts,
            "metric_by_type": {r["t"]: r["n"] for r in type_rows},
            "metric_by_layer": {r["l"] or "?": r["n"] for r in layer_rows},
            "term_by_source": {r["s"]: r["n"] for r in term_rows},
            "top_tables": [dict(r) for r in top_tables],
        }

    def export_json(self) -> Dict[str, Any]:
        """导出整库 JSON（供外部系统 / 前端消费）。"""
        return {
            "meta": self.meta(),
            "summary": self.summary(),
            "scripts": self.scripts(),
            "tables": self.tables(),
            "fields": self.fields(),
            "metrics": self.metrics(),
            "rules": self.rules(),
            "terms": self.terms(),
            "table_lineage": self.edges(),
        }

    def knowledge_hash(self) -> str:
        """业务内容指纹（不含时间戳 / 库路径 / 物理行序），用于验证 rebuild 幂等。"""
        digest = hashlib.sha256()
        for table in BIZ_TABLES:
            digest.update(table.encode("utf-8"))
            rows = sorted(_dump(row) for row in self._rows(f"SELECT * FROM {table}"))
            for row in rows:
                digest.update(row.encode("utf-8"))
        return digest.hexdigest()[:16]


def _now_iso() -> str:
    from datetime import datetime, timezone, timedelta

    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S%z")
