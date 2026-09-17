"""内容形成节点。

从知识消费节点返回的模式卡/证据卡/超边与设计上下文（design_context）中，
形成内容或候选方案。节点不查询数据库，不新增证据 ID。

v0.4.1 关键变化：
1. 候选方案的基本单位是**算子链**（operator_chain），不再是单个“创造操作”；
2. Planner 的 design_contract（目标硬约束、创新等级下限、差异轴、候选数量）
   直接进入提示词，并在代码层做硬校验；
3. 候选池先生成 30–50 个种子再聚类去重、评分排序，选出最终 4–6 个；
4. 审核节点的 revision_actions 会被本节点消费并在输出中逐条回应。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from langchain_core.messages import HumanMessage

from research_agent.config import Settings, settings as default_settings
from research_agent.retrieval.skills import (
    normalize_edge_gaps,
    select_low_support_gaps,
)
from research_agent.study.events import log_study_event
from research_agent.study.json_utils import clean_str, parse_json_object
from research_agent.study.model_call import invoke_with_timeout
from research_agent.study.reaction_operators import (
    INNOVATION_LEVEL_LABEL,
    INNOVATION_LEVEL_RANK,
    LOW_LEVEL_OPERATORS,
    canonical_operator,
    normalize_operator_chain,
    operator_catalog_for_prompt,
)

logger = logging.getLogger(__name__)

CONTENT_PROMPT = """你是科研内容形成节点。工作方式是“先看证据，再形成结构”，
不允许先假设章节再找论据。

输入材料：
1. 研究任务单 task_plan（含 analysis_plan、evidence_policy、design_contract）；
2. 知识消费节点返回的机制理解 consumer_analysis 与设计上下文 design_context；
3. 模式卡 patterns、证据卡 evidence 与科研超边 hyperedges。

要求：
1. 从 patterns 中观察高支持度、高置信度、同关系聚合的模式，再归纳章节和论点；
2. 每条实质性论点必须引用真实存在的 pattern_id / evidence_id / hyperedge_id；
3. 无法被证据支撑但值得提出的内容标为 status="open_question"，不要伪造证据；
4. 不要把 correlation 写成 causality；
5. design_context 是候选方案的主要素材来源：mechanism_states 提供机制起点，
   opportunity_gaps 提供缺口，operator_candidates 提供算子链原型；
6. 只输出 JSON 对象，不要代码块、不要解释。

task_plan:
{plan}

knowledge:
{knowledge}

输出 JSON 结构：
{{
  "title": "内容标题",
  "summary": {{
    "text": "一段摘要",
    "pattern_ids": [],
    "evidence_ids": []
  }},
  "sections": [
    {{
      "heading": "由证据归纳出的章节名",
      "items": [
        {{
          "text": "具体观点",
          "pattern_ids": ["P-xxxx"],
          "evidence_ids": ["E-xxxx"],
          "status": "supported|hypothesis|open_question"
        }}
      ]
    }}
  ],
  "edge_gaps": [
    {{
      "pattern_id": "P-xxxx",
      "source_type": "源实体类型",
      "source_name": "源实体",
      "relation_type": "promotes",
      "target_type": "目标实体类型",
      "target_name": "目标实体",
      "support_count": 1,
      "target_support": 2,
      "priority": "high|medium|low",
      "reason": "该边当前只有单篇支持，若用于强结论需补强"
    }}
  ]
}}

请直接输出 JSON："""

GENERATIVE_BLOCK = """

附加生成要求（任务性质 generative，以下为硬性要求）：
1. 必须在 sections 之外输出 candidates 数组，至少 {pool_size} 个**骨架候选**，
   用于后续聚类去重。骨架候选只需写清：目标、机制算子链、创新等级与依据、
   与其他候选的区别、硬约束是否满足；风险、验证计划与可行性论证会在
   **下一个独立步骤**里对入选候选补全，此处不要展开，保持输出精简。
2. 每个候选的核心是 operator_chain，不是“组合/替换/迁移/扩展”。
   - operator 必须取自下面的算子词表，**不得自造算子名**；
   - 每个算子的 input / output 要写清前后态，序列必须首尾衔接；
   - 至少 {min_mechanistic} 个候选的算子链要达到 {innovation_floor} 级（含机制级算子）。
   - 不要为了凑数量把同一机制换底物写成多条：指纹相同的候选会被代码合并，
     合并后数量不足会直接判定候选池不合格。
3. 每个候选必须显式标注 satisfies_constraints：逐条回应 design_contract 的
   target_constraints.hard_constraints，说明满足或无法满足的理由。
4. differentiation 字段用一句话说明“这个候选和其余候选的本质区别”。
5. status 必须为 hypothesis。

算子词表（只允许使用下列 operator 名称）：
{operators}

candidates JSON 结构（骨架级，字段保持精简）：
{{
  "candidates": [
    {{
      "id": "C-01",
      "title": "候选方案名称",
      "target": "目标对象/骨架",
      "operator_chain": [
        {{"operator": "polarity_reversal",
          "input": "起始态", "output": "中间态"}},
        {{"operator": "intermediate_capture",
          "input": "中间态", "output": "目标骨架"}}
      ],
      "innovation_level": "L3",
      "innovation_basis": "为什么达到该等级（相对已有方案的机制差异，一句话）",
      "differentiation": "与其余候选的本质区别（一句话）",
      "satisfies_constraints": [
        {{"constraint": "Planner 硬约束原文",
          "satisfied": true,
          "reason": "满足或不满足的具体理由"}}
      ],
      "components": ["用到的已有方法/组件"],
      "novelty_source": "创新来源（机制层面，而非换底物）",
      "mechanism_evidence": ["MS-0001", "GAP-0001"],
      "pattern_ids": [],
      "evidence_ids": [],
      "status": "hypothesis"
    }}
  ]
}}
"""

REVIEW_BLOCK = """

附加综述写作要求（任务性质 {task_kind}，必须满足）：
1. 目标长度 {target_chars} 个中文字符（允许 ±15%），不要写成提纲或要点罗列。
2. 必须使用下列小节标题，且每个小节都要有成段论述（每节不少于 200 字）：
   ## 摘要
   ## 引言
   ## 结构特征与反应性
   ## 成环与环加成策略
   ## 催化体系与条件
   ## 区域与立体选择性
   ## 代表性骨架与应用
   ## 挑战与展望
   ## 结论
