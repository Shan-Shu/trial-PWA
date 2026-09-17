"""领域包与技能包加载器（零硬编码的领域层）。

设计目标：**代码里不再出现任何学科词表**。所有领域相关内容放在可加载的包里：

```text
packs/
├── skills/<skill-id>/SKILL.md + content/*.json(l)
└── domains/<domain-kind>/domain.json + vocab/*.jsonl
```

查找顺序（后者仅在前者缺失时兜底）：

1. ``RA_PACKS_DIR`` 环境变量（可为 ``;`` 分隔的多个目录，便于挂载用户包）；
2. ``<项目根>/packs``。

内置默认包使用 ``RA_PACKS_FALLBACK``（默认指向随代码发布的 ``packs/``）。

对外 API（调用方全部通过它拿领域内容）：

- :func:`available_skills` / :func:`skill_dir` / :func:`skill_data` / :func:`skill_terms`
- :func:`available_domains` / :func:`domain_profile` / :func:`infer_domain_kind`
- :func:`domain_terms` / :func:`relation_lexicon` / :func:`strong_relations`

缺失时的行为：**显式告警并返回空值**，绝不用隐藏的内置表兜底——
避免出现"看起来在跑、其实机制层是空的"这种静默失效。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILTIN_PACKS = PROJECT_ROOT / "packs"

_warned: dict[str, None] = {}
_cache: dict[tuple[str, str], Any] = {}


def warn_once(key: str, message: str, *args: Any) -> None:
    """同一问题只告警一次，避免日志刷屏。"""
    if key in _warned:
        return
    _warned[key] = None
    logger.warning(message, *args)


def reset_cache() -> None:
    """清空缓存（测试与运行期切换 packs 目录时使用）。"""
    _cache.clear()
    _warned.clear()


def pack_roots() -> list[Path]:
    """返回按优先级排序的包根目录列表。

    - 未设置 ``RA_PACKS_DIR``：使用随代码发布的 ``<项目根>/packs``；
    - 设置了 ``RA_PACKS_DIR``：**只**使用这些目录（显式配置即权威），
      除非再用 ``RA_PACKS_FALLBACK`` 指定兜底目录。
    """
    roots: list[Path] = []
    env = os.getenv("RA_PACKS_DIR")
    fallback_env = os.getenv("RA_PACKS_FALLBACK")
    if env:
        roots.extend(Path(p) for p in env.split(os.pathsep) if p.strip())
    if fallback_env:
        roots.extend(Path(p) for p in fallback_env.split(os.pathsep)
                     if p.strip())
    elif not env:
        roots.append(PROJECT_ROOT / "packs")
    if BUILTIN_PACKS != (PROJECT_ROOT / "packs") and not roots:
        roots.append(BUILTIN_PACKS)
    seen: set[str] = set()
    out: list[Path] = []
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            out.append(root)
    return out


def _resolve(kind: str, name: str) -> Path | None:
    for root in pack_roots():
        candidate = root / kind / name
        if candidate.is_dir():
            return candidate
    return None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warn_once(f"json:{path}", "包内容读取失败 %s: %s", path, exc)
        return None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """公开的 JSONL 读取（跳过空行与注释行）。"""
    return _read_jsonl(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    except OSError as exc:
        warn_once(f"jsonl:{path}", "包内容读取失败 %s: %s", path, exc)
    return rows


# ------------------------------------------------------------------ 技能包

def available_skills() -> list[str]:
    names: set[str] = set()
    for root in pack_roots():
        base = root / "skills"
        if base.is_dir():
            names.update(p.name for p in base.iterdir() if p.is_dir())
    return sorted(names)


def skill_dir(skill_id: str) -> Path | None:
    return _resolve("skills", skill_id)


def skill_data(skill_id: str) -> dict[str, Any]:
    """读取技能包主数据文件 ``content/data.json``。

    注意：本函数**只**返回 ``data.json``。包内其它数据文件（如
    ``journal-quartiles/content/citation_styles.json``）请用
    :func:`skill_content` 按文件名读取——旧实现仅在"没有 data.json"时
    才合并 ``content/*.json``，导致有 data.json 的包无法访问同目录其它文件。
    """
    key = ("skill", skill_id)
    if key in _cache:
        return _cache[key]
    base = skill_dir(skill_id)
    data: dict[str, Any] = {}
    if base is None:
        warn_once(f"skill-missing:{skill_id}",
                  "未找到技能包 %s（领域层将为空；请检查 packs/skills/ 或 RA_PACKS_DIR）",
                  skill_id)
        _cache[key] = data
        return data
    main = base / "content" / "data.json"
    if main.is_file():
        loaded = _read_json(main)
        if isinstance(loaded, dict):
            data = loaded
    elif (base / "content").is_dir():
        # 没有 data.json 时，把 content/*.json 合并成一个字典
        for path in sorted((base / "content").glob("*.json")):
            loaded = _read_json(path)
            if isinstance(loaded, dict):
                data[path.stem] = loaded
    _cache[key] = data
    return data


def skill_content(skill_id: str, name: str) -> dict[str, Any]:
    """按文件名读取技能包 ``content/<name>.json``（不含扩展名）。

    与 :func:`skill_data` 相比，本函数可以访问同目录下的**任意**数据文件，
    例如 ``skill_content("journal-quartiles", "citation_styles")``。
    文件缺失时显式告警并返回空字典（不静默回落）。
    """
    key = ("skill-content", f"{skill_id}/{name}")
    if key in _cache:
        return _cache[key]
    base = skill_dir(skill_id)
    data: dict[str, Any] = {}
    if base is None:
        warn_once(f"skill-missing:{skill_id}",
                  "未找到技能包 %s", skill_id)
    else:
        path = base / "content" / f"{name}.json"
        if path.is_file():
            loaded = _read_json(path)
            if isinstance(loaded, dict):
                data = loaded
        else:
            warn_once(f"skill-content-missing:{skill_id}/{name}",
                      "技能包 %s 缺少 content/%s.json", skill_id, name)
    _cache[key] = data
    return data


def skill_terms(skill_id: str) -> list[dict[str, Any]]:
    """读取技能包词表 ``content/terms.jsonl``（每行一条 dict）。"""
    key = ("terms", skill_id)
    if key in _cache:
        return _cache[key]
    base = skill_dir(skill_id)
    rows: list[dict[str, Any]] = []
    if base is not None:
        path = base / "content" / "terms.jsonl"
        if path.is_file():
            rows = _read_jsonl(path)
    else:
        warn_once(f"terms-missing:{skill_id}",
                  "未找到技能包 %s 的词表（返回空表）", skill_id)
    _cache[key] = rows
    return rows


# ------------------------------------------------------------------ 领域包

def available_domains() -> list[str]:
    names: set[str] = set()
    for root in pack_roots():
        base = root / "domains"
        if base.is_dir():
            names.update(p.name for p in base.iterdir() if p.is_dir())
    return sorted(names)


def domain_dir(domain_kind: str) -> Path | None:
    return _resolve("domains", domain_kind)


def domain_data(domain_kind: str) -> dict[str, Any]:
    """读取领域包主文件 ``domain.json``。"""
    key = ("domain", domain_kind)
    if key in _cache:
        return _cache[key]
    base = domain_dir(domain_kind)
    data: dict[str, Any] = {}
    if base is None:
        warn_once(f"domain-missing:{domain_kind}",
                  "未找到领域包 %s（该领域将没有画像与词表）", domain_kind)
    else:
        path = base / "domain.json"
        if path.is_file():
            loaded = _read_json(path)
            if isinstance(loaded, dict):
                data = loaded
        else:
            warn_once(f"domain-file:{domain_kind}",
                      "领域包 %s 缺少 domain.json", domain_kind)
    _cache[key] = data
    return data


def domain_profile(domain_kind: str) -> dict[str, Any]:
    """返回领域画像（label/dimensions/candidate_entity_types/...），缺失返回空字典。"""
    data = domain_data(domain_kind)
    profile = data.get("profile")
    return dict(profile) if isinstance(profile, dict) else {}


def domain_terms(domain_kind: str) -> list[dict[str, Any]]:
    """读取领域词表 ``vocab/terms.jsonl``。"""
    return _domain_vocab(domain_kind, "terms")


def _domain_vocab(domain_kind: str, name: str) -> list[dict[str, Any]]:
    key = (f"domain-{name}", domain_kind)
    if key in _cache:
        return _cache[key]
    base = domain_dir(domain_kind)
    rows: list[dict[str, Any]] = []
    if base is not None:
        path = base / "vocab" / f"{name}.jsonl"
        if path.is_file():
            rows = _read_jsonl(path)
    _cache[key] = rows
    return rows


def infer_domain_kind(text: str) -> str:
    """按各领域包声明的关键词判定领域；都不命中时用 fallback 领域。"""
    lowered = str(text or "").lower()
    fallback = "general"
    best: tuple[int, str] | None = None
    for kind in available_domains():
        data = domain_data(kind)
        if data.get("fallback"):
            fallback = kind
        keywords = [str(k) for k in data.get("keywords") or [] if str(k).strip()]
        hits = sum(1 for k in keywords if k.lower() in lowered)
        if hits and (best is None or hits > best[0]):
            best = (hits, kind)
    return best[1] if best else fallback


# ------------------------------------------------------------------ 关系词表

def relation_lexicon() -> dict[str, Any]:
    """返回关系词表：``{"synonyms": {...}, "strong": [...], "types": [...], "node_types": [...]}``。"""
    data = skill_data("relation-lexicon")
    synonyms = data.get("synonyms") if isinstance(data.get("synonyms"), dict) else {}
    strong = data.get("strong_relations") or []
    node_types = data.get("seed_node_types") or []
    relation_types = data.get("seed_relation_types") or []
    if not synonyms:
        warn_once("relation-lexicon-empty",
                  "关系词表为空（packs/skills/relation-lexicon 缺失或未加载）")
    return {
        "synonyms": {str(k): str(v) for k, v in synonyms.items()},
        "strong": [str(x) for x in strong],
        "seed_node_types": node_types,
        "seed_relation_types": relation_types,
    }


def _merge_types(core: list[Any], key: str) -> list[list[Any]]:
    """核心种子类型 + 各领域包的追加类型（按类型键去重，核心优先）。"""
    merged: dict[str, list[Any]] = {}
    for item in core or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            merged[str(item[0])] = [str(item[0]), str(item[1])]
    for kind in available_domains():
        data = domain_data(kind)
        for item in data.get(key) or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                merged.setdefault(str(item[0]), [str(item[0]), str(item[1])])
    return list(merged.values())


def seed_node_types() -> list[list[Any]]:
    """种子节点类型：核心（技能包）+ 各领域包 `extra_seed_node_types`。"""
    return _merge_types(relation_lexicon()["seed_node_types"],
                        "extra_seed_node_types")


def seed_relation_types() -> list[list[Any]]:
    """种子关系类型：核心 + 各领域包 `extra_seed_relation_types`。

    领域专用关系（例如化学的 catalyzed_by/affords）在此扩展，
    避免把它们塞进通用词表。
    """
    return _merge_types(relation_lexicon()["seed_relation_types"],
                        "extra_seed_relation_types")


def strong_relations() -> set[str]:
    return set(relation_lexicon()["strong"])


def seed_types_for_domain(domain_kind: str) -> dict[str, list[list[Any]]]:
    """只取核心 + 指定领域的种子类型（按领域初始化本体时使用）。"""
    data = domain_data(domain_kind)
    lexicon = relation_lexicon()
    return {
        "node_types": _merge_types(lexicon["seed_node_types"],
                                    "extra_seed_node_types"),
        "relation_types": _merge_types(
            lexicon["seed_relation_types"], "extra_seed_relation_types"),
        "domain_extra_node_types": [list(x) for x in
                                    (data.get("extra_seed_node_types") or [])],
        "domain_extra_relation_types": [list(x) for x in
                                        (data.get("extra_seed_relation_types") or [])],
    }


def domain_lexicon(domain_kind: str) -> dict[str, Any]:
    """领域词表：``vocab/terms.jsonl`` 的内容与原质量归并层兼容的查找表。"""
    terms: dict[str, dict[str, Any]] = {}
    for row in domain_terms(domain_kind):
        term = str(row.get("term") or "").strip().lower()
        if not term:
            continue
        terms[term] = {
            "id": row.get("id"),
            "canonical": row.get("canonical") or term,
            "aliases": tuple(row.get("aliases") or ()),
            "source": row.get("source"),
        }
    return terms


def identity_for(term: str, domain_kind: str | None = None) -> dict[str, Any] | None:
    """按域名（或全部领域）查外部身份：返回 {id, canonical, aliases, source, domain}。"""
    key = " ".join(str(term or "").strip().lower().split())
    if not key:
        return None
    kinds = [domain_kind] if domain_kind else available_domains()
    for kind in kinds:
        entry = domain_lexicon(kind).get(key)
        if entry:
            return {**entry, "domain": kind}
    return None


def identity_terms(domain_kind: str | None = None) -> list[dict[str, Any]]:
    """列出所有带外部身份的术语（可用 domain_kind 过滤）。"""
    kinds = [domain_kind] if domain_kind else available_domains()
    out: list[dict[str, Any]] = []
    for kind in kinds:
        for term, entry in domain_lexicon(kind).items():
            out.append({"term": term, **entry, "domain": kind})
    return out


def canonical_relation_type(relation_type: str) -> str:
    """同义归一：命中词表则返回受控词，否则保留原词。"""
    key = " ".join(str(relation_type or "").strip().lower().split())
    synonyms: dict[str, str] = relation_lexicon()["synonyms"]
    return synonyms.get(key, relation_type)


# ------------------------------------------------------------------ 期刊分区

_JOURNAL_PUNCT_RE = re.compile(r"[\-_/\\.,;:'\"()\[\]{}]+")
_JOURNAL_NOISE_RE = re.compile(
    r"\b(?:international edition|int ed|in english|the|and)\b")
# 可安全丢弃的副标题/别名括号（避免"包含匹配"误命中）
_JOURNAL_TRAILING_PAREN_RE = re.compile(
    r"\((?:[^()]*(?:jacs|international|english|edition|series [ab]|"
    r"new york|london|print|online)[^()]*)\)")


def normalize_journal_key(name: str | None) -> str:
    """期刊名归一：大小写、标点、连字符、副标题括号与常见词缀统一。

    目的：让库中的 ``Angewandte Chemie (International ed. in English)`` 与
    JCR 的 ``ANGEWANDTE CHEMIE-INTERNATIONAL EDITION`` 命中同一条目。
    """
    text = str(name or "").strip().lower()
    if not text:
        return ""
    text = _JOURNAL_TRAILING_PAREN_RE.sub(" ", text)
    text = text.replace("&", " and ")
    text = _JOURNAL_PUNCT_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _JOURNAL_NOISE_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _merge_quartile_payload(target: dict[str, str], payload: Any) -> int:
    """把一份分区载荷并入 target；兼容平铺字典与 ``{"quartiles": {...}}`` 包装。"""
    if not isinstance(payload, dict):
        return 0
    inner = payload.get("quartiles")
    data = inner if isinstance(inner, dict) else payload
    added = 0
    for key, value in data.items():
        if not isinstance(value, str):
            continue
        quartile = value.strip().upper()
        if quartile not in ("Q1", "Q2", "Q3", "Q4"):
            continue
        normalized = normalize_journal_key(key)
        if normalized:
            target[normalized] = quartile
            added += 1
    return added


def journal_quartiles() -> dict[str, str]:
    """期刊分区：技能包内置种子 + 分科补充文件 + 环境变量指向的外部覆盖文件。

    合并顺序（后者覆盖前者）：
    1. `packs/skills/journal-quartiles/content/data.json` 的 `quartiles`（跨学科顶刊）；
    2. 同一技能包 `content/quartiles_*.json`（按学科补充，如 `quartiles_chemistry.json`，
       可由 `examples/import_jcr_xlsx.py` 从 JCR 名单生成）；
    3. `RA_JOURNAL_QUARTILES` 指向的 JSON 文件（全量 JCR 表等本地数据）。

    所有键都经 :func:`normalize_journal_key` 归一，查询请用 :func:`journal_quartile`。
    """
    merged: dict[str, str] = {}
    data = skill_data("journal-quartiles")
    _merge_quartile_payload(merged, data.get("quartiles"))
    base = skill_dir("journal-quartiles")
    if base is not None:
        for path in sorted((base / "content").glob("quartiles_*.json")):
            _merge_quartile_payload(merged, _read_json(path))
    override = os.getenv("RA_JOURNAL_QUARTILES")
    if override:
        for raw in override.split(os.pathsep):
            path = Path(raw)
            if not raw.strip():
                continue
            if path.is_file():
                _merge_quartile_payload(merged, _read_json(path))
            else:
                warn_once(f"journal-override:{raw}",
                          "RA_JOURNAL_QUARTILES 指向的文件不存在: %s", raw)
    if not merged:
        warn_once("journal-empty",
                  "期刊分区表为空（packs/skills/journal-quartiles 缺失）；"
                  "权威性评分将只依赖 H 指数与引用数")
    return merged


def journal_quartile(name: str | None,
                     table: dict[str, str] | None = None) -> str | None:
    """按归一化后的期刊名查分区；未命中返回 None。"""
    key = normalize_journal_key(name)
    if not key:
        return None
    quartiles = table if table is not None else journal_quartiles()
    hit = quartiles.get(key)
    if hit:
        return hit
    # 退化匹配：仅当一方是另一方的完整子串且长度足够时接受（副标题差异）
    for candidate, quartile in quartiles.items():
        if len(candidate) < 12:
            continue
        if candidate in key or key in candidate:
            return quartile
    return None


def quartile_scores() -> dict[str, float]:
    data = skill_data("journal-quartiles")
    scores = data.get("quartile_scores")
    if isinstance(scores, dict) and scores:
        return {str(k).upper(): float(v) for k, v in scores.items()}
    return {}


def iter_pack_files(kind: str) -> Iterable[Path]:
    """遍历某个包类别下的所有文件（调试/文档生成用）。"""
    for root in pack_roots():
        base = root / kind
        if base.is_dir():
            yield from sorted(p for p in base.rglob("*") if p.is_file())
