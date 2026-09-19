"""把研究链路产出的内部草稿转换为 ACS 格式的"纯净"文稿。

背景：知识消费/内容形成节点输出的 markdown 里，引用是库内编号
（``P-0001`` / ``E-0001-1`` / ``H-0001``），并带有 ``[supported]``、
``[hypothesis]`` 这类状态标记。面向读者的 ACS 论文/报告不应出现这些内部痕迹。

本模块负责：
1. 建立"库内编号 → 论文"映射，按出现顺序编号，生成 ACS 顺序数字引用；
2. 正文中的 ``[P-xxxx, E-xxxx]`` 替换为 ACS 上标数字 ``^1,2^``（markdown 用 ``<sup>``）；
3. 附 ``References`` 段，按 ACS 期刊格式排版（作者. 标题. *期刊* 年. DOI）；
4. 清理状态标记、超边编号、系统术语，并做自检（残留编号、悬空引用、重复 DOI）；
5. 输出 ``citation_map`` 便于审计"哪个观点引了哪篇论文"。
"""
from __future__ import annotations

import html
import json
import re
from typing import Any

_ID_ATOM = r"[PEH]-\d{1,6}(?:-\d{1,4})?"
# 正文里可能出现的库内编号引用；模型会用多种括号：[]、（）、()、【】
_REF_BLOCK_RE = re.compile(
    r"[\[（(【]([^\[\]（）()【】]*?" + _ID_ATOM + r"[^\[\]（）()【】]*?)[\]）)】]")
_ANY_ID_RE = re.compile(r"\b" + _ID_ATOM + r"\b")
_LEFTOVER_ID_RE = re.compile(r"[（(【]\s*" + _ID_ATOM + r"\s*[）)】]")
_MS_OP_GAP_RE = re.compile(r"\b(?:MS|OP|GAP|OC|FC)-\d{1,4}\b")
_STATUS_MARK_RE = re.compile(r"\[(?:supported|hypothesis|open_question)\]", re.I)
_SECTION_STATUS_RE = re.compile(
    r"^\s*-\s*\[(?:supported|hypothesis|open_question)\]\s*", re.I | re.M)
_HTML_TAG_RE = re.compile(r"<[^>]{1,40}>")
# 系统术语：不应出现在面向读者的文本里
_SYSTEM_TERMS = [
    "pattern_id", "evidence_id", "hyperedge_id", "design_context",
    "consumer_analysis", "candidate_pool", "operator_chain",
    "innovation_level", "mechanism_states", "opportunity_gaps",
    "knowledge 包", "知识包", "本体库", "模式卡", "证据卡", "科研超边",
    "库内", "支持度", "单支持模式", "设计上下文", "证据覆盖",
    "L3（机制级重设计）", "创新等级下限", "design_contract",
    "offline_fallback", "llm+deterministic_fill",
]
_PLACEHOLDER_TERMS = ["（无内容）", "待补充", "TODO", "TBD", "xxx"]
# 系统痕迹 → 面向读者的等价表述（确定性改写，不新增事实）
_SYSTEM_REWRITES: list[tuple[str, str]] = [
    ("库内高支持模式", "现有报道"),
    ("库内高置信模式", "现有报道"),
    ("库内单支持模式", "个例报道"),
    ("库内机制起点之一", "代表性机制之一"),
    ("库内机制起点", "代表性机制"),
    ("库内", "现有文献"),
    ("单支持模式", "个例报道"),
    ("高支持模式", "多篇文献报道"),
    ("支持度 2", "两篇文献支持"),
    ("支持度2", "两篇文献支持"),
    ("支持度 1", "单篇文献支持"),
    ("支持度1", "单篇文献支持"),
    ("支持度", "文献支持"),
    ("证据覆盖", "证据分布"),
    ("设计上下文", "设计依据"),
    ("operator_chain第", "机制算子序列第"),
    ("operator_chain", "机制算子序列"),
    ("evidence_ids中", "所引文献中"),
    ("evidence_ids", "所引文献"),
    ("pattern_ids", "模式编号"),
    ("当前知识包中的", "现有文献中的"),
    ("知识包内", "现有文献中"),
    ("知识包", "现有文献"),
]


def polish_system_terms(text: str) -> str:
    """把系统口径的描述改写成读者可读的等价表述。"""
    for source, target in _SYSTEM_REWRITES:
        text = text.replace(source, target)
    text = re.sub(r"（\s*）|\(\s*\)", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"，\s*，", "，", text)
    return text


def clean_text(value: Any) -> str:
    """清洗来源元数据里的 HTML 实体与标签（OpenAlex/EuropePMC 常见）。"""
    text = html.unescape(str(value if value is not None else ""))
    text = _HTML_TAG_RE.sub("", text)
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s{2,}", " ", text).strip()


