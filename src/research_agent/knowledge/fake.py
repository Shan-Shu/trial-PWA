"""离线假模型：返回固定 JSON，用于 CLI --smoke 与无 Key 端到端验证。"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage


SAMPLE_EXTRACTION = {
    "entities": [
        {"type": "Method", "name": "Retrieval-Augmented Generation",
         "aliases": ["RAG"], "attributes": {"paradigm": "retrieval + generation"},
         "confidence": 0.9, "evidence": "论文提出 Retrieval-Augmented Generation (RAG)。"},
        {"type": "Dataset", "name": "Natural Questions",
         "aliases": [], "attributes": {"language": "English"},
         "confidence": 0.85, "evidence": "在 Natural Questions 数据集上评测。"},
        {"type": "Organization", "name": "Meta AI",
         "aliases": [], "attributes": {},
         "confidence": 0.8, "evidence": "作者单位 Meta AI。"},
    ],
    "relations": [
        {"type": "evaluates", "subject": "Retrieval-Augmented Generation",
         "predicate": "在数据集上评测", "object": "Natural Questions",
         "confidence": 0.88, "evidence": "在 Natural Questions 数据集上评测。"},
        {"type": "authored_by", "subject": "Retrieval-Augmented Generation",
         "predicate": "由……提出", "object": "Meta AI",
         "confidence": 0.8, "evidence": "作者单位 Meta AI。"},
    ],
    "events": [
        {"type": "Experiment", "trigger": "在 Natural Questions 上评测 RAG",
         "participants": ["Retrieval-Augmented Generation", "Natural Questions"],
         "time": None, "confidence": 0.86,
         "evidence": "实验在 Natural Questions 上评测了 RAG。"},
    ],
}


class StaticJsonModel:
    """确定性模型：无论输入返回同一 JSON（演示/冒烟用）。"""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or SAMPLE_EXTRACTION

    def invoke(self, messages, **kwargs) -> AIMessage:
        return AIMessage(content=json.dumps(self.payload, ensure_ascii=False))
