"""协作：规划节点组织并执行"补齐支撑"的任务。

职责边界（方案 v6 §1）：

- **工作规划节点**：协作的**组织者与执行者**——接收需求、组织任务、下单、回报；
- **知识消费节点**：协作的**需求提出方**——它组织材料时发现不足，产出
  `retrieval_requests`；
- 真正抓取由**采集链**（`study/collection.py`）完成，本模块只做组织与调度。

用户的决定映射到协作动作：

| 用户选择 | 协作动作 |
|---|---|
| 保留缺口 | 不协作（直接写作，正文顶部标注） |
| 执行检索补全 | 检索（轮数受用户/模型给的上限约束） |
| 自定义任务 A | 只检索 |
| 自定义任务 B | 检索 + 对新入库文献抽知识 |
| 自定义任务 C | 检索 + 对库内既有同主题文献抽知识 |

**收敛保护**：轮数上限硬约束在 `MAX_COLLECTION_ROUNDS` 内；
每轮结束都比对"新增是否为零"，连续两轮无新增就停止（避免无意义地烧配额）。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from research_agent.config import Settings, settings as default_settings
from research_agent.writing import interview as iv

logger = logging.getLogger(__name__)

__all__ = ["run_collaboration"]


def run_collaboration(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    section_key: str,
    section_state: iv.SectionPlanState,
    project: dict[str, Any],
    settings: Settings | None = None,
    progress_cb: Any = None,
) -> dict[str, Any]:
    """按用户的缺口决定执行协作，返回可审计的回报。

    回报结构：``{"kind", "rounds_requested", "rounds_done", "added",
    "extracted", "summary", "steps": [...]}``
    """
    s = settings or default_settings
    decision = section_state.gap_decision
    _progress(progress_cb, 10.0, "准备协作任务…")

    if decision == iv.GAP_KEEP:
        return {"kind": "keep_gap", "rounds_requested": 0, "rounds_done": 0,
                "added": 0, "extracted": 0,
                "summary": "保留缺口（按用户决定，不补齐）", "steps": []}

    # 自定义任务：取用户选定的执行方案；否则就是"补检索"
    plan = _selected_custom_plan(section_state)
    tasks = list(plan.get("tasks") or ["retrieve"])
    queries = list(plan.get("query_terms")
                   or (section_state.verdict.get("suggested_queries") or []))
    rounds_cap = int(section_state.collection_rounds
                     or iv.DEFAULT_COLLECTION_ROUNDS)
    rounds_cap = max(iv.MIN_COLLECTION_ROUNDS,
                     min(iv.MAX_COLLECTION_ROUNDS, rounds_cap))

    steps: list[dict[str, Any]] = []
    added_total = 0
    extracted_total = 0
    rounds_done = 0
    exhausted = False

    if "retrieve" in tasks:
        added_total, rounds_done, steps, exhausted = _do_retrieve(
            conn, section_key=section_key, queries=queries,
            rounds_cap=rounds_cap, settings=s, progress_cb=progress_cb)

    if "extract_knowledge" in tasks:
        scope = str(plan.get("extract_scope") or "new")
        extracted_total, extract_steps = _do_extract(
            conn, scope=scope, project=project, section_key=section_key,
            added_keys=_added_keys(steps), settings=s, progress_cb=progress_cb)
        steps.extend(extract_steps)

    summary_bits = [f"检索 {rounds_done} 轮", f"新增 {added_total} 篇"]
    if "extract_knowledge" in tasks:
        summary_bits.append(f"抽取 {extracted_total} 篇")
    if exhausted:
        summary_bits.append("连续无新增，已提前停止")
    _progress(progress_cb, 90.0, "、".join(summary_bits))

    return {
        "kind": plan.get("id") or "collect",
        "rounds_requested": rounds_cap,
        "rounds_done": rounds_done,
        "added": added_total,
        "extracted": extracted_total,
        "queries": queries[:6],
        "tasks": tasks,
        "extract_scope": plan.get("extract_scope") or "",
        "exhausted": exhausted,
        "summary": "、".join(summary_bits),
        "steps": steps,
    }


def _selected_custom_plan(sec: iv.SectionPlanState) -> dict[str, Any]:
    """取用户选定的自定义执行方案；"让 AI 决定"时取方案 B（检索+抽取，覆盖面最广）。"""
    plans = sec.custom_plans or []
    if not plans:
        return {}
    if sec.custom_choice and sec.custom_choice != "ai":
        for item in plans:
            if str(item.get("id")) == sec.custom_choice:
                return dict(item)
    if sec.custom_choice == "ai":
        for item in plans:
            if str(item.get("id")) == "B":
                return dict(item)
    return dict(plans[0])


# ------------------------------------------------------------------ 检索

def _do_retrieve(conn: sqlite3.Connection, *, section_key: str,
                 queries: list[str], rounds_cap: int, settings: Settings,
                 progress_cb: Any) -> tuple[int, int, list[dict[str, Any]], bool]:
    """执行检索补全。

    复用既有采集链（`collect_mission` + `pipeline` 服务）。
    **收敛保护**：某轮新增为 0 时，下一轮若仍为 0 就停止。
    """
    from research_agent.study.collection import collect_mission
    from research_agent.pipeline import Services

    terms = [str(q) for q in queries if str(q).strip()][:6]
    if not terms:
        terms = ["research"]     # 兜底，避免空检索词
    request = {"seed_terms": terms, "reason": f"访谈协作：{section_key}"}
    services = Services()
    added_total = 0
    rounds_done = 0
    steps: list[dict[str, Any]] = []
    exhausted = False
    zero_streak = 0

    for index in range(1, rounds_cap + 1):
        _progress(progress_cb, 20.0 + 15.0 * index,
                  f"第 {index}/{rounds_cap} 轮检索：{'、'.join(terms[:3])}…")
        try:
            report = collect_mission(request, services=services) or {}
        except Exception as exc:  # noqa: BLE001 —— 协作失败不该让访谈崩掉
            logger.warning("检索协作失败: %s", exc)
            steps.append({"round": index, "task": "retrieve", "ok": False,
                          "error": f"{type(exc).__name__}: {exc}"})
            break
        count = int(report.get("count") or 0)
        keys = list(report.get("paper_keys") or [])
        added_total += count
        rounds_done += 1
        steps.append({"round": index, "task": "retrieve", "ok": True,
                      "count": count, "paper_keys": keys[:50],
                      "errors": list(report.get("errors") or [])[:5]})
        if count == 0:
            zero_streak += 1
            if zero_streak >= 2:
                exhausted = True
                break
        else:
            zero_streak = 0
    return added_total, rounds_done, steps, exhausted


def _added_keys(steps: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for step in steps:
        for key in step.get("paper_keys") or []:
            if key not in out:
                out.append(str(key))
    return out


# ------------------------------------------------------------------ 抽取

def _do_extract(conn: sqlite3.Connection, *, scope: str,
                project: dict[str, Any], section_key: str,
                added_keys: list[str], settings: Settings,
                progress_cb: Any) -> tuple[int, list[dict[str, Any]]]:
    """执行知识抽取。

    - ``scope == "new"``：只抽本次新增的文献；
    - ``scope == "existing"``：抽库内**同主题**的既有文献（常见瓶颈是"有文献但没抽"）。
    """
    from research_agent.pipeline import Services, process_papers

    if scope == "new":
        targets = added_keys[:20]
    else:
        targets = _existing_same_topic_keys(
            conn, str(project.get("topic") or project.get("title") or ""),
            limit=20)
    if not targets:
        return 0, [{"task": "extract_knowledge", "scope": scope, "ok": True,
                    "count": 0, "note": "没有可抽取的目标文献"}]

    _progress(progress_cb, 70.0, f"正在抽取 {len(targets)} 篇文献的知识…")
    services = Services()
    ok = 0
    for index, key in enumerate(targets, 1):
        _progress(progress_cb, 70.0 + 20.0 * index / max(1, len(targets)),
                  f"抽取 {index}/{len(targets)}：{key}")
        try:
            results = process_papers([key], services=services, conn=conn)
        except Exception as exc:  # noqa: BLE001
            logger.warning("抽取失败 %s: %s", key, exc)
            continue
        status = str((results[0] if results else {}).get("status") or "")
        if status in ("extracted", "knowledge"):
            ok += 1
    return ok, [{"task": "extract_knowledge", "scope": scope, "ok": True,
                 "count": ok, "targets": targets}]


def _existing_same_topic_keys(conn: sqlite3.Connection, topic: str,
                              limit: int = 20) -> list[str]:
    """库内同主题、**尚未抽取**的文献（按标题/摘要关键词匹配）。"""
    from research_agent.study.planner import split_seed_terms
    terms = [t for t in split_seed_terms(topic) if len(t) >= 3][:6]
    if not terms:
        return []
    clauses = []
    params: list[Any] = []
    for term in terms:
        like = f"%{term}%"
        clauses.append("(LOWER(COALESCE(p.title,'')) LIKE ? OR "
                       "LOWER(COALESCE(p.abstract,'')) LIKE ? OR "
                       "LOWER(COALESCE(p.keywords,'')) LIKE ?)")
        params.extend([like, like, like])
    where = " OR ".join(clauses)
    try:
        rows = conn.execute(
            f"SELECT p.paper_key FROM papers p WHERE p.status='ingested' "
            f"  AND ({where}) "
            f"  AND NOT EXISTS (SELECT 1 FROM processing_log l "
            f"                  WHERE l.paper_key = p.paper_key "
            f"                    AND l.event = 'extracted') "
            f"LIMIT ?", (*params, int(limit))).fetchall()
    except sqlite3.Error:
        return []
    return [str(r["paper_key"]) for r in rows]


def _progress(cb: Any, percent: float, message: str) -> None:
    if cb is None:
        return
    try:
        cb(percent, message)
    except Exception:  # noqa: BLE001
        pass
