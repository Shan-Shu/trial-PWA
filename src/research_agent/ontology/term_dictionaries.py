"""领域词典的加载层（**零硬编码**）。

词条来自各领域包的词表文件：``packs/domains/<kind>/vocab/terms.jsonl``，
每行一条：

```json
{"term": "water", "id": "CHEBI:15377", "canonical": "water",
 "aliases": ["H2O"], "source": "ChEBI"}
```

本模块只负责"名字 → 外部身份"的查找，不猜测归并：找不到就返回 None。
新增/修正词条请改数据文件（或通过 `RA_PACKS_DIR` 挂载自己的领域包），不要改代码。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterator

from research_agent import packs

logger = logging.getLogger(__name__)
_warned_empty = False


def _norm(value: str) -> str:
    value = re.sub(r"[\s_\-/\\.,;:'\"()\[\]{}]+", " ", str(value or "").strip())
    return re.sub(r"\s+", " ", value).strip().lower()


def _norm_source(value: str) -> str:
    return re.sub(r"\s+", "_", str(value or "").strip().lower())


def _lookups() -> dict[str, dict[str, dict[str, Any]]]:
    """返回 {领域: {规范名: 词条}}；按领域分开，避免学科间误合并。"""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for kind in packs.available_domains():
        lex = packs.domain_lexicon(kind)
        if lex:
            out[kind] = lex
    if not out:
        global _warned_empty
        if not _warned_empty:
            _warned_empty = True
            logger.warning("领域词表为空（packs/domains/*/vocab/terms.jsonl 未加载）；"
                           "词典归并层将不做任何外部身份匹配")
    return out


def lookup_identity(name: str,
                    *,
                    node_type: str = "",
                    aliases: list[str] | None = None,
                    source: str = "",
                    domain_kind: str | None = None) -> dict[str, Any] | None:
    """返回领域词典身份；无匹配时返回 None，不进行猜测归并。"""
    candidates = [_norm(name or "")] + [_norm(a) for a in aliases or []]
    candidates = [c for c in candidates if c]
    if not candidates:
        return None
    lookups = _lookups()
    kinds = [domain_kind] if domain_kind else sorted(lookups)
    requested = str(source or "").lower()
    for kind in kinds:
        for canonical, entry in (lookups.get(kind) or {}).items():
            names = {_norm(canonical)}
            names.update(_norm(a) for a in entry.get("aliases") or ())
            names.discard("")
            if not (names & set(candidates)):
                continue
            entry_source = _norm_source(entry.get("source"))
            if requested and entry_source and requested != entry_source:
                continue
            return {
                "external_source": entry_source or kind,
                "external_id": entry.get("id"),
                "canonical_name": entry.get("canonical") or canonical,
                "domain_kind": kind,
            }
    return None


def iter_dictionary_terms() -> Iterator[tuple[str, Any, str, tuple]]:
    """供诊断/导出使用：返回 (source, entry_id, canonical, aliases)。"""
    for kind in packs.available_domains():
        for canonical, entry in (packs.domain_lexicon(kind) or {}).items():
            yield (str(entry.get("source") or kind), entry.get("id"), canonical,
                   tuple(entry.get("aliases") or ()))
