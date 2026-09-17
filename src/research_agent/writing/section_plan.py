"""工作规划节点：只凭主题，自己排出"每个部分写什么、要什么证据"。

这是**唯一的规划节点**。它不负责写正文，也不负责判定——只产出一份工作规划，
供每个部分的模板执行时取用。

两种模式**纯并行**（用户要求）：

- ``mode="auto"``：用户没给指令 → 系统依据主题 + 体裁部分表 + 规划任务单自拟；
- ``mode="user"``：用户给了指令 → 指令作为最高优先级的写作要点，规划不覆盖。

分配逻辑刻意做成**确定性**的（不逐节调模型）：

- 逐节调模型意味着 7 个部分 = 7 次调用，成本和时延都不可接受，而且结果更难解释；
- 规划节点的 LLM 只出**一份**任务单（复用既有 ``make_planner_node``），
  再由本模块把 ``analysis_plan`` / ``analysis_targets`` 按"部分职责"做映射下发。
- 有模型就把模型给的维度发下去；没有模型就用模板自带的要点。
  **无论哪种，界面都能看出每个要点是从哪来的**。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from research_agent.config import Settings, settings as default_settings
from research_agent.study.planner import make_planner_node
from research_agent.writing import section_template as stpl
from research_agent.writing.service import get_project, list_sections

__all__ = ["build_work_plan", "section_directive", "plan_sections"]

SOURCE_USER = stpl.SOURCE_USER
SOURCE_PLAN = stpl.SOURCE_PLAN
SOURCE_TEMPLATE = stpl.SOURCE_TEMPLATE

#: 部分 key → 该部分关心的规划维度（与 analysis_plan.dimensions 对齐）
_ROLE_DIMENSIONS: dict[str, list[str]] = {
    "abstract": [],
    "scope": ["方法", "材料"],
    "landscape": ["方法", "应用", "材料"],
    "mechanisms": ["机制", "方法", "材料"],
    "results": ["机制", "性能指标", "材料"],
    "discussion": ["机制", "应用", "开放问题"],
    "open_questions": ["开放问题", "应用"],
    "outlook": ["开放问题", "应用"],
    "introduction": ["方法", "应用", "开放问题"],
    "methods": ["方法", "材料", "性能指标"],
    "objective": ["应用", "开放问题"],
    "background": ["方法", "机制", "应用"],
    "design": ["方法", "材料", "性能指标"],
    "groups": ["方法", "材料"],
    "parameters": ["材料", "方法", "性能指标"],
    "readouts": ["性能指标", "方法"],
    "risks": ["开放问题", "机制", "材料"],
    "significance": ["应用", "开放问题"],
    "objectives": ["应用", "方法"],
    "approach": ["方法", "材料", "性能指标"],
    "feasibility": ["机制", "材料"],
    "plan": ["方法"],
    "summary": [],
    "point_by_point": [],
    "changes": [],
    "references": [],
}


def _planner_contract(topic: str, instruction: str, *, conn: sqlite3.Connection,
                      settings: Settings,
                      planner_model: Any) -> dict[str, Any]:
    """把"主题 + 用户指令"交给既有 Planner，拿一份整篇任务单。"""
    request = topic.strip() or "请检索并整理研究前沿"
    if instruction.strip():
        request = f"{request}：{instruction.strip()}"
    node = make_planner_node(planner_model, conn=conn, settings=settings)
    out = node({"request": request, "run_id": None})
    plan = out.get("plan") or {}
    from research_agent.writing.section_graph import normalize_fallback_plan
    if plan:
        plan = normalize_fallback_plan(plan, request)
    return {"plan": plan, "status": str(out.get("status") or ""),
            "error": str(out.get("error") or "")}


def _dimensions_from_contract(plan: dict[str, Any]) -> dict[str, list[str]]:
    """把任务单里的维度信息整理成"每部分能用的要点池"。"""
    analysis = plan.get("analysis_plan") or {}
    targets = [str(x) for x in (plan.get("analysis_targets") or [])
               if str(x).strip()]
    dimensions = [str(x) for x in (analysis.get("dimensions") or [])
                  if str(x).strip()]
    comparisons = [str(x) for x in (analysis.get("required_comparisons") or [])
                   if str(x).strip()]
    questions = [str(x) for x in (analysis.get("open_questions") or [])
                 if str(x).strip()]
    return {
        "targets": targets,
        "dimensions": dimensions or targets,
        "comparisons": comparisons,
        "open_questions": questions,
    }


def _focus_for_section(section_key: str, tpl: dict[str, Any],
                       pool: dict[str, list[str]]) -> tuple[list[str], str]:
    """某个部分的写作要点：**模板自带要点在前，规划维度补充在后**。

    返回 ``(要点列表, 来源)``。来源只标"规划是否真的补充了内容"，
    以便界面区分"这是系统按主题推出来的"还是"这只是模板的通用建议"。
    """
    base = [str(x) for x in (tpl.get("focus") or []) if str(x).strip()]
    if not (pool.get("dimensions") or pool.get("open_questions")):
        return base, SOURCE_TEMPLATE

    wanted = _ROLE_DIMENSIONS.get(str(section_key), [])
    picked: list[str] = []
    if wanted:
        for token in wanted:
            for candidate in (pool.get("dimensions") or []):
                if (token in candidate or candidate in token) \
                        and candidate not in picked:
                    picked.append(candidate)
    if str(section_key) in ("open_questions", "outlook", "risks"):
        for item in pool.get("open_questions") or []:
            if item not in picked:
                picked.append(item)
    if str(section_key) in ("landscape", "mechanisms", "discussion", "design"):
        for item in pool.get("comparisons") or []:
            if item not in picked:
                picked.append(item)

    if not picked:
        return base, SOURCE_TEMPLATE
    merged = list(dict.fromkeys(base + picked))
    return merged, SOURCE_PLAN


def plan_sections(plan: dict[str, Any], genre: str | None,
                  sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把任务单下发到每个部分，形成工作规划的主体。"""
    pool = _dimensions_from_contract(plan)
    out: list[dict[str, Any]] = []
    for section in sections:
        key = str(section.get("section_key") or section.get("key") or "")
        heading = str(section.get("heading") or key)
        tpl_key, tpl = stpl.template_for_section(genre, key)
        focus, source = _focus_for_section(key, tpl, pool)
        spec = stpl.dimension_spec(genre, key)
        out.append({
            "section_key": key,
            "heading": heading,
            "words": int(section.get("words") or 0),
            "template": tpl_key,
            "role": str(tpl.get("role") or ""),
            "focus": focus,
            "focus_source": source,
            "required_dimensions": spec.required,
            "evidence_types": spec.evidence_types,
            "min_support": spec.min_support,
            "allow_gaps": spec.allow_gaps,
            "fields": stpl.collect_section_fields(genre, key),
            "generates_body": bool(tpl_key),
        })
    return out


