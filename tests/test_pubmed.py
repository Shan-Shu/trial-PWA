"""PubMed 接入离线测试：efetch XML 解析 / 全文 XML 转文本 / 跨源去重。"""
from __future__ import annotations

import unittest

from research_agent.retrieval.api_clients import ApiHub
from research_agent.retrieval.pubmed import PubMedClient


EFETCH_XML = """<?xml version="1.0" ?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation Status="PubMed-in-Process" Owner="NLM">
      <PMID Version="1">99999999</PMID>
      <Article PubModel="Print">
        <Journal>
          <ISSN IssnType="Print">0142-9612</ISSN>
          <JournalIssue CitedMedium="Internet">
            <Volume>45</Volume>
            <PubDate><Year>2024</Year><Month>Mar</Month><Day>01</Day></PubDate>
          </JournalIssue>
          <Title>Biomaterials</Title>
          <ISOAbbreviation>Biomaterials</ISOAbbreviation>
        </Journal>
        <ArticleTitle>Novel bone repair scaffolds for critical-size defects</ArticleTitle>
        <ELocationID EIdType="doi">10.1016/j.biomaterials.2024.122345</ELocationID>
        <Abstract>
          <AbstractText Label="Background">Background text here.</AbstractText>
          <AbstractText>Main abstract sentence about scaffolds.</AbstractText>
        </Abstract>
        <AuthorList CompleteYN="Y">
          <Author ValidYN="Y">
            <LastName>Zhang</LastName><ForeName>Wei</ForeName><Initials>W</Initials>
            <AffiliationInfo><Affiliation>Department of Orthopedics, Fudan University</Affiliation></AffiliationInfo>
          </Author>
          <Author><CollectiveName>BoneRegen Consortium</CollectiveName></Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">99999999</ArticleId>
        <ArticleId IdType="doi">10.1016/j.biomaterials.2024.122345</ArticleId>
        <ArticleId IdType="pmc">PMC88888888</ArticleId>
      </ArticleIdList>
      <PublicationStatus>ppublish</PublicationStatus>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


class PubMedParseTest(unittest.TestCase):
    def test_parse_efetch_record(self):
        rec = PubMedClient._parse_efetch_xml(EFETCH_XML, "99999999")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["paper_key"], "pubmed:99999999")
        self.assertEqual(rec["source"], "pubmed")
        self.assertEqual(rec["venue"], "Biomaterials")
        self.assertEqual(rec["venue_issn"], "0142-9612")
        self.assertEqual(rec["pub_year"], 2024)
        self.assertEqual(rec["pub_date"], "2024-03-01")
        self.assertEqual(rec["doi"], "10.1016/j.biomaterials.2024.122345")
        self.assertEqual(rec["pmcid"], "PMC88888888")
        self.assertIn("scaffolds", rec["title"])
        self.assertIn("Background text here.", rec["abstract"])
        self.assertEqual(rec["authors"][0]["name"], "Wei Zhang")
        self.assertIn("Fudan University", rec["authors"][0]["affiliations"][0])
        self.assertEqual(rec["authors"][1]["name"], "BoneRegen Consortium")

    def test_xml_to_text(self):
        xml = (b"<article><body><sec><title>Introduction</title>"
               b"<p>Para one text.</p><p>Para two text.</p></sec></body></article>")
        text = PubMedClient._xml_to_text(xml)
        self.assertIn("Para one text.", text)
        self.assertIn("Para two text.", text)


class _FakeSearcher:
    def __init__(self, recs):
        self.recs = recs

    def search(self, query, max_results=10):
        return self.recs


class _Passthrough:
    def enrich(self, rec):
        return dict(rec)

    def lookup_by_doi(self, doi):
        return None

    def lookup_by_title(self, title):
        return None


class _NoUnpaywall:
    def lookup_by_doi(self, doi):
        return None


class ApiHubSourceTest(unittest.TestCase):
    def test_both_sources_dedupe_by_doi(self):
        pubmed_rec = {
            "paper_key": "pubmed:1", "source": "pubmed", "title": "P",
            "doi": "10.1000/xyz", "venue": "J Bone", "pub_year": 2024,
            "authors": [{"name": "A", "affiliations": ["U"]}],
        }
        arxiv_rec = {
            "paper_key": "arxiv:2401.00001", "source": "arxiv", "title": "P",
            "doi": "10.1000/xyz", "venue": "arXiv", "pub_year": 2024,
            "authors": [{"name": "A", "affiliations": []}],
        }
        hub = ApiHub(arxiv=_FakeSearcher([arxiv_rec]),
                     openalex=_Passthrough(), crossref=_Passthrough(),
                     pubmed=_FakeSearcher([pubmed_rec]),
                     unpaywall=_NoUnpaywall(),
                     source="both")
        out = hub.search("test", max_results=5)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["paper_key"], "pubmed:1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