3. sections 的每一项 text 必须是**完整段落**（3-6 句、150-400 字），
   不要把一句话拆成一条；同一小节用 2-3 个 items 覆盖不同侧面即可。
4. 每条实质结论都要在 text 末尾带真实编号引用，格式为 [P-xxxx] / [E-xxxx] / [H-xxxx]，
   只能使用输入 knowledge 中真实存在的编号；无证据的推断必须明确写成
   “目前仅有间接证据”或“尚待验证”，不得编造文献。
5. 摘要写成一段连贯文字（200-300 字），不要分点。
6. 不要输出 candidates 数组（本任务不是生成新方法）。
7. 语言：中文；保留化学专有名词、催化剂与反应名称的英文原名。
"""

REVISION_BLOCK = """

上一轮审核未通过，必须逐条处理下列修订意见（revision_actions）：
{revision_actions}

要求：
1. 每条意见都要在 revision_responses 中给出 resolved=true/false 与理由；
2. 若无法解决，说明具体缺什么证据，并写入 edge_gaps；
3. 修订后重新输出完整 JSON（不要只输出差异，正文每个小节的段落都要保留）。
"""

EXPAND_PROMPT = """你是科研内容形成节点的"候选深化"步骤。

上一阶段已经选出下列 {count} 个候选方案（含机制算子链），它们的机制方向已经确定。
现在只做一件事：为这些候选补全可执行细节，不要新增候选、不要改动 operator_chain。

必须为每个候选补全：
1. satisfies_constraints：逐条回应 design_contract 的硬约束（每条给 satisfied + reason）；
2. risks：至少 2 条具体风险（条件兼容性/选择性/副反应/放大）；
3. validation_plan：最小验证实验（底物、条件范围、判据、成功的判定标准）；
4. rationale：为什么该算子链在化学上可能成立；
5. novelty_source：相对已有方案的机制差异（不得写成"换底物/换催化剂"）。

design_contract:
{contract}

待深化的候选（operator_chain 必须原样保留）：
{candidates}

只输出 JSON：
{{"candidates": [{{"id": "C-01", "title": "...", "operator_chain": [...],
  "innovation_level": "L3", "innovation_basis": "...", "differentiation": "...",
  "satisfies_constraints": [{{"constraint": "...", "satisfied": true, "reason": "..."}}],
  "components": [], "novelty_source": "...", "rationale": "...",
  "mechanism_evidence": [], "pattern_ids": [], "evidence_ids": [],
  "risks": [], "validation_plan": "..."}}]}}
"""

DEFAULT_POOL_SIZE = 36
DEFAULT_SELECT = 6
# 综述/前沿类任务的目标篇幅（中文字符），可由 Planner 的 budget.review_target_chars 覆盖
DEFAULT_REVIEW_CHARS = 3000
# 单次 LLM 调用里"种子候选"的期望产出上限：超过这个规模容易出现超长 JSON，
# 导致请求长时间挂起甚至被服务端截断。因此候选池分两阶段：
#   阶段 1（宽松）：生成 max(pool_target, min_candidates) 个"骨架候选"（含算子链）；
#   阶段 2（详细）：只对最终入选的 4–6 个候选补全风险、验证计划与硬约束回应。
MAX_SEED_CANDIDATES = 16

LOW_OPERATOR_LABELS = {
    "combine": "组合已有方案",
    "substitute": "替换组件",
    "migrate": "跨域迁移",
    "extend": "扩展对象范围",
}


def _string_list(value: Any, limit: int = 30) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = [clean_str(x) for x in value if clean_str(x)]
    return list(dict.fromkeys(out))[:limit]


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1", "是", "满足"):
            return True
        if low in ("false", "no", "0", "否", "不满足"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def build_design_contract(plan: dict[str, Any] | None) -> dict[str, Any]:
    """把 design_contract / creative_contract 合并成内容节点使用的稳定契约。"""
    plan = plan or {}
    contract = plan.get("design_contract") or {}
    creative = plan.get("creative_contract") or {}
    constraints = contract.get("target_constraints") or {}
    hard = constraints.get("hard_constraints") if isinstance(constraints, dict) else None
    if hard is None and isinstance(constraints, list):
        hard = constraints
    must_explain = constraints.get("must_explain") if isinstance(
        constraints, dict) else None
    return {
        "objective": clean_str(contract.get("objective")
                               or creative.get("objective"), clean_str(plan.get("goal"))),
        "hard_constraints": _string_list(hard, limit=20),
        "must_explain": _string_list(must_explain, limit=20),
        "innovation_floor": clean_str(
            contract.get("innovation_floor") or creative.get("innovation_floor"), "L3"),
        "min_candidates": max(1, int(contract.get("min_candidates")
                                      or creative.get("min_candidates") or 4)),
        "differentiation_axes": _string_list(
            contract.get("differentiation_axes")
            or creative.get("differentiation_axes"), limit=20),
        "evaluation_criteria": _string_list(
            contract.get("evaluation_criteria")
            or creative.get("evaluation_criteria"), limit=30),
        "constraints": _string_list(
            contract.get("constraints") or creative.get("constraints"), limit=30),
    }


# ------------------------------------------------------------------ 候选池与去重

def candidate_signature(candidate: dict[str, Any]) -> str:
    """候选的机制指纹：算子序列 + 目标对象。用于聚类去重。"""
    chain = candidate.get("operator_chain") or []
    ops = ">".join(str(step.get("operator") or "") for step in chain)
    target = clean_str(candidate.get("target"), "").lower()
    return f"{ops}|{target[:40]}"


def dedupe_candidates(candidates: list[dict[str, Any]]) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]]]:
    """按机制指纹聚类：每组保留信息最全的一个，其余记为同构候选。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        groups.setdefault(candidate_signature(candidate), []).append(candidate)

    def completeness(item: dict[str, Any]) -> tuple:
        return (
            1 if item.get("innovation_basis") else 0,
            len(item.get("satisfies_constraints") or []),
            len(item.get("mechanism_evidence") or []),
            len(item.get("evidence_ids") or []) + len(item.get("pattern_ids") or []),
            len(item.get("rationale") or ""),
        )

    kept: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for signature, items in groups.items():
        items = sorted(items, key=completeness, reverse=True)
        winner = items[0]
        winner["cluster_size"] = len(items)
        winner["cluster_members"] = [clean_str(x.get("title"), "") for x in items]
        kept.append(winner)
        for other in items[1:]:
            duplicates.append({
                "title": clean_str(other.get("title"), ""),
                "same_as": clean_str(winner.get("title"), ""),
                "signature": signature,
            })
    return kept, duplicates


