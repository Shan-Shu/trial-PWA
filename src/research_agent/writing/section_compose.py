"""单段撰写：把"充足"的证据写成一段**可回溯**的正文。

与 ``study/content.py`` 的区别（重要）：

``make_content_node`` 面对的是**整篇研究任务**，输出多候选方案池（``candidates``），
用于段落会产出"一段里塞三个方案"的废稿。本模块只做一件事：在已判定**证据充足**
的前提下，为某个大纲节点写一段正文，并且把每个引文编号绑定到真实
``paper_key`` / ``hyperedge_id``——正文里的 ``[3]`` 必须能查出是哪篇文献的哪条证据。

无模型时**不伪装**：返回 ``generated_by="skeleton_fallback"`` 的骨架草稿，
并在正文顶部写明原因（与 ``writing/service.py`` 同一诚实约定）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "build_materials",
    "render_material_digest",
    "parse_citation_indices",
    "bind_citations",
    "compose_section",
    "render_gap_notice",
    "with_gap_notice",
    "DIMENSION_GAP_PHRASES",
]

#: 未满足的必考维度 → 人话表述（正文顶部的缺口标注直接用它）
DIMENSION_GAP_PHRASES = {
    "papers": "可引用的同主题文献不足",
    "knowledge": "已入库文献尚未抽取成知识",
    "conditions": "缺少可量化条件（温度/当量/时间/收率等）",
    "comparison": "缺少可两两比较的同类证据",
    "requirements": "部分写作要求没有证据支撑",
    "evidence": "可追溯的证据编号不足",
}

#: 正文中的引文编号：``[1]``、``[1,2]``、``[1-3]``
_CITATION_RE = re.compile(r"\[(\d+(?:\s*[,\-–]\s*\d+)*)\]")


def build_materials(conn: sqlite3.Connection, paper_keys: list[str],
                    evidence_ids: list[str] | None = None,
                    limit: int = 12) -> list[dict[str, Any]]:
    """把充分性判定命中的文献整理成**带编号的素材清单**。

    编号即正文可用的引文下标（1 起），因此素材顺序必须稳定——按
    ``paper_key`` 排序，避免同一句话在两次运行里指向不同文献。
    """
    keys = [str(k) for k in (paper_keys or []) if k][:max(1, int(limit))]
    if not keys:
        return []
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT p.paper_key, p.title, p.abstract, p.venue, p.pub_year, p.doi, "
        f"       q.quality "
        f"FROM papers p LEFT JOIN quality_results q ON q.paper_key = p.paper_key "
        f"WHERE p.paper_key IN ({placeholders})",
        keys,
    ).fetchall()
    by_key = {r["paper_key"]: dict(r) for r in rows}

    evidence = [str(e) for e in (evidence_ids or [])]
    materials: list[dict[str, Any]] = []
    # 保序：先按命中顺序，缺失的跳过（不塞占位符，否则编号会指向空素材）
    for key in keys:
        item = by_key.get(key)
        if not item:
            continue
        own_evidence: list[dict[str, Any]] = []
        for token in evidence:
            if token.startswith("H-"):
                try:
                    hyperedge_id = int(token[2:])
                except ValueError:
                    continue
                row = conn.execute(
                    "SELECT h.hyperedge_id, h.label, h.paper_key, "
                    "       (SELECT COUNT(*) FROM ontology_hyperedge_conditions c "
                    "        WHERE c.hyperedge_id = h.hyperedge_id) AS conditions, "
                    "       (SELECT COUNT(*) FROM ontology_hyperedge_measurements m "
                    "        WHERE m.hyperedge_id = h.hyperedge_id) AS measurements "
                    "FROM ontology_hyperedges h WHERE h.hyperedge_id = ?",
                    (hyperedge_id,),
                ).fetchone()
                if row and row["paper_key"] == key:
                    own_evidence.append(dict(row))
        item["evidence"] = own_evidence
        materials.append(item)
    return materials


def render_material_digest(materials: list[dict[str, Any]]) -> str:
    """渲染给模型看的素材块；编号与 ``[n]`` 一一对应。"""
    if not materials:
        return "（无可用素材）"
    lines: list[str] = []
    for index, item in enumerate(materials, 1):
        title = str(item.get("title") or "未命名")
        venue = str(item.get("venue") or "")
        year = item.get("pub_year") or ""
        head = f"[{index}] {title} — {venue} {year}".strip()
        lines.append(head)
        abstract = str(item.get("abstract") or "").strip()
        if abstract:
            lines.append(f"    摘要：{abstract[:500]}")
        for ev in item.get("evidence") or []:
            parts = [str(ev.get("label") or f"超边 {ev.get('hyperedge_id')}")]
            if ev.get("conditions"):
                parts.append(f"{ev['conditions']} 项条件")
            if ev.get("measurements"):
                parts.append(f"{ev['measurements']} 项测量")
            lines.append(f"    证据 H-{int(ev['hyperedge_id']):04d}：" + "，".join(parts))
    return "\n".join(lines)


def parse_citation_indices(text: str) -> list[int]:
    """抽出正文用到的所有引文编号（去重、升序，区间 ``[5-7]`` 展开为 5,6,7）。"""
    out: set[int] = set()
    for group in _CITATION_RE.findall(str(text or "")):
        for chunk in group.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            bounds = re.split(r"[-–]", chunk)
            if len(bounds) == 2 and all(b.strip().isdigit() for b in bounds):
                start, end = (int(bounds[0]), int(bounds[1]))
                # 倒序区间按"无意义"处理，不静默反转
                if start <= end and end - start <= 200:
                    out.update(range(start, end + 1))
                continue
            if chunk.isdigit():
                out.add(int(chunk))
    return sorted(out)


def bind_citations(text: str, materials: list[dict[str, Any]]) -> dict[str, Any]:
    """把正文的 ``[n]`` 绑定到真实文献与证据，并抓出越界编号。

    ``invalid_indices`` 非空即说明模型编造了引文——必须让调用方看见，
    而不是悄悄过滤掉（那会让"引用了不存在文献"的残稿看起来像正常产出）。
    """
    used = parse_citation_indices(text)
    bindings: list[dict[str, Any]] = []
    invalid: list[int] = []
    for index in used:
        if 1 <= index <= len(materials):
            item = materials[index - 1]
            bindings.append({
                "index": index,
                "paper_key": item.get("paper_key"),
                "title": item.get("title"),
                "evidence_ids": [f"H-{int(e['hyperedge_id']):04d}"
                                 for e in (item.get("evidence") or [])],
            })
        else:
            invalid.append(index)
    return {
        "bindings": bindings,
        "invalid_indices": invalid,
        "used_indices": used,
        "citation_ids": [b["paper_key"] for b in bindings],
    }


def render_gap_notice(unmet_dimensions: list[str],
                      evidence_types: list[str] | None = None,
                      detail: str = "") -> str:
    """生成"本节证据缺口"标注块（Markdown 引用块）。

    用户明确要求：证据不足或缺少确定性条件时**可以写出来，但必须标注**，
    并允许在标注范围内作一定的自由发挥与创新。
    """
    if not unmet_dimensions and not evidence_types:
        return ""
    lines = ["> **本节证据缺口（写作时请留意）**"]
    phrases = [DIMENSION_GAP_PHRASES.get(str(d), str(d))
               for d in (unmet_dimensions or [])]
    if phrases:
        lines.append("> - " + "；".join(phrases))
    if evidence_types:
        from research_agent.writing.section_template import EVIDENCE_TYPE_LABELS
        labels = [EVIDENCE_TYPE_LABELS.get(str(t), str(t))
                  for t in evidence_types]
        lines.append("> - 缺少的证据类型：" + "、".join(labels))
    lines.append("> - 以下内容含**未经本库证据支持的推断或一般性描述**，"
                 "已用「（推断）」标出，请勿直接作为结论引用。")
    if detail:
        lines.append(f"> - 判定依据：{detail}")
    return "\n".join(lines)


def with_gap_notice(content: str, unmet_dimensions: list[str],
                    evidence_types: list[str] | None = None,
                    detail: str = "") -> str:
    """把缺口标注插到正文最前面（幂等：已标注过就不再插）。"""
    notice = render_gap_notice(unmet_dimensions, evidence_types, detail)
    if not notice:
        return content
    if "本节证据缺口" in str(content or ""):
        return content
    body = str(content or "").lstrip()
    return f"{notice}\n\n{body}" if body else notice


def _skeleton(heading: str, instruction: str, reason: str,
              materials: list[dict[str, Any]]) -> str:
    lines = [
        f"## {heading}",
        "",
        f"> 骨架草稿（原因：{reason}）——**未生成正文**，"
        "下方为可按编号引用的素材。",
    ]
    if instruction:
        lines.extend(["", "结构化输入：", instruction])
    lines.extend(["", "可用素材：", render_material_digest(materials)])
    return "\n".join(lines)


def _safe_format(template: str, values: dict[str, Any]) -> str:
    """按 ``{key}`` 填充；模板里有引擎不认识的占位符时留空而不是抛 KeyError。

    这样用户可以在 pack 里自由加占位符（哪怕是别的工具用的），
    不会因为一个未知 ``{foo}`` 就让整条成段链路失败。
    """
    class _Keep(dict):
        def __missing__(self, key: str) -> str:  # noqa: D105
            return ""

    try:
        return template.format_map(_Keep(values))
    except (IndexError, ValueError) as exc:
        # 大括号未闭合等语法问题：退回不填充的原文，至少让内容能出来
        logger.warning("提示词模板填充失败（将按原文使用）: %s", exc)
        return template


def compose_section(
    *,
    heading: str,
    instruction: str = "",
    plan: dict[str, Any] | None = None,
    materials: list[dict[str, Any]] | None = None,
    sufficiency: dict[str, Any] | None = None,
    model: Any = None,
    model_reason: str = "",
    section_note: str = "",
    words: int = 0,
    fields_block: str = "",
    unmet_dimensions: list[str] | None = None,
    evidence_types: list[str] | None = None,
    role: str = "",
) -> dict[str, Any]:
    """为单个部分生成正文（或显式骨架草稿）。

    ``fields_block`` 是模板渲染出的**结构化字段块**（每项都标了来源：
    用户指定 / 规划拟定 / 模板默认），替代原来那条自由文本指令。

    返回 ``{content, generated_by, citations, bindings, invalid_indices,
    material_count, model_error, gap_notice}``；**不落库**——落库由
    ``section_service`` 统一做，保证"生成"与"状态流转"只有一个写入点。
    """
    materials = list(materials or [])
    sufficiency = sufficiency or {}
    plan = plan or {}
    unmet_dimensions = list(unmet_dimensions or [])

    goal = str(plan.get("goal") or "").strip()
    target_words = int(words or 0) or 600
    model_error: str | None = None
    generated_by = "llm"

    if model is None:
        model_error = model_reason or "未提供模型"
        generated_by = "skeleton_fallback"
        content = _skeleton(heading, fields_block or instruction, model_error,
                            materials)
    else:
        try:
            from langchain_core.messages import HumanMessage

            from research_agent import packs

            data = packs.skill_data("writing") or {}
            prompts = data.get("prompts") or {}
            template = str(prompts.get("section_compose_user") or "").strip()
            if not template:
                template = (
                    "研究主题：{topic}\n"
                    "本节标题：{heading}\n"
                    "本部分职责：{role}\n"
                    "写作要求：{note}\n"
                    "节点指令：{instruction}\n"
                    "目标字数：{words}\n\n"
                    "可用素材（引文编号即方括号数字，**只能引用下列编号**）：\n"
                    "{materials}\n\n"
                    "充分性判定依据：{basis}\n\n"
                    "允许带缺口的写作说明：{gap_hint}\n\n"
                    "请只输出本节正文（Markdown，不要重复标题），"
                    "每处事实性陈述后用 [编号] 标注来源；不得引用清单外的编号。"
                )
            gap_hint = (
                "本节缺少以下证据：" + "；".join(
                    DIMENSION_GAP_PHRASES.get(str(d), str(d))
                    for d in unmet_dimensions)
                + "。你可以据此写出一般性描述或**明确标注的推断**（写成「（推断）」），"
                  "但不得把这些内容伪装成有证据支持的结论。"
            ) if unmet_dimensions else "无（证据齐备）"
            prompt = _safe_format(template, {
                "topic": goal or str(plan.get("domain") or "") or "未指定",
                "heading": heading,
                "role": role or "未指定",
                "note": section_note or "无特殊要求",
                "instruction": fields_block or instruction or "（无额外指令）",
                "words": target_words,
                "materials": render_material_digest(materials),
                "basis": "；".join(str(r) for r in (sufficiency.get("reasons") or [])[:4])
                         or "无",
                "gap_hint": gap_hint,
            })
            from research_agent.logging import logged_invoke
            msg = logged_invoke(model, prompt, node="content_builder",
                                role="content")
            content = str(getattr(msg, "content", msg) or "").strip()
            if not content:
                raise ValueError("模型返回空内容")
        except Exception as exc:  # noqa: BLE001 —— 撰写失败不应中断整个节点流程
            logger.warning("段落撰写失败，回退骨架草稿: %s", exc)
            model_error = f"{type(exc).__name__}: {exc}"
            generated_by = "skeleton_fallback"
            content = _skeleton(heading, fields_block or instruction, model_error,
                                materials)

    # 带缺口写作时，**在正文最前面显式标注**（用户要求：可以写，但必须标出来）
    gap_notice = ""
    if unmet_dimensions:
        gap_notice = render_gap_notice(
            unmet_dimensions, evidence_types,
            detail="；".join(str(r) for r in (sufficiency.get("reasons") or [])[:2]))
        content = with_gap_notice(content, unmet_dimensions, evidence_types,
                                  detail="；".join(
                                      str(r) for r in
                                      (sufficiency.get("reasons") or [])[:2]))

    binding = bind_citations(content, materials)
    return {
        "content": content,
        "generated_by": generated_by,
        "model_error": model_error,
        "material_count": len(materials),
        "citations": binding["citation_ids"],
        "bindings": binding["bindings"],
        "invalid_indices": binding["invalid_indices"],
        "used_indices": binding["used_indices"],
        "gap_notice": gap_notice,
        "unmet_dimensions": unmet_dimensions,
    }
