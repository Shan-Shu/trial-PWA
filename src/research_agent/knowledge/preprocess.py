"""知识提取前的文本预处理：分句、分段、切块。

v0.4.3 修复（issue P2-1）：
- ``split_sentences`` 支持中文"标点后无空白"的切分（旧实现要求 ``\\s+``，
  中文正文"第一句。第二句。"整段切不开）；
- ``split_paragraphs`` 的 docstring 与实现对齐（只丢空白段，不做长度过滤）；
- ``chunk_paragraphs`` 对超长段落做二次切分，避免单段突破 ``max_chars``；
- 新增 ``chunk_stats`` 报告被截断的块数与字符数，不再静默丢弃。
"""
from __future__ import annotations

import re
from typing import Any


ABBREVIATIONS = (
    "e.g", "i.e", "etc", "vs", "al", "Fig", "Figs", "Eq", "et", "cf",
    "Dr", "Mr", "Ms", "Prof", "Inc", "Ltd", "Co", "No", "vol", "pp",
    "eds", "Eds", "St", "Mt", "U.S", "U.K", "a.m", "p.m",
)
_ABBR_RE = re.compile(r"(?<!\w)(" + "|".join(ABBREVIATIONS) + r")\.\s*$")
# 中文标点结尾时拼接下一句无需补空格（中文本无词间空白）
_CJK_TAIL = ("。", "！", "？", "；", "，", "、", "：", "）", "》", "」")


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def split_paragraphs(text: str) -> list[str]:
    """按空行切分为段落。

    注意：**不做长度过滤**（旧 docstring 称"过滤过短片段"，与实现不符）；
    过短片段的判定发生在抽取层（见 ``extractor.is_garbage_entity``），
    且有语种差异，放在这里会误伤中文单字术语。
    """
    text = normalize_whitespace(text)
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return paras


def split_sentences(text: str) -> list[str]:
    """按中英文句末标点切句；跳过常见缩写词后的句点。

    中文标点（。！？）后无空白也切分；英文 ``.`` 仍需空白以免切断
    小数、缩写与域名。
    """
    text = normalize_whitespace(text)
    parts: list[str] = []
    for block in re.split(r"(?<=[。！？])\s*", text):
        block = block.strip()
        if not block:
            continue
        parts.extend(p for p in re.split(r"(?<=[.!?])\s+", block) if p.strip())
    sentences: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 缩写如 "et al." / "e.g." 后不该断句：合并回上一句
        if sentences and _ABBR_RE.search(sentences[-1]):
            sentences[-1] = sentences[-1] + " " + p
        else:
            sentences.append(p)
    return sentences


def chunk_paragraphs(paragraphs: list[str], max_chars: int = 8000,
                     max_chunks: int | None = None,
                     stats: dict[str, Any] | None = None) -> list[list[str]]:
    """把段落序列切成若干文本窗口（每窗尽量不超过 ``max_chars``）。

    - 超长段落（自身 > ``max_chars``）按句子边界二次切分；
    - ``max_chunks`` 截断时把丢弃的块数与字符数写入 ``stats``（不再静默）。
    """
    max_chars = max(200, int(max_chars))

    def split_long(para: str) -> list[str]:
        if len(para) <= max_chars:
            return [para]
        pieces: list[str] = []
        buffer = ""

        def join_head(head: str, sentence: str) -> str:
            """切句时消耗了分隔空白，拼回时对拉丁文本补一个空格。"""
            if not head or head.endswith((" ", "\n")):
                return head + sentence
            if head.endswith(_CJK_TAIL) or sentence[:1] in "，。！？；：、）":
                return head + sentence
            return head + " " + sentence

        for sentence in split_sentences(para) or [para]:
            if len(sentence) > max_chars:
                # 单句仍然超长（无标点长文）：硬切
                for i in range(0, len(sentence), max_chars):
                    pieces.append(sentence[i:i + max_chars])
                buffer = ""
                continue
            if buffer and len(buffer) + len(sentence) > max_chars:
                pieces.append(buffer)
                buffer = ""
            buffer = join_head(buffer, sentence)
        if buffer:
            pieces.append(buffer)
        return pieces

    expanded: list[str] = []
    for para in paragraphs:
        expanded.extend(split_long(para))

    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for para in expanded:
        plen = len(para)
        if cur and cur_len + plen > max_chars:
            chunks.append(cur)
            cur = []
            cur_len = 0
        cur.append(para)
        cur_len += plen
    if cur:
        chunks.append(cur)

    if max_chunks and len(chunks) > max_chunks:
        dropped = chunks[max_chunks:]
        if stats is not None:
            stats["dropped_chunks"] = len(dropped)
            stats["dropped_paras"] = sum(len(c) for c in dropped)
            stats["dropped_chars"] = sum(len(p) for c in dropped
                                         for p in c)
            stats["total_chunks_before_limit"] = len(chunks)
        chunks = chunks[:max_chunks]
    elif stats is not None:
        stats.setdefault("dropped_chunks", 0)
        stats.setdefault("dropped_paras", 0)
        stats.setdefault("dropped_chars", 0)
        stats["total_chunks_before_limit"] = len(chunks)
    return chunks
