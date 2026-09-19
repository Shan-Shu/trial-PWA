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
    "render_knowledge_digest",
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


def _value_text(rec: dict[str, Any]) -> str:
    """取值：优先 ``value_text``，退回 ``value_num``（去尾零）。"""
    text = str(rec.get("value") or "").strip()
    if text:
        return text
    num = rec.get("num")
    if num is None:
        return ""
    try:
        return f"{float(num):g}"
    except (TypeError, ValueError):
        return str(num)


def _join_units(core: str, unit: str, qualifier: str) -> str:
    if unit:
        core = f"{core} {unit}".strip()
    if qualifier:
        core = f"{core}（{qualifier}）" if core else qualifier
    return core


def format_condition(rec: dict[str, Any]) -> str:
    """一条量化条件 → 人话（如 ``温度 ≤ 80 °C``）。

    **这是本轮修复的核心**：此前只把条件的**条数**交给模型（"2 项条件"），
    模型看不到任何数值，于是正文只能写"在优化条件下"这类空话。
    """
    key = str(rec.get("key") or "").strip()
    operator = str(rec.get("operator") or "").strip()
    core = " ".join(p for p in (key, operator, _value_text(rec)) if p).strip()
    return _join_units(core, str(rec.get("unit") or "").strip(),
                       str(rec.get("qualifier") or "").strip())


def format_measurement(rec: dict[str, Any]) -> str:
    """一条测量 → 人话（如 ``收率 92 %``）。"""
    metric = str(rec.get("metric") or "").strip()
    core = " ".join(p for p in (metric, _value_text(rec)) if p).strip()
    return _join_units(core, str(rec.get("unit") or "").strip(),
                       str(rec.get("qualifier") or "").strip())


def _hyperedge_details(conn: sqlite3.Connection,
                       hyperedge_id: int) -> tuple[list[str], list[str]]:
    """取该超边的**条件与测量明细**（不是条数），供正文直接引用。"""
    conditions: list[str] = []
    for row in conn.execute(
        "SELECT condition_key, operator, value_text, value_num, unit, qualifier "
        "FROM ontology_hyperedge_conditions WHERE hyperedge_id=? "
        "ORDER BY id LIMIT 12", (int(hyperedge_id),),
    ).fetchall():
        text = format_condition({
            "key": row["condition_key"], "operator": row["operator"],
            "value": row["value_text"], "num": row["value_num"],
            "unit": row["unit"], "qualifier": row["qualifier"]})
        if text:
            conditions.append(text)
    measurements: list[str] = []
    for row in conn.execute(
        "SELECT metric, value_text, value_num, unit, qualifier "
        "FROM ontology_hyperedge_measurements WHERE hyperedge_id=? "
        "ORDER BY id LIMIT 12", (int(hyperedge_id),),
    ).fetchall():
        text = format_measurement({
            "metric": row["metric"], "value": row["value_text"],
            "num": row["value_num"], "unit": row["unit"],
            "qualifier": row["qualifier"]})
        if text:
            measurements.append(text)
    return conditions, measurements


def _papers_owning_evidence(conn: sqlite3.Connection, evidence: list[str],
                            limit: int) -> list[str]:
    """从证据编号回溯出**归属文献**（保序去重）。

    每条超边都带 ``paper_key``，所以"命中文献为 0"并不等于"没有可用素材"。
    实测：中文主题在英文库里 ``matched_papers=0``，但库里有 233 条真超边，
    此前 `build_materials` 直接交白卷 —— 成段既拿不到引文，也拿不到我们
    刚从超边里取出的条件与测量数值。
    """
    out: list[str] = []
    for token in evidence:
        if not token.startswith("H-"):
            continue
        try:
            hyperedge_id = int(token[2:])
        except ValueError:
            continue
        row = conn.execute(
            "SELECT paper_key FROM ontology_hyperedges WHERE hyperedge_id = ?",
            (hyperedge_id,)).fetchone()
        key = str((row["paper_key"] if row else "") or "")
        if key and key not in out:
            out.append(key)
        if len(out) >= limit:
            break
    return out


def build_materials(conn: sqlite3.Connection, paper_keys: list[str],
                    evidence_ids: list[str] | None = None,
                    limit: int = 12) -> list[dict[str, Any]]:
    """把充分性判定命中的文献整理成**带编号的素材清单**。

    编号即正文可用的引文下标（1 起），因此素材顺序必须稳定——按
    ``paper_key`` 排序，避免同一句话在两次运行里指向不同文献。

    每条证据都带上该超边的**条件与测量明细**（含数值与单位），
    而不只是条数：正文要能写出"80 °C / 92 % 收率"这种具体内容。

    ``paper_keys`` 为空时**退回由证据编号回溯归属文献**，而不是交白卷：
    文献匹配失败（如中文主题配英文库）不该把库里已有的超边与数值一并丢掉。
    """
    evidence = [str(e) for e in (evidence_ids or [])]
    cap = max(1, int(limit))
    keys = [str(k) for k in (paper_keys or []) if k][:cap]
    if not keys and evidence:
        keys = _papers_owning_evidence(conn, evidence, cap)
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
                    "SELECT hyperedge_id, label, paper_key "
                    "FROM ontology_hyperedges WHERE hyperedge_id = ?",
                    (hyperedge_id,),
                ).fetchone()
                if row and row["paper_key"] == key:
                    detail = dict(row)
                    detail["conditions"], detail["measurements"] = \
                        _hyperedge_details(conn, hyperedge_id)
                    own_evidence.append(detail)
        item["evidence"] = own_evidence
        materials.append(item)
    return materials


