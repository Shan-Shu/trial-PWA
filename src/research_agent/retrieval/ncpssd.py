"""National Center for Philosophy and Social Sciences Documentation (NCPSSD).

The public search endpoint returns structured metadata and abstracts without an
API key. Older full-text PDF links point to a domain that is no longer operated
by the platform, so this client intentionally keeps only current NCPSSD links
and lets callers fall back to abstracts.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)


API_URL = "https://www.ncpssd.cn/searchHandler/search"
DEFAULT_SORT = "synUpdateType|DESC,date|DESC,ik_subject|DESC,id|DESC"
DATE_SORT = "date|DESC,id|DESC"

_FIELD_TAGS = {
    "all": ("IKTE", "IKPYTE", "IKST", "IKET", "IKSE"),
    "title": ("IKTE",),
    "keyword": ("IKST",),
    "creator": ("IKCR", "IKCE"),
    "abstract": ("IKET",),
    "subject": ("IKSE",),
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.ncpssd.cn/",
}


def clean_html(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", "", str(value))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def build_search_query(keyword: str, field: str = "all") -> str:
    """Build an exact-term Solr query over one or more indexed fields."""
    tags = _FIELD_TAGS.get(field, _FIELD_TAGS["all"])
    kw = clean_html(keyword)
    conditions = [f'{tag}="{kw}"' for tag in tags]
    return "(" + " OR ".join(conditions) + ")"


def build_combined_query(*terms: str,
                         fields: tuple[str, ...] = ("IKTE", "IKST", "IKET")) -> str:
    """Build an AND expression, useful for intersecting distinct Chinese concepts."""
    clauses: list[str] = []
    for term in terms:
        term = clean_html(term)
        if term:
            clauses.append(
                "(" + " OR ".join(f'{tag}="{term}"' for tag in fields) + ")"
            )
    if not clauses:
        return ""
    if len(clauses) == 1:
        return clauses[0]
    return " AND ".join(clauses)


class NcpssdClient:
    """NCPSSD open search client.

    API responses are cached in-page style JSON, so no official OAuth/API key is
    required. Use search() for plain keywords and search_expression() when the
    caller already built a Solr-style query.
    """

    def __init__(self, timeout: int | None = None,
                 poll_delay: float = 1.0,
                 max_retries: int = 3) -> None:
        self.timeout = timeout or 60
        self.poll_delay = poll_delay
        self.max_retries = max(1, max_retries)

    def _post(self, payload: dict[str, str]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = requests.post(
                    API_URL,
                    data=urlencode(payload),
                    headers=_HEADERS,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(2.0 * (attempt + 1) + self.poll_delay)
        raise last_error if last_error else RuntimeError("NCPSSD request failed")

    def fetch_page(self, query: str, page: int = 1, page_size: int = 20,
                   sort: str = DATE_SORT) -> dict[str, Any]:
        payload = {
            "search": query,
            "pageNum": str(int(page)),
            "pageSize": str(int(page_size)),
            "sort": sort,
            "sType": "0",
            "ajaxKeys": "",
            "customShowCondition": clean_html(query)[:80],
        }
        return self._post(payload)

    def search_expression(self, query: str, max_results: int = 10,
                          page_size: int | None = None,
                          sort: str = DATE_SORT) -> list[dict[str, Any]]:
        if not clean_html(query):
            return []
        limit = max(1, int(max_results))
        size = min(100, max(1, int(page_size or min(limit, 100))))
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 1
        while len(rows) < limit:
            data = self.fetch_page(query, page=page, page_size=size, sort=sort)
            if not bool(data.get("result")):
                logger.warning("NCPSSD search rejected query: %s", query[:120])
                break
            batch = ((data.get("data") or {}).get("rows")) or []
            if not batch:
                break
            for row in batch:
                key = f'{row.get("id") or row.get("data_id")}|{clean_html(row.get("title"))}'
                if key in seen:
                    continue
                seen.add(key)
                rows.append(self._normalize(row))
                if len(rows) >= limit:
                    break
            total = int(((data.get("data") or {}).get("total")) or 0)
            if page * size >= total or len(batch) < size:
                break
            page += 1
            time.sleep(self.poll_delay)
        return rows[:limit]

    def search(self, keyword: str, max_results: int = 10,
               field: str = "all",
               page_size: int | None = None) -> list[dict[str, Any]]:
        return self.search_expression(
            build_search_query(keyword, field), max_results=max_results,
            page_size=page_size)

    @staticmethod
    def _normalize(row: dict[str, Any]) -> dict[str, Any]:
        row_id = str(row.get("id") or row.get("data_id") or "").strip()
        title = clean_html(row.get("title") or row.get("title_auto") or "")
        raw_authors = (
            row.get("creator")
            or row.get("creator_first")
            or ""
        )
        if not raw_authors and isinstance(row.get("creator_auto"), list):
            raw_authors = ";".join(str(x) for x in row["creator_auto"])
        author_names = [
            a.strip() for a in re.split(r"[;；,，、]+", str(raw_authors or ""))
            if a.strip()
        ]
        authors = [{"name": a, "affiliations": []} for a in author_names]

        raw_date = str(row.get("date") or row.get("pubdate") or "")
        year_raw = str(row.get("years") or "")[:4]
        pub_year = None
        try:
            pub_year = int(year_raw) if year_raw.isdigit() else None
        except (TypeError, ValueError):
            pub_year = None
        if not pub_year:
            m = re.match(r"\s*(\d{4})", raw_date)
            if m:
                pub_year = int(m.group(1))
        pub_date = raw_date[:10].replace("T", " ") if raw_date else None
        if pub_date and len(pub_date) < 10:
            pub_date = None
        if not pub_date and pub_year:
            pub_date = f"{pub_year}-01-01"

        abstract = clean_html(
            row.get("remark")
            or row.get("ik_remark")
            or row.get("introduce")
            or row.get("preface")
        )
        keywords = clean_html(
            row.get("subject") or row.get("ik_subject") or ""
        )
        doi = str(row.get("doi") or "").strip()
        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
        pdf_url = str(row.get("pdfurl") or "").strip()
        if pdf_url and not pdf_url.startswith("https://www.ncpssd.cn"):
            pdf_url = ""
        venue = clean_html(row.get("cbw_name") or row.get("bookname") or "")
        publisher = clean_html(row.get("tspress") or "")
        pages = ""
        begin = str(row.get("beginpage") or "").strip()
        end = str(row.get("endpage") or "").strip()
        if begin and end:
            pages = f"{begin}-{end}"
        elif begin:
            pages = begin

        return {
            "paper_key": f"ncpssd:{row_id}" if row_id else None,
            "source": "ncpssd",
            "title": title,
            "abstract": abstract,
            "doi": doi or None,
            "venue": venue,
            "venue_issn": clean_html(row.get("issn") or "") or None,
            "publisher": publisher,
            "source_type": "journal",
            "pub_year": pub_year,
            "pub_date": pub_date,
            "publication_status": "Published",
            "authors": authors,
            "citation_count": None,
            "volume": clean_html(row.get("volumn") or ""),
            "issue": clean_html(row.get("num") or ""),
            "pages": pages,
            "keywords": keywords,
            "language": "zh",
            "pdf_url": pdf_url or None,
            "ncpssd_id": row_id,
        }
