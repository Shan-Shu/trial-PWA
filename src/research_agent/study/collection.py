"""研究语料补集服务：把检索请求接到现有 pipeline。

现有 retrieval -> quality -> knowledge 流水线已承担“语料建库”职责；这里只做
批量调度，不新增 LLM 角色。无网络/无 Key 时可传入离线 Services 完成冒烟验证。
"""
from __future__ import annotations

import logging
from typing import Any

from research_agent.study.json_utils import clean_str
from research_agent.retrieval.skills import (
    STRATEGY_EVIDENCE_GAP,
    normalize_retrieval_strategy,
    plan_evidence_gap_queries,
)

logger = logging.getLogger(__name__)


def collect_mission(request: dict[str, Any],
                    services=None,
                    max_topics: int = 8,
                    per_topic: int | None = None) -> dict[str, Any]:
    """调用现有 pipeline.run_topic 按 seed_terms 补充本地语料和动态本体。"""
    from research_agent.pipeline import run_topic

    terms = [clean_str(t) for t in request.get("seed_terms") or [] if clean_str(t)]
    if not terms:
        return {"count": 0, "errors": ["seed_terms 为空"]}
    max_results = max(1, int(request.get("max_results") or 80))
    per = per_topic or max(1, min(10, max_results // len(terms)))
    domain_profile = request.get("domain_profile") or None
    dimensions = (domain_profile or {}).get("dimensions") or None
    edge_gaps = request.get("edge_gaps") or []
    strategy = normalize_retrieval_strategy(
        ((request.get("retrieval") or {}).get("strategy")), "")
    evidence_mode = bool(edge_gaps) and strategy == STRATEGY_EVIDENCE_GAP
    # 领域相关性硬门：跨域命中在入库前就被挡掉，避免污染机制抽取
    gate_terms = list(terms) + [str(d) for d in (dimensions or []) if str(d).strip()]
    for extra in ((request.get("retrieval_plan") or {}).get("must_cover") or []):
        text = clean_str(extra)
        if text:
            gate_terms.append(text)
    collected: list[str] = []
    errors: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for term in terms[:max_topics]:
        fixed_queries: list[str] | None = None
        if evidence_mode:
            plans = plan_evidence_gap_queries(
                edge_gaps,
                topic=term,
                model=None,
                max_queries=min(10, max(1, per)),
                limit_gaps=min(20, len(edge_gaps)),
            )
            fixed_queries = [str(p.get("query") or "") for p in plans
                             if str(p.get("query") or "").strip()]
        try:
            out = run_topic(term, max_results=per, services=services,
                            dimensions=dimensions,
                            domain_profile=domain_profile,
                            fixed_queries=fixed_queries,
                            topic_terms=gate_terms)
            keys = (out.get("ingest") or {}).get("paper_keys") or []
            collected.extend(keys)
            gate = (out.get("ingest") or {}).get("relevance_gate")
            if gate:
                dropped.append({"term": term, **gate})
            logger.info("collect topic=%s papers=%d", term, len(keys))
        except Exception as exc:  # noqa: BLE001
            logger.warning("collect topic %s failed: %s", term, exc)
            errors.append({"term": term, "error": str(exc)})
    return {
        "count": len(collected),
        "paper_keys": collected,
        "errors": errors,
        "relevance_gate": dropped,
    }