def render_material_digest(materials: list[dict[str, Any]]) -> str:
    """渲染给模型看的素材块；编号与 ``[n]`` 一一对应。

    证据行给出**条件与测量的具体数值**，而不是"N 项条件"这类计数——
    计数让模型无从下笔，只能产出空泛段落（本轮修复的核心问题）。
    """
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
            lines.append(f"    证据 H-{int(ev['hyperedge_id']):04d}："
                         + str(ev.get("label") or "（无标签）"))
            conditions = [str(c) for c in (ev.get("conditions") or []) if str(c)]
            if conditions:
                lines.append("      条件：" + "；".join(conditions))
            measurements = [str(m) for m in (ev.get("measurements") or []) if str(m)]
            if measurements:
                lines.append("      测量：" + "；".join(measurements))
            if not conditions and not measurements:
                lines.append("      （该超边未记录可量化条件或测量）")
    return "\n".join(lines)


#: 知识消费产物（`design_context`）里要送进提示词的键与中文标签
_KNOWLEDGE_SECTIONS: tuple[tuple[str, str], ...] = (
    ("mechanism_states", "机制状态"),
    ("reaction_primitives", "反应基元"),
    ("operator_candidates", "候选算子链"),
    ("opportunity_gaps", "机会缺口"),
    ("constraint_conflicts", "约束冲突"),
)

#: 各小节取"主文本"时的候选字段（结构由模型或确定性兜底产出，可能缺键）
_KNOWLEDGE_FIELDS: dict[str, tuple[str, ...]] = {
    "mechanism_states": ("label", "state_id"),
    "reaction_primitives": ("label_zh", "name", "op_id"),
    "opportunity_gaps": ("missing_link", "gap_id"),
    "constraint_conflicts": ("label", "description", "conflict", "missing_link",
                             "feature", "gap_id"),
}

#: `consumer_analysis` 里的列表字段 → 中文标签
_ANALYSIS_LISTS: tuple[tuple[str, str], ...] = (
    ("mechanism_clusters", "机制簇"),
    ("known_conflicts", "已知冲突"),
    ("evidence_gaps", "证据缺口"),
)


