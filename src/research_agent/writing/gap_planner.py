"""缺口与方案助手：把"该给用户看的选项"算出来。

三件事（对应方案 v6 §4、§5）：

1. **拟 3 个内容方案摘要**——每部分给出 3 个方向不同的写法预览（2~3 句），
   让用户"从 3 个里挑一个"这件事真的有信息量，而不是三个措辞不同的同一句话；
2. **拟补检轮数建议**——用户选"让 AI 决定"时，给 1~5 的建议值 + 理由；
3. **把自定义任务解析成 3 个执行方案**——用户用自己的话描述要补什么，
   解析成 A（最小）/ B（含新文献抽取）/ C（含既有文献抽取）三套，
   差异体现在**补齐的维度不同**。

模型不可用时全部退回确定性兜底，并且**如实说明这是兜底**（不伪装成模型产物）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "draft_content_options",
    "suggest_collection_rounds",
    "parse_custom_task",
    "CUSTOM_PLAN_TEMPLATES",
    "fallback_content_options",
]

#: 自定义任务的 3 个执行方案骨架（差异点是"补齐哪个维度"）
CUSTOM_PLAN_TEMPLATES = [
    {
        "id": "A",
        "label": "只补检索（最小）",
        "hint": "只执行检索补充文献，随后按常规链路复判",
        "tasks": ["retrieve"],
        "fills": ["papers"],
    },
    {
        "id": "B",
        "label": "补检索 + 对新入库文献抽知识",
        "hint": "检索后把本次新增文献抽成知识，产出可直接引用的证据与超边",
        "tasks": ["retrieve", "extract_knowledge"],
        "extract_scope": "new",
        "fills": ["papers", "knowledge"],
    },
    {
        "id": "C",
        "label": "补检索 + 对库内既有同主题文献抽知识",
        "hint": "常见瓶颈其实是「文献有但没抽」——抽既有文献可能立刻够用，无需新增",
        "tasks": ["retrieve", "extract_knowledge"],
        "extract_scope": "existing",
        "fills": ["knowledge"],
    },
]


def _invoke(model: Any, prompt: str) -> str:
    from langchain_core.messages import HumanMessage
    msg = model.invoke([HumanMessage(content=prompt)])
    return str(getattr(msg, "content", msg) or "")


def _parse_json(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ------------------------------------------------------------------ 3 个内容方案

_OPTIONS_PROMPT = """你在帮我给一篇论文的某个部分定写作方向。

论文主题：{topic}
本部分：{heading}
本部分职责：{role}
本部分应依据的证据类型：{evidence}
{extra}

请给出 **3 个方向明显不同**的写作方案，每个方案配一段**摘要式预览**（2~3 句，
具体到"会写什么、强调什么、用什么口径"），让用户一眼能看出差别。

要求：
1. 三个方案的侧重必须真的不同（例如：偏机制解释 / 偏可复现操作 / 偏对照比较），
   不要用同一句话换三种措辞；
2. 只能依据给定主题与职责来写，**不要编造具体数值或文献**；
3. 只输出 JSON，不要任何其他文字：
{{"options": [{{"id": "A", "summary": "…"}},
              {{"id": "B", "summary": "…"}},
              {{"id": "C", "summary": "…"}}]}}
