"""可编排的专项检索 skills：证据缺口补强、检索式扩展、引用溯源。

这些函数只负责“生成任务/计划/相关性门控”，实际抓取仍走 ApiHub 或调用方注入的
reference_fetcher，便于离线测试和后续替换具体文献源。
"""
from __future__ import annotations

import json
import logging
import re
from collections import deque
from typing import Any, Callable, Iterable

from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)


STRATEGY_BROAD = "broad"
STRATEGY_EVIDENCE_GAP = "evidence_gap"
STRATEGY_DEEP_SINGLE_DOMAIN = "deep_single_domain"
VALID_STRATEGIES = {
    STRATEGY_BROAD, STRATEGY_EVIDENCE_GAP, STRATEGY_DEEP_SINGLE_DOMAIN,
}

SKILL_EVIDENCE_GAP = "evidence_gap_retrieval"
SKILL_QUERY_EXPANSION = "iterative_query_expansion"
SKILL_REFERENCE_TRACING = "reference_tracing"
VALID_SKILLS = {SKILL_EVIDENCE_GAP, SKILL_QUERY_EXPANSION, SKILL_REFERENCE_TRACING}

_EVIDENCE_GAP_TERMS = (
    "证据", "缺口", "补强", "多源", "共识", "验证", "交叉验证",
    "支持论文", "重复证据", "multiple sources", "evidence gap",
    "cross-paper", "corroborat",
)
_DEEP_SINGLE_DOMAIN_TERMS = (
    "单领域", "精深", "深挖", "深挖文献", "系统梳理", "追溯",
    "参考文献", "引文", "引用网络", "snowball", "deep dive",
    "single domain", "reference trace",
)
_SKIP_GAP_RELATIONS = {
    "is_a", "cites", "published_in", "authored_by", "developed_by",
}
_RELATION_HINTS = {
    "promotes": "(promot* OR enhanc* OR induc* OR stimulat* OR accelerat*)",
    "inhibits": "(inhibit* OR suppress* OR reduc* OR block*)",
    "regulates": "(regulat* OR modulat* OR control* OR adjust*)",
    "activates": "(activat* OR up-regulat* OR upregulat*)",
    "treats": "(treat* OR ther* OR effect*)",
    "targets": "(target* OR bind* OR interact*)",
    "enables": "(enabl* OR support* OR allow* OR faciliat*)",
    "results_in": "(result* OR lead* OR caus* OR contribut*)",
}


def infer_retrieval_strategy(request: str = "") -> str:
    """模型不可用时的策略兜底：根据用户需求判断是否启用专项 skill。"""
    text = (request or "").lower()
    if any(t.lower() in text for t in _DEEP_SINGLE_DOMAIN_TERMS):
        return STRATEGY_DEEP_SINGLE_DOMAIN
    if any(t.lower() in text for t in _EVIDENCE_GAP_TERMS):
        return STRATEGY_EVIDENCE_GAP
    return STRATEGY_BROAD


def normalize_retrieval_strategy(value: Any, request: str = "") -> str:
    raw = str(value or "").strip().lower()
    if raw in VALID_STRATEGIES:
        return raw
    return infer_retrieval_strategy(request)


def normalize_retrieval(plan: dict[str, Any] | None,
                        request: str = "") -> dict[str, Any]:
    """把任务单中的 retrieval 配置补齐到稳定结构。"""
    plan = plan or {}
    raw = plan.get("retrieval") or {}
    if isinstance(raw, str):
        raw = {"strategy": raw}
    strategy = normalize_retrieval_strategy(raw.get("strategy"), request)
    evidence_enabled = bool(raw.get("evidence_gap_enabled", strategy
                                     == STRATEGY_EVIDENCE_GAP))
    deep_enabled = bool(raw.get("deep_single_domain_enabled", strategy
                                == STRATEGY_DEEP_SINGLE_DOMAIN))
    return {
        "strategy": strategy,
        "evidence_gap_enabled": evidence_enabled,
        "deep_single_domain_enabled": deep_enabled,
        "min_support_target": max(2, int(raw.get("min_support_target") or 2)),
        "max_skill_rounds": max(1, min(5, int(raw.get("max_skill_rounds") or 2))),
        "relevance_gate": str(raw.get("relevance_gate") or "strict"),
        "reference_direction": str(raw.get("reference_direction") or "both"),
    }


