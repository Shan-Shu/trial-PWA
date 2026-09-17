"""PubMed 文献源客户端（经 NCBI E-utilities + Europe PMC）。

PubMed 本身不提供 PDF/全文，这里组合两个官方通道：
1. NCBI E-utilities（esearch + efetch）：检索 PubMed 并解析
   标题/作者/单位/期刊/年份/DOI/PMID/PMCID；
2. Europe PMC REST（fullTextXML）：对 OA(PMC) 文献拉取全文文本；
   PDF 仅作尽力而为（Europe PMC pdf render，可能被限流，失败回退全文文本/摘要）。
"""
from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests

from research_agent.config import settings

logger = logging.getLogger(__name__)


def _text(el: ET.Element | None, default: str | None = None) -> str | None:
    if el is None:
        return default
    return "".join(el.itertext()).strip() or default


def _collapse(text: str | None) -> str | None:
    if not text:
        return text
    return re.sub(r"\s+", " ", text).strip()


class PubMedClient:
    """PubMed 检索（esearch/efetch）与全文（Europe PMC）客户端。"""

    EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    EUROPE = "https://www.ebi.ac.uk/europepmc/webservices/rest"

    def __init__(self, delay: float = 0.35, email: str | None = None) -> None:
        self.delay = max(0.1, delay)      # 遵守 NCBI 限速（无 Key 时 ≤3 req/s）
        self.email = email or os.getenv("PUBMED_EMAIL", "research-agent@example.com")
        self.api_key = os.getenv("NCBI_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": settings.user_agent})

    def _params(self, **kw) -> dict:
        params = {"tool": "research-agent", "email": self.email}
        if self.api_key:
            params["api_key"] = self.api_key
        params.update(kw)
        return params

    # ---------------- 检索 ----------------
    def search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        """PubMed 检索 → 逐条解析元数据。"""
        pmids = self.esearch(query, max_results=max_results)
        recs: list[dict[str, Any]] = []
        for i, pmid in enumerate(pmids):
            try:
                rec = self.fetch_by_pmid(pmid)
                if rec:
                    recs.append(rec)
            except Exception as exc:  # noqa: BLE001
                logger.warning("PubMed 解析失败 %s: %s", pmid, exc)
            if i < len(pmids) - 1:
                time.sleep(self.delay)
        return recs

    def esearch(self, query: str, max_results: int = 10) -> list[str]:
        resp = self.session.get(
            f"{self.EUTILS}/esearch.fcgi", params=self._params(
                db="pubmed", term=query, retmax=max_results, retmode="json"),
            timeout=settings.http_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return list((data.get("esearchresult") or {}).get("idlist") or [])

    def fetch_by_pmid(self, pmid: str) -> dict[str, Any] | None:
        resp = self.session.get(
            f"{self.EUTILS}/efetch.fcgi", params=self._params(
                db="pubmed", id=pmid, retmode="xml", rettype="abstract"),
            timeout=settings.http_timeout,
        )
        resp.raise_for_status()
        return self._parse_efetch_xml(resp.text, pmid)

    # ---------------- XML 解析 ----------------
    @staticmethod
    def _parse_efetch_xml(xml: str, pmid_hint: str) -> dict[str, Any] | None:
        root = ET.fromstring(xml)
        article = root.find(".//PubmedArticle")
        if article is None:
            return None
        mc = article.find("MedlineCitation")
        if mc is None:
            mc = article
        art = mc.find("Article")
        if art is None:
            art = article

        pmid = _text(mc.find("PMID")) or pmid_hint
        title = _collapse(_text(art.find("ArticleTitle")))
        journal = art.find("Journal")
        if journal is None:
            journal = art.find(".//Journal")
        venue = _collapse(_text(journal.find("Title") if journal is not None else None)) \
            or _collapse(_text(journal.find("ISOAbbreviation")) if journal is not None else None)
        issn = None
        if journal is not None:
            issn = _text(journal.find("ISSN"))
        issue = journal.find("JournalIssue") if journal is not None else None
        year = None
        month = day = None
        if issue is not None:
            pd = issue.find("PubDate")
            if pd is not None:
                year = _text(pd.find("Year"))
                month = _text(pd.find("Month"))
                day = _text(pd.find("Day"))
        if not year:  # 兜底：MedlineDate 形如 "2024 Sep-Oct"
            med = _text(issue.find("PubDate/MedlineDate")) if issue is not None else None
            if med:
                m = re.search(r"\b(19|20)\d{2}\b", med)
                if m:
                    year = m.group(0)
        try:
            pub_year = int(year) if year else None
        except ValueError:
            pub_year = None
        month_num = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }.get((month or "").lower()[:3])
        pub_date = None
        if pub_year:
            pub_date = f"{pub_year}-{month_num:02d}-{int(day):02d}" if month_num and day \
                else f"{pub_year}-01-01"

        # DOI / PMCID
        doi = None
        pmcid = None
        for aid in article.findall(".//ArticleId"):
            if aid.get("IdType") == "doi":
                doi = _collapse(_text(aid))
            elif aid.get("IdType") == "pmc":
                pmcid = _text(aid)
        if not doi:
            doi = _collapse(_text(art.find("ELocationID[@EIdType='doi']")))

        # 摘要（多段 AbstractText）
        abstract_parts = []
        for at in art.findall(".//Abstract/AbstractText"):
            label = at.get("Label")
            t = _collapse(_text(at))
            if t:
                abstract_parts.append(f"{label}: {t}" if label else t)
        abstract = "\n\n".join(abstract_parts) or None

        # 作者 + 单位
        authors = []
        for au in art.findall(".//AuthorList/Author"):
            last = _text(au.find("LastName"))
            fore = _text(au.find("ForeName"))
            coll = _collapse(_text(au.find("CollectiveName")))
            if coll:
                authors.append({"name": coll, "affiliations": []})
                continue
            name = " ".join(x for x in (fore, last) if x) or "Unknown"
            affs = []
            for aff in au.findall("AffiliationInfo/Affiliation"):
                t = _collapse(_text(aff))
                if t and t not in affs:
                    affs.append(t)
            authors.append({"name": name, "affiliations": affs})

        return {
            "paper_key": f"pubmed:{pmid}",
            "source": "pubmed",
            "title": title,
            "abstract": abstract,
            "doi": doi,
            "venue": venue,
            "venue_issn": issn,
            "source_type": "journal",
            "pub_year": pub_year,
            "pub_date": pub_date,
            "publication_status": "Published",
            "authors": authors,
            "pmcid": pmcid,
            "pdf_url": None,
        }

    # ---------------- 全文（Europe PMC，OA 才有） ----------------
    def fetch_fulltext_text(self, pmcid: str, timeout: int = 45) -> str | None:
        """拉取 OA 全文 XML 并转为纯文本；失败/非 OA 返回 None。"""
        try:
            resp = self.session.get(
                f"{self.EUROPE}/{pmcid}/fullTextXML", timeout=timeout)
            if resp.status_code != 200:
                return None
            return self._xml_to_text(resp.content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Europe PMC 全文拉取失败 %s: %s", pmcid, exc)
            return None

    @staticmethod
    def _xml_to_text(xml_bytes: bytes) -> str:
        root = ET.fromstring(xml_bytes)
        parts: list[str] = []
        for el in root.iter():
            tag = (el.tag or "").rsplit("}", 1)[-1].lower()
            if tag in ("p", "title", "caption"):
                t = _collapse(_text(el))
                if t and len(t) > 2:
                    parts.append(t)
        # 去重相邻重复（段落可能与标题重叠）
        seen: list[str] = []
        for p in parts:
            if not seen or seen[-1].lower() != p.lower():
                seen.append(p)
        return "\n\n".join(seen)
