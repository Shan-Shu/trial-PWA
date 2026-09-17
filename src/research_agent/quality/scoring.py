"""A / T / Q 评分模型与元数据完整性判定。

公式（可配置，见 config.Settings）：
    A = a_venue_weight * venue_factor
      + a_hindex_weight * h_factor
      + a_citation_weight * citation_factor
    T = 0.7 * exp(-ln2 * age / half_life(field)) + 0.3 * max(0, 1 - age/25)
    Q = q_weight_a * A + q_weight_t * T

评分项全部归一化到 [0, 1]。任一子数据缺失时取“中性值 0.5 附近”，
避免因单源缺数把文献一刀切打低（缺数本身由元数据完整性判定单独处理）。
"""
from __future__ import annotations

import math
from typing import Any

from research_agent import packs
from research_agent.config import Settings, settings as default_settings


def _quartile_scores() -> dict[str, float]:
    """分区 → 权威性分值（来自 packs/skills/journal-quartiles）。"""
    scores = packs.quartile_scores()
    if not scores:
        packs.warn_once("quartile-scores-missing",
                        "未加载 packs/skills/journal-quartiles 的 quartile_scores；"
                        "分区命中时不加权威性分")
    return scores


def normalize_venue_name(name: str | None) -> str | None:
    if not name:
        return None
    return " ".join(name.lower().split())


def venue_factor(rec: dict[str, Any],
                 settings: Settings | None = None) -> tuple[float, str | None, str]:
    """权威性子项1：期刊/出版社分级（JCR/SCI 分区）→ [0,1]。

    分区数据来自技能包（可按学科用 RA_JOURNAL_QUARTILES 覆盖）；
    命中分区才加分，未命中按来源类型给中性值，不做"未知=低分"的猜测。
    """
    settings = settings or default_settings
    scores = _quartile_scores()
    name = normalize_venue_name(rec.get("venue"))
    quartile = (rec.get("venue_quartile") or "").upper() or None
    note = ""
    if quartile:
        pass
    elif name and name != "arxiv":
        quartile = packs.journal_quartile(name)
        if quartile:
            note = f"分区表匹配: {quartile}"
    if quartile and quartile in scores:
        return scores[quartile], quartile, note or f"分区 {quartile}"
    source_type = (rec.get("source_type") or "").lower()
    if name and name == "arxiv":
        return 0.5, None, "arXiv 预印本（中性）"
    if source_type == "journal":
        return 0.6, None, "期刊但分区未知（中性 0.6）"
    if source_type in ("repository", "preprint", "posted-content"):
        return 0.5, None, "预印本/仓库（中性 0.5）"
    if source_type == "proceedings" or "conference" in str(rec.get("venue", "")).lower():
        return 0.65, None, "会议论文（默认 0.65）"
    return 0.55, None, "来源未知（中性 0.55）"


def _avg_h_index(rec: dict[str, Any]) -> float | None:
    if rec.get("avg_h_index") is not None:
        return float(rec["avg_h_index"])
    vals = [a.get("h_index") for a in (rec.get("authors") or [])
            if a.get("h_index") is not None]
    return sum(vals) / len(vals) if vals else None


def h_factor(rec: dict[str, Any]) -> float:
    """权威性子项2：作者 H 指数 → [0,1]。无数据取中性 0.5。"""
    h = _avg_h_index(rec)
    if h is None:
        return 0.5
    return round(min(1.0, 0.40 + 0.58 * min(max(h, 0.0), 80.0) / 80.0), 3)


def citation_factor(rec: dict[str, Any]) -> float:
    """权威性子项3：被引用次数（对数刻度）→ [0,1]。无数据取中性 0.5。"""
    c = rec.get("citation_count")
    if c is None:
        return 0.5
    c = max(0, int(c))
    if c == 0:
        return 0.30
    return round(min(1.0, 0.35 + 0.22 * math.log10(c + 1.0)), 3)


