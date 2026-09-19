"""反应设计算子库（自研核心资产）。

创新性来自"算子序列"而不是"A 组件 + B 组件"。本模块把算子做成**封闭词表**：

- 词表内的算子才有稳定的名字、语义和输入/输出契约；
- LLM 只能从词表里"选择并排序"，不能自造算子名；
- 代码层校验算子链是否合法、前后是否衔接（``chain_break``）。

算子语义遵循 ``问题与修改方向.md`` 第二节列出的机制级变换清单，
并保留 ``combine / substitute / migrate / extend`` 作为最低层级算子，
它们仍然可用，但创新等级被显式标注为 L1/L2，不能作为高阶候选的主标签。
"""
from __future__ import annotations

import re
from typing import Any

# 创新等级：L0 复述 / L1 底物范围扩展 / L2 组件组合 / L3 机制级重设计 / L4 新反应原理
INNOVATION_LEVELS = ("L0", "L1", "L2", "L3", "L4")

INNOVATION_LEVEL_LABEL = {
    "L0": "复述已有方法",
    "L1": "底物/范围扩展",
    "L2": "组件组合",
    "L3": "机制级重设计",
    "L4": "新反应原理",
}

INNOVATION_LEVEL_RANK = {level: i for i, level in enumerate(INNOVATION_LEVELS)}

