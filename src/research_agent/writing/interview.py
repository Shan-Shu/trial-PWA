"""访谈状态机：写作台唯一的交互节点（工作规划节点）。

设计要点（方案 v6）：

- **只有一个交互块**：整篇通过一轮问答定下来，不再每个部分一套表单。
- **逐部分闭环**：一个部分走完"访谈 → 判定支撑 → 缺口决定 → 协作 → 写作"，
  再进入下一个部分；前一个部分的正文成为后一个部分的上下文。
- **职责边界**：本模块只管**规划与协作的组织**；写作由知识消费节点与内容形成节点完成
  （`section_graph` 里的执行链），本模块不写正文。
- **状态落库**：`writing_projects.interview_json`。访谈可能跨多次操作、刷新不能丢进度，
  且"为什么这么定"要可审计。

阶段流转（每个部分独立）：

    pending → drafting_options → awaiting_choice → judging
      ├─ sufficient ─────────────────────────────→ writing → done
      └─ insufficient → awaiting_gap_decision
             ├─ keep_gap ────────────────────────→ writing → done
             ├─ collect ──→ collaborating → judging（复判）
             └─ custom ──→ awaiting_custom_plan_choice
                              ├─ 选定方案 ─→ collaborating → judging（复判）
                              └─ 再明确一点 ─→ 重新解析出 3 案
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from research_agent.db import utcnow
from research_agent.writing import section_template as stpl
from research_agent.writing.service import get_project, list_sections

__all__ = [
    "InterviewState",
    "SectionPlanState",
    "load_state",
    "save_state",
    "start_interview",
    "current_question",
    "answer",
    "snapshot",
    "INTAKE_STEPS",
]

#: 前置三问（+ 选项目由界面负责，不进状态机）
INTAKE_STEPS = ["genre", "topic", "sections"]

#: 阶段常量（供界面与测试引用，避免散落字符串）
STAGE_PENDING = "pending"
STAGE_DRAFTING = "drafting_options"
STAGE_AWAITING_CHOICE = "awaiting_choice"
STAGE_JUDGING = "judging"
STAGE_AWAITING_GAP = "awaiting_gap_decision"
STAGE_AWAITING_CUSTOM = "awaiting_custom_plan_choice"
STAGE_COLLABORATING = "collaborating"
STAGE_WRITING = "writing"
STAGE_DONE = "done"
STAGE_FAILED = "failed_writable"

#: 缺口决定的三个出口
GAP_KEEP = "keep_gap"
GAP_COLLECT = "collect"
GAP_CUSTOM = "custom"

#: 补检轮数上限（用户可改）
DEFAULT_COLLECTION_ROUNDS = 2
MIN_COLLECTION_ROUNDS = 1
MAX_COLLECTION_ROUNDS = 5


# ------------------------------------------------------------------ 状态结构

class InterviewState:
    """整篇访谈的状态（可序列化进 interview_json）。"""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data if isinstance(data, dict) else {}
        self.intake: dict[str, Any] = {
            "genre": str(data.get("intake", {}).get("genre") or ""),
            "topic": str(data.get("intake", {}).get("topic") or ""),
            "sections": list(data.get("intake", {}).get("sections") or []),
        }
        self.intake_step: str = str(data.get("intake_step") or INTAKE_STEPS[0])
        self.cursor: int = int(data.get("cursor") or 0)
        raw_sections = data.get("sections") or {}
        self.sections: dict[str, SectionPlanState] = {
            str(k): SectionPlanState(str(k), v)
            for k, v in raw_sections.items() if isinstance(v, dict)
        }
        self.history: list[dict[str, str]] = [
            {"role": str(h.get("role") or ""), "text": str(h.get("text") or "")}
            for h in (data.get("history") or []) if isinstance(h, dict)
        ]
        self.run_id: str = str(data.get("run_id") or "")

    # ---------------------------------------------------------- 序列化
    def as_dict(self) -> dict[str, Any]:
        return {
            "intake": dict(self.intake),
            "intake_step": self.intake_step,
            "cursor": self.cursor,
            "sections": {k: v.as_dict() for k, v in self.sections.items()},
            "history": list(self.history),
            "run_id": self.run_id,
        }

    # ---------------------------------------------------------- 便捷访问
    @property
    def genre(self) -> str:
        return self.intake.get("genre") or ""

    @property
    def selected_sections(self) -> list[str]:
        return list(self.intake.get("sections") or [])

    def intake_done(self) -> bool:
        return (bool(self.intake.get("genre"))
                and bool(self.intake.get("topic"))
                and bool(self.selected_sections))

    def current_section_key(self) -> str:
        keys = self.selected_sections
        if 0 <= self.cursor < len(keys):
            return str(keys[self.cursor])
        return ""

    def section(self, key: str) -> "SectionPlanState":
        if key not in self.sections:
            self.sections[key] = SectionPlanState(key, None)
        return self.sections[key]

    def all_done(self) -> bool:
        keys = self.selected_sections
        return bool(keys) and self.cursor >= len(keys)

    def say(self, role: str, text: str) -> None:
        if text:
            self.history.append({"role": role, "text": str(text)})


class SectionPlanState:
    """单个部分的访谈与执行状态。"""

    def __init__(self, key: str, data: dict[str, Any] | None = None) -> None:
        data = data if isinstance(data, dict) else {}
        self.key = str(key)
        self.stage: str = str(data.get("stage") or STAGE_PENDING)
        #: AI 拟的 3 个内容方案（摘要力度）
        self.options: list[dict[str, Any]] = [
            {"id": str(o.get("id") or ""), "summary": str(o.get("summary") or "")}
            for o in (data.get("options") or []) if isinstance(o, dict)
        ]
        #: 用户选择：A/B/C 或 "ai"（让 AI 自己决定）或 "self"（我自己写）
        self.choice: str = str(data.get("choice") or "")
        #: 用户自己写的内容（choice == "self" 时）
        self.custom_text: str = str(data.get("custom_text") or "")
        #: 让 AI 定时的说明（choice == "ai" 时由模型补）
        self.resolved_focus: str = str(data.get("resolved_focus") or "")
        self.verdict: dict[str, Any] = dict(data.get("verdict") or {})
        self.gap_decision: str = str(data.get("gap_decision") or "")
        self.collection_rounds: int = int(
            data.get("collection_rounds") or DEFAULT_COLLECTION_ROUNDS)
        self.rounds_source: str = str(data.get("rounds_source") or "default")
        self.rounds_reason: str = str(data.get("rounds_reason") or "")
        #: 自定义任务：原始输入 + 解析出的 3 个执行方案
        self.custom_input: str = str(data.get("custom_input") or "")
        self.custom_plans: list[dict[str, Any]] = [
            dict(p) for p in (data.get("custom_plans") or []) if isinstance(p, dict)
        ]
        self.custom_choice: str = str(data.get("custom_choice") or "")
        #: 协作与写作的结果
        self.collaboration: dict[str, Any] = dict(data.get("collaboration") or {})
        self.content_chars: int = int(data.get("content_chars") or 0)
        self.error: str = str(data.get("error") or "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "options": list(self.options),
            "choice": self.choice,
            "custom_text": self.custom_text,
            "resolved_focus": self.resolved_focus,
            "verdict": dict(self.verdict),
            "gap_decision": self.gap_decision,
            "collection_rounds": self.collection_rounds,
            "rounds_source": self.rounds_source,
            "rounds_reason": self.rounds_reason,
            "custom_input": self.custom_input,
            "custom_plans": list(self.custom_plans),
            "custom_choice": self.custom_choice,
            "collaboration": dict(self.collaboration),
            "content_chars": self.content_chars,
            "error": self.error,
        }


def _sync_sections(conn: sqlite3.Connection, project_id: int,
                   genre: str, keys: list[str]) -> None:
    """让 ``writing_sections`` 与访谈选定的体裁/部分一致。

    为什么必须做：项目是用**项目默认体裁**（通常 research_article）建的，
    而访谈里用户可能选实验方案/综述——两者的部分 key 完全不同
    （`objective` vs `abstract`）。不同步的话，写作阶段会报"大纲节点不存在"。
    """
    _k, spec = stpl.genre_definition(genre)
    meta = {str(s.get("key")): s for s in (spec.get("sections") or [])
            if isinstance(s, dict)}
    existing = {r["section_key"] for r in conn.execute(
        "SELECT section_key FROM writing_sections WHERE project_id=?",
        (int(project_id),))}
    for key in keys:
        if key in existing:
            continue
        section = meta.get(key) or {}
        conn.execute(
            "INSERT OR IGNORE INTO writing_sections(project_id, section_key, "
            "heading, content, citation_ids, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (int(project_id), str(key),
             str(section.get("heading") or key), "", "[]", "draft",
             utcnow(), utcnow()))
    # 项目体裁也要跟着改，否则界面与导出仍按旧体裁渲染
    conn.execute("UPDATE writing_projects SET genre=?, updated_at=? "
                 "WHERE project_id=?", (str(genre), utcnow(), int(project_id)))
    conn.commit()


# ------------------------------------------------------------------ 落库

def load_state(conn: sqlite3.Connection, project_id: int) -> InterviewState:
    """读取访谈状态；没有就返回一个空状态（不自动落库）。"""
    row = conn.execute(
        "SELECT interview_json FROM writing_projects WHERE project_id=?",
        (int(project_id),)).fetchone()
    if not row:
        return InterviewState(None)
    raw = row["interview_json"] if "interview_json" in row.keys() else None
    if not raw:
        return InterviewState(None)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return InterviewState(None)
    return InterviewState(data)


def save_state(conn: sqlite3.Connection, project_id: int,
               state: InterviewState) -> None:
    conn.execute(
        "UPDATE writing_projects SET interview_json=?, updated_at=? "
        "WHERE project_id=?",
        (json.dumps(state.as_dict(), ensure_ascii=False), utcnow(),
         int(project_id)))
    conn.commit()


# ------------------------------------------------------------------ 启动

def start_interview(conn: sqlite3.Connection, project_id: int, *,
                    genre: str = "", topic: str = "",
                    sections: list[str] | None = None,
                    reset: bool = False) -> InterviewState:
    """开始（或恢复）访谈。

    **三问必问**：体裁 / 主题 / 包含哪些部分都由用户确认，不静默替他决定。
    能预填的只作为**建议值**（`suggested`）随问题返回，用户可直接采纳或改。
    """
    import uuid
    project = get_project(conn, project_id)
    if not project:
        raise ValueError(f"写作项目不存在: {project_id}")
    if reset:
        state = InterviewState(None)
    else:
        state = load_state(conn, project_id)
        if not state.intake.get("genre") and not state.history:
            state = InterviewState(None)   # 空白状态，走全新流程

    # 预填值只写进被显式传入的字段（这些是"用户已经回答过"的语义）；
    # 否则留空，交给问题去问。建议值走 state 之外的 `suggestions`。
    if genre:
        key, _spec = stpl.genre_definition(genre)
        state.intake["genre"] = key
    if topic:
        state.intake["topic"] = str(topic)
    if sections is not None:
        state.intake["sections"] = [str(s) for s in sections]
    if not state.run_id:
        state.run_id = uuid.uuid4().hex[:12]

    # 三问按顺序推进：已经答过（值非空）的跳过
    state.intake_step = ""
    for step in INTAKE_STEPS:
        if not _intake_answered(state, step):
            state.intake_step = step
            break

    for skey in state.selected_sections:
        state.section(skey)

    if not state.history:
        state.say("ai", "我们来把这篇定下来。先确认三件事：体裁、主题、包含哪些部分。")
    save_state(conn, project_id, state)
    return state


def _intake_answered(state: InterviewState, step: str) -> bool:
    if step == "genre":
        return bool(state.intake.get("genre"))
    if step == "topic":
        return bool(state.intake.get("topic"))
    if step == "sections":
        return bool(state.intake.get("sections"))
    return False


# ------------------------------------------------------------------ 当前问题

def current_question(conn: sqlite3.Connection, project_id: int,
                     state: InterviewState | None = None) -> dict[str, Any]:
    """当前要向用户提的问题。

    三种形态：
    - ``intake``：前置三问（体裁 / 主题 / 包含哪些部分）
    - ``section_choice``：某部分要写什么内容（3 案 + AI 自定 + 自己写）
    - ``gap_decision``：支撑不足怎么办（保留缺口 / 补全 / 自定义）
    - ``custom_plan_choice``：自定义任务解析出的 3 个执行方案
    - ``null``：全部完成
    """
    state = state or load_state(conn, project_id)
    if not state.intake_done():
        return _intake_question(conn, project_id, state)
    if state.all_done():
        return {"kind": "finished", "done": True,
                "completed": len(state.selected_sections),
                "total": len(state.selected_sections)}
    key = state.current_section_key()
    sec = state.section(key)
    heading = _heading(conn, project_id, key)

    if sec.stage in (STAGE_AWAITING_GAP,):
        return {"kind": "gap_decision", "section_key": key, "heading": heading,
                "verdict": sec.verdict,
                "suggested_queries": (sec.verdict or {}).get("suggested_queries") or [],
                "options": [
                    {"id": GAP_KEEP, "label": "保留缺口，照常撰写",
                     "hint": "正文顶部会插入显式缺口标注"},
                    {"id": GAP_COLLECT, "label": "执行检索补全，够了再写",
                     "hint": "按下面的检索词补充文献后再复判",
                     "suggested_queries": (sec.verdict or {}).get("suggested_queries") or [],
                     "rounds": {"default": DEFAULT_COLLECTION_ROUNDS,
                                "current": sec.collection_rounds,
                                "min": MIN_COLLECTION_ROUNDS,
                                "max": MAX_COLLECTION_ROUNDS,
                                "source": sec.rounds_source,
                                "reason": sec.rounds_reason}},
                    {"id": GAP_CUSTOM, "label": "自定义任务",
                     "hint": "用你自己的话说明要补什么，我来解析成可执行方案"},
                ]}

    if sec.stage == STAGE_AWAITING_CUSTOM:
        return {"kind": "custom_plan_choice", "section_key": key,
                "heading": heading, "custom_input": sec.custom_input,
                "plans": sec.custom_plans,
                "options": [{"id": p.get("id"), "label": p.get("label"),
                             "hint": p.get("hint") or "",
                             "tasks": p.get("tasks") or []}
                            for p in sec.custom_plans]
                + [{"id": "ai", "label": "让 AI 自己决定"},
                   {"id": "refine", "label": "我再明确一点"}]}

    # 默认：进入"这部分写什么内容"的问答
    return {"kind": "section_choice", "section_key": key, "heading": heading,
            "role": _role(conn, project_id, key),
            "options": [{"id": o["id"], "summary": o["summary"]}
                        for o in sec.options]
            + [{"id": "ai", "label": "让 AI 自己决定"},
               {"id": "self", "label": "我自己写"}]}


def _intake_question(conn: sqlite3.Connection, project_id: int,
                     state: InterviewState) -> dict[str, Any]:
    project = get_project(conn, project_id) or {}
    if state.intake_step == "genre" or not state.intake.get("genre"):
        # 项目本身有个默认体裁，作为建议给出（用户可直接采纳）
        suggested = str(project.get("genre") or "")
        options = _genre_options()
        for opt in options:
            opt["suggested"] = bool(suggested and opt["id"] == suggested)
        return {"kind": "intake",
                "step": "genre",
                "question": "这次要创作什么类型？",
                "options": options}
    if state.intake_step == "topic" or not state.intake.get("topic"):
        return {"kind": "intake", "step": "topic",
                "question": "这篇的主题是什么？",
                "input": {"type": "text",
                          "preset": str(project.get("topic")
                                        or project.get("title") or ""),
                          "placeholder": "例如：金催化炔酰胺环化"}}
    # sections
    key, spec = stpl.genre_definition(state.genre)
    chosen = set(state.selected_sections)
    items: list[dict[str, Any]] = []
    for section in spec.get("sections") or []:
        skey = str(section.get("key"))
        items.append({
            "key": skey,
            "heading": str(section.get("heading") or skey),
            "words": section.get("words") or 0,
            # 没挂模板的部分不生成正文（如参考文献），默认不勾
            "generates_body": bool(section.get("template")),
            "selected": skey in chosen or
                        (not chosen and bool(section.get("template"))),
        })
    return {"kind": "intake", "step": "sections",
            "question": f"要写哪些部分？（{spec.get('label') or state.genre}）",
            "multi": True,
            "items": items,
            "note": "未挂模板的部分不生成正文（如参考文献）"}


def _genre_options() -> list[dict[str, Any]]:
    from research_agent.writing.service import list_genres
    return [{"id": g["key"], "label": g["label"],
             "sections": len(g.get("sections") or []),
             "is_default": bool(g.get("is_default"))}
            for g in list_genres()]


def _heading(conn: sqlite3.Connection, project_id: int, key: str) -> str:
    for section in list_sections(conn, project_id):
        if str(section.get("section_key")) == str(key):
            return str(section.get("heading") or key)
    return str(key)


def _role(conn: sqlite3.Connection, project_id: int, key: str) -> str:
    project = get_project(conn, project_id) or {}
    _k, spec = stpl.genre_definition(str(project.get("genre") or ""))
    for section in spec.get("sections") or []:
        if str(section.get("key")) == str(key):
            name = str(section.get("template") or "")
            tpl = (spec.get("templates") or {}).get(name) or {}
            return str(tpl.get("role") or "")
    return ""


# ------------------------------------------------------------------ 作答

def answer(conn: sqlite3.Connection, project_id: int,
           payload: dict[str, Any]) -> InterviewState:
    """记录一次用户作答，并把状态推进到下一步。

    ``payload`` 的形态由 ``current_question`` 的 ``kind`` 决定：
    - intake: ``{"step": "genre"|"topic"|"sections", "value": …}``
    - section_choice: ``{"section_key": …, "choice": "A"|"ai"|"self", "text": …}``
    - gap_decision: ``{"section_key": …, "decision": "keep_gap"|"collect"|"custom",
                       "rounds": 2|"ai", "text": …}``
    - custom_plan_choice: ``{"section_key": …, "choice": "A"|"ai"|"refine", "text": …}``
    """
    state = load_state(conn, project_id)
    kind = str(payload.get("kind") or "").strip()

    if kind == "intake" or (not kind and payload.get("step")):
        return _answer_intake(conn, project_id, state, payload)
    if kind == "section_choice":
        return _answer_section_choice(conn, project_id, state, payload)
    if kind == "gap_decision":
        return _answer_gap(conn, project_id, state, payload)
    if kind == "custom_plan":
        return _answer_custom_plan(conn, project_id, state, payload)
    raise ValueError(f"无法识别的作答类型: {payload!r}")


def _answer_intake(conn: sqlite3.Connection, project_id: int,
                   state: InterviewState,
                   payload: dict[str, Any]) -> InterviewState:
    step = str(payload.get("step") or state.intake_step or "genre")
    value = payload.get("value")
    if step == "genre":
        key, _spec = stpl.genre_definition(str(value or ""))
        state.intake["genre"] = key
        # 体裁变了：之前选的部分作废，按新体裁重选
        state.intake["sections"] = []
        state.sections = {}
        state.cursor = 0
        state.say("user", f"体裁：{key}")
    elif step == "topic":
        text = str(value or "").strip()
        if not text:
            raise ValueError("主题不能为空")
        state.intake["topic"] = text
        state.say("user", f"主题：{text}")
    elif step == "sections":
        chosen = [str(x) for x in (value or []) if str(x).strip()]
        if not chosen:
            raise ValueError("至少要选一个部分")
        state.intake["sections"] = chosen
        state.cursor = 0
        for skey in chosen:
            state.section(skey)
        # **关键**：把选定的部分落到 writing_sections，否则写作阶段找不到节点
        _sync_sections(conn, project_id, state.genre, chosen)
        state.say("user", "包含部分：" + "、".join(chosen))
    else:
        raise ValueError(f"未知的前置问题: {step}")

    _advance_intake_step(state)
    save_state(conn, project_id, state)
    return state


def _advance_intake_step(state: InterviewState) -> None:
    for step in INTAKE_STEPS:
        if not _intake_answered(state, step):
            state.intake_step = step
            return
    state.intake_step = ""
    # 前置问完，把第一个部分推到"待拟方案"
    key = state.current_section_key()
    if key:
        sec = state.section(key)
        if sec.stage == STAGE_PENDING:
            sec.stage = STAGE_DRAFTING


def _answer_section_choice(conn: sqlite3.Connection, project_id: int,
                           state: InterviewState,
                           payload: dict[str, Any]) -> InterviewState:
    key = str(payload.get("section_key") or state.current_section_key())
    sec = state.section(key)
    choice = str(payload.get("choice") or "").strip()
    text = str(payload.get("text") or "").strip()
    if choice in ("self", "custom"):
        if not text:
            raise ValueError("选择了自己写，就必须给出内容")
        sec.choice = "self"
        sec.custom_text = text
        state.say("user", f"{key}：我自己写 —— {text}")
    elif choice == "ai":
        sec.choice = "ai"
        state.say("user", f"{key}：让 AI 自己决定")
    elif choice:
        ids = {str(o.get("id")) for o in sec.options}
        if ids and choice not in ids:
            raise ValueError(f"未知的方案编号: {choice}")
        sec.choice = choice
        picked = next((o for o in sec.options
                       if str(o.get("id")) == choice), {})
        sec.resolved_focus = str(picked.get("summary") or "")
        state.say("user", f"{key}：选方案 {choice}")
    else:
        raise ValueError("必须给出选择")
    sec.stage = STAGE_JUDGING
    save_state(conn, project_id, state)
    return state


def _answer_gap(conn: sqlite3.Connection, project_id: int,
                state: InterviewState,
                payload: dict[str, Any]) -> InterviewState:
    key = str(payload.get("section_key") or state.current_section_key())
    sec = state.section(key)
    decision = str(payload.get("decision") or "").strip()
    if decision not in (GAP_KEEP, GAP_COLLECT, GAP_CUSTOM):
        raise ValueError(f"未知的缺口决定: {decision}")
    sec.gap_decision = decision

    rounds = payload.get("rounds")
    if decision == GAP_COLLECT:
        if rounds == "ai":
            sec.rounds_source = "ai"
            # 具体值由 gap_planner 决定（阶段 3）；此处先置默认，稍后覆盖
            sec.collection_rounds = DEFAULT_COLLECTION_ROUNDS
        elif rounds is not None:
            try:
                value = int(rounds)
            except (TypeError, ValueError):
                raise ValueError(f"轮数必须是整数或 'ai'：{rounds!r}")
            value = max(MIN_COLLECTION_ROUNDS,
                        min(MAX_COLLECTION_ROUNDS, value))
            sec.collection_rounds = value
            sec.rounds_source = "user"
        else:
            sec.collection_rounds = DEFAULT_COLLECTION_ROUNDS
            sec.rounds_source = "default"
        sec.stage = STAGE_COLLABORATING
        state.say("user", f"{key}：执行检索补全（上限 {sec.collection_rounds} 轮）")
    elif decision == GAP_CUSTOM:
        sec.custom_input = str(payload.get("text") or "").strip()
        if not sec.custom_input:
            raise ValueError("自定义任务需要给出说明")
        sec.stage = STAGE_AWAITING_CUSTOM
        state.say("user", f"{key}：自定义任务 —— {sec.custom_input}")
    else:  # keep_gap
        sec.stage = STAGE_WRITING
        state.say("user", f"{key}：保留缺口继续撰写")
    save_state(conn, project_id, state)
    return state


def _answer_custom_plan(conn: sqlite3.Connection, project_id: int,
                        state: InterviewState,
                        payload: dict[str, Any]) -> InterviewState:
    key = str(payload.get("section_key") or state.current_section_key())
    sec = state.section(key)
    choice = str(payload.get("choice") or "").strip()
    if choice == "refine":
        extra = str(payload.get("text") or "").strip()
        if not extra:
            raise ValueError("要再明确一点，就得给出补充说明")
        sec.custom_input = f"{sec.custom_input}；补充：{extra}".strip("；")
        # 保持 awaiting_custom，等阶段 3 重新解析出新的 3 案
        sec.custom_plans = []
        state.say("user", f"{key}：再明确一点 —— {extra}")
    elif choice == "ai":
        sec.custom_choice = "ai"
        sec.stage = STAGE_COLLABORATING
        state.say("user", f"{key}：自定义任务交给 AI 决定")
    elif choice:
        ids = {str(p.get("id")) for p in sec.custom_plans}
        if ids and choice not in ids:
            raise ValueError(f"未知的执行方案: {choice}")
        sec.custom_choice = choice
        sec.stage = STAGE_COLLABORATING
        state.say("user", f"{key}：选执行方案 {choice}")
    else:
        raise ValueError("必须给出选择")
    save_state(conn, project_id, state)
    return state


# ------------------------------------------------------------------ 快照（给界面）

def snapshot(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """界面渲染所需的全部信息：进度 + 当前问题 + 各部分摘要。

    ``next_action`` 告诉界面"现在要不要起一个作业"：
    空串表示在等用户作答；否则界面应调 `/interview/step` 并轮询。
    """
    state = load_state(conn, project_id)
    total = len(state.selected_sections)
    done = sum(1 for k in state.selected_sections
               if state.section(k).stage == STAGE_DONE)
    from research_agent.writing.interview_loop import next_action
    return {
        "ok": True,
        "project_id": int(project_id),
        "intake": dict(state.intake),
        "intake_done": state.intake_done(),
        "cursor": state.cursor,
        "total": total,
        "completed": done,
        "finished": state.all_done(),
        "next_action": next_action(state),
        "question": current_question(conn, project_id, state),
        "sections": [
            {"section_key": k,
             "heading": _heading(conn, project_id, k),
             "stage": state.section(k).stage,
             "choice": state.section(k).choice,
             "options": state.section(k).options,
             "content_chars": state.section(k).content_chars,
             "verdict": state.section(k).verdict,
             "gap_decision": state.section(k).gap_decision,
             "collection_rounds": state.section(k).collection_rounds,
             "rounds_source": state.section(k).rounds_source,
             "rounds_reason": state.section(k).rounds_reason,
             "custom_plans": state.section(k).custom_plans,
             "custom_choice": state.section(k).custom_choice,
             "collaboration": state.section(k).collaboration,
             "error": state.section(k).error}
            for k in state.selected_sections
        ],
        "history": list(state.history)[-40:],
    }
