"""工作规划节点。

职责边界：只把用户的模糊请求规范化为“语料采集任务”和内容交付要求，
不在这一阶段预设综述章节或研究结论。真正的章节结构由内容形成节点在
获得知识消费结果后按数据形态自然生成。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date
from typing import Any

from langchain_core.messages import HumanMessage

from research_agent import packs
from research_agent.config import Settings, settings as default_settings
from research_agent.domains import normalize_domain_profile
from research_agent.retrieval.skills import (
    normalize_retrieval,
)
from research_agent.study.events import log_study_event
from research_agent.study.json_utils import clean_str, parse_json_object

logger = logging.getLogger(__name__)

PLANNER_PROMPT = """你是科研辅助系统的工作规划节点，也是整个研究任务的统一入口。
你必须先把用户请求转换成可执行的研究任务单，后续检索、质量评估、知识提取、
知识消费、内容形成和审核都只能依据这份任务单工作。

硬性要求：
1. 不要生成内容大纲，不要预设章节，不要预判研究结论；
2. 判断任务性质：summary(综述/调研)、generative(提出新方法/新方案/新设计)、
   frontier(前沿探索)、evaluation(评估/比较/选择)、proof(证明)；
3. 必须生成 retrieval_plan，明确检索什么、为什么检索、覆盖哪些维度、
   使用哪些来源、时间范围和停止条件；用户原话不能未经规划直接作为检索词；
4. 必须生成 analysis_plan 和 evidence_policy，统一约束后续节点的分析维度和证据标准；
5. 对 generative 任务必须生成 design_contract，明确目标对象、目标结构硬约束、
   创新等级下限、候选数量、差异轴、评价标准和限制；
6. creative_contract 仅作兼容字段，可以保留，但不能把组合/替换/迁移当作核心创新；
7. seed_terms 使用能直接投递到目标文献库的检索词：国际学术库用英文，
   NCPSSD/CNKI 等中文库用中文；禁止把用户整句话直接作为 seed_terms 或 domain；
8. content_type 从 research_report/frontier_review/research_directions/experiment_protocol 中选择；
9. retrieval 字段保留现有检索策略兼容；默认 broad，若用户要求补强证据缺口则启用
   evidence_gap，仅在明确要求单领域深挖时启用 deep_single_domain；
10. 只输出 JSON 对象，不要代码块，不要解释。

示例（只参考字段风格，不要照抄用户原话作为 domain/seed_terms）：
用户原话：尝试提出一种炔酰胺构建多元氮杂化合物的新方法
合理 seed_terms 示例：["ynamide annulation", "ynamide nitrogen heterocycle synthesis",
"alkynyl amide cyclization", "ynamide catalytic cycloaddition"]

当前日期：{today}

用户原话：
{request}