# 每个算子：名称、中文标签、层级、是否机制级、语义、输入契约、输出契约
OPERATORS: dict[str, dict[str, Any]] = {
    "polarity_reversal": {
        "label_zh": "极性反转 / Umpolung",
        "level": "L3",
        "input": "亲电性碳中心（如炔酰胺 β-碳、羰基碳）",
        "output": "亲核性碳中心或 d1/d3 合成子",
        "semantics": "把原本亲电的位点改造成亲核位点，从而改变成键极性方向",
    },
    "dearomatization": {
        "label_zh": "去芳构化 / 再芳构化",
        "level": "L3",
        "input": "芳香或杂芳香底物",
        "output": "去芳构化中间体或螺环/稠环骨架",
        "semantics": "以破坏芳香性换取张力或反应性，再通过再芳构化收尾",
    },
    "strain_release": {
        "label_zh": "张力释放驱动开环",
        "level": "L3",
        "input": "小环（双环丁烷/环丙烷/氮杂环丙烷）",
        "output": "开环后形成的 1,3-偶极或双自由基等价体",
        "semantics": "利用环张力作为热力学驱动力，开环后立即被亲电/亲核伙伴捕获",
    },
    "transient_directing": {
        "label_zh": "瞬态导向 / 可逆导向",
        "level": "L3",
        "input": "可原位生成并可自行脱除的导向基",
        "output": "定向活化后的官能化产物",
        "semantics": "用瞬态可逆导向基控制区域选择性，避免化学计量导向基残留",
    },
    "carbene_migration": {
        "label_zh": "卡宾迁移 / Doyle–Kirmse",
        "level": "L3",
        "input": "金属卡宾或 α-亚胺卡宾",
        "output": "1,2-迁移后的重排产物",
        "semantics": "以卡宾为枢纽发生 1,2-迁移，形成季碳或杂环骨架",
    },
    "radical_polar_crossover": {
        "label_zh": "自由基-极性交叉",
        "level": "L3",
        "input": "单电子转移生成的自由基",
        "output": "极性中间体（碳正/碳负/烯醇等价体）",
        "semantics": "在同一序列内切换自由基与极性机制，绕开常规极性不匹配",
    },
    "dual_catalysis_relay": {
        "label_zh": "双催化接力",
        "level": "L3",
        "input": "两类独立催化循环各自的底物",
        "output": "跨循环接力后的偶联/成环产物",
        "semantics": "用两个催化循环分别完成活化与成键，实现单一催化无法完成的转化",
    },
    "selectivity_lock": {
        "label_zh": "选择性锁定",
        "level": "L3",
        "input": "可产生多种区域/立体异构的中间体",
        "output": "单一主导构型或构象的产物",
        "semantics": "通过配体、导向或可逆步骤把选择性锁定在单一路径上",
    },
    "intermediate_capture": {
        "label_zh": "瞬态中间体捕获",
        "level": "L3",
        "input": "高活性瞬态中间体（乙烯基阳离子/卡宾/自由基）",
        "output": "被外源或内源捕获后的成环/官能化产物",
        "semantics": "在中间体寿命内引入捕获剂，把不可观测中间体转化为目标骨架",
    },
    "ring_contraction_expansion": {
        "label_zh": "环收缩 / 扩环",
        "level": "L3",
        "input": "某一环尺寸的中间体或产物",
        "output": "收缩或扩环后的环系",
        "semantics": "通过迁移插入或开环重排改变环尺寸，构建难以直接成环的骨架",
    },
    "bond_migration": {
        "label_zh": "键迁移 / 骨架重排",
        "level": "L3",
        "input": "含可迁移基团的活泼中间体",
        "output": "迁移后重排的骨架",
        "semantics": "利用 1,2-/1,3-迁移重塑碳骨架或杂原子位置",
    },
    "heteroatom_insertion": {
        "label_zh": "杂原子插入 / 删除",
        "level": "L3",
        "input": "缺一个杂原子的前体与杂原子转移试剂",
        "output": "插入（或删除）杂原子后的杂环骨架",
        "semantics": "定向向骨架引入第二个杂原子，直接回应多氮/多杂类硬约束",
    },
    "dynamic_kinetic_resolution": {
        "label_zh": "动态动力学拆分",
        "level": "L3",
        "input": "可快速消旋的手性不稳定底物",
        "output": "高对映体纯度的单一构型产物",
        "semantics": "让底物持续消旋并只让一个对映体反应，把消旋损失转化为产率",
    },
    "switchable_valence": {
        "label_zh": "可切换催化价态",
        "level": "L3",
        "input": "可在两价态间切换的金属催化剂",
        "output": "由价态决定的差异产物",
        "semantics": "通过氧化/还原切换催化剂价态，改变反应路径与产物分布",
    },
    "cascade_termination": {
        "label_zh": "级联终止控制",
        "level": "L3",
        "input": "多步级联的活性链末端",
        "output": "在指定步骤终止的级联产物",
        "semantics": "控制级联在目标步骤终止，避免过度反应或骨架崩塌",
    },
    # 低层级算子：仍然可用，但不得作为高阶候选的核心创新标签
    "combine": {
        "label_zh": "组合已有方案",
        "level": "L2",
        "input": "两个独立可行的已有步骤/模块",
        "output": "串联后的流程",
        "semantics": "把两个已报道模块串起来，创新性来自流程而非机制",
    },
    "substitute": {
        "label_zh": "替换组件",
        "level": "L1",
        "input": "已有方案中的某个组件",
        "output": "替换组件后的同类方案",
        "semantics": "只更换催化剂/配体/底物等组件，机制不变",
    },
    "migrate": {
        "label_zh": "跨域迁移",
        "level": "L1",
        "input": "其他对象/领域已验证的方案",
        "output": "迁移到目标对象后的方案",
        "semantics": "把已有机制搬到新底物类别，机制本身不改写",
    },
    "extend": {
        "label_zh": "扩展对象范围",
        "level": "L1",
        "input": "已有方案的适用对象",
        "output": "更宽范围的适用对象",
        "semantics": "扩大底物或条件范围，属于范围扩展",
    },
    "unclassified_transform": {
        "label_zh": "未归类变换",
        "level": "L2",
        "input": "未知",
        "output": "未知",
        "semantics": "证据不足以归类到上述算子，需人工确认",
    },
}