def _json_list(value: Any) -> list:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def normalize_edge_gap(item: Any) -> dict[str, Any] | None:
    """把内容节点/LLM 输出的缺口统一成稳定 schema。"""
    if not isinstance(item, dict):
        return None
    source = str(item.get("source_name") or "").strip()
    target = str(item.get("target_name") or "").strip()
    relation = str(item.get("relation_type") or "").strip()
    if not source or not target or not relation:
        return None
    try:
        support = max(0, int(item.get("support_count") or 0))
    except (TypeError, ValueError):
        support = 0
    try:
        target_support = max(support, int(item.get("target_support") or 2))
    except (TypeError, ValueError):
        target_support = max(support, 2)
    return {
        "pattern_id": str(item.get("pattern_id") or "").strip(),
        "source_type": str(item.get("source_type") or "").strip(),
        "source_name": source,
        "relation_type": relation,
        "target_type": str(item.get("target_type") or "").strip(),
        "target_name": target,
        "support_count": support,
        "target_support": target_support,
        "reason": str(item.get("reason") or "").strip(),
        "priority": str(item.get("priority") or "medium"),
    }


def normalize_edge_gaps(edge_gaps: Any, limit: int = 20) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _json_list(edge_gaps):
        gap = normalize_edge_gap(item)
        if gap and gap["pattern_id"] not in {g["pattern_id"] for g in out}:
            out.append(gap)
        if len(out) >= max(1, limit):
            break
    return out


def select_low_support_gaps(patterns: Iterable[dict[str, Any]],
                            *, min_support: int = 2,
                            limit: int = 20,
                            skip_relations: set[str] | None = None) -> list[dict[str, Any]]:
    """从知识消费返回的模式卡中选择值得补证的证据缺口。"""
    skip = set(skip_relations or _SKIP_GAP_RELATIONS)
    out: list[dict[str, Any]] = []
    for p in patterns:
        try:
            support = int(p.get("support_count") or 0)
        except (TypeError, ValueError):
            support = 0
        relation = str(p.get("relation_type") or "").strip()
        if support >= min_support or relation in skip:
            continue
        gap = normalize_edge_gap({
            "pattern_id": p.get("pattern_id"),
            "source_type": p.get("source_type"),
            "source_name": p.get("source_name"),
            "relation_type": relation,
            "target_type": p.get("target_type"),
            "target_name": p.get("target_name"),
            "support_count": support,
            "target_support": min_support,
            "reason": f"当前仅 {support} 篇论文支持，目标提升到 {min_support} 篇",
            "priority": "high" if support == 0 else "medium",
        })
        if gap:
            out.append(gap)
        if len(out) >= limit:
            break
    return out


def _search_phrase(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "").strip())
    if not value:
        return ""
    if re.fullmatch(r"[\u4e00-\u9fff]+|[A-Za-z0-9+#.\-]+", value):
        return value
    return f'"{value}"'


def _deterministic_gap_queries(gaps: list[dict[str, Any]],
                               limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for gap in gaps[:limit]:
        src = _search_phrase(gap.get("source_name") or "")
        tgt = _search_phrase(gap.get("target_name") or "")
        if not src or not tgt:
            continue
        relation = gap.get("relation_type") or "related_to"
        hint = _RELATION_HINTS.get(relation, "")
        query = f"{src} AND {tgt}"
        if hint:
            query += f" AND {hint}"
        out.append({
            "query": query,
            "pattern_id": gap.get("pattern_id"),
            "relation_type": relation,
            "source_name": gap.get("source_name"),
            "target_name": gap.get("target_name"),
            "reason": gap.get("reason"),
            "round": "evidence_gap",
        })
    return out


EVIDENCE_GAP_PROMPT = """你是证据缺口反向检索规划器。内容形成节点判断以下本体边仍需要更多论文支持。
请为每条边生成 1 条定向英文检索式（NCPSSD 等中文源可用中文），目标是召回可能重复陈述该关系的论文。

规则：
1. 必须同时包含源实体/其规范别名与目标实体/其规范别名；
2. 可补充关系词同义表达，但不要丢弃实体约束；
3. 不要扩大到整个领域综述；
4. 只输出 JSON 数组，元素结构：
[{{"pattern_id": "...", "query": "...", "rationale": "..."}}]

领域主题：{topic}
边缺口：{gaps}
"""


def _parse_json_array(raw: Any) -> list[Any] | None:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text).strip()
    text = re.sub(r"\s*```$", "", text).strip()
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        s, e = text.find(open_ch), text.rfind(close_ch)
        if s != -1 and e > s:
            try:
                parsed = json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                return parsed
    return None


