"""PDF 文本初步处理：去除页眉/页脚/页码等与正文无关内容，生成精校重排文本。

策略（行级识别，基于 PyMuPDF 的 dict 提取，兼容整页合并成一个 block 的 PDF）：
1. 逐页提取“文本行”（含坐标）；
2. 剔除纯页码行、页眉/页脚区内的元信息行（©、DOI 等）；
3. 在顶/底边距区且跨页重复出现的行视为运行页眉/页脚，剔除；
4. 正文行按坐标排序，按行距聚合成段落（精校重排），修复断词连字符。

输出既包含全文 clean_text，也包含按段落切分的 paragraphs，供知识提取直接使用。
"""
from __future__ import annotations

import re
from typing import Any


def _is_page_number(text: str) -> bool:
    t = text.strip().strip("\u00a0")
    return bool(re.fullmatch(r"(\d{1,4}|[-–—]\s*\d{1,4}\s*[-–—]?|\d+\s*/\s*\d+)", t))


def _is_metadata_line(text: str) -> bool:
    t = text.strip()
    return bool(
        re.fullmatch(r"(?:arXiv|doi|DOI|https?://\S+|www\.\S+)\S*", t)
        or re.search(r"\bdoi\s*[:.]?\s*10\.\d{4,9}/\S+", t, re.IGNORECASE)
        or re.fullmatch(r"©\s?\d{4}.{0,80}", t)
        or re.fullmatch(r"Copyright\s.+", t, re.IGNORECASE)
    )


def _extract_lines(pages) -> list[list[dict[str, Any]]]:
    """每页返回按 y 排序的行列表。"""
    all_lines: list[list[dict[str, Any]]] = []
    for page in pages:
        height = page.rect.height
        lines: list[dict[str, Any]] = []
        data = page.get_text("dict")
        for block in data.get("blocks") or []:
            for line in block.get("lines") or []:
                text = "".join(
                    (span.get("text") or "") for span in (line.get("spans") or [])
                ).strip()
                if not text:
                    continue
                x0, y0, x1, y1 = line.get("bbox") or (0, 0, 0, 0)
                lines.append({
                    "text": text,
                    "x0": float(x0), "y0": float(y0),
                    "x1": float(x1), "y1": float(y1),
                    "page_height": float(height),
                })
        lines.sort(key=lambda ln: (ln["y0"], ln["x0"]))
        all_lines.append(lines)
    return all_lines


def _zone(line: dict, header_frac: float, footer_frac: float) -> str | None:
    h = line["page_height"]
    if h <= 0:
        return None
    if line["y1"] < h * header_frac:
        return "header"
    if line["y0"] > h * footer_frac:
        return "footer"
    return None


def _clean_lines(all_lines: list[list[dict]]) -> tuple[list[list[dict]], dict]:
    """行分类：剔除 页码/边距区元信息/跨页重复页眉页脚；返回 (保留行, 移除统计)。"""
    header_frac, footer_frac = 0.08, 0.92
    kept: list[list[dict]] = []
    removed: dict[str, list[str]] = {"page-number": [], "metadata": [],
                                     "running-header": [], "running-footer": []}
    for page_lines in all_lines:
        page_kept: list[dict] = []
        for ln in page_lines:
            text = ln["text"]
            if _is_page_number(text):
                removed["page-number"].append(text)
                continue
            zone = _zone(ln, header_frac, footer_frac)
            if zone is not None and _is_metadata_line(text):
                removed["metadata"].append(text)
                continue
            ln["_zone"] = zone
            page_kept.append(ln)
        kept.append(page_kept)

    # 跨页重复的边距区行 → 运行页眉/页脚
    count: dict[tuple[str | None, str], int] = {}
    for page_lines in kept:
        for ln in page_lines:
            if ln.get("_zone"):
                key = (ln["_zone"], _norm_repeat(ln["text"]))
                count[key] = count.get(key, 0) + 1
    final: list[list[dict]] = []
    for page_lines in kept:
        page_out = []
        for ln in page_lines:
            if ln.get("_zone"):
                key = (ln["_zone"], _norm_repeat(ln["text"]))
                if count[key] >= 2:
                    removed[f"running-{ln['_zone']}"].append(ln["text"])
                    continue
            page_out.append(ln)
        final.append(page_out)
    return final, removed


def _norm_repeat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()[:120]


def _group_paragraphs(all_lines: list[list[dict]]) -> list[str]:
    """按行距把保留行聚合成段落；修复断词连字符；返回段落列表。"""
    paragraphs: list[str] = []
    for page_lines in all_lines:
        if not page_lines:
            continue
        current: list[dict] = [page_lines[0]]
        for ln in page_lines[1:]:
            prev = current[-1]
            gap = ln["y0"] - prev["y1"]
            line_h = max(1.0, prev["y1"] - prev["y0"])
            if gap > 0.7 * line_h:      # 换段（含页间换行）
                paragraphs.append(_join_lines(current))
                current = [ln]
            else:
                current.append(ln)
        paragraphs.append(_join_lines(current))
    return [p for p in paragraphs if p.strip()]


def _join_lines(lines: list[dict]) -> str:
    """行拼接 + 断词连字符修复。"""
    out = ""
    for ln in lines:
        text = ln["text"].strip()
        if not text:
            continue
        if out.endswith("-") and not out.endswith("--"):
            out = out[:-1] + text
        else:
            out = out + (" " if out else "") + text
    out = re.sub(r"\s+", " ", out)
    return out.strip()


def clean_pdf(pdf_bytes: bytes, cut_references: bool = True) -> dict[str, Any]:
    """处理 PDF 字节流，返回精校文本及统计信息。"""
    import pymupdf

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    all_lines = _extract_lines(list(doc))
    kept, removed = _clean_lines(all_lines)
    paragraphs = _group_paragraphs(kept)
    full_text = "\n\n".join(paragraphs)
    if cut_references:
        full_text = _cut_references(full_text)
        paragraphs = [p for p in full_text.split("\n\n") if p.strip()]
    doc.close()
    return {
        "text": full_text,
        "paragraphs": paragraphs,
        "page_count": len(all_lines),
        "removed_blocks": removed,
        "removed_total": sum(len(v) for v in removed.values()),
    }


def _cut_references(text: str) -> str:
    """从参考文献章节标题处截断（启发式，失败则原样返回）。"""
    patterns = [
        r"^\s*References\s*$",
        r"^\s*Bibliography\s*$",
        r"^\s*REFERENCES\s*$",
        r"^\s*参考\s*文\s*献\s*$",
        r"^\s*References\s*[\[\(].*",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.MULTILINE)
        if m:
            return text[: m.start()].rstrip()
    return text
