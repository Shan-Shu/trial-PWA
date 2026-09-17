"""事实核查节点（Fact Checker）。

对标 GPT Researcher 的 Fact Checker：在内容形成与审核之后，专门检查

1. 事实错误：与知识包内证据句相矛盾的断言；
2. 无来源断言：supported 结论没有挂任何真实引用；
3. 前后矛盾：同一草稿内相互冲突的表述。

与审核节点的分工：
- 审核节点判"是否完成用户指令 + 证据正确性"（含设计契约）；
- 事实核查节点只判**可核查的事实性**，并把问题写入 revision_actions 回流内容节点。

代码层先做确定性检查（引用存在性、无来源断言、数字-证据一致性）；
模型可用时再追加语义级检查，但确定性结论始终优先。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage

from research_agent.study.content import render_draft
from research_agent.study.json_utils import clean_str, parse_json_object
from research_agent.study.model_call import invoke_with_timeout

logger = logging.getLogger(__name__)

FACT_CHECK_PROMPT = """你是事实核查节点。你的职责不是改写内容，而是找出草稿中的事实性问题。

只检查三类问题：
1. fabrication：与给定证据句矛盾、或证据中并不存在的断言；
2. unsupported_claim：标记为 supported 但没有绑定真实 evidence_id / hyperedge_id 的断言；
3. contradiction：同一草稿内部前后矛盾的表述（例如同一数字或机制给出两种说法）。

规则：
- 只能用 knowledge 中真实存在的 pattern_id / evidence_id / hyperedge_id 作为依据；
- 不确定的问题标 severity="low"，不要为了凑数把假设说成错误；
- 每条问题必须给出 location（章节/候选编号）与 revision_action；
- 只输出 JSON 对象，不要代码块。

输出结构：
{{
  "decision": "pass|revise",
  "issues": [
    {{"type": "fabrication|unsupported_claim|contradiction",
      "severity": "high|medium|low",
      "location": "章节或候选编号",
      "problem": "问题描述",
      "evidence_ids": [],
      "revision_action": "应当如何修改"}}
  ],
  "summary": "一句话结论"
}}

knowledge:
{knowledge}

draft:
{draft}

