"""逐部分闭环驱动器：一次推进访谈状态机的一步。

方案 v6 的核心循环：

    访谈这部分 → 判定支撑 → 用户决定 →（必要时协作补齐）→ 写作这部分 → 下一部分

**职责边界**：

- 本模块负责**规划与协作的组织**（拟方案、判定、发起补检/抽取并回报）；
- **写作**由知识消费节点与内容形成节点完成（`section_graph` + `section_compose`），
  本模块只调用它们，不自己写正文。

**形态**：每一步是短作业——`run_step()` 读状态 → 做一件耗时的事 → 写回状态。
界面按阶段轮询，不需要长连接。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any

from research_agent.config import Settings, settings as default_settings
from research_agent.db import connect
from research_agent.logging import bind_trace, log_event
from research_agent.writing import gap_planner
from research_agent.writing import interview as iv
from research_agent.writing.section_judge import judge_section, plan_section_request
from research_agent.writing.service import (
    default_model, default_role_model, get_project, list_sections)

logger = logging.getLogger(__name__)

__all__ = ["run_step", "next_action", "ACTION_STAGES"]

#: 需要起作业的阶段（界面据此决定"回答完要不要轮询"）
ACTION_STAGES = {
    iv.STAGE_DRAFTING: "draft_options",
    iv.STAGE_JUDGING: "judge",
    iv.STAGE_COLLABORATING: "collaborate",
    iv.STAGE_WRITING: "write",
}


def next_action(state: iv.InterviewState) -> str:
    """当前状态下该做哪件事（不需要作业时返回空串）。"""
    if not state.intake_done() or state.all_done():
        return ""
    key = state.current_section_key()
    if not key:
        return ""
    stage = state.section(key).stage
    return ACTION_STAGES.get(stage, "")


# ------------------------------------------------------------------ 一步执行

def run_step(
    *,
    project_id: int,
    db_path: str | None = None,
    settings: Settings | None = None,
    planner_model: Any = None,
    gap_model: Any = None,
    compose_model: Any = None,
    compose_model_reason: str = "",
    use_model: bool = True,
    progress_cb: Any = None,
    cancel_event: threading.Event | None = None,
    trace: str = "",
) -> dict[str, Any]:
    """按当前阶段执行一步；返回 ``{"action": …, "stage": …, "snapshot": …}``。

    ``planner_model`` 用于检索规划；``gap_model`` 用于拟 3 案 / 轮数建议 /
    自定义任务解析（可给轻量模型）；``compose_model`` 用于成段。

    ``use_model=False`` = **本次不注入任何模型**（离线/回归）：
    拟方案走通用方向兜底、规划走确定性任务单、成段走骨架降级。
    它与"模型对象为 None"不是一回事——后者在生产语义下应尝试自动构建模型，
    早期把两者混为一谈，导致离线模式照样发起真实调用、作业卡死。
    """
    s = settings or default_settings
    path = db_path if db_path is not None else s.db_path
    db = connect(path)
    try:
        state = iv.load_state(db, project_id)
        action = next_action(state)
        key = state.current_section_key()
        sec = state.section(key) if key else None
        _trace = trace or None

        if not action:
            log_event("interview.step", node="interview", trace=_trace or "",
                      section=key or "", data={
                          "action": "", "stage": sec.stage if sec else "",
                          "outcome": "no_action",
                          "all_done": state.all_done(),
                          "intake_done": state.intake_done(),
                      })
            return {"ok": True, "action": "", "stage": "",
                    "snapshot": iv.snapshot(db, project_id)}

        project = get_project(db, project_id) or {}
        heading = _heading(db, project_id, key)

        if cancel_event is not None and cancel_event.is_set():
            log_event("interview.step", node="interview", trace=_trace or "",
                      section=key, data={"action": action, "stage": sec.stage,
                                         "outcome": "cancelled"})
            return {"ok": True, "action": action, "stage": sec.stage,
                    "cancelled": True, "snapshot": iv.snapshot(db, project_id)}

        log_event("interview.step", node="interview", trace=_trace or "",
                  section=key, data={"action": action,
                                     "stage": sec.stage, "phase": "start"})
        started = time.monotonic()
        _ctx = bind_trace(_trace) if _trace else _null_ctx()
        with _ctx:
            if action == "draft_options":
                _do_draft_options(db, project_id, state, sec, project, heading,
                                  key, gap_model, progress_cb, use_model)
            elif action == "judge":
                _do_judge(db, project_id, state, sec, project, heading, key,
                          planner_model, s, progress_cb, use_model)
            elif action == "collaborate":
                _do_collaborate(db, project_id, state, sec, project, heading,
                                key, s, progress_cb)
            elif action == "write":
                _do_write(db, project_id, state, sec, project, heading, key,
                          planner_model, compose_model, compose_model_reason,
                          s, progress_cb, db_path=path, use_model=use_model)
            else:
                log_event("interview.step", node="interview",
                          trace=_trace or "", level="ERROR", section=key,
                          data={"action": action, "stage": sec.stage,
                                "outcome": "unknown_action"})
                return {"ok": False, "action": action, "stage": sec.stage,
                        "error": f"未知的动作类型: {action}",
                        "snapshot": iv.snapshot(db, project_id)}

        iv.save_state(db, project_id, state)
        elapsed = (time.monotonic() - started) * 1000
        log_event("interview.step", node="interview", trace=_trace or "",
                  section=key, ms=elapsed,
                  data={"action": action, "stage": sec.stage,
                        "next_stage": state.section(key).stage,
                        "outcome": "ok"})
        return {"ok": True, "action": action, "stage": sec.stage,
                "section_key": key,
                "snapshot": iv.snapshot(db, project_id)}
    except Exception as exc:  # noqa: BLE001 —— 记录现场后原样抛出
        log_event("interview.step", node="interview", level="ERROR",
                  trace=trace or "", data={"outcome": "failed",
                                           "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        db.close()


class _null_ctx:
    """无 trace 时的空上下文（省掉一次 contextvar 设置）。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> None:
        return None


