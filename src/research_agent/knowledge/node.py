"""知识提取节点：文本预处理 → LLM 抽取（实体/关系/属性/事件）→ 写入动态本体。"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from research_agent.config import Settings, settings as default_settings
from research_agent.db import connect, get_paper, get_quality_result, log_event
from research_agent.knowledge.extractor import (
    KnowledgeExtractor,
    blend_confidence,
    is_reporting_phrase,
)
from research_agent.knowledge.preprocess import (
    chunk_paragraphs,
    split_paragraphs,
    split_sentences,
)
from research_agent.ontology.store import (
    add_event_assertion,
    get_node_id,
    graph_summary,
    init_ontology,
    record_ontology_run,
    cleanup_local_label_nodes,
    descriptive_alias,
    is_local_reference_label,
    strong_relation_set,
    rebuild_ontology_views,
    register_material,
    upsert_edge,
    upsert_hyperedge,
    upsert_node,
)
from research_agent.quality.control import maybe_global_merge

logger = logging.getLogger(__name__)


def _lookup_any_type(conn: sqlite3.Connection, name: str) -> int | None:
    """按规范化名称在全部类型中查找既有节点（关系对象可能未在本文声明）。"""
    from research_agent.ontology.store import _norm

    row = conn.execute(
        "SELECT node_id FROM ontology_nodes WHERE normalized_name=? LIMIT 1",
        (_norm(name),),
    ).fetchone()
    return int(row["node_id"]) if row else None


def _evidence_tier(title: str | None) -> str:
    t = (title or "").lower()
    if t.startswith(("comment", "corrigendum", "erratum", "editorial",
                     "retraction", "addendum")):
        return "commentary"
    if "review" in t[:90] or t.startswith("a systematic review"):
        return "review"
    if re.search(r"clinical trial|case series|case report|randomized|cohort|meta-analysis", t):
        return "clinical"
    if re.search(r"in vivo|rat |mice|mouse|rabbit|canine|porcine|animal", t):
        return "primary_in_vivo"
    if re.search(r"in vitro|cell culture|osteoblast|cells? ", t):
        return "primary_in_vitro"
    return "primary"


def _generic_relation_warning(conn: sqlite3.Connection,
                              settings: Settings) -> str | None:
    """库内兜底关系统计提醒：抑制语料内泛化关系持续放大（v0.0.6）。"""
    fb = list(settings.generic_fallback_types or ())
    if not fb:
        return None
    ph = ",".join("?" for _ in fb)
    rows = conn.execute(
        f"SELECT relation_type, COUNT(*) AS c FROM ontology_edges "
        f"WHERE relation_type IN ({ph}) GROUP BY relation_type ORDER BY c DESC",
        tuple(fb),
    ).fetchall()
    if not rows:
        return None
    runs = conn.execute("SELECT COUNT(*) FROM ontology_runs").fetchone()[0] or 0
    lines = [f"语料提醒：本库已处理约 {runs} 篇，以下兜底关系已存在，请勿继续无谓放大："]
    lines += [f"  - {r['relation_type']}: {r['c']} 条" for r in rows]
    lines.append(
        "（ERROR LIST #3）只有当原文语义确实没有更具体关系时，才新增这类兜底关系；"
        "能落到 promotes/inhibits/releases/differentiates_into/enables/regulates/activates/"
        "results_in 等具体关系，就必须用具体关系。"
    )
    return "\n".join(lines)


_NUM_UNIT_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*([%a-zA-Zµμ°/×]+)\s*$")


def _clean_attr_key(key: str) -> str:
    k = re.sub(r"\s+", "_", str(key or "").strip().lower())
    k = re.sub(r"[^a-z0-9_\u4e00-\u9fff]+", "", k)
    return k or "attr"


def _clean_attr_value(v: Any) -> Any:
    """去掉空值；把 “5 wt% / 227.86 kPa / 36°C / 4.67%” 拆为 {value, unit}。"""
    if v is None:
        return None
    if isinstance(v, str):
        if not v.strip():
            return None
        m = _NUM_UNIT_RE.match(v)
        if m and m.group(2):
            return {"value": float(m.group(1)), "unit": m.group(2).strip()}
        return v
    if isinstance(v, (list, dict)) and not v:
        return None
    return v


def _clean_attrs(attrs: dict | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (attrs or {}).items():
        kk = _clean_attr_key(str(k))
        vv = _clean_attr_value(v)
        if vv is None:
            continue
        out[kk] = vv
    return out


def _upsert_knowledge(conn: sqlite3.Connection, data: dict[str, Any], *,
                      quality_q: float | None, flagged: bool,
                      paper_key: str, settings: Settings,
                      evidence_tier: str | None = None,
                      paper_text: str = "") -> dict[str, Any]:
    """把一次抽取结果写入本体，返回统计（新增节点/边/类型）。"""
    stats = {"entities": 0, "relations": 0, "events": 0,
             "hyperedges": 0, "new_nodes": 0, "new_edges": 0,
             "new_hyperedges": 0, "new_types": [], "dropped_garbage": 0}
    name_to_id: dict[tuple[str, str], int] = {}

    for e in data.get("entities") or []:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        if not name:
            continue
        if is_reporting_phrase(name):
            stats["dropped_garbage"] += 1
            continue
        aliases = [str(a) for a in (e.get("aliases") or []) if str(a).strip()]
        if is_local_reference_label(name):
            preferred = descriptive_alias(aliases)
            if not preferred:
                stats["dropped_garbage"] += 1
                continue
            if name not in aliases:
                aliases.append(name)
            name = preferred
        ntype = str(e.get("type") or "Concept").strip() or "Concept"
        model_conf = e.get("confidence")
        try:
            model_conf = float(model_conf) if model_conf is not None else 0.5
        except (TypeError, ValueError):
            model_conf = 0.5
        conf = blend_confidence(model_conf, quality_q, flagged, settings)
        attrs = _clean_attrs(e.get("attributes"))
        prov = [{"paper": paper_key, "evidence": str(e.get("evidence") or "")[:500]}]
        node_id, is_new = upsert_node(
            conn, node_type=ntype, name=name, confidence=conf,
            aliases=aliases, attributes=attrs, provenance=prov,
        )
        if ntype == "Material":
            local_id = register_material(
                conn, name, synonyms=aliases,
                composition=attrs if isinstance(attrs, dict) else None)
            row = conn.execute(
                "SELECT external_source FROM ontology_nodes WHERE node_id=?",
                (node_id,),
            ).fetchone()
            if not row or not row["external_source"]:
                conn.execute(
                    "UPDATE ontology_nodes SET identity_key=?, external_source='local', "
                    "term_status='local_uncurated' WHERE node_id=?",
                    (local_id, node_id),
                )
        name_to_id[(ntype, name.lower())] = node_id
        name_to_id[("*", name.lower())] = node_id
        stats["entities"] += 1
        stats["new_nodes"] += int(is_new)

    def _resolve(name: str) -> int | None:
        if not name:
            return None
        key = ("*", name.lower().strip())
        if key in name_to_id:
            return name_to_id[key]
        return _lookup_any_type(conn, name)

    for r in data.get("relations") or []:
        if not isinstance(r, dict):
            continue
        src = _resolve(str(r.get("subject") or ""))
        tgt = _resolve(str(r.get("object") or ""))
        if src is None or tgt is None:
            continue
        rtype = str(r.get("type") or "related_to").strip() or "related_to"
        model_conf = r.get("confidence")
        try:
            model_conf = float(model_conf) if model_conf is not None else 0.5
        except (TypeError, ValueError):
            model_conf = 0.5
        conf = blend_confidence(model_conf, quality_q, flagged, settings)
        attrs = {"predicate": str(r.get("predicate") or "")}
        if rtype in strong_relation_set() and conf < settings.strong_edge_min_conf:
            attrs["candidate"] = True
        prov = [{"paper": paper_key, "evidence": str(r.get("evidence") or "")[:500]}]
        _, is_new = upsert_edge(
            conn, relation_type=rtype, src_id=src, tgt_id=tgt,
            confidence=conf, attributes=attrs, provenance=prov,
            evidence_tier=evidence_tier,
        )
        stats["relations"] += 1
        stats["new_edges"] += int(is_new)

    for ev_idx, ev in enumerate(data.get("events") or []):
        if not isinstance(ev, dict):
            continue
        trigger = str(ev.get("trigger") or "").strip()
        etype = str(ev.get("type") or "Event").strip() or "Event"
        participants = [str(p) for p in (ev.get("participants") or []) if str(p).strip()]
        if is_reporting_phrase(trigger) or is_reporting_phrase(etype):
            stats["dropped_garbage"] += 1
            continue
        try:
            model_conf = float(ev.get("confidence") or 0.5)
        except (TypeError, ValueError):
            model_conf = 0.5
        conf = blend_confidence(model_conf, quality_q, flagged, settings)
        attrs = {}
        if ev.get("time") is not None and str(ev.get("time") or "").strip():
            attrs["time"] = _clean_attr_value(ev.get("time"))
        attrs.update(_clean_attrs(ev.get("attributes")))
        prov = [{"paper": paper_key, "evidence": str(ev.get("evidence") or "")[:500]}]
        refs = []
        for participant in participants:
            pid = _resolve(str(participant))
            if pid is not None:
                refs.append(pid)
        add_event_assertion(
            conn, paper_key=paper_key, event_type=etype, trigger=trigger[:300],
            participants=participants, entity_refs=refs,
            time_text=ev.get("time"), attributes=attrs, confidence=conf,
            provenance=prov,
        )
        stats["events"] += 1

    explicit_hyperedges = [x for x in (data.get("hyperedges") or [])
                           if isinstance(x, dict)]
    hyperedge_payloads: list[dict[str, Any]] = []
    if explicit_hyperedges:
        # 显式超边：conditions/measurements 必须原样透传。此前这里只用
        # hyperedge_payloads.extend(...) 的原始 dict，键名保持模型输出，
        # 但下面的构建循环会把缺失字段补成空数组，因此不能丢键。
        for h in explicit_hyperedges:
            payload = dict(h)
            payload.setdefault("conditions", [])
            payload.setdefault("measurements", [])
            hyperedge_payloads.append(payload)
    else:
        # Compatibility adapter: old relation/event output is projected into hyperedges
        # without creating Reaction/Event nodes.
        for r in data.get("relations") or []:
            if not isinstance(r, dict):
                continue
            hyperedge_payloads.append({
                "type": "relation",
                "label": str(r.get("predicate") or r.get("type") or "relation"),
                "members": [
                    {"name": r.get("subject"), "role": "subject"},
                    {"name": r.get("object"), "role": "object"},
                ],
                "conditions": r.get("conditions") or [],
                "measurements": r.get("measurements") or [],
                "confidence": r.get("confidence", 0.5),
                "evidence": r.get("evidence"),
                "attributes": {"relation_type": r.get("type")},
            })
        for ev in data.get("events") or []:
            if not isinstance(ev, dict):
                continue
            conditions = list(ev.get("conditions") or [])
            if ev.get("time") is not None and not any(
                    str(c.get("key") or c.get("condition_key") or "") == "time"
                    for c in conditions if isinstance(c, dict)):
                conditions.append({"key": "time", "operator": "described_as",
                                   "value": ev.get("time"), "unit": None})
            hyperedge_payloads.append({
                "type": "event",
                "label": str(ev.get("trigger") or "event"),
                "members": [
                    {"name": name, "role": "participant"}
                    for name in (ev.get("participants") or [])
                ],
                "conditions": conditions,
                "measurements": ev.get("measurements") or [],
                "confidence": ev.get("confidence", 0.5),
                "evidence": ev.get("evidence"),
                "attributes": ev.get("attributes") or {},
            })

    for h in hyperedge_payloads:
        htype = str(h.get("hyperedge_type") or h.get("type") or "claim").strip()
        label = str(h.get("label") or htype).strip()
        raw_members = h.get("members") or []
        members = []
        for member in raw_members:
            if isinstance(member, str):
                member = {"name": member, "role": "participant"}
            if not isinstance(member, dict):
                continue
            node_id = _resolve(str(member.get("name") or member.get("node_id") or ""))
            if node_id is None and member.get("node_id") is not None:
                try:
                    node_id = int(member["node_id"])
                except (TypeError, ValueError):
                    node_id = None
            if node_id is None:
                continue
            members.append({
                "node_id": node_id,
                "name": member.get("name") or "",
                "role": member.get("role") or "participant",
                "position": member.get("position"),
                "qualifiers": _clean_attrs(member.get("qualifiers") or {}),
            })
        try:
            model_conf = float(h.get("confidence") or 0.5)
        except (TypeError, ValueError):
            model_conf = 0.5
        conf = blend_confidence(model_conf, quality_q, flagged, settings)
        evidence_value = h.get("evidence")
        if isinstance(evidence_value, list):
            evidence_items = [e for e in evidence_value if isinstance(e, dict)]
        else:
            evidence_items = [{"paper": paper_key,
                               "evidence": str(evidence_value or "")[:500]}]
        # 证据定位：把证据句在原文中的字符区间写进 provenance（P2-3）。
        # 抽取层只给句子文本，这里在 clean_text 里回查位置（找不到则留空，
        # 不猜位置）。
        for item in evidence_items:
            span = str(item.get("evidence") or item.get("span_text") or "")
            if not span:
                continue
            item.setdefault("section", None)
            if item.get("char_start") is None and paper_text:
                pos = paper_text.find(span[:120])
                if pos >= 0:
                    item["char_start"] = pos
                    item["char_end"] = pos + len(span[:120])
        _, is_new = upsert_hyperedge(
            conn,
            hyperedge_type=htype,
            label=label,
            members=members,
            conditions=h.get("conditions") or {},
            measurements=h.get("measurements") or [],
            confidence=conf,
            evidence_tier=evidence_tier,
            paper_key=paper_key,
            provenance=evidence_items,
            attributes=_clean_attrs(h.get("attributes") or {}),
        )
        stats["hyperedges"] += 1
        stats["new_hyperedges"] += int(is_new)
    return stats


def make_knowledge_node(model=None,
                        conn: sqlite3.Connection | None = None,
                        settings: Settings | None = None,
                        run_init: bool = True):
    """构造 LangGraph 知识提取节点。model 为 None 时跳过抽取（仅统计预处理）。"""
    settings = settings or default_settings

    def knowledge_node(state: dict) -> dict:
        key = state.get("current_key")
        if not key:
            return {"error": "缺少 current_key", "status": "error"}
        own_conn = conn is None
        db = conn or connect(settings.db_path)
        try:
            if run_init:
                init_ontology(db)
            rec = get_paper(db, key)
            if not rec:
                return {"error": f"文献不存在: {key}", "status": "error"}
            qres = get_quality_result(db, key) or {}
            flagged = bool(state.get("needs_review") or qres.get("needs_review"))
            quality_q = qres.get("quality")
            text = rec.get("clean_text") or ""

            paragraphs = split_paragraphs(text)
            sentences = split_sentences(text)
            chunk_stats: dict[str, Any] = {}
            chunks = chunk_paragraphs(
                paragraphs, settings.max_extract_chars, settings.max_extract_chunks,
                stats=chunk_stats,
            )
            pre_stats = {
                "clean_chars": len(text),
                "paragraphs": len(paragraphs),
                "sentences": len(sentences),
                "chunks": len(chunks),
                # 被 max_chunks 截断的量（P2-1：不再静默丢弃）
                "dropped_chunks": chunk_stats.get("dropped_chunks", 0),
                "dropped_chars": chunk_stats.get("dropped_chars", 0),
            }
            totals: dict[str, Any] = {"entities": 0, "relations": 0, "events": 0,
                                      "hyperedges": 0, "new_nodes": 0,
                                      "new_edges": 0, "new_hyperedges": 0,
                                      "new_types": [],
                                      "dropped_garbage": 0,
                                      "failed_chunks": 0, "errors": [],
                                      "refine_runs": 0, "refine_issues": 0,
                                      "refine_attempts": 0, "refine_failed": 0}
            meta = {
                "title": rec.get("title"), "venue": rec.get("venue"),
                "pub_year": rec.get("pub_year"), "doi": rec.get("doi"),
            }
            if model is None:
                log_event(db, "knowledge", "skipped-no-model", key, pre_stats)
                return {"extraction_report": {"preprocess": pre_stats,
                                              "skipped": "model 未配置"},
                        "status": "extracted"}

            extractor = KnowledgeExtractor(model, settings)
            tier = _evidence_tier(rec.get("title"))
            generic_warning = _generic_relation_warning(db, settings)
            existing_entities = [
                r["label"] for r in db.execute(
                    """
                    SELECT n.node_type || ': ' || n.name AS label
                    FROM ontology_nodes n
                    WHERE EXISTS (
                        SELECT 1 FROM ontology_edges e
                        WHERE e.source_node = n.node_id OR e.target_node = n.node_id
                    )
                    ORDER BY n.last_seen_at DESC LIMIT 150
                    """
                )
            ]
            before_types = {
                r["type_key"]
                for r in db.execute("SELECT type_key FROM ontology_type_registry")
            }
            for chunk in chunks:
                data, refine_stats = extractor.extract_with_refine(
                    chunk, meta, existing_entities, generic_warning,
                    state.get("domain_profile"))
                # 块级抽取失败（LLM 报错/JSON 截断/超时）此前被记为"0 实体但成功"，
                # 现在显式计数并记录首个错误（P1-9）
                chunk_error = (data or {}).get("error")
                if chunk_error:
                    totals["failed_chunks"] += 1
                    totals.setdefault("errors", [])
                    if len(totals["errors"]) < 5:
                        totals["errors"].append(str(chunk_error))
                chunk_result = _upsert_knowledge(
                    db, data if isinstance(data, dict) else {},
                    quality_q=quality_q, flagged=flagged,
                    paper_key=key, settings=settings, evidence_tier=tier,
                    paper_text=text,
                )
                for k in ("entities", "relations", "events", "hyperedges",
                          "new_nodes", "new_edges", "new_hyperedges",
                          "dropped_garbage"):
                    totals[k] += chunk_result[k]
                if refine_stats.get("attempts"):
                    totals["refine_runs"] += 1
                totals["refine_issues"] += refine_stats.get("issues", 0)
                totals["refine_attempts"] += refine_stats.get("attempts", 0)
                totals["refine_failed"] += refine_stats.get("failed_attempts", 0)
            db.commit()
            after_types = {
                r["type_key"]
                for r in db.execute("SELECT type_key FROM ontology_type_registry")
            }
            totals["new_types"] = sorted(after_types - before_types)
            record_ontology_run(db, key, totals, totals["new_types"])
            control_stats = maybe_global_merge(db, settings)
            cleanup_stats = cleanup_local_label_nodes(db)
            view_stats = rebuild_ontology_views(db)
            summary = graph_summary(db)
            report = {"preprocess": pre_stats, "extracted": totals,
                      "ontology": summary,
                      "quality_control": control_stats
                      if control_stats.get("triggered") else None,
                      "ontology_views": view_stats,
                      "local_label_cleanup": cleanup_stats}
            failed_chunks = int(totals.get("failed_chunks") or 0)
            if failed_chunks and not any(
                    totals.get(k) for k in ("entities", "relations",
                                            "events", "hyperedges")):
                # 全部块都失败 → 明确标记失败，不与"这篇确实没抽到东西"混淆
                report["failed_chunks"] = failed_chunks
                log_event(db, "knowledge", "extract-failed", key, report)
                return {"extraction_report": report, "status": "extract_failed",
                        "error": (totals.get("errors") or ["抽取失败"])[0]}
            if failed_chunks:
                report["failed_chunks"] = failed_chunks
            log_event(db, "knowledge", "extracted", key, report)
            # 注意：不覆盖顶层 decision（knowledge/flagged 由质量节点给出）
            return {"extraction_report": report, "status": "extracted"}
        finally:
            if own_conn:
                db.close()

    return knowledge_node
