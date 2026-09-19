"""单节点写作链：规划 → 充分性判定 →（补检 ↺）→ 知识消费 → 成段。

与 ``study/graph.py`` 的关系（**刻意不改动后者**，它有 20+ 个既有测试保护整篇
任务语义）：

======================  ==========================  ==========================
阶段                     study 图（整篇任务）         本图（单个大纲节点）
======================  ==========================  ==========================
规划                     planner                      planner（注入节点上下文）
是否够写                 无显式判定（消费后才补检）     **sufficiency（检索前判定）**
知识消费                 knowledge_consumer           knowledge_consumer（复用）
产出                     content（多候选方案池）        section_compose（单段正文）
======================  ==========================  ==========================

关键差异：**充分性判定在检索之前**。既有流程是"先检索一轮，消费时才发现缺口"，
在写作台上意味着用户点一次就白烧一轮配额；本图先判后检，不足才花钱。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from research_agent.config import Settings, settings as default_settings
from research_agent.study.consumer import make_knowledge_consumer_node
from research_agent.study.graph import StudyServices, make_collection_node
from research_agent.study.planner import make_planner_node
from research_agent.writing.sufficiency import evaluate_sufficiency

logger = logging.getLogger(__name__)

__all__ = ["SectionState", "build_section_graph", "build_section_request",
           "normalize_fallback_plan"]

#: 分词后仍不适合当检索词的脚手架词（与充分性判定的噪声表同源思路）
_RETRIEVAL_NOISE = {
    "检索词", "检索", "大纲", "小节", "本节", "论文", "学术", "规划", "材料",
    "指令", "要求", "只写", "强调", "引用", "部分", "search", "section",
    "summarise", "summarize", "cite", "cited", "citation", "highlight",
    "least", "source", "sources",
}

#: 英文缩略词中常见的非实词（避免 "of"/"with" 进检索词）
_EN_STOP = {
    "the", "and", "for", "with", "from", "into", "onto", "that", "this", "are",
    "was", "were", "has", "have", "been", "its", "their", "which", "using",
    "based", "about", "over", "under", "between", "study", "review",
}


class SectionState(TypedDict, total=False):
    """单节点写作链的状态。

    与 ``StudyState`` 分开定义：写作台没有"多轮审核/事实核查/轮次快照"，
    硬塞进同一 TypedDict 会让两边都读不懂对方的字段。
    """

    run_id: str
    project_id: int
    section_key: str
    heading: str
    instruction: str
    section_note: str
    words: int
    topic: str
    planning_request: str
    genre: str
    focus: list[Any]
    fields_block: str
    fields: dict[str, Any]
    role: str
    template_key: str
    unmet_dimensions: list[Any]
    evidence_types: list[Any]
    plan: dict[str, Any]
    sufficiency: dict[str, Any]
    sufficiency_rounds: int
    retrieval_request: dict[str, Any]
    collection_report: dict[str, Any]
    collection_rounds: int
    collect_gaps: bool
    gap_retrieval_done: bool
    edge_gaps: list[Any]
    knowledge: dict[str, Any]
    section: dict[str, Any]
    status: str
    error: str


def build_section_request(project: dict[str, Any], heading: str,
                          instruction: str, section_note: str = "") -> str:
    """把"整篇主题 + 节点指令"拼成 Planner 的 request。

    ``heading`` 仅在**没有主题也没有指令**时兜底使用；正常情况下它不参与——
    章节标题决定"写哪一节"，不是"检索什么"（详见函数体内的说明）。

    刻意写得**短**：Planner 只接受一个自然语言 request，而它的确定性兜底会把
    request 整句当成检索词，因此这里只给"主题 + 指令"两件事，不含脚手架措辞。
    """
    topic = str(project.get("topic") or project.get("title") or "").strip()
    parts: list[str] = []
    if topic:
        parts.append(topic)
    # 章节标题不进检索式：它决定"写哪一节"，不是"检索什么"。
    # 实测把「引言小节」拼进去后，它会变成一条检索词（中文标题配英文库=必然 0 命中），
    # 并污染充分性判定的要求词元。
    if section_note:
        parts.append(section_note)
    if instruction:
        parts.append(instruction)
    if not parts and heading:
        parts.append(str(heading))
    if not parts:
        return "本节检索"
    return "：".join(parts) if len(parts) > 1 else parts[0]


def normalize_fallback_plan(plan: dict[str, Any], request: str) -> dict[str, Any]:
    """无模型时把"整句检索式"换成**分词后的检索式**（只动离线兜底）。

    上游 ``normalize_plan`` 在无模型时把整句 request 同时塞进 ``goal``、
    ``seed_terms`` 和 ``query_variants``。对整篇研究任务这是可接受的兜底，
    但在节点写作链上会直接导致"命中文献 0 篇 → 永远判不足"。

    这里**不修改上游 Planner**（它有既有测试与全局语义），只在写作层就地修正，
    且仅当 ``planner_mode == "offline_fallback"`` 时生效——有模型时不动它。
    """
    if str(plan.get("planner_mode") or "") != "offline_fallback":
        return plan
    from research_agent.study.planner import split_seed_terms

    terms = split_seed_terms(str(request or ""))
    # ``split_seed_terms`` 只认"英文 ≥4 字母 / 中文 ≥2 字"。中文主题配英文库是
    # 真实场景（实测：中文 topic 让命中文献变 0，compose 直接判不足），
    # 因此额外保留原句里的英文实词作为可投递检索词。
    english = [w for w in re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", str(request or ""))
               if w.lower() not in _EN_STOP]
    merged = list(dict.fromkeys(
        [t for t in terms if t not in _RETRIEVAL_NOISE]
        + [w.lower() for w in english if w.lower() not in _RETRIEVAL_NOISE]))
    if not merged:
        return plan
    plan = dict(plan)
    mission = dict(plan.get("mission") or {})
    retrieval_plan = dict(plan.get("retrieval_plan") or {})
    mission["seed_terms"] = merged[:8]
    retrieval_plan["query_variants"] = merged[:8]
    plan["mission"] = mission
    plan["retrieval_plan"] = retrieval_plan
    plan["goal"] = " ".join(merged[:4])
    plan["domain"] = " ".join(merged[:2])
    return plan


def build_section_graph(
    *,
    conn: sqlite3.Connection | None = None,
    settings: Settings | None = None,
    planner_model: Any = None,
    consumer_model: Any = None,
    compose_model: Any = None,
    compose_model_reason: str = "",
    services: StudyServices | None = None,
    on_round: Callable[[dict[str, Any]], None] | None = None,
    skip_judgement: bool = False,
):
    """构建单个大纲节点的写作链。

    ``on_round(state)`` 在每一轮充分性判定后被调用（含补检后的复审），
    供调用方逐轮落库决策轨迹；回调异常被吞掉，绝不影响图执行。
    """
    s = settings or (services.settings if services else default_settings)
    services = services or StudyServices(settings=s)

    def _snapshot(state: dict[str, Any]) -> None:
        if on_round is None:
            return
        try:
            on_round(state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("节点决策快照落库失败: %s", exc)

    # ------------------------------------------------------------ 节点
    planner_node = make_planner_node(
        planner_model, conn=conn, settings=s,
        budget_override={"max_collection_rounds": max(
            1, int(getattr(s, "section_max_collection_rounds", 2)))},
    )
    collection_node = make_collection_node(services, conn=conn)
    consumer_node = make_knowledge_consumer_node(
        conn=conn, settings=s, model=consumer_model)

    def planner_section_node(state: dict[str, Any]) -> dict[str, Any]:
        """复用研究 Planner，但把 request 换成"本节"的规划请求。"""
        request = str(state.get("instruction") or "").strip()
        heading = str(state.get("heading") or "").strip()
        section_note = str(state.get("section_note") or "")
        # 调用方已拼好 request 时直接用（见 build_section_request）
        if state.get("planning_request"):
            request = str(state["planning_request"])
        else:
            request = build_section_request(
                {"topic": state.get("topic") or "", "title": ""},
                heading or "本节", request, section_note)
        out = planner_node({**state, "request": request})
        if out.get("plan"):
            out["plan"] = normalize_fallback_plan(out["plan"], request)
        out["sufficiency_rounds"] = int(state.get("sufficiency_rounds") or 0)
        return out

    def sufficiency_node(state: dict[str, Any]) -> dict[str, Any]:
        own_conn = conn is None
        db = conn
        # **补检轮数以 collection 节点的计数器为准**。曾经的写法把
        # "判定次数"当成"已补检轮数"，导致 max_rounds=1 时第一次判定就自认
        # 已用完预算、max_rounds=2 时白白少补一轮（离一错误）。
        done_rounds = int(state.get("collection_rounds") or 0)
        # 轮次编号必须在**调用回调之前**算好：LangGraph 传给节点的 state 是
        # 上一次归并的结果，节点自己返回的增量还没并进来，否则快照轮次会少 1、
        # 与 planning 行撞进同一主键。
        next_round = int(state.get("sufficiency_rounds") or 0) + 1
        try:
            if db is None:
                from research_agent.db import connect
                db = connect(s.db_path)
            # 模板驱动：判定维度与闸门由"这一部分"的模板决定，不由用户指令决定
            verdict = evaluate_sufficiency(
                db,
                plan=state.get("plan") or {},
                instruction=str(state.get("instruction") or ""),
                heading=str(state.get("heading") or ""),
                settings=s,
                rounds_done=done_rounds,
                section_key=str(state.get("section_key") or ""),
                genre=str(state.get("genre") or ""),
                focus_terms=list(state.get("focus") or []),
            )
        finally:
            if own_conn and db is not None:
                db.close()

        _snapshot({**state, "sufficiency": verdict, "sufficiency_rounds": next_round})
        decision = str(verdict.get("decision") or "")
        out: dict[str, Any] = {
            "sufficiency": verdict,
            "status": decision,
            "sufficiency_rounds": next_round,
            # **必须显式写回 state**：compose_node 靠这两个字段决定要不要在正文
            # 顶部插缺口标注，而判定节点返回的 verdict 不会自动摊平进 state
            # （实测漏了这步 → 走了带缺口写作却没有任何标注）。
            "unmet_dimensions": list(verdict.get("unmet_dimensions") or []),
            "evidence_types": list(verdict.get("evidence_types") or []),
        }
        if decision != "sufficient":
            requests = verdict.get("retrieval_requests") or []
            if requests:
                out["retrieval_request"] = requests[0]
        return out

    def compose_node(state: dict[str, Any]) -> dict[str, Any]:
        from research_agent.writing.section_compose import (
            build_materials, compose_section)

        verdict = state.get("sufficiency") or {}
        own_conn = conn is None
        db = conn
        try:
            if db is None:
                from research_agent.db import connect
                db = connect(s.db_path)
            materials = build_materials(
                db, list(verdict.get("paper_keys") or []),
                list(verdict.get("evidence_ids") or []),
                limit=int(getattr(s, "section_max_materials", 12)),
            )
        finally:
            if own_conn and db is not None:
                db.close()

        # 知识消费节点的产出（`consumer_analysis` + `design_context`）。
        # **必须在这里接上**：此前 compose_node 只读充分性判定，消费节点
        # 归纳出的机制状态与候选算子链在成段这一步被整体丢弃，正文自然
        # 写不出机制与设计层面的内容。
        #
        # 注意消费节点的状态（`consumed` / `needs_collection` / `consumer_failed`）
        # 是写在 **state 顶层**而不是 bundle 里的，这里显式捎进产物——它说明了
        # 机制证据到底可不可用，写作时必须看得到。
        knowledge = dict(state.get("knowledge") or {})
        knowledge.setdefault("consumer_status", str(state.get("status") or ""))
        section = compose_section(
            heading=str(state.get("heading") or ""),
            instruction=str(state.get("instruction") or ""),
            plan=state.get("plan") or {},
            materials=materials,
            sufficiency=verdict,
            knowledge=knowledge,
            model=compose_model,
            model_reason=compose_model_reason,
            section_note=str(state.get("section_note") or ""),
            words=int(state.get("words") or 0),
            # 模板渲染出的结构化字段块（每项标了来源：用户/规划/模板默认）
            fields_block=str(state.get("fields_block") or ""),
            role=str(state.get("role") or ""),
            unmet_dimensions=list(state.get("unmet_dimensions") or []),
            evidence_types=list(state.get("evidence_types") or []),
        )
        return {"section": section, "status": "written"}

    # ------------------------------------------------------------ 布线
    g = StateGraph(SectionState)
    g.add_node("planner_section", planner_section_node)
    g.add_node("sufficiency", sufficiency_node)
    g.add_node("collection", collection_node)
    g.add_node("knowledge_consumer", consumer_node)
    g.add_node("section_compose", compose_node)

    g.add_edge(START, "planner_section")
    g.add_conditional_edges(
        "planner_section",
        lambda state: ("sufficiency" if state.get("plan")
                       and state.get("status") == "planned" else "end"),
        {"sufficiency": "sufficiency", "end": END},
    )
    if skip_judgement:
        # 访谈闭环已经判过支撑：这里只做规划 → 消费 → 成段。
        # 再判一次不仅重复，还会因为 collector 已接上而真的发起检索。
        g.add_edge("sufficiency", "knowledge_consumer")
    else:
        g.add_conditional_edges(
            "sufficiency", _route_after_sufficiency,
            {"collection": "collection",
             "knowledge_consumer": "knowledge_consumer",
             "end": END},
        )
        g.add_edge("collection", "sufficiency")
    g.add_edge("knowledge_consumer", "section_compose")
    g.add_edge("section_compose", END)
    return g.compile()


def _route_after_sufficiency(state: dict[str, Any]) -> str:
    """充分性判定后的去向。

    - ``sufficient`` → 消费知识并成段；
    - ``insufficient`` 且**还有补检预算** → 回到检索扩库；
    - 预算用尽仍不足 → 看该部分的模板允不允许**带缺口写作**：
      ``allow_gaps=True`` 时照常成段，但正文顶部会插入显式的缺口标注
      （用户要求：证据不足可以写，但必须标出来，并允许标注范围内的发挥）；
      ``allow_gaps=False`` 时才返回 ``needs_data`` 且不产出正文。
    """
    verdict = state.get("sufficiency") or {}
    decision = str(verdict.get("decision") or "")
    if decision == "sufficient":
        return "knowledge_consumer"
    if decision == "insufficient":
        # 预算按"已实际补检的轮数"算（collection 节点每跑一次 +1）
        done = int(state.get("collection_rounds") or 0)
        max_rounds = int(verdict.get("max_rounds") or 0)
        if done < max_rounds:
            return "collection"
    # 预算用尽：允许缺口就写（带标注），否则停在这里
    if bool(verdict.get("allow_gaps", False)) and _has_writable_support(verdict):
        return "knowledge_consumer"
    return "end"


def _has_writable_support(verdict: dict[str, Any]) -> bool:
    """带缺口写作的最低门槛：至少要有一点点可引用的东西。

    完全空白（零命中文献、零证据、零材料）时不该产出正文——那不是"带缺口的写作"，
    而是"凭空的写作"，标注也救不回来。
    """
    counts = verdict.get("counts") or {}
    return bool(verdict.get("paper_keys")) or \
        int(counts.get("matched_papers") or 0) > 0 or \
        int(counts.get("evidence_ids") or 0) > 0