def plan_evidence_gap_queries(edge_gaps: Any,
                              *,
                              topic: str = "",
                              model: Any = None,
                              max_queries: int = 10,
                              limit_gaps: int = 20) -> list[dict[str, Any]]:
    """把证据缺口转成检索计划。模型缺失时使用确定性实体对查询。"""
    gaps = normalize_edge_gaps(edge_gaps, limit_gaps)
    max_queries = max(1, min(50, int(max_queries)))
    if model is not None and gaps:
        prompt = EVIDENCE_GAP_PROMPT.replace("{topic}", topic).replace(
            "{gaps}", json.dumps(gaps, ensure_ascii=False, indent=2))
        try:
            msg = model.invoke([HumanMessage(content=prompt)])
            parsed = _parse_json_array(getattr(msg, "content", str(msg)))
            if parsed is not None:
                out = []
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    plan = dict(item)
                    plan["query"] = str(plan.get("query") or "").strip()
                    if plan["query"]:
                        out.append(plan)
                if out:
                    return out[:max_queries]
        except Exception as exc:  # noqa: BLE001
            logger.warning("证据缺口检索规划失败，回退确定性查询: %s", exc)
    return _deterministic_gap_queries(gaps, max_queries)


EXPANSION_PROMPT = """你是单领域检索扩展器。请依据上一轮检索命中的论文标题/摘要，
生成下一轮 {max_new} 条英文检索式。

要求：
1. 每条仍必须包含领域核心约束：{topic_terms}；
2. 从命中结果中提炼尚未充分覆盖的子方向，禁止复制用户原句；
3. 只输出 JSON 数组字符串。

当前命中摘要：
{records}
"""


def derive_expanded_queries(records: Any,
                            *,
                            topic_terms: list[str] | None = None,
                            model: Any = None,
                            max_queries: int = 6,
                            max_records: int = 20) -> list[dict[str, Any]]:
    """从首轮结果生成下一轮受控检索计划。模型缺失时返回空，避免主题漂移。"""
    recs = _json_list(records)
    compact = []
    for r in recs[:max_records]:
        compact.append({
            "title": r.get("title"),
            "abstract": (r.get("abstract") or "")[:500],
            "keywords": r.get("keywords"),
        })
    terms = [str(x) for x in topic_terms or [] if str(x).strip()]
    max_queries = max(1, min(10, int(max_queries)))
    if model is None or not compact:
        return []
    prompt = EXPANSION_PROMPT.replace(
        "{topic_terms}", ", ".join(terms) if terms else "原始领域主题"
    ).replace("{max_new}", str(max_queries)).replace(
        "{records}", json.dumps(compact, ensure_ascii=False, indent=2))
    try:
        msg = model.invoke([HumanMessage(content=prompt)])
        parsed = _parse_json_array(getattr(msg, "content", str(msg)))
        if parsed is None:
            return []
        out: list[dict[str, Any]] = []
        for item in parsed:
            query = str(item if isinstance(item, str)
                        else item.get("query") or "").strip()
            if query:
                out.append({
                    "query": query,
                    "rationale": str(item.get("rationale") or "")
                    if isinstance(item, dict) else "",
                    "round": "query_expansion",
                })
        return out[:max_queries]
    except Exception as exc:  # noqa: BLE001
        logger.warning("检索式扩展失败，本轮不扩展: %s", exc)
        return []


def _record_identity(record: dict[str, Any]) -> str:
    return str(record.get("paper_key") or record.get("doi") or
               record.get("title") or "").strip().lower()


def _topic_overlap(record: dict[str, Any],
                   terms: list[str]) -> float:
    text = " ".join([
        str(record.get("title") or ""),
        str(record.get("abstract") or ""),
        str(record.get("keywords") or ""),
    ]).lower()
    matched = sum(len(str(t).lower()) for t in terms if str(t).lower() in text)
    total = sum(max(1, len(str(t).lower())) for t in terms)
    return matched / total if total else 0.0