def score_candidate(candidate: dict[str, Any], contract: dict[str, Any],
                    pool_size: int) -> dict[str, Any]:
    """候选评分：创新等级 + 目标匹配 + 证据支持 + 差异性。"""
    chain = candidate.get("operator_chain") or []
    validation = candidate.get("operator_chain_validation") or {}
    declared = validation.get("declared_level")
    llm_level = clean_str(candidate.get("innovation_level"), "").upper()
    level = declared or (llm_level if llm_level in INNOVATION_LEVEL_RANK else "L0")
    if llm_level in INNOVATION_LEVEL_RANK and declared and \
            INNOVATION_LEVEL_RANK[llm_level] < INNOVATION_LEVEL_RANK[declared]:
        # 模型自评低于代码判定时以模型为准，避免高估
        level = llm_level
    level_rank = INNOVATION_LEVEL_RANK.get(level, 0)
    floor = clean_str(contract.get("innovation_floor"), "L3").upper()
    floor_rank = INNOVATION_LEVEL_RANK.get(floor, 3)
    hard = contract.get("hard_constraints") or []
    satisfied = [x for x in (candidate.get("satisfies_constraints") or [])
                 if _bool(x.get("satisfied"))]
    evidence_count = len(set(candidate.get("evidence_ids") or [])
                         | set(candidate.get("pattern_ids") or [])
                         | set(candidate.get("mechanism_evidence") or []))
    low_level_only = bool(chain) and all(
        step.get("operator") in LOW_LEVEL_OPERATORS for step in chain)
    scores = {
        "innovation_level": level,
        "innovation_rank": level_rank,
        "meets_innovation_floor": level_rank >= floor_rank,
        "level_basis": validation.get("level_basis")
        or clean_str(candidate.get("innovation_basis"), ""),
        "constraint_total": len(hard),
        "constraint_satisfied": len(satisfied),
        "constraint_ratio": round(len(satisfied) / len(hard), 3) if hard else 1.0,
        "evidence_support": evidence_count,
        "chain_breaks": len(validation.get("chain_breaks") or []),
        "low_level_only": low_level_only,
        "cluster_size": int(candidate.get("cluster_size") or 1),
        "pool_size": pool_size,
    }
    scores["total"] = round(
        3.0 * min(level_rank, 4)
        + 2.5 * scores["constraint_ratio"]
        + 1.5 * min(evidence_count, 6) / 6
        - 1.0 * scores["chain_breaks"]
        - 1.0 * (1 if low_level_only else 0),
        3,
    )
    return scores


def select_final_candidates(candidates: list[dict[str, Any]],
                            contract: dict[str, Any],
                            *,
                            select: int = DEFAULT_SELECT) -> tuple[
                                list[dict[str, Any]], list[dict[str, Any]]]:
    """从候选池中选出最终方案：先保证创新等级下限，再按分数排序。"""
    if not candidates:
        return [], []
    floor = clean_str(contract.get("innovation_floor"), "L3").upper()
    floor_rank = INNOVATION_LEVEL_RANK.get(floor, 3)
    qualified = [c for c in candidates
                 if (c.get("_scores") or {}).get("innovation_rank", 0) >= floor_rank]
    others = [c for c in candidates if c not in qualified]
    qualified.sort(key=lambda c: (c.get("_scores") or {}).get("total", 0), reverse=True)
    others.sort(key=lambda c: (c.get("_scores") or {}).get("total", 0), reverse=True)
    selected = qualified[:select]
    if len(selected) < min(select, len(candidates)):
        for candidate in others:
            if len(selected) >= min(select, len(candidates)):
                break
            selected.append(candidate)
    rejected = [c for c in candidates if c not in selected]
    for rank, candidate in enumerate(selected, start=1):
        candidate["rank"] = rank
        candidate["priority"] = "首选" if rank == 1 else (
            "备选" if rank <= 3 else "低优先")
    return selected, rejected


# ------------------------------------------------------------------ 规范化