请直接输出 JSON："""

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|°C|℃|ee|dr|equiv|mol|h|min)", re.I)


def _reference_sets(knowledge: dict[str, Any]) -> tuple[set[str], set[str]]:
    """收集知识包内**所有**合法编号。

    除模式卡（P-）与证据卡（E-/H-）外，还必须包含消费节点生成的
    ``design_context`` 编号（``MS-xxxx`` / ``GAP-xxxx`` / ``OP-xxxx``）——
    内容节点按提示词把机制证据写成 ``MS-``/``GAP-``，若不在白名单内
    会被判为"伪造引用"，导致生成型任务几乎无法通过事实核查。
    """
    patterns = {
        str(p.get("pattern_id") or "")
        for p in knowledge.get("patterns") or [] if p.get("pattern_id")
    }
    evidence = {
        str(e.get("evidence_id") or "")
        for e in knowledge.get("evidence") or [] if e.get("evidence_id")
    }
    for h in knowledge.get("hyperedges") or []:
        if h.get("hyperedge_id") is None:
            continue
        hid = int(h["hyperedge_id"])
        evidence.add(f"H-{hid:04d}")
        for i in range(len(h.get("evidence") or [])):
            evidence.add(f"H-{hid:04d}-{i + 1}")
        # 超边自带的证据编号（消费节点已生成）
        evidence.update(str(x) for x in h.get("evidence_ids") or [] if x)
    design = knowledge.get("design_context") or {}
    for key, id_key in (("mechanism_states", "state_id"),
                        ("opportunity_gaps", "gap_id"),
                        ("operator_candidates", "op_id"),
                        ("reaction_primitives", "op_id")):
        for item in design.get(key) or []:
            if isinstance(item, dict) and clean_str(item.get(id_key)):
                evidence.add(str(item[id_key]).strip())
    return patterns, evidence


def _evidence_text(knowledge: dict[str, Any]) -> str:
    parts = [clean_str(e.get("sentence"), "")
             for e in knowledge.get("evidence") or []]
    for h in knowledge.get("hyperedges") or []:
        parts.append(clean_str(h.get("label"), ""))
        for ev in h.get("evidence") or []:
            parts.append(clean_str(ev.get("span_text"), ""))
    return " \n".join(x for x in parts if x)


def deterministic_fact_check(draft: dict[str, Any],
                             knowledge: dict[str, Any]) -> dict[str, Any]:
    """确定性核查：引用存在性、无来源断言、数字缺证据。"""
    pattern_ids, evidence_ids = _reference_sets(knowledge)
    known = pattern_ids | evidence_ids
    issues: list[dict[str, Any]] = []

    referenced: set[str] = set()

    def refs_of(item: dict[str, Any]) -> list[str]:
        out = [str(x) for x in item.get("pattern_ids") or []]
        out += [str(x) for x in item.get("evidence_ids") or []]
        out += [str(x) for x in item.get("mechanism_evidence") or []]
        return [x for x in out if x]

    summary = draft.get("summary") or {}
    referenced.update(refs_of(summary))
    invented = sorted(x for x in refs_of(summary) if x not in known)
    if invented:
        issues.append({
            "type": "fabrication", "severity": "high", "location": "summary",
            "problem": f"摘要引用了不存在的编号：{invented}",
            "evidence_ids": [], "revision_action": "删除或替换为知识包中真实存在的编号",
        })

    for si, section in enumerate(draft.get("sections") or []):
        heading = clean_str(section.get("heading"), f"section[{si}]")
        for ii, item in enumerate(section.get("items") or []):
            refs = refs_of(item)
            referenced.update(refs)
            location = f"{heading} · items[{ii}]"
            bad = [x for x in refs if x not in known]
            if bad:
                issues.append({
                    "type": "fabrication", "severity": "high", "location": location,
                    "problem": f"引用了不存在的编号：{bad}",
                    "evidence_ids": [],
                    "revision_action": "删除无效引用或改用真实证据编号",
                })
            if clean_str(item.get("status"), "supported") == "supported" and not refs:
                issues.append({
                    "type": "unsupported_claim", "severity": "high",
                    "location": location,
                    "problem": "标记为 supported 但没有绑定任何真实证据编号",
                    "evidence_ids": [],
                    "revision_action": "补上真实 evidence_id，或把 status 改为 hypothesis",
                })

    corpus_text = _evidence_text(knowledge)
    for candidate in draft.get("strategies") or []:
        title = clean_str(candidate.get("title"), "候选")
        refs = refs_of(candidate)
        referenced.update(refs)
        bad = [x for x in refs if x not in known]
        if bad:
            issues.append({
                "type": "fabrication", "severity": "high",
                "location": f"候选 {title}",
                "problem": f"候选引用了不存在的编号：{bad}",
                "evidence_ids": [],
                "revision_action": "改用真实证据编号，或标注为纯假设",
            })
        if not refs and clean_str(candidate.get("status"), "hypothesis") == "supported":
            issues.append({
                "type": "unsupported_claim", "severity": "medium",
                "location": f"候选 {title}",
                "problem": "候选被标为 supported 但没有依据编号",
                "evidence_ids": [],
                "revision_action": "改为 hypothesis 或补上依据",
            })
        blob = " ".join([
            clean_str(candidate.get("rationale")), clean_str(candidate.get("novelty_source")),
            clean_str(candidate.get("target")),
        ])
        for number in set(_NUMBER_RE.findall(blob)):
            if number.lower() not in corpus_text.lower():
                issues.append({
                    "type": "fabrication", "severity": "medium",
                    "location": f"候选 {title}",
                    "problem": f"出现知识包中未见的具体数值：{number}",
                    "evidence_ids": [],
                    "revision_action": "删除该数值，或补充其来源证据",
                })

    # 未被任何内容使用的引用不影响事实性；仅统计悬空引用
    decision = "revise" if any(i["severity"] in ("high", "medium")
                               for i in issues) else "pass"
    return {
        "decision": decision,
        "issues": issues,
        "summary": (f"事实核查发现 {len(issues)} 个问题"
                    if issues else "事实核查未发现问题"),
        "checked_claims": len(referenced),
        "mode": "deterministic",
    }


def normalize_fact_check(data: dict[str, Any] | None,
                         fallback: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return fallback
    issues: list[dict[str, Any]] = []
    for item in data.get("issues") or []:
        if not isinstance(item, dict):
            continue
        severity = clean_str(item.get("severity"), "low").lower()
        if severity not in ("high", "medium", "low"):
            severity = "low"
        issues.append({
            "type": clean_str(item.get("type"), "unknown"),
            "severity": severity,
            "location": clean_str(item.get("location")),
            "problem": clean_str(item.get("problem")),
            "evidence_ids": [str(x) for x in item.get("evidence_ids") or []],
            "revision_action": clean_str(item.get("revision_action")),
        })
    decision = clean_str(data.get("decision"), "revise").lower()
    if decision not in ("pass", "revise"):
        decision = "revise"
    if any(i["severity"] == "high" for i in issues):
        decision = "revise"
    out = {
        "decision": decision,
        "issues": issues,
        "summary": clean_str(data.get("summary"), ""),
        "checked_claims": fallback.get("checked_claims", 0),
        "mode": "llm",
    }
    # 确定性发现的高危问题不允许被模型判 pass 抹掉
    hard = [i for i in fallback.get("issues") or [] if i["severity"] == "high"]
    if hard:
        merged = {f"{i['type']}|{i['location']}|{i['problem']}": i for i in out["issues"]}
        for item in hard:
            merged.setdefault(f"{item['type']}|{item['location']}|{item['problem']}",
                              item)
        out["issues"] = list(merged.values())
        out["decision"] = "revise"
    return out


def make_fact_check_node(model=None, settings: Any = None,
                         conn: Any = None):
    """构造事实核查节点。model 为 None 时只做确定性核查。"""
    from research_agent.study.events import log_study_event

    settings = settings

    def fact_check_node(state: dict) -> dict:
        draft = state.get("draft") or {}
        knowledge = state.get("knowledge") or {}
        run_id = state.get("run_id")
        fallback = deterministic_fact_check(draft, knowledge)
        result = fallback
        if model is not None:
            knowledge_view = {
                "patterns": (knowledge.get("patterns") or [])[:30],
                "evidence": (knowledge.get("evidence") or [])[:60],
                "hyperedges": [{
                    "reference_id": h.get("reference_id"),
                    "label": h.get("label"),
                    "evidence_ids": h.get("evidence_ids") or [],
                } for h in (knowledge.get("hyperedges") or [])[:40]],
            }
            prompt = FACT_CHECK_PROMPT.replace(
                "{knowledge}", json.dumps(knowledge_view, ensure_ascii=False, indent=2)
            ).replace(
                "{draft}", json.dumps({
                    "title": draft.get("title"),
                    "sections": draft.get("sections"),
                    "strategies": draft.get("strategies") or [],
                    "markdown": render_draft(draft),
                }, ensure_ascii=False, indent=2))
            try:
                raw, call_diag = invoke_with_timeout(
                    model, prompt, node="fact_checker", role="fact_check",
                    timeout=getattr(settings, "study_fact_check_timeout", 300)
                    if settings is not None else 300)
                if raw is None:
                    raise RuntimeError(call_diag.get("error") or "模型调用失败")
                parsed = parse_json_object(raw)
                result = normalize_fact_check(parsed, fallback)
                result["model_call"] = call_diag
            except Exception as exc:  # noqa: BLE001
                logger.warning("事实核查模型调用失败，使用确定性结果: %s", exc)
                result["model_error"] = str(exc)
        if conn is not None or settings is not None:
            log_study_event(conn, settings, "fact_checker", run_id,
                            "done" if result["decision"] == "pass" else "issues",
                            {"decision": result["decision"],
                             "issues": len(result.get("issues") or []),
                             "mode": result.get("mode")})
        return {"fact_check": result, "status": "reviewed"}

    return fact_check_node
