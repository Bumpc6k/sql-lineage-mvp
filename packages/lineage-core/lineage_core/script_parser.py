#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""脚本内嵌 SQL 提取与解析（Shell / Python-PySpark / 纯 SQL）。

为什么要有它
------------
数仓里真正的加工逻辑**只有一半写在 SQL 任务里**。另一半散落在：

* **Shell 脚本**：``hive -e "INSERT OVERWRITE ..."``、``beeline -f etl/xx.sql``、
  ``spark-sql -e "..."``、``hive <<EOF ... EOF``（heredoc）、``SQL="..."`` + ``hive -e "$SQL"``；
* **Python / PySpark 脚本**：``spark.sql( 三引号 SQL )``（含 f-string）、
  ``spark.sql(sql_var)``、``pd.read_sql(...)``、``sqlalchemy`` 的 ``text("...")``、
  模块里当成常量写着的三引号 SQL 段落。

把整段 Shell / Python 脚本**直接丢给 SQL 解析器**，sqlglot 要么报语法错，要么
把 ``echo`` / ``import`` 当成垃圾语句，血缘不可用。本模块先做**内嵌 SQL 提取**
（纯 stdlib 正则 + 手写字符串扫描，不引入任何第三方依赖），再把每一条内嵌 SQL
交给 :class:`lineage.parser.SqlLineageParser` 解析，最后把结果合并成脚本级血缘。

对外 API
--------
* :func:`detect_script_kind` —— 判定脚本是 SQL / Shell / Python（结合海豚任务类型与内容特征）
* :func:`extract_sqls`       —— 从脚本里提取内嵌 SQL（含 ``source_hint`` 定位信息）
* :func:`parse_script`       —— 提取 + 解析 + 合并，返回脚本级血缘结果
* :func:`parse_script_file`  —— 直接解析脚本文件
* :func:`script_summary_text`—— 终端可读的中文摘要（CLI ``parse-script`` 用）

设计原则：**宁少勿假**
----------------------
* 提取不到就返回空列表 + ``unresolved_hints``，绝不把 shell 变量、注释、``echo`` 文本当 SQL；
* ``hive -f etl/x.sql`` 这种「SQL 在外部文件里」的情况，脚本内**没有** SQL 正文，
  除非显式开启 ``resolve_files=True`` 且文件在本地能找到，否则只记一条未解析提示；
* 不确定的一律进 ``unresolved_hints``，不编造血缘。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union as TypingUnion

from lineage_core.parser import DEFAULT_DIALECT, SqlLineageParser

__all__ = [
    "detect_script_kind",
    "extract_sqls",
    "parse_script",
    "parse_script_file",
    "script_summary_text",
    "looks_like_sql",
    "SHELL_SQL_COMMANDS",
]

#: 支持识别内嵌 SQL 的 Shell 命令（前四个是 Hive 生态主力，后三个是常见旁支）
SHELL_SQL_COMMANDS: Tuple[str, ...] = (
    "hive",
    "hivef",
    "beeline",
    "spark-sql",
    "spark_sql",
    "spark3-sql",
    "impala-shell",
    "impala",
    "mysql",
    "psql",
    "clickhouse-client",
)

#: 命令后面跟「内联 SQL 字符串」的参数开关
_INLINE_FLAGS: Tuple[str, ...] = ("-e", "--execute", "-q", "--query", "-c", "--command", "--sql")

#: 命令后面跟「SQL 文件路径」的参数开关
_FILE_FLAGS: Tuple[str, ...] = ("-f", "--file")

#: 合法的命令行起始位置（命令前一个非空白字符）
_CMD_BOUNDARY = set("\n;|&(`${}")

#: SQL 语句开头的关键字（用于「这段文本像不像 SQL」的判断）
_SQL_START_RE = re.compile(
    r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*(SELECT|INSERT|WITH|CREATE|MERGE|UPDATE|DELETE|ALTER|DROP|TRUNCATE|EXPLAIN)\b",
    re.IGNORECASE | re.DOTALL,
)

#: 「整段脚本就是 SQL」的判断：必须**以**一条 SQL 语句开头（不是「包含 SQL」）
_SQL_SCRIPT_START_RE = re.compile(
    r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*"
    r"(?:SET\s+[A-Za-z_]\w*\s*=|SELECT\b|INSERT\b|WITH\b|CREATE\b|MERGE\b|UPDATE\b|"
    r"DELETE\b|ALTER\b|DROP\b|TRUNCATE\b|EXPLAIN\b|USE\b|MSCK\b|ANALYZE\b|REFRESH\b)",
    re.IGNORECASE | re.DOTALL,
)

#: 判断「这段文本里有 SQL」用的结构关键字
_SQL_STRONG_RE = re.compile(
    r"\b(?:INSERT\s+(?:INTO|OVERWRITE)\b|CREATE\s+(?:TABLE|VIEW)\b|"
    r"SELECT\b[\s\S]{0,4000}?\bFROM\b|\bMERGE\s+INTO\b)",
    re.IGNORECASE,
)

#: Python 任务类型的等价写法
_PY_TASK_TYPES: Set[str] = {"PYTHON", "PY", "PYSPARK", "PY_SPARK", "PYTHON3", "PYSQL"}
#: Shell 任务类型的等价写法
_SH_TASK_TYPES: Set[str] = {"SHELL", "SH", "BASH", "SHELL_SCRIPT"}
#: SQL 任务类型的等价写法（海豚里 SQL 任务可再指定 datasource type=HIVE 等）
_SQL_TASK_TYPES: Set[str] = {
    "SQL", "HIVE", "HIVESQL", "HIVE_SQL", "SPARK", "SPARKSQL", "SPARK_SQL", "SPARK3",
    "CLICKHOUSE", "MYSQL", "POSTGRESQL", "ORACLE", "SQLSERVER", "DB2", "PRESTO",
    "DORIS", "STARROCKS", "TRINO", "CLICKHOUSE_SQL",
}

