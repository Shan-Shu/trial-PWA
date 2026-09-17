"""知识消费节点。

代码负责从数据库构建可追溯知识包和执行引用校验；LLM 负责机制理解、
冲突识别、机会发现、设计上下文和补检请求。检索由独立的 collection 节点执行。

设计要点（v0.4.1）：
1. 超边按类型配额检索（event 型优先），不再按置信度整体截断；
2. 相关性打分改为词元命中 + 机制/条件词汇加权，event 型超边额外加权；
3. 每条入选超边可携带多条证据句，证据不再"每条边只用一句"；
4. 机制状态（mechanism_states）必须绑定真实 evidence_id，可校验、可溯源；
5. 引用校验只处理"ID 形状"的字符串，其余文本不再被当作非法引用丢弃。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Callable

from langchain_core.messages import HumanMessage

from research_agent.config import Settings, settings as default_settings
from research_agent.db import connect
from research_agent.domains import normalize_domain_profile
from research_agent.ontology import store as ont
from research_agent.retrieval.skills import (
    build_skill_request,
    normalize_edge_gaps,
)
from research_agent.study.events import log_study_event
from research_agent.study.json_utils import clean_str, parse_json_object
from research_agent.study.mechanism_lexicon import mechanism_lexicon
from research_agent.study.model_call import invoke_with_timeout
from research_agent.study.reaction_operators import (
    OPERATOR_NAMES,
    normalize_operator_chain,
    operator_catalog_for_prompt,
)

logger = logging.getLogger(__name__)

# 超边类型配额：由代码控制的检索预算（与学科无关）
DEFAULT_HYPEREDGE_QUOTA: dict[str, int] = {
    "event": 120,
    "relation": 30,
    "mechanism": 40,
    "reaction": 40,
}
HYPEREDGE_BRIEF_SCAN = 1500
# evidence 归属文本字段；这些 key 里的字符串按“引用 ID”或“描述文本”区分处理。
_ID_PREFIXES = ("P", "E", "H", "MS", "OP", "GAP", "OC", "FC")
_ID_PATTERN = re.compile(
    r"^(?:" + "|".join(_ID_PREFIXES) + r")-\d{1,6}(?:-\d{1,4})?$")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9\-]{2,}")


def _lex(domain_kind: str = "") -> dict[str, Any]:
    """机制词表（技能包 + 领域增补）；未传领域时用通用词表。"""
    return mechanism_lexicon(domain_kind)


def _json_list(raw: Any) -> list:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def is_reference_id(value: str) -> bool:
    """判断字符串是否为本系统的引用编号（P-0001 / E-0001-1 / H-0001 / MS-0001）。"""
    return bool(_ID_PATTERN.match(str(value or "").strip()))


def _tokenize(terms: list[str]) -> set[str]:
    """把检索短语拆成词元，避免整句短语子串匹配恒为 0。"""
    tokens: set[str] = set()
    for term in terms:
        text = clean_str(term).lower()
        if not text:
            continue
        tokens.add(text)
        for token in _TOKEN_RE.findall(text):
            if len(token) >= 4 and not token.isdigit():
                tokens.add(token)
    return tokens


def _plural_variants(token: str) -> tuple[str, ...]:
    if token.endswith("ies") and len(token) > 4:
        return (token, token[:-3] + "y")
    if token.endswith("es") and len(token) > 3:
        return (token, token[:-2])
    if token.endswith("s") and len(token) > 3:
        return (token, token[:-1])
    return (token, token + "s")


def score_text(tokens: set[str], text: str, weight: int = 1) -> int:
    """词元命中打分；同一词元只计一次，命中数量与长度共同决定分值。"""
    if not tokens or not text:
        return 0
    low = text.lower()
    score = 0
    for token in tokens:
        if any(variant in low for variant in _plural_variants(token)):
            score += weight * max(2, min(len(token), 12))
    return score


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    low = (text or "").lower()
    return sum(1 for k in keywords if k in low)


def mine_ontology_evidence(conn: sqlite3.Connection,
                           mission: dict[str, Any],
                           limit_patterns: int = 200,
                           limit_evidence: int = 500,
                           hyperedge_quota: dict[str, int] | None = None,
                           max_evidence_per_hyperedge: int = 3) -> dict[str, Any]:
    """从 ontology_edges + hyperedges + provenance 构建可追溯知识包。"""
    ont.init_ontology(conn)
    summary = ont.graph_summary(conn)
    paper_count = conn.execute("SELECT COUNT(*) AS c FROM papers").fetchone()["c"]
    min_conf = float(mission.get("min_confidence") or 0.6)
    terms = [clean_str(t) for t in mission.get("seed_terms") or [] if clean_str(t)]
    tokens = _tokenize(terms)
    rows = conn.execute(
        """
        SELECT e.edge_id, e.relation_type, e.source_node, e.target_node,
               e.confidence, e.provenance, e.evidence_tier,
               sn.node_type AS source_type, sn.name AS source_name,
               tn.node_type AS target_type, tn.name AS target_name
        FROM ontology_edges e
        JOIN ontology_nodes sn ON sn.node_id = e.source_node
        JOIN ontology_nodes tn ON tn.node_id = e.target_node
        WHERE e.confidence >= ?
        ORDER BY e.confidence DESC, e.edge_id
        """,
        (min_conf,),
    ).fetchall()
    patterns: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    all_papers: set[str] = set()
    for r in rows:
        pattern_id = f"P-{int(r['edge_id']):04d}"
        prov = _json_list(r["provenance"])
        paper_keys: list[str] = []
        for p in prov:
            if isinstance(p, dict) and clean_str(p.get("paper")):
                key = clean_str(p["paper"])
                if key not in paper_keys:
                    paper_keys.append(key)
                all_papers.add(key)
        source_label = f"{r['source_type']}: {r['source_name']}"
        target_label = f"{r['target_type']}: {r['target_name']}"
        title = f"{source_label} --{r['relation_type']}--> {target_label}"
        score = score_text(tokens, title, 2) + score_text(
            tokens, r["relation_type"], 1)
        pattern = {
            "pattern_id": pattern_id,
            "title": title,
            "source_type": r["source_type"],
            "source_name": r["source_name"],
            "relation_type": r["relation_type"],
            "target_type": r["target_type"],
            "target_name": r["target_name"],
            "support_count": len(paper_keys),
            "confidence": round(float(r["confidence"] or 0), 3),
            "evidence_tier": r["evidence_tier"] or "unclassified",
            "paper_keys": paper_keys,
            "evidence_ids": [],
            "relevance": score,
        }
        for pi, p in enumerate(prov):
            if not isinstance(p, dict):
                continue
            evidence_id = f"E-{int(r['edge_id']):04d}-{pi + 1}"
            sentence = clean_str(p.get("evidence"), "")
            if not sentence:
                continue
            evidence.append({
                "evidence_id": evidence_id,
                "pattern_id": pattern_id,
                "source": "ontology",
                "paper_key": clean_str(p.get("paper"), "unknown"),
                "sentence": sentence,
                "confidence": round(float(r["confidence"] or 0), 3),
                "evidence_tier": r["evidence_tier"] or "unclassified",
            })
            pattern["evidence_ids"].append(evidence_id)
        patterns.append(pattern)
    if tokens:
        patterns.sort(key=lambda x: (x["relevance"], x["support_count"],
                                     x["confidence"]), reverse=True)
    patterns = [p for p in patterns if p["evidence_ids"]]
    patterns = patterns[:limit_patterns]
    pattern_ids = {p["pattern_id"] for p in patterns}
    evidence = [e for e in evidence if e["pattern_id"] in pattern_ids][:limit_evidence]

    hyperedges = _select_hyperedges(conn, tokens, min_conf,
                                    hyperedge_quota=hyperedge_quota,
                                    max_evidence=max_evidence_per_hyperedge)
    # 超边证据句同样进入 evidence 列表：否则模型引用 H-xxxx-n 会被误判为非法引用，
    # 机制状态也无法绑定超边级证据。
    for h in hyperedges:
        for ev in (h.get("evidence") or [])[:max_evidence_per_hyperedge]:
            span = clean_str(ev.get("span_text"), "")
            if not span:
                continue
            evidence.append({
                "evidence_id": f"H-{int(h['hyperedge_id']):04d}-"
                               f"{(h.get('evidence') or []).index(ev) + 1}",
                "pattern_id": "",
                "hyperedge_id": h.get("reference_id"),
                "source": "hyperedge",
                "paper_key": clean_str(ev.get("paper_key"), "unknown"),
                "sentence": span,
                "confidence": h.get("confidence"),
                "evidence_tier": h.get("evidence_tier") or "unclassified",
            })
        all_papers.update(h.get("paper_keys") or [])

    year_rows: dict[str, Any] = {}
    if all_papers:
        ph = ",".join("?" * len(all_papers))
        year_rows = {
            r["paper_key"]: r["pub_year"]
            for r in conn.execute(
                f"SELECT paper_key, pub_year FROM papers "
                f"WHERE paper_key IN ({ph})", tuple(all_papers))
        }
    for e in evidence:
        e["year"] = year_rows.get(e["paper_key"])

    coverage = 0.0
    if patterns or hyperedges:
        avg_support = sum(p["support_count"] for p in patterns) / max(1, len(patterns))
        coverage = round(min(1.0, max(
            (len(patterns) + len(hyperedges))
            / max(1, int(mission.get("min_patterns") or 5)),
            avg_support / 3.0,
        )), 3)
    return {
        "corpus": {
            "papers": paper_count,
            **summary,
            "paper_keys": sorted(all_papers),
        },
        "patterns": patterns,
        "evidence": evidence,
        "hyperedges": hyperedges,
        "coverage_score": coverage,
    }


def _select_hyperedges(conn: sqlite3.Connection,
                       tokens: set[str],
                       min_confidence: float,
                       *,
                       hyperedge_quota: dict[str, int] | None = None,
                       max_evidence: int = 3) -> list[dict[str, Any]]:
    """按类型配额 + 相关性选出超边，再批量完整加载。"""
    quota = dict(DEFAULT_HYPEREDGE_QUOTA)
    if hyperedge_quota:
        quota.update({k: int(v) for k, v in hyperedge_quota.items() if v})
    briefs = ont.list_hyperedge_briefs(
        conn, min_confidence=min_confidence, limit=HYPEREDGE_BRIEF_SCAN)
    if not briefs:
        return []
    lexicon = _lex()
    mechanism_keywords = tuple(lexicon["mechanism_keywords"])
    condition_keywords = tuple(lexicon["condition_keywords"])
    low_value_labels = set(lexicon["low_value_relation_labels"])
    scored: list[tuple[float, dict[str, Any]]] = []
    for brief in briefs:
        label = brief.get("label") or ""
        names = " ".join(brief.get("member_names") or [])
        text = f"{label} {names}"
        score = float(score_text(tokens, label, 3) + score_text(tokens, names, 1))
        if brief.get("hyperedge_type") == "event":
            score += 1.5
        mech = _keyword_hits(label, mechanism_keywords)
        cond = _keyword_hits(label, condition_keywords)
        score += 2.0 * min(mech, 4)
        score += 0.5 * min(cond, 2)
        if brief.get("hyperedge_type") == "relation" and \
                label in low_value_labels:
            score -= 3.0
        score += 0.01 * float(brief.get("confidence") or 0)
        scored.append((score, brief))

    by_type: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for item in scored:
        by_type.setdefault(item[1].get("hyperedge_type") or "other", []).append(item)

    selected_ids: list[int] = []
    seen_ids: set[int] = set()
    for hyperedge_type, items in by_type.items():
        items.sort(key=lambda x: (-x[0], -int(x[1]["hyperedge_id"])))
        limit = quota.get(hyperedge_type)
        if limit is None:
            limit = quota.get("relation", 30) if hyperedge_type != "event" else quota["event"]
        for _, brief in items[:max(0, int(limit))]:
            hid = int(brief["hyperedge_id"])
            if hid not in seen_ids:
                seen_ids.add(hid)
                selected_ids.append(hid)

    loaded = ont.load_hyperedges(conn, selected_ids)
    out: list[dict[str, Any]] = []
    for h in loaded:
        members = h.get("members") or []
        conditions = h.get("conditions") or []
        measurements = h.get("measurements") or []
        label = h.get("label") or ""
        member_names = [str(m.get("name") or "") for m in members]
        text = " ".join([label, *member_names,
                         *[str(c.get("condition_key") or "") for c in conditions],
                         *[str(m.get("metric") or "") for m in measurements]])
        raw_evidence = h.get("evidence") or []
        evidence_ids: list[str] = []
        paper_keys: list[str] = []
        for ei, ev in enumerate(raw_evidence[:max_evidence]):
            span = clean_str(ev.get("span_text"), "")
            if not span:
                continue
            evidence_id = f"H-{int(h['hyperedge_id']):04d}-{ei + 1}"
            evidence_ids.append(evidence_id)
        for ev in raw_evidence:
            key = clean_str(ev.get("paper_key"))
            if key and key not in paper_keys:
                paper_keys.append(key)
        h["reference_id"] = f"H-{int(h['hyperedge_id']):04d}"
        h["label"] = label
        h["member_names"] = member_names
        h["evidence_ids"] = evidence_ids
        h["paper_keys"] = paper_keys
        h["support_count"] = len(paper_keys)
        h["relevance"] = score_text(tokens, label, 3) + _keyword_hits(
            text, mechanism_keywords)
        h["mechanism_score"] = _keyword_hits(text, mechanism_keywords)
        out.append(h)
    out.sort(key=lambda x: (x.get("mechanism_score", 0), x.get("relevance", 0),
                            x.get("support_count", 0), x.get("confidence", 0)),
             reverse=True)
    return out


def build_retrieval_request(plan: dict[str, Any] | None,
                            edge_gaps: Any = None) -> dict[str, Any]:
    plan = plan or {}
    mission = plan.get("mission") or {}
    retrieval_plan = plan.get("retrieval_plan") or {}
    terms = [
        clean_str(t) for t in retrieval_plan.get("query_variants") or []
        if clean_str(t)
    ]
    if not terms:
        terms = [clean_str(t) for t in mission.get("seed_terms") or []
                 if clean_str(t)]
    if not terms:
        terms = [clean_str(plan.get("domain"), "research")]
    max_results = int(
        retrieval_plan.get("max_results_per_query")
        or mission.get("max_results") or 80)
    request = {
        "reason": "按工作规划节点的检索计划收集可追溯证据",
        "seed_terms": terms,
        "max_results": max_results,
        "min_confidence": float(
            retrieval_plan.get("min_quality")
            or mission.get("min_confidence") or 0.6),
        "collection_mode": clean_str(mission.get("collection_mode"), "broad"),
        "domain_profile": normalize_domain_profile(
            plan.get("domain_profile"), plan.get("domain"),
            plan.get("goal") or ""),
        "retrieval_plan": retrieval_plan,
        "budget": plan.get("budget") or {},
        "suggested_route": "retrieval -> quality -> knowledge",
    }
    if edge_gaps:
        request["edge_gaps"] = normalize_edge_gaps(edge_gaps)
        request["reason"] = "知识消费节点识别出需要补强的低支持本体边"
    return build_skill_request(plan, edge_gaps=request.get("edge_gaps"))


CONSUMER_PROMPT = """你是科研知识消费节点。数据库查询、证据编号和追溯关系已经由代码准备好。
你的任务不是复述模式卡，而是把结构化知识转换成机制理解、研究边界和设计机会。

