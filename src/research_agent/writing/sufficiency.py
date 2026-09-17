"""章节级充分性判定：当前库数据能否支撑这一段的写作。

设计要点（方案 §3.2）：

- **显式且可解释**：每个维度给出分数、实际值与阈值，``reasons`` 直接渲染到界面的决策轨迹里；
  上游的同类判断隐含在消费节点的 ``retrieval_requests`` 中，看不出"为什么不足"。
- **确定性优先 + LLM 可选**：无模型时纯确定性（默认路径可用）；有模型时由调用方传入
  ``llm_analysis``，两者**取保守者**（任一判不足即为不足），避免模型乐观放行。
- **不猜**：任何维度缺数据记 0 分并在 reasons 里说明缺什么，而不是给中性分。
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from research_agent.config import Settings, settings as default_settings

__all__ = ["evaluate_sufficiency", "tokenize", "cited_count_hint",
           "DIMENSION_LABELS"]

#: 维度中文名（界面直接显示）
DIMENSION_LABELS = {
    "papers": "命中文献",
    "knowledge": "知识覆盖",
    "conditions": "量化条件",
    "requirements": "指令要求覆盖",
    "evidence": "可用证据",
}

#: 停用词（中英混合，仅用于需求词元提取）
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "for", "with", "to", "in", "on", "by",
    "this", "that", "these", "those", "is", "are", "be", "as", "at", "from",
    "write", "section", "paragraph", "paper", "please", "summarise", "summarize",
    "review", "describe", "discuss", "should", "must", "need", "using", "use",
    # 引用指令的常见虚词：这些是"怎么写"的措辞，不是"要写什么"的内容要求。
    # 实测：cite/least/source/highlight 曾被当成未覆盖要求，把覆盖率从 4/5 压到 4/9。
    "cite", "citing", "cited", "citation", "citations", "least", "source",
    "sources", "reference", "references", "highlight", "emphasise", "emphasize",
    "focus", "include", "including", "provide", "given", "only",
    "请", "写", "段落", "本节", "这一节", "要求", "必须", "需要", "以及", "并且",
    "使用", "基于", "关于", "内容", "部分", "引用", "强调", "突出", "重点",
}

#: 通用章节标题（供界面/调用方复用；要求校验本身已不再纳入标题）
_GENERIC_HEADINGS = {
    "摘要", "引言", "方法", "结果", "讨论", "结论", "参考文献", "背景", "展望",
    "范围与方法", "研究版图与分类", "机制与设计原则", "开放问题与机会缺口",
    "研究目标", "实验设计", "分组与对照", "参数与条件", "观察指标", "风险与备选方案",
    "立项依据", "研究目标与内容", "研究方案与技术路线", "可行性分析", "进度安排",
    "总体回应", "逐条回应", "修改清单",
}

#: 含这些动作/数量词的词元不是"要求"，而是写作指令本身 → 抠掉后剩余部分才算要求
_INSTRUCTION_MARKERS = (
    "只写", "仅写", "只要写", "强调", "突出", "引用", "不少于", "不少", "至少",
    "不超过", "最多", "尽量", "注意", "避免", "重点", "围绕", "结合", "给出",
    "列出", "说明", "阐述", "梳理", "对比", "不少于", "要求", "请",
)

#: 脚手架/元话语：即使长短像"要求"，也不是可检索的主题词（实测这些词把要求
#: 覆盖率拉成 0/7，因为知识库里当然不存在"为学术论文的"这种词）
_BOILERPLATE = (
    "学术论文", "论文主题", "检索规划", "检索", "规划", "一节", "小节", "本节",
    "该节", "写作要求", "用户在节点上的指令", "节点指令", "只围绕", "不要规划",
    "整篇", "材料", "证据做", "做检索", "论文的",
)

#: 需求词元：英文 ≥3 字母 / 中文 ≥2 字
_TOKEN_RE = re.compile(r"[a-z][a-z0-9\-]{2,}|[\u4e00-\u9fff]{2,}")

_PUNCT_RE = re.compile(r"[，。；：、！？,.;:!?()（）\[\]【】\"'“”‘’/\\|<>《》—\-]+")

#: 检索式里的脚手架词（"怎么写"的措辞，不是"检索什么"）
_QUERY_NOISE = {
    "summarise", "summarize", "summary", "cite", "citing", "cited",
    "citation", "citations", "least", "source", "sources", "highlight",
    "section", "paragraph", "search", "检索", "小节", "本节", "大纲",
    "指令", "要求", "强调", "引用", "只写",
}

#: 指令里显式的引用条数：``引用不少于3条`` / ``cite at least 2 sources`` / ``至少 5 篇``
_CITE_COUNT_RES = (
    re.compile(r"引[用靠]?\s*(?:不[少低]于|至少|不少|≥|>=)?\s*(\d{1,3})"),
    re.compile(r"(?:不[少低]于|至少|≥|>=)\s*(\d{1,3})\s*(?:条|篇|个|项)"),
    re.compile(r"(?:cite|citing|reference)\s+(?:at\s+least\s+)?(\d{1,3})", re.I),
    re.compile(r"at\s+least\s+(\d{1,3})", re.I),
)


def cited_count_hint(text: str) -> int:
    """从指令里抽出用户要求的引用条数；没写则返回 0。

    "引用不少于3条"是**用户自己声明的证据下限**，应当直接决定命中文献的门槛，
    否则会出现"用户只要 3 条，系统却按 6 篇判不足"的自相矛盾判定。
    """
    raw = str(text or "")
    for pattern in _CITE_COUNT_RES:
        match = pattern.search(raw)
        if match:
            try:
                value = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if 1 <= value <= 200:
                return value
    return 0


def _strip_instruction(text: str) -> str:
    """抠掉指令性措辞，保留可校验的要求名词。

    实测教训：``只写金催化部分，引用不少于3条，强调区域选择性`` 若只按标点切分，
    整段会因含"写"字标记被丢弃，导致要求集为空。
    """
    cleaned = _PUNCT_RE.sub(" ", str(text or ""))
    for marker in _INSTRUCTION_MARKERS:
        cleaned = cleaned.replace(marker, " ")
    for noise in _BOILERPLATE:
        cleaned = cleaned.replace(noise, " ")
    return cleaned


def tokenize(text: str) -> list[str]:
    """把指令/需求切成**可校验的要求**词元（去停用词、去指令措辞、去重保序）。"""
    lowered = _strip_instruction(str(text or "").lower())
    out: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall(lowered):
        token = token.strip("-")
        if not token or token in _STOPWORDS or token in seen:
            continue
        if any(marker in token for marker in _INSTRUCTION_MARKERS):
            continue
        if any(noise in token for noise in _BOILERPLATE):
            continue
        if _is_cjk(token) and len(token) > 8:
            continue
        seen.add(token)
        out.append(token)
    return out


def _is_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


#: 覆盖度搜索的（表, 子句）清单 —— 穷尽"证据可能出现在哪里"
#: 实测教训：只看 ontology_nodes.name 与超边 label 会把"区域选择性"判为缺失，
#: 而它其实存在于节点别名/属性、超边成员、条件键、测量指标或证据句里。
_COVERAGE_TARGETS: tuple[tuple[str, str], ...] = (
    ("ontology_nodes",
     "LOWER(name) LIKE ? OR LOWER(COALESCE(aliases,'')) LIKE ? "
     "OR LOWER(COALESCE(attributes,'')) LIKE ?"),
    ("ontology_edges",
     "LOWER(relation_type) LIKE ? OR LOWER(COALESCE(attributes,'')) LIKE ? "
     "OR LOWER(COALESCE(provenance,'')) LIKE ?"),
    ("ontology_hyperedges",
     "LOWER(COALESCE(label,'')) LIKE ? OR LOWER(COALESCE(attributes,'')) LIKE ? "
     "OR LOWER(COALESCE(provenance,'')) LIKE ?"),
    ("ontology_hyperedge_members",
     "LOWER(COALESCE(role,'')) LIKE ? OR LOWER(COALESCE(qualifiers,'')) LIKE ? "
     "OR LOWER(COALESCE(role,'')) LIKE ?"),
    ("ontology_hyperedge_conditions",
     "LOWER(COALESCE(condition_key,'')) LIKE ? OR LOWER(COALESCE(value_text,'')) LIKE ? "
     "OR LOWER(COALESCE(unit,'')) LIKE ?"),
    ("ontology_hyperedge_measurements",
     "LOWER(COALESCE(metric,'')) LIKE ? OR LOWER(COALESCE(value_text,'')) LIKE ? "
     "OR LOWER(COALESCE(qualifier,'')) LIKE ?"),
    ("ontology_hyperedge_evidence",
     "LOWER(COALESCE(span_text,'')) LIKE ? OR LOWER(COALESCE(section,'')) LIKE ? "
     "OR LOWER(COALESCE(span_text,'')) LIKE ?"),
    ("event_assertions",
     "LOWER(COALESCE(event_type,'')) LIKE ? OR LOWER(COALESCE(trigger,'')) LIKE ? "
     "OR LOWER(COALESCE(participants,'')) LIKE ?"),
)


def _stem(token: str) -> str:
    """极轻量英文词干化：单复数/常见后缀归一。

    实测教训：指令里的 ``ynamides`` 与文献里的 ``ynamide`` 被当成两个不同的
    要求，覆盖率被硬生生压低（4/7 → 触发 requirements 闸门），而实际证据
    完全覆盖了这个要求。只处理最保守的几类后缀，避免把不同概念合并。
    """
    word = str(token or "").lower().strip()
    if not word or not word.isascii():
        return word
    # 复数优先于 -es/-s：'ynamides' 应先去 's' 得 'ynamide'（学术文本里
    # 单复数混用极常见），而不是去 'es' 得 'ynamid'。
    if len(word) >= 5 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    for suffix in ("izations", "ization", "isations", "isation", "ations",
                   "ation", "ings", "ing"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _requirement_is_covered(conn: sqlite3.Connection, token: str) -> bool:
    """该需求词元是否在知识库里出现过（节点/边/超边/成员/条件/测量/证据/事件）。"""
    like = f"%{token}%"
    for table, clause in _COVERAGE_TARGETS:
        try:
            row = conn.execute(
                f"SELECT 1 FROM {table} WHERE {clause} LIMIT 1",
                (like, like, like),
            ).fetchone()
        except sqlite3.Error:
            # 表可能不存在（例如未建 event_assertions 的旧库）
            continue
        if row:
            return True
    # 词干回退：'ynamides' 应能被 'ynamide' 覆盖（英文单复数在学术写作里极常见）
    stem = _stem(token)
    if stem and stem != token:
        like = f"%{stem}%"
        for table, clause in _COVERAGE_TARGETS:
            try:
                row = conn.execute(
                    f"SELECT 1 FROM {table} WHERE {clause} LIMIT 1",
                    (like, like, like),
                ).fetchone()
            except sqlite3.Error:
                continue
            if row:
                return True
    return False


def _settings(settings: Settings | None) -> Settings:
    return settings or default_settings


def _threshold(settings: Settings, key: str, default: float) -> float:
    raw = getattr(settings, key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def evaluate_sufficiency(
    conn: sqlite3.Connection,
    *,
    plan: dict[str, Any] | None,
    instruction: str = "",
    heading: str = "",
    settings: Settings | None = None,
    llm_analysis: dict[str, Any] | None = None,
    rounds_done: int = 0,
) -> dict[str, Any]:
    """判定当前库能否支撑该段写作。

    返回 ``{decision, confidence, dimensions, counts, reasons, retrieval_requests,
    suggested_queries, rounds_done}``。``decision`` 取
    ``sufficient`` / ``insufficient`` / ``exhausted``。
    """
    s = _settings(settings)
    plan = plan or {}
    retrieval_plan = plan.get("retrieval_plan") or {}
    mission = plan.get("mission") or {}

    # 命中文献的检索词：指令 + 章节标题 + Planner 检索词与硬约束（标题在此**有用**）
    demand_parts = [
        str(instruction or ""),
        str(heading or ""),
        " ".join(str(x) for x in (retrieval_plan.get("query_variants") or [])),
        " ".join(str(x) for x in (mission.get("seed_terms") or [])),
        " ".join(str(x) for x in (retrieval_plan.get("must_cover") or [])),
    ]
    tokens = tokenize(" ".join(part for part in demand_parts if part))

    # 要求校验的来源：**只有用户指令（+ Planner 的硬约束）**。
    #
    # 不含章节标题：标题是"要写哪一节"，不是"必须出现哪些内容"，计进去会让
    # 任何标题都成为永远无法被知识库覆盖的缺口（实测把覆盖率从 4/6 拖到 3/7）。
    # 也不含 Planner 的 seed_terms：那是**检索用**词（离线兜底还会从标题/主题
    # 分出一堆中文碎片），拿它当"写作要求"会把库的语言差异误判成证据缺口。
    requirement_parts = [
        str(instruction or ""),
        " ".join(str(x) for x in (retrieval_plan.get("must_cover") or [])),
    ]
    requirement_tokens = tokenize(" ".join(p for p in requirement_parts if p))

    # ---------------------------------------------------------- 维度 1：命中文献
    paper_keys: list[str] = []
    if tokens:
        clauses = []
        params: list[Any] = []
        for token in tokens[:12]:
            like = f"%{token}%"
            clauses.append("(LOWER(COALESCE(p.title,'')) LIKE ? OR "
                           "LOWER(COALESCE(p.abstract,'')) LIKE ? OR "
                           "LOWER(COALESCE(p.keywords,'')) LIKE ?)")
            params.extend([like, like, like])
        where = " OR ".join(clauses)
        rows = conn.execute(
            f"SELECT DISTINCT p.paper_key FROM papers p "
            f"LEFT JOIN quality_results q ON q.paper_key = p.paper_key "
            f"WHERE (p.status = 'ingested' OR q.decision IN ('direct','flagged')) "
            f"  AND ({where}) LIMIT 400",
            params,
        ).fetchall()
        paper_keys = [r["paper_key"] for r in rows]
    min_papers = int(_threshold(s, "section_sufficiency_min_papers", 6))
    # 指令里显式声明的引用条数是**用户给的证据下限**，取"配置下限"与
    # "用户要求 + 余量"的较小者（但仍至少 2 篇，避免单篇文献撑起整段）。
    cite_hint = cited_count_hint(instruction)
    if cite_hint:
        min_papers = max(2, min(min_papers, cite_hint + 1))
    elif min_papers > 2 and bool(getattr(
            s, "section_adaptive_paper_floor", True)):
        # 未声明引用数时按库规模自适应，"6 篇"是给大库的默认值，不该在小库上
        # 一律判死。**阈值 ≤ 2 或显式关闭自适应**（严格模式）时不覆盖配置值。
        try:
            total_papers = int(conn.execute(
                "SELECT COUNT(*) AS c FROM papers WHERE status='ingested'"
            ).fetchone()["c"] or 0)
        except (sqlite3.Error, TypeError):
            total_papers = 0
        if total_papers:
            min_papers = max(2, min(min_papers, total_papers))
    # **绝对比例**而非"除以阈值"：否则"刚达标"就等于满分，短板的权重被稀释
    # （实测教训：命中 1 篇 / 阈值 6 却因其他维度满分而整体判成 sufficient）
    papers_score = min(1.0, len(paper_keys) / max(1, min_papers))

    # ---------------------------------------------------------- 维度 2：知识覆盖
    extracted = 0
    if paper_keys:
        placeholders = ",".join("?" * len(paper_keys[:400]))
        row = conn.execute(
            f"SELECT COUNT(DISTINCT paper_key) AS c FROM processing_log "
            f"WHERE event = 'extracted' AND paper_key IN ({placeholders})",
            paper_keys[:400],
        ).fetchone()
        extracted = int(row["c"] or 0) if row else 0
    coverage = (extracted / len(paper_keys)) if paper_keys else 0.0
    min_coverage = _threshold(s, "section_sufficiency_min_knowledge_ratio", 0.6)
    knowledge_score = coverage

    # ---------------------------------------------------------- 维度 3：量化条件
    total_hyper = 0
    with_quantity = 0
    if paper_keys:
        placeholders = ",".join("?" * len(paper_keys[:400]))
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM ontology_hyperedges "
            f"WHERE paper_key IN ({placeholders})", paper_keys[:400],
        ).fetchone()
        total_hyper = int(row["c"] or 0) if row else 0
        if total_hyper:
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM ontology_hyperedges h WHERE "
                f"h.paper_key IN ({placeholders}) AND ("
                f"  EXISTS(SELECT 1 FROM ontology_hyperedge_conditions c "
                f"         WHERE c.hyperedge_id = h.hyperedge_id) OR "
                f"  EXISTS(SELECT 1 FROM ontology_hyperedge_measurements m "
                f"         WHERE m.hyperedge_id = h.hyperedge_id))",
                paper_keys[:400],
            ).fetchone()
            with_quantity = int(row["c"] or 0) if row else 0
    quantity_ratio = (with_quantity / total_hyper) if total_hyper else 0.0
    min_quantity = _threshold(s, "section_sufficiency_min_condition_ratio", 0.3)
    conditions_score = quantity_ratio

    # ---------------------------------------------------------- 维度 4：指令要求覆盖
    requirements = [t for t in requirement_tokens if len(t) >= 2][:10]
    covered: list[str] = []
    missing: list[str] = []
    for token in requirements:
        if _requirement_is_covered(conn, token):
            covered.append(token)
        else:
            missing.append(token)
    req_ratio = (len(covered) / len(requirements)) if requirements else 1.0
    min_req = _threshold(s, "section_sufficiency_min_requirement_ratio", 0.5)
    requirements_score = req_ratio

    # ---------------------------------------------------------- 维度 5：可用证据
    # 证据 = 库里可被引用的超边/边编号（**全库范围**，不限命中文献）。
    # 早期的写法只把命中文献的超边算进来，后果是"补检进来的论文还没抽取超边"
    # 时反而把证据数从 2 涨不上去（库小 → 永远判不足）。阈值本身仍是"库的
    # 证据密度"要求，由 min_evidence 与自适应上限共同决定。
    evidence_ids: list[str] = []
    rows = conn.execute(
        "SELECT hyperedge_id FROM ontology_hyperedges "
        "ORDER BY hyperedge_id LIMIT 40").fetchall()
    evidence_ids = [f"H-{int(r['hyperedge_id']):04d}" for r in rows]
    if not evidence_ids:
        rows = conn.execute(
            "SELECT edge_id FROM ontology_edges ORDER BY edge_id LIMIT 40"
        ).fetchall()
        evidence_ids = [f"E-{int(r['edge_id']):04d}-1" for r in rows][:10]
    min_evidence = int(_threshold(s, "section_sufficiency_min_evidence", 3))
    if cite_hint:
        # 用户只要 1 条引用时，不该因"证据不足 3 条"再去烧一轮补检配额
        min_evidence = max(1, min(min_evidence, cite_hint))
    elif bool(getattr(s, "section_adaptive_paper_floor", True)):
        # 与命中文献同一套"小库自适应"逻辑：证据天花板就是库里已有的超边数，
        # 小库不该被"证据 ≥ 3 条"卡死（实测补检到 3 篇仍被 evidence 闸门挡住）。
        min_evidence = max(1, min(min_evidence, len(evidence_ids) or 1))
    evidence_score = min(1.0, len(evidence_ids) / max(1, min_evidence))
    # ---------------------------------------------------------- 加权汇总
    dimensions = {
        "papers": round(papers_score, 3),
        "knowledge": round(knowledge_score, 3),
        "conditions": round(conditions_score, 3),
        "requirements": round(requirements_score, 3),
        "evidence": round(evidence_score, 3),
    }
    weights = {
        "papers": _threshold(s, "section_weight_papers", 0.30),
        "knowledge": _threshold(s, "section_weight_knowledge", 0.25),
        "conditions": _threshold(s, "section_weight_conditions", 0.20),
        "requirements": _threshold(s, "section_weight_requirements", 0.15),
        "evidence": _threshold(s, "section_weight_evidence", 0.10),
    }
    total_weight = sum(weights.values()) or 1.0
    confidence = sum(dimensions[k] * weights[k] for k in dimensions) / total_weight
    confidence = round(max(0.0, min(1.0, confidence)), 3)

    threshold = _threshold(s, "section_sufficiency_threshold", 0.62)
    max_rounds = int(_threshold(s, "section_max_collection_rounds", 2))

    # ---------------------------------------------------------- 硬闸门
    # 加权平均会被"饱和维度"稀释：命中 1 篇文献（0.167/0.30）也能被其余满分抬过阈值。
    # 因此任一硬指标未达标即封顶为"未达标"，只允许结论更保守、不允许更乐观。
    caps = [threshold - 0.001]
    gates: list[dict[str, Any]] = []

    def _gate(name: str, ok: bool, detail: str) -> None:
        gates.append({"gate": name, "passed": bool(ok), "detail": detail})

    if tokens:
        _gate("papers", len(paper_keys) >= min_papers,
              f"命中文献 {len(paper_keys)} 篇 / 需 ≥ {min_papers} 篇")
    if paper_keys:
        _gate("knowledge", coverage >= min_coverage,
              f"已抽取知识比例 {coverage:.0%} / 需 ≥ {min_coverage:.0%}")
    if total_hyper:
        _gate("conditions", quantity_ratio >= min_quantity,
              f"带量化条件的超边 {with_quantity}/{total_hyper} / 需 ≥ {min_quantity:.0%}")
    if requirements:
        _gate("requirements", req_ratio >= min_req,
              f"指令要求覆盖 {len(covered)}/{len(requirements)} / 需 ≥ {min_req:.0%}")
    if paper_keys:
        _gate("evidence", len(evidence_ids) >= min_evidence,
              f"可用证据 {len(evidence_ids)} 条 / 需 ≥ {min_evidence} 条")

    failed_gates = [g for g in gates if not g["passed"]]
    if failed_gates:
        confidence = round(min(confidence, min(caps)), 3)

    if confidence >= threshold:
        decision = "sufficient"
    elif rounds_done >= max_rounds:
        decision = "exhausted"
    else:
        decision = "insufficient"

    reasons = _build_reasons(
        decision, confidence, threshold, dimensions,
        {"papers": len(paper_keys), "min_papers": min_papers,
         "extracted": extracted, "coverage": round(coverage, 2),
         "min_coverage": min_coverage,
         "hyperedges": total_hyper, "with_quantity": with_quantity,
         "quantity_ratio": round(quantity_ratio, 2), "min_quantity": min_quantity,
         "requirements": len(requirements), "covered": covered[:6],
         "missing": missing[:6], "min_req": min_req,
         "evidence": len(evidence_ids), "min_evidence": min_evidence},
        rounds_done, max_rounds, gates,
    )

    # LLM 结论（可选）：与确定性结论取保守者
    llm_verdict = None
    if isinstance(llm_analysis, dict):
        wants_more = bool(llm_analysis.get("retrieval_requests")) \
            or str(llm_analysis.get("sufficiency") or "").lower() in ("insufficient", "low", "no")
        if wants_more and decision == "sufficient":
            decision = "insufficient" if rounds_done < max_rounds else "exhausted"
            llm_verdict = "llm 提示证据缺口 → 下调为不足（保守合并）"
            reasons.append(llm_verdict)
        elif wants_more:
            llm_verdict = "llm 同样判定证据缺口"

    # 判定充足时没有"建议补检"这回事：把它留空，而不是把检索词当成
    # "建议补检词"回给界面（实测界面里出现"summarise/cite"这种噪声词）。
    suggested = ([] if decision == "sufficient"
                 else _suggested_queries(plan, instruction, missing) or tokens[:6])
    return {
        "decision": decision,
        "confidence": confidence,
        "threshold": threshold,
        "dimensions": dimensions,
        "weights": weights,
        "counts": {
            "matched_papers": len(paper_keys),
            "extracted_papers": extracted,
            "hyperedges": total_hyper,
            "hyperedges_with_quantity": with_quantity,
            "evidence_ids": len(evidence_ids),
            "requirements_total": len(requirements),
            "requirements_missing": missing,
        },
        "paper_keys": paper_keys[:200],
        "evidence_ids": evidence_ids[:40],
        "reasons": reasons,
        "gates": gates,
        "failed_gates": [g["gate"] for g in failed_gates],
        "suggested_queries": suggested[:6],
        "retrieval_requests": ([] if decision == "sufficient"
                               else [{"query_terms": suggested[:6],
                                      "reason": "；".join(reasons[-2:])}]),
        "rounds_done": rounds_done,
        "max_rounds": max_rounds,
        "llm_verdict": llm_verdict,
    }


def _build_reasons(decision: str, confidence: float, threshold: float,
                   dims: dict[str, float], info: dict[str, Any],
                   rounds_done: int, max_rounds: int,
                   gates: list[dict[str, Any]] | None = None) -> list[str]:
    reasons: list[str] = []
    for gate in gates or []:
        if not gate.get("passed"):
            reasons.append(f"硬指标未达标（{gate['gate']}）：{gate['detail']} → 封顶为未达标")
    if info["papers"] < info["min_papers"]:
        reasons.append(
            f"命中文献 {info['papers']} 篇 < 阈值 {info['min_papers']} 篇")
    if info["coverage"] < info["min_coverage"]:
        reasons.append(
            f"知识抽取覆盖 {info['coverage']:.0%} < 阈值 {info['min_coverage']:.0%}"
            f"（已抽取 {info['extracted']} / {info['papers']}）")
    if info["quantity_ratio"] < info["min_quantity"]:
        reasons.append(
            f"含量化条件的超边占比 {info['quantity_ratio']:.0%} < 阈值 "
            f"{info['min_quantity']:.0%}（{info['with_quantity']} / {info['hyperedges']}）")
    if info["requirements"] and info["missing"]:
        reasons.append(
            f"指令要求未被证据覆盖：{'、'.join(info['missing'])}"
            f"（已覆盖 {len(info['covered'])} / {info['requirements']}）")
    if info["evidence"] < info["min_evidence"]:
        reasons.append(
            f"可用证据编号 {info['evidence']} 条 < 阈值 {info['min_evidence']} 条")
    if not reasons:
        reasons.append(f"五个维度均达标（confidence={confidence}）")
    if decision == "exhausted":
        reasons.append(
            f"已用尽补检预算（{rounds_done}/{max_rounds} 轮）仍不足，不生成正文")
    elif decision == "insufficient":
        reasons.append(
            f"建议补检（已用 {rounds_done}/{max_rounds} 轮）")
    return reasons


def _suggested_queries(plan: dict[str, Any], instruction: str,
                       missing: list[str]) -> list[str]:
    """补检用的检索词：Planner 的检索式 ∪ 缺失要求（去重保序）。"""
    out: list[str] = []
    seen: set[str] = set()

    def push(text: str) -> None:
        text = str(text or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)

    retrieval_plan = plan.get("retrieval_plan") or {}
    for item in retrieval_plan.get("query_variants") or []:
        push(item)
    for item in (plan.get("mission") or {}).get("seed_terms") or []:
        push(item)
    # 指令只取**词元**，绝不把整句当检索词：整句检索式在真实检索器里
    # 几乎必然 0 命中，却会占满建议列表（实测补检词里排第一的是整段指令）。
    for token in tokenize(instruction)[:4]:
        push(token)
    for token in missing[:4]:
        push(token)
    # 兜底过滤：检索式里的脚手架词（summarise/cite/section…）不是检索内容
    cleaned = [q for q in out if q.lower() not in _QUERY_NOISE]
    return cleaned or out


def load_section_run(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """读取某个章节运行的全部轮次（决策轨迹）。"""
    rows = conn.execute(
        "SELECT * FROM section_runs WHERE run_id=? ORDER BY round", (run_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key in ("plan_json", "sufficiency_json", "collection_json"):
            raw = item.pop(key, None)
            name = key.replace("_json", "")
            try:
                item[name] = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                item[name] = None
        out.append(item)
    return out