#: shell 强特征（出现即基本可以断定是 Shell 脚本）
_SHELL_MARKERS = (
    re.compile(r"^#!.*\b(?:ba|z|k)?sh\b", re.MULTILINE),
    re.compile(r"^\s*(?:set\s+-[a-zA-Z]+|export\s+\w+=|echo\s)", re.MULTILINE),
    re.compile(r"\$\{\w+\}|\$\w+\b"),
    re.compile(r"\bfi\b\s*$|\bthen\b\s*$|\bdone\b\s*$", re.MULTILINE),
)

#: python 强特征
_PY_MARKERS = (
    re.compile(r"^#!.*\bpython[0-9.]*\b", re.MULTILINE),
    re.compile(r"^\s*(?:import\s+\w|from\s+[\w.]+\s+import\s)", re.MULTILINE),
    re.compile(r"^\s*def\s+\w+\s*\(", re.MULTILINE),
    re.compile(r"\.sql\s*\(", re.MULTILINE),
    re.compile(r"\b(?:read_sql_query|read_sql_table|read_sql|sqlalchemy\.text)\s*\("),
    re.compile(r"\b(?:SparkSession|spark\s*=|print\s*\()", re.MULTILINE),
)

_PY_PREFIX_RE = re.compile(r"(?i)([rubf]{1,3})(?=(?:'''|\"\"\"|'|\"))")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_VAR_REF_RE = re.compile(r"""^\$(?:\{(?P<braced>[A-Za-z_]\w*)\}|(?P<plain>[A-Za-z_]\w*))$""")


# --------------------------------------------------------------------------- #
# 0) 通用小工具
# --------------------------------------------------------------------------- #
def _line_of(text: str, pos: int) -> int:
    """字符偏移 → 行号（1 起）。"""
    return text.count("\n", 0, max(0, pos)) + 1


def _simplify(sql: str) -> str:
    """SQL 归一化（只用于去重比较，不改变原文）。"""
    return re.sub(r"\s+", " ", (sql or "").strip())


def _strip_line_comments(sql: str, marker: str = "--") -> str:
    """去掉整行注释（用于「像不像 SQL」的判断，不改变原文）。"""
    lines = [ln for ln in (sql or "").splitlines() if not ln.strip().startswith(marker)]
    return "\n".join(lines).strip()


def _looks_like_sql(text: str) -> bool:
    """判断一段文本是否像 SQL（够保守：必须能看出语句结构）。"""
    body = _strip_line_comments(text or "")
    if not body:
        return False
    if _SQL_START_RE.match(body):
        return True
    return bool(_SQL_STRONG_RE.search(body))


def looks_like_sql(text: str) -> bool:
    """公开的「这段文本像不像 SQL」判断（供工作流兜底逻辑复用）。"""
    return _looks_like_sql(text)


def _looks_like_sql_script(text: str) -> bool:
    """整段文本是否**以**一条 SQL 语句开头（区分「就是 SQL 脚本」与「脚本里含 SQL」）。"""
    body = _strip_line_comments(text or "")
    return bool(body) and bool(_SQL_SCRIPT_START_RE.match(body))


def _looks_like_docstring(text: str, start: int) -> bool:
    """某个字符串字面量是否是（模块 / 函数 / 类的）文档字符串。"""
    before = text[:start].rstrip()
    if not before:
        return True                                   # 模块 docstring：文件开头就是它
    prev_line = before[before.rfind("\n") + 1:]
    return bool(re.match(r"^\s*(?:def|class)\b", prev_line)) and prev_line.rstrip().endswith(":")


def _read_quoted(
    text: str, i: int, quotes: Sequence[str] = ('"""', "'''", '"', "'")
) -> Tuple[Optional[str], int, Optional[str]]:
    """从 ``text[i]`` 开始读一个字符串字面量，返回 ``(内容, 结束后偏移, 引号)``。

    支持反斜杠转义（对单字符引号）；未闭合时返回读到文本末尾的内容。
    读不到引号时返回 ``(None, i, None)``。
    """
    for q in quotes:
        if not text.startswith(q, i):
            continue
        j = i + len(q)
        buf: List[str] = []
        while j < len(text):
            ch = text[j]
            if ch == "\\" and j + 1 < len(text) and len(q) == 1:
                buf.append(text[j + 1])          # 单字符引号：吃掉转义符
                j += 2
                continue
            if text.startswith(q, j):
                return "".join(buf), j + len(q), q
            buf.append(ch)
            j += 1
        return "".join(buf), len(text), q         # 未闭合
    return None, i, None


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def _read_balanced(text: str, i: int) -> Tuple[str, int]:
    """从 ``i`` 读到同层 ``)`` 之前（不会跨过嵌套括号），返回 ``(原文, 结束偏移)``。"""
    depth = 0
    j = i
    while j < len(text):
        ch = text[j]
        if ch in "\"'":
            _body, end, q = _read_quoted(text, j, ('"""', "'''", '"', "'"))
            if q:
                j = end
                continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return text[i:j], j
            depth -= 1
        j += 1
    return text[i:j], j


def _is_cmd_position(text: str, pos: int) -> bool:
    """``pos`` 是否是命令行起点（避免把 ``echo "hive -e ..."`` 里的 hive 当命令）。"""
    i = pos - 1
    while i >= 0 and text[i] in " \t\\":
        i -= 1
    return i < 0 or text[i] in _CMD_BOUNDARY


