"""领域画像与候选领域 schema（**零硬编码**）。

所有领域内容都来自可加载的领域包（见 `research_agent.packs`）：

```text
packs/domains/<kind>/domain.json   # label / dimensions / 候选实体与关系类型 / 关键词
packs/domains/<kind>/vocab/*.jsonl # 领域词表（外部身份、机制增补等）
```

- 关键词命中数最多的领域胜出；都不命中时使用声明了 `"fallback": true` 的领域；
- 完全没有可用领域包时返回 `general` 的空画像，并通过日志告警，
  不再回落到任何内联的学科模板。
"""
from __future__ import annotations

import logging
from typing import Any

from research_agent import packs

logger = logging.getLogger(__name__)

EMPTY_PROFILE: dict[str, Any] = {
    "label": "通用科研",
    "dimensions": ["核心主题", "方法与技术", "应用场景", "机理与原理",
                   "性能与评价", "变体与拓展", "开放问题"],
    "candidate_entity_types": ["Concept", "Method", "Task", "Dataset",
                               "Metric", "Theory", "Tool", "Application"],
    "candidate_relation_types": ["uses", "evaluates", "based_on", "part_of",
                                 "improves_upon", "enables", "is_a",
                                 "related_to"],
}


def available_domains() -> list[str]:
    """当前可用的领域包列表。"""
    return packs.available_domains()


def infer_domain_kind(domain: str = "", request: str = "") -> str:
    """按领域包声明的关键词判定领域类型（含 alias_keywords）。"""
    text = f"{domain} {request}"
    kinds = packs.available_domains()
    if not kinds:
        packs.warn_once("no-domain-packs",
                        "未发现任何领域包（packs/domains 为空）；领域画像将为空")
        return "general"
    lowered = text.lower()
    best: tuple[int, str] | None = None
    fallback = "general"
    for kind in kinds:
        data = packs.domain_data(kind)
        if data.get("fallback"):
            fallback = kind
        keywords = [str(k) for k in data.get("keywords") or []]
        keywords += [str(k) for k in data.get("alias_keywords") or []]
        hits = sum(1 for k in keywords if k and k.lower() in lowered)
        if hits and (best is None or hits > best[0]):
            best = (hits, kind)
    return best[1] if best else fallback


def domain_profile_of(kind: str) -> dict[str, Any]:
    """领域画像；缺失时返回空结构（而不是内联模板）。"""
    profile = packs.domain_profile(kind)
    if not profile:
        packs.warn_once(f"profile-missing:{kind}",
                        "领域包 %s 没有 profile（领域画像为空）", kind)
    return profile


def normalize_domain_profile(data: dict[str, Any] | None,
                             domain: str = "",
                             request: str = "") -> dict[str, Any]:
    """根据规划节点输入生成稳定领域画像；缺失字段用该领域的缺省值补齐。"""
    raw = data or {}
    kind = (str(raw.get("domain_kind") or "").strip().lower()
            or infer_domain_kind(domain, request))
    profile = domain_profile_of(kind)
    if not profile:
        kind = "general" if kind != "general" else kind
        profile = domain_profile_of(kind) or EMPTY_PROFILE
    dims = [str(x).strip() for x in raw.get("dimensions") or [] if str(x).strip()]
    entity_types = [str(x).strip() for x in raw.get("candidate_entity_types") or []
                    if str(x).strip()]
    relation_types = [str(x).strip() for x in raw.get("candidate_relation_types") or []
                      if str(x).strip()]
    return {
        "domain_kind": kind,
        "label": profile.get("label") or kind,
        "dimensions": dims or list(profile.get("dimensions") or []),
        "candidate_entity_types": entity_types or list(
            profile.get("candidate_entity_types") or []),
        "candidate_relation_types": relation_types or list(
            profile.get("candidate_relation_types") or []),
        "schema_status": str(raw.get("schema_status") or "candidate"),
    }