def normalize_strategy(candidate: Any, index: int,
                       contract: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    chain, validation = normalize_operator_chain(candidate.get("operator_chain"))
    satisfies: list[dict[str, Any]] = []
    raw_satisfies = candidate.get("satisfies_constraints")
    if isinstance(raw_satisfies, dict):
        raw_satisfies = [{"constraint": k, "satisfied": v, "reason": ""}
                         for k, v in raw_satisfies.items()]
    for item in raw_satisfies or []:
        if isinstance(item, str):
            satisfies.append({"constraint": item, "satisfied": False, "reason": ""})
            continue
        if not isinstance(item, dict):
            continue
        satisfies.append({
            "constraint": clean_str(item.get("constraint")),
            "satisfied": _bool(item.get("satisfied")),
            "reason": clean_str(item.get("reason")),
        })
    for constraint in contract.get("hard_constraints") or []:
        if not any(x["constraint"] == constraint for x in satisfies):
            satisfies.append({"constraint": constraint, "satisfied": False,
                              "reason": "候选未显式回应该硬约束"})
    mechanism_evidence = _string_list(candidate.get("mechanism_evidence"), limit=20)
    out = {
        "id": clean_str(candidate.get("id"), f"S-{index:02d}"),
        "title": clean_str(candidate.get("title")),
        "target": clean_str(candidate.get("target")),
        "operator_chain": chain,
        "operator_chain_validation": validation,
        "innovation_level": clean_str(candidate.get("innovation_level"), "").upper()
        or validation.get("declared_level", "L0"),
        "innovation_basis": clean_str(candidate.get("innovation_basis")),
        "differentiation": clean_str(candidate.get("differentiation")),
        "satisfies_constraints": satisfies,
        "components": _string_list(candidate.get("components")),
        "creative_operation": clean_str(candidate.get("creative_operation")),
        "novelty_source": clean_str(candidate.get("novelty_source")),
        "rationale": clean_str(candidate.get("rationale")),
        "mechanism_evidence": mechanism_evidence,
        "pattern_ids": _string_list(candidate.get("pattern_ids")),
        "evidence_ids": _string_list(candidate.get("evidence_ids")),
        "status": clean_str(candidate.get("status"), "hypothesis"),
        "risks": _string_list(candidate.get("risks")),
        "validation_plan": clean_str(candidate.get("validation_plan")),
        "cluster_size": int(candidate.get("cluster_size") or 1),
        "cluster_members": _string_list(candidate.get("cluster_members"), limit=30),
    }
    return out


def render_draft(draft: dict[str, Any]) -> str:
    """把结构化草稿渲染为最终 markdown，保证文本和证据一致。"""
    lines = [f"# {clean_str(draft.get('title'), '未命名研究内容')}"]
    summary = draft.get("summary") or {}
    if clean_str(summary.get("text")):
        lines.append("")
        lines.append(clean_str(summary["text"]))
    for section in draft.get("sections") or []:
        heading = clean_str(section.get("heading"), "研究发现")
        lines += ["", f"## {heading}"]
        for item in section.get("items") or []:
            text = clean_str(item.get("text"))
            if not text:
                continue
            pattern_ids = [str(x) for x in item.get("pattern_ids") or []]
            ids = list(item.get("evidence_ids") or [])
            status = clean_str(item.get("status"), "supported")
            refs = [*pattern_ids, *ids]
            suffix = f" [{', '.join(refs)}]" if refs else ""
            lines.append(f"- [{status}] {text}{suffix}")
    strategies = draft.get("strategies") or []
    if strategies:
        lines += ["", "## 候选方案"]
        contract = draft.get("design_contract") or {}
        if contract:
            floor = clean_str(contract.get("innovation_floor"), "")
            lines.append("")
            lines.append(
                f"创新等级下限：{floor}；候选数量要求：{contract.get('min_candidates')}；"
                f"差异轴：{'、'.join(contract.get('differentiation_axes') or [])}")
        for s in strategies:
            title = clean_str(s.get("title"), "候选")
            level = clean_str(s.get("innovation_level"), "L0")
            rank = s.get("rank")
            priority = clean_str(s.get("priority"), "")
            lines.append("")
            lines.append(
                f"### {f'[{rank}] ' if rank else ''}{title} "
                f"[{clean_str(s.get('status'), 'hypothesis')}] "
                f"创新等级 {level}"
                f"（{INNOVATION_LEVEL_LABEL.get(level, '')}）"
                f"{f' · {priority}' if priority else ''}")
            for key, label in (
                    ("target", "目标"), ("differentiation", "与其余候选的区别"),
                    ("novelty_source", "创新来源"), ("rationale", "理由"),
                    ("validation_plan", "验证计划")):
                value = clean_str(s.get(key))
                if value:
                    lines.append(f"- {label}: {value}")
            chain = s.get("operator_chain") or []
            if chain:
                lines.append("- 算子链:")
                for step in chain:
                    lines.append(
                        f"  - {step.get('operator')}"
                        f"（{step.get('operator_label') or ''}，{step.get('level')}）: "
                        f"{step.get('input') or '?'} → {step.get('output') or '?'}")
            validation = s.get("operator_chain_validation") or {}
            if validation.get("chain_breaks"):
                lines.append(
                    f"- 算子链校验: {len(validation['chain_breaks'])} 处衔接可疑"
                    f"（{validation.get('level_basis') or ''}）")
            elif validation:
                lines.append(f"- 算子链校验: {validation.get('level_basis') or '通过'}")
            satisfies = s.get("satisfies_constraints") or []
            if satisfies:
                lines.append("- 硬约束核对:")
                for item in satisfies:
                    mark = "满足" if item.get("satisfied") else "未满足"
                    reason = clean_str(item.get("reason"))
                    lines.append(
                        f"  - [{mark}] {clean_str(item.get('constraint'))}"
                        f"{f'：{reason}' if reason else ''}")
            comps = [str(x) for x in s.get("components") or []]
            if comps:
                lines.append(f"- 组件: {'; '.join(comps)}")
            refs = [str(x) for x in s.get("pattern_ids") or []]
            refs += [str(x) for x in s.get("evidence_ids") or []]
            refs += [str(x) for x in s.get("mechanism_evidence") or []]
            if refs:
                lines.append(f"- 依据: {', '.join(dict.fromkeys(refs))}")
    responses = draft.get("revision_responses") or []
    if responses:
        lines += ["", "## 修订回应"]
        for item in responses:
            mark = "已解决" if item.get("resolved") else "未解决"
            note = clean_str(item.get("note"))
            lines.append(f"- [{mark}] {clean_str(item.get('action'))}"
                         f"{f'：{note}' if note else ''}")
    return "\n".join(lines)


def normalize_draft(data: dict[str, Any] | None,
                    plan: dict[str, Any],
                    knowledge: dict[str, Any]) -> dict[str, Any]:
    data = data or {}
    contract = build_design_contract(plan)
    title = clean_str(data.get("title"), clean_str(plan.get("goal"), "研究发现"))
    summary = data.get("summary") or {}
    sections = []
    for sec in data.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        items = []
        for item in sec.get("items") or []:
            if not isinstance(item, dict) or not clean_str(item.get("text")):
                continue
            items.append({
                "text": clean_str(item["text"]),
                "pattern_ids": [str(x) for x in item.get("pattern_ids") or []],
                "evidence_ids": [str(x) for x in item.get("evidence_ids") or []],
                "status": clean_str(item.get("status"), "supported"),
            })
        sections.append({
            "heading": clean_str(sec.get("heading"), "研究发现"),
            "items": items,
        })
    raw_candidates = data.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raw_candidates = data.get("strategies") or []
    strategies = []
    for i, item in enumerate(raw_candidates, start=1):
        candidate = normalize_strategy(item, i, contract)
        if candidate:
            strategies.append(candidate)
    responses = []
    for item in data.get("revision_responses") or []:
        if not isinstance(item, dict):
            continue
        responses.append({
            "action": clean_str(item.get("action") or item.get("revision_action")),
            "resolved": _bool(item.get("resolved")),
            "note": clean_str(item.get("note") or item.get("reason")),
        })
    draft = {
        "title": title,
        "summary": {
            "text": clean_str(summary.get("text")),
            "pattern_ids": [str(x) for x in summary.get("pattern_ids") or []],
            "evidence_ids": [str(x) for x in summary.get("evidence_ids") or []],
        },
        "sections": sections,
        "strategies": strategies,
        "design_contract": contract,
        "revision_responses": responses,
        "candidate_pool": {
            "generated": len(strategies),
            "duplicates": [],
            "selected": 0,
            "rejected": 0,
        },
        "_knowledge_stats": {
            "patterns": len(knowledge.get("patterns") or []),
            "evidence": len(knowledge.get("evidence") or []),
            "hyperedges": len(knowledge.get("hyperedges") or []),
            "mechanism_states": len((knowledge.get("design_context") or {})
                                    .get("mechanism_states") or []),
            "operator_candidates": len((knowledge.get("design_context") or {})
                                       .get("operator_candidates") or []),
        },
    }
    draft["markdown"] = render_draft(draft)
    return draft


def _score_and_select(draft: dict[str, Any], contract: dict[str, Any],
                      select: int) -> dict[str, Any]:
    """对规范化后的候选池做去重、评分、排序与最终选择。"""
    candidates = draft.get("strategies") or []
    pool_size = len(candidates)
    deduped, duplicates = dedupe_candidates(candidates)
    for candidate in deduped:
        candidate["_scores"] = score_candidate(candidate, contract, pool_size)
    selected, rejected = select_final_candidates(deduped, contract, select=select)
    for candidate in selected + rejected:
        candidate.pop("_scores", None)
    draft["strategies"] = selected + [
        {**c, "rank": None, "priority": "不建议优先实施"} for c in rejected]
    draft["candidate_pool"] = {
        "generated": pool_size,
        "after_dedupe": len(deduped),
        "duplicates": duplicates,
        "selected": len(selected),
        "rejected": len(rejected),
        "select_target": select,
    }
    draft["selection"] = {
        "first_choice": clean_str((selected[0] or {}).get("title"), "")
        if selected else "",
        "alternatives": [clean_str(c.get("title"), "") for c in selected[1:]],
        "not_recommended": [clean_str(c.get("title"), "") for c in rejected],
        "innovation_floor": clean_str(contract.get("innovation_floor"), "L3"),
        "floor_met": [
            clean_str(c.get("title"), "") for c in selected
            if INNOVATION_LEVEL_RANK.get(
                clean_str(c.get("innovation_level"), "L0").upper(), 0)
            >= INNOVATION_LEVEL_RANK.get(
                clean_str(contract.get("innovation_floor"), "L3").upper(), 3)
        ],
    }
    return draft


def deterministic_strategies(plan: dict[str, Any],
                             knowledge: dict[str, Any],
                             limit: int = DEFAULT_SELECT) -> list[dict[str, Any]]:
    """无模型时的兜底候选：优先使用 design_context 的机制状态与算子链。"""
    design = knowledge.get("design_context") or {}
    operators = design.get("operator_candidates") or []
    states = design.get("mechanism_states") or []
    gaps = design.get("opportunity_gaps") or []
    contract = build_design_contract(plan)
    out: list[dict[str, Any]] = []
    for i, op in enumerate(operators[:limit], start=1):
        chain = op.get("operator_chain") or []
        refs = list(op.get("evidence_ids") or []) + list(op.get("hyperedge_ids") or [])
        out.append({
            "id": f"S-{i:02d}",
            "title": f"{clean_str(op.get('target'), '目标')} → "
                     f"{'+'.join(str(s.get('operator')) for s in chain)}",
            "target": clean_str(op.get("target")),
            "operator_chain": chain,
            "innovation_basis": (op.get("operator_chain_validation") or {}).get(
                "level_basis", ""),
            "differentiation": f"算子序列：{'+'.join(str(s.get('operator')) for s in chain)}",
            "satisfies_constraints": [
                {"constraint": constraint, "satisfied": True,
                 "reason": "由 design_context 机制状态归纳，待实验确认"}
                for constraint in contract.get("hard_constraints") or []
            ],
            "components": [str(s.get("operator")) for s in chain],
            "novelty_source": clean_str(op.get("name")),
            "rationale": "由已抽取机制状态与缺口归纳的算子链，逐步可验证",
            "mechanism_evidence": [x for x in refs if x.startswith(("MS-", "GAP-"))][:6],
            "pattern_ids": [x for x in refs if x.startswith("P-")][:10],
            "evidence_ids": [x for x in refs if x.startswith(("E-", "H-"))][:10],
            "status": "hypothesis",
            "risks": ["算子链整体尚无直接实验证据"],
            "validation_plan": "按算子链首步做最小验证实验",
        })
    if not out:
        for i, state in enumerate(states[:limit], start=1):
            refs = list(state.get("evidence_ids") or [])
            out.append({
                "id": f"S-{i:02d}",
                "title": clean_str(state.get("label"), f"机制候选 {i}"),
                "target": clean_str(state.get("start_state"), ""),
                "operator_chain": [],
                "innovation_basis": "仅能确认机制状态，未能构造算子链",
                "differentiation": clean_str(state.get("intermediate"), ""),
                "satisfies_constraints": [],
                "components": [],
                "novelty_source": clean_str(state.get("intermediate"), ""),
                "rationale": "机制状态来自真实超边与证据句",
                "mechanism_evidence": [clean_str(state.get("state_id"), "")],
                "pattern_ids": [x for x in refs if x.startswith("P-")][:10],
                "evidence_ids": [x for x in refs if x.startswith("E-")][:10],
                "status": "hypothesis",
                "risks": ["缺少算子链，尚未形成可执行方案"],
                "validation_plan": "先补齐算子链再设计实验",
            })
    if not out:
        for i, gap in enumerate(gaps[:limit], start=1):
            out.append({
                "id": f"S-{i:02d}",
                "title": f"缺口驱动候选：{clean_str(gap.get('missing_link'))[:40]}",
                "target": clean_str(gap.get("unmet_target"), ""),
                "operator_chain": [],
                "innovation_basis": "来自机会缺口，尚未落到算子链",
                "differentiation": clean_str(gap.get("gap_type"), ""),
                "satisfies_constraints": [],
                "components": [],
                "novelty_source": clean_str(gap.get("known_mechanism"), ""),
                "rationale": "机会缺口由真实证据推出",
                "mechanism_evidence": [clean_str(gap.get("gap_id"), "")],
                "pattern_ids": [],
                "evidence_ids": [x for x in (gap.get("evidence_ids") or [])][:10],
                "status": "hypothesis",
                "risks": [clean_str(gap.get("risk"), "")],
                "validation_plan": "先补检缺失环节，再构造算子链",
            })
    if not out:
        # 最后兜底：没有机制素材时，用最低层级算子显式生成 L1/L2 候选。
        # 这些候选会被设计契约检查判为不达标，正是要暴露的失败状态，
        # 而不是像旧版本那样让它们静默通过审核。
        low_ops = ["combine", "substitute", "migrate", "extend"]
        target = clean_str(contract.get("objective"), clean_str(plan.get("goal"), "目标"))
        for i, operator in enumerate(low_ops, start=1):
            chain, validation = normalize_operator_chain([{
                "operator": operator,
                "input": f"{target} 的已有做法",
                "output": f"{target} 的组合/迁移变体",
            }])
            out.append({
                "id": f"S-{i:02d}",
                "title": f"{LOW_OPERATOR_LABELS.get(operator, operator)}候选 {i}",
                "target": target,
                "operator_chain": chain,
                "operator_chain_validation": validation,
                "innovation_basis": validation.get("level_basis", ""),
                "differentiation": f"低阶算子：{operator}",
                "satisfies_constraints": [
                    {"constraint": constraint, "satisfied": False,
                     "reason": "缺少机制素材，无法确认是否满足"}
                    for constraint in contract.get("hard_constraints") or []
                ],
                "components": [operator],
                "novelty_source": "仅由低阶算子构成，机制未改写",
                "rationale": "知识包中没有可用的机制状态与算子链，只能给出低阶候选",
                "mechanism_evidence": [],
                "pattern_ids": [],
                "evidence_ids": [],
                "status": "hypothesis",
                "risks": ["停留在 L1–L2，未达到创新等级下限",
                          "缺少机制层证据支撑"],
                "validation_plan": "先补齐机制状态抽取，再构造算子链",
            })
    return out


def deterministic_draft(plan: dict[str, Any],
                        knowledge: dict[str, Any],
                        model: Any = None) -> dict[str, Any]:
    """无模型时的确定性成稿：按模式卡转写，不新增论点。

    model 提供时，仅对最终入选的 4–6 个候选调用一次"候选深化"，
    用于补齐风险、验证计划与硬约束回应（骨架仍由代码生成，机制不被改写）。
    """
    patterns = knowledge.get("patterns") or []
    title = clean_str(plan.get("goal"), "研究发现")
    items: list[dict[str, Any]] = []
    for p in patterns[:60]:
        if p.get("relation_type") == "related_to":
            status = "hypothesis"
        elif p.get("evidence_tier") in ("review", "commentary", "unclassified"):
            status = "hypothesis"
        else:
            status = "supported"
        items.append({
            "text": (
                f"{p.get('source_name', '')} 通过 "
                f"{p.get('relation_type', '')} 作用于 "
                f"{p.get('target_name', '')}，"
                f"支持来源 {p.get('support_count', 0)} 篇。"
            ),
            "pattern_ids": [p["pattern_id"]],
            "evidence_ids": p.get("evidence_ids") or [],
            "status": status,
        })
    sections = [{
        "heading": "由高可信本体证据归纳的主要模式",
        "items": items,
    }]
    summary_text = (
        f"基于 {len(patterns)} 个模式卡、"
        f"{len(knowledge.get('evidence') or [])} 条可溯源证据和 "
        f"{len(knowledge.get('hyperedges') or [])} 条科研超边生成。"
    )
    strategies = deterministic_strategies(plan, knowledge)
    draft_data = {
        "title": title,
        "summary": {"text": summary_text},
        "sections": sections,
    }
    if strategies:
        draft_data["candidates"] = strategies
    draft = normalize_draft(draft_data, plan, knowledge)
    contract = build_design_contract(plan)
    draft = _score_and_select(draft, contract, DEFAULT_SELECT)
    if model is not None and plan.get("task_kind") == "generative":
        # 只对入选候选做一次深化，把风险/验证计划/硬约束回应补全；
        # 失败时保留代码生成的骨架，不影响主流程。
        draft = _expand_selected(model, draft, contract, plan)
        draft["markdown"] = render_draft(draft)
    return draft


def _compact_knowledge(knowledge: dict[str, Any],
                       max_patterns: int = 14,
                       max_evidence: int = 16,
                       max_hyperedges: int = 24,
                       max_chars: int = 55000) -> dict[str, Any]:
    """把知识包压缩到内容生成所需的最小集合，并透传设计上下文。

    实测（384 篇语料）：不做上限控制时提示词可达 9.1 万字符（≈3 万 tokens），
    真实模型在这种规模下容易出现超长响应甚至长时间无响应。因此这里做**硬预算**：
    - 模式卡/证据只保留排序最前的少量（内容生成主要靠 design_context 的机制素材）；
    - 超边只保留 reference_id / label / 成员名（条件与证据在消费阶段已消化）；
    - 超出预算时按块削减，并记录 `_prompt_chars` / `_truncated`。
    """
    design_context = knowledge.get("design_context") or {}
    consumer_analysis = knowledge.get("consumer_analysis") or {}

    def build(patterns: int, evidence: int, hyperedges: int,
              mechanism: int, gaps: int, operators: int) -> dict[str, Any]:
        return {
            "corpus": {k: v for k, v in (knowledge.get("corpus") or {}).items()
                       if k != "paper_keys"},
            "patterns": (knowledge.get("patterns") or [])[:patterns],
            "evidence": (knowledge.get("evidence") or [])[:evidence],
            "hyperedges": [{
                "reference_id": h.get("reference_id"),
                "type": h.get("hyperedge_type"),
                "label": clean_str(h.get("label"), "")[:160],
                "members": (h.get("member_names") or [])[:4],
                "evidence_ids": (h.get("evidence_ids") or [])[:3],
            } for h in (knowledge.get("hyperedges") or [])[:hyperedges]],
            "coverage_score": knowledge.get("coverage_score", 0),
            "consumer_analysis": {
                "summary": clean_str(consumer_analysis.get("summary"), "")[:800],
                "mechanism_clusters": (consumer_analysis.get(
                    "mechanism_clusters") or [])[:6],
                "known_conflicts": (consumer_analysis.get(
                    "known_conflicts") or [])[:6],
                "evidence_gaps": (consumer_analysis.get("evidence_gaps") or [])[:8],
                "confidence": consumer_analysis.get("confidence"),
            },
            "design_context": {
                "mechanism_states": (design_context.get("mechanism_states")
                                     or [])[:mechanism],
                "reaction_primitives": (design_context.get("reaction_primitives")
                                        or [])[:mechanism],
                "opportunity_gaps": (design_context.get("opportunity_gaps")
                                     or [])[:gaps],
                "operator_candidates": (design_context.get("operator_candidates")
                                        or [])[:operators],
                "constraint_conflicts": (design_context.get("constraint_conflicts")
                                         or [])[:gaps],
                "traceability": design_context.get("traceability"),
            },
        }

    compact = build(max_patterns, max_evidence, max_hyperedges, 12, 8, 8)
    text = json.dumps(compact, ensure_ascii=False)
    steps = [
        lambda c: build(10, 12, 18, 10, 6, 6),
        lambda c: build(8, 8, 12, 8, 5, 4),
        lambda c: build(6, 6, 8, 6, 4, 4),
        lambda c: build(4, 4, 6, 4, 3, 3),
        lambda c: build(3, 3, 4, 3, 2, 2),
    ]
    index = 0
    while len(text) > max_chars and index < len(steps):
        compact = steps[index](compact)
        text = json.dumps(compact, ensure_ascii=False)
        index += 1
    compact["_prompt_chars"] = len(text)
    compact["_truncated"] = len(text) > max_chars
    return compact


def _revision_actions(state: dict[str, Any]) -> list[str]:
    """合并审核意见与事实核查意见，供内容节点定向修订。"""
    review = state.get("review") or {}
    actions: list[str] = []
    for item in review.get("revision_actions") or []:
        text = clean_str(item)
        if text and text not in actions:
            actions.append(text)
    for issue in review.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        text = clean_str(issue.get("problem"))
        if text and text not in actions:
            actions.append(text)
    for item in (review.get("instruction_compliance") or {}).get("requirements") or []:
        if not isinstance(item, dict) or item.get("status") == "met":
            continue
        text = clean_str(item.get("revision_action") or item.get("reason"))
        if text and text not in actions:
            actions.append(text)
    fact_check = state.get("fact_check") or {}
    for issue in fact_check.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        action = clean_str(issue.get("revision_action") or issue.get("problem"))
        if not action:
            continue
        location = clean_str(issue.get("location"), "")
        text = f"[事实核查/{clean_str(issue.get('type'), 'issue')}]" \
               f"{f'{location}：' if location else ''}{action}"
        if text not in actions:
            actions.append(text)
    return actions[:12]


def _expand_selected(model: Any, draft: dict[str, Any], contract: dict[str, Any],
                     plan: dict[str, Any],
                     timeout: int = 420) -> dict[str, Any]:
    """阶段 2：只对入选候选补全细节，把单次输出的长度控制在可控范围。"""
    selected = [s for s in (draft.get("strategies") or []) if s.get("rank")]
    if not selected:
        return draft
    payload = [{
        "id": s.get("id"),
        "title": s.get("title"),
        "target": s.get("target"),
        "operator_chain": s.get("operator_chain"),
        "innovation_level": s.get("innovation_level"),
        "innovation_basis": s.get("innovation_basis"),
        "differentiation": s.get("differentiation"),
    } for s in selected]
    prompt = EXPAND_PROMPT.replace(
        "{count}", str(len(payload))
    ).replace(
        "{contract}", json.dumps(contract, ensure_ascii=False, indent=2)
    ).replace(
        "{candidates}", json.dumps(payload, ensure_ascii=False, indent=2))
    try:
        raw, call_diag = invoke_with_timeout(model, prompt, timeout=timeout)
        if raw is None:
            raise RuntimeError(call_diag.get("error") or "模型调用失败")
        parsed = parse_json_object(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("候选深化调用失败，保留骨架候选: %s", exc)
        draft["expand_error"] = str(exc)
        return draft
    raw = parsed.get("candidates") if isinstance(parsed, dict) else None
    if not isinstance(raw, list) or not raw:
        return draft
    by_id = {}
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        normalized = normalize_strategy(item, i, contract)
        if normalized:
            by_id[normalized["id"]] = normalized
            by_id.setdefault(clean_str(item.get("title"), ""), normalized)
    merged = 0
    for candidate in selected:
        enriched = by_id.get(candidate.get("id")) or by_id.get(
            candidate.get("title"))
        if not enriched:
            continue
        # 算子链以阶段 1 为准，不允许深化步骤改写机制
        enriched["operator_chain"] = candidate.get("operator_chain")
        enriched["operator_chain_validation"] = candidate.get(
            "operator_chain_validation")
        for key in ("id", "rank", "priority", "cluster_size", "cluster_members"):
            enriched[key] = candidate.get(key)
        candidate.update(enriched)
        merged += 1
    draft["expanded_candidates"] = merged
    return draft


def make_content_node(model=None,
                      conn: sqlite3.Connection | None = None,
                      settings: Settings | None = None):
    """构造 LangGraph 内容形成节点。model 为 None 时做确定性成稿。"""
    settings = settings or default_settings

    def content_node(state: dict) -> dict:
        plan = state.get("plan") or {}
        knowledge = state.get("knowledge") or {}
        run_id = state.get("run_id")
        retrieval = plan.get("retrieval") or {}
        contract = build_design_contract(plan)
        budget = plan.get("budget") or {}
        pool_size = max(contract["min_candidates"],
                        int(budget.get("max_candidates") or DEFAULT_POOL_SIZE))
        select = max(1, min(6, contract["min_candidates"] + 2))
        max_gaps = 20
        revision_actions = _revision_actions(state)
        revision_round = int(state.get("review_rounds") or 0)
        log_study_event(conn, settings, "content_builder", run_id, "running",
                        {"revision_actions": len(revision_actions),
                         "revision_round": revision_round})
        draft = None
        model_error = None
        edge_gaps: list[dict[str, Any]] = []
        if model is not None:
            prompt = CONTENT_PROMPT.replace(
                "{plan}", json.dumps(plan, ensure_ascii=False, indent=2)
            ).replace(
                "{knowledge}",
                json.dumps(_compact_knowledge(knowledge),
                           ensure_ascii=False, indent=2),
            )
            if plan.get("task_kind") == "generative":
                floor = clean_str(contract.get("innovation_floor"), "L3")
                floor_rank = INNOVATION_LEVEL_RANK.get(floor.upper(), 3)
                min_mechanistic = max(
                    2, int(contract["min_candidates"] // 2)
                    if floor_rank >= 3 else 0)
                seed_target = max(contract["min_candidates"],
                                  min(pool_size, MAX_SEED_CANDIDATES))
                prompt += GENERATIVE_BLOCK.replace(
                    "{pool_size}", str(seed_target)
                ).replace(
                    "{min_mechanistic}", str(min_mechanistic)
                ).replace(
                    "{innovation_floor}", floor
                ).replace(
                    "{operators}", operator_catalog_for_prompt())
            if revision_actions:
                prompt += REVISION_BLOCK.replace(
                    "{revision_actions}",
                    json.dumps(revision_actions, ensure_ascii=False, indent=2))
            elif plan.get("task_kind") != "generative":
                # 综述/前沿类任务：给出小节结构、段落粒度与篇幅要求。
                # （生成型任务走 GENERATIVE_BLOCK，不要同时注入两套结构约束）
                target_chars = int(budget.get("review_target_chars")
                                   or DEFAULT_REVIEW_CHARS)
                prompt += REVIEW_BLOCK.replace(
                    "{task_kind}", clean_str(plan.get("task_kind"), "summary")
                ).replace("{target_chars}", str(target_chars))
            try:
                raw, call_diag = invoke_with_timeout(
                    model, prompt,
                    timeout=getattr(settings, "study_content_timeout", 420))
                if raw is None:
                    raise RuntimeError(call_diag.get("error") or "模型调用失败")
                parsed = parse_json_object(raw)
                draft = normalize_draft(parsed, plan, knowledge)
                draft = _score_and_select(draft, contract, select)
                if plan.get("task_kind") == "generative":
                    draft = _expand_selected(model, draft, contract, plan,
                                             timeout=getattr(
                                                 settings, "study_content_timeout",
                                                 420))
                if revision_actions:
                    draft = _ensure_revision_responses(draft, revision_actions)
                draft["model_call"] = call_diag
                edge_gaps = normalize_edge_gaps(
                    parsed.get("edge_gaps") if isinstance(parsed, dict) else [],
                    max_gaps,
                )
                known_pattern_ids = {
                    str(p.get("pattern_id") or "")
                    for p in knowledge.get("patterns") or []
                    if str(p.get("pattern_id") or "")
                }
                edge_gaps = [
                    gap for gap in edge_gaps
                    if gap.get("pattern_id") in known_pattern_ids
                ]
            except Exception as exc:  # noqa: BLE001
                logger.warning("内容节点 LLM 调用失败，回退确定性转写: %s", exc)
                model_error = str(exc)
        if draft is None:
            draft = deterministic_draft(plan, knowledge, model=model)
            # 骨架由代码生成：即使深化成功，机制也不是模型拟定的，
            # 因此明确标记来源，供审核节点决定是否值得继续修订。
            draft["generated_by"] = (
                "deterministic_skeleton_expanded"
                if draft.get("expanded_candidates")
                else "deterministic_fallback")
            if revision_actions:
                draft = _ensure_revision_responses(draft, revision_actions)
        if not edge_gaps:
            edge_gaps = select_low_support_gaps(
                knowledge.get("patterns") or [],
                min_support=max(2, int(retrieval.get("min_support_target") or 2)),
                limit=max_gaps,
            )
        if model_error:
            draft["model_error"] = model_error
        pool = draft.get("candidate_pool") or {}
        log_study_event(
            conn, settings, "content_builder", run_id, "done",
            {
                "title": draft.get("title"),
                "sections": len(draft.get("sections") or []),
                "candidates_generated": pool.get("generated"),
                "candidates_after_dedupe": pool.get("after_dedupe"),
                "candidates_selected": pool.get("selected"),
                "selected_levels": [
                    clean_str(s.get("innovation_level"), "L0")
                    for s in (draft.get("strategies") or [])
                    if s.get("rank")
                ],
                "edge_gaps": len(edge_gaps),
                "markdown_chars": len(draft.get("markdown") or ""),
            })
        return {"draft": draft, "edge_gaps": edge_gaps, "status": "drafted"}

    return content_node


def _ensure_revision_responses(draft: dict[str, Any],
                               revision_actions: list[str]) -> dict[str, Any]:
    """保证审核意见被逐条回应；未回应者在代码层补记为未解决。"""
    existing = {clean_str(x.get("action")): x
                for x in draft.get("revision_responses") or []}
    out = list(existing.values())
    for action in revision_actions:
        if action in existing:
            continue
        out.append({
            "action": action,
            "resolved": False,
            "note": "模型未给出回应，代码层标记为未解决，需人工确认",
        })
    draft["revision_responses"] = out[:20]
    draft["revision_consumed"] = len(revision_actions)
    draft["markdown"] = render_draft(draft)
    return draft
