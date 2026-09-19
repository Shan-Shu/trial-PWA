# -*- coding: utf-8 -*-
"""并行知识提取批量脚本（v0.0.6 提速用）。

背景：deepseek-v4-pro 单次抽取约 60~90s（推理型），串行 50 篇需数小时。
提速三板斧：
  1. 并发：多 worker 并行跑论文（本脚本默认 --workers 4）；
  2. 预建连接：每个 worker 串行预开一个 sqlite 连接并只建一次表，
     节点侧 run_init=False 跳过重复 DDL，避免并发建表互相等锁；
  3. 换模型（可选）：--model flash 用 deepseek-v4-flash（约快 30 倍，
     质量略降，慎用于需要与历史版本严格对照的实验）。

用法示例：
  uv run python examples/run_knowledge_batch.py \
      --src data/ontology_v05.db --db data/ontology_v06.db \
      --limit 50 --workers 5 --model pro
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
from research_agent.db import connect, get_paper, upsert_paper
from research_agent.knowledge.node import make_knowledge_node
from research_agent.ontology.store import init_ontology, graph_summary


def _pick_keys(src: Path, limit: int, longest: bool = False) -> list[str]:
    c = connect(src)
    try:
        has_runs = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ontology_runs'"
        ).fetchone()
        if has_runs:
            if longest:
                keys = [r["paper_key"] for r in c.execute(
                    "SELECT DISTINCT r.paper_key AS paper_key FROM ontology_runs r "
                    "JOIN papers p ON p.paper_key=r.paper_key "
                    "WHERE p.clean_text IS NOT NULL ORDER BY length(p.clean_text) DESC")]
            else:
                keys = [r["paper_key"] for r in c.execute(
                    "SELECT DISTINCT paper_key FROM ontology_runs ORDER BY run_id")]
        else:
            keys = [r["paper_key"] for r in c.execute(
                "SELECT paper_key FROM papers WHERE source='pubmed' AND "
                "clean_text IS NOT NULL AND length(clean_text)>100 "
                "ORDER BY length(clean_text) DESC" if longest else
                "SELECT paper_key FROM papers WHERE source='pubmed' AND "
                "clean_text IS NOT NULL AND length(clean_text)>100")]
    finally:
        c.close()
    if limit and limit > 0:
        keys = keys[:limit]
    return keys


def _copy_papers_and_quality(src: Path, dst: Path, keys: list[str]) -> int:
    """复制论文（去 PDF BLOB）+ 质量结果到新库，返回成功数。"""
    sc = connect(src)
    dc = connect(dst)
    init_ontology(dc)
    n = 0
    for k in keys:
        rec = get_paper(sc, k)
        if not rec:
            continue
        for f in ("pdf_blob", "pdf_sha256", "pdf_size"):
            rec[f] = None
        upsert_paper(dc, rec)
        q = sc.execute(
            "SELECT * FROM quality_results WHERE paper_key=?", (k,)).fetchone()
        if q:
            cols = [r[1] for r in dc.execute(
                "PRAGMA table_info(quality_results)") if r[1] != "assessed_at"]
            vals = {col: q[col] for col in cols}
            dc.execute(
                f"INSERT OR REPLACE INTO quality_results({','.join(vals)}) "
                f"VALUES({','.join('?' for _ in vals)})", list(vals.values()))
        n += 1
    dc.commit()
    dc.close()
    sc.close()
    return n


def _chunks(seq: list, n: int) -> list[list]:
    """把论文列表切成 n 份（尽量均匀，保证串行顺序稳定）。"""
    out: list[list] = [[] for _ in range(n)]
    for i, k in enumerate(seq):
        out[i % n].append(k)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="并行知识提取（v0.0.6 提速）")
    ap.add_argument("--src", required=True, help="源库（论文+质量结果），如 data/ontology_v05.db")
    ap.add_argument("--db", required=True, help="目标新库，如 data/ontology_v06.db")
    ap.add_argument("--limit", type=int, default=0, help="最多处理篇数，0=全部")
    ap.add_argument("--longest", action="store_true",
                    help="优先选 clean_text 最长的论文（便于速度验证/质量抽查）")
    ap.add_argument("--workers", type=int, default=4, help="并发数（默认 4）")
    ap.add_argument("--model", choices=["auto", "pro", "flash"], default="auto",
                    help="知识模型：auto=跟随 .env；pro=deepseek-v4-pro(慢/精)；"
                         "flash=deepseek-v4-flash(快，约30x)")
    ap.add_argument("--refine-attempts", type=int, default=None,
                    help="覆盖精修最大轮数（默认取 config=2；提速可设 1）")
    ap.add_argument("--fresh", action="store_true", help="目标库已存在时删除重建")
    args = ap.parse_args(argv)

    src = Path(args.src)
    dst = Path(args.db)
    if dst.exists() and not args.fresh:
        print(f"[abort] 目标库已存在：{dst}（用 --fresh 删除重建）", flush=True)
        return 2
    if dst.exists():
        dst.unlink()

    settings = Settings(db_path=dst)
    if args.refine_attempts is not None:
        settings.refine_max_attempts = max(1, args.refine_attempts)
    workers = max(1, args.workers)

    keys = _pick_keys(src, args.limit, args.longest)
    print(f"待处理 {len(keys)} 篇 <- {src}  写入 {dst}  workers={workers}", flush=True)
    if not keys:
        return 0
    copied = _copy_papers_and_quality(src, dst, keys)
    print(f"已复制论文+质量结果 {copied} 篇", flush=True)

    # 模型工厂（线程本地懒构建）
    local = threading.local()

    def _model():
        mdl = getattr(local, "model", None)
        if mdl is None:
            if args.model == "auto":
                from research_agent.models import build_role_model
                mdl = build_role_model("knowledge")
            else:
                from research_agent.models import build_chat_model
                name = "deepseek-v4-pro" if args.model == "pro" else "deepseek-v4-flash"
                mdl = build_chat_model(provider="deepseek", model_name=name,
                                       temperature=0.2)
            local.model = mdl
        return mdl

    # 预开 N 个连接：串行执行 DDL（避免并发建表等锁），每个 worker 持有一条
    worker_conns: list[sqlite3.Connection] = []
    for _ in range(workers):
        c = connect(dst)
        init_ontology(c)
        c.commit()
        worker_conns.append(c)

    print_lock = threading.Lock()
    done_counter = {"n": 0}
    total = len(keys)
    t_start = time.time()
    results: dict[str, dict] = {}

    def run_slice(conn: sqlite3.Connection, slice_keys: list[str]) -> None:
        node = make_knowledge_node(_model(), conn=conn, settings=settings,
                                   run_init=False)
        for key in slice_keys:
            t0 = time.time()
            try:
                out = node({"current_key": key})
                rep = (out.get("extraction_report") or {}).get("extracted") or {}
                info = {"sec": time.time() - t0, "extracted": rep, "out": out}
            except Exception as exc:  # noqa: BLE001
                info = {"sec": time.time() - t0, "error": str(exc)}
            with print_lock:
                results[key] = info
                done_counter["n"] += 1
                done = done_counter["n"]
                e = info.get("extracted") or {}
                elapsed = time.time() - t_start
                avg = elapsed / done
                eta = avg * (total - done)
                err = f" ERROR={info.get('error')}" if info.get("error") else ""
                print(
                    f"[{done}/{total}] {key} | entities={e.get('entities', 0)} "
                    f"relations={e.get('relations', 0)} events={e.get('events', 0)} "
                    f"refine={e.get('refine_runs', 0)}/a{e.get('refine_attempts', 0)} | "
                    f"{info.get('sec', 0):.0f}s | 累计{elapsed:.0f}s 平均{avg:.0f}s "
                    f"预计剩余{eta:.0f}s{err}", flush=True)

    slices = _chunks(keys, workers)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(run_slice, conn, ks)
                for conn, ks in zip(worker_conns, slices) if ks]
        for fut in as_completed(futs):
            exc = fut.exception()
            if exc:
                print(f"[worker-error] {exc}", flush=True)

    for c in worker_conns:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass

    wall = time.time() - t_start
    ok = sum(1 for r in results.values() if not r.get("error"))
    secs = [r.get("sec", 0) for r in results.values() if not r.get("error")]
    avg = (sum(secs) / len(secs)) if secs else 0
    print(f"\n=== 完成 {ok}/{total}，墙钟 {wall:.0f}s，平均每篇 {avg:.0f}s "
          f"（workers={workers}，model={args.model}）===", flush=True)
    c = connect(dst)
    try:
        gs = graph_summary(c)
        print("目标本体:", {k: gs[k] for k in
                            ("nodes", "edges", "isolated_ratio",
                             "components", "largest_comp_ratio")
                            if k in gs}, flush=True)
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