输出 JSON 结构：
{{
  "goal": "一句话目标",
  "domain": "研究领域",
  "content_type": "research_report|frontier_review|research_directions|experiment_protocol",
  "task_kind": "summary|generative|frontier|evaluation|proof",
  "analysis_plan": {{
    "dimensions": ["机制", "底物范围", "选择性", "条件兼容性", "反例"],
    "required_comparisons": [],
    "open_questions": []
  }},
  "evidence_policy": {{
    "traceability_required": true,
    "hypothesis_label_required": true,
    "minimum_support": 2,
    "allow_evidence_gap_retrieval": true
  }},
  "design_contract": {{
    "objective": "用户期望获得的新对象/新方案描述",
    "target_constraints": {{"hard_constraints": [], "must_explain": []}},
    "innovation_floor": "L3",
    "min_candidates": 4,
    "differentiation_axes": ["mechanism", "intermediate", "selectivity", "operator_chain"],
    "evaluation_criteria": ["目标匹配", "创新等级", "机制可行性", "证据支持", "验证成本"],
    "constraints": ["不能只复述已有方案", "必须区分假设与已知事实"]
  }},
  "creative_contract": {{
    "objective": "兼容旧内容节点的生成目标",
    "focus": "研究或设计焦点",
    "min_candidates": 4,
    "creative_operations": [],
    "constraints": ["不能只复述已有方案", "必须区分假设与已知事实"],
    "evaluation_criteria": ["新颖性", "可行性", "可解释性", "可验证性"]
  }},
  "domain_profile": {{
    "domain_kind": "chemistry|biomedicine|materials|humanities_social_science|general",
    "dimensions": ["该领域应覆盖的检索/分析维度"],
    "candidate_entity_types": ["首轮可试用的实体类型"],
    "candidate_relation_types": ["首轮可试用的关系类型"],
    "schema_status": "candidate"
  }},
  "analysis_targets": ["方法", "材料", "性能指标", "应用", "开放问题"],
  "mission": {{
    "seed_terms": ["英文检索词1", "英文检索词2", "英文检索词3"],
    "max_results": 80,
    "min_confidence": 0.6,
    "collection_mode": "broad",
    "recency_window": "2018-01-01:{today}"
  }},
  "retrieval_plan": {{
    "objective": "需要收集什么证据",
    "query_variants": ["实际可投递检索词"],
    "source_mix": ["europepmc", "arxiv", "semantic_scholar"],
    "dimensions": ["mechanism", "substrate_scope", "selectivity"],
    "recency_window": "2018-01-01:{today}",
    "max_results_per_query": 20,
    "min_quality": 0.6,
    "must_cover": ["目标骨架", "关键中间体", "反例"],
    "stop_conditions": ["核心机制至少两条独立证据", "主要路线覆盖达到阈值"]
  }},
  "retrieval": {{
    "strategy": "broad|evidence_gap|deep_single_domain",
    "evidence_gap_enabled": false,
    "deep_single_domain_enabled": false,
    "min_support_target": 2,
    "max_skill_rounds": 2,
    "relevance_gate": "strict",
    "reference_direction": "both"
  }},
  "instruction_contract": {{
    "task_kind": "summary|generative|frontier|evaluation|proof",
    "deliverable_format": "markdown|docx|pdf|text",
    "language": "zh",
    "required_method_count": 0,
    "required_sections": [],
    "must_include": [],
    "must_exclude": [],
    "reference_style": "ACS|numeric|none",
    "evidence_policy": "每条实质断言可溯源",
    "correctness_threshold": 0.85
  }},
  "stop_conditions": ["检索达到覆盖要求", "新增证据不再改变主要结论"],
  "budget": {{"max_collection_rounds": 2, "max_total_results": 160}},
  "deliverable": {{
    "format": "markdown",
    "sections_policy": "emergent",
    "language": "zh"
  }},
  "constraints": []
}}

