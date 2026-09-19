"""节点写作服务：启动作业、跑链路、逐轮落库决策轨迹。

一次「在大纲节点上输入指令」会经历：

1. ``planner_section`` —— 复用研究 Planner，但只规划**这一节**的检索；
2. ``sufficiency`` —— **检索前**判定现有库够不够写，并落一行轨迹（第 1 轮）；
3. 不足 → ``collection`` 补检扩库 → 回到第 2 步复审（第 2、3 轮…）；
4. 足够 → ``knowledge_consumer`` 消费知识 → ``section_compose`` 成段；
5. 预算用尽仍不足 → 结束于 ``needs_data``，**不生成正文**。

轨迹落在 ``section_runs``（每轮一行），正文落 ``writing_sections``，
两者用 ``run_id`` 关联，因此界面能回答"这段正文是哪一轮判定支撑的"。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from typing import Any

from research_agent.config import Settings, settings as default_settings
from research_agent.db import connect, utcnow
from research_agent.library.jobs import LibraryJobManager
from research_agent.study.graph import StudyServices
from research_agent.writing.section_graph import build_section_graph
from research_agent.writing.section_plan import (
    build_work_plan, section_directive)
from research_agent.writing.service import (
    default_model, get_project, list_sections, save_section)

logger = logging.getLogger(__name__)

__all__ = [
    "ManagerForSections",
    "start_section_run",
    "run_section_workflow",
    "section_job_status",
    "cancel_section_run",
    "plan_section",
    "build_project_plan",
    "start_interview_step",
    "get_section_trace",
    "latest_section_state",
    "SECTION_TERMINAL",
]

#: 作业终态
SECTION_TERMINAL = {"done", "error", "cancelled"}

#: 作业名（进度/审计用）
ACTION = "section-write"

_MANAGER: LibraryJobManager | None = None
_MANAGER_LOCK = threading.Lock()


def ManagerForSections(db_path: str | None = None) -> LibraryJobManager:
    """进程级作业管理器（写作用；测试请自行构造独立实例）。"""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None or _MANAGER.db_path != db_path:
            _MANAGER = LibraryJobManager(db_path)
        return _MANAGER


# ------------------------------------------------------------------ 轨迹落库

def _save_round(conn: sqlite3.Connection, *, run_id: str, round_no: int,
                project_id: int, section_key: str, instruction: str,
                stage: str, verdict: dict[str, Any] | None = None,
                plan: dict[str, Any] | None = None,
                collection: dict[str, Any] | None = None,
                content_chars: int = 0, generated_by: str = "",
                error: str = "", template_key: str = "",
                field_source: dict[str, Any] | None = None,
                unmet: list[str] | None = None) -> None:
    """写入/更新一轮轨迹（``(run_id, round)`` 为主键）。"""
    verdict = verdict or {}
    plan = plan or {}
    field_json = (json.dumps(field_source, ensure_ascii=False)
                  if field_source else None)
    unmet_json = (json.dumps(list(unmet or []), ensure_ascii=False)
                  if unmet else None)
    row = conn.execute(
        "SELECT 1 FROM section_runs WHERE run_id=? AND round=?",
        (run_id, int(round_no)),
    ).fetchone()
    payload = (
        stage, str(verdict.get("decision") or ""),
        json.dumps(plan, ensure_ascii=False) if plan else None,
        json.dumps(verdict, ensure_ascii=False) if verdict else None,
        json.dumps(collection, ensure_ascii=False) if collection else None,
        int(content_chars), str(generated_by or ""), str(error or ""),
        str(template_key or "") or None, field_json, unmet_json,
    )
    if row:
        conn.execute(
            "UPDATE section_runs SET stage=?, decision=?, plan_json=?, "
            "sufficiency_json=?, collection_json=?, content_chars=?, "
            "generated_by=?, error=?, template_key=?, field_source_json=?, "
            "unmet_json=?, instruction=?, ts=? "
            "WHERE run_id=? AND round=?",
            (*payload, instruction, utcnow(), run_id, int(round_no)),
        )
    else:
        conn.execute(
            "INSERT INTO section_runs(run_id, round, project_id, section_key, "
            "instruction, stage, decision, plan_json, sufficiency_json, "
            "collection_json, content_chars, generated_by, error, "
            "template_key, field_source_json, unmet_json, ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, int(round_no), int(project_id), str(section_key),
             instruction, *payload, utcnow()),
        )
    conn.commit()


def _update_section_grounding(conn: sqlite3.Connection, *, project_id: int,
                              section_key: str, run_id: str,
                              grounded_on: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE writing_sections SET grounded_on=?, last_run_id=?, status=?, "
        "updated_at=? WHERE project_id=? AND section_key=?",
        (json.dumps(grounded_on, ensure_ascii=False), run_id,
         str(grounded_on.get("status") or "draft"), utcnow(),
         int(project_id), str(section_key)),
    )
    conn.commit()


def _load_work_plan(conn: sqlite3.Connection,
                    project_id: int) -> dict[str, Any]:
    """读取项目已保存的工作规划（唯一规划节点的产物）。"""
    try:
        row = conn.execute(
            "SELECT plan_json FROM writing_projects WHERE project_id=?",
            (int(project_id),)).fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    raw = row["plan_json"] if "plan_json" in row.keys() else None
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _study_services_with_collector(settings: Settings) -> StudyServices:
    """构造带采集器的 StudyServices（补检才能真正抓文献）。

    不接采集器的后果（实测）：collection 节点走 ``collection_skipped`` 分支，
    返回 ``{"count": 0, "reason": "未配置 collector，使用本地已有知识"}``，
    判定永远是"不足"，用户点多少次"补检"都不会有变化。
    """
    from research_agent.writing.collaboration import build_retrieval_collector
    try:
        collector = build_retrieval_collector(settings)
    except Exception as exc:  # noqa: BLE001 —— 检索栈构建失败不该让写作彻底不可用
        logger.warning("采集器构建失败，补检将不可用: %s", exc)
        collector = None
    base = StudyServices.from_env(require_llms=False)
    base.collector = collector
    return base


# ------------------------------------------------------------------ 作业体

def run_section_workflow(
    *,
    project_id: int,
    section_key: str,
    instruction: str = "",
    user_fields: dict[str, Any] | None = None,
    db_path: str | None = None,
    settings: Settings | None = None,
    services: StudyServices | None = None,
    planner_model: Any = None,
    consumer_model: Any = None,
    compose_model: Any = None,
    compose_model_reason: str = "",
    progress_cb: Any = None,
    cancel_event: threading.Event | None = None,
    skip_judgement: bool = False,
    use_model: bool = True,
) -> dict[str, Any]:
    """作业体：跑一次部分写作链并落库。可被 ``LibraryJobManager.start`` 驱动。

    ``instruction`` 与 ``user_fields`` 都是**可选**：
    两者都空时走"系统自拟"——由工作规划节点与部分模板决定这一部分写什么。

    ``skip_judgement=True``：调用方（访谈闭环）**已经判过支撑**，只需写作。
    开启后会在采集之外再跳过判定节点，避免"补检+复判"跑第二遍——
    判定做两遍不仅重复，第二遍还会因为 collector 已接上而真的发起检索，
    把一个本该几十秒的写作步骤拖到数分钟（实测浏览器冒烟 240s 超时）。

    ``use_model=False``（离线/回归）：**不构建任何模型**。此前只挡住了成段模型，
    规划与消费节点仍会各自自动构建真模型，于是"离线"冒烟照样联网、
    慢到超时（实测 outcome=running 卡住）。
    """
    s = settings or default_settings
    path = db_path if db_path is not None else s.db_path
    run_id = uuid.uuid4().hex[:12]
    user_fields = dict(user_fields or {})

    def _progress(percent: float, message: str = "") -> None:
        if progress_cb is not None:
            try:
                progress_cb(percent, message)
            except Exception:  # noqa: BLE001
                pass

    db = connect(path)
    try:
        project = get_project(db, project_id)
        if not project:
            raise ValueError(f"写作项目不存在: {project_id}")
        section = next(
            (x for x in list_sections(db, project_id)
             if str(x.get("section_key")) == str(section_key)), None)
        if not section:
            raise ValueError(f"大纲节点不存在: {section_key}")
        heading = str(section.get("heading") or section_key)
        words = int(section.get("words") or 0)
        genre = str(project.get("genre") or "")
        # 部分模板：这一部分写什么、考哪几个维度、有哪些可填字段。
        # **没挂模板不报错**：体裁可能还没配模板（如 research_article），
        # 此时退回"无模板"路径——判定用体裁默认权重，提示词只带主题与指令。
        from research_agent.writing import section_template as stpl
        template_key, _tpl = stpl.template_for_section(genre, section_key)

        instruction = str(instruction or "").strip()
        # 指令**可选**：没给就走"系统自拟"（规划节点/模板默认），不再报错
        section_heading_note = str(
            (stpl.genre_definition(genre)[1].get("section_notes") or {})
            .get(section_key) or "")

        # 工作规划：优先用项目里已存的，没有就现在生成一份（auto 模式）
        work_plan = _load_work_plan(db, project_id)
        if not work_plan:
            work_plan = build_work_plan(
                db, project_id=int(project_id),
                topic=str(project.get("topic") or project.get("title") or ""),
                instruction=instruction, settings=s,
                planner_model=planner_model, persist=True)
        directive = section_directive(work_plan, section_key)
        focus = list(directive.get("focus") or _tpl.get("focus") or [])
        role = str(directive.get("role") or _tpl.get("role") or "")
        fields = stpl.resolve_fields(genre, section_key,
                                     user_values=user_fields,
                                     plan_values=directive.get("plan_fields"))
        fields_block = stpl.field_block(genre, section_key, fields,
                                        role=role, focus=focus,
                                        evidence_types=list(
                                            directive.get("evidence_types")
                                            or _tpl.get("evidence_types") or []))
        field_sources = stpl.field_source_summary(fields)

        from research_agent.writing.section_graph import build_section_request
        planning_request = build_section_request(project, heading, instruction,
                                                 section_heading_note)

        # **必须接上采集器**：StudyServices 默认 collector=None 时，
        # make_collection_node 直接返回 collection_skipped——判定说"不足"、
        # 用户选"补检"，结果一篇都没抓，链路原地打转
        # （数据库里有 collection_skipped 记录为证）。写作台此前漏了这一步。
        # 但访谈闭环已判过支撑时（skip_judgement）不该再补检，故此时不接采集器。
        services = services or (
            StudyServices.from_env(require_llms=False) if skip_judgement
            else _study_services_with_collector(s))

        if not use_model:
            # 离线：把三个角色模型显式清空，避免节点内部再各自自动构建
            services.planner_model = None
            services.consumer_model = None
            services.collector = None
            planner_model = None
            consumer_model = None
            compose_model = None
            compose_model_reason = compose_model_reason or "调用方显式要求不使用模型（离线）"

        if compose_model is None and not compose_model_reason:
            compose_model, compose_model_reason = default_model(s)

        rounds_written: set[int] = set()

        def on_round(state: dict[str, Any]) -> None:
            """每一轮充分性判定后落一行轨迹（含补检前的判定）。

            轮次编号用 ``sufficiency_rounds``（第几次判定），而不是补检次数：
            一次完整运行是 "初判(1) → 补检 → 复审(2) → 补检 → 终判(3)"。
            """
            verdict = state.get("sufficiency") or {}
            round_no = int(state.get("sufficiency_rounds") or 1)
            rounds_written.add(round_no)
            # 复审行才挂补检结果（初判之前没有补检）
            collection = (state.get("collection_report") or None
                          if int(state.get("collection_rounds") or 0) else None)
            _save_round(
                db, run_id=run_id, round_no=round_no, project_id=project_id,
                section_key=section_key, instruction=instruction,
                stage="sufficiency", verdict=verdict,
                plan=state.get("plan") or {},
                collection=collection,
            )
            _progress(
                min(90.0, 30.0 + 20.0 * round_no),
                f"第 {round_no} 轮充分性判定：{verdict.get('decision')}")

        _save_round(db, run_id=run_id, round_no=0, project_id=project_id,
                    section_key=section_key, instruction=instruction,
                    stage="planning")
        _progress(10.0, "正在做本节检索规划…")

        if cancel_event is not None and cancel_event.is_set():
            _save_round(db, run_id=run_id, round_no=0, project_id=project_id,
                        section_key=section_key, instruction=instruction,
                        stage="cancelled", error="用户取消")
            return {"run_id": run_id, "status": "cancelled"}

        graph = build_section_graph(
            conn=db, settings=s, services=services,
            planner_model=planner_model, consumer_model=consumer_model,
            compose_model=compose_model,
            compose_model_reason=compose_model_reason,
            on_round=on_round,
            skip_judgement=skip_judgement,
        )
        initial: dict[str, Any] = {
            "run_id": run_id,
            "project_id": int(project_id),
            "section_key": str(section_key),
            "heading": heading,
            "instruction": instruction,
            "genre": genre,
            "focus": focus,
            "role": role,
            "fields": fields,
            "fields_block": fields_block,
            "template_key": template_key,
            "section_note": section_heading_note,
            "words": words,
            "topic": str(project.get("topic") or project.get("title") or ""),
            "planning_request": planning_request,
            "sufficiency_rounds": 0,
        }
        _progress(30.0, "正在判定当前库是否够写…")
        final = graph.invoke(initial)

        verdict = final.get("sufficiency") or {}
        decision = str(verdict.get("decision") or "")
        status = str(final.get("status") or decision or "error")
        composed = final.get("section") or {}
        # 带缺口写作：判定未通过但模板允许，且确实写出了正文
        unmet = list(verdict.get("unmet_dimensions") or [])
        evidence_types = list(verdict.get("evidence_types") or [])
        wrote_with_gaps = (status == "written" and bool(unmet)
                           and decision != "sufficient")

        if cancel_event is not None and cancel_event.is_set():
            _save_round(db, run_id=run_id, round_no=max(rounds_written or {0}),
                        project_id=project_id, section_key=section_key,
                        instruction=instruction, stage="cancelled",
                        verdict=verdict, error="用户取消")
            return {"run_id": run_id, "status": "cancelled",
                    "sufficiency": verdict}

        if status == "written" and composed.get("content"):
            content = str(composed["content"])
            save_section(db, project_id, section_key, heading, content,
                         list(composed.get("citations") or []))
            round_no = max(rounds_written or {0})
            # 带缺口写作时状态标 written_with_gaps：正文有内容，但轨迹里保留"未达标"
            section_status = "written_with_gaps" if wrote_with_gaps else "written"
            _save_round(db, run_id=run_id, round_no=round_no,
                        project_id=project_id, section_key=section_key,
                        instruction=instruction, stage="done", verdict=verdict,
                        plan=final.get("plan") or {}, content_chars=len(content),
                        generated_by=str(composed.get("generated_by") or ""),
                        template_key=template_key,
                        field_source=field_sources, unmet=unmet)
            _update_section_grounding(
                db, project_id=project_id, section_key=section_key, run_id=run_id,
                grounded_on={
                    "run_id": run_id,
                    "status": section_status,
                    "decision": decision,
                    "confidence": verdict.get("confidence"),
                    "paper_keys": verdict.get("paper_keys") or [],
                    "evidence_ids": verdict.get("evidence_ids") or [],
                    "bindings": composed.get("bindings") or [],
                    "invalid_indices": composed.get("invalid_indices") or [],
                    "generated_by": composed.get("generated_by"),
                    "material_count": composed.get("material_count"),
                    "rounds": round_no,
                    "template_key": template_key,
                    "field_sources": field_sources,
                    "unmet_dimensions": unmet,
                    "evidence_types": verdict.get("evidence_types") or [],
                    "gap_notice": composed.get("gap_notice") or "",
                    "work_plan_mode": str(work_plan.get("mode") or ""),
                })
            if composed.get("invalid_indices"):
                _progress(100.0, "完成（但正文含越界引文，请检查）")
            elif wrote_with_gaps:
                _progress(100.0, "完成（证据有缺口，已在正文顶部标注）")
            else:
                _progress(100.0, "完成")
            return {
                "run_id": run_id,
                "status": section_status,
                "decision": decision,
                "confidence": verdict.get("confidence"),
                "rounds": round_no,
                "generated_by": composed.get("generated_by"),
                "model_error": composed.get("model_error") or "",
                "invalid_indices": composed.get("invalid_indices") or [],
                "unmet_dimensions": unmet,
                "evidence_types": verdict.get("evidence_types") or [],
                "template_key": template_key,
                "field_sources": field_sources,
                "work_plan_mode": str(work_plan.get("mode") or ""),
                "sufficiency": verdict,
                "section_key": section_key,
                "heading": heading,
            }

        if status == "needs_data" or decision in ("insufficient", "exhausted"):
            round_no = max(rounds_written or {0})
            _save_round(db, run_id=run_id, round_no=round_no,
                        project_id=project_id, section_key=section_key,
                        instruction=instruction, stage="needs_data",
                        verdict=verdict, plan=final.get("plan") or {},
                        error="证据不足，未生成正文")
            _update_section_grounding(
                db, project_id=project_id, section_key=section_key, run_id=run_id,
                grounded_on={
                    "run_id": run_id,
                    "status": "needs_data",
                    "decision": decision or "exhausted",
                    "confidence": verdict.get("confidence"),
                    "missing": (verdict.get("counts") or {}).get(
                        "requirements_missing") or [],
                    "suggested_queries": verdict.get("suggested_queries") or [],
                    "paper_keys": verdict.get("paper_keys") or [],
                    "rounds": round_no,
                })
            _progress(100.0, "证据不足：已记录缺口，未生成正文")
            return {
                "run_id": run_id,
                "status": "needs_data",
                "decision": decision or "exhausted",
                "confidence": verdict.get("confidence"),
                "rounds": round_no,
                "missing": (verdict.get("counts") or {}).get(
                    "requirements_missing") or [],
                "suggested_queries": verdict.get("suggested_queries") or [],
                "reasons": verdict.get("reasons") or [],
                "sufficiency": verdict,
                "section_key": section_key,
                "heading": heading,
            }

        error = str(final.get("error") or "链路未产出正文")
        _save_round(db, run_id=run_id, round_no=max(rounds_written or {0}),
                    project_id=project_id, section_key=section_key,
                    instruction=instruction, stage="failed", verdict=verdict,
                    plan=final.get("plan") or {}, error=error)
        _update_section_grounding(
            db, project_id=project_id, section_key=section_key, run_id=run_id,
            grounded_on={"run_id": run_id, "status": "failed", "error": error,
                         "decision": decision})
        return {"run_id": run_id, "status": "failed", "error": error,
                "sufficiency": verdict}
    finally:
        db.close()


def start_section_run(
    *,
    project_id: int,
    section_key: str,
    instruction: str = "",
    user_fields: dict[str, Any] | None = None,
    db_path: str | None = None,
    settings: Settings | None = None,
    services: StudyServices | None = None,
    planner_model: Any = None,
    consumer_model: Any = None,
    compose_model: Any = None,
    compose_model_reason: str = "",
    use_model: bool = True,
) -> str:
    """启动一次部分写作作业，立即返回 ``job_id``。"""
    manager = ManagerForSections(db_path)
    return manager.start(
        ACTION,
        run_section_workflow,
        project_id=int(project_id),
        section_key=str(section_key),
        instruction=str(instruction or ""),
        user_fields=dict(user_fields or {}),
        db_path=db_path,
        settings=settings,
        services=services,
        planner_model=planner_model,
        consumer_model=consumer_model,
        compose_model=compose_model,
        compose_model_reason=compose_model_reason,
        use_model=use_model,
        total=1,
    )


def section_job_status(job_id: str, db_path: str | None = None) -> dict[str, Any] | None:
    return ManagerForSections(db_path).status(job_id)


def cancel_section_run(job_id: str, db_path: str | None = None) -> bool:
    return ManagerForSections(db_path).cancel(job_id)


# ------------------------------------------------------------------ 工作规划

def start_interview_step(
    *,
    project_id: int,
    db_path: str | None = None,
    settings: Settings | None = None,
    planner_model: Any = None,
    gap_model: Any = None,
    compose_model: Any = None,
    compose_model_reason: str = "",
    use_model: bool = True,
    trace: str = "",
) -> str:
    """起一个"推进访谈一步"的作业，返回 ``job_id``。

    一步只做一件事（拟方案 / 判定 / 协作 / 写作），因此可以短轮询；
    复用 `LibraryJobManager`，超时、取消、进度都是现成的。
    """
    from research_agent.writing.interview_loop import run_step

    manager = ManagerForSections(db_path)
    return manager.start(
        "interview-step",
        run_step,
        project_id=int(project_id),
        db_path=db_path,
        settings=settings,
        planner_model=planner_model,
        gap_model=gap_model,
        compose_model=compose_model,
        compose_model_reason=compose_model_reason,
        use_model=use_model,
        trace=trace,
        total=1,
    )


def build_project_plan(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    topic: str = "",
    instruction: str = "",
    settings: Settings | None = None,
    planner_model: Any = None,
    persist: bool = True,
) -> dict[str, Any]:
    """生成整篇工作规划（唯一规划节点的对外入口）。

    ``instruction`` 为空 → ``mode="auto"``，系统依主题自拟；
    非空 → ``mode="user"``，用户指令优先，规划不覆盖。
    """
    s = settings or default_settings
    return build_work_plan(
        conn, project_id=int(project_id), topic=topic,
        instruction=instruction, settings=s, planner_model=planner_model,
        persist=persist)


# ------------------------------------------------------------------ 只规划

def plan_section(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    section_key: str,
    instruction: str = "",
    user_fields: dict[str, Any] | None = None,
    settings: Settings | None = None,
    planner_model: Any = None,
    evaluate: bool = True,
) -> dict[str, Any]:
    """只做「检索规划 + 充分性判定」，**不检索、不生成**。（"预判本节够不够写"）

    指令与字段都是可选的：都没给时按工作规划与模板默认判定——
    这正是"系统自拟"模式下的预判。
    """
    s = settings or default_settings
    project = get_project(conn, project_id)
    if not project:
        return {"ok": False, "error": f"写作项目不存在: {project_id}"}
    section = next(
        (x for x in list_sections(conn, project_id)
         if str(x.get("section_key")) == str(section_key)), None)
    if not section:
        return {"ok": False, "error": f"大纲节点不存在: {section_key}"}
    heading = str(section.get("heading") or section_key)
    instruction = str(instruction or "").strip()
    genre = str(project.get("genre") or "")

    from research_agent.writing import section_template as stpl
    template_key, tpl = stpl.template_for_section(genre, section_key)
    section_note = str(
        (stpl.genre_definition(genre)[1].get("section_notes") or {})
        .get(section_key) or "")

    work_plan = _load_work_plan(conn, project_id)
    if not work_plan:
        work_plan = build_work_plan(
            conn, project_id=int(project_id),
            topic=str(project.get("topic") or project.get("title") or ""),
            instruction=instruction, settings=s, planner_model=planner_model,
            persist=True)
    directive = section_directive(work_plan, section_key)
    focus = list(directive.get("focus") or tpl.get("focus") or [])
    role = str(directive.get("role") or tpl.get("role") or "")
    fields = stpl.resolve_fields(genre, section_key,
                                 user_values=user_fields,
                                 plan_values=directive.get("plan_fields"))
    fields_block = stpl.field_block(
        genre, section_key, fields, role=role, focus=focus,
        evidence_types=list(directive.get("evidence_types")
                            or tpl.get("evidence_types") or []))

    from research_agent.study.planner import make_planner_node
    from research_agent.writing.section_graph import (
        build_section_request, normalize_fallback_plan)

    request = build_section_request(project, heading, instruction, section_note)
    node = make_planner_node(planner_model, conn=conn, settings=s)
    out = node({"request": request, "run_id": None})
    plan = out.get("plan") or {}
    if plan:
        plan = normalize_fallback_plan(plan, request)
    status = str(out.get("status") or "")

    result: dict[str, Any] = {
        "ok": status == "planned" and bool(plan),
        "project_id": int(project_id),
        "section_key": str(section_key),
        "heading": heading,
        "instruction": instruction,
        "request": request,
        "plan": plan,
        "planner_mode": plan.get("planner_mode") or "",
        "planner_model_error": plan.get("planner_model_error") or out.get("error") or "",
        "status": status,
        # 模板与双模信息：界面据此渲染"本节要什么"与字段来源
        "template_key": template_key,
        "role": role,
        "focus": focus,
        "focus_source": str(directive.get("focus_source") or ""),
        "fields": fields,
        "field_sources": stpl.field_source_summary(fields),
        "fields_block": fields_block,
        "required_dimensions": list(directive.get("required_dimensions")
                                    or tpl.get("required_dimensions") or []),
        "evidence_types": list(directive.get("evidence_types")
                               or tpl.get("evidence_types") or []),
        "work_plan_mode": str(work_plan.get("mode") or ""),
    }
    if not result["ok"]:
        result["error"] = str(out.get("error") or "规划未产出任务单")
        return result

    if evaluate:
        from research_agent.writing.sufficiency import evaluate_sufficiency
        # 预判阶段一律按"还没补检"判定，让用户看到**当前**库的真实状态
        result["sufficiency"] = evaluate_sufficiency(
            conn, plan=plan, instruction=instruction, heading=heading,
            settings=s, rounds_done=0, section_key=section_key, genre=genre,
            focus_terms=focus,
        )
    return result


# ------------------------------------------------------------------ 轨迹查询

def get_section_trace(conn: sqlite3.Connection, project_id: int,
                      section_key: str, limit: int = 20) -> dict[str, Any]:
    """取某节点的决策轨迹：逐轮判定 + 当前落库状态。

    界面用它渲染"为什么这么写"：每轮的维度分数、缺失要求、建议补检词。
    """
    rows = conn.execute(
        "SELECT * FROM section_runs WHERE project_id=? AND section_key=? "
        "ORDER BY round ASC, ts ASC LIMIT ?",
        (int(project_id), str(section_key), max(1, int(limit))),
    ).fetchall()
    rounds: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for src, dst in (("plan_json", "plan"), ("sufficiency_json", "sufficiency"),
                         ("collection_json", "collection")):
            raw = item.pop(src, None)
            try:
                item[dst] = json.loads(raw) if raw else None
            except (json.JSONDecodeError, TypeError):
                item[dst] = None
        rounds.append(item)

    section_row = conn.execute(
        "SELECT * FROM writing_sections WHERE project_id=? AND section_key=?",
        (int(project_id), str(section_key)),
    ).fetchone()
    section: dict[str, Any] | None = None
    if section_row:
        section = dict(section_row)
        try:
            section["citation_ids"] = json.loads(
                section.get("citation_ids") or "[]")
        except (json.JSONDecodeError, TypeError):
            section["citation_ids"] = []
        try:
            section["grounded_on"] = json.loads(section.get("grounded_on") or "{}")
        except (json.JSONDecodeError, TypeError):
            section["grounded_on"] = {}

    latest = rounds[-1] if rounds else None
    return {
        "project_id": int(project_id),
        "section_key": str(section_key),
        "rounds": rounds,
        "round_count": len(rounds),
        "latest": latest,
        "section": section,
        "status": (section or {}).get("status") or "draft",
        "last_run_id": (section or {}).get("last_run_id"),
    }


def latest_section_state(conn: sqlite3.Connection,
                         project_id: int) -> dict[str, dict[str, Any]]:
    """按 ``section_key`` 取每个节点的落库状态（列表页徽标用，避免 N+1 查询）。"""
    rows = conn.execute(
        "SELECT section_key, status, grounded_on, last_run_id, citation_ids, "
        "       LENGTH(TRIM(COALESCE(content,''))) AS content_len "
        "FROM writing_sections WHERE project_id=?",
        (int(project_id),),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        try:
            item["grounded_on"] = json.loads(item.get("grounded_on") or "{}")
        except (json.JSONDecodeError, TypeError):
            item["grounded_on"] = {}
        try:
            item["citation_ids"] = json.loads(item.get("citation_ids") or "[]")
        except (json.JSONDecodeError, TypeError):
            item["citation_ids"] = []
        out[str(item.get("section_key"))] = item
    return out