# ------------------------------------------------------------------ 引用映射

def build_citation_index(knowledge: dict[str, Any]) -> dict[str, str]:
    """建立 库内编号 → paper_key 的映射（模式卡、证据卡、超边与超边证据句）。

    超边有两种编号：``H-0007``（超边级，取该超边第一篇论文）与
    ``H-0007-1``、``H-0007-2``…（证据句级，**各自归属自己的论文**）。

    旧实现用 ``setdefault`` 先写 ``H-0007`` 再循环写证据句，导致 base 被占用后
    setdefault 不再更新，同一超边的所有证据句都被算到第一篇论文上（P1-5）。
    这里改为两趟显式赋值：先证据句（精确归属），再超边级（兜底）。
    """
    index: dict[str, str] = {}
    for pattern in knowledge.get("patterns") or []:
        pid = str(pattern.get("pattern_id") or "")
        keys = [str(k) for k in pattern.get("paper_keys") or [] if k]
        if pid and keys:
            index[pid] = keys[0]
            for eid in pattern.get("evidence_ids") or []:
                index.setdefault(str(eid), keys[0])
    for ev in knowledge.get("evidence") or []:
        eid = str(ev.get("evidence_id") or "")
        key = str(ev.get("paper_key") or "")
        if eid and key:
            index.setdefault(eid, key)
    # 第一趟：证据句级编号 → 各自的论文（精确）
    for hyper in knowledge.get("hyperedges") or []:
        hid = hyper.get("hyperedge_id")
        if hid is None:
            continue
        base = f"H-{int(hid):04d}"
        for i, ev in enumerate(hyper.get("evidence") or []):
            key = str(ev.get("paper_key") or "")
            if key:
                index[f"{base}-{i + 1}"] = key
                index.setdefault(base, key)
    # 第二趟：超边自带 evidence_ids（消费节点生成）也按顺序归属
    for hyper in knowledge.get("hyperedges") or []:
        hid = hyper.get("hyperedge_id")
        if hid is None:
            continue
        base = f"H-{int(hid):04d}"
        evidence = hyper.get("evidence") or []
        for i, eid in enumerate(hyper.get("evidence_ids") or []):
            eid = str(eid)
            if not eid or eid in index:
                continue
            if i < len(evidence):
                key = str(evidence[i].get("paper_key") or "")
                if key:
                    index[eid] = key
            elif base in index:
                index[eid] = index[base]
    return index


# ------------------------------------------------------------------ ACS 排版

def _acs_author(author: dict[str, Any]) -> str:
    given = str(author.get("given") or "").strip()
    family = str(author.get("family") or "").strip()
    name = str(author.get("name") or "").strip()
    if not family and name:
        if "," in name:
            family, given = [x.strip() for x in name.split(",", 1)]
        else:
            parts = name.split()
            if len(parts) >= 2 and re.fullmatch(r"[A-Za-z]\.?",
                                                parts[-1].strip()):
                # "van Geel R" → 姓 "van Geel"，名 "R"
                given = parts[-1].strip()
                family = " ".join(parts[:-1])
            elif len(parts) >= 2 and re.fullmatch(
                    r"(?:[A-Z]\.?\s*)+", parts[-1].strip()):
                # "Löwik DW" / "Smith J. R." → 姓在前，末尾是首字母串
                given = parts[-1].strip()
                family = " ".join(parts[:-1])
            else:
                family = parts[-1] if parts else name
                given = " ".join(parts[:-1])
    # 把 "A.D.G." / "Ada D. G." / "DW" 统一成 "A. D. G." / "D. W."
    pieces: list[str] = []
    for token in given.replace(".", " ").split():
        if len(token) > 1 and token.isupper():
            pieces.extend(token)
        else:
            pieces.append(token)
    initials = " ".join(f"{p[0].upper()}." for p in pieces if p and p[0].isalpha())
    if not family:
        return ""
    return f"{family}, {initials}".strip().rstrip(",")


