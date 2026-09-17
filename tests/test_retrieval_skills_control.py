# -*- coding: utf-8 -*-
"""检索专项 skill 与质量控制全局归并测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_agent.config import Settings
from research_agent.db import connect
from research_agent.ontology import store as ont
from research_agent.quality.control import (
    maybe_global_merge,
    run_dictionary_merge,
)
from research_agent.study.reviewer import deterministic_review
from research_agent.study.planner import normalize_plan
from research_agent.retrieval.skills import (
    infer_retrieval_strategy,
    normalize_edge_gaps,
    plan_evidence_gap_queries,
    select_low_support_gaps,
)
from research_agent.study.content import make_content_node
from research_agent.study.planner import deterministic_plan
from tests._tmpdir import make_temp_dir


class TopicRelevanceGateTest(unittest.TestCase):
    """语料卫生：跨域命中不得进入知识库（v0.4.1）。"""

    def setUp(self):
        from research_agent.retrieval.node import apply_topic_relevance_gate

        self.gate = apply_topic_relevance_gate

    def test_cross_domain_record_is_dropped(self):
        kept, dropped = self.gate([
            {"paper_key": "a", "title": "Ynamide annulation to nitrogen heterocycles",
             "abstract": "Copper-catalyzed annulation of ynamides gives azacycles."},
            {"paper_key": "b", "title": "PEGylated liposomal doxorubicin in mice",
             "abstract": "Stealth liposomes avoid the reticuloendothelial system."},
        ], ["ynamide annulation", "ynamide nitrogen heterocycle synthesis"])
        keys = [r["paper_key"] for r in kept]
        self.assertIn("a", keys)
        self.assertNotIn("b", keys)
        self.assertEqual([r["paper_key"] for r in dropped], ["b"])

    def test_short_abstract_is_not_gated(self):
        kept, dropped = self.gate([
            {"paper_key": "c", "title": "Short", "abstract": "tiny"},
        ], ["ynamide annulation"])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_no_topic_terms_means_no_gate(self):
        kept, dropped = self.gate([
            {"paper_key": "d", "title": "Anything at all",
             "abstract": "Some reasonably long abstract text goes here."},
        ], None)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_chinese_topics_match(self):
        kept, _ = self.gate([
            {"paper_key": "e", "title": "炔酰胺构建多元氮杂环的新方法",
             "abstract": "本文报道炔酰胺参与的多组分环化反应。"},
        ], ["炔酰胺", "多元氮杂环"])
        self.assertEqual(len(kept), 1)

    def test_ingest_reports_gate_result(self):
        from research_agent.retrieval.node import ingest_search_results

        class FakeApi:
            def search(self, query, max_results=5):
                return [
                    {"paper_key": "x1", "source": "fake",
                     "title": "Ynamide annulation to azacycles",
                     "abstract": "Copper-catalyzed annulation of ynamides.",
                     "authors": []},
                    {"paper_key": "x2", "source": "fake",
                     "title": "Malaria vaccine trial in children",
                     "abstract": "A randomized trial of a malaria vaccine candidate."},
                ]

            def enrich(self, rec):
                return dict(rec)

            def download_pdf(self, rec):
                return None

        with make_temp_dir() as tmp:
            db = Path(tmp.name) / "gate.db"
            out = ingest_search_results(
                "ynamide annulation", 5, api=FakeApi(),
                settings=Settings(db_path=db), topic_terms=["ynamide annulation"])
            self.assertEqual(out["paper_keys"], ["x1"])
            self.assertEqual(out["relevance_gate"]["dropped"], 1)

    def test_content_and_reviewer_deterministic_path(self):
        from research_agent.study.consumer import make_knowledge_consumer_node
        from research_agent.study.content import make_content_node
        from research_agent.study.reviewer import make_review_node

        with make_temp_dir() as tmp:
            db = Path(tmp.name) / "plain.db"
            conn = connect(db)
            ont.init_ontology(conn)
            try:
                plan = {
                    "goal": "测试目标", "domain": "RAG",
                    "content_type": "frontier_review",
                    "mission": {"seed_terms": ["RAG"]},
                }
                consumed = make_knowledge_consumer_node(
                    conn=conn, settings=Settings(db_path=db))({"plan": plan})
                drafted = make_content_node(None)(consumed)
                self.assertEqual(drafted["status"], "drafted")
            finally:
                conn.close()


class RetrievalSkillsControlTest(unittest.TestCase):
    def test_planner_evidence_gap_inference(self):
        plan = deterministic_plan(
            "请对骨修复材料促进成骨的证据做多源补强和共识验证")
        self.assertEqual(plan["retrieval"]["strategy"], "evidence_gap")
        self.assertTrue(plan["retrieval"]["evidence_gap_enabled"])
        self.assertFalse(plan["retrieval"]["deep_single_domain_enabled"])

    def test_planner_deep_single_domain_inference(self):
        plan = deterministic_plan(
            "对炔酰胺催化环化这一单领域做精深挖掘并追溯参考文献")
        self.assertEqual(plan["retrieval"]["strategy"], "deep_single_domain")
        self.assertTrue(plan["retrieval"]["deep_single_domain_enabled"])

    def test_content_returns_low_support_edge_gaps(self):
        knowledge = {
            "patterns": [
                {
                    "pattern_id": "P-0001",
                    "source_type": "Material",
                    "source_name": "CPC",
                    "relation_type": "promotes",
                    "target_type": "BiologicalProcess",
                    "target_name": "osteogenesis",
                    "support_count": 1,
                },
                {
                    "pattern_id": "P-0002",
                    "source_type": "Material",
                    "source_name": "Hydrogel",
                    "relation_type": "is_a",
                    "target_type": "Material",
                    "target_name": "Biomaterial",
                    "support_count": 1,
                },
            ],
            "evidence": [],
        }
        node = make_content_node(None)
        out = node({
            "plan": {
                "goal": "test",
                "mission": {"seed_terms": ["CPC"]},
                "retrieval": {"min_support_target": 2},
            },
            "knowledge": knowledge,
        })
        ids = [g["pattern_id"] for g in out["edge_gaps"]]
        self.assertEqual(ids, ["P-0001"])

    def test_gap_query_plan_keeps_entity_constraint(self):
        gaps = normalize_edge_gaps([{
            "pattern_id": "P-9",
            "source_type": "Material",
            "source_name": "Calcium phosphate cement",
            "relation_type": "promotes",
            "target_type": "BiologicalProcess",
            "target_name": "osteogenesis",
            "support_count": 1,
        }])
        plans = plan_evidence_gap_queries(gaps)
        self.assertTrue(plans)
        self.assertIn("Calcium phosphate cement", plans[0]["query"])
        self.assertIn("osteogenesis", plans[0]["query"])
        self.assertEqual(plans[0]["pattern_id"], "P-9")

    def test_select_low_support_skips_taxonomy_edge(self):
        gaps = select_low_support_gaps([
            {
                "pattern_id": "P-1",
                "source_type": "Method",
                "source_name": "A",
                "relation_type": "uses",
                "target_type": "Task",
                "target_name": "B",
                "support_count": 1,
            },
            {
                "pattern_id": "P-2",
                "source_type": "Material",
                "source_name": "A",
                "relation_type": "is_a",
                "target_type": "Material",
                "target_name": "B",
                "support_count": 1,
            },
        ])
        self.assertEqual([g["pattern_id"] for g in gaps], ["P-1"])

    def test_dictionary_merge_repoints_edges(self):
        tmp = make_temp_dir()
        path = Path(tmp.name) / "merge.db"
        conn = connect(path)
        ont.init_ontology(conn)
        try:
            tgt, _ = ont.upsert_node(
                conn, node_type="BiologicalProcess", name="osteogenesis",
                confidence=0.9)
            src1, _ = ont.upsert_node(
                conn, node_type="Chemical", name="water",
                confidence=0.9,
                provenance=[{"paper": "p1", "evidence": "water"}])
            src2, _ = ont.upsert_node(
                conn, node_type="Chemical", name="H2O",
                confidence=0.8,
                provenance=[{"paper": "p2", "evidence": "H2O"}])
            self.assertNotEqual(src1, src2)
            ont.upsert_edge(
                conn, relation_type="promotes", src_id=src1, tgt_id=tgt,
                confidence=0.9,
                provenance=[{"paper": "p1", "evidence": "water promotes"}])
            ont.upsert_edge(
                conn, relation_type="promotes", src_id=src2, tgt_id=tgt,
                confidence=0.8,
                provenance=[{"paper": "p2", "evidence": "H2O promotes"}])
            conn.commit()
            stats = run_dictionary_merge(conn)
            self.assertGreaterEqual(stats["merged_nodes"], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM ontology_nodes").fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM ontology_edges").fetchone()[0],
                1,
            )
            edge = conn.execute(
                "SELECT provenance FROM ontology_edges"
            ).fetchone()
            self.assertEqual(len(json.loads(edge["provenance"])), 2)
        finally:
            conn.close()
            tmp.cleanup()

    def test_reviewer_uses_four_to_one_weighting_and_correctness_gate(self):
        plan = normalize_plan({
            "goal": "提出两个新方法",
            "domain": "测试领域",
            "task_kind": "generative",
            "instruction_contract": {
                "task_kind": "generative",
                "deliverable_format": "markdown",
                "language": "zh",
                "required_method_count": 2,
                "correctness_threshold": 0.85,
            },
            "creative_contract": {"min_candidates": 2},
        }, "提出两个新方法")
        draft = {
            "title": "测试",
            "summary": {"text": "中文摘要", "pattern_ids": [], "evidence_ids": []},
            "sections": [{"heading": "方法", "items": [
                {"text": "方法一", "pattern_ids": ["P-0001"],
                 "evidence_ids": ["E-0001-1"], "status": "supported"}
            ]}],
            "strategies": [{"title": "S1", "pattern_ids": ["P-0001"],
                            "evidence_ids": ["E-0001-1"]}],
            "markdown": "# 测试\n中文内容",
        }
        knowledge = {
            "patterns": [{"pattern_id": "P-0001"}],
            "evidence": [{"evidence_id": "E-0001-1"}],
        }
        review = deterministic_review(plan, draft, knowledge, "提出两个新方法")
        self.assertEqual(review["decision"], "revise")
        self.assertAlmostEqual(
            review["overall_score"],
            0.8 * review["instruction_compliance"]["score"]
            + 0.2 * review["evidence_correctness"]["score"], places=3)

        bad_draft = {
            "title": "坏草稿",
            "summary": {"text": "中文", "pattern_ids": [], "evidence_ids": []},
            "sections": [{"heading": "结论", "items": [
                {"text": "断言", "pattern_ids": [],
                 "evidence_ids": ["E-9999-1"], "status": "supported"}
            ]}],
            "markdown": "# 坏草稿\n中文断言",
        }
        bad = deterministic_review(
            normalize_plan({"goal": "总结", "task_kind": "summary"}, "总结"),
            bad_draft, {"patterns": [], "evidence": []}, "总结")
        self.assertLess(
            bad["evidence_correctness"]["score"],
            bad["evidence_correctness"]["threshold"])
        self.assertNotEqual(bad["decision"], "pass")

    def test_hyperedge_keeps_roles_conditions_measurements_without_event_node(self):
        tmp = make_temp_dir()
        path = Path(tmp.name) / "hyperedge.db"
        conn = connect(path)
        ont.init_ontology(conn)
        try:
            a, _ = ont.upsert_node(conn, node_type="Chemical",
                                   name="N-allyl-ynamide", confidence=0.9)
            pd, _ = ont.upsert_node(conn, node_type="Catalyst", name="Pd",
                                    confidence=0.9)
            b, _ = ont.upsert_node(conn, node_type="Chemical",
                                   name="Benzimidazole", confidence=0.9)
            edge_id, is_new = ont.upsert_hyperedge(
                conn, hyperedge_type="reaction", label="A to B",
                members=[
                    {"node_id": a, "role": "substrate"},
                    {"node_id": pd, "role": "catalyst"},
                    {"node_id": b, "role": "product"},
                ],
                conditions=[{"key": "temperature", "value": 80, "unit": "°C"}],
                measurements=[{"metric": "yield", "value": 85, "unit": "%"}],
                confidence=0.9, paper_key="p1",
                provenance=[{"paper": "p1", "evidence": "A with Pd gives B"}],
            )
            self.assertTrue(is_new)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM ontology_nodes").fetchone()[0], 3)
            rows = ont.list_hyperedges(conn)
            self.assertEqual(len(rows), 1)
            node = rows[0]
            self.assertEqual({m["role"] for m in node["members"]},
                             {"substrate", "catalyst", "product"})
            self.assertEqual(node["conditions"][0]["condition_key"], "temperature")
            self.assertEqual(node["measurements"][0]["metric"], "yield")
            self.assertGreaterEqual(edge_id, 1)
            views = ont.rebuild_ontology_views(conn)
            self.assertGreaterEqual(views["domains"], 2)
            self.assertGreaterEqual(views["channels"], 1)
            labels_a = {r["label"] for r in conn.execute(
                "SELECT d.label FROM ontology_domain_members m "
                "JOIN ontology_domains d ON d.domain_key=m.domain_key "
                "WHERE m.node_id=?", (a,)).fetchall()}
            labels_b = {r["label"] for r in conn.execute(
                "SELECT d.label FROM ontology_domain_members m "
                "JOIN ontology_domains d ON d.domain_key=m.domain_key "
                "WHERE m.node_id=?", (b,)).fetchall()}
            self.assertIn("ynamides", labels_a)
            self.assertIn("nitrogen heterocycles", labels_b)
            self.assertNotEqual(labels_a, labels_b)
        finally:
            conn.close()
            tmp.cleanup()

    def test_cleanup_local_reference_labels_and_rename_with_alias(self):
        tmp = make_temp_dir()
        path = Path(tmp.name) / "labels.db"
        conn = connect(path)
        ont.init_ontology(conn)
        try:
            junk, _ = ont.upsert_node(
                conn, node_type="Chemical", name="Compound 4aa",
                aliases=["4aa"], confidence=0.8)
            useful, _ = ont.upsert_node(
                conn, node_type="Chemical", name="Compound 33",
                aliases=["(2E)-3-phenyl-N-(3,4,5-trichlorophenyl)prop-2-enamide"],
                confidence=0.8)
            stats = ont.cleanup_local_label_nodes(conn)
            self.assertGreaterEqual(stats["dropped"], 1)
            self.assertGreaterEqual(stats["renamed"], 1)
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM ontology_nodes WHERE node_id=?", (junk,)).fetchone())
            row = conn.execute(
                "SELECT name,aliases FROM ontology_nodes WHERE node_id=?",
                (useful,)).fetchone()
            self.assertEqual(
                row["name"], "(2E)-3-phenyl-N-(3,4,5-trichlorophenyl)prop-2-enamide")
            self.assertNotIn("Compound 33", json.loads(row["aliases"]))
        finally:
            conn.close()
            tmp.cleanup()

    def test_maybe_global_merge_threshold(self):
        tmp = make_temp_dir()
        path = Path(tmp.name) / "threshold.db"
        conn = connect(path)
        ont.init_ontology(conn)
        settings = Settings(db_path=path)
        settings.global_merge_interval_nodes = 2
        try:
            ont.upsert_node(conn, node_type="Chemical", name="water",
                            confidence=0.9)
            conn.commit()
            first = maybe_global_merge(conn, settings)
            self.assertFalse(first["triggered"])
            ont.upsert_node(conn, node_type="Chemical", name="H2O",
                            confidence=0.8)
            conn.commit()
            second = maybe_global_merge(conn, settings)
            self.assertTrue(second["triggered"])
        finally:
            conn.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
