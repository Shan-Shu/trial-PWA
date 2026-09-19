"""批次 3 回归：P1-2 精修测试被收集、P1-7 PDF 失败降级、P1-8 单源隔离、P1-9 抽取失败标记。

对应 docs/ISSUE_TRIAGE_2026-09-11.md 的批次 3。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from research_agent.config import Settings
from research_agent.db import connect, get_paper, upsert_paper
from research_agent.knowledge.node import make_knowledge_node
from research_agent.ontology import store as ont
from research_agent.pipeline import process_papers
from research_agent.retrieval.api_clients import ApiHub
from research_agent.retrieval.node import ingest_search_results
from tests._tmpdir import make_temp_dir


class RefineTestsAreCollectedTest(unittest.TestCase):
    """P1-2：精修测试必须是 TestCase（此前 7 个模块级函数从未被执行）。"""

    def test_refine_suite_is_a_testcase(self):
        from tests import test_extractor_refine as mod

        names = [n for n in dir(mod) if n.startswith("test_")]
        self.assertEqual(names, [], "仍有模块级 test_ 函数不会被收集")
        cases = [c for c in vars(mod).values()
                 if isinstance(c, type) and issubclass(c, unittest.TestCase)]
        self.assertTrue(cases, "未找到 TestCase 子类")
        count = sum(len([m for m in dir(c) if m.startswith("test_")])
                    for c in cases)
        self.assertGreaterEqual(count, 7, "精修测试数量少于原有 7 项")


class _FakeApi:
    """坏 PDF + 可用摘要的假数据源。"""

    def __init__(self, pdf: bytes | None, abstract: str = "可用摘要") -> None:
        self.pdf = pdf
        self.abstract = abstract

    def search(self, query, max_results=5):
        return [{
            "paper_key": "p-bad-pdf", "source": "fake", "title": "T",
            "abstract": self.abstract, "authors": [{"name": "A"}],
            "pmcid": None, "source_type": "journal", "pub_year": 2024,
        }]

    def enrich(self, rec):
        return dict(rec)

    def download_pdf(self, rec):
        return self.pdf

    def fulltext_text(self, pmcid):
        return None


class CleanPdfFailureTest(unittest.TestCase):
    """P1-7：PDF 解析失败不应丢弃整条记录。"""

    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "p17.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_broken_pdf_falls_back_to_abstract(self):
        out = ingest_search_results(
            "topic", 3, api=_FakeApi(b"not a real pdf at all"),
            settings=Settings(db_path=self.db), topic_terms=None)
        self.assertEqual(out["paper_keys"], ["p-bad-pdf"],
                         "PDF 解析失败导致整条记录被丢弃（P1-7 回归）")
        conn = connect(self.db)
        try:
            rec = get_paper(conn, "p-bad-pdf")
        finally:
            conn.close()
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "ingested")
        self.assertTrue(rec["clean_text"])
        self.assertIn(rec["fulltext_source"], ("abstract", "xml"))

    def test_missing_pdf_uses_abstract(self):
        out = ingest_search_results(
            "topic", 3, api=_FakeApi(None), settings=Settings(db_path=self.db))
        self.assertEqual(out["paper_keys"], ["p-bad-pdf"])


class ApiHubSourceIsolationTest(unittest.TestCase):
    """P1-8：单个来源抛错不应中断整轮检索。"""

    class _Boom:
        def search(self, query, max_results=5):
            raise RuntimeError("429 Too Many Requests")

    class _Ok:
        def search(self, query, max_results=5):
            return [{"paper_key": "ok:1", "source": "ok", "title": "T",
                     "abstract": "a", "authors": [], "doi": None}]

    def test_one_source_failure_does_not_kill_search(self):
        hub = ApiHub(source="custom")
        hub.pubmed = self._Boom()
        hub.arxiv = self._Ok()
        from research_agent.retrieval.api_clients import SOURCE_SETS

        original = SOURCE_SETS.get("custom")
        SOURCE_SETS["custom"] = ["pubmed", "arxiv"]
        report: dict = {}
        try:
            hits = hub.search("query", max_results=5, source="custom",
                              report=report)
        finally:
            if original is None:
                SOURCE_SETS.pop("custom", None)
            else:
                SOURCE_SETS["custom"] = original
        self.assertEqual([h["paper_key"] for h in hits], ["ok:1"],
                         "单源异常穿透，整轮检索失败（P1-8 回归）")
        self.assertIn("pubmed", report.get("source_errors", {}))
        self.assertEqual(report["source_hits"]["arxiv"], 1)


class ExtractionFailureMarkingTest(unittest.TestCase):
    """P1-9：块级抽取失败要显式标记，不能记成"0 实体但成功"。"""

    class _BoomModel:
        def invoke(self, messages, **kwargs):
            raise RuntimeError("LLM 调用失败")

    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "p19.db"
        self.settings = Settings(db_path=self.db)
        conn = connect(self.db)
        ont.init_ontology(conn)
        upsert_paper(conn, {
            "paper_key": "p-fail", "source": "t", "title": "T",
            "abstract": "a", "clean_text": "正文内容。" * 20,
            "authors": [{"name": "A"}], "status": "ingested",
        })
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_chunks_failed_marks_extract_failed(self):
        node = make_knowledge_node(model=self._BoomModel(),
                                   settings=self.settings)
        out = node({"current_key": "p-fail", "quality": 0.9})
        self.assertEqual(out.get("status"), "extract_failed",
                         f"抽取失败被记为成功: {out.get('status')}")
        self.assertTrue(out.get("error"))

    def test_process_papers_survives_per_paper_failure(self):
        class _Services:
            settings = self.settings
            api = None
            retriever_model = None
            quality_model = None
            knowledge_model = None

        import research_agent.pipeline as pipeline_mod

        original = pipeline_mod.build_pipeline_graph

        class _Graph:
            def invoke(self, state):
                key = state.get("current_key")
                if key == "bad":
                    raise RuntimeError("boom")
                return {"current_key": key, "status": "extracted"}

        def _boom(services, conn=None):
            return _Graph()

        pipeline_mod.build_pipeline_graph = _boom
        try:
            # conn 传占位对象，避免真实建库；图已被替换所以不会被使用
            results = process_papers(["bad", "good"], services=_Services(),
                                     conn=object())
        finally:
            pipeline_mod.build_pipeline_graph = original
        self.assertEqual(len(results), 2, "单篇异常中断了整批处理（P1-9 回归）")
        self.assertEqual(results[0]["status"], "error")
        self.assertEqual(results[1]["status"], "extracted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
