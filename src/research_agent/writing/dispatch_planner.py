"""自然语言 → 派工单：把"用你自己的话说的要求"变成**可执行的步骤序列**。

**为什么需要它**：派工协议（`dispatch.py`）已经能执行"检索 → 抽取 → 消费"这样的
任务链，登记表也写清了每个节点能干什么；但用户说的话（"把所有 ynamide 文献抽成
知识"）和那张任务单之间还缺一层翻译。这一层就是本模块。

**三条硬约束**：

1. **只认登记表里的任务**：模型给的任务名不在 `node_registry` 里就丢掉——
   宁可少做，不可假装能执行；
2. **怎么问就给怎么多的方案**：A 只检索（最小）→ B 检索+抽新 → C 检索+抽既有，
   三条的差异点是"补齐哪个维度"，用户的选择才有信息量；
3. **模型不可用要诚实**：走确定性解析，并在 `note` 里写明，不把兜底说成模型产物。

自然语言里能认出来的意图只有有限的几种（检索/抽取/评估/消费/审核/核查/导出），
因此确定性兜底并不弱：正则命中关键词就能给出合理方案，模型只是让措辞更贴切。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "parse_dispatch_request",
    "build_dispatch_plan",
    "detect_intents",
    "INTENT_TASKS",
    "plan_templates",
]

#: 意图 → 登记表里的任务名（键是"用户可能想干什么"）
INTENT_TASKS: dict[str, str] = {
    "retrieve": "retrieve",
    "extract": "extract_knowledge",
    "quality": "assess_quality",
    "consume": "consume_knowledge",
    "compose": "compose_section",
    "review": "review",
    "fact_check": "fact_check",
    "export": "export",
    "enrich": "enrich_metadata",
    "ontology": "rebuild_ontology",
}

#: 关键词 → 意图。中英都给，用户怎么打都能命中。
INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "retrieve": ("检索", "搜", "找文献", "补充文献", "补检", "抓", "下载",
                 "search", "retrieve", "collect", "fetch"),
    "extract": ("抽取", "抽成", "抽知识", "提取", "知识化", "入库知识",
                "建超边", "抽", "知识", "extract", "knowledge"),
    "quality": ("质量", "评分", "评估", "打分", "q值", "a/t/q", "quality",
                "assess", "score"),
    "consume": ("消费", "机制理解", "机会发现", "设计上下文", "consume"),
    "compose": ("写正文", "成段", "生成正文", "撰写", "compose", "draft"),
    "review": ("审核", "校对", "审阅", "review", "proofread"),
    "fact_check": ("事实核查", "核查", "查证", "伪造引用", "fact",
                   "verify", "check"),
    "export": ("导出", "export", "markdown"),
    "enrich": ("元数据", "补全信息", "作者", "机构", "enrich", "metadata"),
    "ontology": ("本体", "重建视图", "聚类", "ontology", "rebuild"),
}

#: 任务的典型参数默认值（模型没给就用它）
_TASK_DEFAULTS: dict[str, dict[str, Any]] = {
    "retrieve": {"max_results": 20},
    "extract_knowledge": {"scope": "new"},
}


def detect_intents(text: str) -> list[str]:
    """从自然语言里认出意图（按关键词命中，顺序即登记表顺序）。"""
    lowered = str(text or "").lower()
    hits: list[str] = []
    for intent, words in INTENT_KEYWORDS.items():
        if any(word.lower() in lowered for word in words):
            hits.append(intent)
    return hits


#: 任务名 → 意图名（反向表；用于模型直接给任务名的情况）
_TASK_INTENT: dict[str, str] = {}
for _intent, _task in INTENT_TASKS.items():
    _TASK_INTENT.setdefault(_task, _intent)


def _intent_for_task(task: str) -> str:
    return _TASK_INTENT.get(task, task)


def plan_templates(intents: list[str] | None = None) -> list[dict[str, Any]]:
    """3 个执行方案骨架。**差异点是"补齐哪个维度"**，用户的选择才有信息量。

    按用户**真正的意图**分成三族：

    - **没说要检索，但说要抽知识**（如"把 ynamide 文献抽成知识"）：
      A 先检索再抽新文献 / B 只抽库里**已有**的同主题文献（不花检索配额）/
      C 检索 + 抽新 + 顺带抽既有。
      这里**不能**给出"只检索"的方案——用户没要求检索，摆上去是答非所问。
    - **明确要检索**（"补检一下"）：A 只检索（最小）/ B 检索 + 抽新 /
      C 检索 + 抽既有（常见瓶颈是"文献有但没抽"）。
    - **只要别的任务**（"重建本体"、"导出"、"审核这一段"）：三个方案是
      **这件事本身的轻重档**，不能被强行塞进"检索 + 抽取"的模子。

    三个方案都会带上用户提到的**全部**任务——用户要的事不能被"三选一"吞掉。
    """
    wanted = [i for i in (intents or []) if i in INTENT_TASKS]
    wants_retrieve = "retrieve" in wanted
    wants_extract = "extract" in wanted

    # "required" 标记让解析器知道哪些步骤**不能因为条件不满足就删掉**：
    # 用户点名要的事必须留着并说明为什么跳过；为了拉开方案差异而追加的辅助步骤
    # （抽取、质检…）条件不满足时删掉，免得方案里挂一堆"注定跳过"的噪音。
    def step(intent: str, scope: str = "") -> dict[str, Any]:
        task = INTENT_TASKS[intent]
        item: dict[str, Any] = {"intent": intent, "task": task,
                               "required": _user_wants(task, wanted)}
        if scope:
            item["scope"] = scope
        return item

    # 用户提到的其它任务，每个方案都要带上——三选一不能吞掉要求
    extras = list(dict.fromkeys(INTENT_TASKS[i] for i in wanted
                                if i not in ("retrieve", "extract")))
    extra_steps = [{"intent": _intent_for_task(task), "task": task,
                    "required": True}
                   for task in extras]

    if wants_extract and not wants_retrieve:
        plans = _plans(
            ("先检索补充，再抽新入库文献的知识",
             "库内证据可能不够，先补文献再抽取，产出可直接引用的证据与超边",
             [step("retrieve"), step("extract", "new")]),
            ("只抽库内既有同主题文献（不检索）",
             "常见瓶颈是「文献有但没抽」——不动检索配额，可能立刻够用",
             [step("extract", "existing")]),
            ("检索补充 + 抽新入库 + 顺带抽既有",
             "覆盖最广，代价也最高（既花检索配额，又抽两批文献）",
             [step("retrieve"), step("extract", "new"),
              step("extract", "existing")]),
        )
    elif wants_retrieve or wants_extract:
        plans = _plans(
            ("只补检索（最小）", "只补充文献，随后按常规链路复判",
             [step("retrieve")]),
            ("补检索 + 对新入库文献抽知识",
             "检索后把本次新增文献抽成知识，产出可直接引用的证据与超边",
             [step("retrieve"), step("extract", "new")]),
            ("补检索 + 对库内既有同主题文献抽知识",
             "常见瓶颈其实是「文献有但没抽」——抽既有文献可能立刻够用",
             [step("retrieve"), step("extract", "existing")]),
        )
    else:
        # 单一任务没有"轻重档"可分，硬凑三个只是噪音。给出三档**真正不同**的做法：
        # 只做 / 做完沉淀到本体 / 做完再复核。
        base = list(extra_steps)
        joined = "、".join(reg_label(s["task"]) for s in base)
        plans = list(_plans(
            (f"只执行：{joined}",
             "只做你要的这一件事，然后回报结果",
             list(base)),
            (f"执行并沉淀：{joined} + 重建本体视图",
             "做完后重建本体视图，让新抽取的知识立刻反映在图谱里",
             list(base) + [{"intent": "ontology", "task": "rebuild_ontology",
                            "required": False}]),
            (f"执行并复核：{joined}",
             "做完后立刻带上事实核查，把结果一并交回",
             list(base) + [step("fact_check")]),
        ))

    # 每个方案都要带上用户提到的**全部**任务（三选一不能吞掉要求）。
    # 注意在"步骤名"层面去重，并**先去掉方案自身内部的重复任务**——
    # 否则"审核 + 复核（审核、事实核查）"会变成审核跑两遍。
    plans = [_dedupe_steps(plan) for plan in plans]
    if extra_steps:
        for plan in plans:
            have = {s["task"] for s in plan["steps"]}
            added = [s for s in extra_steps if s["task"] not in have]
            if not added:
                continue
            plan["steps"].extend(dict(s) for s in added)
            plan["label"] += " + " + "、".join(
                reg_label(s["task"]) for s in added)
    return _distinct(plans)


def _distinct(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """丢掉步骤序列重复的方案。

    "同一件事的三档做法"很容易在某个具体请求上退化成三个一模一样的方案
    （实测："重建本体视图"会出现 A 与 B 完全相同）。让用户在三份一样的东西里
    选，比只给他一份更糟——所以这里按步骤序列去重。
    """
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for plan in plans:
        # 兼容两种形态：方案骨架用 ``steps``（意图步骤），可执行方案用 ``plan``
        # （登记表步骤）。取错字段会让每个方案都被当成"空方案"丢掉，
        # 最后只剩兜底的那一条。
        steps = plan.get("plan") or plan.get("steps") or []
        key = tuple(
            (str(s.get("task") or ""), str(s.get("scope") or ""),
             str(s.get("skip_reason") or ""))
            for s in steps)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(plan)
    return out


def _dedupe_steps(plan: dict[str, Any]) -> dict[str, Any]:
    """同一方案内的重复任务只保留第一次出现。

    键是 ``任务 + 抽取范围``：``extract_knowledge(scope=new)`` 与
    ``(scope=existing)`` 是**两个不同**的步骤（抽新入库 vs 抽既有），
    不能当成重复；而"审核 + 复核（审核、事实核查）"里的两个审核是重复，要去掉。
    """
    seen: set[tuple[str, str]] = set()
    steps: list[dict[str, Any]] = []
    for item in plan.get("steps") or []:
        key = (str(item.get("task") or ""), str(item.get("scope") or ""))
        if key in seen:
            continue
        seen.add(key)
        steps.append(item)
    plan["steps"] = steps
    return plan


def _honest_label(label: str, steps: list[dict[str, Any]]) -> str:
    """若某个方案**整体一步都跑不了**，在标签上直接写明，别让用户选了个空壳。"""
    if any(not s.get("skip_reason") for s in steps):
        return label
    reasons = {str(s.get("skip_reason") or "") for s in steps}
    return f"{label}（当前条件下无法执行：{'；'.join(sorted(reasons))}）"


def _user_wants(task: str, wanted: list[str]) -> bool:
    """用户是否点名要了这个任务（``wanted`` 是意图名列表）。"""
    for intent in wanted:
        if INTENT_TASKS.get(intent) == task:
            return True
    return False


def _plans(*triples: tuple[str, str, list[dict[str, str]]]
           ) -> list[dict[str, Any]]:
    """把三个 ``(标签, 提示, 步骤)`` 拼成 A/B/C 方案。"""
    return [
        {"id": "ABC"[index], "label": label, "hint": hint, "steps": steps}
        for index, (label, hint, steps) in enumerate(triples)
    ]


def reg_label(task: str) -> str:
    """任务的中文名（登记表为准，查不到或没写就退回任务名）。"""
    from research_agent.writing import node_registry as reg
    found = reg.get_task(task)
    if not found:
        return task
    return str(found[1].label or task)


def parse_dispatch_request(*, request: str, topic: str = "",
                           heading: str = "",
                           verdict: dict[str, Any] | None = None,
                           model: Any = None,
                           registry_text: str = "",
                           conn: Any = None,
                           project: dict[str, Any] | None = None,
                           project_id: int = 0,
                           ) -> dict[str, Any]:
    """把用户的要求解析成 3 个**可直接执行**的派工方案。

    返回::

        {
          "plans": [
            {"id": "A", "label": ..., "hint": ...,
             "plan": [{"task": "retrieve", "node": "retrieval",
                       "args": {...}}, ...]},
            ...
          ],
          "intents": ["retrieve", "extract"],
          "query_terms": [...],
          "year_from": 0,
          "note": "...",
          "parsed_by": "llm" | "fallback",
          "dropped": ["..."],      # 模型给了但登记表里没有、被丢掉的任务名
        }
    """
    from research_agent.writing import node_registry as reg

    verdict = verdict or {}
    text = str(request or "").strip()
    intents = detect_intents(text)
    dropped: list[str] = []
    queries: list[str] = []
    year_from = 0
    note = ""
    parsed_by = "fallback"

    if model is not None:
        try:
            from research_agent.writing.gap_planner import _invoke, _parse_json
            prompt = _PARSE_PROMPT.format(
                topic=topic or "未指定", heading=heading or "未指定",
                request=text,
                unmet="、".join(str(x) for x in
                               (verdict.get("unmet_dimensions") or [])) or "无",
                registry=registry_text or reg.describe_for_prompt())
            data = _parse_json(_invoke(_LoggedNode(model), prompt)) or {}
            model_intents = [str(x).strip() for x in
                             (data.get("intents") or []) if str(x).strip()]
            if model_intents:
                # 模型可能填了**任务名**而不是意图名（提示词里给的是任务清单），
                # 两种都接受；其余一律丢掉并记录，绝不假装能执行。
                kept, unknown = [], []
                for name in model_intents:
                    if name in INTENT_TASKS:
                        kept.append(name)
                    elif reg.get_task(name):
                        kept.append(_intent_for_task(name))
                    else:
                        unknown.append(name)
                dropped = unknown
                intents = list(dict.fromkeys(kept))
            queries = [str(x).strip() for x in (data.get("query_terms") or [])
                       if str(x).strip()]
            note = str(data.get("note") or "")[:160]
            try:
                year_from = int(data.get("year_from") or 0)
            except (TypeError, ValueError):
                year_from = 0
            if intents:
                parsed_by = "llm"
        except Exception as exc:  # noqa: BLE001 —— 解析失败必须能退回确定性
            logger.warning("自然语言派工解析失败，走确定性兜底: %s", exc)
            note = f"解析失败（{type(exc).__name__}），按关键词识别执行"

    if not intents:
        # 一句都没认出来：最保守的选择是"只是要更多文献"
        intents = ["retrieve"]
        note = note or "未识别出具体意图，按「补充文献」处理"

    if not queries:
        from research_agent.writing.gap_planner import _topic_terms
        queries = [str(q) for q in (verdict.get("suggested_queries") or [])][:6]
        if not queries:
            queries = _topic_terms(topic or text)[:4]

    plans: list[dict[str, Any]] = []
    for tpl in plan_templates(intents):
        steps = _steps_to_plan(tpl["steps"], queries, year_from, reg,
                               conn=conn, project=project, heading=heading,
                               project_id=int(project_id
                                              or (project or {}).get("project_id")
                                              or 0))
        steps = _dedupe_steps({"steps": steps})["steps"]
        if not steps:
            continue
        plans.append({"id": tpl["id"], "label": tpl["label"],
                      "hint": tpl["hint"],
                      "intents": [s["intent"] for s in tpl["steps"]],
                      "tasks": [s["task"] for s in tpl["steps"]],
                      "plan": steps})
    plans = _distinct(plans)
    if not plans:
        fallback = _steps_to_plan([{"intent": "retrieve", "task": "retrieve"}],
                                  queries, year_from, reg, heading=heading)
        plans = [{"id": "A", "label": "只补检索（最小）",
                  "hint": "没有可执行的其它方案，先补充文献",
                  "intents": ["retrieve"], "tasks": ["retrieve"],
                  "plan": fallback}]
    # 标签要如实：整条被跳过的任务不该出现在"这个方案做什么"里
    for plan in plans:
        plan["label"] = _honest_label(plan["label"], plan["plan"])
    # 方案编号重排：去掉重复方案后 A/B/C 不能有缺口
    for index, plan in enumerate(plans):
        plan["id"] = "ABC"[index] if index < 3 else f"P{index + 1}"

    return {
        "plans": plans,
        "intents": intents,
        "query_terms": queries,
        "year_from": year_from,
        "note": note,
        "parsed_by": parsed_by,
        "dropped": dropped,
    }


class _LoggedNode:
    """给解析调用用的角色标记（让日志里 node=planner 而不是推断值）。"""

    def __init__(self, model: Any) -> None:
        self._model = model

    def invoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        from research_agent.logging.proxy import LoggedModel
        return LoggedModel(self._model, role="planner",
                           node="planner").invoke(messages, *args, **kwargs)


def _steps_to_plan(steps: list[dict[str, Any]], queries: list[str],
                   year_from: int, reg: Any, *, conn: Any = None,
                   project: dict[str, Any] | None = None,
                   heading: str = "",
                   project_id: int = 0) -> list[dict[str, Any]]:
    """把"意图步骤"翻成"登记表步骤"，并接上 ``$stepN`` 引用。

    两处**在解析阶段就把参数定下来**，比留给执行器猜要清楚：

    - ``scope="existing"`` 要抽的是**库内同主题但尚未抽取**的文献，集合由
      `collaboration._existing_same_topic_keys` 给出，直接写成具体 ``paper_keys``；
    - 需要"一批文献"但没有前序检索步骤的任务（质量评估、知识抽取…），
      退回**库内已有文献**——用户说"给这批文献做评估"时指的就是库里那些。

    ``project_id`` 之类能自己补上的参数直接补（否则任务会因为"没给 project_id"
    永远跳过，登记了却用不了）。
    """
    out: list[dict[str, Any]] = []
    for raw in steps:
        task = str(raw.get("task") or "")
        found = reg.get_task(task)
        if not found:
            continue
        node, spec = found
        args: dict[str, Any] = {}
        skip_reason = ""
        if "project_id" in spec.accepts and project_id:
            args["project_id"] = int(project_id)
        if task == "retrieve":
            args["seed_terms"] = list(queries)
            if year_from:
                args["year_from"] = year_from
            args.update({k: v for k, v in _TASK_DEFAULTS["retrieve"].items()
                         if k in spec.accepts})
        elif task == "extract_knowledge":
            scope = str(raw.get("scope") or "new")
            args["scope"] = scope
            if scope == "existing":
                keys = _existing_keys(conn, project)
                if keys:
                    args["paper_keys"] = keys
                else:
                    skip_reason = "库内没有可抽的既有同主题文献"
            else:
                retrieve_index = _last_index(out, "retrieve")
                if retrieve_index is None:
                    skip_reason = "没有前序检索步骤，取不到新增文献"
                else:
                    args["paper_keys"] = f"$step{retrieve_index}.paper_keys"
        else:
            if "plan" in spec.accepts:
                args["plan"] = {}
            if "section_key" in spec.accepts and heading:
                args["section_key"] = heading
            if "paper_keys" in spec.accepts:
                retrieve_index = _last_index(out, "retrieve")
                if retrieve_index is not None:
                    args["paper_keys"] = f"$step{retrieve_index}.paper_keys"
                else:
                    # 没有前序检索：用**库内已有文献**。用户说"给这批文献做评估"
                    # 指的就是库里那些；取不到才如实跳过。
                    keys = _library_keys(conn)
                    if keys:
                        args["paper_keys"] = keys
                    else:
                        skip_reason = "没有前序检索步骤，且库内没有可用文献"
        # 解析时就跑不了的步骤：**若它正是用户点名要的任务，一步都不能省**，
        # 保留并说明原因（执行器会如实报 skipped）；若只是自动追加的辅助步骤
        # （抽取、质检…），删掉它，避免方案里挂着一堆"注定跳过"的噪音。
        if skip_reason and not raw.get("required"):
            continue
        item: dict[str, Any] = {"task": task, "node": node.node, "args": args,
                                "label": spec.label}
        if skip_reason:
            item["skip_reason"] = skip_reason
        out.append(item)
    return out


def _existing_keys(conn: Any, project: dict[str, Any] | None) -> list[str]:
    if conn is None:
        return []
    try:
        from research_agent.writing.collaboration import _existing_same_topic_keys
        topic = str((project or {}).get("topic")
                    or (project or {}).get("title") or "")
        return _existing_same_topic_keys(conn, topic, limit=20)
    except Exception as exc:  # noqa: BLE001 —— 查不到就当作没有
        logger.debug("查询既有同主题文献失败: %s", exc)
        return []


def _library_keys(conn: Any, limit: int = 20) -> list[str]:
    """库内已入库的文献（按引用数/时间取前 N 篇）。

    用在"没有前序检索，但这个任务需要一批文献"的场合（质量评估、抽取…）。
    用户说"给这批文献做…"时指的就是库里已有的那些。
    """
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT paper_key FROM papers WHERE status='ingested' "
            "ORDER BY COALESCE(cited_by_count, 0) DESC, "
            "COALESCE(pub_year, 0) DESC LIMIT ?",
            (int(limit),)).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("查询库内文献失败: %s", exc)
        return []
    return [str(r["paper_key"]) for r in rows]


def _last_index(steps: list[dict[str, Any]], task: str) -> int | None:
    """最近一个指定任务的步骤下标（用于 ``$stepN`` 引用）。"""
    for index in range(len(steps) - 1, -1, -1):
        if steps[index].get("task") == task:
            return index
    return None


#: 解析提示词：明确要求只从登记表里选任务，并给出 3 个方案的差异点
_PARSE_PROMPT = """你是科研工作流的派工调度员。用户用自然语言提了一个要求，
请把它翻译成**可执行的任务序列**。

