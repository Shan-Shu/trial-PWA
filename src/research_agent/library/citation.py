"""引用格式化（合并自 paper_writing_assistant 的 library/citation.py）。

相对来源版本的改进（对应合并方案 v2 §5）：

1. 期刊身份统一走 :func:`research_agent.packs.normalize_journal_key`，
   缩写 / BibTeX 写法 / 规范名 / 分区共享同一个键，不再各自归一；
2. BibTeX 条目类型按 ``source_type`` 选择（来源版恒 ``@article``）；
3. BibTeX 补齐 ``volume`` / ``number`` / ``pages`` / ``url`` 字段（来源版只有 5 个字段）；
4. cite key 做 ASCII 化与冲突去重（来源版对中文作者会产出中文 key）。

所有样式模板来自技能包 ``packs/skills/journal-quartiles/content/citation_styles.json``，
代码内不含任何院校/期刊硬编码。
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Iterable

from research_agent import packs

__all__ = [
    "available_styles",
    "default_style",
    "format_citation",
    "render_many",
    "export",
    "venue_label",
    "citation_styles",
]

#: venue 覆盖索引缓存（按包内容构建一次；测试切包请调用 reset_cache()）
_OVERRIDE_CACHE: dict[str, Any] = {}


def reset_cache() -> None:
    """清空内部缓存（测试与运行期切换 packs 目录时使用）。"""
    _OVERRIDE_CACHE.clear()

#: 来源类型 -> 粗粒度类别（用于 doc_type / entry_type / RIS type 的兜底）
_SOURCE_ALIASES = {
    "journal-article": "journal",
    "journal_article": "journal",
    "posted-content": "preprint",
    "posted_content": "preprint",
    "proceedings-article": "proceedings",
    "book-chapter": "book-chapter",
    "book_chapter": "book-chapter",
}


def _style_data() -> dict[str, Any]:
    data = packs.skill_content("journal-quartiles", "citation_styles")
    styles = data if isinstance(data, dict) else {}
    return styles


def citation_styles() -> dict[str, Any]:
    """返回样式定义表（``{"styles": {...}, "venue_overrides": {...}}``）。"""
    data = _style_data()
    if not data:
        packs.warn_once("citation-styles-missing",
                        "未加载引用样式（packs/skills/journal-quartiles/content/"
                        "citation_styles.json 缺失）；引用导出将不可用")
    return data


def available_styles() -> list[str]:
    """可用样式 id 列表（保持包内声明顺序）。"""
    return list((citation_styles().get("styles") or {}).keys())


def default_style() -> str:
    data = citation_styles()
    styles = data.get("styles") or {}
    declared = str(data.get("default_style") or "").strip()
    if declared and declared in styles:
        return declared
    return next(iter(styles), "gb7714")


def _style(style: str) -> tuple[str, dict[str, Any]]:
    styles = citation_styles().get("styles") or {}
    key = str(style or "").strip()
    if key not in styles:
        raise ValueError(f"未知引用样式: {style!r}，可选 {sorted(styles)}")
    spec = styles[key]
    return key, spec if isinstance(spec, dict) else {}


# --------------------------------------------------------------------- 字段取值

def _as_authors(value: Any) -> list[str]:
    """把 papers.authors_meta（JSON 字符串或列表）转成作者名列表。"""
    if not value:
        return []
    data: Any = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = [part.strip() for part in re.split(r"[;；,，]", text) if part.strip()]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, (list, tuple)):
        return []
    names: list[str] = []
    for item in data:
        if isinstance(item, dict):
            name = item.get("name") or item.get("full_name") or item.get("author")
        else:
            name = item
        name = str(name or "").strip()
        if name:
            names.append(name)
    return names


def _source_kind(rec: dict[str, Any]) -> str:
    raw = str(rec.get("source_type") or "").strip().lower()
    return _SOURCE_ALIASES.get(raw, raw)


def _year(rec: dict[str, Any], missing: str) -> str:
    year = rec.get("pub_year") or rec.get("year")
    try:
        return str(int(year)) if year not in (None, "") else missing
    except (TypeError, ValueError):
        return str(year)


def _doi(rec: dict[str, Any]) -> str:
    return str(rec.get("doi") or "").strip()


def _url(rec: dict[str, Any]) -> str:
    return str(rec.get("url") or "").strip()


def _venue_raw(rec: dict[str, Any]) -> str:
    for key in ("venue", "journal", "publisher"):
        value = str(rec.get(key) or "").strip()
        if value:
            return value
    return ""


def _override_index() -> dict[str, dict[str, Any]]:
    """venue_overrides 的**归一键**索引（带缓存）。

    必须重新归一：``packs.normalize_journal_key`` 会剥掉副标题括号与噪声词，
    例如 ``Angewandte Chemie (International ed. in English)`` → ``angewandte chemie``，
    而包里的键是写作 ``angewandte chemie international edition``。
    该函数对已归一的字符串是幂等的，因此对键再归一安全。
    """
    cached = _OVERRIDE_CACHE.get("index")
    if cached is not None:
        return cached
    raw = citation_styles().get("venue_overrides") or {}
    index: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            key = packs.normalize_journal_key(name)
            if key:
                index.setdefault(key, entry)
    _OVERRIDE_CACHE["index"] = index
    return index


def venue_label(rec: dict[str, Any], *, abbreviated: bool = False) -> str:
    """期刊显示名：优先使用 pack 的 ``canonical`` / ``abbrev`` 覆盖。"""
    raw = _venue_raw(rec)
    if not raw:
        return ""
    entry = _override_index().get(packs.normalize_journal_key(raw))
    if isinstance(entry, dict):
        field = "abbrev" if abbreviated else "canonical"
        value = str(entry.get(field) or "").strip()
        if value:
            return value
    return raw


def _volume_part(rec: dict[str, Any], *, spaced: bool = True) -> str:
    volume = str(rec.get("volume") or "").strip()
    issue = str(rec.get("issue") or "").strip()
    if not volume and not issue:
        return ""
    if volume and issue:
        return f", {volume}({issue})" if spaced else f",{volume}({issue})"
    if volume:
        return f", {volume}" if spaced else f",{volume}"
    return f", ({issue})" if spaced else f",({issue})"


def _pages_part(rec: dict[str, Any]) -> str:
    pages = str(rec.get("pages") or "").strip()
    if not pages:
        return ""
    if re.match(r"^[A-Za-z]?\d+\s*[-–—]\s*[A-Za-z]?\d+$", pages):
        return f", {pages}"
    return f", {pages}"


# --------------------------------------------------------------------- 作者格式

def _initial(given: str) -> str:
    parts = re.split(r"[\s\-]+", given.strip())
    return " ".join(f"{p[0].upper()}." for p in parts if p)


def _split_name(name: str) -> tuple[str, str]:
    """把作者名拆成 (family, given)；支持 "Given Family" 与 "Family, Given"。"""
    text = (name or "").strip()
    if not text:
        return "", ""
    if "," in text:
        family, _, given = text.partition(",")
        return family.strip(), given.strip()
    parts = text.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[-1], " ".join(parts[:-1])


def _format_authors(names: list[str], style: str, missing: str) -> str:
    if not names:
        return missing
    if style == "gb7714":
        shown = names[:3]
        text = ", ".join(n.replace(" ", "") if _is_cjk(n) else n for n in shown)
        return f"{text}, 等" if len(names) > 3 else text
    if style == "apa":
        formatted = []
        for name in names[:20]:
            family, given = _split_name(name)
            formatted.append(f"{family}, {_initial(given)}" if given else family)
        if len(formatted) == 1:
            return formatted[0]
        return ", ".join(formatted[:-1]) + ", & " + formatted[-1]
    if style == "acs":
        formatted = []
        for name in names[:10]:
            family, given = _split_name(name)
            formatted.append(f"{family}, {_initial(given)}" if given else family)
        if len(formatted) == 1:
            return formatted[0]
        return "; ".join(formatted[:-1]) + ("; " if len(formatted) > 2 else " and ") \
            + formatted[-1]
    # 默认（bibtex 由 _bibtex_entry 单独处理，这里用于 ris/未知样式）
    return "; ".join(names)


def _is_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


# --------------------------------------------------------------------- 各样式渲染

def _doc_type(rec: dict[str, Any], spec: dict[str, Any]) -> str:
    mapping = spec.get("doc_type_by_source_type") or {}
    kind = _source_kind(rec)
    return str(mapping.get(kind) or spec.get("doc_type_default") or "")


def _fill(template: str, values: dict[str, str]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    # 清理模板里因缺值留下的空产物
    out = re.sub(r"\s*:\s*\.", ".", out)
    out = out.replace("..", ".").replace(" .", ".")
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def _plain_citation(rec: dict[str, Any], style: str, spec: dict[str, Any]) -> str:
    missing = str(spec.get("missing") or "")
    values = {
        "authors": _format_authors(_as_authors(rec.get("authors_meta")), style, missing),
        "title": str(rec.get("title") or "").strip() or "未命名文献",
        "venue": venue_label(rec),
        "venue_abbrev": venue_label(rec, abbreviated=True),
        "year": _year(rec, missing),
        "doi": _doi(rec) or missing,
        "url": _url(rec),
        "doc_type": _doc_type(rec, spec),
        "volume_part": _volume_part(rec),
        "pages_part": _pages_part(rec),
    }
    template = str(spec.get("journal_template") or "{authors}. {title}. {venue}, {year}.")
    return _fill(template, values)


def _bibtex_entry(rec: dict[str, Any], spec: dict[str, Any], key: str) -> str:
    kind = _source_kind(rec)
    entry_map = spec.get("entry_type_by_source_type") or {}
    entry = str(entry_map.get(kind) or spec.get("entry_type_default") or "misc")
    venue_field_map = spec.get("venue_field_by_source_type") or {}
    venue_field = str(venue_field_map.get(kind) or "journal")
    missing = str(spec.get("missing") or "n.d.")
    fields: list[tuple[str, str]] = []
    fields.append(("author", " and ".join(_as_authors(rec.get("authors_meta"))) or missing))
    fields.append(("title", str(rec.get("title") or "").strip() or "untitled"))
    venue = str(rec.get("venue") or rec.get("journal") or "").strip()
    if venue:
        venue = venue_label(rec, abbreviated=True)
        fields.append((venue_field, venue))
    fields.append(("year", _year(rec, missing)))
    # (源字段, BibTeX 字段名)：papers.issue 写进 @article 的 number 字段
    for source_field, bibtex_field in (("volume", "volume"), ("issue", "number"),
                                       ("pages", "pages")):
        value = str(rec.get(source_field) or "").strip()
        if value:
            fields.append((bibtex_field, value))
    doi = _doi(rec)
    if doi:
        fields.append(("doi", doi))
    url = _url(rec)
    if url and not doi:
        fields.append(("url", url))
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
    return f"@{entry}{{{key},\n{body}\n}}"


def _ris_entry(rec: dict[str, Any], spec: dict[str, Any]) -> str:
    kind = _source_kind(rec)
    type_map = spec.get("type_by_source_type") or {}
    ris_type = str(type_map.get(kind) or spec.get("type_default") or "GEN")
    lines = [f"TY  - {ris_type}"]
    for name in _as_authors(rec.get("authors_meta")):
        lines.append(f"AU  - {name}")
    lines.append(f"TI  - {str(rec.get('title') or '').strip() or 'untitled'}")
    venue = _venue_raw(rec)
    if venue:
        lines.append(f"JO  - {venue}")
    year = _year(rec, "")
    if year:
        lines.append(f"PY  - {year}")
    for field, tag in (("volume", "VL"), ("issue", "IS"), ("pages", "SP")):
        value = str(rec.get(field) or "").strip()
        if value:
            lines.append(f"{tag}  - {value}")
    doi = _doi(rec)
    if doi:
        lines.append(f"DO  - {doi}")
    url = _url(rec)
    if url:
        lines.append(f"UR  - {url}")
    lines.append("ER  - ")
    return "\n".join(lines)


# --------------------------------------------------------------------- cite key

_NON_ASCII = re.compile(r"[^a-z0-9]+")


def _ascii_slug(text: str) -> str:
    """ASCII 化：先做 NFKD 折音，再丢弃非 [a-z0-9]，保证 key 可被 BibTeX 消化。"""
    folded = unicodedata.normalize("NFKD", str(text or ""))
    folded = folded.encode("ascii", "ignore").decode("ascii").lower()
    slug = _NON_ASCII.sub("", folded)
    return slug


def _bibtex_keys(records: list[dict[str, Any]]) -> list[str]:
    """生成 ASCII cite key，并按首次出现顺序去重（后缀 a/b/c…）。"""
    used: dict[str, int] = {}
    keys: list[str] = []
    for rec in records:
        authors = _as_authors(rec.get("authors_meta"))
        surname = _ascii_slug(_split_name(authors[0])[0]) if authors else ""
        if not surname:
            surname = _ascii_slug(_split_name(str(rec.get("publisher") or ""))[0])
        if not surname:
            surname = "anon"
        year = _ascii_slug(_year(rec, "nd")) or "nd"
        title = _ascii_slug(rec.get("title") or "")
        stem = f"{surname}{year}{title[:12]}"
        count = used.get(stem, 0)
        used[stem] = count + 1
        keys.append(stem if count == 0 else f"{stem}{chr(ord('a') + count - 1)}")
    return keys


# --------------------------------------------------------------------- 对外接口

def format_citation(record: dict[str, Any], style: str | None = None) -> str:
    """按样式渲染单条文献。style 为 None 时使用包内默认样式。"""
    key, spec = _style(style or default_style())
    if key == "bibtex":
        return _bibtex_entry(record, spec, _bibtex_keys([record])[0])
    if key == "ris":
        return _ris_entry(record, spec)
    return _plain_citation(record, key, spec)


def render_many(records: Iterable[dict[str, Any]], style: str | None = None) -> str:
    """渲染多条文献；BibTeX 去重编号、其余样式加序号。"""
    rows = [r for r in records if isinstance(r, dict)]
    key, spec = _style(style or default_style())
    if key == "bibtex":
        keys = _bibtex_keys(rows)
        return "\n\n".join(_bibtex_entry(rec, spec, k) for rec, k in zip(rows, keys))
    if key == "ris":
        return "\n\n".join(_ris_entry(rec, spec) for rec in rows)
    lines: list[str] = []
    for index, rec in enumerate(rows, 1):
        lines.append(f"[{index}] {_plain_citation(rec, key, spec)}")
    return "\n".join(lines)


def _markdown_export(records: list[dict[str, Any]]) -> str:
    lines = ["# 文献导出", ""]
    for index, rec in enumerate(records, 1):
        lines.append(f"## {index}. {str(rec.get('title') or '未命名文献')}")
        authors = _as_authors(rec.get("authors_meta"))
        if authors:
            lines.extend(["", "作者：" + "；".join(authors)])
        for label, value in (
            ("期刊", _venue_raw(rec)),
            ("年份", _year(rec, "")),
            ("卷期页", (_volume_part(rec).lstrip(", ") + _pages_part(rec).lstrip(", ")).strip()),
            ("DOI", _doi(rec)),
            ("链接", _url(rec)),
        ):
            if value:
                lines.append(f"{label}：{value}")
        abstract = str(rec.get("abstract") or "").strip()
        if abstract:
            lines.extend(["", "摘要：", abstract])
        lines.extend(["", "引用：" + _plain_citation(rec, default_style(),
                                                     _style(default_style())[1]), ""])
    return "\n".join(lines)


def export(records: Iterable[dict[str, Any]], style: str | None = None) -> str:
    """统一导出入口：``bibtex`` / ``ris`` / ``markdown`` / 任意 plain 样式。"""
    rows = [r for r in records if isinstance(r, dict)]
    key, _spec = _style(style or default_style())
    if key == "markdown":
        return _markdown_export(rows)
    return render_many(rows, key)
