"""流式全流程执行：候选检索、质量/知识提取与研究节点并行推进。

采用预建连接 + 多 worker，避免 SQLite 并发建表锁；研究节点在首批知识完成
后立即启动，不等待全部候选完成。
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from research_agent.config import Settings
from research_agent.db import connect
from research_agent.knowledge.node import make_knowledge_node
from research_agent.models import build_role_model
from research_agent.ontology.store import init_ontology
from research_agent.quality.node import make_quality_node
from research_agent.retrieval.api_clients import ApiHub
from research_agent.retrieval.monitor import PaperMonitor
from research_agent.retrieval.node import ingest_search_results
from research_agent.study.graph import StudyServices, run_study

logger = logging.getLogger(__name__)


def _fast_settings(db_path: str | Path) -> Settings:
    """增量提取设置：每篇只取 1 块并关闭精修，避免单步阻塞太久。"""
    s = Settings(db_path=Path(db_path))
    s.max_extract_chunks = 1
    s.knowledge_refine_enabled = False
    return s


def _pending_keys(db_path: str | Path, limit: int) -> list[str]:
    conn = connect(Path(db_path))
    try:
        has_runs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ontology_runs'"
        ).fetchone()
        sql = (
            "SELECT p.paper_key FROM papers p "
            "WHERE length(COALESCE(p.clean_text, '')) > 500 "
            "AND p.paper_key NOT IN (SELECT paper_key FROM ontology_runs) "
            "ORDER BY length(p.clean_text) DESC LIMIT ?"
            if has_runs else
            "SELECT paper_key FROM papers "
            "WHERE length(COALESCE(clean_text, '')) > 500 "
            "ORDER BY length(clean_text) DESC LIMIT ?"
        )
        rows = conn.execute(sql, (limit,)).fetchall()
        return [r["paper_key"] for r in rows]
    finally:
        conn.close()


def _ontology_counts(db_path: str | Path) -> tuple[int, int]:
    conn = connect(Path(db_path))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        nodes = conn.execute("SELECT COUNT(*) FROM ontology_nodes").fetchone()[0] \
            if "ontology_nodes" in tables else 0
        edges = conn.execute("SELECT COUNT(*) FROM ontology_edges").fetchone()[0] \
            if "ontology_edges" in tables else 0
        return nodes, edges
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="流式全流程：检索/提取/研究并行推进")
    ap.add_argument("--db", required=True)
    ap.add_argument("--request",
                    default="尝试为我提出一个炔酰胺合成多元氮杂化合物的新方法")
    ap.add_argument("--source", default="europepmc")
    ap.add_argument("--queries", nargs="*", default=[
        "ynamide dearomative cyclization diazabicycle",
        "ynamide fused indole synthesis",
        "ynamide [4+2] annulation",
    ])
    ap.add_argument("--max-knowledge", type=int, default=6)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--min-success", type=int, default=2)
    ap.add_argument("--time-budget", type=float, default=360.0)
    args = ap.parse_args(argv)

    db = Path(args.db)
    settings = _fast_settings(db)
    stop = threading.Event()
    success_event = threading.Event()
    done_counter = {"n": 0}
    lock = threading.Lock()
    api = ApiHub(source=args.source)

    # 预建 worker 连接：只在启动时执行 DDL，后续并发提取不互相等表锁。
    worker_conns = []
    for _ in range(args.workers):
        conn = connect(db)
        init_ontology(conn)
        conn.commit()
        worker_conns.append(conn)

    # 后台候选生产者：检索线程不阻塞质量/知识/研究线程。
    def producer() -> None:
        for q in args.queries:
            if stop.is_set():
                return
            print(f"[candidate] 检索 {q}", flush=True)
            try:
                res = ingest_search_results(
                    q, max_results=5, api=api, settings=settings)
                print(f"[candidate] 新增 {res['count']} 篇: {q}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[candidate] 失败 {q}: {exc}", flush=True)

    producer_thread = threading.Thread(target=producer, daemon=True,
                                       name="candidate-producer")
    producer_thread.start()

    def run_slice(conn, keys) -> None:
        quality_model = build_role_model("quality")
        knowledge_model = build_role_model("knowledge")
        qnode = make_quality_node(
            api=None, model=quality_model, conn=conn,
            settings=settings, offline=True)
        knew = make_knowledge_node(
            model=knowledge_model, conn=conn, settings=settings,
            run_init=False)
        for key in keys:
            if stop.is_set():
                continue
            print(f"[extract] start {key}", flush=True)
            try:
                qnode({"current_key": key, "meta_attempts": 0})
                knew({"current_key": key})
                with lock:
                    done_counter["n"] += 1
                    n = done_counter["n"]
                print(f"[extract] done {key} (#{n})", flush=True)
                if n >= args.min_success:
                    success_event.set()
            except Exception as exc:  # noqa: BLE001
                logger.exception("处理失败 %s", key)
                print(f"[extract] error {key}: {exc}", flush=True)

    # 从现有候选和后续 monitor 新发现中，只提交前 max_knowledge 篇。
    selected = _pending_keys(db, args.max_knowledge)
    slices = [selected[i::args.workers] for i in range(args.workers)]
    futures = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(run_slice, conn, ks)
            for conn, ks in zip(worker_conns, slices) if ks
        ]

        # 即使后续 monitor 发现更多论文，也不再阻塞性地排长队。
        t0 = time.time()
        while not success_event.is_set() and time.time() - t0 < args.time_budget:
            time.sleep(2)

        nodes, edges = _ontology_counts(db)
        print(f"[study] 首批完成，当前本体 {nodes} nodes / {edges} edges；启动研究节点",
              flush=True)
        study_services = StudyServices(
            planner_model=build_role_model("planner"),
            content_model=build_role_model("content"),
            review_model=None,
            settings=settings,
        )
        out = run_study(args.request, study_services, max_results_override=10)
        review = out.get("review") or {}
        draft = out.get("draft") or {}
        print("\n=== 流式全流程结果 ===", flush=True)
        print("状态:", out.get("status"), "| 审核:", review.get("decision"), flush=True)
        print("标题:", draft.get("title"), flush=True)
        print((draft.get("markdown") or "")[:5000], flush=True)

        # 停止候选补库并等待已提交的提取任务收尾。
        stop.set()
        for fut in futures:
            try:
                fut.result(timeout=240)
            except Exception as exc:  # noqa: BLE001
                logger.warning("worker future error: %s", exc)

    for c in worker_conns:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    print("\n流式执行结束。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
