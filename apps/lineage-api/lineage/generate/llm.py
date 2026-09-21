"""可插拔 LLM（模板引擎为主，LLM 只做「评审 / 候选筛选」）。

设计原则（安全第一）
--------------------
* **模板永远是真身**：没有 ``LLM_API_KEY`` 时生成器完全可用，不报错、不降级成空结果；
* LLM 只做两件**不产生新事实**的事：
  1. :meth:`GenerateLLM.review_sql`：对已生成的 SQL 提改进建议（**不改写 SQL**）；
  2. :meth:`GenerateLLM.pick_tables`：从**候选表清单**里挑最相关的表（返回值必须命中候选清单，
     编出来的表名直接丢弃）。
* 任何异常都静默跳过（返回 ``None``），绝不影响生成主流程。

环境变量：``LLM_API_KEY`` / ``LLM_BASE_URL`` / ``LLM_MODEL``（与问数模块共用）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

from lineage.knowledge.qa import LLMClient, LLMSettings

__all__ = ["GenerateLLM"]

_SQL_SYSTEM = (
    "你是烟草行业数仓的 SQL 评审专家。只做评审，不要重写 SQL、不要编造字段。"
    "输出 2~5 条中文要点，每条一行，以「- 」开头；只指出真实存在的问题："
    "字段来源是否可追溯、聚合粒度是否一致、分区/去重/空值处理是否遗漏。"
    "如果没发现问题就回一条「- 未发现明显问题」。"
)

_PICK_SYSTEM = (
    "你是数仓建模助手。只能从用户给出的候选表清单里选择，禁止编造表名。"
    "输出 JSON：{\"tables\": [\"库.表\", ...], \"reason\": \"一句话中文理由\"}。"
    "最多选 8 张表。"
)


class GenerateLLM:
    """LLM 包装（可插拔；不可用时 :attr:`available` 为 ``False``）。"""

    def __init__(self, enabled: Any = "auto", client: Optional[LLMClient] = None,
                 settings: Optional[LLMSettings] = None) -> None:
        self.enabled = enabled
        self._client = client or LLMClient(settings)

    # -- 状态 -------------------------------------------------------------- #
    @property
    def configured(self) -> bool:
        """是否配置了 key（决定 ``auto`` 模式下是否启用）。"""
        return self._client.available

    @property
    def available(self) -> bool:
        if self.enabled in (False, "off", "false", "0", 0):
            return False
        if not self.configured:
            return False
        return True

    def info(self) -> Dict[str, Any]:
        return {
            "configured": self.configured,
            "used": self.available,
            "model": self._client.settings.model if self.configured else None,
            "base_url": self._client.settings.base_url if self.configured else None,
            "env": "LLM_API_KEY / LLM_BASE_URL / LLM_MODEL",
        }

    # -- 能力 1：评审 SQL --------------------------------------------------- #
    def review_sql(self, sql: str, context: str = "") -> Optional[List[str]]:
        """让 LLM 评审 SQL，返回中文要点列表；不可用/失败返回 ``None``。"""
        if not self.available:
            return None
        text = self._client.complete(
            _SQL_SYSTEM,
            f"待评审 SQL：\n{sql}\n\n背景（知识库依据）：\n{context[:3000]}",
        )
        if not text:
            return None
        notes: List[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            line = line.lstrip("-*• ").strip()
            if line:
                notes.append(line)
        return notes[:8] or None

    # -- 能力 2：从候选表里挑表 --------------------------------------------- #
    def pick_tables(self, requirement: str, candidates: Sequence[str]) -> Optional[Dict[str, Any]]:
        """从候选清单里挑表；返回的表名一律与候选清单逐字比对，编造的一律丢弃。"""
        if not self.available or not candidates:
            return None
        listing = "\n".join(f"- {c}" for c in candidates)
        text = self._client.complete(
            _PICK_SYSTEM,
            f"业务需求：{requirement}\n\n候选表（只能从中选）：\n{listing}",
        )
        if not text:
            return None
        picked: List[str] = []
        for name in re.findall(r"[A-Za-z_][\w]*\.[\w\u4e00-\u9fff]+", text):
            if name in candidates and name not in picked:
                picked.append(name)
        if not picked:
            return None
        reason = ""
        try:
            payload = json.loads(text[text.index("{"):text.rindex("}") + 1])
            if isinstance(payload, dict):
                reason = str(payload.get("reason") or "")
        except (ValueError, TypeError):
            reason = ""
        return {"tables": picked[:8], "reason": reason, "raw": text[:500]}
