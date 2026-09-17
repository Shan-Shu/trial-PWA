# -*- coding: utf-8 -*-
"""知识提取二次精修的回归测试。

说明：原文件使用模块级 ``def test_xxx()`` + 裸 ``assert``，而
``python -m unittest discover -s tests`` 只收集 ``TestCase`` 子类中的
``test_*`` 方法 —— 这 7 个测试此前**从未被执行**（issue P1-2）。
现改为 ``TestCase``，执行命令保持不变。
"""
from __future__ import annotations

import json
import unittest

from langchain_core.messages import AIMessage

from research_agent.config import Settings
from research_agent.knowledge.extractor import (
    KnowledgeExtractor,
    REFINE_PROMPT,
    build_prompt,
    flag_issues,
)


DIRTY = {
    "entities": [
        {"type": "Material", "name": "Calcium phosphate cement",
         "aliases": ["CPC"], "attributes": {},
         "confidence": 0.9, "evidence": "采用磷酸钙骨水泥(CPC)修复骨缺损。"},
        {"type": "Concept", "name": "These results suggest that CPC promotes osteogenesis",
         "aliases": [], "attributes": {},
         "confidence": 0.7, "evidence": "These results suggest that CPC promotes osteogenesis."},
    ],
    "relations": [
        {"type": "related_to", "subject": "Calcium phosphate cement",
         "predicate": "促进成骨", "object": "osteogenesis",
         "confidence": 0.6, "evidence": "These results suggest that CPC promotes osteogenesis."},
    ],
    "events": [],
}


CLEAN = {
    "entities": [
        {"type": "Material", "name": "Calcium phosphate cement",
         "aliases": ["CPC"], "attributes": {},
         "confidence": 0.9, "evidence": "采用磷酸钙骨水泥(CPC)修复骨缺损。"},
        {"type": "BiologicalProcess", "name": "osteogenesis",
         "aliases": [], "attributes": {},
         "confidence": 0.9, "evidence": "CPC promotes osteogenesis."},
    ],
    "relations": [
        {"type": "promotes", "subject": "Calcium phosphate cement",
         "predicate": "促进成骨", "object": "osteogenesis",
         "confidence": 0.9, "evidence": "CPC promotes osteogenesis."},
    ],
    "events": [],
}


class ScriptedModel:
    """按脚本依次返回内容的假模型。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[str] = []

    def invoke(self, messages, **kwargs) -> AIMessage:
        self.calls.append(messages[0].content if messages else "")
        if not self.responses:
            raise RuntimeError("没有更多脚本响应")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return AIMessage(content=nxt)


def _settings() -> Settings:
    s = Settings()
    s.knowledge_refine_enabled = True
    s.refine_min_conf = 0.75
    s.refine_max_items = 12
    s.refine_max_attempts = 2
    return s


def _extractor(model) -> KnowledgeExtractor:
    return KnowledgeExtractor(model, _settings())


def _core(data: dict) -> dict:
    """只取抽取结果的核心字段做比较。

    注意事项：抽取结果自 v0.3.0 起还会带 ``hyperedges``（科研超边）等字段，
    因此不能用 ``data == DIRTY`` 这种全等断言（否则 schema 一扩展测试就误报）。
    """
    return {
        "entities": data.get("entities"),
        "relations": data.get("relations"),
        "events": data.get("events"),
    }


class ExtractorRefineTest(unittest.TestCase):
    def test_flag_issues_detects_reporting_entity_low_conf_and_generic(self):
        # min_conf=0.95：让 0.9 置信度的正常实体也被点名“低置信”，覆盖三类问题
        issues = flag_issues(DIRTY, min_conf=0.95, max_items=12)
        self.assertTrue(any("These results suggest" in it for it in issues),
                        f"未检出报告语实体: {issues}")
        self.assertTrue(any("置信度偏低" in it for it in issues),
                        f"未检出低置信: {issues}")
        self.assertTrue(any("related_to" in it and "偏泛化" in it for it in issues),
                        f"未检出泛化关系: {issues}")

    def test_extract_with_refine_improves_dirty_output(self):
        model = ScriptedModel([json.dumps(DIRTY, ensure_ascii=False),
                               json.dumps(CLEAN, ensure_ascii=False)])
        data, stats = _extractor(model).extract_with_refine(
            ["CPC promotes osteogenesis in bone defect repair."])
        self.assertGreaterEqual(stats["issues"], 2)
        self.assertEqual(stats["attempts"], 1)
        self.assertTrue(stats["refined"])
        self.assertEqual(data["relations"][0]["type"], "promotes")
        names = {e["name"] for e in data["entities"]}
        self.assertNotIn("These results suggest that CPC promotes osteogenesis",
                         names)
        # 首遍提示词包含 ERROR LIST，精修提示词包含“定向精修”
        self.assertIn("硬性禁区", model.calls[0])
        self.assertIn("定向精修", model.calls[1])

    def test_extract_without_refine_when_clean(self):
        model = ScriptedModel([json.dumps(CLEAN, ensure_ascii=False)])
        data, stats = _extractor(model).extract_with_refine(
            ["CPC promotes osteogenesis in bone defect repair."])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(stats["issues"], 0)
        self.assertFalse(stats["refined"])
        self.assertEqual(data["relations"][0]["type"], "promotes")

    def test_refine_converges_when_unchanged(self):
        model = ScriptedModel([json.dumps(DIRTY, ensure_ascii=False),
                               json.dumps(DIRTY, ensure_ascii=False)])
        data, stats = _extractor(model).extract_with_refine(["some text"])
        self.assertEqual(stats["attempts"], 1)
        self.assertTrue(stats["converged"])
        self.assertFalse(stats["refined"])
        self.assertEqual(_core(data), _core(DIRTY))

    def test_refine_fallback_on_llm_error(self):
        model = ScriptedModel([json.dumps(DIRTY, ensure_ascii=False),
                               RuntimeError("refine call failed")])
        data, stats = _extractor(model).extract_with_refine(["some text"])
        self.assertEqual(stats["failed_attempts"], 1)
        self.assertFalse(stats["refined"])
        self.assertEqual(_core(data), _core(DIRTY))

    def test_build_prompt_includes_error_list_and_warning(self):
        p = build_prompt(["CPC promotes osteogenesis."],
                         {"title": "T", "venue": "V", "pub_year": 2025},
                         existing_entities=["Material: Calcium phosphate cement"],
                         recent_generic_warning="语料提醒：兜底关系已存在 20 条。")
        self.assertIn("硬性禁区", p)
        self.assertIn("语料提醒", p)
        self.assertIn("ERROR LIST #1", p)

    def test_refine_prompt_has_required_markers(self):
        p = REFINE_PROMPT.format(
            header="论文: T",
            text="CPC promotes osteogenesis.",
            first_json=json.dumps(DIRTY, ensure_ascii=False),
            issues="- 问题示例",
        )
        self.assertIn("定向精修", p)
        self.assertIn("I am done", p)
        self.assertIn("首遍", p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