def acs_reference_line(paper: dict[str, Any], number: int) -> str:
    """ACS 期刊格式：作者. 标题. *期刊* 年, 卷, 页. DOI"""
    authors = [_acs_author(a) for a in paper.get("authors") or []
               if isinstance(a, dict)]
    authors = [a for a in authors if a]
    if len(authors) > 10:
        author_text = "; ".join(authors[:10]) + "; et al."
    else:
        author_text = "; ".join(authors)
    title = clean_text(paper.get("title") or "Untitled").rstrip(".")
    venue = clean_text(paper.get("venue"))
    year = clean_text(paper.get("pub_year")) or "n.d."
    volume = clean_text(paper.get("volume"))
    pages = clean_text(paper.get("pages"))
    doi = re.sub(r"^https?://doi\.org/", "", clean_text(paper.get("doi")),
                 flags=re.I)
    line = f"({number}) "
    if author_text:
        line += f"{author_text.rstrip('.')}. "
    line += f"{title}."
    if venue:
        line += f" *{venue}*"
    line += f" {year}"
    if volume:
        line += f", {volume}"
    if pages:
        line += f", {pages}"
    line += "."
    if doi:
        line += f" https://doi.org/{doi}"
    return line


# ------------------------------------------------------------------ 正文清理

def clean_body(text: str) -> str:
    """去掉状态标记、遗留编号与系统术语，保留正文语义。"""
    text = _SECTION_STATUS_RE.sub("- ", text or "")
    text = _STATUS_MARK_RE.sub("", text)
    text = _LEFTOVER_ID_RE.sub("", text)
    text = _MS_OP_GAP_RE.sub("", text)
    text = text.replace("【", "").replace("】", "")
    text = polish_system_terms(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _numbers_for(ids: list[str], index: dict[str, str], order: list[str],
                 dangling: list[str]) -> list[int]:
    numbers: list[int] = []
    for rid in ids:
        key = index.get(rid)
        if not key:
            dangling.append(rid)
            continue
        if key not in order:
            order.append(key)
        num = order.index(key) + 1
        if num not in numbers:
            numbers.append(num)
    return numbers


def _replace_reference_blocks(text: str, index: dict[str, str],
                              order: list[str]) -> tuple[str, list[str]]:
    """把 ``[P-0001, E-0001-1]`` 替换为 ACS 上标数字；返回正文与悬空引用。"""
    dangling: list[str] = []

    def repl(match: re.Match[str]) -> str:
        numbers = _numbers_for(_ANY_ID_RE.findall(match.group(1)), index, order,
                               dangling)
        if not numbers:
            return ""
        return "<sup>" + ",".join(str(n) for n in numbers) + "</sup>"

    return _REF_BLOCK_RE.sub(repl, text), dangling


# ------------------------------------------------------------------ 主转换

def to_acs_document(draft: dict[str, Any],
                    knowledge: dict[str, Any],
                    papers: dict[str, dict[str, Any]],
                    *,
                    title: str,
                    subtitle: str = "",
                    header_notes: list[str] | None = None,
                    keep_candidate_section: bool = True,
                    existing_citation_map: dict[str, Any] | None = None
                    ) -> dict[str, Any]:
    """把内容节点的结构化草稿转换为 ACS 纯净文稿。

    existing_citation_map 用于**保持既有编号**：传入上一次转换的
    ``citation_map``（{"1": {"paper_key": ...}}）时，同一篇论文沿用原编号，
    只对新出现的论文追加编号。

    返回 ``{"markdown", "references", "citation_map", "checks", "stats"}``。
    """
    index = build_citation_index(knowledge)
    order: list[str] = []
    dangling: list[str] = []
    # 保持既有编号：同一篇论文沿用上次转换的编号，新论文追加到末尾
    if existing_citation_map:
        for key in sorted(existing_citation_map, key=lambda x: int(x)
                          if str(x).isdigit() else 10**6):
            entry = existing_citation_map[key]
            paper_key = str(entry.get("paper_key") or "") if isinstance(
                entry, dict) else str(entry or "")
            if paper_key and paper_key not in order:
                order.append(paper_key)

    sections_out: list[str] = []
    for section in draft.get("sections") or []:
        heading = str(section.get("heading") or "").strip()
        if heading:
            sections_out.append(f"## {heading}")
        for item in section.get("items") or []:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            # 先处理结构化引用字段（离线草稿把编号放在数组里），再处理行内编号
            structured = [str(x) for x in (item.get("pattern_ids") or [])]
            structured += [str(x) for x in (item.get("evidence_ids") or [])]
            structured += [str(x) for x in (item.get("mechanism_evidence") or [])]
            structured_numbers = _numbers_for(
                [x for x in structured if _ANY_ID_RE.fullmatch(x)],
                index, order, dangling)
            rewritten, bad = _replace_reference_blocks(text, index, order)
            dangling.extend(bad)
            extra = re.findall(r"<sup>([\d,]+)</sup>", rewritten)
            for group in extra:
                for token in group.split(","):
                    if token.isdigit() and int(token) not in structured_numbers:
                        structured_numbers.append(int(token))
            structured_numbers = sorted(set(structured_numbers))
            if structured_numbers:
                rewritten += ("<sup>"
                              + ",".join(str(n) for n in structured_numbers)
                              + "</sup>")
            status = str(item.get("status") or "supported").lower()
            if status == "hypothesis":
                rewritten += "（该机制目前为推断，尚待实验验证）"
            elif status == "open_question":
                rewritten += "（该问题在现有语料中缺少直接证据）"
            sections_out.append(f"- {rewritten}")

    if keep_candidate_section and draft.get("strategies"):
        ranked = [s for s in draft["strategies"] if s.get("rank")]
        if ranked:
            sections_out.append("## 候选新方法")
            for cand in ranked:
                title_text = str(cand.get("title") or "").strip()
                level = str(cand.get("innovation_level") or "").strip()
                sections_out.append(f"### {cand.get('rank')}. {title_text}")
                chain = cand.get("operator_chain") or []
                if chain:
                    steps = " → ".join(
                        f"{s.get('operator')}（{s.get('input') or '?'} → "
                        f"{s.get('output') or '?'}）" for s in chain)
                    sections_out.append(f"- 机制算子序列：{steps}")
                if cand.get("differentiation"):
                    sections_out.append(f"- 与其余候选的区别：{cand['differentiation']}")
                if cand.get("novelty_source"):
                    sections_out.append(f"- 创新来源：{cand['novelty_source']}")
                if cand.get("rationale"):
                    sections_out.append(f"- 可行性理由：{cand['rationale']}")
                risks = cand.get("risks") or []
                if risks:
                    sections_out.append("- 风险：" + "；".join(str(r) for r in risks))
                if cand.get("validation_plan"):
                    sections_out.append(f"- 最小验证实验：{cand['validation_plan']}")
                constraints = cand.get("satisfies_constraints") or []
                if constraints:
                    rows = ["| 目标硬约束 | 是否满足 | 理由 |", "|---|---|---|"]
                    for c in constraints:
                        rows.append(
                            f"| {str(c.get('constraint') or '').strip()} | "
                            f"{'满足' if c.get('satisfied') else '未满足'} | "
                            f"{str(c.get('reason') or '').strip()} |")
                    sections_out.extend(rows)
                if level:
                    sections_out.append(f"- 创新等级：{level}")

    body = clean_body("\n\n".join(sections_out))

    references = []
    for i, key in enumerate(order, start=1):
        paper = papers.get(key)
        if not paper:
            references.append(f"({i}) [文献元数据缺失：{key}]")
            continue
        references.append(acs_reference_line(paper, i))

    citation_map = {}
    for i, key in enumerate(order, start=1):
        paper = papers.get(key) or {}
        citation_map[str(i)] = {
            "paper_key": key,
            "title": paper.get("title"),
            "doi": paper.get("doi"),
            "venue": paper.get("venue"),
            "pub_year": paper.get("pub_year"),
        }

    header = [f"# {title}"]
    if subtitle:
        header += ["", f"> {subtitle}"]
    if header_notes:
        header += ["", *[f"> {note}" for note in header_notes]]
    document = "\n".join(header + ["", "---", "", body])
    if references:
        document += "\n\n## References\n\n" + "\n\n".join(references)

    residual_ids = sorted(set(_ANY_ID_RE.findall(document)))
    system_hits = [t for t in _SYSTEM_TERMS if t in document]
    placeholder_hits = [t for t in _PLACEHOLDER_TERMS if t in document]
    dois = [str((papers.get(k) or {}).get("doi") or "") for k in order]
    dois = [d for d in dois if d]
    dup_dois = sorted({d for d in dois if dois.count(d) > 1})
    cjk = sum(1 for ch in document if "\u4e00" <= ch <= "\u9fff")

    checks = {
        "residual_internal_ids": residual_ids,
        "dangling_citations": sorted(set(dangling)),
        "system_terms": system_hits,
        "placeholders": placeholder_hits,
        "duplicate_dois": dup_dois,
        "references": len(references),
        "citation_marks_in_body": len(re.findall(r"<sup>", document)),
        "cjk_chars": cjk,
        "clean": not (residual_ids or system_hits or placeholder_hits
                      or dangling or dup_dois),
    }
    stats = {
        "title": title,
        "sections": len(draft.get("sections") or []),
        "body_chars": len(body),
        "cjk_chars": cjk,
        "references": len(references),
        "candidates": len([s for s in draft.get("strategies") or []
                           if s.get("rank")]),
    }
    return {
        "markdown": document + "\n",
        "references": references,
        "citation_map": citation_map,
        "checks": checks,
        "stats": stats,
    }


def dump_json(path: Any, payload: Any) -> None:
    from pathlib import Path

    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")