# 提示词展示用别名（中英混用的常见写法统一到规范名）
OPERATOR_ALIASES = {
    "umpolung": "polarity_reversal",
    "极性反转": "polarity_reversal",
    "去芳构化": "dearomatization",
    "dearomatisation": "dearomatization",
    "张力释放": "strain_release",
    "瞬态导向": "transient_directing",
    "卡宾迁移": "carbene_migration",
    "自由基-极性交叉": "radical_polar_crossover",
    "双催化接力": "dual_catalysis_relay",
    "选择性锁定": "selectivity_lock",
    "瞬态中间体捕获": "intermediate_capture",
    "中间体捕获": "intermediate_capture",
    "环收缩": "ring_contraction_expansion",
    "扩环": "ring_contraction_expansion",
    "键迁移": "bond_migration",
    "杂原子插入": "heteroatom_insertion",
    "动态动力学拆分": "dynamic_kinetic_resolution",
    "dkr": "dynamic_kinetic_resolution",
    "可切换催化价态": "switchable_valence",
    "级联终止控制": "cascade_termination",
    "组合": "combine",
    "替换": "substitute",
    "迁移": "migrate",
    "扩展": "extend",
}

OPERATOR_NAMES = {name: spec["label_zh"] for name, spec in OPERATORS.items()}
MECHANISTIC_OPERATORS = {name for name, spec in OPERATORS.items()
                         if spec["level"] in ("L3", "L4")}
LOW_LEVEL_OPERATORS = {"combine", "substitute", "migrate", "extend"}

_OP_NAME_RE = re.compile(r"[^a-z0-9_]+")


