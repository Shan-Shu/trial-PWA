"""部分规划与判定：访谈闭环与执行图**共用**的这一层。

存在的理由：写作台现在有两条进入判定的路径——

- **访谈闭环**（方案 v6）：逐部分问答后判定；
- **执行图**（`section_graph`）：既有的一次成段链路。

如果两边各写一套"拼检索请求 → 判定"，口径迟早会漂移（同一部分在一个入口
判"充足"、在另一个入口判"不足"）。所以把这两步集中在这里，两边都调用它。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from research_agent.config import Settings, settings as default_settings
from research_agent.study.planner import make_planner_node
from research_agent.writing.sufficiency import evaluate_sufficiency

__all__ = ["plan_section_request", "judge_section"]


def plan_section_request(
    conn: sqlite3.Connection,
    *,
    project: dict[str, Any],
    heading: str,
    instruction: str = "",
    section_note: str = "",
    settings: Settings | None = None,
    planner_model: Any = None,
) -> dict[str, Any]:
    """为本部分做检索规划（复用研究 Planner）。

    返回 ``{"plan": {...}, "status": str, "error": str, "request": str}``。
    """
    from research_agent.writing.section_graph import (
        build_section_request, normalize_fallback_plan)

    s = settings or default_settings
    request = build_section_request(project, heading, instruction, section_note)
    node = make_planner_node(planner_model, conn=conn, settings=s)
    out = node({"request": request, "run_id": None})
    plan = out.get("plan") or {}
    if plan:
        # 离线兜底会把整句当检索词 → 在这里就地分词修正
        plan = normalize_fallback_plan(plan, request)
    return {"plan": plan, "status": str(out.get("status") or ""),
            "error": str(out.get("error") or ""), "request": request}


def judge_section(
    conn: sqlite3.Connection,
    *,
    plan: dict[str, Any],
    section_key: str,
    genre: str,
    heading: str = "",
    instruction: str = "",
    focus: list[str] | None = None,
    rounds_done: int = 0,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """判定当前库能否支撑这一部分（模板驱动：只有 required 维度是必考）。

    ``rounds_done`` 是**已实际完成的补检轮数**（不是判定次数）。
    """
    return evaluate_sufficiency(
        conn,
        plan=plan or {},
        instruction=str(instruction or ""),
        heading=str(heading or ""),
        settings=settings or default_settings,
        rounds_done=int(rounds_done or 0),
        section_key=str(section_key or ""),
        genre=str(genre or ""),
        focus_terms=list(focus or []),
    )