def _progress(cb: Any, percent: float, message: str) -> None:
    if cb is None:
        return
    try:
        cb(percent, message)
    except Exception:  # noqa: BLE001
        pass


def _heading(conn: sqlite3.Connection, project_id: int, key: str) -> str:
    for section in list_sections(conn, project_id):
        if str(section.get("section_key")) == str(key):
            return str(section.get("heading") or key)
    return str(key)


def _tpl(conn: sqlite3.Connection, project_id: int,
         key: str) -> tuple[str, dict[str, Any]]:
    from research_agent.writing import section_template as stpl
    project = get_project(conn, project_id) or {}
    genre = str(project.get("genre") or "")
    return stpl.template_for_section(genre, key)


# ------------------------------------------------------------------ 各阶段

def _do_draft_options(db: sqlite3.Connection, project_id: int,
                      state: iv.InterviewState, sec: iv.SectionPlanState,
                      project: dict[str, Any], heading: str, key: str,
                      gap_model: Any, progress_cb: Any,
                      use_model: bool = True) -> None:
    """拟 3 个内容方案（摘要力度）。

    ``use_model=False`` → 不构建模型，直接走通用方向兜底（离线/回归专用）。
    否则模型对象为 None 时尝试自动构建。
    """
    _progress(progress_cb, 20.0, f"正在为「{heading}」拟 3 个方案…")
    tpl_name, tpl = _tpl(db, project_id, key)
    model = gap_model
    if model is None and use_model:
        model, _reason = default_model()
    result = gap_planner.draft_content_options(
        topic=str(state.intake.get("topic") or ""),
        heading=heading,
        role=str(tpl.get("role") or ""),
        evidence_types=list(tpl.get("evidence_types") or []),
        focus=list(tpl.get("focus") or []),
        model=model,
    )
    sec.options = list(result.get("options") or [])
    sec.stage = iv.STAGE_AWAITING_CHOICE
    state.say("ai", f"「{heading}」我拟了 3 个方向，你挑一个：")
    for opt in sec.options:
        state.say("ai", f"  方案{opt['id']}：{opt['summary']}")
    _progress(progress_cb, 100.0, "方案已就绪")


