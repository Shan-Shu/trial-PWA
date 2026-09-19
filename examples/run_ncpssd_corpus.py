"""NCPSSD large corpus pipeline: fetch -> ingest -> quality -> knowledge.

The NCPSSD public search endpoint returns structured Chinese humanities records
with abstracts and current metadata. Full-text PDF links on older records point
to a retired domain, so the corpus intentionally stores abstract + keywords as
the extractable text.

Usage:
  uv run python examples/run_ncpssd_corpus.py --fetch-only
  uv run python examples/run_ncpssd_corpus.py --extract-only
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from research_agent.config import Settings
from research_agent.db import connect, log_event, meta_set, upsert_paper
from research_agent.knowledge.node import make_knowledge_node
from research_agent.models import build_role_model
from research_agent.ontology.store import init_ontology
from research_agent.quality.node import make_quality_node
from research_agent.retrieval.ncpssd import NcpssdClient, build_combined_query

logger = logging.getLogger(__name__)


SEARCH_UNITS = [
    # (label, Solr query, max candidates from this unit)
    ("战争_人民群众", build_combined_query("战争", "人民群众"), 190),
    ("伟力_人民群众", build_combined_query("伟力", "人民群众"), 8),
]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _metadata_text(rec: dict) -> str:
    parts = []
    if rec.get("abstract"):
        parts.append(str(rec["abstract"]).strip())
    if rec.get("keywords"):
        parts.append("关键词：" + str(rec["keywords"]).strip())
    return "\n\n".join(parts)


def _fast_settings(db: Path) -> Settings:
    s = Settings(db_path=db)
    s.max_extract_chunks = 1
    s.knowledge_refine_enabled = False
    return s


def fetch_candidates(min_papers: int) -> tuple[list[dict], dict]:
    client = NcpssdClient()
    by_key: dict[str, dict] = {}
    reports: dict[str, dict] = {}
    for label, query, limit in SEARCH_UNITS:
        try:
            rows = client.search_expression(
                query, max_results=limit, page_size=10)
            kept = 0
            for r in rows:
                key = r.get("paper_key")
                if not key or key in by_key:
                    continue
                by_key[key] = r
                kept += 1
            reports[label] = {"query": query, "rows": len(rows), "kept": kept}
            print(f"[fetch] {label}: rows={len(rows)} kept={kept} "
                  f"total_now={len(by_key)}", flush=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("NCPSSD query failed: %s", label)
            reports[label] = {"error": str(exc)}
        if len(by_key) >= min_papers:
            break
    valid = [
        r for r in by_key.values()
        if (r.get("title") or "").strip()
        and (
            len((r.get("abstract") or "").strip()) >= 20
            or len((r.get("keywords") or "").strip()) >= 20
        )
    ]
    return valid, reports


def ingest_records(db: Path, records: list[dict]) -> dict:
    conn = connect(db)
    try:
        counts = {"candidate": len(records), "ingested": 0,
                  "with_abstract": 0}
        for i, rec in enumerate(records, 1):
            abstract = str(rec.get("abstract") or "").strip()
            text = _metadata_text(rec)
            rec["pdf_blob"] = None
            rec["pdf_size"] = None
            rec["pdf_sha256"] = None
            rec["clean_text"] = text or None
            rec["clean_text_sha256"] = _sha256(text) if text else None
            rec["fulltext_source"] = "abstract"
            rec["status"] = "ingested"
            upsert_paper(conn, rec)
            if abstract:
                counts["with_abstract"] += 1
            counts["ingested"] += 1
            if i % 50 == 0 or i == len(records):
                print(f"[ingest] {i}/{len(records)}", flush=True)
        meta_set(conn, "ncpssd_corpus_built_at",
                 datetime.now().astimezone().isoformat(timespec="seconds"))
        return counts
    finally:
        conn.close()


def pending_keys(db: Path) -> list[str]:
    conn = connect(db)
    try:
        return [r["paper_key"] for r in conn.execute(
            """
            SELECT paper_key FROM papers
            WHERE source='ncpssd'
              AND length(COALESCE(clean_text, '')) > 0
              AND paper_key NOT IN (SELECT paper_key FROM ontology_runs)
            ORDER BY paper_key
            """
        )]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NCPSSD 大规模语料建库")
    ap.add_argument("--db", default="data/ncpssd_war_masses.db")
    ap.add_argument("--min-papers", type=int, default=180)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--extract-only", action="store_true")
    ap.add_argument("--time-budget", type=float, default=2400.0)
    args = ap.parse_args(argv)

    db = Path(args.db)
    settings = _fast_settings(db)
    if not args.extract_only:
        conn = connect(db)
        try:
            init_ontology(conn)
            conn.commit()
        finally:
            conn.close()
        records, reports = fetch_candidates(args.min_papers)
        print(f"[fetch] candidates={len(records)}", flush=True)
        if len(records) < args.min_papers:
            print(f"[abort] only {len(records)} valid candidates; "
                  "increase query coverage or relax filters", flush=True)
            return 2
        counts = ingest_records(db, records)
        print(f"[ingest] {counts}", flush=True)
        conn = connect(db)
        try:
            meta_set(conn, "ncpssd_search_reports", reports)
        finally:
            conn.close()
        if args.fetch_only:
            return 0

    keys = pending_keys(db)
    print(f"[extract] pending={len(keys)} workers={args.workers}", flush=True)
    if not keys:
        return 0

    conns = []
    for _ in range(args.workers):
        c = connect(db)
        init_ontology(c)
        c.commit()
        conns.append(c)
    slices = [keys[i::args.workers] for i in range(args.workers)]
    stats = {"done": 0, "ok": 0, "error": 0}
    lock = threading.Lock()
    start = time.time()

    def run_part(conn: sqlite3.Connection, part: list[str]) -> None:
        qnode = make_quality_node(
            api=None, model=None, conn=conn, settings=settings, offline=True)
        knew = make_knowledge_node(
            model=build_role_model("knowledge"), conn=conn,
            settings=settings, run_init=False)
        for key in part:
            try:
                qnode({"current_key": key, "meta_attempts": 0})
                knew({"current_key": key})
                with lock:
                    stats["done"] += 1
                    stats["ok"] += 1
                    done = stats["done"]
                    if done % 20 == 0 or done == len(keys):
                        elapsed = time.time() - start
                        rate = done / max(1.0, elapsed)
                        eta = (len(keys) - done) / rate
                        print(f"[extract] {done}/{len(keys)} "
                              f"rate={rate:.2f}/s eta={eta/60:.1f}min",
                              flush=True)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    stats["done"] += 1
                    stats["error"] += 1
                    print(f"[extract] error {key}: {exc}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(run_part, conn, part)
            for conn, part in zip(conns, slices) if part
        ]
        for fut in as_completed(futures):
            try:
                fut.result(timeout=args.time_budget)
            except Exception as exc:  # noqa: BLE001
                print(f"[extract] worker error: {exc}", flush=True)
    for c in conns:
        c.close()

    conn = connect(db)
    try:
        nodes = conn.execute("SELECT COUNT(*) FROM ontology_nodes").fetchone()[0]
        edges = conn.execute("SELECT COUNT(*) FROM ontology_edges").fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM ontology_runs").fetchone()[0]
        paper_count = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE source='ncpssd'").fetchone()[0]
    finally:
        conn.close()
    print(f"\n[complete] papers={paper_count} runs={runs} "
          f"nodes={nodes} edges={edges} stats={stats}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