输入：
1. task_plan：Planner 生成的研究任务契约（含 design_contract，如创新等级下限、目标硬约束）；
2. knowledge：patterns、evidence、hyperedges（含 event 型机制超边、条件、测量和证据句）。

硬性规则：
- 只能使用输入中真实存在的 pattern_id、evidence_id 或 H-xxxx 超边编号；
- design_context 中每个条目都要带 evidence_ids 或 hyperedge_ids，指向真实编号；
- 需要新建条目时用 state_id / gap_id / op_id 编号（MS-0001 / GAP-0001 / OP-0001）；
- 严禁把描述性文字写进名为 *ids 的字段；描述文字放到普通文本字段；
- 不得补造机制、中间体、条件或文献结论；信息不足时写入 evidence_gaps；
- 区分已知事实、机制推断和未知缺口；
- 至少检查机制冲突、条件冲突、目标对象缺口和证据覆盖缺口；
- 若需要补检，返回 retrieval_requests，而不是假装已有证据；
- 只输出 JSON 对象，不要代码块和解释。

算子词表（operator_chain 中的 operator 必须取自下表；不要自造算子名）：
{operators}

输出结构：
{{
  "summary": "当前知识包的主要机制、边界和缺口摘要",
  "mechanism_clusters": [
    {{"name": "机制簇", "states": [], "evidence_ids": []}}
  ],
  "known_conflicts": [
    {{"description": "冲突", "evidence_ids": [], "severity": "high|medium|low"}}
  ],
  "evidence_gaps": [
    {{"description": "缺少什么证据", "why_needed": "为什么影响任务", "priority": "high|medium|low"}}
  ],
  "retrieval_requests": [
    {{"reason": "补检原因", "query_terms": [], "must_cover": [], "evidence_ids": []}}
  ],
  "design_context": {{
    "mechanism_states": [
      {{
        "state_id": "MS-0001",
        "start_state": "起始底物/起始态",
        "activation_mode": "活化方式",
        "intermediate": "关键中间体",
        "bond_changes": ["C-N formation", "C-C cleavage"],
        "selectivity_control": "区域/立体选择性来源",
        "catalyst_cycle": "催化循环或价态变化",
        "termination": "终止步骤",
        "known_side_reactions": [],
        "evidence_ids": ["E-0001-1"],
        "hyperedge_ids": ["H-0001"],
        "confidence": 0.0
      }}
    ],
    "reaction_primitives": [
      {{"op_id": "OP-0001", "name": "反应原语名称", "input_state": "输入",
        "output_state": "输出", "evidence_ids": [], "hyperedge_ids": []}}
    ],
    "opportunity_gaps": [
      {{"gap_id": "GAP-0001",
        "gap_type": "mechanism_target_gap|condition_conflict|missing_link|selectivity_unresolved",
        "known_mechanism": "已有机制", "unmet_target": "目标对象",
        "missing_link": "缺失环节", "risk": "主要风险",
        "evidence_ids": [], "hyperedge_ids": []}}
    ],
    "operator_candidates": [
      {{"op_id": "OP-0001", "name": "算子名（取自词表）",
        "operator_chain": [{{"operator": "polarity_reversal",
                            "input": "输入态", "output": "输出态"}}],
        "target": "该算子链针对的目标对象",
        "evidence_ids": [], "hyperedge_ids": []}}
    ],
    "constraint_conflicts": [
      {{"constraint": "Planner 硬约束", "conflict": "冲突点",
        "evidence_ids": [], "hyperedge_ids": []}}
    ]
  }},
  "confidence": 0.0
}}

