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
import time
from typing import Any

from research_agent.config import Settings, settings as default_settings
from research_agent.writing import interview as iv

logger = logging.getLogger(__name__)

__all__ = ["run_collaboration", "build_retrieval_services",
           "build_retrieval_collector", "KNOWLEDGE_ONLY_DIMENSIONS"]

#: 判定里**只有抽取才能改善**的维度。
#:
#: 来源（`sufficiency.py` 的真实 SQL）：`papers` 是实时查 `papers` 表，检索入库
#: 立刻生效；而 `knowledge`（processing_log 的 extracted 占比）、`conditions`
#: （超边带条件/测量的比例）、`requirements`（知识库词元覆盖）、`evidence`
#: （超边/边条数）、`comparison`（按类型成组）**全部读抽取产物**。
#:
#: 所以"只检索不抽取"最多只能补上 papers 一个维度——其余五个纹丝不动，
#: 判定结论不会变，用户看到的就是"补检了还是不行"而卡在同一处。
KNOWLEDGE_ONLY_DIMENSIONS = ("knowledge", "conditions", "requirements",
                            "evidence", "comparison")


def build_retrieval_services(settings: Settings | None = None) -> Any:
    """构造**能真正检索**的 pipeline 服务栈。

    为什么必须有这个函数：写作台此前直接用裸 ``Services()``（三个模型都是 None），
    或干脆没配 collector —— 两条路都会让"补检"变成空转：

    - 没配 collector → ``make_collection_node`` 返回 ``collection_skipped``，
      看着"流程走完了"，实际一篇都没抓（数据库里有 ``collection_skipped`` 记录）；
    - 裸 ``Services()`` → 检索能跑，但质量评估与知识抽取没有模型，
      抓回来的文献落不了库、更成不了可引用证据。

    照上游 CLI 的做法注入 ApiHub + retriever/quality/knowledge 三个模型。
    """
    from research_agent.pipeline import Services as PipelineServices
    from research_agent.retrieval.api_clients import ApiHub

    s = settings or default_settings
    services = PipelineServices(api=ApiHub(), settings=s)
    for attr, role in (("retriever_model", "retriever"),
                       ("quality_model", "quality"),
                       ("knowledge_model", "knowledge")):
        try:
            from research_agent.models import build_role_model
            setattr(services, attr, build_role_model(role))
        except Exception as exc:  # noqa: BLE001 —— 缺 Key 是预期情况
            logger.warning("检索链路角色 %s 未绑定模型: %s", role, exc)
    return services


def build_retrieval_collector(settings: Settings | None = None) -> Any:
    """给 ``StudyServices.collector`` 用的采集器（**惰性构建**）。

    惰性很重要：构建 ApiHub 与三个角色模型并不便宜，而绝大多数写作步骤
    根本走不到采集那一步。若在每次 run_section_workflow 里立刻建好，
    连"判定充足、直接成段"的正常路径也要白付这份成本——
    实测这样会让冒烟里的写作步骤从秒级变成几十秒。
    """
    from research_agent.study.collection import collect_mission

    state: dict[str, Any] = {}

    def collector(request: dict[str, Any]) -> dict[str, Any]:
        if "services" not in state:
            state["services"] = build_retrieval_services(settings)
        return collect_mission(request, services=state["services"])

    return collector


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

    # 自定义任务：取用户选定的执行方案；否则就是"补检索"。
    plan = _selected_custom_plan(section_state)
    if plan:
        tasks = list(plan.get("tasks") or ["retrieve"])
        extract_scope = str(plan.get("extract_scope") or "new")
    else:
        # **普通「补检」必须连抽取一起做**。
        #
        # 判定的六个维度里只有 papers 读检索产物；knowledge / conditions /
        # requirements / evidence / comparison 全读抽取产物。只抓不抽 ⇒
        # 新文献进不了知识库 ⇒ 判定结论不变 ⇒ 用户反复点补检也永远"卡在"同一处
        # （实测：抓回 116 篇、抽取 0 篇，三个硬闸门一个都没动）。
        tasks = ["retrieve", "extract_knowledge"]
        extract_scope = _preferred_extract_scope(conn, section_state)

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
    extract_note = ""

    if "retrieve" in tasks:
        added_total, rounds_done, steps, exhausted = _do_retrieve(
            conn, section_key=section_key, queries=queries,
            rounds_cap=rounds_cap, settings=s, progress_cb=progress_cb)

    if "extract_knowledge" in tasks:
        extracted_total, extract_steps = _do_extract(
            conn, scope=extract_scope, project=project, section_key=section_key,
            added_keys=_added_keys(steps), settings=s, progress_cb=progress_cb)
        steps.extend(extract_steps)
        for item in extract_steps:
            if item.get("note"):
                extract_note = str(item["note"])

    summary_bits = [f"检索 {rounds_done} 轮", f"新增 {added_total} 篇"]
    if "extract_knowledge" in tasks:
        # 抽取数**必须无条件下报**：为 0 时也要让用户看见，
        # 否则"补检了但没抽"这件事在界面上完全不可见（实测就是这样漏掉的）。
        summary_bits.append(f"抽取 {extracted_total} 篇")
        if extracted_total == 0 and extract_note:
            summary_bits.append(f"（{extract_note}）")
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
        # 实际用的抽取范围：普通补检时由 `_preferred_extract_scope` 决定，
        # 不再是空串（以前只有自定义任务才有值，日志里看不出来）。
        "extract_scope": extract_scope if "extract_knowledge" in tasks else "",
        "exhausted": exhausted,
        "summary": "、".join(summary_bits),
        "steps": steps,
    }


