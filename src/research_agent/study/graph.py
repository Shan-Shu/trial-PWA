"""LangGraph 编排：工作规划 -> 检索/评估/提取 -> LLM 知识消费 -> 内容 -> 审核。

Planner 是唯一入口。collection 节点只执行 Planner 制定的 retrieval_plan；
Knowledge Consumer 不再自行触发检索，只消费已构建的结构化知识。
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from research_agent.config import Settings
from research_agent.db import connect, log_event, save_study_run
from research_agent.models import ROLE_LABEL, build_role_model, role_model_binding
from research_agent.study.collection import collect_mission
from research_agent.study.consumer import (
    build_retrieval_request,
    make_knowledge_consumer_node,
)
from research_agent.study.content import make_content_node
from research_agent.study.events import log_study_event
from research_agent.study.fact_check import make_fact_check_node
from research_agent.study.planner import make_planner_node
from research_agent.study.reviewer import make_review_node

logger = logging.getLogger(__name__)


class StudyState(TypedDict, total=False):
    request: str
    run_id: str
    plan: dict
    knowledge: dict
    collection_report: dict
    retrieval_request: dict
    collection_rounds: int
    collection_available: bool
    draft: dict
    review: dict
    review_rounds: int
    decision: str
    fact_check: dict
    edge_gaps: list
    collect_gaps: bool
    gap_retrieval_done: bool
    status: str
    error: str


@dataclass
class StudyServices:
    """研究任务五个 LLM 角色及 collection 子流程依赖。"""

    planner_model: Any = None
    consumer_model: Any = None
    content_model: Any = None
    review_model: Any = None
    fact_check_model: Any = None
    settings: Settings = field(default_factory=Settings.from_env)
    collector: Any = None

    @classmethod
    def from_env(cls, *, require_llms: bool = False,
                 collector: Any = None,
                 settings: Settings | None = None,
                 include_fact_check: bool = True) -> "StudyServices":
        """按角色绑定模型；生产入口应使用 require_llms=True。"""
        settings = settings or Settings.from_env()
        roles = ["planner", "consumer", "content", "review"]
        if include_fact_check:
            roles.append("fact_check")
        loaded: dict[str, Any] = {}
        errors: list[str] = []
        for role in roles:
            try:
                loaded[role] = build_role_model(role)
            except Exception as exc:  # noqa: BLE001
                loaded[role] = None
                if role == "fact_check":
                    # 事实核查有确定性实现，缺少模型不算致命
                    logger.warning("事实核查模型未绑定，将只做确定性核查: %s", exc)
                    continue
                errors.append(f"{ROLE_LABEL.get(role, role)}: {exc}")
        if require_llms and errors:
            raise RuntimeError("研究任务 LLM 绑定失败: " + "; ".join(errors))
        return cls(
            planner_model=loaded.get("planner"),
            consumer_model=loaded.get("consumer"),
            content_model=loaded.get("content"),
            review_model=loaded.get("review"),
            fact_check_model=loaded.get("fact_check"),
            settings=settings,
            collector=collector,
        )

    def binding_summary(self) -> list[dict[str, str]]:
        return [role_model_binding(role)
                for role in ("planner", "consumer", "content", "review",
                             "fact_check")]


def make_collection_node(services: StudyServices,
                         conn: sqlite3.Connection | None = None):
    """执行 Planner 的 retrieval_plan，不自行生成检索主题。"""

    def collection_node(state: StudyState) -> dict:
        plan = state.get("plan") or {}
        retrieval_plan = plan.get("retrieval_plan") or {}
        run_id = state.get("run_id")
        if not plan or not retrieval_plan:
            log_study_event(conn, services.settings, "collection", run_id,
                            "failed", {"error": "Planner retrieval_plan 为空"})
            return {"status": "planning_failed",
                    "error": "Planner retrieval_plan 为空，拒绝启动检索"}
        rounds = int(state.get("collection_rounds") or 0)
        budget = plan.get("budget") or {}
        max_rounds = max(1, int(budget.get("max_collection_rounds") or 2))
        if rounds >= max_rounds:
            return {"status": "collection_budget_exhausted",
                    "error": f"已达到最大检索轮数 {max_rounds}"}
        request = build_retrieval_request(
            plan,
            edge_gaps=(state.get("edge_gaps")
                       if state.get("collect_gaps")
                       or state.get("edge_gaps") else None),
        )
        dynamic = state.get("retrieval_request") or {}
        if isinstance(dynamic, dict) and dynamic:
            dynamic_terms = (dynamic.get("query_terms")
                             or dynamic.get("seed_terms") or [])
            if dynamic_terms:
                request["seed_terms"] = list(dict.fromkeys(dynamic_terms))
            if dynamic.get("must_cover"):
                request["must_cover"] = dynamic["must_cover"]
            if dynamic.get("reason"):
                request["reason"] = dynamic["reason"]
        log_study_event(conn, services.settings, "collection", run_id, "running",
                        {"round": rounds + 1,
                         "queries": request.get("seed_terms") or []})
        if services.collector is None:
            report = {
                "count": 0,
                "paper_keys": [],
                "errors": [],
                "skipped": True,
                "reason": "未配置 collector，使用本地已有知识",
            }
            status = "collection_skipped"
        else:
            try:
                report = services.collector(request) or {}
                status = "collected"
            except Exception as exc:  # noqa: BLE001
                logger.warning("collection failed: %s", exc)
                report = {"count": 0, "paper_keys": [], "errors": [str(exc)]}
                status = "collection_failed"
        log_study_event(conn, services.settings, "collection", run_id,
                        "done" if status != "collection_failed" else "failed",
                        {"round": rounds + 1, "status": status,
                         "count": report.get("count")})
        return {
            "collection_report": report,
            "retrieval_request": request,
            "collection_rounds": rounds + 1,
            "collection_available": services.collector is not None,
            "collect_gaps": False,
            "gap_retrieval_done": bool(state.get("gap_retrieval_done")
                                       or state.get("collect_gaps")),
            "status": status,
        }

    return collection_node


def _route_after_planner(state: StudyState) -> str:
    return "collection" if state.get("status") == "planned" else "end"


def _route_after_collection(state: StudyState) -> str:
    if state.get("status") == "planning_failed":
        return "end"
    return "consumer"


def _route_after_consumer(state: StudyState) -> str:
    """消费节点的补检请求不丢结果。

    若还有检索预算，回到 collection 补检；若没有（或未配置 collector），
    仍然继续到内容节点——否则消费节点产出的机制理解、机会缺口和冲突
    会随 ``manual_review → END`` 一起被丢弃。
    """
    status = state.get("status")
    if status == "consumer_failed":
        return "manual_review"
    knowledge = state.get("knowledge") or {}
    analysis = knowledge.get("consumer_analysis") or {}
    has_retrieval_requests = bool(analysis.get("retrieval_requests"))
    if status != "needs_collection" and not has_retrieval_requests:
        return "content"
    plan = state.get("plan") or {}
    max_rounds = max(1, int((plan.get("budget") or {}).get(
        "max_collection_rounds") or 2))
    rounds = int(state.get("collection_rounds") or 0)
    if state.get("collection_available") and rounds < max_rounds:
        return "collection"
    return "content"


def _route_after_content(state: StudyState) -> str:
    plan = state.get("plan") or {}
    retrieval = plan.get("retrieval") or {}
    max_rounds = max(1, int((plan.get("budget") or {}).get(
        "max_collection_rounds") or 2))
    rounds = int(state.get("collection_rounds") or 0)
    if (state.get("edge_gaps") and not state.get("gap_retrieval_done")
            and state.get("collection_available") and rounds < max_rounds
            and (retrieval.get("evidence_gap_enabled")
                 or retrieval.get("strategy") == "evidence_gap")):
        return "collection"
    return "reviewer"


MAX_FACT_CHECK_REVISIONS = 1


def _route_after_reviewer(state: StudyState) -> str:
    decision = state.get("decision") or "revise"
    if decision == "pass":
        return "fact_check"
    if decision == "manual_review":
        return "manual_review"
    plan = state.get("plan") or {}
    max_rounds = max(1, int((plan.get("budget") or {}).get(
        "max_collection_rounds") or 2))
    rounds = int(state.get("collection_rounds") or 0)
    if decision == "need_more_data":
        if state.get("collection_available") and rounds < max_rounds:
            return "collection"
        return "content_builder"
    return "content_builder"


def _route_after_fact_check(state: StudyState) -> str:
    """事实核查失败时回到内容节点定向修订，只允许有限次，避免死循环。"""
    result = state.get("fact_check") or {}
    if result.get("decision") == "pass":
        return "pass"
    # review_rounds 由审核节点每轮 +1，可同时封顶"审核-内容"与"事实核查-内容"循环
    if int(state.get("review_rounds") or 0) > MAX_FACT_CHECK_REVISIONS:
        return "manual_review"
    return "content_builder"


def build_study_graph(services: StudyServices | None = None,
                      conn: sqlite3.Connection | None = None,
                      max_results_override: int | None = None,
                      budget_override: dict[str, Any] | None = None,
                      max_review_rounds: int | None = None,
                      on_round: Any = None):
    """构建研究图。

    ``on_round(state)`` 会在审核轮次结束时被调用（P2-4：逐轮落库支持），
    由调用方决定如何持久化；回调异常被吞掉，绝不影响图执行。
    """
    services = services or StudyServices()

    def _snapshot(state: StudyState) -> None:
        if on_round is None:
            return
        try:
            on_round(state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("研究轮次落库失败: %s", exc)

    g = StateGraph(StudyState)
    rounds = max(1, int(max_review_rounds
                       or getattr(services.settings, "study_max_review_rounds", 3)))
    g.add_node("planner", make_planner_node(
        services.planner_model, max_results_override=max_results_override,
        conn=conn, settings=services.settings, budget_override=budget_override))
    g.add_node("collection", make_collection_node(services, conn=conn))
    g.add_node(
        "knowledge_consumer",
        make_knowledge_consumer_node(
            conn=conn, settings=services.settings,
            model=services.consumer_model),
    )
    g.add_node("content_builder", make_content_node(
        services.content_model, conn=conn, settings=services.settings))
    g.add_node(
        "reviewer",
        make_review_node(services.review_model, max_rounds=rounds,
                         conn=conn, settings=services.settings),
    )
    g.add_node("fact_checker", make_fact_check_node(
        services.fact_check_model, conn=conn, settings=services.settings))
    # 轮次快照节点：只做副作用（逐轮落库），不改状态
    g.add_node("round_snapshot", lambda state: (_snapshot(state), {})[1])

    g.add_edge(START, "planner")
    g.add_conditional_edges(
        "planner", _route_after_planner,
        {"collection": "collection", "end": END},
    )
    g.add_conditional_edges(
        "collection", _route_after_collection,
        {"consumer": "knowledge_consumer", "end": END},
    )
    g.add_conditional_edges(
        "knowledge_consumer", _route_after_consumer,
        {"content": "content_builder", "collection": "collection",
         "manual_review": END},
    )
    g.add_conditional_edges(
        "content_builder", _route_after_content,
        {"collection": "collection", "reviewer": "reviewer"},
    )
    g.add_edge("reviewer", "round_snapshot")
    g.add_conditional_edges(
        "round_snapshot", _route_after_reviewer,
        {
            "fact_check": "fact_checker",
            "content_builder": "content_builder",
            "collection": "collection",
            "manual_review": END,
        },
    )
    g.add_conditional_edges(
        "fact_checker", _route_after_fact_check,
        {
            "pass": END,
            "content_builder": "content_builder",
            "manual_review": END,
        },
    )
    return g.compile()


def run_study(request: str,
              services: StudyServices | None = None,
              conn: sqlite3.Connection | None = None,
              force_collect: bool = False,
              max_results_override: int | None = None,
              persist: bool = True,
              budget_override: dict[str, Any] | None = None,
              max_review_rounds: int | None = None) -> dict[str, Any]:
    # 默认生产路径必须绑定真实 LLM，不能静默使用确定性实现。
    services = services or StudyServices.from_env(require_llms=True)
    run_id = uuid4().hex[:12]

    def _persist_round(state: StudyState) -> None:
        """审核轮次快照（P2-4）：逐轮落库，支持事后审计"每轮改了什么"。"""
        round_index = int(state.get("review_rounds") or 0)
        snapshot = dict(state)
        snapshot["run_id"] = run_id
        snapshot["request"] = request
        if conn is not None:
            save_study_run(conn, run_id, snapshot,
                           round_index=round_index, request=request)
            return
        # 独立连接：图执行期间 conn 可能不属于本调用方
        own_round_conn = connect(services.settings.db_path)
        try:
            save_study_run(own_round_conn, run_id, snapshot,
                           round_index=round_index, request=request)
        finally:
            own_round_conn.close()

    app = build_study_graph(
        services, conn,
        max_results_override=max_results_override,
        budget_override=budget_override,
        max_review_rounds=max_review_rounds,
        on_round=_persist_round if persist else None)

    own = conn is None
    db = conn or connect(services.settings.db_path)
    try:
        log_event(db, "study", "session-start", None,
                  {"run_id": run_id, "request": request, "status": "running"})
    finally:
        if own:
            db.close()
    out = app.invoke({
        "request": request,
        "run_id": run_id,
        "review_rounds": 0,
        "collection_rounds": 0,
        "collection_available": services.collector is not None,
        "status": "started",
        "force_collect": force_collect,
    })
    out["run_id"] = run_id
    own = conn is None
    db = conn or connect(services.settings.db_path)
    try:
        # 关键中间态落库：没有这张表就无法审计"改了什么、为什么改"
        if persist:
            try:
                save_study_run(db, run_id, out,
                               round_index=int(out.get("review_rounds") or 0),
                               request=request)
            except Exception as exc:  # noqa: BLE001
                logger.warning("研究任务中间态落库失败: %s", exc)
        log_event(db, "study", "session-end", None, {
            "run_id": run_id,
            "status": out.get("status"),
            "decision": out.get("decision"),
            "fact_check": (out.get("fact_check") or {}).get("decision"),
            "request": request,
        })
    finally:
        if own:
            db.close()
    return out


def _resolve_study_role(role: str, choice: str, smoke: bool) -> Any:
    if smoke or choice == "smoke":
        return None
    if choice == "none":
        return None
    provider = None if choice in ("auto", None) else choice
    return build_role_model(role, provider=provider)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    providers = sorted({"openai", "deepseek", "qwen", "glm",
                        "anthropic", "google"})
    ap = argparse.ArgumentParser(
        description="Planner-first 研究任务：规划/检索/消费/内容/审核")
    ap.add_argument("--request", default="调研 3D 打印骨支架的最新研究前沿")
    ap.add_argument("--db", help="SQLite 数据库路径（默认 data/research_agent.db）")
    for role in ("planner", "consumer", "content", "review", "fact_check"):
        ap.add_argument(f"--{role}-llm",
                        choices=["auto", "none", "smoke"] + providers,
                        default="auto")
    for role in ("retriever", "quality", "knowledge"):
        ap.add_argument(f"--{role}-llm",
                        choices=["auto", "none", "smoke"] + providers,
                        default="auto")
    ap.add_argument("--llm-smoke", action="store_true",
                    help="离线模式：研究节点不使用真实 LLM")
    ap.add_argument("--no-collect", action="store_false", dest="collect",
                    help="不执行初始检索，只消费本地知识")
    ap.add_argument("--max-results", type=int, default=None,
                    help="覆盖 Planner 的单查询结果上限")
    ap.add_argument(
        "--source",
        choices=["pubmed", "arxiv", "both", "europepmc",
                 "semantic_scholar", "openalex", "ncpssd", "fulltext", "all"],
        default="fulltext")
    args = ap.parse_args(argv)
    if args.llm_smoke:
        args.collect = False

    settings = Settings.from_env()
    if args.db:
        settings.db_path = args.db

    collector = None
    pipeline_services = None
    if args.collect:
        from research_agent.pipeline import Services as PipelineServices
        from research_agent.retrieval.api_clients import ApiHub

        pipeline_services = PipelineServices(
            api=ApiHub(source=args.source),
            retriever_model=_resolve_study_role(
                "retriever", args.retriever_llm, args.llm_smoke),
            quality_model=_resolve_study_role(
                "quality", args.quality_llm, args.llm_smoke),
            knowledge_model=_resolve_study_role(
                "knowledge", args.knowledge_llm, args.llm_smoke),
            settings=settings,
        )
        collector = lambda req: collect_mission(req, services=pipeline_services)

    errors: list[str] = []
    models: dict[str, Any] = {}
    for role in ("planner", "consumer", "content", "review", "fact_check"):
        try:
            models[role] = _resolve_study_role(
                role, getattr(args, f"{role}_llm"), args.llm_smoke)
        except Exception as exc:  # noqa: BLE001
            models[role] = None
            if role == "fact_check":
                # 事实核查有确定性实现，模型缺失只降级不影响主链路
                continue
            errors.append(f"{ROLE_LABEL.get(role, role)}: {exc}")
    if errors and not args.llm_smoke:
        for error in errors:
            print(f"[LLM 绑定失败] {error}")
        return 2
    services = StudyServices(
        planner_model=models.get("planner"),
        consumer_model=models.get("consumer"),
        content_model=models.get("content"),
        review_model=models.get("review"),
        fact_check_model=models.get("fact_check"),
        settings=settings,
        collector=collector,
    )
    if not args.llm_smoke:
        for binding in services.binding_summary():
            print(f"[{binding['label']}] {binding['provider']}/{binding['model']}")
    out = run_study(
        args.request, services,
        force_collect=args.collect,
        max_results_override=args.max_results,
    )
    status = out.get("status")
    print(f"\n状态: {status} | 请求: {args.request}")
    plan = out.get("plan") or {}
    if plan:
        print(f"任务单: {plan.get('goal', '')} [{plan.get('content_type', '')}]")
    if status in ("planning_failed", "consumer_failed",
                  "collection_budget_exhausted"):
        print(f"失败原因: {out.get('error') or status}")
        return 1
    knowledge = out.get("knowledge") or {}
    design = knowledge.get("design_context") or {}
    print(f"模式卡: {len(knowledge.get('patterns') or [])} | "
          f"证据卡: {len(knowledge.get('evidence') or [])} | "
          f"超边: {len(knowledge.get('hyperedges') or [])} | "
          f"检索轮数: {out.get('collection_rounds', 0)}")
    print(f"机制状态: {len(design.get('mechanism_states') or [])} | "
          f"算子链候选: {len(design.get('operator_candidates') or [])} | "
          f"可追溯率: {(design.get('traceability') or {}).get('ratio')}")
    draft = out.get("draft") or {}
    review = out.get("review") or {}
    pool = draft.get("candidate_pool") or {}
    if pool:
        print(f"候选池: 生成 {pool.get('generated')} → 去重 {pool.get('after_dedupe')} "
              f"→ 选中 {pool.get('selected')}（同构 {len(pool.get('duplicates') or [])} 组）")
    levels = [s.get("innovation_level") for s in (draft.get("strategies") or [])
              if s.get("rank")]
    if levels:
        print(f"入选候选创新等级: {levels}")
    fact = out.get("fact_check") or {}
    if fact:
        print(f"事实核查: {fact.get('decision')} | 问题 {len(fact.get('issues') or [])} 个 "
              f"| 模式 {fact.get('mode')}")
    if review.get("decision") == "pass" and fact.get("decision") != "revise":
        print("\n----- 审核通过内容 -----")
        print(draft.get("markdown") or "（无内容）")
    else:
        print("\n----- 审核结论 -----")
        print(review.get("summary") or out.get("status") or "")
        for issue in review.get("issues") or []:
            print(f"- [{issue.get('severity')}] {issue.get('problem')}")
        for issue in fact.get("issues") or []:
            print(f"- [事实核查/{issue.get('type')}] {issue.get('problem')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