def build_work_plan(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    topic: str = "",
    instruction: str = "",
    settings: Settings | None = None,
    planner_model: Any = None,
    persist: bool = False,
) -> dict[str, Any]:
    """生成整篇工作规划。

    ``instruction`` 非空即为 **user 模式**：它作为最高优先级要点，规划不覆盖。
    ``persist=True`` 时写入 ``writing_projects.plan_json``。
    """
    settings = settings or default_settings
    project = get_project(conn, project_id)
    if not project:
        raise ValueError(f"写作项目不存在: {project_id}")
    genre = str(project.get("genre") or "")
    topic = str(topic or project.get("topic") or project.get("title") or "").strip()
    instruction = str(instruction or "").strip()
    mode = "user" if instruction else "auto"

    contract = _planner_contract(topic, instruction, conn=conn,
                                 settings=settings,
                                 planner_model=planner_model)
    plan = contract["plan"] or {}

    sections = list_sections(conn, project_id)
    section_plans = plan_sections(plan, genre, sections)

    result: dict[str, Any] = {
        "ok": bool(section_plans),
        "project_id": int(project_id),
        "genre": genre,
        "topic": topic,
        "mode": mode,
        "instruction": instruction,
        "planner_status": contract["status"],
        "planner_error": contract["error"],
        "planner_mode": str(plan.get("planner_mode") or ""),
        "plan": plan,
        "sections": section_plans,
        "section_count": len(section_plans),
        "generates_body_count": sum(
            1 for s in section_plans if s["generates_body"]),
    }
    if not result["ok"]:
        result["error"] = contract["error"] or "工作规划未产出任何部分"
    if persist and result["ok"]:
        import json
        from research_agent.db import utcnow
        conn.execute(
            "UPDATE writing_projects SET plan_json=?, updated_at=? "
            "WHERE project_id=?",
            (json.dumps(result, ensure_ascii=False), utcnow(),
             int(project_id)),
        )
        conn.commit()
    return result


def section_directive(work_plan: dict[str, Any] | None,
                      section_key: str) -> dict[str, Any]:
    """从工作规划里取某个部分的指令；没有就返回空。"""
    for item in (work_plan or {}).get("sections") or []:
        if str(item.get("section_key")) == str(section_key):
            return dict(item)
    return {}
