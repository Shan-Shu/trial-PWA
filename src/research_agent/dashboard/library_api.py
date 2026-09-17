"""文献库与系统状态的 REST 数据层（合并新增）。

对应合并方案 v2 §6.1 的 13 个端点：筛选分页、分面、标签/文件夹/收藏、批量作业、
导出、引用、统计，以及 ``/api/system/packs``（包加载状态 + 生效配置 + 未命中分区期刊）。

与来源项目 PWA 的差异：
- PWA 的 ``library_query`` 先取 5000 行再在 Python 里过滤切片，这里**下推 SQL**；
- PWA 的年份筛选恒传 1900/2100，导致 ``pub_year IS NULL`` 的记录永久不可见，
  这里提供显式的 ``year_mode=unknown`` 档位；
- 批量操作返回 ``job_id`` 走轮询，不再"同步跑完提示刷新页面"。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from research_agent import packs
from research_agent.config import settings as default_settings
from research_agent.db import connect, get_paper
from research_agent.library import citation as cite
from research_agent.library.jobs import manager_for, run_batch_extract
from research_agent.library.store import LibraryStore

__all__ = [
    "list_papers",
    "facets",
    "add_tag",
    "remove_tag",
    "create_folder",
    "delete_folder",
    "add_to_folder",
    "remove_from_folder",
    "set_favorites",
    "start_batch",
    "job_status",
    "cancel_job",
    "list_jobs",
    "citation_for",
    "export_records",
    "stats",
    "system_packs",
    "paper_sidecar",
]

_EXPORT_MIME = {
    "bibtex": "text/plain; charset=utf-8",
    "ris": "text/plain; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
}
_EXPORT_EXT = {"bibtex": "bib", "ris": "ris", "markdown": "md"}


def _open(db_path: str | Path | None = None) -> sqlite3.Connection:
    return connect(Path(db_path) if db_path else default_settings.db_path)


def _store(db_path: str | Path | None) -> tuple[sqlite3.Connection, LibraryStore]:
    conn = _open(db_path)
    return conn, LibraryStore(conn)


# ----------------------------------------------------------------- 列表与分面

def list_papers(
    db_path: str | Path | None = None,
    *,
    q: str | None = None,
    status: str | None = None,
    source: str | None = None,
    source_type: str | None = None,
    decision: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    year_mode: str | None = None,
    tag: str | None = None,
    folder_id: int | None = None,
    favorite: bool = False,
    low_signal: str | None = None,
    has_knowledge: bool | None = None,
    sort_by: str = "quality",
    sort_desc: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    conn, store = _store(db_path)
    try:
        return store.query_papers(
            q=q, status=status, source=source, source_type=source_type,
            decision=decision, year_from=year_from, year_to=year_to,
            year_mode=year_mode, tag=tag, folder_id=folder_id,
            favorite_only=favorite, low_signal=low_signal,
            has_knowledge=has_knowledge, sort_by=sort_by, sort_desc=sort_desc,
            limit=limit, offset=offset,
        )
    finally:
        conn.close()


def facets(db_path: str | Path | None = None) -> dict[str, Any]:
    conn, store = _store(db_path)
    try:
        return store.facets()
    finally:
        conn.close()


def stats(db_path: str | Path | None = None) -> dict[str, Any]:
    conn, store = _store(db_path)
    try:
        return store.stats()
    finally:
        conn.close()


# ----------------------------------------------------------------- 侧车操作

def add_tag(db_path: str | Path | None, paper_key: str, tag: str) -> bool:
    conn, store = _store(db_path)
    try:
        return store.add_tag(paper_key, tag)
    finally:
        conn.close()


def remove_tag(db_path: str | Path | None, paper_key: str, tag: str) -> int:
    conn, store = _store(db_path)
    try:
        return store.remove_tag(paper_key, tag)
    finally:
        conn.close()


def create_folder(db_path: str | Path | None, name: str) -> int:
    conn, store = _store(db_path)
    try:
        return store.create_folder(name)
    finally:
        conn.close()


def delete_folder(db_path: str | Path | None, folder_id: int) -> None:
    conn, store = _store(db_path)
    try:
        store.delete_folder(folder_id)
    finally:
        conn.close()


def add_to_folder(db_path: str | Path | None, folder_id: int,
                  paper_keys: list[str]) -> int:
    conn, store = _store(db_path)
    try:
        return store.batch_add_to_folder(paper_keys, folder_id)
    finally:
        conn.close()


def remove_from_folder(db_path: str | Path | None, folder_id: int,
                       paper_keys: list[str]) -> int:
    conn, store = _store(db_path)
    changed = 0
    try:
        for key in paper_keys:
            changed += store.remove_from_folder(key, folder_id)
        return changed
    finally:
        conn.close()


def set_favorites(db_path: str | Path | None, paper_keys: list[str],
                  value: bool) -> int:
    conn, store = _store(db_path)
    try:
        return store.batch_favorite(paper_keys, value)
    finally:
        conn.close()


def paper_sidecar(db_path: str | Path | None,
                  paper_key: str) -> dict[str, Any]:
    """单篇的标签 / 文件夹 / 收藏（详情面板用）。"""
    conn, store = _store(db_path)
    try:
        folders = {f["folder_id"]: f["name"] for f in store.list_folders()}
        folder_ids = store.folder_ids_for(paper_key)
        return {
            "paper_key": paper_key,
            "tags": store.get_tags(paper_key),
            "folder_ids": folder_ids,
            "folder_names": [folders[f] for f in folder_ids if f in folders],
            "favorite": store.is_favorite(paper_key),
            "folders": store.list_folders(),
            "all_tags": store.all_tags(),
        }
    finally:
        conn.close()


# ----------------------------------------------------------------- 批量作业

def start_batch(db_path: str | Path | None, action: str,
                paper_keys: list[str], payload: dict[str, Any] | None = None
                ) -> dict[str, Any]:
    """启动批量作业，返回 job_id。"""
    keys = [str(k) for k in paper_keys if k]
    payload = payload or {}
    manager = manager_for(str(db_path) if db_path else None)
    action = str(action or "").strip()

    if action == "extract_knowledge":
        job_id = manager.start(
            action, run_batch_extract, keys,
            total=len(keys), db_path=str(db_path) if db_path else None,
        )
        return {"ok": True, "job_id": job_id, "action": action, "total": len(keys)}

    if action in ("tag", "folder", "favorite", "delete"):
        def _sync_runner(progress_cb=None, cancel_event=None):
            conn, store = _store(db_path)
            done = 0
            try:
                for index, key in enumerate(keys, 1):
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    if action == "tag":
                        store.add_tag(key, str(payload.get("tag") or ""))
                    elif action == "folder":
                        store.add_to_folder(key, int(payload.get("folder_id") or 0))
                    elif action == "favorite":
                        store.set_favorite(key, bool(payload.get("value", True)))
                    elif action == "delete":
                        store.delete_paper_records(key)
                        conn.execute("DELETE FROM papers WHERE paper_key=?", (key,))
                        conn.commit()
                    done += 1
                    if progress_cb is not None:
                        progress_cb(index / max(1, len(keys)) * 100.0,
                                    f"{action} {index}/{len(keys)}")
                if progress_cb is not None:
                    progress_cb(100.0, "完成")
                return {"total": len(keys), "done": done}
            finally:
                conn.close()

        job_id = manager.start(action, _sync_runner, total=len(keys))
        return {"ok": True, "job_id": job_id, "action": action, "total": len(keys)}

    return {"ok": False, "error": f"未知批量动作: {action}"}


def job_status(db_path: str | Path | None, job_id: str) -> dict[str, Any] | None:
    manager = manager_for(str(db_path) if db_path else None)
    return manager.status(job_id)


def list_jobs(db_path: str | Path | None, limit: int = 20) -> list[dict[str, Any]]:
    manager = manager_for(str(db_path) if db_path else None)
    return manager.list_jobs(limit=limit)


def cancel_job(db_path: str | Path | None, job_id: str) -> bool:
    manager = manager_for(str(db_path) if db_path else None)
    return manager.cancel(job_id)


# ----------------------------------------------------------------- 引用与导出

def citation_for(db_path: str | Path | None, paper_key: str,
                 style: str | None = None) -> dict[str, Any]:
    conn = _open(db_path)
    try:
        rec = get_paper(conn, paper_key)
        if not rec:
            return {"ok": False, "error": f"文献不存在: {paper_key}"}
        styles = cite.available_styles()
        chosen = style or cite.default_style()
        return {
            "ok": True,
            "paper_key": paper_key,
            "style": chosen,
            "styles": styles,
            "text": cite.format_citation(rec, chosen),
            "rendered": {s: cite.format_citation(rec, s) for s in styles
                         if s != "markdown"},
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def export_records(db_path: str | Path | None, paper_keys: list[str],
                   style: str | None = None) -> dict[str, Any]:
    """返回 ``{filename, mime, content}``；未知样式返回 error 而不是静默回落。"""
    conn = _open(db_path)
    try:
        records = []
        missing: list[str] = []
        for key in paper_keys:
            rec = get_paper(conn, key)
            if rec:
                records.append(rec)
            else:
                missing.append(key)
        chosen = style or cite.default_style()
        try:
            content = cite.export(records, chosen)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        ext = _EXPORT_EXT.get(chosen, "txt")
        return {
            "ok": True,
            "count": len(records),
            "missing": missing,
            "style": chosen,
            "filename": f"library_export.{ext}",
            "mime": _EXPORT_MIME.get(chosen, "text/plain; charset=utf-8"),
            "content": content,
        }
    finally:
        conn.close()


# ----------------------------------------------------------------- 系统状态

def system_packs(db_path: str | Path | None = None) -> dict[str, Any]:
    """包加载状态 + 生效配置 + 未命中分区期刊 Top-N（合并方案 v2 的 R2/R3 兜底）。"""
    skills = packs.available_skills()
    domains = packs.available_domains()
    quartiles = packs.journal_quartiles()
    scores = packs.quartile_scores()
    styles = cite.citation_styles()

    loaded = {
        "skills": {name: bool(packs.skill_data(name)) for name in skills},
        "domains": {name: bool(packs.domain_data(name)) for name in domains},
        "citation_styles": bool(styles),
    }
    warnings = sorted(packs._warned.keys())  # noqa: SLF001 —— 只读诊断用

    override = None
    raw_override = os.getenv("RA_JOURNAL_QUARTILES")
    if raw_override:
        parts = [p for p in raw_override.split(os.pathsep) if p.strip()]
        override = {
            "value": raw_override,
            "files": [{"path": p, "exists": Path(p).is_file()} for p in parts],
        }

    unmatched: list[dict[str, Any]] = []
    unknown_year = 0
    if db_path:
        conn = _open(db_path)
        try:
            rows = conn.execute(
                "SELECT venue, COUNT(*) AS count FROM papers "
                "WHERE venue IS NOT NULL AND TRIM(venue) <> '' "
                "GROUP BY venue ORDER BY count DESC LIMIT 400"
            ).fetchall()
            for row in rows:
                if packs.journal_quartile(row["venue"]) is None:
                    unmatched.append({"venue": row["venue"],
                                      "count": int(row["count"])})
            unmatched = unmatched[:25]
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM papers WHERE pub_year IS NULL"
            ).fetchone()
            unknown_year = int(row["c"] or 0)
        finally:
            conn.close()

    return {
        "packs": {
            "roots": [str(p) for p in packs.pack_roots()],
            "loaded": loaded,
            "warnings": warnings,
        },
        "journal": {
            "entries": len(quartiles),
            "quartile_scores": scores,
            "default_subset": _default_subset_size(),
            "override": override,
            "unmatched_top": unmatched,
            "unmatched_total": len(unmatched),
            "unknown_year_papers": unknown_year,
        },
        "citation": {
            "styles": cite.available_styles(),
            "default_style": cite.default_style(),
            "venue_overrides": len(
                (styles.get("venue_overrides") or {}) if isinstance(styles, dict) else {}
            ),
        },
        "config": _effective_config(),
    }


def _default_subset_size() -> int:
    """统计内置分科子集条数（不加载 override）。"""
    base = packs.skill_dir("journal-quartiles")
    if base is None:
        return 0
    total = 0
    for path in sorted((base / "content").glob("quartiles_*.json")):
        payload = packs._read_json(path)  # noqa: SLF001 —— 只读诊断用
        if isinstance(payload, dict):
            inner = payload.get("quartiles")
            data = inner if isinstance(inner, dict) else payload
            total += len(data)
    return total


def _effective_config() -> dict[str, Any]:
    s = default_settings
    return {
        "db_path": str(s.db_path),
        "data_dir": str(s.data_dir),
        "threshold_direct": s.threshold_direct,
        "threshold_flag": s.threshold_flag,
        "q_weight_a": s.q_weight_a,
        "q_weight_t": s.q_weight_t,
        "q_flag_penalty": s.q_flag_penalty,
        "max_meta_attempts": s.max_meta_attempts,
        "max_extract_chars": s.max_extract_chars,
        "max_extract_chunks": s.max_extract_chunks,
        "relevance_gate_low_signal": s.relevance_gate_low_signal,
        "global_merge_enabled": s.global_merge_enabled,
        "global_merge_interval_nodes": s.global_merge_interval_nodes,
    }