def _neutralize_fstring(body: str) -> Tuple[str, List[str]]:
    """把 f-string 的 ``{expr}`` 占位符替换成 ``0``（保持可解析），返回 ``(文本, 占位符列表)``。"""
    out: List[str] = []
    placeholders: List[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "{" and i + 1 < len(body) and body[i + 1] == "{":
            out.append("{")
            i += 2
            continue
        if ch == "}" and i + 1 < len(body) and body[i + 1] == "}":
            out.append("}")
            i += 2
            continue
        if ch == "{":
            depth = 1
            j = i + 1
            while j < len(body) and depth:
                if body[j] == "{":
                    depth += 1
                elif body[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            expr = body[i + 1:j].strip()
            placeholders.append(expr)
            out.append("0")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), placeholders


def _entry(
    sql: Optional[str],
    source_hint: str,
    start: int,
    line: int,
    *,
    note: Optional[str] = None,
    placeholders: Optional[List[str]] = None,
    resolved: bool = True,
) -> Dict[str, Any]:
    """构造一条提取结果（``extract_sqls`` 的元素）。"""
    return {
        "sql": sql,
        "source_hint": source_hint,
        "start": start,
        "line": line,
        "note": note,
        "placeholders": list(placeholders or []),
        "resolved": resolved,
    }


def _dedupe(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 (归一化 SQL, source_hint) 去重并保持出现顺序。"""
    out: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()
    for e in entries:
        key = (_simplify(e.get("sql") or ""), e.get("source_hint") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


# --------------------------------------------------------------------------- #
# 1) 脚本类型判定
# --------------------------------------------------------------------------- #
def _has_shell_markers(text: str) -> bool:
    if any(p.search(text) for p in _SHELL_MARKERS):
        return True
    return _find_shell_commands(text) != []


def _has_python_markers(text: str) -> bool:
    return any(p.search(text) for p in _PY_MARKERS)


def _task_type_kind(task_type: Optional[str]) -> Optional[str]:
    t = str(task_type or "").strip().upper()
    if not t:
        return None
    if t in _PY_TASK_TYPES:
        return "python"
    if t in _SH_TASK_TYPES:
        return "shell"
    if t in _SQL_TASK_TYPES:
        return "sql"
    # 兜底：海豚里偶发 ``PYTHON3`` / ``SHELL_SCRIPT`` 之类的写法
    if "PYTHON" in t or t.startswith("PY"):
        return "python"
    if "SHELL" in t or t in ("SH", "BASH"):
        return "shell"
    if "SQL" in t or "HIVE" in t or "SPARK" in t:
        return "sql"
    return None


def detect_script_kind(text: str, task_type: Optional[str] = None) -> str:
    """判定脚本类型，返回 ``'sql'`` / ``'shell'`` / ``'python'`` / ``'unknown'``。

    判定顺序（**内容特征优先于平台任务类型**，因为任务类型经常被填错或与内容不符）：

    1. 有 shell 命令 / 脚本特征（``hive -e`` / ``beeline`` / heredoc / ``#!/bin/bash`` …）→ ``shell``；
    2. 有 Python 特征（``import`` / ``def`` / ``spark.sql(`` / ``#!/usr/bin/env python`` …）→ ``python``；
    3. 其余情况看内容是否像纯 SQL → ``sql``；
    4. 都没有 → 退回 ``task_type`` 推断；仍无 → ``unknown``。

    注意：海豚 SHELL 任务的 ``rawScript`` 经 ``ds_client.normalize_script`` 剥掉
    ``hive -e "..."`` 外壳后，剩下的就是**纯 SQL 文本**，此时本函数会如实返回 ``sql`` ——
    因为它就是要喂给 SQL 解析器的东西。
    """
    text = text or ""
    if not text.strip():
        return "unknown"

    if _has_shell_markers(text):
        return "shell"
    if _has_python_markers(text):
        return "python"
    if _looks_like_sql_script(text):
        return "sql"
    return _task_type_kind(task_type) or "unknown"


# --------------------------------------------------------------------------- #
# 2) Shell 提取
# --------------------------------------------------------------------------- #
def _find_shell_commands(text: str) -> List[Tuple[int, str]]:
    """找出所有「命令行起始位置」的 SQL 客户端命令，返回 ``[(偏移, 命令名)]``。"""
    out: List[Tuple[int, str]] = []
    pattern = re.compile(
        r"(?<![\w./-])(?P<cmd>" + "|".join(re.escape(c) for c in SHELL_SQL_COMMANDS) + r")(?![\w-])",
        re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        if _is_cmd_position(text, m.start()):
            out.append((m.start(), m.group("cmd").lower()))
    return out


def _collect_shell_vars(text: str) -> Dict[str, Dict[str, Any]]:
    """收集 ``NAME="..."`` / ``NAME='...'`` 形式的变量赋值（值可以是多行引号串）。"""
    out: Dict[str, Dict[str, Any]] = {}
    pattern = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?(?P<name>[A-Za-z_]\w*)=")
    for m in pattern.finditer(text):
        q_at = m.end()
        while q_at < len(text) and text[q_at] in " \t":
            q_at += 1
        if text[q_at:q_at + 1] not in ("'", '"'):
            continue                                   # 值不是引号串（如 $(...) / 裸值）→ 不认
        body, end, _q = _read_quoted(text, q_at, ('"', "'"))
        if body is None:
            continue
        nl = text.find("\n", end)
        tail = text[end:nl if nl != -1 else len(text)]
        if tail.strip() and not tail.strip().startswith("#"):
            continue                                   # 引号后面还接了别的东西，不认
        out[m.group("name")] = {
            "body": body,
            "start": m.start(),
            "line": _line_of(text, m.start()),
            "end": end,
        }
    return out


def _resolve_shell_var(body: str, var_map: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """``-e "$SQL"`` 这种：把变量名换成变量内容。"""
    m = _VAR_REF_RE.match((body or "").strip())
    if not m:
        return None
    name = m.group("braced") or m.group("plain")
    info = var_map.get(name)
    if info is None:
        return None
    return {"name": name, **info}


def _iter_heredocs(text: str) -> List[Dict[str, Any]]:
    """扫出 ``cmd <<EOF ... EOF`` 形式的 heredoc 正文（含 ``<<-EOF`` / 引号 tag）。"""
    out: List[Dict[str, Any]] = []
    lines = text.splitlines(keepends=True)
    offsets: List[int] = []
    acc = 0
    for ln in lines:
        offsets.append(acc)
        acc += len(ln)

    start_re = re.compile(
        r"<<(?P<dash>-?)\s*(?P<q>['\"]?)(?P<tag>[A-Za-z_]\w*)(?P=q)"
    )
    for idx, ln in enumerate(lines):
        if ln.lstrip().startswith("#"):
            continue                                   # 注释里的 <<EOF 不是 heredoc
        m = start_re.search(ln)
        if not m:
            continue
        # 必须真的是「某个 SQL 客户端命令 + heredoc」（排除 cat <<EOF > x.sql 之类）
        if not _find_shell_commands(ln):
            continue
        tag = m.group("tag")
        strip_tabs = bool(m.group("dash"))
        body_lines: List[str] = []
        end_idx = None
        for k in range(idx + 1, len(lines)):
            cand = lines[k].rstrip("\r\n")
            if strip_tabs:
                cand = cand.lstrip("\t")
            if cand.strip() == tag:
                end_idx = k
                break
            body_lines.append(lines[k])
        if end_idx is None:
            continue                                   # 未闭合的 heredoc：不当 SQL
        body = "".join(body_lines)
        start = offsets[idx + 1] if idx + 1 < len(offsets) else offsets[idx]
        # 命令名：同一行 heredoc 之前出现的 SQL 客户端命令（若有）
        cmd = ""
        for cpos, cname in _find_shell_commands(text[:offsets[idx] + len(ln)]):
            if cpos >= offsets[idx] - 2:
                cmd = cname
        if not cmd:
            cmd = "heredoc"
        out.append(
            {
                "sql": body,
                "cmd": cmd,
                "tag": tag,
                "start": start,
                "line": _line_of(text, start),
                "end": offsets[end_idx] if end_idx < len(offsets) else len(text),
                "cmd_line_start": offsets[idx],
                "cmd_line_end": offsets[idx] + len(ln),
            }
        )
    return out


def _extract_shell(
    text: str, base_dir: Optional[TypingUnion[str, Path]] = None, resolve_files: bool = False
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Shell 脚本 → 内嵌 SQL 列表 + 未解析提示。"""
    entries: List[Dict[str, Any]] = []
    hints: List[str] = []
    var_map = _collect_shell_vars(text)
    used_vars: Set[str] = set()

    # --- (1) heredoc ------------------------------------------------------- #
    heredoc_spans: List[Tuple[int, int]] = []
    heredoc_cmd_lines: List[Tuple[int, int]] = []
    for hd in _iter_heredocs(text):
        heredoc_spans.append((hd["start"], hd["end"]))
        heredoc_cmd_lines.append((hd["cmd_line_start"], hd["cmd_line_end"]))
        if not _looks_like_sql(hd["sql"]):
            hints.append(
                f"第 {hd['line']} 行 {hd['cmd']} heredoc（tag={hd['tag']}）正文不像 SQL，已跳过")
            continue
        entries.append(
            _entry(hd["sql"], f"{hd['cmd']} heredoc <<{hd['tag']} 第 {hd['line']} 行", hd["start"], hd["line"])
        )

    def _in_heredoc(pos: int) -> bool:
        return any(s <= pos < e for s, e in heredoc_spans)

    # --- (2) 命令内联 SQL：hive -e "..." / beeline -e / spark-sql -e / impala-shell -q --- #
    for pos, cmd in _find_shell_commands(text):
        if _in_heredoc(pos) or any(s <= pos < e for s, e in heredoc_cmd_lines):
            continue
        rest_at = _skip_ws(text, pos + len(cmd))
        flag_re = re.compile(
            r"(?P<flag>--?(?:%s))\b" % "|".join(f.lstrip("-") for f in _INLINE_FLAGS)
        )
        # 只在当前命令行内找开关（到换行 / ';' / '|' 为止）
        line_end = len(text)
        for stop in ("\n", ";", "|"):
            k = text.find(stop, rest_at)
            if k != -1:
                line_end = min(line_end, k)
        seg = text[rest_at:line_end]
        arg_m = flag_re.search(seg)
        if arg_m:
            after = _skip_ws(text, rest_at + arg_m.end())
            body, end, quote = _read_quoted(text, after, ('"', "'"))
            if quote is not None and body is not None:
                simple = (body or "").strip()
                var_info = _resolve_shell_var(simple, var_map)
                if var_info is not None and not var_info["body"].strip():
                    hints.append(
                        f"第 {_line_of(text, after)} 行 {cmd} -e 引用了变量 ${var_info['name']}，"
                        f"但变量内容为空")
                    continue
                if var_info is not None:
                    used_vars.add(var_info["name"])
                    if not _looks_like_sql(var_info["body"]):
                        hints.append(
                            f"第 {var_info['line']} 行变量 {var_info['name']} 被 {cmd} -e 引用，"
                            f"但内容不像 SQL，已跳过")
                        continue
                    entries.append(
                        _entry(
                            var_info["body"],
                            f"{cmd} -e \"${var_info['name']}\"（变量 {var_info['name']} "
                            f"定义于第 {var_info['line']} 行）",
                            var_info["start"],
                            var_info["line"],
                            note="SQL 来自 shell 变量展开",
                        )
                    )
                    continue
                if simple.startswith("$"):
                    hints.append(
                        f"第 {_line_of(text, after)} 行 {cmd} -e 引用了未在脚本内定义的变量 "
                        f"{simple}，无法提取 SQL")
                    continue
                if not _looks_like_sql(simple):
                    hints.append(
                        f"第 {_line_of(text, after)} 行 {cmd} -e 的内容不像 SQL，已跳过")
                    continue
                entries.append(
                    _entry(simple, f"{cmd} -e 第 {_line_of(text, after)} 行", after,
                           _line_of(text, after))
                )
                continue

        # --- (3) 文件引用：hive -f etl/xx.sql ------------------------------ #
        file_re = re.compile(
            r"(?P<flag>--?(?:%s))\s+(?P<path>\"[^\"]+\"|'[^']+'|[^\s;&|]+)"
            % "|".join(f.lstrip("-") for f in _FILE_FLAGS)
        )
        fm = file_re.search(seg)
        if fm:
            raw_path = fm.group("path").strip("\"'")
            line_no = _line_of(text, rest_at + fm.start())
            resolved_path = None
            if base_dir:
                cand = Path(base_dir) / raw_path
                if cand.exists():
                    resolved_path = cand
            elif resolve_files:
                cand = Path(raw_path)
                if cand.exists():
                    resolved_path = cand
            if resolved_path is not None:
                body = resolved_path.read_text(encoding="utf-8", errors="replace")
                entries.append(
                    _entry(body, f"{cmd} -f {raw_path}（已读取本地文件）", rest_at + fm.start(),
                           line_no, note=f"SQL 正文来自文件 {resolved_path}")
                )
            else:
                entries.append(
                    _entry(
                        None,
                        f"{cmd} -f {raw_path}",
                        rest_at + fm.start(),
                        line_no,
                        note="SQL 在外部文件里，脚本内没有正文，未解析",
                        resolved=False,
                    )
                )
                hints.append(
                    f"第 {line_no} 行 {cmd} -f {raw_path}：SQL 在外部文件里，"
                    f"脚本内无正文（可用 resolve_files=True + base_dir 让它读取本地文件）")
            continue

        hints.append(f"第 {_line_of(text, pos)} 行发现 {cmd} 命令但未识别到 -e/-q/-c/-f 参数，已跳过")

    # --- (4) 变量里的 SQL（未被命令引用的才单独提取） --------------------- #
    for name, info in var_map.items():
        if name in used_vars:
            continue
        if not _looks_like_sql(info["body"]):
            continue
        if _in_heredoc(info["start"]):
            continue
        entries.append(
            _entry(
                info["body"],
                f"shell 变量 {name}=\"...\" 第 {info['line']} 行",
                info["start"],
                info["line"],
                note="变量内嵌 SQL（脚本内未见 hive/beeline 等命令引用）",
            )
        )

    entries = _dedupe(sorted(entries, key=lambda e: (e["start"], e["source_hint"])))
    return entries, hints


# --------------------------------------------------------------------------- #
# 3) Python / PySpark 提取
# --------------------------------------------------------------------------- #
def _scan_python_code(text: str) -> Tuple[List[Dict[str, Any]], List[Tuple[int, int]]]:
    """扫出脚本里的字符串字面量与 ``#`` 注释区间，返回 ``(literals, comment_spans)``。"""
    out: List[Dict[str, Any]] = []
    comments: List[Tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "#":
            j = text.find("\n", i)
            j = n if j < 0 else j
            comments.append((i, j))
            i = j
            continue
        prefix = ""
        quote_at = i
        m = _PY_PREFIX_RE.match(text, i)
        if m and not (i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_")):
            if text.startswith(("'''", '"""'), m.end()) or text[m.end()] in "\"'":
                prefix = m.group(1)
                quote_at = m.end()
        if text[quote_at:quote_at + 1] in ("'", '"'):
            body, end, q = _read_quoted(text, quote_at, ('"""', "'''", '"', "'"))
            if q is not None and body is not None:
                out.append(
                    {
                        "start": quote_at,
                        "end": end,
                        "body": body,
                        "quote": q,
                        "prefix": prefix,
                        "fstring": "f" in prefix.lower(),
                        "line": _line_of(text, quote_at),
                    }
                )
                i = end
                continue
        i += 1
    return out, comments


def _scan_python_literals(text: str) -> List[Dict[str, Any]]:
    """扫出脚本里所有字符串字面量（跳过 ``#`` 注释）。"""
    return _scan_python_code(text)[0]


def _collect_python_vars(literals: Sequence[Dict[str, Any]], text: str) -> Dict[str, Dict[str, Any]]:
    """收集 ``name = \"\"\"...\"\"\"`` 形式的 SQL 变量。"""
    out: Dict[str, Dict[str, Any]] = {}
    for lit in literals:
        head = text[max(0, lit["start"] - 200):lit["start"]]
        m = re.search(r"(?m)^[ \t]*(?P<name>[A-Za-z_]\w*)[ \t]*(?::[^=\n]+)?=[ \t]*$", head)
        if m:
            out[m.group("name")] = {"body": lit["body"], "line": lit["line"], "literal": lit}
    return out


def _extract_python(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Python / PySpark 脚本 → 内嵌 SQL 列表 + 未解析提示。"""
    entries: List[Dict[str, Any]] = []
    hints: List[str] = []
    literals, comment_spans = _scan_python_code(text)
    var_map = _collect_python_vars(literals, text)
    consumed: Set[Tuple[int, int]] = set()
    lit_spans: List[Tuple[int, int]] = [(l["start"], l["end"]) for l in literals]

    def _inside_literal(pos: int) -> bool:
        """位置是否落在字符串字面量或 ``#`` 注释里。

        文档字符串 / 注释里提到的 ``spark.sql(...)`` 只是文字，不是代码，不能算血缘。
        """
        return any(s <= pos < e for s, e in lit_spans + comment_spans)

    def _add(body: str, hint: str, start: int, line: int, note: Optional[str] = None,
             fstring: bool = False, span: Optional[Tuple[int, int]] = None,
             force: bool = False) -> None:
        """加一条提取结果；``body`` 先做 f-string 占位符中和。"""
        placeholders: List[str] = []
        sql_text = body
        if fstring:
            sql_text, placeholders = _neutralize_fstring(body)
        if not force and not _looks_like_sql(sql_text):
            hints.append(f"第 {line} 行 {hint} 的内容不像 SQL，已跳过")
            return
        if span:
            consumed.add(span)
        entries.append(
            _entry(sql_text, hint, start, line, note=note, placeholders=placeholders)
        )

    # --- (1) spark.sql(...) / session.sql(...) ----------------------------- #
    sql_call_re = re.compile(r"(?<![\w.])(?P<obj>[A-Za-z_]\w*)\s*\.\s*sql\s*\(")
    for m in sql_call_re.finditer(text):
        if _inside_literal(m.start()):
            continue                                   # 文档字符串里提到的 spark.sql() 不算
        open_paren = m.end() - 1
        arg_at = _skip_ws(text, open_paren + 1)
        if arg_at >= len(text) or text[arg_at] == ")":
            hints.append(f"第 {_line_of(text, m.start())} 行 {m.group('obj')}.sql() 参数为空，已跳过")
            continue
        # 允许 f""" / r''' 这类字符串前缀
        arg_prefix = ""
        pm = _PY_PREFIX_RE.match(text, arg_at)
        if pm and (text.startswith(("'''", '"""'), pm.end()) or text[pm.end():pm.end() + 1] in ("'", '"')):
            arg_prefix = pm.group(1)
            arg_at = pm.end()
        quote = text[arg_at]
        span: Optional[Tuple[int, int]] = None
        fstring = False
        placeholders: List[str] = []
        if quote in "\"'":
            body, end, q = _read_quoted(text, arg_at, ('"""', "'''", '"', "'"))
            if q is None or body is None:
                hints.append(f"第 {_line_of(text, m.start())} 行 {m.group('obj')}.sql() "
                             f"字符串未闭合，已跳过")
                continue
            fstring = "f" in arg_prefix.lower()
            span = (arg_at, end)
            sql_text = body
            if fstring:
                sql_text, placeholders = _neutralize_fstring(body)
            if not _looks_like_sql(sql_text):
                hints.append(
                    f"第 {_line_of(text, m.start())} 行 {m.group('obj')}.sql() 的内容不像 SQL，已跳过")
                continue
            consumed.add(span)
            hint = f"{m.group('obj')}.sql() 第 {_line_of(text, m.start())} 行"
            note = "f-string 占位符已替换为 0（值需运行时才知道）" if placeholders else None
            entries.append(_entry(sql_text, hint, arg_at, _line_of(text, arg_at),
                                  note=note, placeholders=placeholders))
            continue

        # 变量拼接 / 表达式：尽力而为
        expr, end = _read_balanced(text, arg_at)
        name = expr.strip()
        line_no = _line_of(text, m.start())
        if _IDENT_RE.fullmatch(name) and name in var_map:
            info = var_map[name]
            lit = info["literal"]
            sql_text = info["body"]
            ph: List[str] = []
            if lit["fstring"]:
                sql_text, ph = _neutralize_fstring(info["body"])
            if not _looks_like_sql(sql_text):
                hints.append(f"第 {line_no} 行 {m.group('obj')}.sql({name}) 变量内容不像 SQL，已跳过")
                continue
            consumed.add((lit["start"], lit["end"]))
            entries.append(
                _entry(sql_text, f"{m.group('obj')}.sql({name}) 第 {line_no} 行"
                                 f"（变量定义于第 {info['line']} 行）",
                       lit["start"], info["line"],
                       note="SQL 来自脚本内变量", placeholders=ph)
            )
            continue
        hints.append(
            f"第 {line_no} 行 {m.group('obj')}.sql({name[:40]}{'...' if len(name) > 40 else ''}) "
            f"参数不是字符串字面量，静态层面无法求值（未提取）")

    # --- (2) pandas / sqlalchemy ------------------------------------------- #
    call_re = re.compile(
        r"(?<![\w.])(?:(?P<obj>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\.\s*)?"
        r"(?P<fn>read_sql_query|read_sql_table|read_sql|text)\s*\(")
    for m in call_re.finditer(text):
        if _inside_literal(m.start()):
            continue
        fn = m.group("fn")
        obj = m.group("obj") or ""
        arg_at = _skip_ws(text, m.end())
        pm = _PY_PREFIX_RE.match(text, arg_at)
        if pm and (text.startswith(("'''", '"""'), pm.end()) or text[pm.end():pm.end() + 1] in ("'", '"')):
            arg_at = pm.end()
        if arg_at >= len(text) or text[arg_at] not in "\"'":
            continue
        body, end, q = _read_quoted(text, arg_at, ('"""', "'''", '"', "'"))
        if q is None or body is None:
            continue
        line_no = _line_of(text, m.start())
        if fn == "text":
            # sqlalchemy.text("...")：只认确实像 SQL 的，避免误伤同名函数
            if not _looks_like_sql(body):
                continue
            if "sqlalchemy" not in text and obj and obj != "text":
                continue
            label = f"{obj + '.' if obj else ''}text()"
            note = "SQL 来自 sqlalchemy.text()"
        elif fn == "read_sql_table":
            continue
        else:
            label = f"{obj + '.' if obj else ''}{fn}()"
            note = "SQL 从 pandas/sqlalchemy 读取"
        consumed.add((arg_at, end))
        _add(body, f"{label} 第 {line_no} 行", arg_at, line_no, note=note,
             span=(arg_at, end))

    # --- (3) 其余「整段就是 SQL」的字符串段落（模块常量 / 三引号） ---------- #
    for lit in literals:
        if (lit["start"], lit["end"]) in consumed:
            continue
        if _looks_like_docstring(text, lit["start"]):
            continue                                   # 文档字符串里的 SQL 只是说明文字
        sql_text = lit["body"]
        ph: List[str] = []
        if lit["fstring"]:
            sql_text, ph = _neutralize_fstring(lit["body"])
        # 必须**以** SQL 语句开头：`示例：spark.sql("INSERT ...")` 这种散文不提取
        if not _looks_like_sql_script(sql_text):
            continue
        consumed.add((lit["start"], lit["end"]))
        entries.append(
            _entry(sql_text, f"python 字符串字面量 第 {lit['line']} 行", lit["start"], lit["line"],
                   note="脚本里以字符串形式存在的 SQL 段落", placeholders=ph)
        )

    entries = _dedupe(sorted(entries, key=lambda e: (e["start"], e["source_hint"])))
    return entries, hints


# --------------------------------------------------------------------------- #
# 4) 对外：提取
# --------------------------------------------------------------------------- #
def extract_sqls(
    text: str,
    kind: Optional[str] = None,
    *,
    task_type: Optional[str] = None,
    base_dir: Optional[TypingUnion[str, Path]] = None,
    resolve_files: bool = False,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """从脚本里提取内嵌 SQL。

    参数：
        text: 脚本文本。
        kind: ``'sql'`` / ``'shell'`` / ``'python'``；省略（或 ``unknown``）时内部调用
            :func:`detect_script_kind` 判定。
        task_type: 海豚任务类型，仅用于类型判定兜底。
        base_dir: 解析 ``hive -f etl/x.sql`` 这类外部文件引用时的相对目录。
        resolve_files: 不带 ``base_dir`` 时是否也尝试按当前工作目录找文件。

    返回 ``([{sql, source_hint, line, note, placeholders, resolved}], [未解析提示])``。

    ``sql`` 为 ``None``（``resolved=False``）表示这条 SQL 的正文**不在脚本里**
    （典型：``hive -f etl/x.sql``），只记录引用位置，不编造内容。
    """
    kind = (kind or "").lower()
    if kind in ("", "unknown", "auto"):
        kind = detect_script_kind(text, task_type)

    if kind == "shell":
        return _extract_shell(text, base_dir=base_dir, resolve_files=resolve_files)
    if kind == "python":
        return _extract_python(text)

    body = _strip_line_comments(text)
    if not body:
        return [], ["脚本文本为空（或只有注释），没有可解析的 SQL"]
    return [_entry(body, "(整段脚本按 SQL 解析)", 0, 1)], []


# --------------------------------------------------------------------------- #
# 5) 对外：解析
# --------------------------------------------------------------------------- #
def _blank_result(kind: str, dialect: str) -> Dict[str, Any]:
    return {
        "kind": kind,
        "detected_kind": kind,
        "task_type": None,
        "source": None,
        "dialect": dialect,
        "sqls": [],
        "statements": [],
        "sql_count": 0,
        "statement_count": 0,
        "input_tables": [],
        "output_tables": [],
        "input_table_names": [],
        "output_table_names": [],
        "table_lineage": [],
        "column_lineage": [],
        "column_lineage_count": 0,
        "unresolved_hints": [],
        "errors": [],
    }


def parse_script(
    text: str,
    kind: Optional[str] = None,
    dialect: str = DEFAULT_DIALECT,
    *,
    task_type: Optional[str] = None,
    base_dir: Optional[TypingUnion[str, Path]] = None,
    resolve_files: bool = False,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """提取脚本内嵌 SQL 并解析出表级 / 字段级血缘。

    返回结构（``column_lineage`` 每条都带 ``source_hint``，标明它来自脚本的哪一段）::

        {
          "kind": "shell",                     # 实际使用的脚本类型
          "dialect": "hive",
          "sql_count": 2,                      # 提取到的内嵌 SQL 条数
          "statement_count": 3,                # 解析出的 SQL 语句数（一条内嵌 SQL 可含多语句）
          "sqls": [{"sql":..., "source_hint":..., "parse": {...}, "error": None}],
          "input_tables"/"output_tables": [...],
          "table_lineage": [...], "column_lineage": [...],
          "unresolved_hints": [...],           # 提取不到 / 无法静态求值的如实记录
          "errors": [...]
        }
    """
    detected = detect_script_kind(text, task_type)
    use_kind = (kind or "").lower()
    if use_kind in ("", "unknown", "auto"):
        use_kind = detected

    result = _blank_result(use_kind, dialect)
    result["detected_kind"] = detected
    result["task_type"] = task_type
    result["source"] = source

    entries, hints = extract_sqls(
        text, use_kind, task_type=task_type, base_dir=base_dir, resolve_files=resolve_files
    )
    result["unresolved_hints"].extend(hints)

    parser = SqlLineageParser(dialect=dialect)
    inputs: List[str] = []
    outputs: List[str] = []
    table_pairs: Dict[Tuple[str, str], Dict[str, Any]] = {}
    columns: List[Dict[str, Any]] = []
    statements_out: List[Dict[str, Any]] = []

    for item in entries:
        sql_text = item.get("sql")
        if not sql_text:
            result["sqls"].append(
                {
                    "sql": None,
                    "source_hint": item.get("source_hint"),
                    "line": item.get("line"),
                    "note": item.get("note"),
                    "parse": None,
                    "error": None,
                }
            )
            continue
        parsed: Optional[Dict[str, Any]] = None
        error: Optional[str] = None
        try:
            statements = parser.parse_sql(sql_text, source=item.get("source_hint")) or []
        except Exception as e:  # noqa: BLE001 — 单条内嵌 SQL 失败不影响其它
            error = f"{type(e).__name__}: {e}"
            result["errors"].append(f"{item.get('source_hint')}: {error}")
            result["unresolved_hints"].append(
                f"{item.get('source_hint')}：内嵌 SQL 解析失败（{type(e).__name__}），该段血缘缺失")
            statements = []

        if statements:
            agg = parser.aggregate(statements)
            for st in statements:
                tagged = dict(st)
                tagged["source_hint"] = item.get("source_hint")
                statements_out.append(tagged)
            parsed = {
                "statement_count": agg["statement_count"],
                "input_table_names": agg["input_table_names"],
                "output_table_names": agg["output_table_names"],
                "table_lineage": agg["table_lineage"],
                "column_lineage_count": len(agg["column_lineage"]),
            }
            result["statement_count"] += agg["statement_count"]
            for t in agg["input_table_names"]:
                if t not in inputs:
                    inputs.append(t)
            for t in agg["output_table_names"]:
                if t not in outputs:
                    outputs.append(t)
            for pair in agg["table_lineage"]:
                rec = table_pairs.setdefault(
                    (pair["source"], pair["target"]),
                    {"source": pair["source"], "target": pair["target"], "source_hint": item.get("source_hint")},
                )
                rec.setdefault("via", [])
                if item.get("source_hint") not in rec["via"]:
                    rec["via"].append(item.get("source_hint"))
            for col in agg["column_lineage"]:
                enriched = dict(col)
                enriched["source_hint"] = item.get("source_hint")
                columns.append(enriched)

        result["sqls"].append(
            {
                "sql": sql_text,
                "source_hint": item.get("source_hint"),
                "line": item.get("line"),
                "note": item.get("note"),
                "placeholders": item.get("placeholders") or [],
                "parse": parsed,
                "error": error,
            }
        )

    if not entries:
        result["unresolved_hints"].append(
            f"未从该 {use_kind} 脚本中提取到任何内嵌 SQL（请确认 SQL 是否写在外部文件里）")

    for item in entries:
        for ph in item.get("placeholders") or []:
            result["unresolved_hints"].append(
                f"{item.get('source_hint')}：f-string 占位符 {{{ph}}} 的值需运行时才知道，"
                f"已按 0 替换后解析，该列血缘可能不精确")

    result["sql_count"] = sum(1 for e in entries if e.get("sql"))
    # 未解析提示去重（保序）：同一个占位符 / 同一个文件引用只提示一次
    seen_hints: Set[str] = set()
    result["unresolved_hints"] = [
        h for h in result["unresolved_hints"]
        if not (h in seen_hints or seen_hints.add(h))
    ]
    result["input_tables"] = inputs
    result["output_tables"] = outputs
    result["input_table_names"] = inputs
    result["output_table_names"] = outputs
    result["table_lineage"] = [
        {"source": k[0], "target": k[1], "via": table_pairs[k]["via"]} for k in sorted(table_pairs)
    ]
    result["column_lineage"] = columns
    result["column_lineage_count"] = len(columns)
    result["statements"] = statements_out
    return result


def parse_script_file(
    path: TypingUnion[str, Path],
    kind: Optional[str] = None,
    dialect: str = DEFAULT_DIALECT,
    *,
    task_type: Optional[str] = None,
    resolve_files: bool = False,
) -> Dict[str, Any]:
    """解析一个脚本文件（``.sh`` / ``.py`` / ``.sql`` 都能吃）。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    return parse_script(
        text, kind, dialect, task_type=task_type, base_dir=p.parent,
        resolve_files=resolve_files, source=str(p),
    )


# --------------------------------------------------------------------------- #
# 6) 终端摘要
# --------------------------------------------------------------------------- #
def script_summary_text(result: Dict[str, Any], color: bool = False) -> str:
    """把 :func:`parse_script` 的结果渲染成终端可读的中文摘要。"""
    bold = "\033[1m" if color else ""
    dim = "\033[2m" if color else ""
    reset = "\033[0m" if color else ""
    rule = "=" * 72
    lines = [rule,
             f"{bold}脚本内嵌 SQL 血缘报告{reset}  kind={result.get('kind')}  "
             f"dialect={result.get('dialect')}",
             rule]
    if result.get("source"):
        lines.append(f"来源脚本: {result['source']}")
    lines.append(f"提取到内嵌 SQL: {result.get('sql_count', 0)} 条 / "
                 f"解析出语句: {result.get('statement_count', 0)} 条")

    for i, item in enumerate(result.get("sqls") or [], start=1):
        lines.append("")
        lines.append(f"{bold}[SQL {i}] 位置: {item.get('source_hint')}{reset}")
        if item.get("note"):
            lines.append(f"  说明: {item['note']}")
        if not item.get("sql"):
            lines.append("  (无正文，未解析)")
            continue
        body = (item["sql"] or "").strip().splitlines()
        preview = "\n".join(f"    | {ln}" for ln in body[:6])
        if len(body) > 6:
            preview += f"\n    | ...（共 {len(body)} 行）"
        lines.append(preview)
        parse = item.get("parse")
        if parse:
            lines.append(f"  语句数: {parse['statement_count']}   "
                         f"输出表: {', '.join(parse['output_table_names']) or '(无)'}")
            lines.append(f"  输入表: {', '.join(parse['input_table_names']) or '(无)'}")
        if item.get("error"):
            lines.append(f"  {dim}解析失败: {item['error']}{reset}")

    if result.get("table_lineage"):
        lines.append("")
        lines.append("表级血缘:")
        for pair in result["table_lineage"]:
            lines.append(f"  {pair['source']}  -->  {pair['target']}")
    if result.get("column_lineage"):
        lines.append("")
        lines.append(f"字段级血缘 ({len(result['column_lineage'])} 条):")
        for col in result["column_lineage"]:
            src = f"{col['source_table']}.{col['source_column']}" if col["source_table"] else col["source_column"]
            flag = "" if col.get("resolved", True) else f" {dim}(未能解析){reset}"
            lines.append(f"  {col['target_table']}.{col['target_column']}  <-  {src}   "
                         f"[{col['expression']}]{flag}")
    if result.get("unresolved_hints"):
        lines.append("")
        lines.append("未解析 / 需人工确认:")
        for h in result["unresolved_hints"]:
            lines.append(f"  - {h}")
    lines.append("")
    lines.append(rule)
    lines.append(f"{bold}汇总{reset}: 输入表 {len(result.get('input_tables') or [])} 张 / "
                 f"输出表 {len(result.get('output_tables') or [])} 张 / "
                 f"表级血缘 {len(result.get('table_lineage') or [])} 对 / "
                 f"字段级血缘 {len(result.get('column_lineage') or [])} 条")
    lines.append(rule)
    return "\n".join(lines)
