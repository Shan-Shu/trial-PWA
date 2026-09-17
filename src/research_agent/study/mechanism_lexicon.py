"""机制层词表加载：把 `packs/skills/mechanism-keywords` 与领域增补合并。

- 基础词表（技能包）：机制关键词、条件关键词、触发式规则、算子关键词等；
- 领域增补（`packs/domains/<kind>/vocab/mechanism.jsonl`）：按 ``section`` 追加条目，
  每行形如 ``{"section": "intermediate_rules", "name": "...", "keywords": [...]}``；
- 合并结果按领域缓存；领域为空时**显式告警**并返回基础词表，不做静默兜底。
"""
from __future__ import annotations

import logging
from typing import Any

from research_agent import packs

logger = logging.getLogger(__name__)
SKILL_ID = "mechanism-keywords"

_LIST_SECTIONS = (
    "mechanism_keywords", "condition_keywords", "bond_change_rules",
    "hard_constraint_hints", "low_value_relation_labels",
)
_RULE_SECTIONS = (
    "activation_rules", "intermediate_rules", "selectivity_rules", "risk_rules",
)
_DICT_SECTIONS = ("operator_keywords",)
_cache: dict[str, dict[str, Any]] = {}


def _as_rule(value: Any) -> tuple[str, tuple[str, ...]] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return str(value[0]), tuple(str(x) for x in value[1] or ())
    return None


def _domain_rows(domain_kind: str) -> list[dict[str, Any]]:
    if not domain_kind:
        return []
    base = packs.domain_dir(domain_kind)
    if base is None:
        packs.warn_once(f"mech-domain-missing:{domain_kind}",
                        "领域包 %s 不存在；机制词表只用通用技能包", domain_kind)
        return []
    path = base / "vocab" / "mechanism.jsonl"
    if not path.is_file():
        packs.warn_once(f"mech-vocab-missing:{domain_kind}",
                        "领域 %s 没有 vocab/mechanism.jsonl；机制层将只用通用词表"
                        "（非化学领域命中率会很低）", domain_kind)
        return []
    return packs.read_jsonl(path)


def mechanism_lexicon(domain_kind: str = "") -> dict[str, Any]:
    """返回某领域合并后的机制词表（带缓存）。"""
    key = domain_kind or "__default__"
    if key in _cache:
        return _cache[key]

    data = packs.skill_data(SKILL_ID)
    lex: dict[str, Any] = {
        name: list(data.get(name) or []) for name in _LIST_SECTIONS
    }
    for name in _RULE_SECTIONS:
        lex[name] = [r for r in (_as_rule(x) for x in data.get(name) or []) if r]
    for name in _DICT_SECTIONS:
        lex[name] = {str(k): [str(x) for x in (v or [])]
                     for k, v in (data.get(name) or {}).items()}

    for row in _domain_rows(domain_kind):
        section = str(row.get("section") or "").strip()
        if section in _LIST_SECTIONS:
            lex[section].extend(str(x) for x in row.get("items") or [])
        elif section in _RULE_SECTIONS:
            rule = _as_rule([row.get("name"), row.get("keywords")])
            if rule:
                lex[section].append(rule)
        elif section in _DICT_SECTIONS:
            for k, v in (row.get("entries") or {}).items():
                lex[section].setdefault(str(k), [])
                lex[section][str(k)].extend(str(x) for x in v or [])

    if not lex["mechanism_keywords"]:
        packs.warn_once("mech-empty",
                        "机制关键词为空（packs/skills/mechanism-keywords 缺失）；"
                        "机制加权与机制状态抽取将退化为空结果")
    _cache[key] = lex
    return lex


def reset_cache() -> None:
    _cache.clear()
