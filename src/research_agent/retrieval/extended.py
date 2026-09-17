"""补充文献来源：Europe PMC / Semantic Scholar / Unpaywall。

- Europe PMC：既是检索库，也是 OA 全文提供方；
- Semantic Scholar：检索 + Open Access PDF 定位；
- Unpaywall：按 DOI 补 OA PDF，不参与检索。
"""
from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

import requests

from research_agent.config import settings

logger = logging.getLogger(__name__)


def _get_json(url: str, params: dict | None = None, timeout: int | None = None,
              headers: dict | None = None) -> dict | None:
    try:
        resp = requests.get(
            url, params=params, timeout=timeout or settings.http_timeout,
            headers=headers or {"User-Agent": settings.user_agent},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("外部检索请求失败 %s: %s", url, exc)
        return None


class EuropePmcClient:
    """Europe PMC REST 检索：返回带 PMCID/PMID/DOI/OA PDF 的规范记录。"""

    BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"

    def search(self, query: str, max_results: int = 10,
               open_access_only: bool = True) -> list[dict[str, Any]]:
        q = query or ""
        if open_access_only and "OPEN_ACCESS" not in q.upper():
            q = f"({q}) AND (OPEN_ACCESS:Y)"
        data = _get_json(
            f"{self.BASE}/search",
            params={
                "query": q,
                "format": "json",
                "pageSize": max_results,
                "resultType": "core",
            },
            timeout=45,
        )
        results = ((data or {}).get("resultList") or {}).get("result") or []
        return [self._normalize(r) for r in results]

    def search_all(self, query: str, limit: int = 300,
                   page_size: int = 100,
                   open_access_only: bool = True) -> list[dict[str, Any]]:
        """分页检索 Europe PMC，用于数百篇候选批量获取。"""
        q = query or ""
        if open_access_only and "OPEN_ACCESS" not in q.upper():
            q = f"({q}) AND (OPEN_ACCESS:Y)"
        out: list[dict[str, Any]] = []
        cursor = "*"
        while len(out) < limit:
            data = _get_json(
                f"{self.BASE}/search",
                params={
                    "query": q,
                    "format": "json",
                    "pageSize": page_size,
                    "cursorMark": cursor,
                    "resultType": "core",
                },
                timeout=60,
            )
            results = ((data or {}).get("resultList") or {}).get("result") or []
            if not results:
                break
            out.extend(self._normalize(r) for r in results)
            nxt = (data or {}).get("nextCursorMark")
            if not nxt or nxt == cursor or len(results) < page_size:
                break
            cursor = nxt
        return out[:limit]

    @staticmethod
    def _normalize(r: dict[str, Any]) -> dict[str, Any]:
        src = r.get("source") or "EPMC"
        rid = r.get("id") or ""
        pmid = r.get("pmid")
        pmcid = r.get("pmcid")
        key = f"europepmc:{src}:{rid}"
        authors = []
        for name in (r.get("authorString") or "").split(","):
            name = name.strip()
            if name:
                authors.append({"name": name, "affiliations": []})
        pdf_url = None
        for fu in ((r.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
            style = (fu.get("documentStyle") or "").lower()
            availability = (fu.get("availability") or "").lower()
            url = fu.get("url") or ""
            if style == "pdf" and "open access" in availability and url:
                pdf_url = url
                break
        return {
            "paper_key": key,
            "source": "europepmc",
            "title": (r.get("title") or "").strip(),
            "abstract": (r.get("abstractText") or "").strip(),
            "doi": r.get("doi"),
            "venue": r.get("journalTitle") or ((r.get("journalInfo") or {}).get("journal") or {}).get(
                "title"),
            "source_type": "journal" if r.get("journalTitle") else None,
            "pub_year": r.get("pubYear"),
            "pub_date": r.get("pubDate") or r.get("firstPublicationDate"),
            "publication_status": "Published",
            "authors": authors,
            "pmid": pmid,
            "pmcid": pmcid,
            "is_open_access": bool(r.get("isOpenAccess")),
            "pdf_url": pdf_url,
        }

    @staticmethod
    def _source_id(rec: dict[str, Any]) -> tuple[str, str] | None:
        """从已规范化记录中解析 Europe PMC source/id。"""
        key = str(rec.get("paper_key") or "")
        if key.startswith("europepmc:"):
            parts = key.split(":", 2)
            if len(parts) == 3 and parts[1] and parts[2]:
                return parts[1].upper(), parts[2]
        if rec.get("pmid"):
            return "MED", str(rec["pmid"])
        if rec.get("pmcid"):
            return "PMC", str(rec["pmcid"]).lstrip("PMC")
        return None

    @staticmethod
    def _normalize_reference(row: dict[str, Any]) -> dict[str, Any]:
        source = str(row.get("source") or "MED").upper()
        rid = str(row.get("id") or row.get("pmid") or row.get("pmcid") or "").strip()
        title = str(row.get("title") or row.get("fullTitle") or "").strip()
        pmid = str(row.get("pmid") or "").strip()
        pmcid = str(row.get("pmcid") or "").strip()
        if source not in ("MED", "PMC"):
            source = "MED" if pmid else "PMC"
        if rid and source:
            key = f"europepmc:{source}:{rid}"
        elif pmid:
            key = f"pubmed:{pmid}"
        elif title:
            key = f"europepmc:title:{title.lower()[:120]}"
        else:
            key = None
        return {
            "paper_key": key,
            "source": "europepmc",
            "title": title,
            "abstract": None,
            "doi": str(row.get("doi") or "").strip() or None,
            "venue": (str(row.get("journalTitle") or "").strip()
                      or str(row.get("journalAbbreviation") or "").strip() or None),
            "pub_year": row.get("pubYear"),
            "publication_status": "Published",
            "authors": [
                {"name": n.strip(), "affiliations": []}
                for n in str(row.get("authorString") or "").split(",") if n.strip()
            ],
            "pmid": pmid or None,
            "pmcid": pmcid or None,
            "pdf_url": None,
            "is_open_access": None,
        }

    def _fetch_cited_list(self, rec: dict[str, Any],
                          endpoint: str) -> list[dict[str, Any]]:
        parsed = self._source_id(rec)
        if parsed is None:
            return []
        source, rid = parsed
        data = _get_json(
            f"{self.BASE}/{source}/{rid}/{endpoint}",
            params={"format": "json", "pageSize": 100, "page": 1},
            timeout=45,
        )
        if not data:
            return []
        key = f"{endpoint}List"
        items = ((data.get(key) or {}).get(endpoint.rstrip("s")) or [])
        return [self._normalize_reference(r) for r in items]

    def references(self, rec: dict[str, Any]) -> list[dict[str, Any]]:
        """返回某条 Europe PMC/PubMed 记录的参考文献列表。"""
        return self._fetch_cited_list(rec, "references")

    def citations(self, rec: dict[str, Any]) -> list[dict[str, Any]]:
        """返回某条记录在 Europe PMC 中被引用的论文。"""
        return self._fetch_cited_list(rec, "citations")


class SemanticScholarSearcher:
    """Semantic Scholar Graph API：检索 + Open Access PDF 定位。"""

    BASE = "https://api.semanticscholar.org/graph/v1/paper/search"

    def __init__(self) -> None:
        self.api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()

    def search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        headers = {"User-Agent": settings.user_agent}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        data = _get_json(
            self.BASE,
            params={
                "query": query,
                "limit": max_results,
                "fields": ",".join([
                    "paperId", "title", "abstract", "year", "venue",
                    "publicationVenue", "publicationDate", "authors",
                    "externalIds", "openAccessPdf", "isOpenAccess",
                ]),
            },
            timeout=30,
            headers=headers,
        )
        results = (data or {}).get("data") or []
        return [self._normalize(r) for r in results]

    @staticmethod
    def _normalize(r: dict[str, Any]) -> dict[str, Any]:
        paper_id = r.get("paperId") or ""
        ext = r.get("externalIds") or {}
        oa = r.get("openAccessPdf") or {}
        venue_obj = r.get("publicationVenue") or {}
        authors = [
            {"name": a.get("name"), "affiliations": []}
            for a in (r.get("authors") or [])
            if a.get("name")
        ]
        return {
            "paper_key": f"semantic_scholar:{paper_id}" if paper_id else None,
            "source": "semantic_scholar",
            "title": (r.get("title") or "").strip(),
            "abstract": (r.get("abstract") or "").strip(),
            "doi": ext.get("DOI"),
            "venue": (venue_obj.get("name") if venue_obj else None) or r.get("venue"),
            "source_type": "journal" if venue_obj else None,
            "pub_year": r.get("year"),
            "pub_date": r.get("publicationDate"),
            "publication_status": "Published" if r.get("publicationDate") else None,
            "authors": authors,
            "is_open_access": bool(r.get("isOpenAccess")),
            "pdf_url": oa.get("url"),
            "arxiv_id": ext.get("ArXiv"),
            "pmid": ext.get("PubMed"),
        }


class UnpaywallClient:
    """Unpaywall OA PDF 补全；按 DOI 查询，不负责检索。"""

    BASE = "https://api.unpaywall.org/v2"

    def __init__(self) -> None:
        self.email = os.getenv("UNPAYWALL_EMAIL", "research-agent@example.com").strip()

    def lookup_by_doi(self, doi: str) -> dict[str, Any] | None:
        if not self.email:
            return None
        data = _get_json(
            f"{self.BASE}/{quote(doi.strip(), safe='')}",
            params={"email": self.email},
            timeout=30,
        )
        if not data:
            return None
        best = data.get("best_oa_location") or {}
        pdf = best.get("url_for_pdf")
        if not pdf:
            pdf = best.get("url") if (best.get("url") or "").lower().endswith(".pdf") else None
        return {
            "doi": data.get("doi"),
            "pdf_url": pdf,
            "is_oa": bool(data.get("is_oa")),
            "oa_status": data.get("oa_status"),
        }
