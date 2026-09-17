"""质量控制节点：在文献质量评估之外承担全局一致性控制。

流程：
1. 装载文献 → （可选）用 OpenAlex 刷新被引/H 指数等权威性数据；
2. 元数据完整性判定：全部作者 / 单位 / 发表情况 / DOI 缺漏时
   decision='enrich' 发回检索节点补全；超过最大轮数则转人工；
3. 计算 A / T / Q 并路由：
   - Q >= 0.8            → decision='knowledge'  直接送知识提取；
   - 0.5 <= Q < 0.8      → decision='flagged'    标记后送知识提取；
   - Q < 0.5             → decision='human'      人工审核。
4. 每次质量控制节点运行时检查本体节点增量；达到阈值后按 IUPAC Gold Book /
   ChEBI 领域词典做全局实体/关系归并。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from research_agent.config import Settings, settings as default_settings
from research_agent.db import connect, get_paper, log_event, save_quality_result, upsert_paper
from research_agent.quality.llm import assess_with_llm
from research_agent.quality.control import maybe_global_merge
from research_agent.quality.scoring import check_metadata_completeness, quality_assess
from research_agent.retrieval.api_clients import ApiHub

logger = logging.getLogger(__name__)


def _refresh_authority(rec: dict[str, Any], api: ApiHub | None,
                       conn: sqlite3.Connection,
                       settings: Settings) -> dict[str, Any]:
    """元数据中的被引/H 指数缺失时，尝试用 OpenAlex 刷新（失败静默）。"""
    if api is None or (rec.get("citation_count") is not None and rec.get("avg_h_index") is not None):
        return rec
    try:
        slim = {k: rec.get(k) for k in
                ("paper_key", "doi", "title", "venue", "authors", "source_type")}
        refreshed = api.enrich(slim)
        if refreshed.get("citation_count") is not None or refreshed.get("avg_h_index") is not None:
            for field in ("pdf_blob", "pdf_sha256", "pdf_size", "clean_text",
                          "clean_text_sha256", "status", "abstract", "pub_year",
                          "pub_date", "publication_status", "pmcid",
                          "fulltext_source"):
                if field in rec:
                    refreshed[field] = rec[field]
            upsert_paper(conn, refreshed)
            return refreshed
    except Exception as exc:  # noqa: BLE001
        logger.warning("刷新权威性数据失败 %s: %s", rec.get("paper_key"), exc)
    return rec


def make_quality_node(api: ApiHub | None = None,
                      model=None,
                      conn: sqlite3.Connection | None = None,
                      settings: Settings | None = None,
                      offline: bool = False):
    """构造 LangGraph 质量控制节点。model 为绑定 GLM 4.7 Flash 的 ChatModel。"""
    settings = settings or default_settings

    def quality_node(state: dict) -> dict:
        key = state.get("current_key")
        if not key:
            return {"error": "缺少 current_key，无法评估", "status": "error"}
        own_conn = conn is None
        db = conn or connect(settings.db_path)
        try:
            rec = get_paper(db, key)
            if not rec:
                return {"error": f"文献不存在: {key}", "status": "error"}
            if not offline and api is not None:
                rec = _refresh_authority(rec, api, db, settings)
            rec.pop("pdf_blob", None)
            rec.pop("clean_text", None)

            ok, missing = check_metadata_completeness(rec)
            attempts = int(state.get("meta_attempts") or 0)
            llm_part = None
            if model is not None:
                try:
                    llm_part = assess_with_llm(rec, model)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("GLM 评估调用异常，回退规则评分: %s", exc)
            result = quality_assess(rec, settings, llm=llm_part)
            result["paper_key"] = key

            if not ok and attempts < settings.max_meta_attempts:
                # 发回检索节点补全（状态机里经条件边回到 retrieval 节点）
                decision, needs_review, rationale = "enrich", False, (
                    f"元数据缺漏: {', '.join(missing)}，第 {attempts + 1} 次回补")
                result["meta_missing"] = missing
            elif not ok:
                decision, needs_review, rationale = "human", True, (
                    f"元数据无法补全（{', '.join(missing)}），进入人工审核")
                result["meta_missing"] = missing
            else:
                decision = {
                    "direct": "knowledge",
                    "flagged": "flagged",
                    "human": "human",
                }[result["decision"]]
                needs_review = result["needs_review"]
                base_rationale = (
                    f"A={result['authority']}(venue={result['venue_factor']}, "
                    f"h={result['h_factor']}, cite={result['citation_factor']}), "
                    f"T={result['timeliness']} → Q={result['quality']}")
                llm_note = result.get("rationale")
                rationale = (
                    f"[LLM 评审] {llm_note} | {base_rationale}"
                    if llm_note else base_rationale
                )
            result["decision"] = decision
            result["needs_review"] = bool(needs_review)
            result["rationale"] = rationale
            save_quality_result(db, result)
            quality_control = maybe_global_merge(db, settings)
            if quality_control.get("triggered"):
                result["quality_control"] = quality_control
            log_event(db, "quality", "assessed", key,
                      {"decision": decision, "missing": missing,
                       "A": result.get("authority"), "T": result.get("timeliness"),
                       "Q": result.get("quality"), "rationale": rationale})
            if decision == "enrich":
                rec.pop("pdf_blob", None)
                rec.pop("clean_text", None)
                return {
                    "quality_result": result,
                    "decision": decision,
                    "missing_fields": missing,
                    "meta_attempts": attempts + 1,
                    "mode": "enrich",
                    "paper_record": rec,
                    "status": "needs-metadata",
                }
            return {
                "quality_result": result,
                "decision": decision,
                "needs_review": bool(needs_review),
                "meta_attempts": attempts,
                "status": "assessed",
            }
        finally:
            if own_conn:
                db.close()

    return quality_node


def make_human_review_node(conn: sqlite3.Connection | None = None,
                           settings: Settings | None = None):
    """人工审核节点：将文献标记为等待人工处理。"""
    settings = settings or default_settings

    def human_review_node(state: dict) -> dict:
        key = state.get("current_key")
        own_conn = conn is None
        db = conn or connect(settings.db_path)
        try:
            rec = get_paper(db, key) if key else None
            reason = (state.get("quality_result") or {}).get("rationale") or state.get("error")
            if rec:
                rec["status"] = "human_review"
                # 只更新状态：大字段（PDF/精校文本）交给 upsert_paper 的
                # COALESCE 保留，避免把已入库正文清空（P0-2）
                for field in ("pdf_blob", "pdf_sha256", "pdf_size", "clean_text",
                              "clean_text_sha256"):
                    rec.pop(field, None)
                upsert_paper(db, rec)
                log_event(db, "quality", "human-review", key,
                          {"reason": reason, "decision": state.get("decision")})
            return {"status": "human_review", "decision": "human",
                    "human_reason": reason}
        finally:
            if own_conn:
                db.close()

    return human_review_node
