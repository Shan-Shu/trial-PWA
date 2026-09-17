"""部分模板：把"这一部分要什么"变成可执行的数据。

模板住在 ``packs/skills/writing/content/data.json`` 的
``genres.<genre>.templates.<key>``，由 `sections[].template` 引用。

它承担三件事（这是"部分 = 固定模板"的落地）：

1. **判定的锚**：``required_dimensions`` 决定这一部分考哪几个维度——方法/参数/指标节
   重点考"量化条件"，综述的版图/机制节重点考"可比研究"，而不是全篇一套阈值。
   未被列出的维度在本部分**权重为 0**，因此不参与它的闸门。
2. **提示词的预设**：``role`` / ``focus`` / ``evidence_types`` 会被渲染成结构化字段块，
   塞进 ``section_compose_user`` 提示词。
3. **用户可填的字段**：``fields`` 声明这一部分暴露哪些输入项与默认值。

**双模并行（纯并行）**：每个字段各自决定来源——
用户填了 → ``user``；规划节点给了 → ``plan``；否则用模板预设 → ``template``。
用户填过的字段，规划**不再覆盖**。
"""
from __future__ import annotations

from typing import Any

from research_agent import packs

__all__ = [
    "DimensionSpec",
    "template_for_section",
    "genre_definition",
    "collect_section_fields",
    "resolve_fields",
    "field_block",
    "dimension_spec",
    "field_source_summary",
    "list_templates",
    "EVIDENCE_TYPE_LABELS",
]

#: 证据类型的中文名（正文顶部的缺失标注直接用它）
EVIDENCE_TYPE_LABELS = {
    "citation": "可引用文献",
    "mechanism": "机制证据",
    "quantitative": "量化条件/数值",
    "comparative": "可比研究或对照",
    "protocol": "可复现流程",
    "risk": "风险与负结果",
}

#: 字段来源
SOURCE_USER = "user"
SOURCE_PLAN = "plan"
SOURCE_TEMPLATE = "template"

_EMPTY_TEMPLATE: dict[str, Any] = {}


class DimensionSpec:
    """某一部分的判定维度规格。

    ``weights`` 已把"不在 required 里"的维度权重置 0 并归一化，
    因此可以直接交给 ``evaluate_sufficiency`` 用，不必在判定函数里写分支。
    """

    def __init__(self, required: list[str], weights: dict[str, float],
                 allow_gaps: bool, min_support: int,
                 evidence_types: list[str] | None = None,
                 has_template: bool = True) -> None:
        self.required = list(required)
        self.weights = dict(weights)
        self.allow_gaps = bool(allow_gaps)
        self.min_support = max(1, int(min_support or 1))
        self.evidence_types = list(evidence_types or [])
        #: 该部分是否真的挂了模板——决定"允许带缺口写作"能不能生效
        self.has_template = bool(has_template)

    def as_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "weights": self.weights,
            "allow_gaps": self.allow_gaps,
            "min_support": self.min_support,
            "evidence_types": self.evidence_types,
            "has_template": self.has_template,
        }


def genre_definition(genre: str | None) -> tuple[str, dict[str, Any]]:
    """取体裁定义；返回 ``(genre_key, spec)``。

    与 ``writing/service.py::_genre`` 的区别：**不抛异常**。模板层要在
    "技能包缺失/体裁未知"时优雅退回空模板，而不是把整条链路打断。
    """
    data = packs.skill_data("writing") or {}
    genres = data.get("genres") or {}
    if not isinstance(genres, dict) or not genres:
        return str(genre or ""), {}
    chosen = str(genre or data.get("default_genre") or "")
    if chosen not in genres:
        chosen = next(iter(genres))
    spec = genres.get(chosen)
    return chosen, spec if isinstance(spec, dict) else {}


def _dimension_weights(genre_spec: dict[str, Any]) -> dict[str, float]:
    raw = genre_spec.get("dimension_weights") or {}
    if isinstance(raw, dict) and raw:
        return {str(k): float(v) for k, v in raw.items() if _is_number(v)}
    # 兜底：与 section_graph 的历史默认值保持一致
    return {"papers": 0.30, "knowledge": 0.20, "conditions": 0.20,
            "requirements": 0.15, "evidence": 0.10, "comparison": 0.05}


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def list_templates(genre: str | None) -> list[dict[str, Any]]:
    """列出该体裁所有模板（供界面渲染表单）。"""
    key, spec = genre_definition(genre)
    templates = spec.get("templates") or {}
    out: list[dict[str, Any]] = []
    for name, tpl in templates.items():
        if not isinstance(tpl, dict):
            continue
        out.append({"genre": key, "template": name, **tpl})
    return out


def template_for_section(genre: str | None,
                         section_key: str) -> tuple[str, dict[str, Any]]:
    """取某个部分对应的模板；该部分没挂模板时返回空模板（不报错）。"""
    _key, spec = genre_definition(genre)
    section = _find_section(spec, section_key)
    name = str((section or {}).get("template") or "")
    if not name:
        return "", dict(_EMPTY_TEMPLATE)
    tpl = (spec.get("templates") or {}).get(name)
    if not isinstance(tpl, dict):
        return "", dict(_EMPTY_TEMPLATE)
    return name, tpl


def _find_section(genre_spec: dict[str, Any],
                  section_key: str) -> dict[str, Any]:
    for section in genre_spec.get("sections") or []:
        if isinstance(section, dict) and str(section.get("key")) == str(section_key):
            return section
    return {}