主题：{topic}
当前部分：{heading}
用户的要求：{request}
当前判定缺的维度：{unmet}

**可派工的任务只有下面这些**（任务名必须严格取自这里，不得编造）：
{registry}

请只输出 JSON：

{{"intents": ["retrieve", "extract_knowledge"],
  "query_terms": ["检索词1", "检索词2"],
  "year_from": 2020,
  "note": "一句话说明你理解到的意图"}}

纪律：
- `intents` 只能填上面清单里出现过的**任务名**（如 retrieve、extract_knowledge、
  assess_quality、consume_knowledge、compose_section、review、fact_check）；
- 用户没提检索就不要加 retrieve；用户明确说"只审不改"就不要加 compose_section；
- `query_terms` 给 3~6 个英文或中文学术检索词，不要一句话；
- `year_from` 只在用户提到年份时才给数字，否则给 0；
- 不要输出任务之外的字段，不要解释。
"""


def build_dispatch_plan(*, request: str, db_path: Any = None, conn: Any = None,
                        project_id: int = 0, section_key: str = "",
                        topic: str = "", heading: str = "",
                        verdict: dict[str, Any] | None = None,
                        use_model: bool = True, model: Any = None,
                        settings: Any = None) -> dict[str, Any]:
    """**对外入口**：自然语言 → 3 个可直接派工的方案（带项目上下文）。

    把"取上下文 → 解析 → 补默认值"这套动作收在一个函数里，让 API 层与访谈
    各拿一处调用，避免两边各写一遍而慢慢漂移。
    """
    from research_agent.config import settings as default_settings
    from research_agent.db import connect

    s = settings or default_settings
    own_conn = conn is None
    db = conn or connect(db_path or s.db_path)
    try:
        project: dict[str, Any] = {}
        if project_id:
            from research_agent.writing.service import get_project
            project = get_project(db, int(project_id)) or {}
        if not topic:
            topic = str(project.get("topic") or project.get("title") or "")
        picked = model
        if picked is None and use_model:
            try:
                from research_agent.writing.service import default_model
                picked, _reason = default_model()
            except Exception as exc:  # noqa: BLE001 —— 缺 Key 是预期情况
                logger.warning("派工解析未绑定模型: %s", exc)
                picked = None
        result = parse_dispatch_request(
            request=request, topic=topic, heading=heading, verdict=verdict,
            model=picked, conn=db, project=project,
            project_id=int(project_id or 0))
        result.update({"ok": True, "section_key": section_key,
                       "project_id": int(project_id or 0)})
        return result
    finally:
        if own_conn:
            db.close()
