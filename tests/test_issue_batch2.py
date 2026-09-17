"""批次 2 回归：P1-1 examples 导入、P1-3 planner 失败语义、
P1-4 机会缺口不再恒真、P1-5 超边证据引用归属。

对应 docs/ISSUE_TRIAGE_2026-09-11.md 的批次 2。
"""
from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage

from research_agent.study.acs_format import build_citation_index
from research_agent.study.consumer import deterministic_design_context
from research_agent.study.planner import make_planner_node
from tests._tmpdir import make_temp_dir  # noqa: F401  （保持与其他测试一致的导入风格）

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ExamplesImportTest(unittest.TestCase):
    """P1-1：examples/ 里不得再引用已被移除的符号。"""

    def test_all_research_agent_imports_resolve(self):
        problems: list[str] = []
        checked = 0
        for path in sorted((PROJECT_ROOT / "examples").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.ImportFrom) and node.module
                        and node.module.startswith("research_agent")):
                    continue
                for alias in node.names:
                    checked += 1
                    try:
                        module = __import__(node.module, fromlist=[alias.name])
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"{path.name}: import {node.module}: {exc!r}")
                        continue
                    if not hasattr(module, alias.name):
                        problems.append(
                            f"{path.name}: {node.module}.{alias.name} 不存在")
        self.assertGreater(checked, 50, "导入冒烟未覆盖到 examples")
        self.assertEqual(problems, [], f"examples 导入问题: {problems}")


class PlannerFailureSemanticsTest(unittest.TestCase):
    """P1-3：模型输出不可解析必须显式失败，不得伪装成 llm 计划。"""

    class _BadModel:
        def invoke(self, messages, **kwargs):
            return AIMessage(content="这不是 JSON，只是解释文字。")

    class _GoodModel:
        def invoke(self, messages, **kwargs):
            return AIMessage(content=json.dumps(
                {"goal": "g", "task_kind": "summary"}, ensure_ascii=False))

    def test_invalid_json_goes_to_planning_failed(self):
        out = make_planner_node(self._BadModel())({"request": "写一份综述"})
        self.assertEqual(out["status"], "planning_failed")
        self.assertFalse(out.get("plan"))
        self.assertIn("无法解析", out.get("error") or "")

    def test_valid_json_is_marked_llm(self):
        out = make_planner_node(self._GoodModel())({"request": "写一份综述"})
        self.assertEqual(out["status"], "planned")
        self.assertEqual(out["plan"]["planner_mode"], "llm")

    def test_no_model_is_marked_offline(self):
        out = make_planner_node(None)({"request": "写一份综述"})
        self.assertEqual(out["status"], "planned")
        self.assertEqual(out["plan"]["planner_mode"], "offline_fallback")


class OpportunityGapTest(unittest.TestCase):
    """P1-4：`any(... for x in [])` 恒真会让多氮缺口无条件出现。"""

    def _bundle(self) -> dict:
        return {"patterns": [], "evidence": [], "hyperedges": [{
            "hyperedge_id": 1, "reference_id": "H-0001",
            "label": "gold catalysis of alkyne", "member_names": ["alkyne"],
            "evidence_ids": ["H-0001-1"], "mechanism_score": 3, "relevance": 3,
            "support_count": 1,
            "evidence": [{"paper_key": "p1",
                          "span_text": "gold catalysis of alkyne"}],
        }]}

    def _plan(self, hard: list[str]) -> dict:
        return {"goal": "研究炔烃反应",
                "domain_profile": {"domain_kind": "chemistry"},
                "design_contract": {"target_constraints":
                                    {"hard_constraints": hard}}}

    def test_multi_nitrogen_gap_only_when_target_requires_it(self):
        with_target = deterministic_design_context(
            self._bundle(), self._plan(["氮原子数≥2"]))
        without_target = deterministic_design_context(
            self._bundle(), self._plan(["普适性"]))
        types_with = {g["gap_type"] for g in with_target["opportunity_gaps"]}
        types_without = {g["gap_type"] for g in without_target["opportunity_gaps"]}
        self.assertIn("mechanism_target_gap", types_with)
        self.assertNotIn("mechanism_target_gap", types_without,
                         "目标未要求多氮时不应报多氮缺口（P1-4 回归）")

    def test_gap_always_traceable(self):
        dc = deterministic_design_context(self._bundle(),
                                          self._plan(["氮原子数≥2"]))
        for gap in dc["opportunity_gaps"]:
            self.assertTrue(gap["evidence_ids"] or gap["hyperedge_ids"])


class CitationAttributionTest(unittest.TestCase):
    """P1-5：同一超边的不同证据句必须归属各自论文。"""

    def test_hyperedge_evidence_sentences_map_to_own_paper(self):
        index = build_citation_index({
            "patterns": [], "evidence": [],
            "hyperedges": [{
                "hyperedge_id": 7,
                "evidence": [{"paper_key": "paperA", "span_text": "s1"},
                             {"paper_key": "paperB", "span_text": "s2"}],
                "evidence_ids": ["H-0007-1", "H-0007-2"],
            }],
        })
        self.assertEqual(index["H-0007-1"], "paperA")
        self.assertEqual(index["H-0007-2"], "paperB")
        self.assertEqual(index["H-0007"], "paperA")  # 超边级取首篇

    def test_consumer_generated_evidence_ids_follow_order(self):
        index = build_citation_index({
            "patterns": [], "evidence": [],
            "hyperedges": [{
                "hyperedge_id": 12,
                "evidence": [{"paper_key": "pX", "span_text": "a"},
                             {"paper_key": "pY", "span_text": "b"},
                             {"paper_key": "pZ", "span_text": "c"}],
                "evidence_ids": ["H-0012-1", "H-0012-2", "H-0012-3"],
            }],
        })
        self.assertEqual([index[f"H-0012-{i}"] for i in (1, 2, 3)],
                         ["pX", "pY", "pZ"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
