"""科研文献 API 客户端：arXiv / OpenAlex / Crossref + PDF 下载 + 元数据合并。

所有外部调用均带超时与容错：单源失败不影响整体流程（返回 None / 空结果）。
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

import requests

from research_agent.config import settings
from research_agent.retrieval.extended import (
    EuropePmcClient,
    SemanticScholarSearcher,
    UnpaywallClient,
)
from research_agent.retrieval.ncpssd import NcpssdClient
from research_agent.retrieval.pubmed import PubMedClient

logger = logging.getLogger(__name__)


def _safe_get(url: str, params: dict | None = None, timeout: int | None = None) -> dict | None:
    try:
        resp = requests.get(
            url, params=params, timeout=timeout or settings.http_timeout,
            headers={"User-Agent": settings.user_agent},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001 —— 外部服务不可用不应中断流程
        logger.warning("HTTP 请求失败 %s: %s", url, exc)
        return None


def download_pdf(url: str, timeout: int | None = None) -> bytes | None:
    """下载 PDF 二进制内容；失败返回 None。"""
    try:
        resp = requests.get(
            url, timeout=timeout or settings.http_timeout,
            headers={"User-Agent": settings.user_agent},
            stream=True,
        )
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if "pdf" not in ctype and not resp.url.lower().endswith(".pdf"):
            # 付费墙 HTML 等非 PDF 响应不再当作 PDF 返回（会导致解析失败并丢记录）
            logger.warning("疑似非 PDF 响应，跳过: %s (%s)", resp.url, ctype)
            return None
        return resp.content
    except Exception as exc:  # noqa: BLE001
        logger.warning("PDF 下载失败 %s: %s", url, exc)
        return None


def _canonical_authors(authors: list[dict]) -> list[dict]:
    out = []
    for a in authors or []:
        out.append({
            "name": a.get("name") or " ".join(
                filter(None, [a.get("given"), a.get("family")])
            ) or "Unknown",
            "given": a.get("given"),
            "family": a.get("family"),
            "orcid": a.get("orcid"),
            "h_index": a.get("h_index"),
            "affiliations": list(a.get("affiliations") or []),
        })
    return out


def merge_metadata(base: dict[str, Any], extra: dict[str, Any] | None) -> dict[str, Any]:
    """用 extra 补全 base 中缺失的字段（不覆盖已有值）。作者按姓名位置合并补全。"""
    if not extra:
        return base
    rec = dict(base)
    for key in ("title", "abstract", "doi", "venue", "venue_issn", "source_type",
                "pub_year", "pub_date", "publication_status", "citation_count",
                "avg_h_index", "pdf_url"):
        if (rec.get(key) in (None, "", []) ) and extra.get(key) not in (None, ""):
            rec[key] = extra[key]
    # 作者补全：位置对齐，缺 name 或 affiliations 时用 extra 填充
    base_authors = list(rec.get("authors") or [])
    extra_authors = list(extra.get("authors") or [])
    if not base_authors:
        rec["authors"] = _canonical_authors(extra_authors)
    elif extra_authors:
        merged = []
        for i, a in enumerate(base_authors):
            b = extra_authors[i] if i < len(extra_authors) else {}
            affs = list(a.get("affiliations") or b.get("affiliations") or [])
            if not a.get("name") and b.get("name"):
                a["name"] = b["name"]
            merged.append({
                **a,
                "affiliations": affs,
                "h_index": a.get("h_index") or b.get("h_index"),
                "orcid": a.get("orcid") or b.get("orcid"),
            })
        rec["authors"] = merged
    return rec


class ArxivSearcher:
    """arXiv 检索：命中结果自带 PDF 地址，可直接下载。"""

    def search(self, query: str, max_results: int = 5) -> list[dict]:
        try:
            import arxiv
        except ImportError:
            logger.warning("缺少 arxiv 包")
            return []
        results: list[dict] = []
        try:
            client = arxiv.Client(page_size=max_results, delay_seconds=1, num_retries=1)
            for r in client.results(arxiv.Search(query=query, max_results=max_results)):
                short_id = r.get_short_id()
                pdf_url = getattr(r, "pdf_url", None) or f"https://arxiv.org/pdf/{short_id}"
                results.append({
                    "paper_key": f"arxiv:{short_id}",
                    "source": "arxiv",
                    "title": (r.title or "").strip().replace("\n", " "),
                    "abstract": (r.summary or "").strip().replace("\n", " "),
                    "doi": r.doi,
                    "venue": "arXiv",
                    "venue_issn": None,
                    "source_type": "repository",
                    "pub_year": r.published.year if r.published else None,
                    "pub_date": r.published.date().isoformat() if r.published else None,
                    "publication_status": "Preprint",
                    "citation_count": None,
                    "avg_h_index": None,
                    "authors": [{"name": a.name} for a in (r.authors or [])],
                    "pdf_url": pdf_url,
                })
        except Exception as exc:  # noqa: BLE001
            logger.warning("arXiv 检索失败: %s", exc)
        return results


class OpenAlexClient:
    """OpenAlex：被引、来源（期刊）信息、作者单位与 H 指数。"""

    BASE = "https://api.openalex.org"

    def work_by_doi(self, doi: str) -> dict | None:
        return _safe_get(f"{self.BASE}/works/https://doi.org/{quote(doi.strip(), safe='')}")

    def search_works(self, title: str, per_page: int = 3) -> list[dict]:
        data = _safe_get(f"{self.BASE}/works", params={
            "search": title, "per-page": per_page,
            "select": "id,doi,title,publication_year,publication_date,cited_by_count,"
                      "primary_location,authorships,open_access,concepts",
        })
        return (data or {}).get("results") or []

    def search_publications(self, query: str, per_page: int = 5,
                            open_access_only: bool = True) -> list[dict]:
        """OpenAlex 检索源：返回规范化记录，并优先返回 OA PDF 位置。"""
        params = {
            "search": query,
            "per-page": per_page,
            "select": ",".join([
                "id", "doi", "title", "publication_year", "publication_date",
                "cited_by_count", "primary_location", "best_oa_location",
                "authorships", "open_access", "ids", "abstract_inverted_index",
            ]),
        }
        if open_access_only:
            params["filter"] = "open_access.is_oa:true"
        data = _safe_get(f"{self.BASE}/works", params=params)
        return [self._normalize_publication(r)
                for r in (data or {}).get("results") or []]

    @staticmethod
    def _abstract_text(index: dict | None) -> str:
        items: list[tuple[int, str]] = []
        for word, positions in (index or {}).items():
            for pos in positions or []:
                items.append((int(pos), word))
        return " ".join(w for _, w in sorted(items))

    @classmethod
    def _normalize_publication(cls, work: dict) -> dict:
        best = work.get("best_oa_location") or {}
        primary = work.get("primary_location") or {}
        loc = best or primary
        source = loc.get("source") or {}
        pdf_url = best.get("pdf_url") or primary.get("pdf_url")
        ids = work.get("ids") or {}
        authors = []
        for a in (work.get("authorships") or []):
            auth = a.get("author") or {}
            insts = [(i or {}).get("display_name")
                     for i in (a.get("institutions") or [])]
            authors.append({
                "name": auth.get("display_name"),
                "orcid": auth.get("orcid"),
                "affiliations": [x for x in insts if x],
            })
        wid = work.get("id") or ""
        return {
            "paper_key": f"openalex:{wid.rstrip('/').split('/')[-1]}" if wid else None,
            "source": "openalex",
            "title": (work.get("title") or "").strip(),
            "abstract": cls._abstract_text(work.get("abstract_inverted_index")),
            "doi": work.get("doi"),
            "venue": source.get("display_name"),
            "source_type": source.get("type"),
            "pub_year": work.get("publication_year"),
            "pub_date": work.get("publication_date"),
            "publication_status": "Published" if work.get("publication_date") else None,
            "citation_count": work.get("cited_by_count"),
            "authors": authors,
            "is_open_access": bool((work.get("open_access") or {}).get("is_oa")),
            "pdf_url": pdf_url,
            "pmcid": ids.get("pmcid"),
        }

    def author_h_indices(self, author_ids: list[str], limit: int = 5) -> dict[str, float]:
        """按 OpenAlex author id 批量取 H 指数（最多 limit 位作者）。"""
        if not author_ids:
            return {}
        ids = "|".join(author_ids[:limit])
        data = _safe_get(f"{self.BASE}/authors", params={
            "filter": f"openalex:{ids}",
            "select": "id,summary_stats",
            "per-page": limit,
        })
        out: dict[str, float] = {}
        for r in (data or {}).get("results") or []:
            h = ((r.get("summary_stats") or {}).get("h_index"))
            if h is not None:
                out[r["id"]] = float(h)
        return out

    def enrich(self, rec: dict[str, Any]) -> dict[str, Any]:
        """用 OpenAlex 补全：DOI、来源期刊、被引、作者单位、H 指数等。"""
        work = None
        if rec.get("doi"):
            work = self.work_by_doi(rec["doi"])
        if not work and rec.get("title"):
            for hit in self.search_works(rec["title"]):
                if _title_similar(hit.get("title"), rec.get("title")):
                    work = hit
                    break
        if not work:
            return rec

        loc = work.get("primary_location") or {}
        src = loc.get("source") or {}
        oa = work.get("open_access") or {}
        authors = []
        author_ids: list[str] = []
        for a in (work.get("authorships") or []):
            inst = [(i or {}).get("display_name") for i in (a.get("institutions") or [])]
            inst = [x for x in inst if x]
            raw = (a.get("raw_affiliation_strings") or [])
            auth = (a.get("author") or {})
            if auth.get("id"):
                author_ids.append(auth["id"])
            authors.append({
                "name": auth.get("display_name"),
                "orcid": auth.get("orcid"),
                "affiliations": inst or raw,
                "openalex_id": auth.get("id"),
            })
        h_indices = self.author_h_indices(author_ids)
        for a in authors:
            oid = a.pop("openalex_id", None)
            if oid and oid in h_indices:
                a["h_index"] = h_indices[oid]
        h_vals = [a["h_index"] for a in authors if a.get("h_index") is not None]

        extra = {
            "title": work.get("title"),
            "doi": work.get("doi"),
            "pub_year": work.get("publication_year"),
            "pub_date": work.get("publication_date"),
            "citation_count": work.get("cited_by_count"),
            "venue": src.get("display_name") or rec.get("venue"),
            "venue_issn": src.get("issn_l") or rec.get("venue_issn"),
            "source_type": src.get("type") or rec.get("source_type"),
            "publication_status": "Published" if work.get("publication_date") else rec.get(
                "publication_status"),
            "authors": authors,
            "avg_h_index": (sum(h_vals) / len(h_vals)) if h_vals else None,
            "pdf_url": (oa.get("oa_url") or (loc.get("pdf_url"))) or rec.get("pdf_url"),
        }
        return merge_metadata(rec, extra)


def _title_similar(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    na = re.sub(r"[^a-z0-9]+", " ", a.lower()).strip()
    nb = re.sub(r"[^a-z0-9]+", " ", b.lower()).strip()
    if len(na) < 10 or len(nb) < 10:
        return False
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    return short in long or _overlap_ratio(na, nb) >= 0.7


def _overlap_ratio(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


class CrossrefClient:
    """Crossref：DOI 反查与元数据补全（作者、单位、发表情况）。"""

    BASE = "https://api.crossref.org/works"

    def lookup_by_title(self, title: str) -> dict | None:
        data = _safe_get(self.BASE, params={
            "query.title": title, "rows": 3,
            "select": "DOI,title,author,container-title,type,published-print,published-online",
        })
        for item in (data or {}).get("message", {}).get("items") or []:
            t = (item.get("title") or [""])[0]
            if t and _title_similar(t, title):
                return self._normalize(item)
        return None

    def lookup_by_doi(self, doi: str) -> dict | None:
        data = _safe_get(f"{self.BASE}/{quote(doi.strip(), safe='')}")
        if not data:
            return None
        return self._normalize(data.get("message") or {})

    @staticmethod
    def _normalize(msg: dict) -> dict:
        authors = []
        for a in msg.get("author") or []:
            affs = [x.get("name") for x in (a.get("affiliation") or [])]
            authors.append({
                "given": a.get("given"),
                "family": a.get("family"),
                "name": f"{a.get('given', '')} {a.get('family', '')}".strip(),
                "orcid": (a.get("ORCID") or "").replace("http://orcid.org/", ""),
                "affiliations": [x for x in affs if x],
            })
        date = msg.get("published-print") or msg.get("published-online") or {}
        parts = date.get("date-parts") or [[None]]
        year = parts[0][0]
        return {
            "title": (msg.get("title") or [None])[0],
            "doi": msg.get("DOI"),
            "venue": (msg.get("container-title") or [None])[0],
            "source_type": "journal" if msg.get("type") == "journal-article" else msg.get("type"),
            "publication_status": "Published",
            "pub_year": year,
            "pub_date": f"{year}-01-01" if year and parts[0] and len(parts[0]) > 1
                        else (f"{year}" if year else None),
            "authors": authors,
        }


SOURCE_SETS = {
    "pubmed": ["pubmed"],
    "arxiv": ["arxiv"],
    "both": ["pubmed", "arxiv"],
    "europepmc": ["europepmc"],
    "semantic_scholar": ["semantic_scholar"],
    "openalex": ["openalex"],
    "ncpssd": ["ncpssd"],
    "fulltext": ["europepmc", "arxiv", "semantic_scholar", "openalex"],
    "all": ["europepmc", "pubmed", "arxiv", "semantic_scholar", "openalex"],
}


class ApiHub:
    """统一入口：多源检索（全文源优先）→ 逐条补全。

    source 支持 pubmed/arxiv/both/europepmc/semantic_scholar/openalex/
    fulltext/all；fulltext 为默认，按可提供全文/PDF 的程度排序。
    """

    def __init__(self, arxiv: ArxivSearcher | None = None,
                 openalex: OpenAlexClient | None = None,
                 crossref: CrossrefClient | None = None,
                 pubmed: PubMedClient | None = None,
                 europepmc: EuropePmcClient | None = None,
                 semantic_scholar: SemanticScholarSearcher | None = None,
                 unpaywall: UnpaywallClient | None = None,
                 ncpssd: NcpssdClient | None = None,
                 source: str = "fulltext") -> None:
        self.arxiv = arxiv or ArxivSearcher()
        self.openalex = openalex or OpenAlexClient()
        self.crossref = crossref or CrossrefClient()
        self.pubmed = pubmed or PubMedClient()
        self.europepmc = europepmc or EuropePmcClient()
        self.semantic_scholar = semantic_scholar or SemanticScholarSearcher()
        self.unpaywall = unpaywall or UnpaywallClient()
        self.ncpssd = ncpssd or NcpssdClient()
        self.source = source

    def search(self, query: str, max_results: int = 5,
               source: str | None = None,
               report: dict[str, Any] | None = None) -> list[dict]:
        """按 source 检索并补全元数据，返回规范记录列表。跨源按 key/DOI 去重。

        每个来源单独 try/except（P1-8）：单个源限流/报错不应中断整轮检索，
        失败原因写入 ``report["source_errors"]``，供调用方区分"无命中"与"失败"。
        """
        source = (source or self.source).lower()
        source_set = SOURCE_SETS.get(source)
        if source_set is None:
            source_set = SOURCE_SETS["fulltext"]
        raw: list[dict] = []
        errors: dict[str, str] = {}
        per_source: dict[str, int] = {}
        for name in source_set:
            try:
                if name == "pubmed":
                    hits = self.pubmed.search(query, max_results=max_results)
                elif name == "arxiv":
                    hits = self.arxiv.search(query, max_results=max_results)
                elif name == "europepmc":
                    hits = self.europepmc.search(query, max_results=max_results)
                elif name == "semantic_scholar":
                    hits = self.semantic_scholar.search(query,
                                                        max_results=max_results)
                elif name == "openalex":
                    hits = self.openalex.search_publications(
                        query, per_page=max_results, open_access_only=True)
                elif name == "ncpssd":
                    hits = self.ncpssd.search(query, max_results=max_results)
                else:
                    hits = []
            except Exception as exc:  # noqa: BLE001
                logger.warning("来源 %s 检索失败: %s", name, exc)
                errors[name] = f"{type(exc).__name__}: {exc}"
                hits = []
            per_source[name] = len(hits or [])
            raw += hits or []
        if report is not None:
            report["source_errors"] = errors
            report["source_hits"] = per_source
        uniq: list[dict] = []
        seen_keys: set[str] = set()
        seen_dois: set[str] = set()
        for hit in raw:
            key = hit.get("paper_key")
            doi = (hit.get("doi") or "").strip().lower()
            if key in seen_keys or (doi and doi in seen_dois):
                continue
            seen_keys.add(key)
            if doi:
                seen_dois.add(doi)
            uniq.append(hit)
            if len(uniq) >= max_results:
                break
        enriched: list[dict] = []
        for hit in uniq:
            if source_set == ["ncpssd"]:
                enriched.append(hit)
                continue
            try:
                enriched.append(self.enrich(hit))
            except Exception as exc:  # noqa: BLE001
                logger.warning("补全失败 %s: %s", hit.get("paper_key"), exc)
                enriched.append(hit)
        return enriched

    def enrich(self, rec: dict[str, Any]) -> dict[str, Any]:
        """尽力补全：OpenAlex/Crossref 元数据，Unpaywall 补 OA PDF。"""
        rec = self.openalex.enrich(rec)
        if not (rec.get("doi") and rec.get("venue") and rec.get("authors")):
            extra = None
            if rec.get("doi"):
                extra = self.crossref.lookup_by_doi(rec["doi"])
            elif rec.get("title"):
                extra = self.crossref.lookup_by_title(rec["title"])
            if extra:
                rec = merge_metadata(rec, extra)
        if rec.get("doi") and not rec.get("pdf_url"):
            oa = self.unpaywall.lookup_by_doi(rec["doi"])
            if oa and oa.get("pdf_url"):
                rec["pdf_url"] = oa["pdf_url"]
                rec["is_open_access"] = bool(oa.get("is_oa"))
        return rec

    def download_pdf(self, rec: dict[str, Any]) -> bytes | None:
        """按候选地址依次尝试下载 PDF。"""
        candidates = []
        if (rec.get("source") or "").lower() == "ncpssd":
            logger.warning(
                "NCPSSD current endpoint does not expose stable PDF full text; "
                "using abstract fallback for %s", rec.get("paper_key"))
            return None
        if rec.get("pdf_url"):
            candidates.append(rec["pdf_url"])
        if rec.get("pmcid"):
            candidates.append(
                f"https://europepmc.org/articles/{rec['pmcid']}?pdf=render")
        key = rec.get("paper_key") or ""
        if key.startswith("arxiv:"):
            aid = key.split(":", 1)[1]
            candidates.append(f"https://export.arxiv.org/pdf/{aid}")
            candidates.append(f"https://arxiv.org/pdf/{aid}")
        for url in dict.fromkeys(candidates):
            data = download_pdf(url)
            if data:
                return data
        return None

    def fulltext_text(self, pmcid: str) -> str | None:
        """Europe PMC OA 全文文本（任意来源有 PMCID 时的 XML 回退）。"""
        return self.pubmed.fetch_fulltext_text(pmcid)

    def fetch_references(self, rec: dict[str, Any]) -> list[dict[str, Any]]:
        """返回一条记录的参考文献（引用溯源 skill backward 方向）。"""
        if (rec.get("source") or "").lower() in ("ncpssd",):
            return []
        return self.europepmc.references(rec)

    def fetch_citations(self, rec: dict[str, Any]) -> list[dict[str, Any]]:
        """返回一条记录的引文（引用溯源 skill forward 方向）。"""
        if (rec.get("source") or "").lower() in ("ncpssd",):
            return []
        return self.europepmc.citations(rec)
