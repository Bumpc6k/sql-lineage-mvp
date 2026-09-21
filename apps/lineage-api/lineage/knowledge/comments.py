"""SQL 注释挖掘：把脚本里的「人写的业务知识」挖出来。

数仓脚本最大的隐性资产是注释：
* 文件头注释块写着「这一层干什么、上游是谁、下游是谁」；
* 每个 SELECT 项后面的行内注释写着字段的业务名（``-- 产量（箱）``）；
* ``CREATE TABLE ... COMMENT '卷烟产量明细事实表'`` 写着表的中文名；
* 行尾注释偶尔写着口径说明（``-- 箱转条``）。

本模块按行做词法扫描（不依赖 AST），产出上面四类信息。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

__all__ = ["SqlComments", "parse_sql_comments", "strip_sql_comment"]

_COMMENT_BLOCK_RE = re.compile(r"^\s*--")
_TABLE_COMMENT_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w\u4e00-\u9fff.]+)\s+COMMENT\s+'([^']*)'",
    re.IGNORECASE,
)
_AS_RE = re.compile(
    r"\bAS\s+`?([A-Za-z_][A-Za-z_0-9]*|\u4e00-\u9fff\w*)[`\s]*[,)]?\s*$", re.IGNORECASE
)
_WHERE_RE = re.compile(r"^\s*(?:WHERE|AND|OR|HAVING|QUALIFY)\b", re.IGNORECASE)
_WHERE_ANY_RE = re.compile(r"\b(?:WHERE|AND|OR|HAVING|QUALIFY)\b", re.IGNORECASE)


@dataclass
class SqlComments:
    """一个 SQL 文件里挖出来的注释信息。"""

    #: 文件头注释块（多行合并，用 `` / `` 连接）
    header: str = ""
    header_lines: List[str] = field(default_factory=list)
    #: 输出列名 -> 行内中文注释
    column_comments: Dict[str, str] = field(default_factory=dict)
    #: 表名（原样） -> COMMENT '...'
    table_comments: Dict[str, str] = field(default_factory=dict)
    #: WHERE 行上的行内注释
    where_comments: List[str] = field(default_factory=list)
    #: 所有行内注释 ``(字段/位置, 注释文本)``，用于口径备注
    inline_notes: List[Tuple[str, str]] = field(default_factory=list)

    def header_summary(self, limit: int = 400) -> str:
        return self.header[:limit]


def strip_sql_comment(line: str, in_block: bool = False) -> Tuple[str, Optional[str], bool]:
    """把一行拆成 ``(代码, 注释文本, 是否还在块注释里)``。

    简单实现：只处理 ``--`` 行注释与 ``/* */`` 块注释，字符串字面量内的
    ``--`` 极少出现在数仓脚本里，这里不做完整词法分析（属于已知限制）。
    """
    if in_block:
        end = line.find("*/")
        if end == -1:
            return "", None, True
        line = line[end + 2 :]

    code = line
    comment: Optional[str] = None
    dash = code.find("--")
    if dash != -1:
        comment = code[dash + 2 :].strip()
        code = code[:dash]

    while True:
        start = code.find("/*")
        if start == -1:
            break
        end = code.find("*/", start + 2)
        if end == -1:
            inner = code[start + 2 :].strip()
            if inner and comment is None:
                comment = inner
            return code[:start], comment, True
        inner = code[start + 2 : end].strip()
        code = code[:start] + code[end + 2 :]
        if inner and comment is None:
            comment = inner
    return code, comment, False


def parse_sql_comments(sql_text: str) -> SqlComments:
    """扫描 SQL 文本，挖出文件头 / 行内 / 表级注释。"""
    result = SqlComments()
    if not sql_text:
        return result

    in_block = False
    header_lines: List[str] = []
    header_done = False

    for raw_line in sql_text.splitlines():
        code, comment, in_block = strip_sql_comment(raw_line, in_block)
        stripped_code = code.strip()

        # 1) 文件头注释块：开头连续（允许空行）的注释行
        if not header_done:
            if not stripped_code and not comment:
                continue
            if not stripped_code and comment:
                if comment and not re.match(r"^[=\-*#]{3,}", comment):
                    header_lines.append(comment)
                continue
            header_done = True

        if comment:
            result.inline_notes.append((stripped_code[:60], comment))
            m = _AS_RE.search(stripped_code)
            if m:
                result.column_comments.setdefault(m.group(1).lower(), comment)
            elif _WHERE_RE.match(stripped_code) or _WHERE_ANY_RE.search(stripped_code):
                result.where_comments.append(comment)

    # 2) 表级 COMMENT
    for m in _TABLE_COMMENT_RE.finditer(sql_text):
        result.table_comments.setdefault(m.group(1), m.group(2).strip())

    result.header_lines = header_lines
    result.header = " / ".join(header_lines)
    return result
