"""离线角色假模型：分别模拟 检索(DeepSeek V4)/质量(GLM)/知识(gpt-5.6) 三路 LLM 输出。

用于 --smoke 全链路演示与离线测试：不联网、无 API Key 也可验证
“三节点均由 LLM 驱动”的编排路径。
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage

from research_agent.knowledge.fake import StaticJsonModel


class FakeRetrieverModel:
    """检索节点假 LLM：规划检索式 + 返回可解析的元数据规整结果。"""

    def __init__(self, queries: list[str] | None = None) -> None:
        self.queries = queries or ["retrieval augmented generation", "RAG survey"]
        self.calls: list[dict] = []

    def invoke(self, messages, **kwargs) -> AIMessage:
        prompt = messages[0].content if messages else ""
        self.calls.append({"kind": "retriever", "prompt": prompt[:200]})
        if "JSON 数组" in prompt:
            return AIMessage(content=json.dumps(self.queries, ensure_ascii=False))
        # 元数据规整：返回空对象即视为“已规整”（保持原值，路径走通即可）
        return AIMessage(content="{}")


class FakeQualityModel:
    """质量节点假 LLM：输出子项评分（A/T/Q 由代码按公式计算）。"""

    def __init__(self, factors: dict | None = None) -> None:
        self.factors = factors or {
            "venue_quartile": "Q1",
            "venue_factor": 0.95,
            "h_factor": 0.80,
            "citation_factor": 0.95,
            "field_velocity": "medium",
            "venue_note": "顶级期刊，假模型判断为 Q1",
            "rationale": "方法新颖、实验充分、引用可观（假模型评审意见）",
        }
        self.calls: list[dict] = []

    def invoke(self, messages, **kwargs) -> AIMessage:
        prompt = messages[0].content if messages else ""
        self.calls.append({"kind": "quality", "prompt": prompt[:200]})
        return AIMessage(content=json.dumps(self.factors, ensure_ascii=False))


def make_role_fake(role: str):
    """返回角色对应的假模型。"""
    if role == "retriever":
        return FakeRetrieverModel()
    if role == "quality":
        return FakeQualityModel()
    if role == "knowledge":
        return StaticJsonModel()
    raise ValueError(f"未知角色: {role}")