def filter_relevant_records(records: Iterable[dict[str, Any]],
                            *,
                            topic_terms: list[str] | None = None,
                            required_terms: list[str] | None = None,
                            min_topic_ratio: float = 0.0,
                            max_results: int = 200) -> list[dict[str, Any]]:
    """严格相关性门控：主题必须重叠，且 required_terms 至少命中一项。"""
    terms = [str(t).strip() for t in topic_terms or [] if str(t).strip()]
    required = [str(t).strip().lower() for t in required_terms or [] if str(t).strip()]
    out: list[dict[str, Any]] = []
    for rec in records:
        text = " ".join([
            str(rec.get("title") or ""),
            str(rec.get("abstract") or ""),
            str(rec.get("keywords") or ""),
        ]).lower()
        if required and not any(req in text for req in required):
            continue
        if terms and _topic_overlap(rec, terms) < min_topic_ratio:
            continue
        out.append(rec)
        if len(out) >= max_results:
            break
    return out


def trace_references(seed_paper_keys: Iterable[str],
                     *,
                     fetcher: Callable[[str], list[dict[str, Any]]] | None = None,
                     topic_terms: list[str] | None = None,
                     required_terms: list[str] | None = None,
                     max_depth: int = 1,
                     max_results: int = 200,
                     max_seeds: int = 20) -> dict[str, Any]:
    """沿引用图做受限 BFS。

    fetcher 接收一个 paper_key 并返回该论文的引用/被引记录；调用方负责接入
    Europe PMC / OpenAlex / Semantic Scholar。relevance gate 默认要求记录与主题重叠。
    """
    if fetcher is None:
        return {
            "candidates": [],
            "seen": [],
            "report": {
                "status": "not_configured",
                "error": "reference fetcher 未配置，无法执行引用溯源",
            },
        }
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    queue: deque[tuple[str, int]] = deque()
    for key in list(seed_paper_keys)[:max_seeds]:
        identity = str(key or "").strip()
        if identity and identity not in seen:
            seen.add(identity)
            queue.append((identity, 0))
    errors: list[dict[str, Any]] = []
    while queue:
        key, depth = queue.popleft()
        if depth >= max(1, int(max_depth)):
            continue
        try:
            refs = fetcher(key) or []
        except Exception as exc:  # noqa: BLE001
            errors.append({"seed": key, "error": str(exc)})
            continue
        relevant = filter_relevant_records(
            refs,
            topic_terms=topic_terms,
            required_terms=required_terms,
            min_topic_ratio=0.1,
            max_results=max_results - len(candidates),
        )
        for rec in relevant:
            identity = _record_identity(rec)
            if identity in seen:
                continue
            seen.add(identity)
            rec["_trace_depth"] = depth + 1
            rec["_trace_source"] = key
            candidates.append(rec)
            if depth + 1 < max(1, int(max_depth)):
                queue.append((identity, depth + 1))
            if len(candidates) >= max_results:
                queue.clear()
                break
    return {
        "candidates": candidates,
        "seen": sorted(seen),
        "report": {
            "status": "ok",
            "new_candidates": len(candidates),
            "errors": errors,
        },
    }


def build_skill_request(plan: dict[str, Any] | None,
                        *,
                        edge_gaps: Any = None,
                        seed_paper_keys: list[str] | None = None) -> dict[str, Any]:
    """把 planner/content 状态翻译成 retrieval node 可消费的 skill request。"""
    plan = plan or {}
    mission = plan.get("mission") or {}
    retrieval_plan = plan.get("retrieval_plan") or {}
    retrieval = normalize_retrieval(plan, str(plan.get("goal") or ""))
    seed_terms = retrieval_plan.get("query_variants") or mission.get("seed_terms") or []
    request: dict[str, Any] = {
        "reason": "执行 Planner 生成的检索计划",
        "seed_terms": seed_terms,
        "max_results": int(
            retrieval_plan.get("max_results_per_query")
            or mission.get("max_results") or 80),
        "min_confidence": float(
            retrieval_plan.get("min_quality")
            or mission.get("min_confidence") or 0.6),
        "domain_profile": plan.get("domain_profile"),
        "retrieval_plan": retrieval_plan,
        "budget": plan.get("budget") or {},
        "dimensions": retrieval_plan.get("dimensions") or [],
        "source_mix": retrieval_plan.get("source_mix") or [],
        "stop_conditions": retrieval_plan.get("stop_conditions") or [],
        "retrieval": retrieval,
        "suggested_route": "retrieval -> quality -> knowledge",
    }
    if edge_gaps:
        request["edge_gaps"] = normalize_edge_gaps(edge_gaps)
    if seed_paper_keys:
        request["seed_paper_keys"] = list(seed_paper_keys)
    return request
