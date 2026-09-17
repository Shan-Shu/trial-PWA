"""把 paper_writing_assistant 的文献库迁移进 research-agent。

用法::

    uv run python scripts/migrate_from_pwa.py --src "D:/pwa/data/paper_assistant.db" \
        --db data/research_agent.db --dry-run
    uv run python scripts/migrate_from_pwa.py --src ... --db ... --apply

设计要点（合并方案 v2 §5）：

- **幂等**：以生成的 ``paper_key`` 为准；已存在的记录默认跳过（``--overwrite`` 可覆盖）；
- **dry-run 优先**：不加 ``--apply`` 时只报告不改库；
- **两库主键不同**（PWA 用 INTEGER id，RA 用 TEXT paper_key），因此生成
  ``pwa:<source>:<doi|title_norm>`` 形式的 key，并把原 id 记进 metadata；
- **不搬无意义数据**：PWA 无 PDF/全文列，故 ``pdf_blob`` / ``clean_text`` 不写；
  RA 的 ``upsert_paper`` 已改为 COALESCE 语义，缺字段不会清空既有值；
- **侧车表一并迁移**：标签 / 文件夹 / 收藏（PWA 的四张表 -> RA 的四张表）。

报告会写入 stdout，并可用 ``--report path.json`` 落盘。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from research_agent.db import (  # noqa: E402
    connect,
    get_paper,
    meta_set,
    upsert_paper,
    utcnow,
)
from research_agent.library.store import LibraryStore  # noqa: E402


def normalize_title(title: str) -> str:
    """标题归一（与 PWA 的 title_norm 同构：小写、压空白、去标点）。"""
    import re
    text = str(title or "").lower()
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

#: PWA 状态 -> RA 状态（显式映射，不猜）
STATUS_MAP = {
    "collected": "ingested",
    "quality_passed": "ingested",
    "quality_reviewed": "ingested",
    "quality_reject": "human_review",
    "quality_rejected": "human_review",
    "quality_review": "human_review",
    "duplicate": "human_review",
    "human_review": "human_review",
}

#: PWA 质量状态（0-100）阈值 —— PWA 自己用的是硬编码 80/60/40
def quality_to_decision(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 80:
        return "direct"
    if score >= 60:
        return "direct"
    if score >= 40:
        return "flagged"
    return "human"


def make_paper_key(source: str, doi: str, title: str, legacy_id: object) -> str:
    src = (source or "pwa").strip().lower() or "pwa"
    if doi:
        return f"pwa:{src}:doi:{doi.strip().lower()}"
    norm = normalize_title(title)
    if norm:
        return f"pwa:{src}:title:{norm[:80]}"
    return f"pwa:{src}:id:{legacy_id}"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _load_pwa(src_path: Path) -> tuple[sqlite3.Connection, dict]:
    if not src_path.is_file():
        raise SystemExit(f"PWA 数据库不存在: {src_path}")
    conn = sqlite3.connect(str(src_path))
    conn.row_factory = sqlite3.Row
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "papers" not in tables:
        raise SystemExit(f"{src_path} 不像 PWA 数据库（缺少 papers 表）")
    return conn, {"tables": tables}


def migrate(src: Path, db_path: Path | None, *, apply: bool,
            overwrite: bool, limit: int | None) -> dict:
    src_conn, meta = _load_pwa(src)
    report: dict = {
        "src": str(src),
        "db": str(db_path) if db_path else "(默认)",
        "applied": bool(apply),
        "dry_run": not apply,
        "papers": {"total": 0, "inserted": 0, "skipped_existing": 0,
                   "overwritten": 0, "errors": []},
        "sidecar": {"tags": 0, "folders": 0, "memberships": 0, "favorites": 0},
        "status_mapped": {},
        "quality_results": 0,
    }

    sql = "SELECT * FROM papers ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    papers = _rows(src_conn, sql)
    report["papers"]["total"] = len(papers)

    dst = connect(db_path)
    store = LibraryStore(dst)
    status_counter: Counter = Counter()

    # legacy id -> new key，供侧车表映射
    id_to_key: dict[object, str] = {}

    try:
        for row in papers:
            try:
                key = make_paper_key(
                    row.get("source") or "", row.get("doi") or "",
                    row.get("title") or "", row.get("id"),
                )
                id_to_key[row.get("id")] = key
                exists = get_paper(dst, key) is not None
                if exists and not overwrite:
                    report["papers"]["skipped_existing"] += 1
                    continue
                if not apply:
                    report["papers"]["inserted" if not exists else "overwritten"] += 1
                    continue

                authors = row.get("authors_json") or "[]"
                try:
                    parsed = json.loads(authors)
                    authors_list = [
                        {"name": a} if isinstance(a, str) else a for a in parsed
                    ]
                except (json.JSONDecodeError, TypeError):
                    authors_list = [{"name": authors}] if authors else []

                rec = {
                    "paper_key": key,
                    "source": row.get("source") or "pwa",
                    "title": row.get("title") or "",
                    "abstract": row.get("abstract") or "",
                    "doi": row.get("doi") or "",
                    "venue": row.get("journal") or "",
                    "venue_issn": row.get("venue_issn") or "",
                    "source_type": row.get("source_type") or "journal",
                    "pub_year": row.get("year"),
                    "citation_count": row.get("citation_count") or 0,
                    # upsert_paper 读 rec["authors"] 并自行 JSON 序列化进 authors_meta
                    "authors": authors_list,
                    "volume": row.get("volume"),
                    "issue": row.get("issue"),
                    "pages": row.get("pages"),
                    "language": row.get("language"),
                    "fulltext_source": "abstract",
                    "status": STATUS_MAP.get(str(row.get("status") or ""), "ingested"),
                }
                upsert_paper(dst, rec)
                status_counter[rec["status"]] += 1

                score = row.get("quality_score")
                if score is not None:
                    quality = float(score) / 100.0
                    dst.execute(
                        """
                        INSERT INTO quality_results(
                            paper_key, venue_factor, h_factor, citation_factor,
                            authority, timeliness, quality, decision, needs_review,
                            meta_missing, rationale, assessed_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(paper_key) DO UPDATE SET
                            quality=excluded.quality, decision=excluded.decision,
                            rationale=excluded.rationale, assessed_at=excluded.assessed_at
                        """,
                        (key, None, None, None, None, None, quality,
                         quality_to_decision(score),
                         0 if (score or 0) >= 60 else 1, "[]",
                         f"迁移自 PWA（原分 {score}）", utcnow()),
                    )
                    dst.commit()
                    report["quality_results"] += 1

                if exists:
                    report["papers"]["overwritten"] += 1
                else:
                    report["papers"]["inserted"] += 1
            except Exception as exc:  # noqa: BLE001 —— 单篇失败不中断迁移
                report["papers"]["errors"].append(
                    {"legacy_id": row.get("id"), "error": f"{type(exc).__name__}: {exc}"})

        # ---------------- 侧车：标签 ----------------
        if _table_exists(src_conn, "paper_tags"):
            for row in _rows(src_conn, "SELECT paper_id, tag FROM paper_tags"):
                key = id_to_key.get(row["paper_id"])
                if not key:
                    continue
                report["sidecar"]["tags"] += 1
                if apply:
                    store.add_tag(key, row["tag"])

        # ---------------- 侧车：文件夹 ----------------
        if _table_exists(src_conn, "paper_folders"):
            folder_map: dict[object, int] = {}
            for row in _rows(src_conn, "SELECT id, name FROM paper_folders"):
                report["sidecar"]["folders"] += 1
                if apply:
                    try:
                        folder_map[row["id"]] = store.create_folder(row["name"])
                    except ValueError:
                        continue
                else:
                    folder_map[row["id"]] = -1
            if _table_exists(src_conn, "paper_folder_map"):
                for row in _rows(
                        src_conn, "SELECT folder_id, paper_id FROM paper_folder_map"):
                    key = id_to_key.get(row["paper_id"])
                    fid = folder_map.get(row["folder_id"])
                    if not key or fid in (None, -1):
                        continue
                    report["sidecar"]["memberships"] += 1
                    if apply:
                        store.add_to_folder(key, fid)

        # ---------------- 侧车：收藏 ----------------
        if _table_exists(src_conn, "paper_favorites"):
            for row in _rows(src_conn, "SELECT paper_id FROM paper_favorites"):
                key = id_to_key.get(row["paper_id"])
                if not key:
                    continue
                report["sidecar"]["favorites"] += 1
                if apply:
                    store.set_favorite(key, True)

        report["status_mapped"] = dict(status_counter)
        if apply:
            # 迁移溯源写 meta 表（papers 表没有 metadata 列）
            meta_set(dst, "pwa_migration", {
                "src": str(src),
                "papers_total": report["papers"]["total"],
                "papers_inserted": report["papers"]["inserted"],
                "papers_overwritten": report["papers"]["overwritten"],
                "sidecar": report["sidecar"],
                "ran_at": utcnow(),
            })
            dst.commit()
    finally:
        dst.close()
        src_conn.close()
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="迁移 PWA 文献库到 research-agent")
    ap.add_argument("--src", required=True, help="PWA 的 paper_assistant.db 路径")
    ap.add_argument("--db", help="目标 research_agent.db（默认取 config）")
    ap.add_argument("--apply", action="store_true",
                    help="真正写库；不加则为 dry-run")
    ap.add_argument("--overwrite", action="store_true",
                    help="目标库已存在同 key 时覆盖")
    ap.add_argument("--limit", type=int, help="只处理前 N 篇（调试用）")
    ap.add_argument("--report", help="把 JSON 报告写入该路径")
    args = ap.parse_args(argv)

    report = migrate(
        Path(args.src),
        Path(args.db) if args.db else None,
        apply=bool(args.apply),
        overwrite=bool(args.overwrite),
        limit=args.limit,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    if not args.apply:
        print("\n[dry-run] 未写库；确认无误后加 --apply 执行。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
