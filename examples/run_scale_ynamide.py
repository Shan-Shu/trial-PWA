"""大规模流式全流程：数百篇 Europe PMC OA 全文候选，边入库边提取。

流程：
1. 分页检索 ynamide 的 OA 全文候选；
2. 多线程下载/清洗/入库；
3. 论文一入库即进入质量+知识提取队列，与检索并发；
4. 首批若干篇完成后运行研究四节点，不等待数百篇全部完成。
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import queue
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from research_agent.config import Settings
from research_agent.db import connect, log_event, upsert_paper
from research_agent.knowledge.node import make_knowledge_node
from research_agent.models import build_role_model
from research_agent.ontology.store import init_ontology
from research_agent.quality.node import make_quality_node
from research_agent.retrieval.api_clients import ApiHub
from research_agent.retrieval.extended import EuropePmcClient
from research_agent.retrieval.pdf_cleaner import clean_pdf
from research_agent.study.graph import StudyServices, run_study

logger = logging.getLogger(__name__)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fast_settings(db_path: Path) -> Settings:
    s = Settings(db_path=db_path)
    s.max_extract_chunks = 1
    s.knowledge_refine_enabled = False
    return s


def ingest_record(api: ApiHub, rec: dict, settings: Settings) -> bool:
    """下载/清洗/入库一篇候选；返回是否获得有效文本。"""
    conn = connect(settings.db_path)
    try:
        key = rec.get("paper_key")
        pdf = api.download_pdf(rec)
        clean = None
        fulltext_source = None
        if pdf:
            clean = clean_pdf(pdf)
            fulltext_source = "pdf"
        rec["pdf_blob"] = pdf
        rec["pdf_size"] = len(pdf) if pdf else None
        rec["pdf_sha256"] = _sha256(pdf) if pdf else None
        if clean and clean["text"]:
            rec["clean_text"] = clean["text"]
            rec["clean_text_sha256"] = _sha256(clean["text"].encode("utf-8"))
        elif rec.get("pmcid"):
            xml = api.fulltext_text(rec["pmcid"])
            if xml:
                rec["clean_text"] = xml
                rec["clean_text_sha256"] = _sha256(xml.encode("utf-8"))
                fulltext_source = "xml"
        if not rec.get("clean_text"):
            if rec.get("abstract"):
                rec["clean_text"] = rec["abstract"]
                fulltext_source = "abstract"
                rec["clean_text_sha256"] = _sha256(
                    rec["clean_text"].encode("utf-8"))
        rec["fulltext_source"] = fulltext_source
        rec["status"] = "ingested"
        if key:
            upsert_paper(conn, rec)
            log_event(conn, "retrieval", "paper-ingested", key,
                      {"fulltext_source": fulltext_source,
                       "clean_chars": len(rec.get("clean_text") or "")})
        return bool(rec.get("clean_text") and len(rec.get("clean_text", "")) > 100)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="数百篇流式全流程")
    ap.add_argument("--db", required=True)
    ap.add_argument("--limit", type=int, default=320)
    ap.add_argument("--request",
                    default="尝试为我提出一个炔酰胺合成多元氮杂化合物的新方法")
    ap.add_argument("--ingest-workers", type=int, default=8)
    ap.add_argument("--process-workers", type=int, default=6)
    ap.add_argument("--min-study", type=int, default=3)
    ap.add_argument("--time-budget", type=float, default=1500.0)
    args = ap.parse_args(argv)

    db = Path(args.db)
    settings = _fast_settings(db)
    api = ApiHub(source="europepmc")
    eupmc = EuropePmcClient()

    candidates: queue.Queue[dict] = queue.Queue(maxsize=args.limit)
    to_process: queue.Queue[str] = queue.Queue()
    stop = threading.Event()
    study_event = threading.Event()

    counters = {
        "fetched": 0,
        "ingested": 0,
        "processed": 0,
        "fulltext_ingested": 0,
    }
    lock = threading.Lock()

    def fetch_candidates() -> None:
        print(f"[fetch] 拉取 Europe PMC OA 候选，目标 {args.limit} 篇", flush=True)
        try:
            recs = eupmc.search_all("ynamide", limit=args.limit)
            for r in recs:
                candidates.put(r)
            with lock:
                counters["fetched"] = len(recs)
            print(f"[fetch] 获取 {len(recs)} 条候选", flush=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("候选检索失败")
            print(f"[fetch] 失败: {exc}", flush=True)
        finally:
            for _ in range(args.ingest_workers):
                candidates.put(None)

    def ingest_worker() -> None:
        while not stop.is_set():
            rec = candidates.get()
            if rec is None:
                candidates.task_done()
                return
            try:
                ok = ingest_record(api, rec, settings)
                with lock:
                    counters["ingested"] += 1
                    if ok:
                        counters["fulltext_ingested"] += 1
                        to_process.put(rec.get("paper_key"))
                    n = counters["ingested"]
                    if n % 25 == 0 or n == args.limit:
                        print(f"[ingest] {n}/{args.limit} ok={ok}", flush=True)
            except Exception as exc:  # noqa: BLE001
                logger.exception("入库失败")
                print(f"[ingest] error: {exc}", flush=True)
            finally:
                candidates.task_done()

    # 预建处理连接，避免并发建表锁。
    process_conns = []
    for _ in range(args.process_workers):
        c = connect(db)
        init_ontology(c)
        c.commit()
        process_conns.append(c)

    def process_worker(conn: sqlite3.Connection) -> None:
        qnode = make_quality_node(api=None, model=build_role_model("quality"),
                                  conn=conn, settings=settings, offline=True)
        knew = make_knowledge_node(
            model=build_role_model("knowledge"), conn=conn,
            settings=settings, run_init=False)
        while not stop.is_set():
            try:
                key = to_process.get(timeout=2)
            except queue.Empty:
                continue
            if key is None:
                return
            print(f"[extract] start {key}", flush=True)
            try:
                qnode({"current_key": key, "meta_attempts": 0})
                knew({"current_key": key})
                with lock:
                    counters["processed"] += 1
                    n = counters["processed"]
                print(f"[extract] done {key} (#{n})", flush=True)
                if n >= args.min_study:
                    study_event.set()
            except Exception as exc:  # noqa: BLE001
                logger.exception("提取失败 %s", key)
                print(f"[extract] error {key}: {exc}", flush=True)

    fetch_thread = threading.Thread(target=fetch_candidates, daemon=True)
    fetch_thread.start()

    with ThreadPoolExecutor(max_workers=args.ingest_workers) as ingest_pool:
        ingest_futs = [
            ingest_pool.submit(ingest_worker)
            for _ in range(args.ingest_workers)
        ]
        process_pool = ThreadPoolExecutor(max_workers=args.process_workers)
        process_futs = [
            process_pool.submit(process_worker, c)
            for c in process_conns
        ]

        t0 = time.time()
        while not study_event.is_set() and time.time() - t0 < args.time_budget:
            time.sleep(2)

        nodes, edges = 0, 0
        conn = connect(db)
        try:
            nodes = conn.execute("SELECT COUNT(*) FROM ontology_nodes").fetchone()[0]
            edges = conn.execute("SELECT COUNT(*) FROM ontology_edges").fetchone()[0]
        finally:
            conn.close()
        print(f"[study] 本体 {nodes} nodes / {edges} edges，启动研究节点", flush=True)
        study_services = StudyServices(
            planner_model=build_role_model("planner"),
            content_model=build_role_model("content"),
            review_model=None,
            settings=settings,
        )
        out = run_study(args.request, study_services, max_results_override=20)
        review = out.get("review") or {}
        draft = out.get("draft") or {}
        print("\n=== 规模化流式研究结果 ===", flush=True)
        print("状态:", out.get("status"), "| 审核:", review.get("decision"), flush=True)
        print("标题:", draft.get("title"), flush=True)
        print((draft.get("markdown") or "")[:4000], flush=True)

        # 继续等待提取达到目标/超时，期间 dashboard 持续刷新。
        while time.time() - t0 < args.time_budget:
            with lock:
                p = counters["processed"]
                i = counters["ingested"]
                ft = counters["fulltext_ingested"]
                fetched = counters["fetched"]
            if fetched > 0 and ft >= fetched and p >= ft:
                break
            if ft >= args.limit and p >= ft:
                break
            time.sleep(5)

        stop.set()
        for _ in range(args.process_workers):
            to_process.put(None)
        for fut in process_futs:
            try:
                fut.result(timeout=30)
            except Exception:  # noqa: BLE001
                pass
        process_pool.shutdown(wait=True)
        for fut in ingest_futs:
            try:
                fut.result(timeout=30)
            except Exception:  # noqa: BLE001
                pass

    for c in process_conns:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass

    with lock:
        print(f"\n最终规模：fetched={counters['fetched']} "
              f"ingested={counters['ingested']} "
              f"fulltext={counters['fulltext_ingested']} "
              f"processed={counters['processed']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
