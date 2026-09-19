"""v0.4.1 回归：事实核查、修订闭环、研究任务中间态落库。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage

from research_agent.config import Settings
from research_agent.db import connect, get_study_runs, upsert_paper
from research_agent.ontology import store as ont
from research_agent.study.content import make_content_node
from research_agent.study.fact_check import (
    deterministic_fact_check,
    make_fact_check_node,
)
from research_agent.study.graph import StudyServices, build_study_graph, run_study
from tests._tmpdir import make_temp_dir

PAPER = "test:1"


def seed(db: Path) -> None:
    conn = connect(db)
    ont.init_ontology(conn)
    upsert_paper(conn, {
        "paper_key": PAPER, "source": "test",
        "title": "Ynamide cyclization",
        "abstract": "Copper-catalyzed cyclization of ynamides.",
        "clean_text": "Copper-catalyzed cyclization of ynamides.",
        "pub_year": 2024,
        "authors": [{"name": "Ada Lovelace", "affiliations": []}],
        "status": "ingested",
    })
    src, _ = ont.upsert_node(
        conn, node_type="Chemical", name="ynamide", confidence=0.9,
        provenance=[{"paper": PAPER, "evidence": "ynamide 1a was used."}])
    tgt, _ = ont.upsert_node(conn, node_type="Chemical", name="azacycle",
                             confidence=0.8)
    ont.upsert_edge(conn, relation_type="results_in", src_id=src, tgt_id=tgt,
                    confidence=0.9,
                    provenance=[{"paper": PAPER,
                                 "evidence": "cyclization gives azacycle."}])
    conn.commit()
    conn.close()


def fake_draft(**overrides) -> dict:
    draft = {
        "title": "测试草稿",
        "summary": {"text": "摘要", "pattern_ids": ["P-0001"],
                    "evidence_ids": ["E-0001-1"]},
        "sections": [{
            "heading": "发现",
            "items": [{"text": "ynamide 可环化。", "pattern_ids": ["P-0001"],
                       "evidence_ids": ["E-0001-1"], "status": "supported"}],
        }],
        "strategies": [{
            "id": "S-01", "title": "候选一",
            "operator_chain": [{"operator": "polarity_reversal",
                                "input": "ynamide", "output": "nucleophilic carbon"}],
            "innovation_level": "L3",
            "evidence_ids": ["E-0001-1"],
            "satisfies_constraints": [],
            "status": "hypothesis",
        }],
        "candidate_pool": {"generated": 1, "after_dedupe": 1, "duplicates": [],
                           "selected": 1, "rejected": 0},
    }
    draft.update(overrides)
    return draft


class FactCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "fc.db"
        seed(self.db)
        self.conn = connect(self.db)
        self.settings = Settings(db_path=self.db)
        self.knowledge = {
            "patterns": [{"pattern_id": "P-0001", "evidence_ids": ["E-0001-1"]}],
            "evidence": [{"evidence_id": "E-0001-1", "pattern_id": "P-0001",
                          "paper_key": PAPER, "sentence": "ynamide 1a was used."}],
            "hyperedges": [],
        }

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_unsupported_claim_is_flagged(self):
        draft = fake_draft()
        draft["sections"][0]["items"][0]["evidence_ids"] = []
        draft["sections"][0]["items"][0]["pattern_ids"] = []
        result = deterministic_fact_check(draft, self.knowledge)
        self.assertEqual(result["decision"], "revise")
        types = {i["type"] for i in result["issues"]}
        self.assertIn("unsupported_claim", types)

    def test_invented_reference_is_fabrication(self):
        draft = fake_draft()
        draft["sections"][0]["items"][0]["evidence_ids"] = ["E-9999-9"]
        result = deterministic_fact_check(draft, self.knowledge)
        self.assertEqual(result["decision"], "revise")
        self.assertIn("fabrication", {i["type"] for i in result["issues"]})

    def test_unsupported_number_is_flagged(self):
        draft = fake_draft()
        draft["strategies"][0]["rationale"] = "收率可达 95%。"
        result = deterministic_fact_check(draft, self.knowledge)
        self.assertIn("fabrication", {i["type"] for i in result["issues"]})

    def test_clean_draft_passes(self):
        result = deterministic_fact_check(fake_draft(), self.knowledge)
        self.assertEqual(result["decision"], "pass")
        self.assertEqual(result["issues"], [])

    def test_llm_cannot_override_high_severity_finding(self):
        class LyingModel:
            def invoke(self, messages, **kwargs):
                return AIMessage(content=json.dumps({
                    "decision": "pass", "issues": [], "summary": "没问题"}))

        draft = fake_draft()
        draft["sections"][0]["items"][0]["evidence_ids"] = ["E-9999-9"]
        node = make_fact_check_node(model=LyingModel(), conn=self.conn,
                                    settings=self.settings)
        out = node({"draft": draft, "knowledge": self.knowledge,
                    "run_id": "t"})
        self.assertEqual(out["fact_check"]["decision"], "revise")


class RevisionLoopTest(unittest.TestCase):
    """审核/事实核查的修订意见必须真的改变内容节点的下一次输入。"""

    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "loop.db"
        seed(self.db)
        self.settings = Settings(db_path=self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_revision_actions_reach_content_prompt(self):
        seen_prompts: list[str] = []

        class ContentModel:
            def invoke(self, messages, **kwargs):
                prompt = messages[0].content
                seen_prompts.append(prompt)
                if "上一轮审核未通过" in prompt:
                    return AIMessage(content=json.dumps({
                        "title": "修订后",
                        "sections": [{"heading": "发现", "items": []}],
                        "candidates": [{
                            "id": "C-01", "title": "修订候选",
                            "operator_chain": [
                                {"operator": "polarity_reversal",
                                 "input": "ynamide", "output": "nucleophilic carbon"},
                                {"operator": "intermediate_capture",
                                 "input": "nucleophilic carbon",
                                 "output": "azacycle"},
                            ],
                            "innovation_basis": "引入机制级算子",
                            "evidence_ids": ["E-0001-1"],
                            "satisfies_constraints": [],
                            "status": "hypothesis",
                        }],
                        "revision_responses": [
                            {"action": "补上算子链", "resolved": True,
                             "note": "已补充"},
                        ],
                    }, ensure_ascii=False))
                return AIMessage(content=json.dumps({
                    "title": "首轮",
                    "sections": [{"heading": "发现", "items": []}],
                    "candidates": [{
                        "id": "C-01", "title": "低级候选",
                        "creative_operation": "组合",
                        "evidence_ids": ["E-0001-1"],
                        "status": "hypothesis",
                    }],
                }, ensure_ascii=False))

        plan = {
            "goal": "提出新方法",
            "task_kind": "generative",
            "design_contract": {
                "objective": "新方法",
                "target_constraints": {"hard_constraints": ["氮原子数≥2"]},
                "innovation_floor": "L3",
                "min_candidates": 4,
                "differentiation_axes": ["mechanism"],
                "evaluation_criteria": ["创新等级"],
            },
            "instruction_contract": {"required_method_count": 4},
            "mission": {"seed_terms": ["ynamide"]},
        }
        state = {
            "plan": plan,
            "knowledge": {},
            "review_rounds": 1,
            "review": {"revision_actions": ["补上算子链"],
                       "issues": [], "instruction_compliance": {}},
        }
        node = make_content_node(ContentModel(), settings=self.settings)
        first = node(state)
        self.assertIn("上一轮审核未通过", seen_prompts[0])
        self.assertIn("补上算子链", seen_prompts[0])
        draft = first["draft"]
        self.assertTrue(draft["revision_responses"])
        self.assertEqual(draft["revision_consumed"], 1)
        self.assertIn("修订回应", draft["markdown"])

    def test_graph_stores_revision_history(self):
        graph = build_study_graph(
            StudyServices(settings=self.settings), conn=None)
        out = graph.invoke({"request": "调研 ynamide 环化", "review_rounds": 0})
        # 图跑通即可：落库在 run_study 中验证
        self.assertIn(out.get("status"), ("reviewed", "manual_review"))

    def test_run_study_persists_study_runs(self):
        out = run_study("调研 ynamide 环化生成氮杂环的新方法",
                        StudyServices(settings=self.settings))
        run_id = out.get("run_id")
        self.assertTrue(run_id)
        conn = connect(self.db)
        try:
            rows = get_study_runs(conn, run_id)
        finally:
            conn.close()
        self.assertTrue(rows, "研究任务中间态必须落库以便审计")
        row = rows[-1]
        self.assertIn("plan", row)
        self.assertTrue(row["consumer"])
        self.assertIn("mechanism_states", row["consumer"]["design_context"])
        self.assertTrue(row["draft"])
        self.assertIn("fact_check", row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