def dimension_spec(genre: str | None, section_key: str) -> DimensionSpec:
    """该部分的维度规格：只保留 required 里的维度参与加权与闸门。"""
    name, tpl = template_for_section(genre, section_key)
    required = [str(x) for x in (tpl.get("required_dimensions") or [])
                if str(x).strip()]
    base = _dimension_weights(genre_definition(genre)[1])
    if not name:
        # 没挂模板（如摘要、参考文献节，或该体裁还没配模板）：
        # 沿用体裁默认权重，不设硬闸门，**也不允许带缺口写作**（退回旧约定）
        return DimensionSpec([], base, False, 1,
                             list(tpl.get("evidence_types") or []),
                             has_template=False)
    if not required:
        # 挂了模板但没声明必考维度：同样不设硬闸门，但允许按模板的 allow_gaps 走
        return DimensionSpec([], base, bool(tpl.get("allow_gaps", True)), 1,
                             list(tpl.get("evidence_types") or []),
                             has_template=True)
    weights = {k: (v if k in required else 0.0) for k, v in base.items()}
    for key in required:
        weights.setdefault(key, 1.0)
    total = sum(weights.values()) or 1.0
    weights = {k: round(v / total, 6) for k, v in weights.items()}
    return DimensionSpec(
        required, weights,
        bool(tpl.get("allow_gaps", True)),
        int(tpl.get("min_support") or 1),
        [str(x) for x in (tpl.get("evidence_types") or [])],
        has_template=True,
    )


def collect_section_fields(genre: str | None,
                           section_key: str) -> list[dict[str, Any]]:
    """该部分声明的可填字段（带预设值）。"""
    _name, tpl = template_for_section(genre, section_key)
    out: list[dict[str, Any]] = []
    for field in tpl.get("fields") or []:
        if not isinstance(field, dict):
            continue
        key = str(field.get("key") or "").strip()
        if not key:
            continue
        out.append({
            "key": key,
            "label": str(field.get("label") or key),
            "type": str(field.get("type") or "text"),
            "preset": field.get("preset"),
            "placeholder": str(field.get("placeholder") or ""),
        })
    return out


def resolve_fields(genre: str | None, section_key: str,
                   user_values: dict[str, Any] | None = None,
                   plan_values: dict[str, Any] | None = None) -> dict[str, Any]:
    """合并字段值并标注来源（**纯并行**：用户填过的不被规划覆盖）。

    优先级：用户 > 规划 > 模板预设。
    返回 ``{field_key: {"label","type","value","source","preset"}}``。
    """
    user_values = user_values or {}
    plan_values = plan_values or {}
    out: dict[str, Any] = {}
    for field in collect_section_fields(genre, section_key):
        key = field["key"]
        user_raw = user_values.get(key)
        plan_raw = plan_values.get(key)
        if _has_value(user_raw):
            value, source = user_raw, SOURCE_USER
        elif _has_value(plan_raw):
            value, source = plan_raw, SOURCE_PLAN
        else:
            # 模板声明了这个字段 → 来源恒为 template（值可能为空，
            # 表示"该字段没有预设默认值，等用户填"）。来源标记不能因为
            # 值为空就丢掉，否则界面与轨迹无法解释字段从哪来。
            value, source = field.get("preset"), SOURCE_TEMPLATE
        out[key] = {**field, "value": value, "source": source}
    # 用户额外填的、模板没声明的字段也保留下来（不被静默丢弃）
    for key, raw in user_values.items():
        if key in out or not _has_value(raw):
            continue
        out[str(key)] = {"key": str(key), "label": str(key), "type": "text",
                         "preset": None, "placeholder": "",
                         "value": raw, "source": SOURCE_USER}
    return out


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def field_source_summary(fields: dict[str, Any]) -> dict[str, list[str]]:
    """按来源归类字段名，用于界面与轨迹展示"哪些是用户给的、哪些是系统拟的"。"""
    out: dict[str, list[str]] = {SOURCE_USER: [], SOURCE_PLAN: [],
                                 SOURCE_TEMPLATE: []}
    for key, item in (fields or {}).items():
        source = str((item or {}).get("source") or "")
        if source in out:
            out[source].append(str(key))
    return out


def field_block(genre: str | None, section_key: str,
                fields: dict[str, Any],
                role: str = "", focus: list[str] | None = None,
                evidence_types: list[str] | None = None) -> str:
    """渲染给模型看的结构化字段块。

    每个字段都标出来源：用户输入优先于模板默认——这是双模并行在提示词层面的
    体现，模型必须能分辨"这是用户明确要求的"还是"这是模板的默认建议"。
    """
    _name, tpl = template_for_section(genre, section_key)
    role = role or str(tpl.get("role") or "")
    focus = list(focus if focus is not None else (tpl.get("focus") or []))
    evidence_types = list(
        evidence_types if evidence_types is not None
        else (tpl.get("evidence_types") or []))

    lines: list[str] = []
    if role:
        lines.append(f"本部分职责：{role}")
    if focus:
        lines.append("写作要点：" + "；".join(str(x) for x in focus))
    if evidence_types:
        labels = [EVIDENCE_TYPE_LABELS.get(str(x), str(x))
                  for x in evidence_types]
        lines.append("应依据的证据类型：" + "、".join(labels))
    if lines:
        lines.append("")

    if not fields:
        return "\n".join(lines).strip()

    lines.append("结构化输入（source=user 的条目是用户的明确要求，优先于任何默认）：")
    for key, item in fields.items():
        item = item or {}
        source = str(item.get("source") or "")
        if not source:
            continue
        value = item.get("value")
        if not _has_value(value):
            continue
        tag = {"user": "用户指定", "plan": "规划拟定",
               "template": "模板默认"}.get(source, source)
        lines.append(f"- [{tag}] {item.get('label') or key}：{value}")
    return "\n".join(lines).strip()
