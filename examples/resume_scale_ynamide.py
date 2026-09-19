"""断点续跑：对已入库的数百篇全文直接并行质量+知识提取。

用法：
  uv run python examples/resume_scale_ynamide.py --db data/ynamide_scale_320.db \
      --workers 12
"""
from __future__ import annotations

import argparse
import sqlite3
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


def pending_keys(db_path: Path) -> list[str]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT paper_key FROM papers "
            "WHERE length(COALESCE(clean_text, '')) > 100 "
            "AND paper_key NOT IN (SELECT paper_key FROM ontology_runs) "
            "ORDER BY paper_key"
        ).fetchall()
        return [r["paper_key"] for r in rows]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--deterministic-quality", action="store_true",
                    help="质量评分走确定性公式，避免再等一轮 LLM")
    ap.add_argument("--time-budget", type=float, default=2400.0)
    args = ap.parse_args(argv)

    db = Path(args.db)
    settings = Settings(db_path=db)
    settings.max_extract_chunks = 1
    settings.knowledge_refine_enabled = False
    keys = pending_keys(db)
    print(f"待处理 {len(keys)} 篇, workers={args.workers}", flush=True)
    if not keys:
        return 0

    conns = []
    for _ in range(args.workers):
        c = connect(db)
        init_ontology(c)
        c.commit()
        conns.append(c)

    slices = [keys[i::args.workers] for i in range(args.workers)]
    counter = {"n": 0, "ok": 0, "err": 0}
    lock = threading.Lock()
    start = time.time()

    def run(conn: sqlite3.Connection, part: list[str]) -> None:
        quality_model = None if args.deterministic_quality \
            else build_role_model("quality")
        knowledge_model = build_role_model("knowledge")
        qnode = make_quality_node(api=None, model=quality_model, conn=conn,
                                  settings=settings, offline=True)
        knew = make_knowledge_node(model=knowledge_model, conn=conn,
                                   settings=settings, run_init=False)
        for key in part:
            try:
                qnode({"current_key": key, "meta_attempts": 0})
                knew({"current_key": key})
                with lock:
                    counter["n"] += 1
                    counter["ok"] += 1
                    n = counter["n"]
                    if n % 20 == 0 or n == len(keys):
                        elapsed = time.time() - start
                        rate = n / max(1.0, elapsed)
                        eta = (len(keys) - n) / rate
                        print(f"[resume] {n}/{len(keys)} "
                              f"rate={rate:.2f}/s eta={eta/60:.1f}min", flush=True)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    counter["n"] += 1
                    counter["err"] += 1
                    print(f"[resume] error {key}: {exc}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(run, conn, part)
            for conn, part in zip(conns, slices) if part
        ]
        for fut in as_completed(futures):
            try:
                fut.result(timeout=args.time_budget)
            except Exception as exc:  # noqa: BLE001
                print(f"[resume] worker error {exc}", flush=True)

    for c in conns:
        c.close()
    print(f"\n完成 ok={counter['ok']} err={counter['err']} "
          f"elapsed={(time.time()-start)/60:.1f}min", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