def _first_text(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _chain_text(chain: Any) -> str:
    """算子链 → ``氧化 [A→B] → 环化 [B→C]``。"""
    parts: list[str] = []
    for step in (chain or []):
        if isinstance(step, dict):
            operator = str(step.get("operator") or step.get("name") or "").strip()
            src = str(step.get("input") or "").strip()
            dst = str(step.get("output") or "").strip()
            text = f"{operator}[{src}→{dst}]" if (src or dst) else operator
        else:
            text = str(step).strip()
        if text:
            parts.append(text)
    return " → ".join(parts)


def _trace_text(item: dict[str, Any]) -> str:
    """溯源说明：有编号就列出来，没有就**明说这是推断**（不伪装成有据）。"""
    ids = [str(x) for x in (item.get("hyperedge_ids") or []) if str(x)][:6]
    if not ids:
        ids = [str(x) for x in (item.get("evidence_ids") or []) if str(x)][:6]
    if ids:
        return "溯源 " + "、".join(ids)
    return "无可溯源编号（属推断）"


def _render_knowledge_item(section: str, item: dict[str, Any]) -> str:
    if section == "operator_candidates":
        name = _first_text(item, ("label_zh", "name", "op_id"))
        chain = _chain_text(item.get("operator_chain"))
        text = f"{name}｜{chain}" if (name and chain) else (chain or name)
    elif section == "reaction_primitives":
        name = _first_text(item, ("label_zh", "name", "op_id"))
        src = str(item.get("input_state") or "").strip()
        dst = str(item.get("output_state") or "").strip()
        text = f"{name}[{src}→{dst}]" if (name and (src or dst)) else (name or "")
    else:
        text = _first_text(item, _KNOWLEDGE_FIELDS.get(section, ("label",)))
    text = text.strip()
    if not text:
        return ""
    return f"{text}（{_trace_text(item)}）"


def render_knowledge_digest(knowledge: dict[str, Any] | None) -> str:
    """把**知识消费节点的产出**渲染成提示词块。

    这是本轮修复的另一半：``compose_node`` 此前只读充分性判定，消费节点产出的
    机制状态、候选算子链、机会缺口与约束冲突**全部被丢弃**——写作时自然写不出
    机制与设计层面的内容，只能复述摘要。这里把它们（连同溯源编号）交回给模型。
    """
    if not isinstance(knowledge, dict) or not knowledge:
        return "（本节没有知识消费产物：机制状态与算子链不可用）"
    design = knowledge.get("design_context") or {}
    analysis = knowledge.get("consumer_analysis") or {}
    lines: list[str] = []

    head: list[str] = []
    mode = str(analysis.get("consumer_mode") or design.get("mode") or "").strip()
    if mode:
        head.append(f"消费模式 {mode}")
    confidence = analysis.get("confidence")
    if confidence is not None:
        head.append(f"置信度 {confidence}")
    if head:
        lines.append("· " + "，".join(head))

    summary = str(analysis.get("summary") or "").strip()
    if summary:
        lines.append("· 机制综述：" + summary[:400])

    for key, label in _ANALYSIS_LISTS:
        values = [str(x).strip() for x in (analysis.get(key) or []) if str(x).strip()]
        if values:
            lines.append(f"· {label}：" + "；".join(values[:6]))

    for section, label in _KNOWLEDGE_SECTIONS:
        items = [x for x in (design.get(section) or []) if isinstance(x, dict)]
        rendered = [text for text in
                    (_render_knowledge_item(section, x) for x in items[:6]) if text]
        if not rendered:
            continue
        lines.append(f"· {label}（共 {len(items)} 条）：")
        lines.extend(f"    - {text}" for text in rendered)

    # 消费节点**没找到机制证据时会提前返回**（不产出 design_context），只带
    # status 与 retrieval_request。这条信息对写作至关重要——它解释了为什么
    # 写不出机制层面的内容、以及缺的是什么，不能当成"没有产物"丢掉。
    status = str(knowledge.get("consumer_status")
                 or knowledge.get("status") or "").strip()
    if status:
        lines.insert(0, f"· 消费状态 {status}")
    counts = [f"{label} {len(value)}" for key, label in
              (("patterns", "模式"), ("hyperedges", "超边"),
               ("evidence", "证据句"))
              if isinstance((value := knowledge.get(key)), list)]
    if counts:
        lines.append("· 库内可用：" + "，".join(counts))
    request = knowledge.get("retrieval_request")
    if isinstance(request, dict):
        reason = str(request.get("reason") or "").strip()
        if reason:
            lines.append("· 消费节点要求补检：" + reason[:200])

    if not lines:
        return "（本节没有知识消费产物：机制状态与算子链不可用）"
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
              materials: list[dict[str, Any]],
              knowledge: dict[str, Any] | None = None) -> str:
    lines = [
        f"## {heading}",
        "",
        f"> 骨架草稿（原因：{reason}）——**未生成正文**，"
        "下方为可按编号引用的素材。",
    ]
    if instruction:
        lines.extend(["", "结构化输入：", instruction])
    # 知识消费产物也要进骨架：它本身就来自确定性兜底（机制状态/算子链），
    # 离线时正是最该被用户看到的"能写成什么"的依据。此前被整体丢弃。
    digest = render_knowledge_digest(knowledge)
    if "没有知识消费产物" not in digest:
        lines.extend(["", "知识消费产物：", digest])
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
    knowledge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """为单个部分生成正文（或显式骨架草稿）。

    ``fields_block`` 是模板渲染出的**结构化字段块**（每项都标了来源：
    用户指定 / 规划拟定 / 模板默认），替代原来那条自由文本指令。

    ``knowledge`` 是知识消费节点的产出（``state["knowledge"]``：``consumer_analysis``
    + ``design_context``）。此前成段节点**从不接收它**，机制状态与候选算子链在
    成段这一步被整体丢弃，正文因此只能复述摘要。

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
    knowledge_digest = render_knowledge_digest(knowledge)

    if model is None:
        model_error = model_reason or "未提供模型"
        generated_by = "skeleton_fallback"
        content = _skeleton(heading, fields_block or instruction, model_error,
                            materials, knowledge)
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
                    "知识消费节点产出（机制状态 / 算子链 / 缺口，带溯源编号）：\n"
                    "{knowledge}\n\n"
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
                "knowledge": knowledge_digest,
                "basis": "；".join(str(r) for r in (sufficiency.get("reasons") or [])[:4])
                         or "无",
                "gap_hint": gap_hint,
            })
            # 用户/旧 pack 的模板可能没有 `{knowledge}` 占位符。此时**追加**而不是
            # 静默丢失——否则换了包就等于把知识消费产物再次丢回垃圾桶。
            if "{knowledge}" not in template:
                prompt += ("\n\n知识消费节点产出（机制状态 / 算子链 / 缺口，"
                           "带溯源编号，**写作时要转写进正文**）：\n"
                           + knowledge_digest)
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
                                materials, knowledge)

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
