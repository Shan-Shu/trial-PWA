"""离线端到端测试：不访问任何外部网络。

覆盖：
- PDF 清洗（页眉/页脚/页码剔除）；
- 检索节点：检索→PDF BLOB 入库→精校文本→元数据补全；
- 质量节点：A/T/Q 计算与 direct/flagged/human 路由、元数据回补循环；
- 知识节点：分句分段→LLM(假模型)抽取→动态本体入库/合并/版本；
- LangGraph 全流程（三个节点串联）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_agent.config import Settings
from research_agent.db import connect, get_paper, get_quality_result
from research_agent.knowledge.fake import StaticJsonModel
from research_agent.knowledge.preprocess import split_paragraphs, split_sentences
from research_agent.ontology import store as ont
from research_agent.pipeline import Services, build_pipeline_graph, process_papers
from research_agent.quality.scoring import (
    check_metadata_completeness,
    quality_assess,
)
from research_agent.retrieval.pdf_cleaner import clean_pdf
from tests._tmpdir import make_temp_dir


def make_pdf_bytes(pages: list[list[str]]) -> bytes:
    """用 PyMuPDF 生成带页眉/页脚/页码的测试 PDF。pages 每项为正文段落列表。"""
    import pymupdf

    doc = pymupdf.open()
    for i, body_paras in enumerate(pages, start=1):
        page = doc.new_page()
        header = "IEEE TRANSACTIONS ON PATTERN ANALYSIS AND MACHINE INTELLIGENCE"
        page.insert_text((70, 60), header, fontsize=8)
        body = "\n".join(body_paras)
        page.insert_textbox((70, 90, 520, 760), body, fontsize=10)
        page.insert_text((70, 810), f"© 2024 IEEE. Personal use is permitted.", fontsize=7)
        page.insert_text((520, 810), str(i), fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


BODY_PARAS = [
    "Retrieval-Augmented Generation (RAG) combines parametric memory with "
    "non-parametric retrieval. The method was proposed by researchers at Meta AI.",
    "Experiments on the Natural Questions dataset show that RAG outperforms "
    "fine-tuned seq2seq baselines on open-domain question answering.",
    "Our study concludes that retrieval augmentation is a promising direction "
    "for knowledge-intensive tasks.",
]


class FakeApi:
    """离线假 API：search/enrich/download_pdf 三接口齐备。"""

    def __init__(self, record: dict, pdf: bytes, enrich_fill: dict | None = None) -> None:
        self.record = dict(record)
        self.pdf = pdf
        self.enrich_fill = enrich_fill or {}
        self.enrich_calls = 0

    def search(self, query, max_results=5):
        rec = {k: v for k, v in self.record.items()}
        rec.setdefault("pdf_url", "https://fake.example/paper.pdf")
        return [rec]

    def download_pdf(self, rec):
        return self.pdf

    def enrich(self, rec):
        self.enrich_calls += 1
        out = {**rec, **self.enrich_fill}
        out["paper_key"] = rec["paper_key"]
        return out


def base_record(**overrides) -> dict:
    rec = {
        "paper_key": "arxiv:2402.99999",
        "source": "arxiv",
        "title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
        "abstract": "We explore RAG for knowledge-intensive tasks.",
        "doi": "10.48550/arXiv.2402.99999",
        "venue": "Nature",
        "venue_issn": "0028-0836",
        "source_type": "journal",
        "pub_year": 2024,
        "pub_date": "2024-05-01",
        "publication_status": "Published",
        "citation_count": 1500,
        "avg_h_index": 45.0,
        "authors": [
            {"name": "Ada Lovelace", "given": "Ada", "family": "Lovelace",
             "affiliations": ["Meta AI"], "h_index": 45.0},
            {"name": "Alan Turing", "given": "Alan", "family": "Turing",
             "affiliations": ["Meta AI"], "h_index": 40.0},
        ],
        "field_velocity": "medium",
    }
    rec.update(overrides)
    return rec


def quality_of(rec: dict) -> dict:
    return quality_assess(rec, Settings(current_year=2026))


class PdfCleanerTest(unittest.TestCase):
    def test_removes_header_footer_and_page_numbers(self):
        pdf = make_pdf_bytes([BODY_PARAS, BODY_PARAS, BODY_PARAS])
        result = clean_pdf(pdf)
        text = result["text"]
        self.assertIn("Retrieval-Augmented Generation", text)
        self.assertNotIn("IEEE TRANSACTIONS", text)
        self.assertNotIn("Personal use is permitted", text)
        self.assertGreaterEqual(result["removed_total"], 3)


class PreprocessTest(unittest.TestCase):
    def test_paragraph_and_sentence_split(self):
        paras = split_paragraphs("第一段。\n\n第二段 sentence one. sentence two. "
                                 "Meta et al. published it.")
        self.assertEqual(len(paras), 2)
        sentences = split_sentences(paras[1])
        # “et al.” 后不应断句
        self.assertEqual(len(sentences), 3)


class OntologyStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.conn = connect(Path(self.tmp.name) / "o.db")
        ont.init_ontology(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_merge_and_dynamic_types(self):
        n1, new1 = ont.upsert_node(
            self.conn, node_type="Method", name="RAG", confidence=0.7,
            aliases=["Retrieval-Augmented Generation"],
            attributes={"year": 2020},
            provenance=[{"paper": "p1", "evidence": "s1"}])
        self.assertTrue(new1)
        # 同类型同名称再次写入 → 合并而非新增，置信度取 max
        n2, new2 = ont.upsert_node(
            self.conn, node_type="Method", name="RAG", confidence=0.9,
            aliases=["retrieval augmented generation"],
            attributes={"kind": "hybrid"},
            provenance=[{"paper": "p2", "evidence": "s2"}])
        self.assertEqual(n1, n2)
        self.assertFalse(new2)
        row = self.conn.execute(
            "SELECT confidence, aliases FROM ontology_nodes WHERE node_id=?", (n1,)
        ).fetchone()
        self.assertAlmostEqual(row["confidence"], 0.9)
        # 动态类型自动注册（未 seed 的新关系类型）
        src = ont.upsert_node(self.conn, node_type="Dataset", name="NQ", confidence=0.5)[0]
        e, e_new = ont.upsert_edge(self.conn, relation_type="involves",
                                   src_id=n1, tgt_id=src, confidence=0.8)
        self.assertTrue(e_new)
        s = ont.graph_summary(self.conn)
        self.assertEqual(s["nodes"], 2)
        self.assertEqual(s["edges"], 1)
        self.assertIn("involves", s["edge_by_type"])
        self.assertGreater(s["schema_version"], 1)


class RetrievalIngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "t.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_and_clean_store(self):
        rec = base_record(venue="arXiv", source_type="repository", venue_issn=None,
                          publication_status="Preprint",
                          authors=[{"name": "Ada Lovelace", "affiliations": []}])
        api = FakeApi(rec, make_pdf_bytes([BODY_PARAS] * 3))
        settings = Settings(db_path=self.db)
        from research_agent.retrieval.node import ingest_search_results

        res = ingest_search_results("rag", 1, api=api, settings=settings)
        self.assertEqual(res["count"], 1)
        conn = connect(self.db)
        try:
            stored = get_paper(conn, rec["paper_key"])
            self.assertIsNotNone(stored)
            self.assertIsNotNone(stored["pdf_blob"])
            self.assertIn("Retrieval-Augmented Generation", stored["clean_text"])
            self.assertNotIn("IEEE TRANSACTIONS", stored["clean_text"])
        finally:
            conn.close()


class QualityScoringTest(unittest.TestCase):
    def test_authority_formula(self):
        rec = base_record()
        q = quality_of(rec)
        # A 权重：0.5 venue(Q1 .95) + 0.3 h + 0.2 cite
        self.assertGreaterEqual(q["authority"], 0.85)
        self.assertGreaterEqual(q["quality"], 0.8)
        self.assertEqual(q["decision"], "direct")

    def test_flag_route(self):
        rec = base_record(venue="Scientific Reports", venue_quartile="Q2",
                          citation_count=10, avg_h_index=5.0, pub_year=2022)
        q = quality_of(rec)
        self.assertGreaterEqual(q["quality"], 0.5)
        self.assertLess(q["quality"], 0.8)
        self.assertEqual(q["decision"], "flagged")
        self.assertTrue(q["needs_review"])

    def test_human_route(self):
        rec = base_record(venue=None, source_type=None, citation_count=0,
                          avg_h_index=None, pub_year=1990, field_velocity="slow",
                          authors=[{"name": "Old Scholar", "affiliations": ["X"]}])
        q = quality_of(rec)
        self.assertLess(q["quality"], 0.5)
        self.assertEqual(q["decision"], "human")

    def test_metadata_completeness(self):
        full = base_record()
        self.assertEqual(check_metadata_completeness(full), (True, []))
        missing = base_record(doi=None,
                              authors=[{"name": "No Affil", "affiliations": []}])
        ok, missing_fields = check_metadata_completeness(missing)
        self.assertFalse(ok)
        self.assertIn("affiliations", missing_fields)
        self.assertIn("doi", missing_fields)


class KnowledgeExtractionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "k.db"
        self.conn = connect(self.db)
        ont.init_ontology(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_extract_writes_dynamic_ontology(self):
        rec = base_record()
        from research_agent.db import save_quality_result, upsert_paper

        upsert_paper(self.conn, {**rec, "clean_text": "\n\n".join(BODY_PARAS),
                                 "status": "ingested"})
        save_quality_result(self.conn, {
            "paper_key": rec["paper_key"], "quality": 0.9, "decision": "knowledge",
            "needs_review": False, "authority": 0.9, "timeliness": 0.9,
            "rationale": "t", "meta_missing": []})
        node = __import__("research_agent.knowledge.node", fromlist=["make_knowledge_node"])
        kn = node.make_knowledge_node(model=StaticJsonModel(), conn=self.conn,
                                      settings=Settings(db_path=self.db))
        out = kn({"current_key": rec["paper_key"], "decision": "knowledge"})
        self.assertEqual(out["status"], "extracted")
        report = out["extraction_report"]
        ext = report["extracted"]
        self.assertGreaterEqual(ext["entities"], 3)
        self.assertGreaterEqual(ext["relations"], 2)
        self.assertGreaterEqual(ext["events"], 1)
        s = ont.graph_summary(self.conn)
        # v0.0.5：事件写入旁路表，不再生成事件节点/星型 involves 边
        self.assertGreaterEqual(s["nodes"], 3)
        self.assertGreaterEqual(s["edges"], 2)
        ev = self.conn.execute(
            "SELECT COUNT(*) FROM event_assertions WHERE paper_key=?",
            (rec["paper_key"],),
        ).fetchone()[0]
        self.assertEqual(ev, 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM ontology_nodes WHERE node_type='Experiment'"
            ).fetchone()[0], 0)
        # 节点置信度融合了文献质量 Q
        conf = self.conn.execute(
            "SELECT confidence FROM ontology_nodes WHERE node_type='Method' "
            "AND normalized_name='retrieval augmented generation'"
        ).fetchone()
        self.assertIsNotNone(conf)
        self.assertGreater(conf["confidence"], 0.8)


class PipelineEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "pipeline.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _settings(self):
        return Settings(db_path=self.db, current_year=2026)

    def test_direct_full_flow_search_to_ontology(self):
        rec = base_record()
        api = FakeApi(rec, make_pdf_bytes([BODY_PARAS]))
        services = Services(api=api, knowledge_model=StaticJsonModel(),
                            settings=self._settings(), offline=True)
        from research_agent.pipeline import run_topic

        out = run_topic("retrieval augmented generation", 1, services)
        self.assertEqual(out["ingest"]["count"], 1)
        self.assertEqual(len(out["per_paper"]), 1)
        paper_out = out["per_paper"][0]
        self.assertEqual(paper_out["decision"], "knowledge")
        self.assertEqual(paper_out["status"], "extracted")
        # 质量与本体入库
        conn = connect(self.db)
        try:
            q = get_quality_result(conn, rec["paper_key"])
            self.assertIsNotNone(q)
            self.assertGreaterEqual(q["quality"], 0.8)
            summary = ont.graph_summary(conn)
            self.assertGreater(summary["nodes"], 0)
            self.assertGreater(summary["edges"], 0)
        finally:
            conn.close()

    def test_missing_metadata_enrich_loop_then_knowledge(self):
        rec = base_record(
            doi=None,
            venue=None, venue_issn=None, source_type=None,
            publication_status=None,
            authors=[{"name": "Ada Lovelace", "affiliations": []}],
        )
        fill = base_record()  # enrich 模拟检索节点补全成功
        api = FakeApi(rec, make_pdf_bytes([BODY_PARAS]),
                      enrich_fill={"doi": fill["doi"], "venue": fill["venue"],
                                   "source_type": "journal",
                                   "publication_status": "Published",
                                   "authors": fill["authors"]})
        services = Services(api=api, knowledge_model=StaticJsonModel(),
                            settings=self._settings(), offline=True)
        graph = build_pipeline_graph(services)
        # 先手动入库（模拟检索已入库），再走 质量→回补→质量→知识
        conn = connect(self.db)
        try:
            from research_agent.db import upsert_paper

            upsert_paper(conn, {**rec, "clean_text": "\n\n".join(BODY_PARAS),
                                "pdf_blob": make_pdf_bytes([BODY_PARAS]),
                                "status": "ingested"})
        finally:
            conn.close()
        out = graph.invoke({"current_key": rec["paper_key"], "mode": "load",
                            "meta_attempts": 0})
        self.assertEqual(out["decision"], "knowledge")
        self.assertGreaterEqual(out["meta_attempts"], 1)
        conn = connect(self.db)
        try:
            stored = get_paper(conn, rec["paper_key"])
            self.assertTrue(stored["doi"])
        finally:
            conn.close()

    def test_low_quality_goes_human_review(self):
        rec = base_record(
            venue=None, venue_issn=None, source_type=None,
            citation_count=0, avg_h_index=None, pub_year=1990,
            field_velocity="slow",
            authors=[{"name": "Old Scholar", "affiliations": ["X University"]}])
        api = FakeApi(rec, make_pdf_bytes([BODY_PARAS]))
        services = Services(api=api, knowledge_model=StaticJsonModel(),
                            settings=self._settings(), offline=True)
        graph = build_pipeline_graph(services)
        conn = connect(self.db)
        try:
            from research_agent.db import upsert_paper

            upsert_paper(conn, {**rec, "clean_text": "\n\n".join(BODY_PARAS),
                                "status": "ingested"})
        finally:
            conn.close()
        out = graph.invoke({"current_key": rec["paper_key"], "mode": "load",
                            "meta_attempts": 0})
        self.assertEqual(out["decision"], "human")
        self.assertEqual(out["status"], "human_review")
        conn = connect(self.db)
        try:
            stored = get_paper(conn, rec["paper_key"])
            self.assertEqual(stored["status"], "human_review")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
