"""新增多源检索测试：Europe PMC / Semantic Scholar / OpenAlex / fulltext 路由。"""
from __future__ import annotations

import unittest

from research_agent.retrieval.api_clients import ApiHub, OpenAlexClient
from research_agent.retrieval.extended import (
    EuropePmcClient,
    SemanticScholarSearcher,
)


class SourceNormalizationTest(unittest.TestCase):
    def test_europepmc_picks_open_access_pdf(self):
        r = EuropePmcClient._normalize({
            "id": "1234",
            "source": "MED",
            "pmid": "1234",
            "pmcid": "PMC888",
            "title": "OA paper",
            "abstractText": "abstract",
            "doi": "10.1000/oa",
            "journalTitle": "Bone Research",
            "authorString": "Alice Zhang, Bob Li",
            "pubYear": 2025,
            "isOpenAccess": "Y",
            "fullTextUrlList": {
                "fullTextUrl": [
                    {
                        "url": "https://example.com/paper.pdf",
                        "documentStyle": "pdf",
                        "availability": "Open access",
                    },
                    {
                        "url": "https://example.com/landing",
                        "documentStyle": "html",
                        "availability": "Open access",
                    },
                ]
            },
        })
        self.assertEqual(r["source"], "europepmc")
        self.assertEqual(r["pmcid"], "PMC888")
        self.assertEqual(r["pdf_url"], "https://example.com/paper.pdf")
        self.assertEqual(len(r["authors"]), 2)

    def test_semantic_scholar_open_access_pdf(self):
        r = SemanticScholarSearcher._normalize({
            "paperId": "abc123",
            "title": "A paper",
            "year": 2025,
            "venue": "Journal",
            "publicationDate": "2025-03-01",
            "externalIds": {"DOI": "10.1000/x", "ArXiv": "2401.0001"},
            "authors": [{"name": "Alice"}, {"name": "Bob"}],
            "openAccessPdf": {"url": "https://oa.example/paper.pdf"},
            "isOpenAccess": True,
        })
        self.assertEqual(r["paper_key"], "semantic_scholar:abc123")
        self.assertEqual(r["pdf_url"], "https://oa.example/paper.pdf")
        self.assertEqual(r["arxiv_id"], "2401.0001")

    def test_openalex_abstract_and_oa_fields(self):
        r = OpenAlexClient._normalize_publication({
            "id": "https://openalex.org/W123",
            "title": "Open work",
            "publication_year": 2025,
            "publication_date": "2025-06-01",
            "doi": "https://doi.org/10.1000/oa",
            "abstract_inverted_index": {
                "scaffolds": [1],
                "promote": [2],
                "osteogenesis": [3],
                "Some": [0],
            },
            "best_oa_location": {
                "pdf_url": "https://oa.example/w123.pdf",
                "source": {"display_name": "Bone Journal", "type": "journal"},
            },
            "primary_location": {},
            "open_access": {"is_oa": True},
            "ids": {"pmcid": "PMC999"},
            "authorships": [
                {
                    "author": {"display_name": "Alice", "orcid": None},
                    "institutions": [{"display_name": "Fudan University"}],
                }
            ],
        })
        self.assertEqual(r["paper_key"], "openalex:W123")
        self.assertEqual(r["pmcid"], "PMC999")
        self.assertEqual(r["abstract"], "Some scaffolds promote osteogenesis")
        self.assertEqual(r["pdf_url"], "https://oa.example/w123.pdf")
        self.assertEqual(r["authors"][0]["affiliations"], ["Fudan University"])


class _FakeOpenAlex:
    def search_publications(self, query, per_page=5, open_access_only=True):
        return [{
            "paper_key": "openalex:W1",
            "source": "openalex",
            "title": "OpenAlex item",
            "doi": "10.1000/openalex",
            "venue": "J",
            "pub_year": 2025,
            "authors": [{"name": "Alice", "affiliations": []}],
        }]

    def enrich(self, rec):
        return dict(rec)


class _FakeUnpaywall:
    def lookup_by_doi(self, doi):
        return None


class _FakeEuro:
    def search(self, query, max_results=10, open_access_only=True):
        return [{
            "paper_key": "europepmc:MED:1",
            "source": "europepmc",
            "title": "OA Europe item",
            "doi": "10.1000/shared",
            "venue": "J Bone",
            "pub_year": 2025,
            "authors": [{"name": "Alice", "affiliations": []}],
        }]


class _FakeArxiv:
    def search(self, query, max_results=10):
        return [{
            "paper_key": "arxiv:2401.00001",
            "source": "arxiv",
            "title": "Arxiv duplicate",
            "doi": "10.1000/shared",
            "venue": "arXiv",
            "pub_year": 2025,
            "authors": [{"name": "Alice", "affiliations": []}],
        }]


class _FakeSemantic:
    def search(self, query, max_results=10):
        return [{
            "paper_key": "semantic_scholar:S1",
            "source": "semantic_scholar",
            "title": "Semantic item",
            "doi": None,
            "pub_year": 2025,
            "authors": [{"name": "Alice", "affiliations": []}],
        }]


class _FakeCrossref:
    def lookup_by_doi(self, doi):
        return None

    def lookup_by_title(self, title):
        return None


class FulltextRoutingTest(unittest.TestCase):
    def test_fulltext_source_order_and_dedupe(self):
        hub = ApiHub(
            europepmc=_FakeEuro(),
            arxiv=_FakeArxiv(),
            semantic_scholar=_FakeSemantic(),
            openalex=_FakeOpenAlex(),
            crossref=_FakeCrossref(),
            unpaywall=_FakeUnpaywall(),
            source="fulltext",
        )
        out = hub.search("bone scaffold", max_results=5)
        self.assertEqual(
            [r["source"] for r in out],
            ["europepmc", "semantic_scholar", "openalex"])
        self.assertEqual(out[0]["paper_key"], "europepmc:MED:1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
