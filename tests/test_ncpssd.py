"""NCPSSD source tests: query building, normalization, pagination and routing."""
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from research_agent.db import connect, upsert_paper
from research_agent.retrieval.api_clients import ApiHub
from research_agent.retrieval.ncpssd import (
    NcpssdClient,
    build_combined_query,
    build_search_query,
)
from tests._tmpdir import make_temp_dir


def _row(title: str = "人民战争中的群众伟力", row_id: str = "12345") -> dict:
    return {
        "id": row_id,
        "title": f"<b>{title}</b>",
        "creator": "张伟;李民",
        "creator_first": "张伟",
        "cbw_name": "思想理论教育导刊",
        "tspress": "高等教育出版社",
        "date": "2024-03-01T00:00:00.000+0000",
        "years": "2024",
        "volumn": "2024",
        "num": "3",
        "beginpage": "12",
        "endpage": "18",
        "remark": "文章说明，战争伟力深藏在人民群众之中。",
        "subject": "人民战争;人民群众;战争伟力",
        "issn": "1009-2528",
        "doi": "https://doi.org/10.1000/example",
        "pdfurl": "",
        "encryptedUrl": "token",
        "qkEncryptedUrl": "token2",
    }


class _FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        return None


class QueryBuildTest(unittest.TestCase):
    def test_plain_and_combined_queries(self):
        self.assertIn('IKTE="人民战争"', build_search_query("人民战争"))
        q = build_combined_query("战争", "人民群众")
        self.assertIn("AND", q)
        self.assertIn("战争", q)
        self.assertIn("人民群众", q)

    def test_clean_html(self):
        r = NcpssdClient._normalize(_row())
        self.assertEqual(r["paper_key"], "ncpssd:12345")
        self.assertEqual(r["title"], "人民战争中的群众伟力")
        self.assertEqual(r["venue"], "思想理论教育导刊")
        self.assertEqual(r["pub_year"], 2024)
        self.assertEqual(r["pub_date"], "2024-03-01")
        self.assertEqual(r["pages"], "12-18")
        self.assertEqual(r["doi"], "10.1000/example")
        self.assertEqual(len(r["authors"]), 2)
        self.assertIn("人民群众", r["abstract"])


class SearchPaginationTest(unittest.TestCase):
    def test_search_expression_reads_all_requested_rows(self):
        page1 = {
            "result": True,
            "data": {
                "total": 10,
                "rows": [_row(title=f"文章{i}", row_id=str(i)) for i in range(2)],
            },
        }
        page2 = {
            "result": True,
            "data": {
                "total": 10,
                "rows": [_row(title=f"文章{i}", row_id=str(i + 2)) for i in range(2)],
            },
        }
        client = NcpssdClient(poll_delay=0)
        with patch("research_agent.retrieval.ncpssd.requests.post",
                   side_effect=[_FakeResponse(page1), _FakeResponse(page2)]) as mock_post:
            rows = client.search_expression("人民战争", max_results=4, page_size=2)
        self.assertEqual(len(rows), 4)
        self.assertEqual(mock_post.call_count, 2)


class _FakeNcpssd:
    def search(self, query, max_results=10, field="all"):
        return [NcpssdClient._normalize(_row(row_id="9001"))]


class _ExplodingEnrich:
    def enrich(self, rec):
        raise AssertionError("NCPSSD should not call external enrichment")


class ApiHubNcpssdTest(unittest.TestCase):
    def test_source_ncpssd_skips_external_enrichment(self):
        hub = ApiHub(
            ncpssd=_FakeNcpssd(),
            openalex=_ExplodingEnrich(),
            crossref=_ExplodingEnrich(),
            unpaywall=_ExplodingEnrich(),
            source="ncpssd",
        )
        out = hub.search("人民战争", max_results=3)
        self.assertEqual(out[0]["source"], "ncpssd")
        self.assertEqual(out[0]["paper_key"], "ncpssd:9001")


class PaperCitationColumnsTest(unittest.TestCase):
    def test_volume_issue_pages_keywords_columns_are_not_shifted(self):
        with make_temp_dir() as tmpdir:
            db = connect(Path(tmpdir.name) / "t.db")
            rec = {
                "paper_key": "ncpssd:123",
                "source": "ncpssd",
                "title": "样本",
                "abstract": "摘要",
                "venue": "期刊",
                "pub_year": 2025,
                "authors": [{"name": "作者"}],
                "volume": "2025",
                "issue": "3",
                "pages": "5-14",
                "keywords": "人民战争;人民群众",
                "publisher": "出版社",
                "language": "zh",
                "clean_text": "摘要关键词",
                "status": "ingested",
            }
            upsert_paper(db, rec)
            row = dict(db.execute(
                """
                SELECT volume, issue, pages, keywords, publisher, language,
                       pdf_sha256, pdf_size, clean_text
                FROM papers WHERE paper_key='ncpssd:123'
                """
            ).fetchone())
            db.close()
        self.assertEqual(row["volume"], "2025")
        self.assertEqual(row["issue"], "3")
        self.assertEqual(row["pages"], "5-14")
        self.assertEqual(row["keywords"], "人民战争;人民群众")
        self.assertEqual(row["publisher"], "出版社")
        self.assertEqual(row["language"], "zh")
        self.assertIsNone(row["pdf_sha256"])
        self.assertIsNone(row["pdf_size"])
        self.assertEqual(row["clean_text"], "摘要关键词")


if __name__ == "__main__":
    unittest.main(verbosity=2)