def _do_judge(db: sqlite3.Connection, project_id: int,
              state: iv.InterviewState, sec: iv.SectionPlanState,
              project: dict[str, Any], heading: str, key: str,
              planner_model: Any, settings: Settings, progress_cb: Any,
              use_model: bool = True) -> None:
    """判定支撑：本部分的要求（来自模板）在库里够不够。"""
    _progress(progress_cb, 30.0, f"正在判定「{heading}」的支撑是否足够…")
    genre = str(project.get("genre") or "")
    tpl_name, tpl = _tpl(db, project_id, key)
    focus = _focus_for(sec, tpl)
    if planner_model is None and use_model:
        planner_model, _r = default_model()
    planned = plan_section_request(
        db, project=project, heading=heading,
        instruction=sec.resolved_focus or sec.custom_text,
        section_note=_section_note(project, key),
        settings=settings, planner_model=planner_model)
    plan = planned.get("plan") or {}
    queries = (plan.get("retrieval_plan") or {}).get("query_variants") or []
    state.say("ai", "检索计划：" + ("、".join(str(q) for q in queries)[:160]
                                    or "（无）"))

    verdict = judge_section(
        db, plan=plan, section_key=key, genre=genre, heading=heading,
        instruction=sec.resolved_focus or sec.custom_text,
        focus=focus,
        # 传**累计**轮数：判定才知道预算是真的用完了还是刚开始。
        # 用单次报告的轮数会让"补检 → 仍不足 → 再补检"无限循环。
        rounds_done=int(sec.rounds_total
                        or sec.collaboration.get("rounds_done") or 0),
        settings=settings)
    sec.verdict = verdict
    decision = str(verdict.get("decision") or "")
    log_event("sufficiency.judge", node="interview", section=key, db=db,
              data={"decision": decision,
                    "headline": verdict.get("headline") or "",
                    "unmet": list(verdict.get("unmet_dimensions") or []),
                    "hard_gates": list(verdict.get("hard_gates") or []),
                    "cited": verdict.get("cited_count"),
                    "rounds_done": int(sec.collaboration.get("rounds_done") or 0),
                    "queries": len(queries)})
    if decision == "sufficient":
        sec.stage = iv.STAGE_WRITING
        state.say("ai", f"「{heading}」的支撑足够，开始撰写。")
    else:
        sec.stage = iv.STAGE_AWAITING_GAP
        unmet = verdict.get("unmet_dimensions") or []
        state.say("ai", f"「{heading}」的支撑不足（缺：{'、'.join(unmet) or '—'}）。"
                        "你想怎么处理？")
    _progress(progress_cb, 100.0, f"判定完成：{decision}")


def _do_collaborate(db: sqlite3.Connection, project_id: int,
                    state: iv.InterviewState, sec: iv.SectionPlanState,
                    project: dict[str, Any], heading: str, key: str,
                    settings: Settings, progress_cb: Any) -> None:
    """协作：按用户的决定执行补检 / 抽取，然后回到判定。

    **协作的组织与执行在这里**（规划节点的职责）；具体抓取由采集链完成。
    """
    from research_agent.writing.collaboration import run_collaboration

    _progress(progress_cb, 20.0, f"正在为「{heading}」补齐支撑…")
    before = int(sec.rounds_total or 0)
    report = run_collaboration(
        db, project_id=project_id, section_key=key,
        section_state=sec, project=project, settings=settings,
        progress_cb=progress_cb)
    sec.collaboration = report
    # 累计实际轮数：判定据此判断预算是否真的用尽（见 `_do_judge`）
    sec.rounds_total = before + int(report.get("rounds_done") or 0)
    sec.stage = iv.STAGE_JUDGING      # 补齐 → 复判
    state.say("ai", "协作完成：" + str(report.get("summary") or "—")
              + f"（累计补检 {sec.rounds_total} 轮）")
    _progress(progress_cb, 90.0, "补齐完成，正在复判…")