def authority_score(rec: dict[str, Any],
                    settings: Settings | None = None,
                    llm: dict[str, Any] | None = None) -> dict[str, Any]:
    """A = w1*venue + w2*h_index + w3*citation。

    llm（如 GLM 4.7 Flash 输出的子项）存在时优先使用其 venue/h/citation 因子，
    缺失项回退确定性计算。
    """
    settings = settings or default_settings
    o = llm or {}

    def _num(v: Any) -> float | None:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return None

    vf = _num(o.get("venue_factor"))
    quartile = (o.get("venue_quartile") or "").upper() or None
    vnote = o.get("venue_note")
    if vf is None:
        rec2 = dict(rec)
        if quartile:
            rec2["venue_quartile"] = quartile
        vf, quartile, vnote = venue_factor(rec2, settings)
    hf = _num(o.get("h_factor"))
    hf = hf if hf is not None else h_factor(rec)
    cf = _num(o.get("citation_factor"))
    cf = cf if cf is not None else citation_factor(rec)
    a = (settings.a_venue_weight * vf
         + settings.a_hindex_weight * hf
         + settings.a_citation_weight * cf)
    return {
        "venue_factor": round(vf, 3),
        "venue_quartile": quartile,
        "venue_note": vnote,
        "h_factor": hf,
        "citation_factor": cf,
        "authority": round(a, 3),
    }


def timeliness_score(rec: dict[str, Any],
                     settings: Settings | None = None,
                     year_now: int | None = None) -> dict[str, Any]:
    """T：由出版年份与学科前沿迭代速度综合判断 → [0,1]。"""
    settings = settings or default_settings
    year_now = year_now or settings.current_year
    year = rec.get("pub_year")
    if not year:
        return {"timeliness": 0.5, "year_score": 0.5, "half_life": None,
                "field_velocity": rec.get("field_velocity")}
    field = (rec.get("field_velocity") or settings.default_field_velocity).lower()
    half_life = settings.field_half_life.get(field, settings.field_half_life[
        settings.default_field_velocity])
    age = max(0, year_now - int(year))
    if age <= 0:
        return {"timeliness": 1.0, "year_score": 1.0,
                "half_life": half_life, "field_velocity": field}
    recency = math.exp(-math.log(2.0) * age / half_life)
    linear = max(0.0, 1.0 - age / 25.0)
    t = 0.7 * recency + 0.3 * linear
    return {"timeliness": round(min(1.0, t), 3), "year_score": round(recency, 3),
            "half_life": half_life, "field_velocity": field}


def quality_assess(rec: dict[str, Any],
                   settings: Settings | None = None,
                   year_now: int | None = None,
                   llm: dict[str, Any] | None = None) -> dict[str, Any]:
    """综合评估，返回含 A/T/Q 与路由决策的结果字典。

    llm 为质量节点绑定的 LLM（GLM 4.7 Flash）给出的子项评分/学科速度/rationale。
    """
    settings = settings or default_settings
    rec2 = dict(rec)
    if llm and llm.get("field_velocity"):
        rec2["field_velocity"] = str(llm["field_velocity"]).lower()
    a_part = authority_score(rec2, settings, llm)
    t_part = timeliness_score(rec2, settings, year_now)
    q = settings.q_weight_a * a_part["authority"] + settings.q_weight_t * t_part["timeliness"]
    q = round(max(0.0, min(1.0, q)), 3)
    if q >= settings.threshold_direct:
        decision, needs_review = "direct", False
    elif q >= settings.threshold_flag:
        decision, needs_review = "flagged", True
    else:
        decision, needs_review = "human", True
    return {
        **a_part, **t_part,
        "quality": q,
        "decision": decision,
        "needs_review": needs_review,
        "threshold_direct": settings.threshold_direct,
        "threshold_flag": settings.threshold_flag,
        **({"rationale": str(llm.get("rationale") or "")} if llm and llm.get("rationale") else {}),
    }


def check_metadata_completeness(rec: dict[str, Any]) -> tuple[bool, list[str]]:
    """检查：全部作者、作者单位、发表情况、DOI 是否齐备。

    NCPSSD 等中文公益性期刊源常不提供 DOI，机构也可能由统一出版主体承担，
    因此对这些来源只要求作者、期刊/出版信息和年份齐备，缺失字段单独记录。
    """
    missing: list[str] = []
    source = str(rec.get("source") or "").lower()
    authors = rec.get("authors") or []
    if not authors or any(not (a.get("name") or "").strip() for a in authors):
        missing.append("authors")
    affs = [a.get("affiliations") or [] for a in authors]
    if source != "ncpssd" and not any(x for sub in affs for x in sub):
        missing.append("affiliations")
    venue_ok = bool((rec.get("venue") or "").strip()) or bool((rec.get("source_type") or "").strip())
    if not venue_ok or not rec.get("pub_year"):
        missing.append("publication")
    if source != "ncpssd" and not (rec.get("doi") or "").strip():
        missing.append("doi")
    return not missing, missing