请直接输出可解析的 JSON："""




def infer_deliverable_format(request: str) -> str:
    text = (request or "").lower()
    if "docx" in text or "word" in text or "文档" in text:
        return "docx"
    if "pdf" in text:
        return "pdf"
    if "markdown" in text or "md" in text:
        return "markdown"
    return "markdown"


def normalize_instruction_contract(
        data: dict[str, Any] | None, request: str, task_kind: str,
        creative_contract: dict[str, Any] | None = None,
        deliverable: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = data or {}
    deliverable = deliverable or {}
    creative_contract = creative_contract or {}
    required_count = raw.get("required_method_count")
    if required_count is None and task_kind == "generative":
        required_count = creative_contract.get("min_candidates") or 0
    try:
        required_count = max(0, int(required_count or 0))
    except (TypeError, ValueError):
        required_count = 0
    reference_style = clean_str(raw.get("reference_style"), "")
    if not reference_style and "acs" in request.lower():
        reference_style = "ACS"
    threshold = raw.get("correctness_threshold", 0.85)
    try:
        threshold = min(1.0, max(0.0, float(threshold)))
    except (TypeError, ValueError):
        threshold = 0.85
    return {
        "task_kind": clean_str(raw.get("task_kind"), task_kind),
        "deliverable_format": clean_str(
            raw.get("deliverable_format"),
            deliverable.get("format") or infer_deliverable_format(request)),
        "language": clean_str(raw.get("language"), deliverable.get("language") or "zh"),
        "required_method_count": required_count,
        "required_sections": [str(x) for x in raw.get("required_sections") or [] if str(x).strip()],
        "must_include": [str(x) for x in raw.get("must_include") or [] if str(x).strip()],
        "must_exclude": [str(x) for x in raw.get("must_exclude") or [] if str(x).strip()],
        "reference_style": reference_style,
        "evidence_policy": clean_str(raw.get("evidence_policy"), "每条实质断言可溯源"),
        "correctness_threshold": threshold,
    }


def _task_hints() -> dict[str, list[str]]:
    """任务类型/内容类型判定关键词（来自 packs/skills/task-kind-hints）。

    支持两种包结构：顶层直接是关键词数组，或内容类型集中在 ``content_type`` 下。
    """
    data = packs.skill_data("task-kind-hints")
    if not data:
        packs.warn_once("task-hints-missing",
                        "未加载 packs/skills/task-kind-hints；任务类型将统一回落到 generative")
    out: dict[str, list[str]] = {}
    for key, value in data.items():
        if isinstance(value, list):
            out[key] = [str(x) for x in value]
        elif isinstance(value, dict) and key == "content_type":
            for sub, items in value.items():
                if isinstance(items, list):
                    out[str(sub)] = [str(x) for x in items]
    return out


def infer_content_type(request: str) -> str:
    text = request.lower()
    hints = _task_hints()
    # 顺序敏感：frontier_review 与 research_directions 的词有重叠，
    # 先判定更"体裁明确"的综述/实验，再落到研究方向。
    for content_type in ("frontier_review", "experiment_protocol",
                         "research_directions"):
        if any(k in text for k in hints.get(content_type) or []):
            return content_type
    return "research_report"

def infer_task_kind(request: str) -> str:
    text = request.lower()
    hints = _task_hints()
    for kind in ("generative", "evaluation", "frontier", "summary"):
        if any(str(k).lower() in text for k in hints.get(kind) or []):
            return kind
    return "generative"


def _string_list(value: Any, limit: int = 50) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [clean_str(item) for item in value if clean_str(item)]
    return list(dict.fromkeys(out))[:limit]


def _int_value(value: Any, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _float_value(value: Any, default: float, minimum: float = 0.0,
                 maximum: float = 1.0) -> float:
    try:
        return min(maximum, max(minimum, float(value)))
    except (TypeError, ValueError):
        return default


def normalize_creative_contract(data: dict[str, Any] | None,
                                request: str,
                                domain: str,
                                design_contract: dict[str, Any] | None = None) -> dict[str, Any]:
    """领域无关的生成任务契约，保留旧 Content/Reviewer 兼容字段。"""
    raw = data or {}
    design = design_contract or {}
    objective = clean_str(raw.get("objective"), clean_str(
        design.get("objective"), request))
    focus = clean_str(raw.get("focus"), domain or objective)
    operations = _string_list(raw.get("creative_operations"), limit=20)
    if not operations:
        operations = [
            "组合已有方案/方法",
            "把已有方法迁移到新的对象或场景",
            "替换或改造成分/组件/条件",
            "扩展原有方案到更一般情形",
            "设计新的顺序或流水线",
        ]
    criteria = _string_list(
        raw.get("evaluation_criteria") or design.get("evaluation_criteria"),
        limit=30,
    )
    if not criteria:
        criteria = ["新颖性", "可行性", "可解释性", "可验证性"]
    constraints = _string_list(
        raw.get("constraints") or design.get("constraints"), limit=30)
    if not constraints:
        constraints = [
            "不能只复述已有方案",
            "组合的每一部分应可追溯",
            "整体候选应标记为待验证假设",
        ]
    return {
        "objective": objective,
        "focus": focus,
        "min_candidates": _int_value(
            raw.get("min_candidates") or design.get("min_candidates"), 4, 1),
        "innovation_floor": clean_str(
            raw.get("innovation_floor") or design.get("innovation_floor"), "L3"),
        "target_constraints": (
            raw.get("target_constraints") or design.get("target_constraints") or {}),
        "differentiation_axes": _string_list(
            raw.get("differentiation_axes") or design.get("differentiation_axes"),
            limit=20,
        ),
        "creative_operations": operations,
        "constraints": constraints,
        "evaluation_criteria": criteria,
    }


def normalize_analysis_plan(data: dict[str, Any] | None,
                            fallback_targets: list[str] | None = None) -> dict[str, Any]:
    raw = data or {}
    dimensions = _string_list(raw.get("dimensions") or fallback_targets, limit=30)
    return {
        "dimensions": dimensions,
        "required_comparisons": _string_list(
            raw.get("required_comparisons"), limit=30),
        "open_questions": _string_list(raw.get("open_questions"), limit=30),
    }


def normalize_evidence_policy(data: dict[str, Any] | None) -> dict[str, Any]:
    raw = data or {}
    return {
        "traceability_required": bool(raw.get("traceability_required", True)),
        "hypothesis_label_required": bool(
            raw.get("hypothesis_label_required", True)),
        "minimum_support": _int_value(raw.get("minimum_support"), 2, 1),
        "allow_evidence_gap_retrieval": bool(
            raw.get("allow_evidence_gap_retrieval", True)),
    }


def normalize_retrieval_plan(data: dict[str, Any] | None,
                             mission: dict[str, Any],
                             retrieval: dict[str, Any],
                             analysis: list[str],
                             domain: str,
                             today: str) -> dict[str, Any]:
    raw = data or {}
    query_variants = _string_list(
        raw.get("query_variants") or mission.get("seed_terms"), limit=30)
    if not query_variants:
        query_variants = [domain]
    dimensions = _string_list(
        raw.get("dimensions") or analysis, limit=30)
    stop_conditions = raw.get("stop_conditions") or [
        "核心机制至少获得两条独立证据",
        "新增检索不再改变主要结论",
    ]
    if not isinstance(stop_conditions, list):
        stop_conditions = [stop_conditions]
    return {
        "objective": clean_str(raw.get("objective"), "收集完成任务所需的可追溯证据"),
        "query_variants": query_variants,
        "source_mix": _string_list(raw.get("source_mix"), limit=12),
        "dimensions": dimensions,
        "recency_window": clean_str(
            raw.get("recency_window"), mission.get("recency_window")
            or f"2018-01-01:{today}"),
        "max_results_per_query": _int_value(
            raw.get("max_results_per_query") or mission.get("max_results"), 20, 1),
        "min_quality": _float_value(
            raw.get("min_quality"), float(mission.get("min_confidence") or 0.6)),
        "must_cover": _string_list(raw.get("must_cover"), limit=30),
        "stop_conditions": stop_conditions,
        "strategy": clean_str(raw.get("strategy"), retrieval.get("strategy")
                              or "broad"),
    }


def normalize_design_contract(data: dict[str, Any] | None,
                              task_kind: str,
                              request: str,
                              domain: str,
                              creative_contract: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if task_kind != "generative":
        return None
    raw = data or {}
    creative = creative_contract or {}
    target_constraints = raw.get("target_constraints") or {}
    if isinstance(target_constraints, list):
        target_constraints = {"hard_constraints": target_constraints}
    return {
        "objective": clean_str(
            raw.get("objective") or creative.get("objective"), request),
        "focus": clean_str(
            raw.get("focus") or creative.get("focus"), domain or request),
        "target_constraints": target_constraints if isinstance(
            target_constraints, dict) else {},
        "innovation_floor": clean_str(
            raw.get("innovation_floor") or creative.get("innovation_floor"), "L3"),
        "min_candidates": _int_value(
            raw.get("min_candidates") or creative.get("min_candidates"), 4, 1),
        "differentiation_axes": _string_list(
            raw.get("differentiation_axes")
            or creative.get("differentiation_axes"), limit=30),
        "evaluation_criteria": _string_list(
            raw.get("evaluation_criteria")
            or creative.get("evaluation_criteria"), limit=30),
        "constraints": _string_list(
            raw.get("constraints") or creative.get("constraints"), limit=30),
    }


def normalize_budget(data: dict[str, Any] | None,
                     mission: dict[str, Any]) -> dict[str, Any]:
    raw = data or {}
    max_total = _int_value(
        raw.get("max_total_results"), int(mission.get("max_results") or 80), 1)
    return {
        "max_collection_rounds": _int_value(
            raw.get("max_collection_rounds"), 2, 1),
        "max_total_results": max_total,
        "max_candidates": _int_value(raw.get("max_candidates"), 50, 1),
    }


def normalize_plan(data: dict[str, Any] | None, request: str) -> dict[str, Any]:
    """补全字段，形成 Planner-first 的统一研究任务契约。"""
    raw = data or {}
    goal = clean_str(raw.get("goal"), request)
    domain = clean_str(raw.get("domain"), goal)
    content_type = clean_str(raw.get("content_type"), infer_content_type(request))
    task_kind = clean_str(raw.get("task_kind"), infer_task_kind(request)).lower()
    if task_kind not in ("summary", "generative", "frontier", "evaluation", "proof"):
        task_kind = infer_task_kind(request)
    today = date.today().isoformat()

    mission_raw = raw.get("mission") or {}
    terms = _string_list(mission_raw.get("seed_terms"), limit=30)
    if not terms:
        terms = [domain]
    mission = {
        "seed_terms": terms,
        "max_results": _int_value(mission_raw.get("max_results"), 80, 1),
        "min_confidence": _float_value(mission_raw.get("min_confidence"), 0.6),
        "collection_mode": clean_str(mission_raw.get("collection_mode"), "broad"),
        "recency_window": clean_str(
            mission_raw.get("recency_window"), f"2018-01-01:{today}"),
    }
    retrieval = normalize_retrieval(raw.get("retrieval"), request)
    analysis_targets = _string_list(raw.get("analysis_targets"), limit=30)
    if not analysis_targets:
        analysis_targets = ["方法", "材料", "性能指标", "应用", "开放问题"]
    analysis_plan = normalize_analysis_plan(
        raw.get("analysis_plan"), analysis_targets)
    if analysis_plan["dimensions"]:
        analysis_targets = analysis_plan["dimensions"]

    creative_contract = None
    design_contract = None
    if task_kind == "generative":
        creative_contract = normalize_creative_contract(
            raw.get("creative_contract"), request, domain)
        design_contract = normalize_design_contract(
            raw.get("design_contract"), task_kind, request, domain,
            creative_contract)
        if design_contract is not None:
            creative_contract = normalize_creative_contract(
                raw.get("creative_contract"), request, domain, design_contract)

    deliverable = raw.get("deliverable") or {}
    deliverable_out = {
        "format": clean_str(deliverable.get("format"), "markdown"),
        "sections_policy": "emergent",
        "language": clean_str(deliverable.get("language"), "zh"),
    }
    instruction_contract = normalize_instruction_contract(
        raw.get("instruction_contract"), request, task_kind,
        creative_contract, deliverable_out)
    retrieval_plan = normalize_retrieval_plan(
        raw.get("retrieval_plan"), mission, retrieval, analysis_targets,
        domain, today)
    evidence_policy = normalize_evidence_policy(raw.get("evidence_policy"))
    stop_conditions = raw.get("stop_conditions") or retrieval_plan.get(
        "stop_conditions") or []
    if not isinstance(stop_conditions, list):
        stop_conditions = [stop_conditions]
    budget = normalize_budget(raw.get("budget"), mission)
    return {
        "goal": goal,
        "domain": domain,
        "content_type": content_type,
        "task_kind": task_kind,
        "analysis_plan": analysis_plan,
        "evidence_policy": evidence_policy,
        "design_contract": design_contract,
        "creative_contract": creative_contract,
        "domain_profile": normalize_domain_profile(
            raw.get("domain_profile"), domain, request),
        "analysis_targets": analysis_targets,
        "mission": mission,
        "retrieval_plan": retrieval_plan,
        "retrieval": retrieval,
        "instruction_contract": instruction_contract,
        "stop_conditions": stop_conditions,
        "budget": budget,
        "deliverable": deliverable_out,
        "constraints": raw.get("constraints") or [],
    }


def deterministic_plan(request: str) -> dict[str, Any]:
    """无模型或模型解析失败时生成可运行的任务单。"""
    return normalize_plan(None, request)


def make_planner_node(model=None,
                      max_results_override: int | None = None,
                      conn: sqlite3.Connection | None = None,
                      settings: Settings | None = None,
                      budget_override: dict[str, Any] | None = None):
    """构造 LangGraph 工作规划节点。model 为 None 时使用确定性任务单。

    budget_override 可按顶层入口下调预算（如候选池规模），
    用于控制单次 LLM 的输出长度，避免超大 JSON 导致请求长时间挂起。
    """
    settings = settings or default_settings

    def planner_node(state: dict) -> dict:
        request = clean_str(state.get("request"), "请检索并整理研究前沿")
        run_id = state.get("run_id")
        log_study_event(conn, settings, "planner", run_id, "running",
                        {"request": request[:500]})
        plan = None
        model_error = None
        parsed_ok = False
        if model is not None:
            prompt = PLANNER_PROMPT.replace(
                "{today}", date.today().isoformat()
            ).replace("{request}", request)
            try:
                msg = model.invoke([HumanMessage(content=prompt)])
                raw = getattr(msg, "content", str(msg))
                parsed = parse_json_object(raw)
                if not isinstance(parsed, dict) or not parsed:
                    raise ValueError(
                        "Planner 输出无法解析为 JSON 对象（原文前 120 字："
                        f"{clean_str(raw, '')[:120]}）")
                plan = normalize_plan(parsed, request)
                parsed_ok = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("规划节点 LLM 调用失败: %s", exc)
                model_error = str(exc)
        # 有模型但输出不可解析 → 明确失败，不静默降级（P1-3）：
        # 否则 status=planned + planner_mode="llm" 会把确定性兜底伪装成模型结果，
        # planning_failed 分支永远不可达，掩盖模型故障。
        if model is not None and not parsed_ok:
            log_study_event(conn, settings, "planner", run_id, "failed",
                            {"error": model_error or "empty planner output"})
            return {
                "plan": {},
                "status": "planning_failed",
                "error": model_error or "Planner LLM 未返回有效任务单",
            }
        if plan is None:
            plan = deterministic_plan(request)
            plan["planner_mode"] = "offline_fallback"
        else:
            plan["planner_mode"] = "llm"
        if model_error:
            plan["planner_model_error"] = model_error
        if max_results_override is not None:
            plan["mission"]["max_results"] = max(1, int(max_results_override))
            plan.setdefault("retrieval_plan", {})[
                "max_results_per_query"] = max(1, int(max_results_override))
        if budget_override:
            budget = plan.setdefault("budget", {})
            for key, value in budget_override.items():
                if value is None:
                    continue
                try:
                    budget[key] = max(1, int(value))
                except (TypeError, ValueError):
                    continue
        log_study_event(
            conn, settings, "planner", run_id, "done",
            {
                "goal": plan.get("goal"),
                "domain": plan.get("domain"),
                "content_type": plan.get("content_type"),
                "task_kind": plan.get("task_kind"),
                "min_candidates": ((plan.get("design_contract") or {})
                                   .get("min_candidates")),
                "innovation_floor": ((plan.get("design_contract") or {})
                                     .get("innovation_floor")),
                "seed_terms": plan.get("mission", {}).get("seed_terms"),
                "max_results": plan.get("mission", {}).get("max_results"),
                "retrieval_queries": (
                    plan.get("retrieval_plan", {}).get("query_variants")),
            })
        return {"plan": plan, "status": "planned"}

    return planner_node


def dump_plan(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, indent=2)


_WORDISH = re.compile(r"[a-zA-Z0-9]{4,}|[\u4e00-\u9fff]{2,}")


def split_seed_terms(request: str) -> list[str]:
    """供外部工具层使用的轻量分词，仅作为确定性检索兜底。"""
    return list(dict.fromkeys(m.group(0) for m in _WORDISH.finditer(request)))[:8]
