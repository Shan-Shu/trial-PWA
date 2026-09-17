"""三角色 LLM 驱动测试（离线）：检索→DeepSeek V4、质量→GLM、知识→gpt-5.6 假模型。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research_agent.config import Settings
from research_agent.db import connect, get_paper, get_quality_result, upsert_paper
from research_agent.quality.node import make_quality_node
from research_agent.retrieval.node import ingest_search_results
from research_agent.models import ROLE_KEY_ENV, ROLE_MODEL_DEFAULT, ROLE_PROVIDER
from research_agent.role_fakes import FakeQualityModel, FakeRetrieverModel
from tests._tmpdir import make_temp_dir


def tiny_pdf() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_textbox((70, 70, 520, 760),
                     "Retrieval-Augmented Generation (RAG) combines retrieval "
                     "with generation. Experiments evaluate the method.")
    data = doc.tobytes()
    doc.close()
    return data


class CountingFakeApi:
    """每次 search 返回一条不同的记录（模拟多查询命中多篇）。"""

    def __init__(self, records: list[dict]) -> None:
        self.records = records
        self.search_calls: list[str] = []

    def search(self, query, max_results=5):
        self.search_calls.append(query)
        idx = min(len(self.search_calls) - 1, len(self.records) - 1)
        return [dict(self.records[idx])]

    def enrich(self, rec):
        return dict(rec)

    def download_pdf(self, rec):
        return tiny_pdf()


def rec_for(key: str, title: str, **kw) -> dict:
    rec = {
        "paper_key": key, "source": "arxiv", "title": title,
        "abstract": "abstract", "doi": f"10.48550/{key}",
        "venue": "arXiv", "venue_issn": None, "source_type": "repository",
        "pub_year": 2025, "pub_date": "2025-01-01",
        "publication_status": "Preprint", "citation_count": None,
        "avg_h_index": None,
        "authors": [{"name": "Ada Lovelace", "affiliations": ["Meta AI"]}],
        "pdf_url": "https://fake.example/x.pdf",
    }
    rec.update(kw)
    return rec


class StudyRoleBindingTest(unittest.TestCase):
    def test_study_roles_use_deepseek_v4_pro_and_same_key(self):
        for role in ("planner", "consumer", "content", "review"):
            self.assertEqual(ROLE_PROVIDER[role], "deepseek")
            self.assertEqual(ROLE_MODEL_DEFAULT[role], "deepseek-v4-pro")
            self.assertEqual(ROLE_KEY_ENV[role], "DEEPSEEK_API_KEY")



class LlmRoleRetrieverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "ret.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_retriever_llm_plans_queries(self):
        recs = [
            rec_for("arxiv:1111.00001", "RAG for QA"),
            rec_for("arxiv:1111.00002", "A Survey of Retrieval Augmentation"),
        ]
        api = CountingFakeApi(recs)
        model = FakeRetrieverModel(
            queries=["retrieval augmented generation", "RAG survey 2025"])
        res = ingest_search_results("retrieval augmented generation", 2,
                                    api=api, model=model,
                                    settings=Settings(db_path=self.db))
        self.assertEqual(len(api.search_calls), 2)
        self.assertEqual(res["count"], 2)
        self.assertEqual(len(model.calls), 3)  # 1 次规划 + 每篇 1 次元数据规整
        conn = connect(self.db)
        try:
            self.assertIsNotNone(get_paper(conn, recs[0]["paper_key"]))
            self.assertIsNotNone(get_paper(conn, recs[1]["paper_key"]))
        finally:
            conn.close()


class LlmRoleQualityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "q.db"
        self.conn = connect(self.db)
        rec = rec_for(
            "arxiv:2222.00001", "Marginal venue paper",
            venue="Unknown Journal X", source_type="journal",
            citation_count=0,  # 确定性规则会把 Q 压到 flagged 档
            authors=[{"name": "Ada Lovelace", "affiliations": ["Meta AI"]}],
        )
        upsert_paper(self.conn, {**rec, "clean_text": "text", "status": "ingested"})
        self.key = rec["paper_key"]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_quality_llm_overrides_sub_scores(self):
        model = FakeQualityModel()
        node = make_quality_node(model=model, conn=self.conn,
                                 settings=Settings(db_path=self.db), offline=True)
        out = node({"current_key": self.key, "meta_attempts": 0})
        # 假 GLM 给 Q1/高因子 → 直接送知识提取（确定性规则本会 flagged）
        self.assertEqual(out["decision"], "knowledge")
        q = out["quality_result"]
        self.assertAlmostEqual(q["venue_factor"], 0.95, places=3)
        self.assertGreaterEqual(q["quality"], 0.8)
        self.assertIn("[LLM 评审]", q["rationale"])
        self.assertTrue(model.calls)
        saved = get_quality_result(self.conn, self.key)
        self.assertAlmostEqual(saved["quality"], q["quality"], places=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