"""

#: 方向模板：模型不可用时用它们拼出三个**确实不同**的兜底方案
_FALLBACK_ANGLES = [
    ("机制与原理", "侧重解释机制与内在原理，说明各步骤为什么这样设计"),
    ("可复现操作", "侧重给出可复现的操作细节与量化条件，便于他人照做"),
    ("对比与边界", "侧重与既有工作对比，说明适用边界与局限"),
]


def fallback_content_options(heading: str, focus: list[str] | None = None,
                             role: str = "") -> list[dict[str, str]]:
    """确定性兜底：按"侧重方向"拼出三个不同的方案摘要。

    刻意让三条各自不同，避免退化成"三个一样的选项"。
    """
    focus = [str(x) for x in (focus or []) if str(x).strip()]
    focus_text = "、".join(focus[:3]) if focus else "核心内容"
    out: list[dict[str, str]] = []
    for idx, (name, desc) in enumerate(_FALLBACK_ANGLES):
        prefix = "（未调用模型，按通用方向生成）"
        out.append({
            "id": "ABC"[idx],
            "summary": f"{prefix}以「{name}」为侧重：{desc}。"
                       f"覆盖要点：{focus_text}。",
            "angle": name,
            "source": "fallback",
        })
    return out


def draft_content_options(*, topic: str, heading: str, role: str = "",
                          evidence_types: list[str] | None = None,
                          focus: list[str] | None = None,
                          instruction: str = "",
                          model: Any = None) -> dict[str, Any]:
    """拟 3 个内容方案（摘要力度）。

    返回 ``{"options": [...], "generated_by": "llm"|"fallback", "error": ""}``。
    """
    options: list[dict[str, str]] = []
    generated_by = "llm"
    error = ""
    extra = ""
    if focus:
        extra += "本部分写作要点：" + "、".join(str(x) for x in focus[:5]) + "\n"
    if instruction:
        extra += f"用户的补充说明：{instruction}\n"

    if model is not None:
        try:
            raw = _invoke(model, _OPTIONS_PROMPT.format(
                topic=topic or "未指定", heading=heading, role=role or "未指定",
                evidence="、".join(str(x) for x in (evidence_types or [])) or "不限",
                extra=extra))
            data = _parse_json(raw) or {}
            for item in data.get("options") or []:
                if not isinstance(item, dict):
                    continue
                summary = str(item.get("summary") or "").strip()
                if not summary:
                    continue
                options.append({
                    "id": str(item.get("id") or "ABC"[len(options)]),
                    "summary": summary,
                    "source": "llm",
                })
            if len(options) < 3:
                raise ValueError(f"模型只给出 {len(options)} 个可用方案（需 3 个）")
            options = options[:3]
        except Exception as exc:  # noqa: BLE001 —— 拟方案失败不该卡死访谈
            logger.warning("拟内容方案失败，退回通用方向: %s", exc)
            error = f"{type(exc).__name__}: {exc}"
            options = []
            generated_by = "fallback"
    else:
        generated_by = "fallback"
        error = "未提供模型"

    if not options:
        options = fallback_content_options(heading, focus, role)
        generated_by = "fallback"
    # 统一补齐 id（A/B/C），保证界面与后端用同一套编号
    for idx, item in enumerate(options):
        item["id"] = str(item.get("id") or "ABC"[idx])[:1]
    return {"options": options, "generated_by": generated_by, "error": error}


# ------------------------------------------------------------------ 轮数建议

_ROUNDS_PROMPT = """我要为论文的一个部分补充检索，需要你建议"最多补检几轮"。

论文主题：{topic}
本部分：{heading}
判定结论：{decision}
未达标的维度：{unmet}
当前命中文献：{papers} 篇
可量化条件的超边：{with_q}/{total} 条
建议的检索词：{queries}

补检一轮 = 按检索词去学术库抓一批新文献并入库，成本较高（时间与配额）。
请给出 1~5 之间的建议轮数，并说明理由（20 字以内）。

