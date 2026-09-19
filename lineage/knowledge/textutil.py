"""文本工具层：中文业务名推断（词典 + 命名规则）与表达式归一化。

本模块只做「纯文本 / 纯词法」的推导，不碰 AST，也不连接任何外部系统：
* :class:`Glossary` / :class:`ChineseNameResolver` —— 字段名 -> 中文业务名；
* :func:`normalize_expression` —— 把 ``SUM(i.output_qty) AS output_qty`` 归一化成
  ``SUM(output_qty)``（去表别名前缀、去 AS 别名、压缩空白、规范运算符间距）；
* :func:`strip_uniform_aggregates` —— 当表达式里所有聚合函数一致时，剥掉聚合壳，
  得到人类更爱看的「口径本体」（``SUM(a) + SUM(b)`` -> ``a + b``），
  聚合类型单独记录在 ``metric_type`` / ``aggregate_func`` 里；
* :func:`localize_expression` —— 把归一化表达式里的字段名换成中文业务名，
  得到 ``产量 = 打码量 + 跳码量 - 重码量`` 这种可读口径。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "Glossary",
    "ChineseNameResolver",
    "NameHit",
    "normalize_expression",
    "strip_uniform_aggregates",
    "localize_expression",
    "split_identifier",
    "AGGREGATE_FUNCS",
    "FUNC_NAMES",
    "SQL_KEYWORDS",
]

#: 聚合函数
AGGREGATE_FUNCS = {
    "SUM",
    "COUNT",
    "AVG",
    "MAX",
    "MIN",
    "STDDEV",
    "VARIANCE",
    "COLLECT_SET",
    "COLLECT_LIST",
    "PERCENTILE",
    "MEDIAN",
}

#: 常见 SQL 函数名（归一化/中文化时不能当成字段名替换掉）
FUNC_NAMES = {
    "SUM", "COUNT", "AVG", "MAX", "MIN", "ROUND", "CAST", "COALESCE", "NULLIF",
    "IFNULL", "IF", "NVL", "ABS", "CEIL", "CEILING", "FLOOR", "CONCAT", "SUBSTR",
    "SUBSTRING", "TRIM", "LTRIM", "RTRIM", "UPPER", "LOWER", "LENGTH", "LEN",
    "TO_DATE", "TO_CHAR", "DATE_FORMAT", "DATE_SUB", "DATE_ADD", "DATEDIFF",
    "MONTHS_BETWEEN", "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND",
    "ROW_NUMBER", "RANK", "DENSE_RANK", "NTILE", "LAG", "LEAD", "FIRST_VALUE",
    "LAST_VALUE", "SUM_OVER", "OVER", "PERCENTILE_APPROX", "GREATEST", "LEAST",
    "DECIMAL", "BIGINT", "INT", "INTEGER", "DOUBLE", "FLOAT", "STRING", "VARCHAR",
    "DATE", "TIMESTAMP", "BOOLEAN", "SIGN", "POWER", "SQRT", "EXP", "LN", "LOG",
    "REPLACE", "REGEXP_REPLACE", "SPLIT", "ARRAY", "MAP", "STRUCT", "EXPLODE",
    "COLLECT_SET", "COLLECT_LIST", "GET_JSON_OBJECT", "JSON_EXTRACT", "MD5",
    "CURRENT_DATE", "CURRENT_TIMESTAMP", "NOW", "UNIX_TIMESTAMP", "FROM_UNIXTIME",
}

#: SQL 关键字（同样不能当字段名替换）
SQL_KEYWORDS = {
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "AS", "CASE", "WHEN", "THEN",
    "ELSE", "END", "GROUP", "BY", "ORDER", "PARTITION", "OVER", "DISTINCT",
    "IS", "NULL", "IN", "LIKE", "BETWEEN", "DESC", "ASC", "ON", "JOIN", "LEFT",
    "RIGHT", "FULL", "INNER", "OUTER", "UNION", "ALL", "LIMIT", "HAVING",
    "QUALIFY", "ROWS", "RANGE", "UNBOUNDED", "PRECEDING", "FOLLOWING", "CURRENT",
    "ROW", "TRUE", "FALSE", "INTERVAL", "EXISTS", "CAST", "STRUCT", "ARRAY",
    "MAP", "VALUES", "INSERT", "INTO", "CREATE", "TABLE", "WITH", "WINDOW",
}

#: 表名/库名限定符前缀：``i.`` / ``db.t.``
_PREFIX_RE = re.compile(r"(?<![\w.$])([A-Za-z_][A-Za-z_0-9]*)\s*\.\s*(?=[A-Za-z_（(])")
#: 结尾的 ``AS 别名``
_AS_ALIAS_RE = re.compile(r"\s+AS\s+`?[\w\u4e00-\u9fff]+`?\s*$", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_OPERATOR_RE = re.compile(r"(?<=[\w\)\]])\s*([+\-*/])\s*(?=[\w\(\[])")


# --------------------------------------------------------------------------- #
# 词典
# --------------------------------------------------------------------------- #
def _default_glossary_path() -> Path:
    return Path(__file__).resolve().parent / "glossary.json"


def _clean_mapping(raw: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            key = str(k).strip().lower()
            val = str(v).strip()
            if key and val:
                out[key] = val
    return out


def _clean_list_mapping(raw: Any) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            vals = [str(x).strip() for x in v] if isinstance(v, (list, tuple)) else [str(v).strip()]
            vals = [x for x in vals if x]
            if k and vals:
                out[str(k).strip()] = vals
    return out


@dataclass
class Glossary:
    """业务词典：字段 / 表 / 词根 / 前缀 / 后缀 的中文映射。"""

    version: str = "0"
    description: str = ""
    domains: List[str] = field(default_factory=list)
    columns: Dict[str, str] = field(default_factory=dict)
    tables: Dict[str, str] = field(default_factory=dict)
    tokens: Dict[str, str] = field(default_factory=dict)
    prefixes: Dict[str, str] = field(default_factory=dict)
    suffixes: Dict[str, str] = field(default_factory=dict)
    metric_keywords: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.columns) + len(self.tables) + len(self.tokens)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Glossary":
        return cls(
            version=str(data.get("version") or "0"),
            description=str(data.get("description") or ""),
            domains=[str(d) for d in (data.get("domains") or [])],
            columns=_clean_mapping(data.get("columns")),
            tables=_clean_mapping(data.get("tables")),
            tokens=_clean_mapping(data.get("tokens")),
            prefixes=_clean_mapping(data.get("prefixes")),
            suffixes=_clean_mapping(data.get("suffixes")),
            metric_keywords=_clean_list_mapping(data.get("metric_keywords")),
        )

    @classmethod
    def load(cls, path: Any) -> "Glossary":
        p = Path(path)
        with p.open(encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    @classmethod
    def load_default(cls) -> "Glossary":
        """内置词典；支持用环境变量 ``KB_GLOSSARY`` 覆盖路径。"""
        custom = os.environ.get("KB_GLOSSARY")
        if custom and Path(custom).exists():
            return cls.load(custom)
        return cls.load(_default_glossary_path())

    def merged_with(self, other: Optional["Glossary"]) -> "Glossary":
        """把另一份词典叠加到本词典之上（后者优先）。"""
        if other is None:
            return self
        return Glossary(
            version=f"{self.version}+{other.version}",
            description=self.description or other.description,
            domains=sorted(set(self.domains) | set(other.domains)),
            columns={**self.columns, **other.columns},
            tables={**self.tables, **other.tables},
            tokens={**self.tokens, **other.tokens},
            prefixes={**self.prefixes, **other.prefixes},
            suffixes={**self.suffixes, **other.suffixes},
            metric_keywords={**self.metric_keywords, **other.metric_keywords},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "description": self.description,
            "domains": self.domains,
            "columns": self.columns,
            "tables": self.tables,
            "tokens": self.tokens,
            "prefixes": self.prefixes,
            "suffixes": self.suffixes,
            "metric_keywords": self.metric_keywords,
        }


@dataclass
class NameHit:
    """一次中文名推断结果。"""

    column: str
    chinese_name: Optional[str] = None
    #: exact_glossary / comment / rule / pending
    source: str = "pending"
    confidence: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.chinese_name)


def split_identifier(name: str) -> List[str]:
    """把 ``total_output_qty`` 拆成 ``["total", "output", "qty"]``。

    同时兼容驼峰（``totalOutputQty``）与数字尾巴（``col_1`` -> ``col`` / ``1``）。
    """
    if not name:
        return []
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name))
    parts = [p for p in re.split(r"[_\-\s]+", text) if p]
    out: List[str] = []
    for part in parts:
        if part.isdigit():
            continue
        out.append(part.lower())
    return out


# --------------------------------------------------------------------------- #
# 中文名推断
# --------------------------------------------------------------------------- #
class ChineseNameResolver:
    """字段 / 表 中文业务名解析器：词典精确命中 → SQL 注释 → 命名规则组合 → 待确认。"""

    #: 命名规则置信度
    CONF_EXACT = 1.0
    CONF_COMMENT = 0.9
    CONF_RULE_FULL = 0.7
    CONF_RULE_PARTIAL = 0.45

    def __init__(self, glossary: Optional[Glossary] = None) -> None:
        self.glossary = glossary or Glossary.load_default()

    # -- 字段 ------------------------------------------------------------- #
    def resolve(self, column: str, comment: Optional[str] = None) -> NameHit:
        """推断一个字段的中文业务名。

        优先级：SQL 行内注释（作者自己写的业务名，最权威）> 内置词典精确命中
        > 命名规则组合（词根/前缀/后缀）。都不命中则返回 ``pending``，留给人工确认。
        """
        if not column:
            return NameHit(column=column or "", chinese_name=None, source="pending", confidence=0.0)

        cleaned_comment, _unit = split_comment_unit(comment)
        key = column.strip().lower()
        gloss = self.glossary.columns.get(key)

        if cleaned_comment:
            # 注释是脚本作者写的业务名，通常最权威；但像「箱转条」这种是**口径说明**
            # 而不是字段名，此时若词典里有正式名（产量（条）），以词典为准，
            # 注释改由 business_desc / 备注承载。
            if not gloss or gloss in cleaned_comment or cleaned_comment in gloss:
                return NameHit(column=column, chinese_name=cleaned_comment,
                               source="comment", confidence=self.CONF_COMMENT)
            return NameHit(column=column, chinese_name=gloss,
                           source="exact_glossary", confidence=self.CONF_EXACT)
        if gloss:
            return NameHit(column=column, chinese_name=gloss,
                           source="exact_glossary", confidence=self.CONF_EXACT)

        guess = self.compose(column)
        if guess:
            parts = split_identifier(column)
            known = sum(1 for p in parts if p in self.glossary.tokens or p in self.glossary.prefixes)
            full = known >= len(parts)
            return NameHit(
                column=column,
                chinese_name=guess,
                source="rule" if full else "rule_partial",
                confidence=self.CONF_RULE_FULL if full else self.CONF_RULE_PARTIAL,
            )
        return NameHit(column=column, chinese_name=None, source="pending", confidence=0.0)

    def compose(self, column: str) -> Optional[str]:
        """命名规则组合：按 ``_`` 拆词，逐个查前缀 / 词根 / 后缀词典后拼接。"""
        parts = split_identifier(column)
        if not parts:
            return None
        pieces: List[str] = []
        unknown = 0
        for idx, part in enumerate(parts):
            is_last = idx == len(parts) - 1
            if idx == 0 and part in self.glossary.prefixes:
                pieces.append(self.glossary.prefixes[part])
                continue
            if is_last and part in self.glossary.suffixes:
                pieces.append(self.glossary.suffixes[part])
                continue
            if part in self.glossary.tokens:
                pieces.append(self.glossary.tokens[part])
                continue
            if part in self.glossary.prefixes:
                pieces.append(self.glossary.prefixes[part])
                continue
            if part in self.glossary.suffixes:
                pieces.append(self.glossary.suffixes[part])
                continue
            unknown += 1
            pieces.append(part)

        if unknown == len(parts):
            return None
        # 完全未知的英文词根会原样保留，避免编造业务含义
        text = "".join(pieces)
        return text if any("\u4e00" <= ch <= "\u9fff" for ch in text) else None

    # -- 表 --------------------------------------------------------------- #
    def resolve_table(self, table_name: str, comment: Optional[str] = None) -> NameHit:
        """推断表的中文名：先看词典，再看 ``COMMENT '...'``，最后看表名里的中文。"""
        if not table_name:
            return NameHit(column="", chinese_name=None, source="pending", confidence=0.0)
        short = table_name.split(".")[-1]
        cleaned, _unit = split_comment_unit(comment)
        if cleaned:
            return NameHit(column=short, chinese_name=cleaned, source="comment", confidence=self.CONF_COMMENT)
        key = short.strip().lower()
        if key in self.glossary.tables:
            return NameHit(column=short, chinese_name=self.glossary.tables[key],
                           source="exact_glossary", confidence=self.CONF_EXACT)
        # 表名里本身带中文（如 dws_产量汇总）
        zh = re.sub(r"[_\-]+", "", re.sub(r"[A-Za-z0-9]+", "", short))
        if zh:
            return NameHit(column=short, chinese_name=zh, source="rule", confidence=self.CONF_RULE_FULL)
        return NameHit(column=short, chinese_name=None, source="pending", confidence=0.0)


# --------------------------------------------------------------------------- #
# 注释清洗
# --------------------------------------------------------------------------- #
_UNIT_RE = re.compile(r"[（(]\s*([^（()）]{1,4})\s*[)）]\s*$")

#: 括号里出现这些字样时，它是「说明」而不是「单位」（如「产量折条（箱转条）」）
_NOT_A_UNIT = re.compile(r"转|折|换算|说明|明细|见|等")

#: 注释里出现这些词，说明作者自己也没定下业务名 → 视为「待确认」，不当作字段名
_UNKNOWN_HINTS = re.compile(
    r"待确认|待补充|待定|待明确|待核实|TBD|TODO|未知|含义不明|业务含义不明|历史遗留", re.IGNORECASE
)


def split_comment_unit(comment: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """把 ``产量（箱）`` 拆成 ``("产量", "箱")``。

    括号里只有 1~4 个字符时视为单位；更长（如 ``税种（消费税/增值税）``）时
    认为是值域说明，整体保留，不拆分。

    注释里若出现「待确认 / 历史遗留 / TODO」这类字样，说明作者也没定业务名，
    此时返回 ``(None, None)``，让调用方走「待确认」流程（而不是把这句话当字段名）。
    """
    if not comment:
        return None, None
    text = str(comment).strip().strip("—-=*# ")
    text = re.sub(r"^(业务含义|说明|含义|备注)\s*[:：]\s*", "", text)
    if not text:
        return None, None
    if _UNKNOWN_HINTS.search(text):
        return None, None
    m = _UNIT_RE.search(text)
    if m and not _NOT_A_UNIT.search(m.group(1)):
        unit = m.group(1).strip()
        body = text[: m.start()].strip()
        if body:
            return body, unit
    return text, None


# --------------------------------------------------------------------------- #
# 表达式处理
# --------------------------------------------------------------------------- #
def normalize_expression(expression: str) -> str:
    """归一化 SQL 表达式：去表别名前缀 + 去结尾 ``AS 别名`` + 规范空白与运算符。

    ``SUM(i.output_qty) AS output_qty``            -> ``SUM(output_qty)``
    ``ROUND(s.sale_qty / NULLIF(s.output_qty,0),4)`` -> ``ROUND(sale_qty / NULLIF(output_qty, 0), 4)``
    """
    if not expression:
        return ""
    text = str(expression).strip().rstrip(";")
    text = _AS_ALIAS_RE.sub("", text)
    prev = None
    while prev != text:  # db.table.column 需要两轮
        prev = text
        text = _PREFIX_RE.sub("", text)
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s*\(\s*", "(", text)
    text = re.sub(r"\s*\)", ")", text)
    text = _OPERATOR_RE.sub(r" \1 ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _find_func_calls(expr: str, func: str) -> List[Tuple[int, int]]:
    """返回 ``func(`` 到配对右括号的区间列表（大小写不敏感）。"""
    spans: List[Tuple[int, int]] = []
    pattern = re.compile(rf"\b{re.escape(func)}\s*\(", re.IGNORECASE)
    for m in pattern.finditer(expr):
        depth = 0
        start = m.start()
        open_idx = expr.index("(", m.start())
        for i in range(open_idx, len(expr)):
            ch = expr[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    spans.append((start, i + 1))
                    break
    return spans


def strip_uniform_aggregates(expr: str) -> Optional[str]:
    """当表达式里所有聚合函数都是同一个时，剥掉聚合外壳。

    ``SUM(dama_qty) + SUM(tiaoma_qty) - SUM(chongma_qty)`` -> ``dama_qty + tiaoma_qty - chongma_qty``

    只在「聚合函数一致、且表达式里没有其它非聚合列引用」时生效，否则返回 ``None``
    （宁可不剥，也不给出误导性的口径）。
    """
    if not expr:
        return None
    upper = expr.upper()
    used = {f for f in AGGREGATE_FUNCS if re.search(rf"\b{f}\s*\(", upper)}
    if len(used) != 1:
        return None
    func = next(iter(used))
    spans = _find_func_calls(expr, func)
    if not spans:
        return None

    out = []
    cursor = 0
    for start, end in spans:
        inner = expr[expr.index("(", start) + 1 : end - 1]
        # COUNT(1) / COUNT(*) 这类没有业务字段，保持原样
        if inner.strip() in ("1", "*", "0") or "," in inner:
            return None
        out.append(expr[cursor:start])
        out.append(inner.strip())
        cursor = end
    out.append(expr[cursor:])
    stripped = "".join(out)

    # 剥完之后不能再出现聚合函数（否则是嵌套聚合，属于复杂场景，保持原样）
    if any(re.search(rf"\b{f}\s*\(", stripped.upper()) for f in AGGREGATE_FUNCS):
        return None
    # 表达式中若还有 DISTINCT 之类修饰，保持原样
    if "DISTINCT" in stripped.upper():
        return None
    stripped = _WS_RE.sub(" ", stripped).strip()
    return stripped or None


_IDENT_RE = re.compile(r"(?<![\w.$])([A-Za-z_][A-Za-z_0-9]*)(?![\w$]*\s*\()")


def localize_expression(
    expression: str,
    name_map: Optional[Dict[str, str]] = None,
    extra_names: Optional[Dict[str, str]] = None,
) -> str:
    """把表达式里的字段名替换成中文业务名（未命中词典的保持英文原名）。"""
    mapping: Dict[str, str] = {}
    for src in (name_map or {}, extra_names or {}):
        for k, v in src.items():
            if v:
                mapping[str(k).lower()] = str(v)
    if not expression:
        return ""

    def _sub(m: re.Match) -> str:
        token = m.group(1)
        upper = token.upper()
        if upper in FUNC_NAMES or upper in SQL_KEYWORDS:
            return token
        return mapping.get(token.lower(), token)

    return _WS_RE.sub(" ", _IDENT_RE.sub(_sub, expression)).strip()