def _preferred_extract_scope(conn: sqlite3.Connection,
                             sec: iv.SectionPlanState) -> str:
    """普通「补检」该抽哪批文献。

    判据是**哪一批更可能补上缺的维度**：

    - 缺的维度里有 knowledge / conditions / requirements / evidence /
      comparison 这类"知识类"维度 → 库里**已经有**同主题未抽取的文献时优先抽它们
      （不花检索配额，而且往往正是瓶颈："文献有但没抽"）；
    - 否则（只缺 papers）→ 抽这次新抓回来的；
    - 两批都有 → ``both``，先抽新的再补既有。
    """
    unmet = set(sec.verdict.get("unmet_dimensions") or [])
    knowledge_gap = bool(unmet & set(KNOWLEDGE_ONLY_DIMENSIONS)) or not unmet
    pending_existing = _pending_existing_count(conn)
    if knowledge_gap and pending_existing:
        return "both" if not unmet or "papers" not in unmet else "existing"
    if knowledge_gap and not pending_existing:
        return "new"
    return "new"


def _pending_existing_count(conn: sqlite3.Connection) -> int:
    """库内已入库、但还没有抽取记录的文献数（判断"抽既有"值不值得）。"""
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM papers p WHERE p.status='ingested' "
            "AND NOT EXISTS (SELECT 1 FROM processing_log l "
            "                WHERE l.paper_key = p.paper_key "
            "                  AND l.event = 'extracted')").fetchone()
    except sqlite3.Error:
        return 0
    return int(row["c"] or 0) if row else 0


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

    terms = [str(q) for q in queries if str(q).strip()][:6]
    if not terms:
        terms = ["research"]     # 兜底，避免空检索词
    request = {"seed_terms": terms, "reason": f"访谈协作：{section_key}"}
    # **必须用注入模型的 services**：裸 Services() 会让质量评估与知识抽取没有模型，
    # 抓回来的文献成不了可引用证据（实测：裸 Services 能抓到 3 篇，但只到"入库"）
    services = build_retrieval_services(settings)
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
    - ``scope == "existing"``：抽库内**同主题**的既有文献（常见瓶颈是"有文献但没抽"）；
    - ``scope == "both"``：先抽新增的，再补既有的。

    两个上限都来自配置：``section_extract_max_papers``（篇数）与
    ``section_extract_max_seconds``（时长）。**上限不能太小**：抽取是逐个 LLM
    调用，而上限过小会让"补检"永远推不动知识覆盖率——实测原先是 20 篇，
    而把覆盖率从 30% 提到 60% 需要约 120 篇，于是用户每点一次补检都"没有变化"。
    """
    from research_agent.pipeline import process_papers

    topic = str(project.get("topic") or project.get("title") or "")
    limit = max(1, int(getattr(settings, "section_extract_max_papers", 60) or 60))
    budget = max(30, int(getattr(settings, "section_extract_max_seconds", 900)
                         or 900))

    targets: list[str] = []
    if scope in ("new", "both"):
        targets.extend(str(k) for k in added_keys)
    if scope in ("existing", "both"):
        # 既有那一批按"同主题且未抽取"取，留出与新增不重复的额度
        targets.extend(_existing_same_topic_keys(conn, topic, limit=limit))
    # 去重且保序（新增优先），再截断到上限
    seen: set[str] = set()
    unique: list[str] = []
    for key in targets:
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    targets = unique[:limit]

    if not targets:
        return 0, [{"task": "extract_knowledge", "scope": scope, "ok": True,
                    "count": 0, "note": "没有可抽取的目标文献"}]

    _progress(progress_cb, 70.0, f"正在抽取 {len(targets)} 篇文献的知识…")
    services = build_retrieval_services(settings)
    started = time.monotonic()
    ok = 0
    failed = 0
    stopped_by = ""
    for index, key in enumerate(targets, 1):
        if time.monotonic() - started > budget:
            stopped_by = f"已达抽取时长上限（{budget}s），已抽 {ok} 篇"
            break
        _progress(progress_cb, 70.0 + 20.0 * index / max(1, len(targets)),
                  f"抽取 {index}/{len(targets)}：{key}")
        try:
            results = process_papers([key], services=services, conn=conn)
        except Exception as exc:  # noqa: BLE001
            logger.warning("抽取失败 %s: %s", key, exc)
            failed += 1
            continue
        status = str((results[0] if results else {}).get("status") or "")
        if status in ("extracted", "knowledge"):
            ok += 1
        else:
            failed += 1

    step: dict[str, Any] = {"task": "extract_knowledge", "scope": scope,
                            "ok": True, "count": ok, "attempted": len(targets),
                            "failed": failed, "targets": targets[:50],
                            "seconds": round(time.monotonic() - started, 1)}
    if stopped_by:
        step["note"] = stopped_by
    elif ok == 0:
        # 一篇都没成，必须说清原因——最可能是知识模型没配/没 Key，
        # 而不是"库里没东西可抽"。沉默会让用户以为补检生效了。
        step["note"] = ("抽取未成功：知识提炼模型不可用或全部失败"
                        if failed else "没有可抽取的目标文献")
    return ok, [step]


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