task_plan:
{plan}

knowledge:
{knowledge}

请直接输出 JSON："""


def _compact_consumer_knowledge(knowledge: dict[str, Any],
                                max_patterns: int = 24,
                                max_evidence_per_pattern: int = 2,
                                max_hyperedges: int = 90,
                                max_evidence_per_hyperedge: int = 3,
                                max_chars: int = 120000) -> dict[str, Any]:
    """控制提示词预算：模式卡压缩、超边以 event 型优先、证据句按条附上。"""
    evidence_by_pattern: dict[str, list[dict[str, Any]]] = {}
    for e in knowledge.get("evidence") or []:
        evidence_by_pattern.setdefault(str(e.get("pattern_id")), []).append(e)
    pattern_cards = []
    for p in (knowledge.get("patterns") or [])[:max_patterns]:
        refs = evidence_by_pattern.get(str(p.get("pattern_id")), [])
        pattern_cards.append({
            "pattern_id": p.get("pattern_id"),
            "title": p.get("title"),
            "support_count": p.get("support_count"),
            "confidence": p.get("confidence"),
            "evidence_tier": p.get("evidence_tier"),
            "evidence": [
                {"evidence_id": e.get("evidence_id"),
                 "sentence": clean_str(e.get("sentence"), "")[:400]}
                for e in refs[:max_evidence_per_pattern]
            ],
        })
    hyperedge_cards = []
    for h in (knowledge.get("hyperedges") or [])[:max_hyperedges]:
        hyperedge_cards.append({
            "reference_id": h.get("reference_id"),
            "type": h.get("hyperedge_type"),
            "label": h.get("label"),
            "members": (h.get("member_names") or [])[:8],
            "conditions": [
                {k: c.get(k) for k in ("condition_key", "operator", "value_text",
                                       "value_num", "unit") if c.get(k) is not None}
                for c in (h.get("conditions") or [])[:6]
            ],
            "measurements": [
                {k: m.get(k) for k in ("metric", "value_text", "value_num", "unit")
                 if m.get(k) is not None}
                for m in (h.get("measurements") or [])[:6]
            ],
            "evidence": [
                {"evidence_id": f"H-{int(h['hyperedge_id']):04d}-{i + 1}",
                 "paper": ev.get("paper_key"),
                 "sentence": clean_str(ev.get("span_text"), "")[:400]}
                for i, ev in enumerate((h.get("evidence") or [])[:max_evidence_per_hyperedge])
                if clean_str(ev.get("span_text"), "")
            ],
            "confidence": h.get("confidence"),
        })
    corpus = dict(knowledge.get("corpus") or {})
    corpus.pop("paper_keys", None)
    compact = {
        "corpus": corpus,
        "coverage_score": knowledge.get("coverage_score", 0),
        "patterns": pattern_cards,
        "hyperedges": hyperedge_cards,
    }
    text = json.dumps(compact, ensure_ascii=False)
    while len(text) > max_chars and (compact["hyperedges"] or compact["patterns"]):
        if len(compact["hyperedges"]) > 30:
            compact["hyperedges"] = compact["hyperedges"][:len(compact["hyperedges"]) - 10]
        elif len(compact["patterns"]) > 8:
            compact["patterns"] = compact["patterns"][:len(compact["patterns"]) - 4]
        else:
            break
        text = json.dumps(compact, ensure_ascii=False)
    compact["_prompt_chars"] = len(text)
    compact["_truncated"] = len(text) >= max_chars
    return compact


def reference_ids(knowledge: dict[str, Any]) -> set[str]:
    """知识包内所有合法引用编号（模式卡、证据卡、超边及其证据句）。"""
    refs = {
        str(p.get("pattern_id") or "")
        for p in knowledge.get("patterns") or []
        if p.get("pattern_id")
    }
    refs |= {
        str(e.get("evidence_id") or "")
        for e in knowledge.get("evidence") or []
        if e.get("evidence_id")
    }
    for h in knowledge.get("hyperedges") or []:
        if h.get("hyperedge_id") is None:
            continue
        hid = int(h["hyperedge_id"])
        refs.add(f"H-{hid:04d}")
        for ei in range(len(h.get("evidence") or [])):
            refs.add(f"H-{hid:04d}-{ei + 1}")
        # 超边自带的 evidence_ids 已在 _select_hyperedges 中生成，一并纳入
        refs |= {str(x) for x in h.get("evidence_ids") or [] if x}
    return {r for r in refs if r}


_REF_KEY_HINT = re.compile(r"(?:^|_)(?:ids?|evidence|references?|hyperedges?)$", re.I)


def _is_ref_key(key: str) -> bool:
    return bool(_REF_KEY_HINT.search(str(key or "")))


def _sanitize_references(value: Any, known: set[str],
                         parent_key: str = "") -> tuple[Any, list[str], list[str]]:
    """只校验“ID 形状”的字符串。

    返回 ``(清理后的值, 无效引用, 放错位置的描述文本)``。描述性文本不再被
    当成非法引用删除，而是单独回收，避免把模型的机制设想静默丢掉。
    """
    invalid: list[str] = []
    misplaced: list[str] = []
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            clean_item, bad, lost = _sanitize_references(item, known, str(key))
            out[key] = clean_item
            invalid.extend(bad)
            misplaced.extend(lost)
        return out, invalid, misplaced
    if isinstance(value, list):
        if value and all(isinstance(x, str) for x in value) and _is_ref_key(parent_key):
            kept: list[str] = []
            for item in value:
                text = str(item).strip()
                if not text:
                    continue
                if text in known:
                    kept.append(text)
                elif is_reference_id(text):
                    invalid.append(text)
                else:
                    misplaced.append(text)
            return list(dict.fromkeys(kept)), invalid, misplaced
        out_list = []
        for item in value:
            clean_item, bad, lost = _sanitize_references(item, known, parent_key)
            out_list.append(clean_item)
            invalid.extend(bad)
            misplaced.extend(lost)
        return out_list, invalid, misplaced
    return value, invalid, misplaced


def _string_list(value: Any, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [clean_str(x) for x in value if isinstance(x, (str, int, float))
           and clean_str(x)]
    return list(dict.fromkeys(out))[:limit]


def normalize_consumer_output(data: dict[str, Any] | None,
                              knowledge: dict[str, Any],
                              plan: dict[str, Any]) -> dict[str, Any]:
    raw = data or {}
    known = reference_ids(knowledge)
    sanitized, invalid, misplaced = _sanitize_references(raw, known)
    design_context = sanitized.get("design_context")
    if not isinstance(design_context, dict):
        design_context = {}
    design_context = normalize_design_context(design_context, knowledge)
    try:
        confidence = min(1.0, max(0.0, float(sanitized.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0

    def list_value(key: str) -> list[Any]:
        value = sanitized.get(key)
        return value if isinstance(value, list) else []

    analysis = {
        "summary": clean_str(sanitized.get("summary")),
        "mechanism_clusters": list_value("mechanism_clusters"),
        "known_conflicts": list_value("known_conflicts"),
        "evidence_gaps": list_value("evidence_gaps"),
        "retrieval_requests": list_value("retrieval_requests"),
        "unattributed_notes": list(dict.fromkeys(misplaced))[:20],
        "confidence": confidence,
        "invalid_references": sorted(set(invalid)),
        "consumer_mode": "llm",
    }
    return {
        "consumer_analysis": analysis,
        "design_context": design_context,
    }


# ---------------------------------------------------------------- 离线兜底设计上下文
#
# 本节的机制/条件/算子规则全部来自可加载的机制词表
# （`packs/skills/mechanism-keywords` + `packs/domains/<kind>/vocab/mechanism.jsonl`），
# 代码里不再内联任何学科词表。缺失时由 mechanism_lexicon 显式告警。


class MechanismRules:
    """一次机制抽取所用的规则集合（词表快照）。"""

    def __init__(self, domain_kind: str = "") -> None:
        lex = _lex(domain_kind)
        self.domain_kind = domain_kind
        self.mechanism_keywords: tuple[str, ...] = tuple(lex["mechanism_keywords"])
        self.condition_keywords: tuple[str, ...] = tuple(lex["condition_keywords"])
        self.low_value_labels: set[str] = set(lex["low_value_relation_labels"])
        self.bond_change_rules: tuple[str, ...] = tuple(lex["bond_change_rules"])
        self.hard_constraint_hints: tuple[str, ...] = tuple(lex["hard_constraint_hints"])
        self.activation_rules = tuple(lex["activation_rules"])
        self.intermediate_rules = tuple(lex["intermediate_rules"])
        self.selectivity_rules = tuple(lex["selectivity_rules"])
        self.risk_rules = tuple(lex["risk_rules"])
        self.operator_keywords: dict[str, tuple[str, ...]] = {
            k: tuple(v) for k, v in lex["operator_keywords"].items()}

    # --- 规则匹配 -------------------------------------------------
    def match(self, text: str, rules) -> str:
        low = (text or "").lower()
        for name, keys in rules:
            if any(k in low for k in keys):
                return str(name)
        return ""

    def hits(self, text: str, rules) -> list[str]:
        low = (text or "").lower()
        return [str(name) for name, keys in rules if any(k in low for k in keys)]

    def bond_changes(self, text: str) -> list[str]:
        low = (text or "").lower()
        return [b for b in self.bond_change_rules if b.lower() in low][:4]

    def keyword_hits(self, text: str, keywords) -> int:
        low = (text or "").lower()
        return sum(1 for k in keywords if k in low)

    def looks_mechanistic(self, text: str) -> int:
        low = (text or "").lower()
        return (3 * self.keyword_hits(low, self.mechanism_keywords)
                + 2 * self.keyword_hits(low, self.condition_keywords)
                + len(self.bond_changes(low)))

    def mechanism_hypothesis(self, text: str) -> str:
        parts = [x for x in (self.match(text, self.activation_rules),
                             self.match(text, self.intermediate_rules)) if x]
        return " + ".join(parts)

    def operator_names_for(self, text: str) -> list[str]:
        low = (text or "").lower()
        return [name for name, keys in self.operator_keywords.items()
                if any(k in low for k in keys)]


def _rules_for(plan: dict[str, Any] | None) -> MechanismRules:
    plan = plan or {}
    domain_kind = str((plan.get("domain_profile") or {}).get("domain_kind")
                      or plan.get("domain_kind") or "")
    return MechanismRules(domain_kind)


def _rows_for_hyperedge(hyperedge: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    hid = hyperedge.get("hyperedge_id")
    if hid is None:
        return rows
    base = f"H-{int(hid):04d}"
    for i, ev in enumerate(hyperedge.get("evidence") or []):
        span = clean_str(ev.get("span_text"), "")
        if not span:
            continue
        rows.append({
            "reference_id": f"{base}-{i + 1}",
            "text": span,
            "paper_key": clean_str(ev.get("paper_key")),
        })
    return rows


def _mechanism_states(items: list[dict[str, Any]], rules: "MechanismRules",
                      limit: int = 12) -> list[dict[str, Any]]:
    """确定性机制状态抽取：从 event 超边与证据句归纳，全部绑定真实编号。"""
    candidates: list[dict[str, Any]] = []
    for h in items:
        label = clean_str(h.get("label"), "")
        if not label:
            continue
        refs = [h.get("reference_id")] if h.get("reference_id") else []
        refs += list(h.get("evidence_ids") or [])
        rows = _rows_for_hyperedge(h)
        pooled = " ".join([label, *[r["text"] for r in rows]])
        refs += [r["reference_id"] for r in rows]
        activation = rules.match(pooled, rules.activation_rules)
        intermediate = rules.match(pooled, rules.intermediate_rules)
        selectivity = rules.match(pooled, rules.selectivity_rules)
        bonds = rules.bond_changes(pooled)
        if not (activation or intermediate or bonds or selectivity):
            continue
        candidates.append({
            "start_state": " / ".join((h.get("member_names") or [])[:3]) or label,
            "activation_mode": activation,
            "intermediate": intermediate,
            "bond_changes": bonds,
            "selectivity_control": selectivity,
            "catalyst_cycle": rules.match(pooled, rules.activation_rules),
            "termination": "",
            "known_side_reactions": rules.hits(pooled, rules.risk_rules),
            "label": label,
            "score": rules.looks_mechanistic(pooled),
            "evidence_ids": [r for r in refs if is_reference_id(r)][:6],
        })
    candidates.sort(key=lambda x: (x["score"], len(x["evidence_ids"])), reverse=True)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in candidates:
        key = f"{c['activation_mode']}|{c['intermediate']}|{','.join(c['bond_changes'])}"
        if key in seen or not c["evidence_ids"]:
            continue
        seen.add(key)
        out.append({
            "state_id": f"MS-{len(out) + 1:04d}",
            "start_state": c["start_state"],
            "activation_mode": c["activation_mode"],
            "intermediate": c["intermediate"],
            "bond_changes": c["bond_changes"],
            "selectivity_control": c["selectivity_control"],
            "catalyst_cycle": c["catalyst_cycle"],
            "termination": c["termination"],
            "known_side_reactions": c["known_side_reactions"],
            "label": c["label"],
            "evidence_ids": c["evidence_ids"],
            "hyperedge_ids": [e for e in c["evidence_ids"] if e.startswith("H-")],
            "confidence": 0.5 if c["intermediate"] else 0.35,
        })
        if len(out) >= limit:
            break
    return out


def _reaction_primitives(items: list[dict[str, Any]],
                         states: list[dict[str, Any]],
                         rules: "MechanismRules",
                         limit: int = 15) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for state in states:
        blob = " ".join([
            state.get("activation_mode") or "", state.get("intermediate") or "",
            state.get("selectivity_control") or "", state.get("label") or "",
            " ".join(state.get("bond_changes") or []),
        ])
        for name in rules.operator_names_for(blob):
            if any(x["name"] == name for x in out):
                continue
            out.append({
                "op_id": f"OP-{len(out) + 1:04d}",
                "name": name,
                "label_zh": OPERATOR_NAMES.get(name, name),
                "input_state": state.get("start_state") or state.get("label") or "",
                "output_state": state.get("intermediate")
                or (state.get("bond_changes") or [""])[0],
                "evidence_ids": state.get("evidence_ids") or [],
                "hyperedge_ids": state.get("hyperedge_ids") or [],
            })
            break
    if out:
        return out[:limit]
    for h in items:
        label = clean_str(h.get("label"), "")
        if not label:
            continue
        out.append({
            "op_id": f"OP-{len(out) + 1:04d}",
            "name": "unclassified_transform",
            "label_zh": "未归类变换",
            "input_state": " / ".join((h.get("member_names") or [])[:3]),
            "output_state": label,
            "evidence_ids": list(h.get("evidence_ids") or [])[:4],
            "hyperedge_ids": [h.get("reference_id")] if h.get("reference_id") else [],
        })
        if len(out) >= limit:
            break
    return out


def _opportunity_gaps(items: list[dict[str, Any]],
                      states: list[dict[str, Any]],
                      plan: dict[str, Any] | None,
                      limit: int = 8) -> list[dict[str, Any]]:
    """机会缺口挖掘：已知机制但目标未覆盖 / 条件冲突 / 选择性未解决。"""
    plan = plan or {}
    constraints = ((plan.get("design_contract") or {}).get("target_constraints")
                   or {})
    hard = _string_list(constraints.get("hard_constraints"), limit=10)
    objective = clean_str(constraints.get("must_explain") and (
        (plan.get("design_contract") or {}).get("objective")),
        clean_str(plan.get("goal"), ""))
    corpus_text = " ".join([objective, *hard]).lower()
    # 目标是否要求多氮骨架：从硬约束/目标里判断（领域无关的关键词表来自词表包）
    nitrogen_hints = tuple(_lex().get("heteroatom_hints") or
                           ("氮", "氮原子", "nitrogen", "aza", "diaza", "polyaza"))
    wants_multi_nitrogen = any(h in corpus_text for h in nitrogen_hints)
    out: list[dict[str, Any]] = []
    for state in states:
        refs = list(state.get("evidence_ids") or [])
        if not refs:
            continue
        missing = ""
        # 只有"目标要求多氮"且"已抽取机制确实只涉及单氮引入"时才报该缺口。
        # 旧实现是 `any(... for x in [])`（对空列表求 any 恒 False，取反恒 True），
        # 会把这条缺口无条件加到每个机制状态上（P1-4）。
        if wants_multi_nitrogen and not _mentions_multi_nitrogen(state):
            missing = "目标要求多氮骨架，但已抽取机制均只涉及单个氮引入步骤"
        if state.get("intermediate") and not state.get("termination"):
            missing = missing or "缺少终止步骤与副反应信息，难以判断该中间体能否被定向捕获"
        out.append({
            "gap_id": f"GAP-{len(out) + 1:04d}",
            "gap_type": "mechanism_target_gap" if missing else "selectivity_unresolved",
            "known_mechanism": " + ".join(
                [x for x in (state.get("activation_mode"), state.get("intermediate")) if x])
            or state.get("label") or "",
            "unmet_target": objective,
            "missing_link": missing or "缺少面向目标骨架的成环/串联证据",
            "risk": (state.get("known_side_reactions") or ["条件兼容性未知"])[0],
            "evidence_ids": refs[:4],
            "hyperedge_ids": list(state.get("hyperedge_ids") or [])[:4],
        })
        if len(out) >= limit:
            break
    # 条件冲突：有测量/条件但缺关键条件时显式标出
    for state in states:
        if state.get("intermediate") and not state.get("known_side_reactions"):
            continue
        if len(out) >= limit:
            break
        out.append({
            "gap_id": f"GAP-{len(out) + 1:04d}",
            "gap_type": "condition_conflict",
            "known_mechanism": state.get("label") or "",
            "unmet_target": objective,
            "missing_link": "关键中间体的条件窗口（温度/溶剂/当量）未结构化，无法核对兼容性",
            "risk": "条件不兼容可能导致候选方案不可执行",
            "evidence_ids": list(state.get("evidence_ids") or [])[:4],
            "hyperedge_ids": list(state.get("hyperedge_ids") or [])[:4],
        })
    return out


def _operator_candidates(states: list[dict[str, Any]],
                         primitives: list[dict[str, Any]],
                         rules: "MechanismRules",
                         limit: int = 8) -> list[dict[str, Any]]:
    """算子链候选：把机制状态串成"起始→中间体→选择性锁定"的算子序列。"""
    out: list[dict[str, Any]] = []
    for state in states:
        chain: list[dict[str, Any]] = []
        blob = " ".join([
            state.get("activation_mode") or "", state.get("intermediate") or "",
            state.get("label") or "", " ".join(state.get("bond_changes") or []),
        ]).lower()
        for name in rules.operator_names_for(blob):
            chain.append({"operator": name,
                          "operator_label": OPERATOR_NAMES.get(name, name),
                          "input": state.get("start_state") or "",
                          "output": state.get("intermediate")
                          or (state.get("bond_changes") or [""])[0]})
        if state.get("selectivity_control"):
            name = "selectivity_lock"
            chain.append({"operator": name,
                          "operator_label": OPERATOR_NAMES.get(name, name),
                          "input": state.get("intermediate") or "",
                          "output": state.get("selectivity_control") or ""})
        if not chain:
            continue
        chain, validation = normalize_operator_chain(chain)
        if not chain:
            continue
        out.append({
            "op_id": f"OP-{len(out) + 1:04d}",
            "name": "+".join(x["operator"] for x in chain),
            "operator_chain": chain,
            "target": state.get("start_state") or "",
            "operator_chain_validation": validation,
            "evidence_ids": list(state.get("evidence_ids") or [])[:4],
            "hyperedge_ids": list(state.get("hyperedge_ids") or [])[:4],
        })
        if len(out) >= limit:
            break
    if out:
        return out
    # 退化：用反应原语给出单算子链，保证字段非空且可校验
    for primitive in primitives[:limit]:
        chain, validation = normalize_operator_chain([{
            "operator": primitive.get("name") or "unclassified_transform",
            "input": primitive.get("input_state") or "",
            "output": primitive.get("output_state") or "",
        }])
        if not chain:
            continue
        out.append({
            "op_id": f"OP-{len(out) + 1:04d}",
            "name": chain[0]["operator"],
            "operator_chain": chain,
            "target": primitive.get("input_state") or "",
            "operator_chain_validation": validation,
            "evidence_ids": list(primitive.get("evidence_ids") or [])[:4],
            "hyperedge_ids": list(primitive.get("hyperedge_ids") or [])[:4],
        })
    return out


def _mentions_multi_nitrogen(state: dict[str, Any]) -> bool:
    """判断某个机制状态是否已经描述了"多氮/第二氮"的引入。

    依据机制状态自身的文本（标签、中间体、键变化、侧反应），而不是空列表。
    """
    blob = " ".join([
        clean_str(state.get("label"), ""),
        clean_str(state.get("intermediate"), ""),
        clean_str(state.get("start_state"), ""),
        " ".join(str(x) for x in state.get("bond_changes") or []),
    ]).lower()
    hints = ("n-n", "diaza", "diazo", "diazine", "pyrimidine", "pyrazine",
             "pyridazine", "triazine", "bis-nitrogen", "second nitrogen",
             "两个氮", "双氮", "二氮", "多氮", "另一个氮", "第二氮", "氮插入")
    return any(h in blob for h in hints)


def _constraint_conflicts(states: list[dict[str, Any]],
                          items: list[dict[str, Any]],
                          plan: dict[str, Any] | None,
                          rules: "MechanismRules",
                          limit: int = 8) -> list[dict[str, Any]]:
    plan = plan or {}
    constraints = ((plan.get("design_contract") or {}).get("target_constraints")
                   or {})
    hard = _string_list(constraints.get("hard_constraints"), limit=10)
    out: list[dict[str, Any]] = []
    for state in states:
        refs = list(state.get("evidence_ids") or [])
        blob = " ".join([state.get("label") or "", state.get("intermediate") or ""])
        risks = state.get("known_side_reactions") or []
        if not risks and not refs:
            continue
        out.append({
            "constraint": "；".join(hard) or clean_str(plan.get("goal"), ""),
            "conflict": (risks[0] if risks
                         else "现有证据仅有单篇支持，尚不足以确认该机制可迁移到目标骨架"),
            "evidence_ids": refs[:4],
            "hyperedge_ids": list(state.get("hyperedge_ids") or [])[:4],
        })
        if len(out) >= limit:
            break
    for h in items[:limit]:
        label = clean_str(h.get("label"), "")
        mech = rules.keyword_hits(label, rules.mechanism_keywords)
        cond_missing = not (h.get("conditions") or [])
        if not mech or not cond_missing:
            continue
        out.append({
            "constraint": "需要可执行的反应条件",
            "conflict": f"「{label[:60]}」缺少结构化条件（温度/溶剂/当量），无法核对硬约束",
            "evidence_ids": list(h.get("evidence_ids") or [])[:4],
            "hyperedge_ids": [h.get("reference_id")] if h.get("reference_id") else [],
        })
        if len(out) >= limit:
            break
    return out


def deterministic_design_context(knowledge: dict[str, Any],
                                 plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """离线兜底：用同一套五键结构从超边/模式卡归纳设计上下文。

    规则来自 `packs`：通用词表 + 由 `plan.domain_profile.domain_kind` 选定的领域增补。
    """
    rules = _rules_for(plan)
    items = sorted(knowledge.get("hyperedges") or [],
                   key=lambda h: (h.get("mechanism_score", 0),
                                  h.get("relevance", 0),
                                  h.get("support_count", 0)), reverse=True)
    states = _mechanism_states(items, rules)
    primitives = _reaction_primitives(items, states, rules)
    gaps = _opportunity_gaps(items, states, plan)
    ops = _operator_candidates(states, primitives, rules)
    conflicts = _constraint_conflicts(states, items, plan, rules)
    return normalize_design_context({
        "mechanism_states": states,
        "reaction_primitives": primitives,
        "opportunity_gaps": gaps,
        "operator_candidates": ops,
        "constraint_conflicts": conflicts,
        "mode": "deterministic",
    }, knowledge)


def _normalize_state(item: Any, index: int) -> dict[str, Any] | None:
    if isinstance(item, str):
        return {"state_id": f"MS-{index:04d}", "label": item,
                "evidence_ids": [], "hyperedge_ids": [], "confidence": 0.0}
    if not isinstance(item, dict):
        return None
    out = dict(item)
    out["state_id"] = clean_str(out.get("state_id"), f"MS-{index:04d}")
    for key in ("bond_changes", "known_side_reactions"):
        out[key] = _string_list(out.get(key), limit=12)
    out["evidence_ids"] = _string_list(out.get("evidence_ids"), limit=12)
    out["hyperedge_ids"] = _string_list(out.get("hyperedge_ids"), limit=12)
    if not out["hyperedge_ids"]:
        out["hyperedge_ids"] = [x for x in out["evidence_ids"] if x.startswith("H-")]
    try:
        out["confidence"] = min(1.0, max(0.0, float(out.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    # 机制状态必须绑定真实编号；无法溯源的状态降级为描述性条目但仍保留
    out["traceable"] = bool(out["evidence_ids"] or out["hyperedge_ids"])
    return out


def _normalize_gap(item: Any, index: int) -> dict[str, Any] | None:
    if isinstance(item, str):
        return {"gap_id": f"GAP-{index:04d}", "missing_link": item,
                "gap_type": "unclassified", "evidence_ids": [], "hyperedge_ids": []}
    if not isinstance(item, dict):
        return None
    out = dict(item)
    out["gap_id"] = clean_str(out.get("gap_id"), f"GAP-{index:04d}")
    out["gap_type"] = clean_str(out.get("gap_type"), "unclassified")
    out["evidence_ids"] = _string_list(out.get("evidence_ids"), limit=12)
    out["hyperedge_ids"] = _string_list(out.get("hyperedge_ids"), limit=12)
    out["traceable"] = bool(out["evidence_ids"] or out["hyperedge_ids"])
    return out


def _normalize_operator_candidate(item: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    out = dict(item)
    out["op_id"] = clean_str(out.get("op_id"), f"OP-{index:04d}")
    chain, validation = normalize_operator_chain(out.get("operator_chain"))
    if not chain and clean_str(out.get("name")) in OPERATOR_NAMES:
        chain, validation = normalize_operator_chain(
            [{"operator": out["name"], "input": out.get("input_state") or "",
              "output": out.get("output_state") or ""}])
    out["operator_chain"] = chain
    out["operator_chain_validation"] = validation
    out["evidence_ids"] = _string_list(out.get("evidence_ids"), limit=12)
    out["hyperedge_ids"] = _string_list(out.get("hyperedge_ids"), limit=12)
    out["traceable"] = bool(out["evidence_ids"] or out["hyperedge_ids"])
    return out


def normalize_design_context(data: dict[str, Any] | None,
                             knowledge: dict[str, Any] | None = None) -> dict[str, Any]:
    """把 LLM 或兜底结果统一成稳定五键结构，并保留可追溯标记。"""
    raw = data if isinstance(data, dict) else {}
    states: list[dict[str, Any]] = []
    for i, item in enumerate(raw.get("mechanism_states") or [], start=1):
        state = _normalize_state(item, i)
        if state:
            states.append(state)
    primitives: list[dict[str, Any]] = []
    for i, item in enumerate(raw.get("reaction_primitives") or [], start=1):
        if isinstance(item, str):
            primitives.append({"op_id": f"OP-{i:04d}", "name": item,
                               "label_zh": "", "input_state": "", "output_state": "",
                               "evidence_ids": [], "hyperedge_ids": [],
                               "traceable": False})
            continue
        if not isinstance(item, dict):
            continue
        prim = dict(item)
        prim["op_id"] = clean_str(prim.get("op_id"), f"OP-{i:04d}")
        prim["name"] = clean_str(prim.get("name"), "unclassified_transform")
        prim["evidence_ids"] = _string_list(prim.get("evidence_ids"), limit=12)
        prim["hyperedge_ids"] = _string_list(prim.get("hyperedge_ids"), limit=12)
        prim["traceable"] = bool(prim["evidence_ids"] or prim["hyperedge_ids"])
        primitives.append(prim)
    gaps: list[dict[str, Any]] = []
    for i, item in enumerate(raw.get("opportunity_gaps") or [], start=1):
        gap = _normalize_gap(item, i)
        if gap:
            gaps.append(gap)
    ops: list[dict[str, Any]] = []
    for i, item in enumerate(raw.get("operator_candidates") or [], start=1):
        op = _normalize_operator_candidate(item, i)
        if op:
            ops.append(op)
    conflicts: list[dict[str, Any]] = []
    for item in raw.get("constraint_conflicts") or []:
        if isinstance(item, str):
            conflicts.append({"constraint": "", "conflict": item,
                              "evidence_ids": [], "hyperedge_ids": [],
                              "traceable": False})
            continue
        if not isinstance(item, dict):
            continue
        conf = dict(item)
        conf["evidence_ids"] = _string_list(conf.get("evidence_ids"), limit=12)
        conf["hyperedge_ids"] = _string_list(conf.get("hyperedge_ids"), limit=12)
        conf["traceable"] = bool(conf["evidence_ids"] or conf["hyperedge_ids"])
        conflicts.append(conf)
    traceable = sum(1 for x in [*states, *primitives, *gaps, *ops, *conflicts]
                    if x.get("traceable"))
    total = len(states) + len(primitives) + len(gaps) + len(ops) + len(conflicts)
    return {
        "mechanism_states": states,
        "reaction_primitives": primitives,
        "opportunity_gaps": gaps,
        "operator_candidates": ops,
        "constraint_conflicts": conflicts,
        "mode": clean_str(raw.get("mode"), "llm"),
        "traceability": {
            "total_items": total,
            "traceable_items": traceable,
            "ratio": round(traceable / total, 3) if total else 0.0,
        },
    }


def build_design_context(knowledge: dict[str, Any],
                         plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """兼容入口：离线设计上下文现在与 LLM 路径同结构。"""
    return deterministic_design_context(knowledge, plan)


def make_knowledge_consumer_node(conn: sqlite3.Connection | None = None,
                                 settings: Settings | None = None,
                                 collector: Callable[[dict[str, Any]], dict] | None = None,
                                 model: Any = None):
    """构造 LLM 驱动的知识消费节点；collector 参数仅为旧接口兼容保留。"""
    settings = settings or default_settings
    del collector

    def knowledge_consumer_node(state: dict) -> dict:
        plan = state.get("plan") or {}
        mission = plan.get("mission") or {}
        run_id = state.get("run_id")
        own_conn = conn is None
        db = conn or connect(settings.db_path)
        try:
            log_study_event(
                conn, settings, "knowledge_consumer", run_id, "running",
                {"domain": plan.get("domain"), "mode": "llm" if model else "offline"})
            bundle = mine_ontology_evidence(db, mission)
            if not bundle["patterns"] and not bundle.get("hyperedges"):
                retrieval_request = build_retrieval_request(plan)
                log_study_event(
                    conn, settings, "knowledge_consumer", run_id,
                    "needs_collection",
                    {"patterns": 0, "evidence": 0,
                     "reason": retrieval_request.get("reason")})
                return {
                    "knowledge": bundle,
                    "retrieval_request": retrieval_request,
                    "status": "needs_collection",
                }

            if model is None:
                design_context = deterministic_design_context(bundle, plan)
                consumer_analysis = {
                    "summary": "离线模式：未调用 LLM，仅生成确定性设计上下文。",
                    "mechanism_clusters": [],
                    "known_conflicts": [],
                    "evidence_gaps": [],
                    "retrieval_requests": [],
                    "unattributed_notes": [],
                    "confidence": 0.0,
                    "invalid_references": [],
                    "consumer_mode": "offline_fallback",
                }
            else:
                prompt = CONSUMER_PROMPT.replace(
                    "{operators}", operator_catalog_for_prompt()
                ).replace(
                    "{plan}", json.dumps(plan, ensure_ascii=False, indent=2)
                ).replace(
                    "{knowledge}",
                    json.dumps(_compact_consumer_knowledge(bundle),
                               ensure_ascii=False, indent=2),
                )
                try:
                    raw, call_diag = invoke_with_timeout(
                        model, prompt,
                        timeout=getattr(settings, "study_consumer_timeout", 600))
                    if raw is None:
                        raise RuntimeError(call_diag.get("error") or "模型调用失败")
                    parsed = parse_json_object(raw)
                    normalized = normalize_consumer_output(parsed, bundle, plan)
                    consumer_analysis = normalized["consumer_analysis"]
                    design_context = normalized["design_context"]
                    consumer_analysis["model_call"] = call_diag
                except Exception as exc:  # noqa: BLE001
                    logger.warning("知识消费节点 LLM 调用失败: %s", exc)
                    log_study_event(
                        conn, settings, "knowledge_consumer", run_id, "failed",
                        {"error": str(exc)})
                    return {
                        "knowledge": bundle,
                        "status": "consumer_failed",
                        "error": str(exc),
                    }
                if not design_context.get("mechanism_states") and \
                        not design_context.get("operator_candidates"):
                    # 模型没给出可用设计上下文时，用确定性层补齐，而不是留空
                    fallback = deterministic_design_context(bundle, plan)
                    for key in ("mechanism_states", "reaction_primitives",
                                "opportunity_gaps", "operator_candidates",
                                "constraint_conflicts"):
                        if not design_context.get(key):
                            design_context[key] = fallback[key]
                    # 补齐后重新规范化，否则 traceability 仍按补齐前的空集合计算
                    design_context = normalize_design_context(design_context, bundle)
                    design_context["mode"] = "llm+deterministic_fill"

            bundle["consumer_analysis"] = consumer_analysis
            bundle["design_context"] = design_context
            retrieval_requests = consumer_analysis.get("retrieval_requests") or []
            retrieval_request = _merge_retrieval_requests(retrieval_requests, plan)
            log_study_event(
                conn, settings, "knowledge_consumer", run_id, "done",
                {
                    "patterns": len(bundle.get("patterns") or []),
                    "evidence": len(bundle.get("evidence") or []),
                    "hyperedges": len(bundle.get("hyperedges") or []),
                    "coverage_score": bundle.get("coverage_score"),
                    "consumer_mode": consumer_analysis.get("consumer_mode"),
                    "confidence": consumer_analysis.get("confidence"),
                    "invalid_references": len(
                        consumer_analysis.get("invalid_references") or []),
                    "mechanism_states": len(
                        design_context.get("mechanism_states") or []),
                    "operator_candidates": len(
                        design_context.get("operator_candidates") or []),
                    "traceability": design_context.get("traceability"),
                    "retrieval_requests": len(retrieval_requests),
                })
            return {
                "knowledge": bundle,
                "retrieval_request": retrieval_request,
                "status": "consumed",
            }
        finally:
            if own_conn:
                db.close()

    return knowledge_consumer_node


def _merge_retrieval_requests(requests: Any, plan: dict[str, Any]) -> dict[str, Any] | None:
    """把多条补检请求合并成一条可执行的检索请求（不丢任何一条的查询词）。"""
    items = [r for r in (requests or []) if isinstance(r, dict)]
    if not items:
        return None
    terms: list[str] = []
    must_cover: list[str] = []
    reasons: list[str] = []
    evidence_ids: list[str] = []
    for item in items:
        for term in (item.get("query_terms") or item.get("seed_terms") or []):
            text = clean_str(term)
            if text and text not in terms:
                terms.append(text)
        for cover in item.get("must_cover") or []:
            text = clean_str(cover)
            if text and text not in must_cover:
                must_cover.append(text)
        if clean_str(item.get("reason")):
            reasons.append(clean_str(item["reason"]))
        for ref in item.get("evidence_ids") or []:
            text = clean_str(ref)
            if text and text not in evidence_ids:
                evidence_ids.append(text)
    max_terms = int(((plan.get("budget") or {}).get("max_retrieval_terms")) or 12)
    return {
        "reason": "；".join(reasons[:4]) or "知识消费节点请求补检",
        "query_terms": terms[:max_terms],
        "must_cover": must_cover[:8],
        "evidence_ids": evidence_ids[:12],
        "request_count": len(items),
    }
