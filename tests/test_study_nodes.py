# -*- coding: utf-8 -*-
"""四节点研究任务层测试（离线）：规划/知识消费/内容形成/审核校对。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage

from research_agent.config import Settings
from research_agent.db import connect, upsert_paper
from research_agent.ontology import store as ont
from research_agent.study.consumer import make_knowledge_consumer_node
from research_agent.study.content import make_content_node
from research_agent.study.graph import StudyServices, build_study_graph
from research_agent.study.planner import make_planner_node
from research_agent.study.reviewer import make_review_node
from tests._tmpdir import make_temp_dir


def seed_single_pattern(path: Path) -> str:
    """写入一篇文献 + 一条带溯源的关系，返回 paper_key。"""
    conn = connect(path)
    ont.init_ontology(conn)
    paper_key = "arxiv:2402.10001"
    upsert_paper(conn, {
        "paper_key": paper_key,
        "source": "arxiv",
        "title": "Retrieval-Augmented Generation",
        "abstract": "RAG combines retrieval and generation.",
        "doi": "10.48550/arXiv.2402.10001",
        "venue": "arXiv",
        "source_type": "repository",
        "pub_year": 2024,
        "publication_status": "Preprint",
        "authors": [{"name": "Ada Lovelace", "affiliations": ["Meta AI"]}],
        "clean_text": "RAG is evaluated on Natural Questions.",
        "status": "ingested",
    })
    src, _ = ont.upsert_node(
        conn, node_type="Method", name="RAG", aliases=["Retrieval-Augmented Generation"],
        confidence=0.95,
        provenance=[{"paper": paper_key, "evidence": "RAG is proposed."}])
    tgt, _ = ont.upsert_node(
        conn, node_type="Dataset", name="Natural Questions", confidence=0.9)
    ont.upsert_edge(
        conn, relation_type="evaluates", src_id=src, tgt_id=tgt,
        confidence=0.95, evidence_tier="primary",
        provenance=[{"paper": paper_key,
                     "evidence": "RAG is evaluated on Natural Questions."}])
    conn.commit()
    conn.close()
    return paper_key


class StudyNodesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "study.db"
        self.settings = Settings(db_path=self.db)
        seed_single_pattern(self.db)
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_four_node_graph_passes_offline(self):
        services = StudyServices(settings=self.settings)
        graph = build_study_graph(services, conn=self.conn)
        out = graph.invoke({
            "request": "调研 RAG 前沿",
            "review_rounds": 0,
        })
        self.assertEqual(out["status"], "reviewed")
        self.assertEqual(out["decision"], "pass")
        plan = out["plan"]
        self.assertEqual(plan["mission"]["collection_mode"], "broad")
        knowledge = out["knowledge"]
        self.assertGreaterEqual(len(knowledge["patterns"]), 1)
        self.assertGreaterEqual(len(knowledge["evidence"]), 1)
        draft = out["draft"]
        self.assertIn("P-0001", draft["markdown"])
        self.assertIn("E-0001-1", draft["markdown"])
        study_events = self.conn.execute(
            "SELECT COUNT(*) FROM processing_log WHERE node='study'"
        ).fetchone()[0]
        self.assertGreaterEqual(study_events, 8)

    def test_planner_llm_and_consumer_contracts(self):
        request = "写一份 2024-2026 年 RAG 前沿综述"

        class FakePlanner:
            def invoke(self, messages, **kwargs):
                return AIMessage(content=json.dumps({
                    "goal": "梳理 RAG 2024-2026 研究前沿",
                    "domain": "Retrieval-Augmented Generation",
                    "content_type": "frontier_review",
                    "analysis_targets": ["方法", "数据集", "评测"],
                    "mission": {
                        "seed_terms": ["retrieval augmented generation survey",
                                       "RAG benchmark 2025"],
                        "max_results": 20,
                        "min_confidence": 0.7,
                        "collection_mode": "broad",
                    },
                    "deliverable": {"format": "markdown", "language": "zh"},
                    "constraints": [],
                }, ensure_ascii=False))

        node = make_planner_node(FakePlanner())
        out = node({"request": request})
        self.assertEqual(out["status"], "planned")
        self.assertEqual(out["plan"]["content_type"], "frontier_review")
        self.assertEqual(len(out["plan"]["mission"]["seed_terms"]), 2)
        self.assertTrue(out["plan"]["retrieval_plan"]["query_variants"])
        self.assertEqual(
            out["plan"]["retrieval_plan"]["query_variants"],
            out["plan"]["mission"]["seed_terms"],
        )
        self.assertIn("dimensions", out["plan"]["analysis_plan"])
        self.assertTrue(out["plan"]["evidence_policy"]["traceability_required"])

        consumer = make_knowledge_consumer_node(
            conn=self.conn, settings=self.settings)
        consumed = consumer(out)
        self.assertEqual(consumed["status"], "consumed")
        self.assertIn("P-0001", {
            p["pattern_id"] for p in consumed["knowledge"]["patterns"]})

    def test_content_and_reviewer_deterministic_path(self):
        plan = {
            "goal": "测试目标",
            "domain": "RAG",
            "content_type": "frontier_review",
            "mission": {
                "seed_terms": ["RAG"],
                "max_results": 5,
                "min_confidence": 0.6,
                "collection_mode": "broad",
                "recency_window": "2020-01-01:2026-09-08",
            },
            "deliverable": {"format": "markdown", "language": "zh"},
        }
        consumer = make_knowledge_consumer_node(
            conn=self.conn, settings=self.settings)
        consumed = consumer({"plan": plan})
        content = make_content_node(None)
        drafted = content(consumed)
        self.assertEqual(drafted["status"], "drafted")
        self.assertIn("RAG", drafted["draft"]["markdown"])
        review = make_review_node(None)({
            **drafted,
            "plan": plan,
            "knowledge": consumed["knowledge"],
            "review_rounds": 0,
        })
        self.assertEqual(review["decision"], "pass")

    def test_empty_corpus_emits_retrieval_request(self):
        empty = Path(self.tmp.name) / "empty.db"
        settings = Settings(db_path=empty)
        conn = connect(empty)
        ont.init_ontology(conn)
        try:
            node = make_knowledge_consumer_node(conn=conn, settings=settings)
            out = node({"plan": {
                "goal": "未知领域调研",
                "domain": "unknown-domain",
                "content_type": "research_report",
                "mission": {
                    "seed_terms": ["unknown domain method"],
                    "max_results": 5,
                    "min_confidence": 0.6,
                    "collection_mode": "broad",
                },
            }})
            self.assertEqual(out["status"], "needs_collection")
            self.assertTrue(out["retrieval_request"]["seed_terms"])
        finally:
            conn.close()

    def test_collection_is_planner_first_and_consumer_consumes_result(self):
        empty = Path(self.tmp.name) / "backfill.db"
        settings = Settings(db_path=empty)
        conn = connect(empty)
        ont.init_ontology(conn)
        calls: list[dict] = []

        def fake_collector(request):
            calls.append(request)
            upsert_paper(conn, {
                "paper_key": "arxiv:2402.20002",
                "source": "arxiv",
                "title": "A backfilled paper",
                "abstract": "abstract",
                "pub_year": 2024,
                "authors": [{"name": "Ada Lovelace", "affiliations": []}],
                "status": "ingested",
            })
            src, _ = ont.upsert_node(
                conn, node_type="Method", name="BackfillMethod",
                confidence=0.9,
                provenance=[{"paper": "arxiv:2402.20002",
                             "evidence": "BackfillMethod is used."}])
            tgt, _ = ont.upsert_node(
                conn, node_type="Task", name="BackfillTask", confidence=0.8)
            ont.upsert_edge(
                conn, relation_type="uses", src_id=src, tgt_id=tgt,
                confidence=0.9,
                provenance=[{"paper": "arxiv:2402.20002",
                             "evidence": "BackfillMethod performs BackfillTask."}])
            conn.commit()
            return {"count": 1}

        try:
            graph = build_study_graph(
                StudyServices(settings=settings, collector=fake_collector),
                conn=conn,
            )
            out = graph.invoke({"request": "补集测试", "review_rounds": 0})
            self.assertEqual(len(calls), 1)
            self.assertIn("BackfillMethod", {
                p["source_name"] for p in out["knowledge"]["patterns"]})
            self.assertGreaterEqual(out.get("collection_rounds", 0), 1)
            self.assertTrue(calls[0].get("retrieval_plan"))
        finally:
            conn.close()

    def test_llm_consumer_builds_design_context_and_filters_invalid_refs(self):
        class FakeConsumer:
            def invoke(self, messages, **kwargs):
                return AIMessage(content=json.dumps({
                    "summary": "RAG 由检索与生成模块组成。",
                    "mechanism_clusters": [{
                        "name": "retrieval-generation",
                        "evidence_ids": ["E-0001-1", "E-9999-1"],
                    }],
                    "known_conflicts": [],
                    "evidence_gaps": [],
                    "retrieval_requests": [],
                    "design_context": {
                        "mechanism_states": [{
                            "state_id": "MS-0001",
                            "intermediate": "vinyl cation",
                            "evidence_ids": ["E-0001-1"],
                        }],
                        "reaction_primitives": [],
                        "opportunity_gaps": [],
                        "operator_candidates": [{
                            "op_id": "OP-0001",
                            "operator_chain": [
                                {"operator": "polarity_reversal",
                                 "input": "RAG query", "output": "retrieved context"},
                            ],
                            "evidence_ids": ["E-0001-1"],
                        }],
                        "constraint_conflicts": [],
                    },
                    "confidence": 0.82,
                }, ensure_ascii=False))

        node = make_knowledge_consumer_node(
            conn=self.conn, settings=self.settings, model=FakeConsumer())
        out = node({"plan": {
            "goal": "分析 RAG 机制",
            "domain": "RAG",
            "mission": {"seed_terms": ["RAG"]},
            "retrieval_plan": {"query_variants": ["RAG"]},
        }})
        self.assertEqual(out["status"], "consumed")
        analysis = out["knowledge"]["consumer_analysis"]
        self.assertEqual(analysis["consumer_mode"], "llm")
        # 形状正确但不存在的编号才算非法引用
        self.assertIn("E-9999-1", analysis["invalid_references"])
        refs = analysis["mechanism_clusters"][0]["evidence_ids"]
        self.assertEqual(refs, ["E-0001-1"])
        design = out["knowledge"]["design_context"]
        self.assertTrue(design["mechanism_states"])
        self.assertEqual(design["mechanism_states"][0]["state_id"], "MS-0001")
        self.assertTrue(design["mechanism_states"][0]["traceable"])
        self.assertEqual(design["operator_candidates"][0]["operator_chain"][0]["operator"],
                         "polarity_reversal")

    def test_consumer_keeps_descriptive_text_out_of_id_fields(self):
        """描述性文字被放进 *ids 字段时不应被静默删除，而应回收到 notes。"""
        class FakeConsumer:
            def invoke(self, messages, **kwargs):
                return AIMessage(content=json.dumps({
                    "summary": "机制摘要",
                    "mechanism_clusters": [],
                    "known_conflicts": [],
                    "evidence_gaps": [],
                    "retrieval_requests": [],
                    "design_context": {
                        "operator_candidates": [{
                            "op_id": "OP-0001",
                            "operator_chain": [
                                {"operator": "polarity_reversal",
                                 "input": "ynamide", "output": "nucleophilic carbon"},
                            ],
                            "evidence_ids": [
                                "将 N-sulfonyl ynamide 的 DKR 与 (5+1)-annulation 结合",
                            ],
                        }],
                    },
                    "confidence": 0.5,
                }, ensure_ascii=False))

        node = make_knowledge_consumer_node(
            conn=self.conn, settings=self.settings, model=FakeConsumer())
        out = node({"plan": {
            "goal": "机制分析",
            "domain": "chemistry",
            "mission": {"seed_terms": ["ynamide"]},
            "retrieval_plan": {"query_variants": ["ynamide"]},
        }})
        analysis = out["knowledge"]["consumer_analysis"]
        self.assertEqual(analysis["invalid_references"], [])
        self.assertTrue(analysis["unattributed_notes"])
        self.assertIn("DKR", analysis["unattributed_notes"][0])


class StudyLlmParsingTest(unittest.TestCase):
    def test_content_llm_parse_and_review(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "llm.db"
        self.settings = Settings(db_path=self.db)
        seed_single_pattern(self.db)
        conn = connect(self.db)
        try:
            consumer = make_knowledge_consumer_node(conn=conn,
                                                    settings=self.settings)
            knowledge = consumer({"plan": {
                "goal": "RAG 前沿",
                "domain": "RAG",
                "content_type": "frontier_review",
                "mission": {
                    "seed_terms": ["RAG"],
                    "max_results": 5,
                    "min_confidence": 0.6,
                    "collection_mode": "broad",
                },
            }})["knowledge"]

            class FakeContent:
                def invoke(self, messages, **kwargs):
                    return AIMessage(content=json.dumps({
                        "title": "RAG 前沿摘要",
                        "summary": {
                            "text": "从本体证据中归纳的 RAG 评测现状。",
                            "pattern_ids": ["P-0001"],
                            "evidence_ids": ["E-0001-1"],
                        },
                        "sections": [{
                            "heading": "评测模式",
                            "items": [{
                                "text": "RAG 在 Natural Questions 上被评测。",
                                "pattern_ids": ["P-0001"],
                                "evidence_ids": ["E-0001-1"],
                                "status": "supported",
                            }],
                        }],
                    }, ensure_ascii=False))

            drafted = make_content_node(FakeContent())(
                {"plan": {
                    "goal": "RAG 前沿",
                    "domain": "RAG",
                    "content_type": "frontier_review",
                    "mission": {"seed_terms": ["RAG"]},
                }, "knowledge": knowledge})
            review = make_review_node(None)({
                **drafted,
                "plan": {
                    "goal": "RAG 前沿",
                    "domain": "RAG",
                    "content_type": "frontier_review",
                    "mission": {"seed_terms": ["RAG"]},
                },
                "knowledge": knowledge,
                "review_rounds": 0,
            })
            self.assertEqual(review["decision"], "pass")
        finally:
            conn.close()
            self.tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
