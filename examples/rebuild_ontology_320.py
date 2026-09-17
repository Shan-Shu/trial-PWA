"""从同一 ynamide 语料重建新动态本体库（保留文献/质量，不继承旧本体）。

用法：
  uv run python examples/rebuild_ontology_320.py \
      --src data/ynamide_scale_320.db \
      --db data/ynamide_320_v011.db --workers 16 --fresh
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from research_agent.config import Settings
from research_agent.db import connect, get_paper, upsert_paper
from research_agent.domains import normalize_domain_profile
from research_agent.knowledge.node import make_knowledge_node
from research_agent.models import build_role_model
from research_agent.ontology.store import init_ontology


def domain_profile(kind: str) -> dict:
    """从 packs 取领域画像（DOMAIN_PROFILES 已在 v0.4.2 外置）。"""
    return normalize_domain_profile({"domain_kind": kind}, kind, kind)


def _copy_corpus(src: Path, dst: Path) -> int:
    sc = connect(src)
    dc = connect(dst)
    init_ontology(dc)
    keys = [r["paper_key"] for r in sc.execute(
        "SELECT paper_key FROM papers "
        "WHERE length(COALESCE(clean_text, '')) > 100 ORDER BY paper_key")]
    for key in keys:
        rec = get_paper(sc, key)
        if not rec:
            continue
        rec["pdf_blob"] = None
        rec["pdf_size"] = None
        rec["pdf_sha256"] = None
        upsert_paper(dc, rec)
        q = sc.execute(
            "SELECT * FROM quality_results WHERE paper_key=?", (key,)).fetchone()
        if q:
            cols = [r[1] for r in dc.execute(
                "PRAGMA table_info(quality_results)") if r[1] != "assessed_at"]
            vals = {col: q[col] for col in cols}
            dc.execute(
                f"INSERT OR REPLACE INTO quality_results({','.join(vals)}) "
                f"VALUES({','.join('?' for _ in vals)})", list(vals.values()))
    dc.commit()
    dc.close()
    sc.close()
    return len(keys)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同语料重建新本体")
    ap.add_argument("--src", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args(argv)

    src = Path(args.src)
    dst = Path(args.db)
    if dst.exists() and not args.fresh:
        print(f"[abort] {dst} 已存在，用 --fresh 重建", flush=True)
        return 2
    if dst.exists():
        dst.unlink()

    settings = Settings(db_path=dst)
    settings.max_extract_chunks = 1
    settings.knowledge_refine_enabled = False
    n = _copy_corpus(src, dst)
    print(f"语料复制完成: {n} 篇 -> {dst}", flush=True)

    # 领域画像来自 packs（v0.4.2 起 DOMAIN_PROFILES 已外置）
    profile = domain_profile("chemistry").copy()
    profile["schema_status"] = "candidate"
    conns = []
    for _ in range(args.workers):
        c = connect(dst)
        init_ontology(c)
        c.commit()
        conns.append(c)

    conn = connect(dst)
    keys = [r["paper_key"] for r in conn.execute(
        "SELECT paper_key FROM papers ORDER BY paper_key")]
    conn.close()
    slices = [keys[i::args.workers] for i in range(args.workers)]
    counter = {"n": 0, "ok": 0, "err": 0}
    lock = threading.Lock()
    start = time.time()

    def run_slice(c: sqlite3.Connection, part: list[str]) -> None:
        knew = make_knowledge_node(model=build_role_model("knowledge"),
                                   conn=c, settings=settings, run_init=False)
        for key in part:
            try:
                knew({"current_key": key, "domain_profile": profile})
                with lock:
                    counter["n"] += 1
                    counter["ok"] += 1
                    n = counter["n"]
                    if n % 20 == 0 or n == len(keys):
                        elapsed = time.time() - start
                        print(f"[rebuild] {n}/{len(keys)} "
                              f"elapsed={elapsed/60:.1f}min", flush=True)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    counter["n"] += 1
                    counter["err"] += 1
                    print(f"[rebuild] error {key}: {exc}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_slice, c, part)
                   for c, part in zip(conns, slices) if part]
        for fut in futures:
            fut.result()
    for c in conns:
        c.close()
    print(f"\n新本体重建完成 ok={counter['ok']} err={counter['err']} "
          f"elapsed={(time.time()-start)/60:.1f}min", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
