"""看板后端数据访问：从 SQLite 读取本体/论文/质量/日志，供 REST API 使用。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from research_agent.config import Settings, settings as default_settings
from research_agent.ontology import store as ont
from research_agent.db import (
    connect,
    get_paper,
    get_quality_result,
    log_event,
    save_quality_result,
    upsert_paper,
    utcnow,
)


def _open(db_path: Path | str | None = None) -> sqlite3.Connection:
    return connect(Path(db_path) if db_path else default_settings.db_path)


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r["name"] for r in rows}


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return name in _tables(conn)


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _table(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])


def list_databases() -> list[dict[str, Any]]:
    """扫描 data 目录，返回可访问的 SQLite 动态本体库清单。"""
    out: list[dict[str, Any]] = []
    if not default_settings.data_dir.exists():
        return out
    for path in sorted(default_settings.data_dir.glob("*.db")):
        item = {
            "path": str(path),
            "name": path.name,
            "size": path.stat().st_size if path.exists() else 0,
            "papers": 0,
            "nodes": 0,
            "edges": 0,
            "runs": 0,
            "logs": 0,
            "last_activity": None,
            "has_ontology": False,
        }
        try:
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            item["has_ontology"] = "ontology_nodes" in tables
            for table in ("papers", "ontology_nodes", "ontology_edges",
                          "ontology_runs", "processing_log"):
                if table in tables:
                    val = int(conn.execute(
                        f"SELECT COUNT(*) AS c FROM {table}"
                    ).fetchone()["c"])
                    if table == "papers":
                        item["papers"] = val
                    elif table == "ontology_nodes":
                        item["nodes"] = val
                    elif table == "ontology_edges":
                        item["edges"] = val
                    elif table == "ontology_runs":
                        item["runs"] = val
                    else:
                        item["logs"] = val
            if "processing_log" in tables:
                row = conn.execute(
                    "SELECT MAX(ts) AS ts FROM processing_log"
                ).fetchone()
                item["last_activity"] = row["ts"]
            conn.close()
        except sqlite3.Error:
            item["has_ontology"] = False
        out.append(item)
    return out


def _latest_run_per_paper(conn: sqlite3.Connection) -> dict[str, dict]:
    """paper_key → 该论文最近一次本体提取统计。"""
    if not _table(conn, "ontology_runs"):
        return {}
    rows = conn.execute(
        """
        SELECT r.paper_key, r.counts, r.new_types, r.ontology_version, r.ran_at
        FROM ontology_runs r
        WHERE r.run_id = (
            SELECT MAX(r2.run_id) FROM ontology_runs r2 WHERE r2.paper_key = r.paper_key
        )
        """
    ).fetchall()
    out = {}
    for r in rows:
        try:
            counts = json.loads(r["counts"] or "{}")
        except json.JSONDecodeError:
            counts = {}
        try:
            new_types = json.loads(r["new_types"] or "[]")
        except json.JSONDecodeError:
            new_types = []
        out[r["paper_key"]] = {
            "counts": counts, "new_types": new_types,
            "version": r["ontology_version"], "ran_at": r["ran_at"],
        }
    return out


def overview(db_path: Path | str | None = None) -> dict[str, Any]:
    conn = _open(db_path)
    try:
        statuses = {}
        if _table(conn, "papers"):
            for r in conn.execute("SELECT status, COUNT(*) AS c FROM papers GROUP BY status"):
                statuses[r["status"]] = r["c"]
        decisions = {}
        if _table(conn, "quality_results"):
            for r in conn.execute(
                "SELECT decision, COUNT(*) AS c FROM quality_results GROUP BY decision"
            ):
                decisions[r["decision"]] = r["c"]
        node_types = {}
        edge_types = {}
        if _table(conn, "ontology_nodes"):
            node_types = {
                r["node_type"]: r["c"] for r in conn.execute(
                    "SELECT node_type, COUNT(*) AS c FROM ontology_nodes GROUP BY node_type")
            }
        if _table(conn, "ontology_edges"):
            edge_types = {
                r["relation_type"]: r["c"] for r in conn.execute(
                    "SELECT relation_type, COUNT(*) AS c FROM ontology_edges GROUP BY relation_type")
            }
        last_activity = None
        if _table(conn, "processing_log"):
            r = conn.execute("SELECT MAX(ts) AS ts FROM processing_log").fetchone()
            last_activity = r["ts"]
        return {
            "papers": _count(conn, "papers"),
            "paper_status": statuses,
            "quality_decisions": decisions,
            "ontology": {
                "nodes": _count(conn, "ontology_nodes"),
                "edges": _count(conn, "ontology_edges"),
                "hyperedges": _count(conn, "ontology_hyperedges"),
                "domains": _count(conn, "ontology_domains"),
                "channels": _count(conn, "ontology_relation_channels"),
                "types": _count(conn, "ontology_type_registry"),
                "node_types": node_types,
                "edge_types": edge_types,
            },
            "logs": _count(conn, "processing_log"),
            "last_activity": last_activity,
        }
    finally:
        conn.close()


def list_papers(db_path: Path | str | None = None) -> list[dict[str, Any]]:
    conn = _open(db_path)
    try:
        runs = _latest_run_per_paper(conn)
        if not _table(conn, "papers"):
            return []
        rows = conn.execute(
            """
            SELECT p.paper_key, p.source, p.title, p.venue, p.pub_year, p.doi,
                   p.publication_status, p.citation_count, p.status, p.created_at,
                   p.authors_meta, q.quality, q.authority, q.timeliness,
                   q.decision AS q_decision, q.needs_review, q.meta_missing
            FROM papers p LEFT JOIN quality_results q ON q.paper_key = p.paper_key
            ORDER BY p.created_at DESC
            """
        ).fetchall()
        papers = []
        for r in rows:
            try:
                authors = json.loads(r["authors_meta"] or "[]")
            except json.JSONDecodeError:
                authors = []
            try:
                meta_missing = json.loads(r["meta_missing"] or "[]")
            except json.JSONDecodeError:
                meta_missing = []
            run = runs.get(r["paper_key"]) or {}
            counts = run.get("counts") or {}
            extracted = counts.get("extracted") or counts
            papers.append({
                "paper_key": r["paper_key"],
                "source": r["source"], "title": r["title"], "venue": r["venue"],
                "pub_year": r["pub_year"], "doi": r["doi"],
                "publication_status": r["publication_status"],
                "citation_count": r["citation_count"],
                "status": r["status"], "created_at": r["created_at"],
                "authors_count": len(authors),
                "affiliations_count": sum(
                    len(a.get("affiliations") or []) for a in authors),
                "quality": r["quality"], "authority": r["authority"],
                "timeliness": r["timeliness"], "decision": r["q_decision"],
                "needs_review": bool(r["needs_review"]),
                "meta_missing": meta_missing,
                "extracted": {
                    "entities": extracted.get("entities", 0),
                    "relations": extracted.get("relations", 0),
                    "events": extracted.get("events", 0),
                },
                "run_at": run.get("ran_at"),
            })
        return papers
    finally:
        conn.close()


def get_paper_detail(key: str, db_path: Path | str | None = None) -> dict[str, Any] | None:
    conn = _open(db_path)
    try:
        if not _table(conn, "papers"):
            return None
        row = conn.execute("SELECT * FROM papers WHERE paper_key=?", (key,)).fetchone()
        if not row:
            return None
        rec = dict(row)
        try:
            rec["authors"] = json.loads(rec.pop("authors_meta") or "[]")
        except json.JSONDecodeError:
            rec["authors"] = []
        blob = rec.pop("pdf_blob", None)
        rec["pdf_size"] = len(blob) if blob else rec.get("pdf_size")
        clean = rec.pop("clean_text", None) or ""
        rec["clean_preview"] = clean[:12000]
        rec["clean_chars"] = len(clean)

        quality = None
        if _table(conn, "quality_results"):
            q = conn.execute(
                "SELECT * FROM quality_results WHERE paper_key=?", (key,)
            ).fetchone()
            if q:
                quality = dict(q)
                try:
                    quality["meta_missing"] = json.loads(
                        quality.get("meta_missing") or "[]")
                except json.JSONDecodeError:
                    quality["meta_missing"] = []
        run = _latest_run_per_paper(conn).get(key)
        logs = []
        if _table(conn, "processing_log"):
            for r in conn.execute(
                "SELECT node, event, details, ts FROM processing_log "
                "WHERE paper_key=? ORDER BY id DESC LIMIT 100", (key,)
            ):
                d = dict(r)
                try:
                    d["details"] = json.loads(d.get("details") or "null")
                except json.JSONDecodeError:
                    d["details"] = None
                logs.append(d)
        return {
            "paper": rec, "quality": quality, "run": run, "logs": logs,
            "db_has_blob": blob is not None,
        }
    finally:
        conn.close()


def agent_status(db_path: Path | str | None = None,
                 recent: int = 40) -> list[dict[str, Any]]:
    """按节点聚合智能体工作状态与最近事件。"""
    conn = _open(db_path)
    try:
        agents = {
            "retrieval": {"id": "retrieval", "label": "文献检索节点",
                          "desc": "检索 / PDF 入库 / 清洗 / 元数据回补"},
            "quality": {"id": "quality", "label": "质量控制节点",
                        "desc": "A/T/Q 评分 / 路由 / 人工审核判定"},
            "knowledge": {"id": "knowledge", "label": "知识提取节点",
                          "desc": "预处理 / 实体关系事件抽取 / 动态本体写入"},
            "human_review": {"id": "human_review", "label": "人工审核",
                             "desc": "低质量或元数据无法补全的文献"},
        }
        for a in agents.values():
            a["events"] = []
            a["count"] = 0
            a["paper_count"] = 0
            a["last_ts"] = None
        recent_events: list[dict[str, Any]] = []
        if _table(conn, "processing_log"):
            rows = conn.execute(
                "SELECT paper_key, node, event, details, ts FROM processing_log "
                "ORDER BY id DESC LIMIT ?", (recent,)
            ).fetchall()
            for r in rows:
                d = dict(r)
                try:
                    d["details"] = json.loads(d.get("details") or "null")
                except json.JSONDecodeError:
                    d["details"] = None
                bucket = d["node"]
                if d["node"] == "quality" and d["event"] == "human-review":
                    bucket = "human_review"
                if bucket not in agents:
                    continue
                agents[bucket]["events"].append(d)
                recent_events.append({**d, "agent": bucket})
            # 聚合统计
            for r in conn.execute(
                "SELECT node, COUNT(*) AS c, COUNT(DISTINCT paper_key) AS pc, MAX(ts) AS mt "
                "FROM processing_log GROUP BY node"
            ):
                if r["node"] in agents:
                    agents[r["node"]]["count"] = r["c"]
                    agents[r["node"]]["paper_count"] = r["pc"]
                    agents[r["node"]]["last_ts"] = r["mt"]
            hrow = conn.execute(
                "SELECT COUNT(*) AS c, COUNT(DISTINCT paper_key) AS pc, MAX(ts) AS mt "
                "FROM processing_log WHERE node='quality' AND event='human-review'"
            ).fetchone()
            agents["human_review"]["count"] = hrow["c"]
            agents["human_review"]["paper_count"] = hrow["pc"]
            agents["human_review"]["last_ts"] = hrow["mt"]
        if _table(conn, "papers"):
            n_human = int(conn.execute(
                "SELECT COUNT(*) AS c FROM papers WHERE status='human_review'"
            ).fetchone()["c"])
            if n_human and agents["human_review"]["paper_count"] == 0:
                agents["human_review"]["paper_count"] = n_human
        return list(agents.values()), recent_events
    finally:
        conn.close()


def node_status(db_path: Path | str | None = None,
                recent: int = 30) -> dict[str, Any]:
    """聚合数据构建与研究流程各节点状态、事件、日志。"""
    defs: list[dict[str, Any]] = [
        {"id": "retrieval", "group": "数据构建", "label": "文献检索节点",
         "desc": "检索 / PDF 入库 / 清洗 / 元数据回补"},
        {"id": "quality", "group": "数据构建", "label": "质量控制节点",
         "desc": "A/T/Q 评分 / 路由 / 领域词典全局归并"},
        {"id": "knowledge", "group": "数据构建", "label": "知识提取节点",
         "desc": "预处理 / 实体关系事件抽取 / 动态本体写入"},
        {"id": "human_review", "group": "数据构建", "label": "人工审核",
         "desc": "低质量或元数据无法补全的文献"},
        {"id": "planner", "group": "研究流程", "label": "工作规划节点",
         "desc": "解析用户请求，生成检索、分析、证据与生成契约"},
        {"id": "collection", "group": "研究流程", "label": "检索执行节点",
         "desc": "执行 Planner retrieval_plan -> 检索/评估/知识提取"},
        {"id": "knowledge_consumer", "group": "研究流程", "label": "知识消费节点",
         "desc": "LLM 机制理解 / 机会发现 / 设计上下文 / 补检请求"},
        {"id": "content_builder", "group": "研究流程", "label": "内容形成节点",
         "desc": "生成可溯源草稿，并返回需要补强的边"},
        {"id": "reviewer", "group": "研究流程", "label": "审核校对节点",
         "desc": "核查引用、证据支持、设计契约（算子链/创新等级/硬约束）与覆盖缺口"},
        {"id": "fact_checker", "group": "研究流程", "label": "事实核查节点",
         "desc": "核查无来源断言、伪造引用与数值缺证据，可回流修订"},
    ]
    conn = _open(db_path)
    try:
        recent_events: list[dict[str, Any]] = []
        if _table(conn, "processing_log"):
            rows = conn.execute(
                "SELECT id, paper_key, node, event, details, ts "
                "FROM processing_log ORDER BY id DESC LIMIT ?",
                (max(100, recent * 20),),
            ).fetchall()
            for r in rows:
                d = dict(r)
                try:
                    d["details"] = json.loads(d.get("details") or "null")
                except json.JSONDecodeError:
                    d["details"] = None
                recent_events.append(d)
        nodes: list[dict[str, Any]] = []
        for d in defs:
            nid = d["id"]
            events: list[dict[str, Any]] = []
            for ev in recent_events:
                bucket = None
                if nid == "human_review" and ev["node"] == "quality" \
                        and ev["event"] == "human-review":
                    bucket = "human_review"
                elif nid == "study" and ev["node"] == "study":
                    bucket = "study"
                elif ev["node"] == "study" and ev["event"] == nid:
                    bucket = nid
                elif ev["node"] == nid:
                    bucket = nid
                if bucket == nid:
                    events.append(ev)
            last = events[0] if events else None
            det = (last.get("details") or {}) if last else {}
            status = "idle"
            if last:
                if det.get("status") in ("running",):
                    status = "running"
                elif det.get("status") == "error":
                    status = "error"
                elif det.get("status") == "needs_collection":
                    status = "needs_collection"
                elif nid == "human_review" or det.get("decision") == "human":
                    status = "waiting"
                else:
                    status = "done"
            count = 0
            paper_count = 0
            if _table(conn, "processing_log"):
                if nid == "human_review":
                    cond = "node='quality' AND event='human-review'"
                elif nid in {x["id"] for x in defs[4:]}:
                    cond = f"node='study' AND event='{nid}'"
                else:
                    cond = f"node='{nid}'"
                row = conn.execute(
                    f"SELECT COUNT(*) AS c, COUNT(DISTINCT paper_key) AS pc "
                    f"FROM processing_log WHERE {cond}"
                ).fetchone()
                count = int(row["c"] or 0)
                paper_count = int(row["pc"] or 0)
            nodes.append({
                **d,
                "status": status,
                "count": count,
                "paper_count": paper_count,
                "last_ts": last["ts"] if last else None,
                "last_event": last["event"] if last else None,
                "details": det,
                "events": events[:5],
            })
        return {"nodes": nodes, "recent": recent_events[:recent]}
    finally:
        conn.close()


def study_status(db_path: Path | str | None = None,
                 recent: int = 300) -> dict[str, Any]:
    """返回最近一次研究任务四节点的运行状态。"""
    conn = _open(db_path)
    try:
        if not _table(conn, "processing_log"):
            return {"run_id": None, "status": "idle", "request": None,
                    "nodes": [], "events": [], "summary": {}}
        rows = conn.execute(
            "SELECT id, paper_key, node, event, details, ts "
            "FROM processing_log WHERE node='study' "
            "ORDER BY id DESC LIMIT ?", (recent,)
        ).fetchall()
        parsed = []
        for r in rows:
            d = dict(r)
            try:
                d["details"] = json.loads(d.get("details") or "null")
            except json.JSONDecodeError:
                d["details"] = None
            parsed.append(d)
        start = next((d for d in parsed if d["event"] == "session-start"), None)
        if not start:
            return {"run_id": None, "status": "idle", "request": None,
                    "nodes": [], "events": [], "summary": {}}
        start_id = int(start["id"])
        run_events = [d for d in parsed if int(d["id"]) >= start_id]
        run_events.reverse()
        run_id = (start.get("details") or {}).get("run_id")

        defs = [
            ("planner", "工作规划节点",
             "解析用户请求，生成检索、分析、证据与生成契约"),
            ("collection", "检索执行节点",
             "执行 Planner retrieval_plan -> 检索/评估/知识提取"),
            ("knowledge_consumer", "知识消费节点",
             "LLM 机制理解 / 机会发现 / 设计上下文 / 补检请求"),
            ("content_builder", "内容形成节点",
             "候选池 -> 算子链 -> 聚类去重与排序"),
            ("reviewer", "审核校对节点",
             "引用、证据、设计契约与覆盖缺口"),
            ("fact_checker", "事实核查节点",
             "无来源断言、伪造引用与数值缺证据"),
        ]
        nodes = []
        for event_name, label, desc in defs:
            evs = [d for d in run_events if d["event"] == event_name]
            status = "pending"
            last = evs[-1] if evs else None
            if last:
                det = last.get("details") or {}
                status = det.get("status", "done")
                if status in ("running",):
                    status = "running"
                elif status == "error":
                    status = "error"
                elif status == "needs_collection":
                    status = "needs_collection"
                elif status in ("reviewed", "manual_review", "done"):
                    status = "done"
            nodes.append({
                "id": event_name,
                "label": label,
                "desc": desc,
                "status": status,
                "last_ts": last["ts"] if last else None,
                "count": len(evs),
                "details": (last.get("details") or {}) if last else {},
            })
        end = next((d for d in run_events if d["event"] == "session-end"), None)
        overall = "running"
        if end:
            det = end.get("details") or {}
            overall = "completed" if det.get("status") != "error" else "error"
        summary = {}
        for ev in run_events:
            det = ev.get("details") or {}
            if ev["event"] == "planner" and det.get("status") == "done":
                summary["plan"] = {
                    "goal": det.get("goal"),
                    "domain": det.get("domain"),
                    "content_type": det.get("content_type"),
                    "task_kind": det.get("task_kind"),
                    "min_candidates": det.get("min_candidates"),
                    "seed_terms": det.get("seed_terms"),
                }
            elif ev["event"] == "knowledge_consumer":
                summary["knowledge"] = {
                    "patterns": det.get("patterns"),
                    "evidence": det.get("evidence"),
                    "hyperedges": det.get("hyperedges"),
                    "coverage_score": det.get("coverage_score"),
                    "papers": det.get("papers"),
                    "mechanism_states": det.get("mechanism_states"),
                    "operator_candidates": det.get("operator_candidates"),
                    "traceability": det.get("traceability"),
                }
            elif ev["event"] == "content_builder" and det.get("status") == "done":
                summary["draft"] = {
                    "title": det.get("title"),
                    "sections": det.get("sections"),
                    "markdown_chars": det.get("markdown_chars"),
                    "candidates_generated": det.get("candidates_generated"),
                    "candidates_after_dedupe": det.get("candidates_after_dedupe"),
                    "candidates_selected": det.get("candidates_selected"),
                    "selected_levels": det.get("selected_levels"),
                }
            elif ev["event"] == "reviewer":
                summary["review"] = {
                    "decision": det.get("decision"),
                    "issues": det.get("issues"),
                    "round": det.get("round"),
                    "summary": det.get("summary"),
                    "design_failures": det.get("design_failures"),
                }
            elif ev["event"] == "fact_checker":
                summary["fact_check"] = {
                    "decision": det.get("decision"),
                    "issues": det.get("issues"),
                    "mode": det.get("mode"),
                }
        return {
            "run_id": run_id,
            "status": overall,
            "request": (start.get("details") or {}).get("request"),
            "started_at": start["ts"],
            "ended_at": end["ts"] if end else None,
            "nodes": nodes,
            "events": run_events[-80:],
            "summary": summary,
        }
    finally:
        conn.close()


def activity_log(limit: int = 80,
                 db_path: Path | str | None = None,
                 node: str | None = None,
                 event: str | None = None,
                 search: str | None = None) -> list[dict[str, Any]]:
    conn = _open(db_path)
    try:
        if not _table(conn, "processing_log"):
            return []
        sql = "SELECT id, paper_key, node, event, details, ts FROM processing_log WHERE 1=1"
        params: list[Any] = []
        if node:
            sql += " AND node=?"
            params.append(node)
        if event:
            sql += " AND event=?"
            params.append(event)
        if search:
            sql += " AND (paper_key LIKE ? OR details LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like])
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(
            sql, params
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["details"] = json.loads(d.get("details") or "null")
            except json.JSONDecodeError:
                d["details"] = None
            out.append(d)
        return out
    finally:
        conn.close()


def run_planner_request(request_text: str,
                        db_path: Path | str | None = None,
                        model: Any = None) -> dict[str, Any]:
    """在工作规划节点上执行一条用户指令，返回任务单并写入事件日志。"""
    from research_agent.config import Settings
    from research_agent.study.planner import make_planner_node

    settings = Settings(db_path=Path(db_path or default_settings.db_path))
    conn = _open(settings.db_path)
    try:
        node = make_planner_node(model, conn=conn, settings=settings)
        out = node({"request": str(request_text or "").strip()})
        return {
            "status": out.get("status"),
            "plan": out.get("plan") or {},
            "db": str(settings.db_path),
        }
    finally:
        conn.close()


HUMAN_REVIEW_PRESETS = {
    "accept": {
        "label": "通过并进入知识提取",
        "decision": "knowledge",
        "needs_review": False,
        "status": "ingested",
    },
    "accept_flagged": {
        "label": "标记后进入知识提取",
        "decision": "flagged",
        "needs_review": True,
        "status": "ingested",
    },
    "enrich": {
        "label": "退回元数据补全",
        "decision": "enrich",
        "needs_review": False,
        "status": "needs_enrich",
    },
    "reject": {
        "label": "拒绝并排除该文献",
        "decision": "rejected",
        "needs_review": False,
        "status": "rejected",
    },
}


def human_review_items(db_path: Path | str | None = None,
                       include_history: bool = False) -> dict[str, Any]:
    """返回待人工审核文献与可选历史记录。"""
    conn = _open(db_path)
    try:
        pending: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        if _table(conn, "papers"):
            rows = conn.execute(
                """
                SELECT p.paper_key, p.source, p.title, p.venue, p.pub_year,
                       p.status, p.created_at,
                       substr(COALESCE(p.clean_text, ''), 1, 1800) AS clean_preview,
                       q.quality, q.authority, q.timeliness, q.decision,
                       q.needs_review, q.rationale, q.assessed_at
                FROM papers p
                LEFT JOIN quality_results q ON q.paper_key = p.paper_key
                WHERE p.status = 'human_review'
                ORDER BY p.created_at DESC
                """
            ).fetchall()
            for r in rows:
                d = dict(r)
                d["presets"] = list(HUMAN_REVIEW_PRESETS.keys())
                pending.append(d)
        if include_history and _table(conn, "human_reviews"):
            rows = conn.execute(
                """
                SELECT r.id, r.paper_key, r.action, r.decision, r.rationale,
                       r.custom_result, r.reviewed_at,
                       p.title, p.source, p.pub_year
                FROM human_reviews r
                LEFT JOIN papers p ON p.paper_key = r.paper_key
                ORDER BY r.id DESC LIMIT 200
                """
            ).fetchall()
            history = [dict(r) for r in rows]
        return {
            "pending": pending,
            "history": history,
            "presets": HUMAN_REVIEW_PRESETS,
        }
    finally:
        conn.close()


def submit_human_review(db_path: Path | str | None,
                        payload: dict[str, Any]) -> dict[str, Any]:
    """写入人工审核结果：支持预制动作或自定义 decision/rationale。"""
    key = str(payload.get("paper_key") or "").strip()
    if not key:
        return {"ok": False, "error": "paper_key 为空"}
    action = str(payload.get("action") or "custom").strip()
    rationale = str(payload.get("rationale") or "").strip()
    custom_result = payload.get("custom_result")
    conn = _open(db_path)
    try:
        paper = get_paper(conn, key)
        if not paper:
            return {"ok": False, "error": f"文献不存在: {key}"}
        preset = HUMAN_REVIEW_PRESETS.get(action)
        if preset:
            decision = preset["decision"]
            needs_review = preset["needs_review"]
            status = preset["status"]
            action_label = preset["label"]
        elif action == "custom":
            decision = str(payload.get("decision") or "human").strip().lower()
            if decision not in ("knowledge", "flagged", "enrich", "rejected", "human"):
                decision = "human"
            needs_review = bool(payload.get("needs_review", decision == "flagged"))
            status = {
                "knowledge": "ingested",
                "flagged": "ingested",
                "enrich": "needs_enrich",
                "rejected": "rejected",
                "human": "human_review",
            }.get(decision, paper.get("status") or "human_review")
            action_label = f"自定义: {decision}"
        else:
            return {"ok": False, "error": f"未知审核动作: {action}"}

        q = get_quality_result(conn, key) or {
            "paper_key": key,
            "quality": None, "authority": None, "timeliness": None,
        }
        q.update({
            "paper_key": key,
            "decision": decision,
            "needs_review": needs_review,
            "rationale": rationale or q.get("rationale") or action_label,
        })
        save_quality_result(conn, q)
        conn.execute("UPDATE papers SET status=? WHERE paper_key=?",
                     (status, key))
        custom_text = None
        if custom_result is not None:
            custom_text = custom_result if isinstance(custom_result, str) \
                else json.dumps(custom_result, ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO human_reviews(
                paper_key, action, decision, rationale, custom_result, reviewed_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (key, action, decision, rationale, custom_text, utcnow()),
        )
        log_event(conn, "human_review", "review-submitted", key, {
            "action": action,
            "action_label": action_label,
            "decision": decision,
            "status": status,
            "rationale": rationale,
        })
        return {"ok": True, "paper_key": key, "decision": decision,
                "status": status, "action": action}
    finally:
        conn.close()


def ontology_graph(db_path: Path | str | None = None, *,
                   min_confidence: float = 0.0,
                   types: list[str] | None = None,
                   query: str | None = None,
                   domain: str | None = None,
                   limit: int = 800) -> dict[str, Any]:
    """返回本体子图（节点+边）。默认返回全部；支持按类型/置信度/名称过滤。"""
    conn = _open(db_path)
    try:
        if not _table(conn, "ontology_nodes"):
            return {"nodes": [], "edges": [], "truncated": False, "total": 0}
        sql = ("SELECT node_id, node_type, name, confidence, attributes, aliases, "
               "first_seen_at, last_seen_at FROM ontology_nodes WHERE 1=1")
        params: list[Any] = []
        if types:
            placeholders = ",".join("?" * len(types))
            sql += f" AND node_type IN ({placeholders})"
            params.extend(types)
        if min_confidence > 0:
            sql += " AND confidence >= ?"
            params.append(min_confidence)
        if query:
            sql += " AND (name LIKE ? OR normalized_name LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like])
        if domain and _table(conn, "ontology_domain_members"):
            domain_ids = [int(r["node_id"]) for r in conn.execute(
                "SELECT m.node_id FROM ontology_domain_members m "
                "JOIN ontology_domains d ON d.domain_key=m.domain_key "
                "WHERE d.domain_key=? OR d.label=?", (domain, domain)).fetchall()]
            if not domain_ids:
                return {"nodes": [], "edges": [], "hyperedges": [], "domains": [],
                        "channels": [], "truncated": False, "total": 0,
                        "shown_nodes": 0, "shown_edges": 0,
                        "shown_hyperedges": 0, "shown_domains": 0,
                        "shown_channels": 0}
            ph = ",".join("?" * len(domain_ids))
            sql += f" AND node_id IN ({ph})"
            params.extend(domain_ids)
        rows = conn.execute(sql, params).fetchall()
        total = len(rows)
        truncated = total > limit
        if truncated:
            rows = rows[:limit]
        node_ids: set[int] = set()
        nodes = []
        for r in rows:
            node_ids.add(int(r["node_id"]))
            nodes.append({
                "id": int(r["node_id"]),
                "label": r["name"],
                "type": r["node_type"],
                "confidence": round(float(r["confidence"] or 0), 3),
                "first_seen_at": r["first_seen_at"],
                "last_seen_at": r["last_seen_at"],
            })
        edges = []
        if node_ids and _table(conn, "ontology_edges"):
            ids = sorted(node_ids)
            if len(ids) <= limit * 2:
                ph = ",".join("?" * len(ids))
                erows = conn.execute(
                    f"SELECT edge_id, relation_type, source_node, target_node, confidence "
                    f"FROM ontology_edges WHERE source_node IN ({ph}) AND target_node IN ({ph})",
                    ids + ids,
                ).fetchall()
                for e in erows:
                    edges.append({
                        "id": int(e["edge_id"]),
                        "from": int(e["source_node"]),
                        "to": int(e["target_node"]),
                        "label": e["relation_type"],
                        "type": e["relation_type"],
                        "confidence": round(float(e["confidence"] or 0), 3),
                    })
        hyperedges = []
        try:
            if _table(conn, "ontology_hyperedges"):
                hyperedges = ont.list_hyperedges(
                    conn, limit=min(500, max(50, limit)),
                    min_confidence=min_confidence,
                    node_ids=node_ids if node_ids else None,
                )
        except Exception:
            hyperedges = []
        domains = []
        channels = []
        try:
            if _table(conn, "ontology_domains"):
                domains = [dict(r) for r in conn.execute(
                    "SELECT d.domain_key, d.label, d.domain_type, d.description, "
                    "COUNT(m.node_id) AS member_count "
                    "FROM ontology_domains d LEFT JOIN ontology_domain_members m "
                    "ON m.domain_key=d.domain_key GROUP BY d.domain_key "
                    "ORDER BY member_count DESC LIMIT 100"
                ).fetchall()]
            if _table(conn, "ontology_relation_channels"):
                channels = [dict(r) for r in conn.execute(
                    "SELECT channel_key, relation_family, role_profile, "
                    "source_domains, target_domains, support_count, paper_count, "
                    "confidence, summary FROM ontology_relation_channels "
                    "ORDER BY support_count DESC, confidence DESC LIMIT 300"
                ).fetchall()]
                for c in channels:
                    for key in ("role_profile", "source_domains", "target_domains"):
                        try: c[key] = json.loads(c.get(key) or "null")
                        except json.JSONDecodeError: pass
        except Exception:
            domains = domains or []
            channels = channels or []
        return {"nodes": nodes, "edges": edges,
                "hyperedges": hyperedges, "domains": domains, "channels": channels,
                "truncated": truncated, "total": total,
                "shown_nodes": len(nodes), "shown_edges": len(edges),
                "shown_hyperedges": len(hyperedges),
                "shown_domains": len(domains), "shown_channels": len(channels)}
    finally:
        conn.close()


def ontology_node_detail(node_id: int,
                         db_path: Path | str | None = None) -> dict[str, Any] | None:
    conn = _open(db_path)
    try:
        if not _table(conn, "ontology_nodes"):
            return None
        r = conn.execute(
            "SELECT * FROM ontology_nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        if not r:
            return None
        node = dict(r)
        for k in ("attributes", "aliases", "provenance"):
            try:
                node[k] = json.loads(node.get(k) or ("[]" if k == "aliases" else "{}"))
            except json.JSONDecodeError:
                node[k] = [] if k == "aliases" else {}
        neighbors = []
        if _table(conn, "ontology_edges"):
            rows = conn.execute(
                """
                SELECT e.edge_id, e.relation_type, e.source_node, e.target_node,
                       e.confidence, e.provenance, n.node_type AS other_type,
                       n.name AS other_name, n.normalized_name AS other_norm
                FROM ontology_edges e
                JOIN ontology_nodes n ON n.node_id =
                    CASE WHEN e.source_node=? THEN e.target_node ELSE e.source_node END
                WHERE e.source_node=? OR e.target_node=?
                ORDER BY e.edge_id
                """,
                (node_id, node_id, node_id),
            ).fetchall()
            for e in rows:
                try:
                    prov = json.loads(e["provenance"] or "[]")
                except json.JSONDecodeError:
                    prov = []
                direction = "out" if int(e["source_node"]) == node_id else "in"
                neighbors.append({
                    "edge_id": int(e["edge_id"]),
                    "relation_type": e["relation_type"],
                    "direction": direction,
                    "other_id": int(e["source_node"] if direction == "out" else e["target_node"]),
                    "other_type": e["other_type"],
                    "other_name": e["other_name"],
                    "confidence": round(float(e["confidence"] or 0), 3),
                    "provenance": prov,
                })
        return {"node": node, "neighbors": neighbors}
    finally:
        conn.close()