只输出 JSON：{{"rounds": 2, "reason": "…"}}
"""


def suggest_collection_rounds(*, topic: str, heading: str, verdict: dict[str, Any],
                              model: Any = None,
                              default: int = 2) -> dict[str, Any]:
    """建议补检轮数（用户选"让 AI 决定"时用）。

    返回 ``{"rounds": int, "reason": str, "source": "llm"|"fallback"}``。
    """
    counts = verdict.get("counts") or {}
    unmet = verdict.get("unmet_dimensions") or []
    if model is None:
        return {"rounds": int(default), "source": "fallback",
                "reason": "未提供模型，按默认轮数"}
    try:
        raw = _invoke(model, _ROUNDS_PROMPT.format(
            topic=topic or "未指定", heading=heading,
            decision=verdict.get("decision") or "未判定",
            unmet="、".join(str(x) for x in unmet) or "无",
            papers=counts.get("matched_papers") or 0,
            with_q=counts.get("hyperedges_with_quantity") or 0,
            total=counts.get("hyperedges") or 0,
            queries="、".join((verdict.get("suggested_queries") or [])[:5]) or "无"))
        data = _parse_json(raw) or {}
        rounds = int(data.get("rounds") or default)
        rounds = max(1, min(5, rounds))
        return {"rounds": rounds, "source": "llm",
                "reason": str(data.get("reason") or "")[:60]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("轮数建议失败，用默认值: %s", exc)
        return {"rounds": int(default), "source": "fallback",
                "reason": f"建议失败（{type(exc).__name__}），按默认轮数"}


# ------------------------------------------------------------------ 自定义任务

_CUSTOM_PROMPT = """用户要求为论文的某个部分补充支撑，请把他的话解析成可执行的检索计划。

论文主题：{topic}
本部分：{heading}
用户的说法：{request}
判定发现缺的维度：{unmet}
系统已建议的检索词：{queries}

请只输出 JSON，包含：
{{"query_terms": ["检索词1", "检索词2"],
  "year_from": 2019,
  "note": "一句话概括你对他意图的理解"}}

要求：
1. query_terms 要能直接投递到学术库（英文主题用英文词，中文主题用中文词）；
2. 最多 6 个检索词，去掉"文献""论文""补充"这类无检索价值的词；
3. 不要编造具体的论文标题或作者。
"""


def parse_custom_task(*, topic: str, heading: str, request: str,
                      verdict: dict[str, Any] | None = None,
                      model: Any = None) -> dict[str, Any]:
    """把用户的自定义请求解析成 **3 个执行方案**。

    三个方案的差异点是**补齐的维度不同**（A 只补文献 / B 补文献+新抽取 /
    C 补既有文献的抽取），这样用户的选择才有信息量。

    返回 ``{"plans": [...], "query_terms": [...], "note": str,
    "parsed_by": "llm"|"fallback"}``。
    """
    verdict = verdict or {}
    queries = [str(x) for x in (verdict.get("suggested_queries") or [])][:6]
    parsed_by = "llm"
    note = ""
    year_from = 0
    if model is not None:
        try:
            raw = _invoke(model, _CUSTOM_PROMPT.format(
                topic=topic or "未指定", heading=heading, request=request,
                unmet="、".join(str(x) for x in
                               (verdict.get("unmet_dimensions") or [])) or "无",
                queries="、".join(queries) or "无"))
            data = _parse_json(raw) or {}
            terms = [str(x).strip() for x in (data.get("query_terms") or [])
                     if str(x).strip()]
            if terms:
                queries = list(dict.fromkeys(terms + queries))[:6]
            note = str(data.get("note") or "")[:120]
            try:
                year_from = int(data.get("year_from") or 0)
            except (TypeError, ValueError):
                year_from = 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("自定义任务解析失败，用兜底检索词: %s", exc)
            parsed_by = "fallback"
            note = f"解析失败（{type(exc).__name__}），按系统建议的检索词执行"
    else:
        parsed_by = "fallback"
        note = "未提供模型，按系统建议的检索词执行"

    if not queries:
        # 连建议检索词都没有时，退回主题分词，保证任务不是空的
        queries = _topic_terms(topic)[:4]

    plans: list[dict[str, Any]] = []
    for tpl in CUSTOM_PLAN_TEMPLATES:
        plan = dict(tpl)
        plan["tasks"] = list(tpl["tasks"])
        plan["query_terms"] = list(queries)
        if year_from:
            plan["year_from"] = year_from
        plan["note"] = note
        plans.append(plan)
    return {"plans": plans, "query_terms": queries, "note": note,
            "year_from": year_from, "parsed_by": parsed_by}


def _topic_terms(topic: str) -> list[str]:
    from research_agent.study.planner import split_seed_terms
    return [t for t in split_seed_terms(str(topic or "")) if len(t) >= 3]
