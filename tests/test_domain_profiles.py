"""领域画像优化测试：规划节点生成 profile，检索/抽取使用 profile。"""
from __future__ import annotations

import json
import unittest

from langchain_core.messages import AIMessage

from research_agent.domains import normalize_domain_profile
from research_agent.knowledge.extractor import build_prompt
from research_agent.quality.llm import QUALITY_PROMPT_TEMPLATE
from research_agent.retrieval.llm import RetrievalLLM, PLAN_PROMPT_TEMPLATE
from research_agent.study.content import deterministic_draft
from research_agent.study.planner import deterministic_plan
from research_agent.study.reviewer import deterministic_review


class DomainProfileTest(unittest.TestCase):
    def test_chemistry_profile_in_plan(self):
        plan = deterministic_plan(
            "尝试提出一种炔酰胺合成多元氮杂化合物的新方法")
        profile = plan["domain_profile"]
        self.assertEqual(profile["domain_kind"], "chemistry")
        self.assertIn("催化剂与试剂", profile["dimensions"])
        self.assertIn("Reaction", profile["candidate_entity_types"])
        self.assertIn("catalyzed_by", profile["candidate_relation_types"])

    def test_generative_plan_contains_creative_contract(self):
        plan = deterministic_plan("提出一个新的数据处理方法")
        self.assertEqual(plan["task_kind"], "generative")
        self.assertGreaterEqual(plan["creative_contract"]["min_candidates"], 3)
        self.assertTrue(any("组合" in op for op in
                            plan["creative_contract"]["creative_operations"]))

    def test_low_level_draft_is_rejected_by_review(self):
        """无算子链的低阶候选必须被审核拦下（这是 v0.4.1 的核心回归点）。"""
        plan = deterministic_plan("提出一个新的数据分析框架")
        knowledge = {
            "patterns": [
                {"pattern_id": "P-0001", "relation_type": "uses",
                 "source_type": "Method", "source_name": "A",
                 "target_type": "Task", "target_name": "B",
                 "evidence_ids": ["E-0001-1"], "support_count": 1},
                {"pattern_id": "P-0002", "relation_type": "enables",
                 "source_type": "Method", "source_name": "C",
                 "target_type": "Application", "target_name": "D",
                 "evidence_ids": ["E-0002-1"], "support_count": 2},
            ],
            "evidence": [
                {"evidence_id": "E-0001-1", "pattern_id": "P-0001",
                 "paper_key": "p1", "sentence": "A uses B."},
                {"evidence_id": "E-0002-1", "pattern_id": "P-0002",
                 "paper_key": "p2", "sentence": "C enables D."},
            ],
        }
        draft = deterministic_draft(plan, knowledge)
        self.assertGreaterEqual(len(draft.get("strategies") or []), 1)
        review = deterministic_review(plan, draft, knowledge)
        self.assertEqual(review["decision"], "revise")
        requirements = review["instruction_compliance"]["requirements"]
        design = [r for r in requirements if r.get("category") == "design"]
        self.assertTrue(design, "生成型任务必须产生设计契约检查项")
        failed = {r["requirement"] for r in design if r["status"] != "met"}
        self.assertTrue(
            any(("创新等级" in item) or ("核心创新标签" in item) for item in failed),
            f"低阶候选必须被判不达标，实际 failed={failed}")
        self.assertTrue(any("算子链" in r["requirement"] for r in design))

    def test_design_context_operator_chain_reaches_high_level(self):
        """有机制状态与算子链时，候选应达到创新等级下限并携带算子链。"""
        plan = deterministic_plan("提出一种炔酰胺构建多元氮杂环的新方法")
        knowledge = {
            "patterns": [],
            "evidence": [],
            "hyperedges": [],
            "design_context": {
                "mechanism_states": [{
                    "state_id": "MS-0001",
                    "label": "copper-catalyzed cyclization of ynamide to vinyl cation",
                    "start_state": "N-propargyl ynamide",
                    "activation_mode": "π-acid / carbophilic activation",
                    "intermediate": "vinyl cation",
                    "bond_changes": ["C-N formation"],
                    "selectivity_control": "chiral ligand control",
                    "known_side_reactions": [],
                    "evidence_ids": ["E-2033-1"],
                    "hyperedge_ids": ["H-2033"],
                    "confidence": 0.6,
                }],
                "operator_candidates": [{
                    "op_id": "OP-0001",
                    "target": "N-propargyl ynamide",
                    "operator_chain": [
                        {"operator": "polarity_reversal",
                         "input": "ynamide beta-carbon", "output": "nucleophilic carbon"},
                        {"operator": "intermediate_capture",
                         "input": "nucleophilic carbon", "output": "vinyl cation trapped ring"},
                        {"operator": "selectivity_lock",
                         "input": "vinyl cation trapped ring", "output": "single enantiomer"},
                    ],
                    "evidence_ids": ["E-2033-1"],
                    "hyperedge_ids": ["H-2033"],
                }],
            },
        }
        draft = deterministic_draft(plan, knowledge)
        strategies = draft["strategies"]
        self.assertTrue(strategies)
        levels = {s.get("innovation_level") for s in strategies}
        self.assertTrue(levels & {"L3", "L4"}, f"等级应达到 L3+，实际 {levels}")
        self.assertTrue(all(s.get("operator_chain") for s in strategies))
        review = deterministic_review(plan, draft, knowledge)
        design = [r for r in review["instruction_compliance"]["requirements"]
                  if r.get("category") == "design"]
        floor_checks = [r for r in design if "创新等级" in r["requirement"]]
        self.assertTrue(floor_checks, "必须存在创新等级检查项")
        self.assertIn("达到", floor_checks[0]["requirement"])
        low_level_check = [r for r in design if "核心创新标签" in r["requirement"]]
        self.assertTrue(low_level_check)
        self.assertEqual(low_level_check[0]["status"], "met")

    def test_normalize_profile_falls_back(self):
        p = normalize_domain_profile(
            {"domain_kind": "biomedicine", "dimensions": ["适应症", "机制"]},
            "biomedical", "")
        self.assertEqual(p["domain_kind"], "biomedicine")
        self.assertEqual(p["dimensions"], ["适应症", "机制"])

    def test_humanities_social_science_profile_detected(self):
        p = normalize_domain_profile(
            None,
            "",
            "战争胜利的伟力深藏在人民群众之中，请按人文社科问题研讨",
        )
        self.assertEqual(p["domain_kind"], "humanities_social_science")
        self.assertIn("Concept", p["candidate_entity_types"])
        self.assertIn("历史背景与史实", p["dimensions"])

    def test_retrieval_prompt_no_biomedical_hardcode(self):
        prompt = PLAN_PROMPT_TEMPLATE
        self.assertNotIn("适应症", prompt)
        self.assertNotIn("生物相容性", prompt)
        self.assertNotIn("临床转化", prompt)

    def test_extract_prompt_injects_domain_schema(self):
        profile = {
            "domain_kind": "chemistry",
            "label": "化学合成",
            "schema_status": "frozen",
            "candidate_entity_types": ["Reaction", "Substrate", "Catalyst"],
            "candidate_relation_types": ["catalyzed_by", "affords"],
        }
        prompt = build_prompt(["Reaction text."], None, None, None, profile)
        self.assertIn("Reaction", prompt)
        self.assertIn("catalyzed_by", prompt)
        self.assertIn("已冻结 schema", prompt)

    def test_quality_prompt_no_estimated_missing_data(self):
        self.assertNotIn("按期刊水平估计", QUALITY_PROMPT_TEMPLATE)
        self.assertIn("不得估计真实被引次数", QUALITY_PROMPT_TEMPLATE)


class _QueryModel:
    def __init__(self):
        self.prompt = ""

    def invoke(self, messages, **kwargs):
        self.prompt = messages[0].content
        return AIMessage(content='["ynamide annulation"]')


class RetrievalDimensionTest(unittest.TestCase):
    def test_plan_queries_receives_dimensions(self):
        model = _QueryModel()
        qs = RetrievalLLM(model).plan_queries(
            "ynamide", ["催化", "区域选择性", "反应机理"])
        self.assertEqual(qs, ["ynamide annulation"])
        self.assertIn("- 催化", model.prompt)
        self.assertIn("- 区域选择性", model.prompt)
        self.assertNotIn("适应症", model.prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