def _do_write(db: sqlite3.Connection, project_id: int,
              state: iv.InterviewState, sec: iv.SectionPlanState,
              project: dict[str, Any], heading: str, key: str,
              planner_model: Any, compose_model: Any,
              compose_model_reason: str, settings: Settings,
              progress_cb: Any, db_path: str | None = None,
              use_model: bool = True) -> None:
    """写作：交给既有执行链（知识消费节点 → 内容形成节点），本模块不写正文。"""
    from research_agent.writing.section_service import run_section_workflow

    _progress(progress_cb, 30.0, f"正在撰写「{heading}」…")
    consumer_model: Any = None
    if not use_model:
        # 离线/回归：**绝不构建模型**。早期只判了 compose_model is None，
        # 结果离线时仍然自动构建了真实模型，写作阶段照样发起调用、卡到超时
        # （实测浏览器冒烟等 240s）。use_model 必须能一路管到成段。
        compose_model_reason = compose_model_reason or "调用方显式要求不使用模型（离线）"
    else:
        if compose_model is None and not compose_model_reason:
            compose_model, compose_model_reason = default_model()
        # **知识消费节点的模型必须在这里给上**：接口上一直有 consumer_model，
        # 但写作台这条路径从不传（只有 dashboard/app.py 的研究链路传），于是消费
        # 节点永远走 offline_fallback、confidence=0.0 —— 机制状态与候选算子链
        # 退化成纯确定性兜底，成段时自然写不出机制层面的内容。
        # 缺该角色 Key 时优雅退回兜底，不报错、不中断写作。
        consumer_model, _consumer_reason = default_role_model("consumer")
        if consumer_model is None:
            logger.info("知识消费模型不可用（将走确定性兜底）: %s",
                        _consumer_reason)
    result = run_section_workflow(
        project_id=project_id, section_key=key,
        instruction=sec.resolved_focus or sec.custom_text,
        user_fields={},
        db_path=db_path,
        settings=settings,
        planner_model=planner_model,
        compose_model=compose_model,
        compose_model_reason=compose_model_reason,
        # 知识消费模型（缺 Key 时为 None → 消费节点走确定性兜底）
        consumer_model=consumer_model,
        progress_cb=progress_cb,
        # 访谈闭环已经判过支撑（sec.verdict）：让执行链只做消费+成段，
        # 不再重复判定、也不再发起第二遍补检（那会把写作拖到数分钟）
        skip_judgement=True,
    )
    status = str(result.get("status") or "")
    if status in ("written", "written_with_gaps"):
        row = db.execute(
            "SELECT LENGTH(TRIM(COALESCE(content,''))) AS n FROM writing_sections "
            "WHERE project_id=? AND section_key=?",
            (int(project_id), str(key))).fetchone()
        sec.content_chars = int(row["n"] or 0) if row else 0
        sec.stage = iv.STAGE_DONE
        state.say("ai", f"「{heading}」已写好（{status}，{sec.content_chars} 字符）。")
        state.cursor += 1
        nxt = state.current_section_key()
        if nxt:
            state.section(nxt).stage = iv.STAGE_DRAFTING
            state.say("ai", f"接下来是「{_heading(db, project_id, nxt)}」。")
        else:
            state.say("ai", "所有选中的部分都已完成。")
    else:
        sec.stage = iv.STAGE_FAILED
        sec.error = str(result.get("error") or status or "写作未产出正文")
        state.say("ai", f"「{heading}」写作失败：{sec.error}")
    _progress(progress_cb, 100.0, f"写作结束：{status}")


def _db_path(db: sqlite3.Connection) -> str:
    try:
        row = db.execute("PRAGMA database_list").fetchone()
        return str(row[2]) if row else ""
    except sqlite3.Error:
        return ""


def _section_note(project: dict[str, Any], key: str) -> str:
    from research_agent.writing import section_template as stpl
    genre = str(project.get("genre") or "")
    _k, spec = stpl.genre_definition(genre)
    notes = spec.get("section_notes") or {}
    return str(notes.get(key) or "") if isinstance(notes, dict) else ""


def _focus_for(sec: iv.SectionPlanState, tpl: dict[str, Any]) -> list[str]:
    """判定用的写作要点：用户的选择优先，其次模板 focus。"""
    out: list[str] = []
    if sec.resolved_focus:
        out.append(sec.resolved_focus)
    if sec.custom_text:
        out.append(sec.custom_text)
    for item in (tpl.get("focus") or []):
        text = str(item)
        if text and text not in out:
            out.append(text)
    return out
