"""简易问数骨架：自然语言问题 → 口径 / 血缘 / 术语答案。

两条路径：
1. **规则模式（默认，零依赖、离线可用）**：正则识别意图（口径怎么算 / 用在哪些表 /
   从哪来 / 有什么字段 / 术语含义 / 指标清单），再从知识库里取证据拼答案；
2. **LLM 模式（可选，可插拔）**：设置 ``LLM_API_KEY``（可选 ``LLM_BASE_URL`` /
   ``LLM_MODEL``）后，把「检索到的知识」作为上下文交给 OpenAI 兼容接口润色成答案。
   **任何异常都会静默降级回规则模式**，绝不让问答不可用。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from lineage.knowledge.search import search
from lineage.knowledge.store import KnowledgeStore

__all__ = [
    "answer",
    "format_metric",
    "LLMSettings",
    "LLMClient",
    "detect_intent",
    "extract_entity",
    "INTENT_LABELS",
]

INTENT_LABELS = {
    "metric_formula": "指标口径查询",
    "metric_usage": "指标/字段使用方查询",
    "upstream": "上游溯源",
    "downstream": "下游影响",
    "term_meaning": "术语解释",
    "list_metrics": "指标清单",
    "table_fields": "表字段清单",
    "search": "关键词检索",
    "empty": "空问题",
}

#: 意图识别规则（顺序敏感：越具体的越靠前）
_INTENT_RULES: Sequence[Tuple[str, str]] = (
    ("list_metrics", r"(有哪些指标|指标清单|口径清单|所有口径|统计了哪些|有哪些口径|指标有哪些)"),
    ("metric_formula", r"(怎么算|如何算|怎么计算|如何计算|计算公式|公式是|口径是|的口径|怎么得出来|怎么来的|定义是什么|是什么意思的指标)"),
    ("table_fields", r"([\w\u4e00-\u9fff.]+)(表|table)?\s*(有哪些字段|的字段|字段清单|表结构)"),
    ("metric_usage", r"(哪些表|哪张表|谁|哪里|何处)[^,，。?？]{0,12}(用到|使用|引用|消费|读取|包含|依赖)"),
    ("metric_usage", r"([\w\u4e00-\u9fff]+)\s*(用在哪些表|被哪些表|出现在哪些表|被谁用|用在哪儿|用在哪些地方)"),
    ("upstream", r"([\w\u4e00-\u9fff]+)\s*(从哪来|来自哪里|从哪来|上游是|上游|来源是|数据来源|是从哪)"),
    ("downstream", r"([\w\u4e00-\u9fff]+)\s*(下游|影响哪些|影响什么|改了会怎样|变更影响)"),
    ("term_meaning", r"([\w\u4e00-\u9fff]+)\s*(是什么意思|的含义|代表什么|是什么指标|指什么)"),
)

_FILLER_RE = re.compile(
    r"^(请问|问一下|帮我|帮忙|我想知道|我想问|麻烦|那个|这个|我们的|咱们)+"
)
_TAIL_RE = re.compile(r"(呢|吗|啊|呀|吧|的|了|是多少|是什么|怎么算的|怎么算)$")
_ENTITY_NOISE = {
    "指标", "口径", "字段", "表", "数据", "数", "值", "量", "统计", "含义", "意思",
    "什么", "哪些", "怎么", "如何", "计算", "公式", "定义", "来源", "上游", "下游",
    "是谁", "哪里", "哪个", "多少", "谁", "业务", "关系",
}

#: 各指标类型的「口径丰富度」权重（用于同名词多条口径时排序）
_TYPE_RICHNESS = {
    "比率": 3.0,
    "条件分支": 2.6,
    "窗口函数": 2.4,
    "算术计算": 2.0,
    "聚合": 1.8,
    "函数转换": 1.0,
    "直取": 0.2,
}


def _clean(value: str) -> str:
    text = (value or "").strip().strip("“”\"'‘’《》【】()（）[] ")
    text = _TAIL_RE.sub("", text).strip()
    text = _FILLER_RE.sub("", text).strip()
    return text


def detect_intent(question: str) -> str:
    """识别问题意图（规则匹配，可解释）。"""
    if not question or not question.strip():
        return "empty"
    for intent, pattern in _INTENT_RULES:
        if re.search(pattern, question):
            return intent
    return "search"


def extract_entity(question: str) -> str:
    """从问题里抠出被问的对象（指标名 / 字段名 / 表名）。"""
    text = _clean(question)
    patterns = (
        r"^(?:哪些|哪张|哪个|谁|哪里|何处)[^,，。?？]{0,6}(?:表|字段|指标)?[^,，。?？]{0,8}"
        r"(?:用到了?|使用了?|引用了?|消费了?|读取了?|包含了?|依赖了?|出现了?)",
        r"(?:用|出现)在(?:哪些|哪张|哪个)(?:表|地方)",
        r"(?:怎么算|如何算|怎么计算|如何计算)(?:的)?$",
        r"的(?:口径|公式|定义|含义|意思)$",
        r"(?:用|出现)在(?:哪些|哪张|哪个)表",
        r"被(?:哪些|哪张|谁)",
        r"(?:从哪来|来自哪里|上游是|上游|来源是|数据来源|下游|影响哪些|影响什么)",
        r"(?:有|是)哪些字段$",
        r"的字段$",
        r"(?:是什么意思|的含义|代表什么|是什么指标|指什么)$",
        r"是什么$",
    )
    for pattern in patterns:
        text = re.sub(pattern, "", text).strip()
    # 去掉「的」「呢」等虚词与嘈杂词
    text = re.sub(r"[的了吗呢啊呀吧。，,?？!！]", "", text)
    for noise in sorted(_ENTITY_NOISE, key=len, reverse=True):
        if text == noise:
            return ""
    return text.strip()


# --------------------------------------------------------------------------- #
# LLM（可插拔，无 key 自动降级）
# --------------------------------------------------------------------------- #
@dataclass
class LLMSettings:
    """LLM 配置：全部来自环境变量，不硬编码任何密钥。"""

    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    timeout: float = 20.0

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "LLMSettings":
        env = env if env is not None else os.environ
        return cls(
            api_key=(env.get("LLM_API_KEY") or "").strip(),
            base_url=(env.get("LLM_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/"),
            model=(env.get("LLM_MODEL") or "gpt-4o-mini").strip(),
            timeout=float(env.get("LLM_TIMEOUT") or 20.0),
        )

    @property
    def available(self) -> bool:
        return bool(self.api_key) and bool(self.base_url)


class LLMClient:
    """OpenAI 兼容 ``/chat/completions`` 客户端（标准库 urllib，零额外依赖）。"""

    def __init__(self, settings: Optional[LLMSettings] = None) -> None:
        self.settings = settings or LLMSettings.from_env()

    @property
    def available(self) -> bool:
        return self.settings.available

    def complete(self, system: str, user: str) -> Optional[str]:
        """调用 LLM；任何异常都返回 ``None``（由调用方降级到规则模式）。"""
        if not self.available:
            return None
        payload = json.dumps({
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.settings.base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
            return None
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            return None


_SYSTEM_PROMPT = (
    "你是烟草行业数仓的业务口径助手。只能依据给定的知识库片段回答，"
    "不要编造字段、表或公式；片段里没有的内容要明确说「知识库里没有」。"
    "回答用中文，先给结论（口径公式），再给来源脚本与依赖字段。"
)


# --------------------------------------------------------------------------- #
# 规则模式
# --------------------------------------------------------------------------- #
def _rank_metrics(rows: Sequence[Dict[str, Any]], entity: str) -> List[Dict[str, Any]]:
    """同名词多条口径的排序：中文名精确 > 口径丰富度 > 置信度 > 表名。"""
    key = (entity or "").lower()

    def sort_key(row: Dict[str, Any]) -> Tuple:
        exact = 1 if (row.get("chinese_name") or "").lower() == key else 0
        body = row.get("formula") or ""
        ops = len(re.findall(r"[+\-*/]", body))
        richness = _TYPE_RICHNESS.get(row.get("metric_type") or "", 0.5) + min(ops, 4) * 0.4
        return (-exact, -richness, -float(row.get("confidence") or 0), row.get("table_name") or "")

    return sorted(rows, key=sort_key)


def _fmt_metric(row: Dict[str, Any], with_path: Sequence[str] = ()) -> str:
    deps = row.get("depends_on") or []
    dep_txt = "、".join(
        f"{d.get('table')}.{d.get('column')}({d.get('chinese_name') or '未定名'})"
        if d.get("table") else f"{d.get('column')}({d.get('chinese_name') or '常量'})"
        for d in deps if isinstance(d, dict)
    ) or "（无上游字段，常量口径）"
    lines = [
        f"口径：{row.get('formula')}",
        f"     忠实表达式：{row.get('formula_full')}",
        f"指标名：{row.get('chinese_name') or '(待确认)'} / {row.get('metric_name')}",
        f"所属表：{row.get('table_name')}（{row.get('layer')} 层）",
        f"口径类型：{row.get('metric_type')}"
        + (f"（聚合函数 {row.get('aggregate_func')}）" if row.get("aggregate_func") else ""),
        f"依赖字段：{dep_txt}",
        f"来源脚本：{row.get('source_file')} 第 {row.get('source_stmt')} 条语句"
        f"（{row.get('source_task_type')}）；责任人：{row.get('owner') or '待指定'}；版本：{row.get('version')}",
        f"置信度：{row.get('confidence')}",
    ]
    if row.get("notes"):
        lines.append(f"备注：{row.get('notes')}")
    if row.get("expression_raw"):
        lines.append(f"实现 SQL 片段：{row.get('expression_raw')}")
    if with_path:
        lines.append("血缘链路（上游 → 本表）：")
        for path in with_path[:3]:
            lines.append("    " + "  →  ".join(path))
    return "\n".join(lines)


def _answer_metric_formula(store: KnowledgeStore, entity: str) -> Dict[str, Any]:
    rows = _rank_metrics(store.get_metric(entity), entity)
    if not rows:
        return {"answer": f"知识库里没有找到与「{entity}」相关的指标口径。"
                          f"\n建议：用 `kb search {entity}` 做模糊检索，确认指标/字段名。",
                "evidence": {"entity": entity, "metrics": []}}
    top = rows[0]
    parts = [f"【{top.get('chinese_name') or top.get('metric_name')}】计算口径（共 {len(rows)} 条相关口径，列前 {min(len(rows), 3)} 条）", ""]
    for idx, row in enumerate(rows[:3], start=1):
        path = store.upstream_paths(row["table_name"], depth=8, max_paths=2) if idx == 1 else []
        parts.append(f"[{idx}] " + _fmt_metric(row, path))
        parts.append("")
    return {"answer": "\n".join(parts).strip(),
            "evidence": {"entity": entity, "metrics": rows[:5]}}


def _answer_metric_usage(store: KnowledgeStore, entity: str) -> Dict[str, Any]:
    """哪些表用到了 X：字段维度 + 口径依赖维度。"""
    result = search(store, entity, kinds=("fields", "metrics"), limit=50)
    fields = result["groups"].get("fields") or []
    hit_fields = [f for f in fields if (f.get("column_name") or "").lower() == entity.lower()
                  or (f.get("chinese_name") or "") == entity] or fields[:10]
    tables: Dict[str, List[str]] = {}
    for item in hit_fields:
        tables.setdefault(item["table_name"], []).append(
            f"{item['column_name']}({item.get('chinese_name') or '未定名'})"
        )
    parts = [f"「{entity}」出现在 {len(tables)} 张表的字段定义里：", ""]
    for table, cols in sorted(tables.items()):
        parts.append(f"  - {table}：{'、'.join(sorted(set(cols)))}")
    # 哪些口径依赖它
    deps_used: List[str] = []
    for metric in store.metrics():
        for dep in metric.get("depends_on") or []:
            if not isinstance(dep, dict):
                continue
            if (dep.get("column") or "").lower() == entity.lower() or \
                    (dep.get("chinese_name") or "") == entity:
                deps_used.append(f"{metric.get('formula')}  @ {metric.get('table_name')}"
                                 f"（{metric.get('source_file')}）")
                break
    if deps_used:
        parts.append("")
        parts.append(f"另有 {len(deps_used)} 条指标口径直接依赖它：")
        for line in deps_used[:10]:
            parts.append(f"  - {line}")
    if not tables and not deps_used:
        parts.append("（没有命中：换关键词试试，或先 kb build 建库）")
    return {"answer": "\n".join(parts),
            "evidence": {"entity": entity, "tables": {k: sorted(set(v)) for k, v in tables.items()},
                         "dependent_metrics": deps_used[:20]}}


def _answer_lineage(store: KnowledgeStore, entity: str, up: bool) -> Dict[str, Any]:
    table = store.resolve_table_name(entity)
    paths = store.upstream_paths(table) if up else store.downstream_paths(table)
    direction = "上游" if up else "下游"
    if not paths:
        return {"answer": f"知识库里没有找到「{entity}」所在表（或它没有{direction}血缘）。"
                          f"\n可用 `kb search {entity}` 先确认表名。",
                "evidence": {"entity": entity, "table": table, "paths": []}}
    parts = [f"「{table}」的{direction}血缘（{len(paths)} 条链路）：", ""]
    for path in paths:
        ordered = path if up else list(reversed(path))
        parts.append("  " + "  →  ".join(ordered))
    return {"answer": "\n".join(parts),
            "evidence": {"entity": entity, "table": table, "paths": paths}}


def _answer_table_fields(store: KnowledgeStore, entity: str) -> Dict[str, Any]:
    table = store.get_table(entity)
    if not table:
        return {"answer": f"知识库里没有表「{entity}」。可用 `kb search {entity}` 检索。",
                "evidence": {"entity": entity, "fields": []}}
    fields = store.fields(table["table_name"])
    parts = [
        f"表 {table['table_name']}（{table.get('chinese_name') or '待确认'}，{table.get('layer')} 层）共 {len(fields)} 个字段：",
        "",
    ]
    for field in fields:
        parts.append(f"  - {field['column_name']:<24} {field.get('chinese_name') or '(待确认)'}"
                     f"   [{field.get('chinese_source')}]")
    return {"answer": "\n".join(parts), "evidence": {"table": table, "fields": fields}}


def _answer_term_meaning(store: KnowledgeStore, entity: str) -> Dict[str, Any]:
    result = search(store, entity, kinds=("terms", "metrics"), limit=15)
    terms = result["groups"].get("terms") or []
    if not terms:
        return {"answer": f"知识库里没有术语「{entity}」的解释。",
                "evidence": {"entity": entity, "terms": []}}
    parts = []
    for item in terms[:5]:
        parts.append(
            f"「{item.get('term')}」=> {item.get('chinese_name') or '(待确认)'}"
            f"（来源：{item.get('source')}，置信度 {item.get('confidence')}，"
            f"出现 {item.get('occurrences')} 次，涉及表：{'、'.join(item.get('tables') or []) or '—'}）"
        )
    metrics = result["groups"].get("metrics") or []
    if metrics:
        parts.append("")
        parts.append("相关指标口径：")
        for item in metrics[:3]:
            parts.append(f"  - {item.get('formula')}  @ {item.get('table_name')}")
    return {"answer": "\n".join(parts), "evidence": {"entity": entity, "terms": terms[:10]}}


def _answer_list_metrics(store: KnowledgeStore) -> Dict[str, Any]:
    summary = store.summary()
    metrics = store.metrics()
    parts = [
        f"知识库共 {len(metrics)} 条指标口径，覆盖 {summary['counts']['kb_tables']} 张表、"
        f"{summary['counts']['kb_fields']} 个字段、{summary['counts']['kb_terms']} 条业务术语。",
        "",
        "按分层分布：" + "  ".join(f"{k}={v}" for k, v in summary["metric_by_layer"].items()),
        "按类型分布：" + "  ".join(f"{k}={v}" for k, v in summary["metric_by_type"].items()),
        "",
        "口径清单（前 30 条）：",
    ]
    for item in metrics[:30]:
        parts.append(f"  - {item.get('formula')}   @ {item['table_name']}"
                     f"  [{item.get('metric_type')}]")
    return {"answer": "\n".join(parts), "evidence": {"summary": summary, "metrics": metrics[:30]}}


def _answer_search(store: KnowledgeStore, entity: str) -> Dict[str, Any]:
    result = search(store, entity, limit=10)
    if not result["total"]:
        return {"answer": f"没有检索到「{entity}」相关的知识条目。"
                          f"\n可以试：kb summary 看库里有什么，或换个关键词。",
                "evidence": result}
    counts = result["counts"]
    parts = [f"「{entity}」命中："
             + "  ".join(f"{k}={counts.get(k, 0)}" for k in ("metrics", "fields", "tables", "terms", "rules")),
             ""]
    for item in (result["groups"].get("metrics") or [])[:5]:
        parts.append(f"  [口径] {item.get('formula')}  @ {item['table_name']}")
    for item in (result["groups"].get("fields") or [])[:5]:
        parts.append(f"  [字段] {item['table_name']}.{item['column_name']} = "
                     f"{item.get('chinese_name') or '待确认'}")
    for item in (result["groups"].get("tables") or [])[:5]:
        parts.append(f"  [表] {item['table_name']} = {item.get('chinese_name') or '待确认'}")
    for item in (result["groups"].get("terms") or [])[:5]:
        parts.append(f"  [术语] {item.get('term')} = {item.get('chinese_name') or '待确认'}")
    return {"answer": "\n".join(parts), "evidence": result}


def format_metric(store: KnowledgeStore, row: Dict[str, Any], max_paths: int = 3) -> str:
    """渲染单条口径的详情（CLI ``kb show`` 用）。"""
    paths = store.upstream_paths(row.get("table_name") or "", depth=8, max_paths=max_paths)
    return _fmt_metric(row, paths)


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def answer(
    store: KnowledgeStore,
    question: str,
    use_llm: Any = "auto",
    llm: Optional[LLMClient] = None,
) -> Dict[str, Any]:
    """回答一个自然语言问题。

    参数：
        use_llm: ``"auto"``（有 key 就用）/ ``True`` / ``False``。
    返回：
        ``{"question", "intent", "intent_label", "entity", "answer", "mode", "llm", "evidence"}``
    """
    question = (question or "").strip()
    intent = detect_intent(question)
    entity = extract_entity(question)
    client = llm or LLMClient()

    if intent == "list_metrics":
        payload = _answer_list_metrics(store)
    elif intent == "metric_formula":
        payload = _answer_metric_formula(store, entity)
    elif intent == "metric_usage":
        payload = _answer_metric_usage(store, entity)
    elif intent == "upstream":
        payload = _answer_lineage(store, entity, up=True)
    elif intent == "downstream":
        payload = _answer_lineage(store, entity, up=False)
    elif intent == "table_fields":
        payload = _answer_table_fields(store, entity)
    elif intent == "term_meaning":
        payload = _answer_term_meaning(store, entity)
    elif intent == "empty":
        payload = {"answer": "请把问题说清楚一点，例如：产量怎么算的 / 哪些表用到了打码量。",
                   "evidence": {}}
    else:
        payload = _answer_search(store, entity or question)

    mode = "rule"
    want_llm = use_llm is True or (use_llm == "auto" and client.available)
    if want_llm:
        refined = _refine_with_llm(client, question, payload.get("answer") or "")
        if refined:
            payload = {**payload, "answer": refined}
            mode = "llm"
        else:
            mode = "rule(llm 不可用/失败，已降级)"

    return {
        "question": question,
        "intent": intent,
        "intent_label": INTENT_LABELS.get(intent, intent),
        "entity": entity,
        "answer": payload.get("answer", ""),
        "mode": mode,
        "llm": {
            "enabled": client.available,
            "model": client.settings.model if client.available else None,
            "base_url": client.settings.base_url if client.available else None,
        },
        "evidence": payload.get("evidence", {}),
    }


def _refine_with_llm(client: LLMClient, question: str, rule_answer: str) -> Optional[str]:
    context = rule_answer[:4000]
    return client.complete(
        _SYSTEM_PROMPT,
        f"问题：{question}\n\n知识库检索结果（规则模式已整理）：\n{context}\n\n请基于以上内容回答。",
    )