def canonical_operator(value: Any) -> str:
    """把模型输出的算子名/中文名/别名统一到词表规范名；不在词表内返回空串。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text in OPERATORS:
        return text
    lowered = text.lower()
    if lowered in OPERATORS:
        return lowered
    if text in OPERATOR_ALIASES:
        return OPERATOR_ALIASES[text]
    if lowered in OPERATOR_ALIASES:
        return OPERATOR_ALIASES[lowered]
    normalized = _OP_NAME_RE.sub("_", lowered).strip("_")
    if normalized in OPERATORS:
        return normalized
    # 子串兜底：如 "polarity reversal (umpolung)" → polarity_reversal
    for alias, target in sorted(OPERATOR_ALIASES.items(), key=lambda x: -len(x[0])):
        if alias and alias in lowered:
            return target
    for name, spec in OPERATORS.items():
        if name in normalized or spec["label_zh"].lower() in lowered:
            return name
    return ""


def _normalize_step(step: Any) -> dict[str, Any] | None:
    if isinstance(step, str):
        name = canonical_operator(step)
        if not name:
            return None
        return {"operator": name, "operator_label": OPERATOR_NAMES.get(name, name),
                "input": "", "output": "", "level": OPERATORS[name]["level"]}
    if not isinstance(step, dict):
        return None
    name = canonical_operator(step.get("operator") or step.get("name")
                              or step.get("type"))
    if not name:
        return None
    spec = OPERATORS[name]
    return {
        "operator": name,
        "operator_label": spec["label_zh"],
        "input": str(step.get("input") or step.get("input_state") or "").strip(),
        "output": str(step.get("output") or step.get("output_state") or "").strip(),
        "level": spec["level"],
    }


def _texts_connect(prev_output: str, next_input: str) -> bool:
    """判断前后两步是否衔接；未知信息（空串）保守视为衔接。"""
    prev = (prev_output or "").strip().lower()
    nxt = (next_input or "").strip().lower()
    if not prev or not nxt:
        return True
    if prev == nxt:
        return True
    tokens_prev = {t for t in re.findall(r"[a-z\u4e00-\u9fff]{2,}", prev)}
    tokens_next = {t for t in re.findall(r"[a-z\u4e00-\u9fff]{2,}", nxt)}
    if not tokens_prev or not tokens_next:
        return True
    return bool(tokens_prev & tokens_next)


def normalize_operator_chain(chain: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """校验并规范化算子链。

    返回 ``(算子链, 校验报告)``。报告中包含：

    - ``unknown_operators``：不在词表内的算子名（被丢弃）；
    - ``chain_breaks``：前后输入/输出不衔接的位置；
    - ``declared_level``：由算子序列推出的创新等级下限；
    - ``mechanistic_count``：机制级算子数量；
    - ``level_basis``：等级判定依据。
    """
    report: dict[str, Any] = {
        "unknown_operators": [],
        "duplicate_operators": [],
        "chain_breaks": [],
        "steps": 0,
        "mechanistic_count": 0,
        "declared_level": "L0",
        "max_step_level": "L0",
        "level_basis": "",
        "low_level_only": False,
    }
    if not isinstance(chain, list):
        report["level_basis"] = "未提供算子链"
        return [], report
    out: list[dict[str, Any]] = []
    for step in chain:
        if step is None:
            continue
        if isinstance(step, dict) and not canonical_operator(
                step.get("operator") or step.get("name") or step.get("type")):
            unknown = str(step.get("operator") or step.get("name")
                          or step.get("type") or "").strip()
            if unknown:
                report["unknown_operators"].append(unknown)
            continue
        if isinstance(step, str) and not canonical_operator(step):
            report["unknown_operators"].append(step)
            continue
        parsed = _normalize_step(step)
        if parsed is None:
            continue
        # 拒绝相邻重复算子：同一算子连续出现不构成"算子链"，只是占位
        if out and out[-1]["operator"] == parsed["operator"]:
            report.setdefault("duplicate_operators", []).append(parsed["operator"])
            continue
        out.append(parsed)
    report["steps"] = len(out)
    if not out:
        report["level_basis"] = "算子链为空或全部不在词表内"
        return [], report

    mechanistic = [s for s in out if s["operator"] in MECHANISTIC_OPERATORS]
    report["mechanistic_count"] = len(mechanistic)
    max_level = "L0"
    for step in out:
        if INNOVATION_LEVEL_RANK[step["level"]] > INNOVATION_LEVEL_RANK[max_level]:
            max_level = step["level"]
    report["max_step_level"] = max_level
    report["low_level_only"] = all(
        s["operator"] in LOW_LEVEL_OPERATORS or s["operator"] == "unclassified_transform"
        for s in out)
    # 等级判定：链中机制级算子越多、序列越长，等级越高
    if len(mechanistic) >= 2:
        level = "L4"
        basis = f"{len(mechanistic)} 个机制级算子串联，构成本知识库中未见过的算子序列"
    elif len(mechanistic) == 1:
        level = "L3"
        basis = f"包含机制级算子 {mechanistic[0]['operator']}（{mechanistic[0]['operator_label']}）"
    elif any(s["operator"] in ("combine", "unclassified_transform") for s in out):
        level = "L2"
        basis = "仅由组合/未归类算子构成，属于流程重组"
    else:
        level = "L1"
        basis = "仅由替换/迁移/扩展构成，属于范围扩展"
    report["declared_level"] = level
    report["level_basis"] = basis
    for i in range(1, len(out)):
        previous, current = out[i - 1], out[i]
        if not _texts_connect(previous.get("output"), current.get("input")):
            report["chain_breaks"].append({
                "index": i,
                "previous_output": previous.get("output"),
                "next_input": current.get("input"),
                "reason": "前一步输出与后一步输入无重叠，序列可能不衔接",
            })
    return out, report


def chain_level(chain: Any) -> str:
    """只取算子链推出的创新等级。"""
    return normalize_operator_chain(chain)[1]["declared_level"]


def meets_innovation_floor(chain: Any, floor: str | None) -> bool:
    """判断算子链是否达到 Planner 设定的创新等级下限。"""
    floor = str(floor or "L0").upper()
    if floor not in INNOVATION_LEVEL_RANK:
        floor = "L0"
    level = chain_level(chain)
    return INNOVATION_LEVEL_RANK[level] >= INNOVATION_LEVEL_RANK[floor]


def operator_catalog_for_prompt(max_items: int = 24) -> str:
    """生成给 LLM 的算子词表清单（名称 + 中文 + 契约 + 等级）。"""
    lines: list[str] = []
    for name, spec in list(OPERATORS.items())[:max_items]:
        lines.append(
            f"- {name}（{spec['label_zh']}，{spec['level']}）："
            f"输入={spec['input']}；输出={spec['output']}；语义={spec['semantics']}")
    return "\n".join(lines)


def operator_spec(name: str) -> dict[str, Any] | None:
    canonical = canonical_operator(name)
    return OPERATORS.get(canonical) if canonical else None
