"""LangGraph 流水线：文献检索 → 质量评估 → 知识提取（动态本体）。

图结构::

    START ──> retrieval ──> quality ──┬─(knowledge / flagged)─> knowledge ──> END
        mode=search|load|enrich        ├─(enrich)────────────────> retrieval  ①
                                      └─(human)─────────────────> human_review ──> END

① 元数据缺漏时“发回”检索节点补全（受 max_meta_attempts 限制，超限转人工）。
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from research_agent.config import Settings, settings as default_settings
from research_agent.db import connect
from research_agent.knowledge.node import make_knowledge_node
from research_agent.models import (
    ROLE_LABEL,
    build_role_model,
    role_model_binding,
)
from research_agent.quality.node import make_human_review_node, make_quality_node
from research_agent.role_fakes import make_role_fake
from research_agent.retrieval.api_clients import ApiHub
from research_agent.retrieval.monitor import PaperMonitor
from research_agent.retrieval.node import ingest_search_results, make_retrieval_node

logger = logging.getLogger(__name__)


class PipelineState(TypedDict, total=False):
    """图状态。每次运行处理一篇文献（检索模式会先批量入库多篇）。"""
    query: str
    max_results: int
    mode: str                 # search | load | enrich | quality
    current_key: str
    paper_keys: list[str]
    paper_record: dict
    retrieval_report: dict
    enrich_report: dict
    quality_result: dict
    decision: str             # knowledge | flagged | enrich | human
    needs_review: bool
    missing_fields: list[str]
    meta_attempts: int
    extraction_report: dict
    status: str
    error: str
    domain_profile: dict


@dataclass
class Services:
    """注入依赖：外部 API 聚合 + 三节点各自的 LLM + 设置。

    retriever_model / quality_model / knowledge_model 分别对应
    DeepSeek V4 / GLM 4.7 Flash / DeepSeek V4 Pro(临时替代 gpt-5.6) 的绑定模型。
    """
    api: ApiHub = field(default_factory=ApiHub)
    retriever_model: Any = None
    quality_model: Any = None
    knowledge_model: Any = None
    settings: Settings = field(default_factory=Settings.from_env)
    offline: bool = False


def route_after_quality(state: PipelineState) -> str:
    decision = state.get("decision", "human")
    if decision in ("knowledge", "flagged"):
        return "knowledge"
    if decision == "enrich":
        return "retrieval"
    return "human_review"


def build_pipeline_graph(services: Services | None = None,
                         conn: sqlite3.Connection | None = None):
    """组装并编译三节点 LangGraph。"""
    services = services or Services()
    s = services.settings
    g = StateGraph(PipelineState)
    g.add_node("retrieval", make_retrieval_node(
        services.api, model=services.retriever_model, conn=conn, settings=s))
    g.add_node("quality", make_quality_node(
        None if services.offline else services.api,
        model=services.quality_model, conn=conn, settings=s,
        offline=services.offline))
    g.add_node("knowledge", make_knowledge_node(
        services.knowledge_model, conn=conn, settings=s))
    g.add_node("human_review", make_human_review_node(conn, s))

    g.add_edge(START, "retrieval")
    g.add_edge("retrieval", "quality")
    g.add_conditional_edges(
        "quality", route_after_quality,
        {"knowledge": "knowledge", "retrieval": "retrieval",
         "human_review": "human_review"},
    )
    g.add_edge("knowledge", END)
    g.add_edge("human_review", END)
    return g.compile()


def process_papers(keys: list[str], services: Services | None = None,
                   conn: sqlite3.Connection | None = None,
                   max_meta_attempts: int | None = None,
                   domain_profile: dict | None = None) -> list[dict]:
    """对多篇文献逐篇运行 质量→知识 全流程（供主题批量与监控回调使用）。

    单篇失败不影响批次其余部分（P1-9）：异常被包装成该篇的失败结果。
    """
    services = services or Services()
    graph = build_pipeline_graph(services, conn)
    results = []
    for key in keys:
        state: PipelineState = {"current_key": key, "mode": "load",
                                "meta_attempts": 0}
        if domain_profile:
            state["domain_profile"] = domain_profile
        try:
            out = graph.invoke(state)
        except Exception as exc:  # noqa: BLE001
            logger.exception("处理文献失败 %s", key)
            out = {"current_key": key, "status": "error", "error": str(exc)}
        results.append(out)
    return results


def run_topic(query: str, max_results: int = 3, services: Services | None = None,
              conn: sqlite3.Connection | None = None,
              dimensions: list[str] | None = None,
              domain_profile: dict | None = None,
              fixed_queries: list[str] | None = None,
              topic_terms: list[str] | None = None) -> dict[str, Any]:
    """完整流程：检索批量入库 → 逐篇 质量评估+知识提取。返回检索报告与逐篇结果。

    topic_terms 提供时启用领域相关性硬门，跨域命中不入库。
    """
    services = services or Services()
    s = services.settings
    own = conn is None
    db = conn or connect(s.db_path)
    try:
        ingest = ingest_search_results(
            query, max_results, api=services.api,
            model=services.retriever_model, conn=db, settings=s,
            dimensions=dimensions, fixed_queries=fixed_queries,
            topic_terms=topic_terms)
        keys = ingest["paper_keys"]
        per_paper = process_papers(keys, services, db,
                                   domain_profile=domain_profile) if keys else []
        return {"ingest": ingest, "per_paper": per_paper}
    finally:
        if own:
            db.close()


def _fmt_outcome(out: dict) -> str:
    q = (out.get("quality_result") or {}).get("quality")
    qs = f"Q={q}" if q is not None else "Q=-"
    extra = ""
    ext = out.get("extraction_report") or {}
    if ext:
        e = ext.get("extracted") or {}
        extra = (f" | entities={e.get('entities', 0)} relations={e.get('relations', 0)} "
                 f"events={e.get('events', 0)} new_types={e.get('new_types', [])}")
    return f"{out.get('current_key')} | {out.get('decision', '?')} | {qs} | {out.get('status')}{extra}"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="LangGraph 多模型科研辅助 Agent 流水线")
    choices = ["auto", "none", "smoke"] + sorted(
        {"openai", "deepseek", "qwen", "glm", "anthropic", "google"})
    ap.add_argument("--query", help="检索主题/关键词")
    ap.add_argument("--max-results", type=int, default=3)
    ap.add_argument(
        "--source",
        choices=["pubmed", "arxiv", "both", "europepmc",
                 "semantic_scholar", "openalex", "ncpssd", "fulltext", "all"],
        default="fulltext",
        help="文献来源；fulltext=Europe PMC/arXiv/Semantic Scholar/OpenAlex 全文优先")
    ap.add_argument("--db", help="SQLite 数据库路径（默认 data/research_agent.db）")
    ap.add_argument("--retriever-llm", choices=choices, default="auto",
                    help="文献检索节点 LLM（默认 auto → DeepSeek V4）")
    ap.add_argument("--quality-llm", choices=choices, default="auto",
                    help="质量评估节点 LLM（默认 auto → GLM 4.7 Flash）")
    ap.add_argument("--knowledge-llm", choices=choices, default="auto",
                    help="知识提取节点 LLM（默认 auto → deepseek-v4-pro，临时替代 gpt-5.6）")
    ap.add_argument("--llm-smoke", action="store_true",
                    help="三角色都用离线假 LLM 跑通全链路（等价三个 --*-llm smoke）")
    ap.add_argument("--model", default=None,
                    help="[兼容] 旧参数，等价 --knowledge-llm")
    ap.add_argument("--offline", action="store_true",
                    help="不调用外部网络（API 与各节点 LLM 均禁用，知识节点假模型除外）")
    ap.add_argument("--monitor", action="store_true",
                    help="进入实时监控模式：轮询数据库新文献并自动处理")
    ap.add_argument("--poll-interval", type=float, default=60.0)
    args = ap.parse_args(argv)

    settings = Settings.from_env()
    if args.db:
        settings.db_path = args.db

    def _resolve_role(role: str, choice: str) -> Any:
        if args.llm_smoke or choice == "smoke":
            model = make_role_fake(role)
            print(f"[{ROLE_LABEL[role]}] 使用离线假 LLM（演示模式）")
            return model
        if choice == "none":
            print(f"[{ROLE_LABEL[role]}] LLM 未启用（确定性实现）")
            return None
        if args.offline and role != "knowledge":
            print(f"[{ROLE_LABEL[role]}] --offline：跳过外部 LLM")
            return None
        provider = None if choice in (None, "auto") else choice
        try:
            model = build_role_model(role, provider=provider)
            binding = role_model_binding(role)
            print(f"[{ROLE_LABEL[role]}] LLM 已绑定: "
                  f"{binding['provider']}/{binding['model']}")
            return model
        except Exception as exc:  # noqa: BLE001
            print(f"[{ROLE_LABEL[role]}] 警告：{exc}（将用确定性实现，"
                  f"或在 .env 配好 Key 后重试）")
            return None

    if args.model is not None:
        args.knowledge_llm = args.model
    services = Services(
        api=ApiHub(source=args.source),
        retriever_model=_resolve_role("retriever", args.retriever_llm),
        quality_model=_resolve_role("quality", args.quality_llm),
        knowledge_model=_resolve_role("knowledge", args.knowledge_llm),
        settings=settings, offline=args.offline,
    )

    if args.monitor:
        monitor = PaperMonitor(db_path=str(settings.db_path))

        def _on_new(keys: list[str]) -> None:
            for out in process_papers(keys, services):
                print(_fmt_outcome(out))

        print(f"监控启动，轮询间隔 {args.poll_interval}s，Ctrl+C 退出...")
        try:
            monitor.run_forever(_on_new, poll_interval=args.poll_interval)
        except KeyboardInterrupt:
            print("\n监控停止")
        return 0

    if not args.query:
        ap.error("--query 必填（--monitor 模式除外）")
    res = run_topic(args.query, max_results=args.max_results, services=services)
    print(f"\n检索入库 {res['ingest']['count']} 篇: {res['ingest']['paper_keys']}")
    for out in res["per_paper"]:
        print(_fmt_outcome(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
