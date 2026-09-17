"""LLM 知识抽取：把论文文本转为结构化的 实体/关系/属性/事件 JSON。

提示词允许模型按需引入新的实体/关系类型（动态本体 schema 演化的来源）。
返回的每条知识自带 confidence（模型自评），最终入库时再与文献质量 Q 融合。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

logger = logging.getLogger(__name__)


SYSTEM_HINT = (
    "你是科研知识抽取与本体构建引擎。你的任务是从单篇论文中抽取结构化事实，"
    "输出可合并进统一科研知识图谱的 JSON。\n"
    "核心原则：\n"
    "- 只抽取文中明确陈述或直接可推断的内容，禁止臆造、补全或泛化。\n"
    "- 优先复用库中已有规范实体（运行时提供），确保同一概念在不同文献中使用相同规范名，"
    "避免重复创建。\n"
    "- 抽取粒度应足够细，能支持后续推理（如方法-材料-性能-应用之间的关联），"
    "而非仅概括主题。\n"
    "- 同时识别并抽取论文中的关键事件（如实验、发现、临床试验），"
    "它们可能表达重要的过程性知识。"
)

ENTITY_TYPES = (
    "Method, Material, Device, Drug, Disease, Model, Metric, Dataset, Task, Theory, "
    "Parameter, Property, Application, Organism, CellLine, Chemical, Target, "
    "BiologicalProcess, Technology, Tool, Standard, Regulation, Institution, Researcher"
)

SCHEMA_HINT = """请严格输出一个 JSON 对象（不要输出其它文字、不要 markdown 代码块），结构如下：
{
  "entities": [
    {
      "type": "受控类型；从下方类型列表选择，若确有必要才新造英文 CamelCase",
      "name": "规范名：优先复用库中已有规范名；否则使用该领域最标准、无歧义的写法",
      "aliases": ["该实体在文中出现的其它写法/缩写，如 abbreviated-form、中文全称"],
      "attributes": {"属性名": "值", "属性名2": "值2"},
      "confidence": 0.0,
      "evidence": "支撑该实体的原句（可截断，≤300字符）"
    }
  ],
  "relations": [
    {
      "type": "受控词表中的关系词（必须按同义归一规则选择）",
      "subject": "entities.name 或库中已有规范名（必须与 entities 列表中 name 完全一致）",
      "predicate": "一句话补述，不要与 type 重复表达同一动词，例如 type=uses 时 predicate 可为 '用于…场景'",
      "object": "entities.name 或库中已有规范名（必须与 entities 列表中 name 完全一致）",
      "confidence": 0.0,
      "evidence": "支撑原句（可截断，≤300字符）"
    }
  ],
  "events": [
    {
      "type": "Experiment|Study|Discovery|ClinicalTrial|Observation",
      "trigger": "精简名词短语（如 'histological analysis of group A'），不要整句、不要以报告语开头",
      "participants": ["直接参与该事件的关键实体(≤5个)，必须与 entities 列表中的 name 完全一致"],
      "time": "时间描述或 null",
      "attributes": {},
      "confidence": 0.0,
      "evidence": "支撑原句（可截断，≤300字符）"
    }
  ],
  "hyperedges": [
    {
      "type": "observation|claim|procedure|causal_relation|comparison|definition|chronology|argument",
      "label": "该超边的简短名称；不要创建 Reaction/Event 节点",
      "members": [
        {"name": "entities 中的实体名", "role": "跨学科通用角色或领域角色", "qualifiers": {}}
      ],
      "conditions": [
        {"key": "temperature|duration|dose|setting|period|location|...", "operator": "=|>|<|between|described_as", "value": "值", "unit": "单位或 null"}
      ],
      "measurements": [
        {"metric": "yield|effect_size|p_value|accuracy|frequency|...", "value": "值", "unit": "单位或 null", "qualifier": "可选"}
      ],
      "confidence": 0.0,
      "evidence": "支撑原句（可截断，≤300字符）"
    }
  ]
}

实体类型建议列表（优先选择，若都不匹配再自造）：
Method, Material, Device, Drug, Disease, Model, Metric, Dataset, Task, Theory,
Parameter, Property, Application, Organism, CellLine, Chemical, Target,
BiologicalProcess, Technology, Tool, Standard, Regulation, Institution, Researcher

超边通用角色建议：agent, patient, target, instrument, medium, context,
moderator, mediator, outcome, comparison, location, time, evidence。
领域可用更精确角色，但角色只存在于超边成员中，不要把角色本身建成实体节点。

硬性要求：
1. 连通性：每条 relation 的 subject 和 object，以及每个 event 的 participants，
   必须与 entities 列表中的 name 或「库中已有规范名」字符串完全相同（包括大小写和空格）。
   不要使用变体或缩写。
2. 每个实体尽量至少出现在一条 relation 或 event 中；确实无法关联的才作为孤立实体输出。
3. 同一概念合并：若论文中的某个概念与库中已有规范名是同一事物（包括其别名、缩写），
   则 name 必须直接复用库中规范名，并将本文中的写法加入 aliases 数组。例如库中已有
   "Method: <规范全称>"，论文中写 "<缩写>"，则 name 应为
   "<规范全称>"，aliases 包含 "<缩写>"。
4. 实体命名：优先使用领域通用、无歧义的标准名称；缩写需在 name 或 aliases 中给出全称。
   例如 name 可为 "<全称>"，aliases 含 "<缩写>"。
5. 置信度标尺：0.9+ 多句/多段交叉印证；0.75~0.89 原文单句直接支持；0.6~0.74 由上下文明确推断；
   <0.6 存疑尽量不输出。
6. evidence 必须引用原文句子（可节选），不得改写或总结，长度≤300字符。
7. 若论文中未出现事件，可省略 events 数组或输出空数组；但不要强行创造事件。
8. 实体命名必须是名词性领域术语/专名。禁止把句子、衔接语、证据句或报告性短语当作
   name（如 "These results suggest ...", "The histological analysis showed ...",
   "In this study, we ...", "This review summarizes ...", "We demonstrated ..."）。
   这类内容属于 relation/event 的 evidence 或 predicate，而不是实体；实体应只保留
   被陈述的核心事物名词，例如 "Calcium phosphate cement" 而非
   "The calcium phosphate cement was found to promote ..."。
9. 细节保留、禁止过度合并：仅当两个名称指向“同一个具体事物”时才复用规范名。
   带实质性修饰的不同对象必须分别建实体，并把组成/配比/掺杂/工艺写入 attributes；
   例如 "Magnesium-doped calcium phosphate cement"、"Strontium-doped calcium
   phosphate cement" 与 泛称 "Calcium phosphate cement" 是不同实体；
   禁止为了复用规范名而把不同配方、掺杂、比例或变体并入同一通用节点。
10. 关系语义要具体：能用具体关系（uses/evaluates/made_of/promotes/inhibits/
    releases/differentiates_into/regulates/activates 等）就不要退回笼统的
    related_to/causes；related_to 仅在确无更具体关系时作兜底。
11. 事件必须是“做了什么的实验/过程/发现”，而不是一句话结论：trigger 用精简名词短语
    （如 "histological analysis of group A"），禁止把整句或报告语（如
    "We demonstrate ...", "results showed ...", "was developed using ..."）作为事件；
    这类“结论性陈述”应表达为 relation/event 的 evidence，而不是事件本身。
12. involves 关系克制使用：event.participants 仅列直接参与该事件的关键实体（≤5 个），
    仅在确有参与关系时给出；不要把同句共现的无关概念全部拉成 participants，
    避免 involves 变成笼统的“共现”关系。
13. conditions / measurements 必须真的填写，不允许留空数组：只要原文出现任何可量化的
    做法、配方、参数或结果，就必须落到 conditions 或 measurements 里。
    - 化学/材料/实验类超边（procedure、causal_relation、observation）
      必须给出 conditions：temperature(°C)、duration(h/min)、solvent、catalyst、
      ligand、additive、base、atmosphere、equivalent、mol%、concentration、pH、
      pressure、setting；有产率/选择性/性能数字时必须给出 measurements。
    - 评测/社科/生物医学类超边必须给出 measurements：accuracy、effect_size、p_value、
      frequency、score、sample_size、coverage、period 等。
    - conditions 用 {"key","operator","value","unit"}：key 用上面的标准键；
      operator 用 = / > / < / between / described_as；
      value 写原文数值或名称（如 "80" 或 "toluene"），unit 写单位或 null。
    - measurements 用 {"metric","value","unit","qualifier"}：value 写原文数值，
      qualifier 可写测定条件（如 "isolated"、"NMR"、"per 100 g"）。
    - 例子：80 °C、12 h、5 mol% Pd(PPh3)4、2.0 equiv Cs2CO3、toluene、under argon、
      收率 87%、ee 94%、dr > 20:1、p < 0.01、n = 120。
    - 严禁把上面这些数字塞进 attributes 或不写；conditions/measurements 空着等于丢数据。"""


REPORTING_PHRASE_PREFIXES = [
    "these results", "these findings", "these data", "these observations",
    "our results", "our data", "our findings", "our observations",
    "the results", "the findings", "the data", "the observation",
    "the histological", "histological analysis", "immunohistochemical",
    "the present study", "this study", "this paper", "this review", "this framework",
    "in this study", "we found", "we observed", "we demonstrated", "we show",
    "we demonstrate", "here, we demonstrate", "we have demonstrated",
    "we described", "we summarize", "we propose", "it was found",
    "analysis showed", "analysis revealed", "results showed", "results demonstrated",
    "results indicated", "findings showed", "findings suggest", "findings demonstrate",
    "data showed", "data revealed", "taken together", "collectively",
    "the aim of", "the goal of",
]
_REPORT_VERBS = re.compile(
    r"\b(show(s|ed)?|suggest(s|ed)?|indicat(es|ed)?|demonstrat(es|ed)?|reveal(s|ed)?|"
    r"found|observed|describe(s|d)?|summariz(e|es|ed)?|propose(s|d)?|highlight(s|ed)?|"
    r"aim(s|ed)?|was found|were found|were developed|was investigated)\b",
    re.IGNORECASE,
)
# CJK 字符（用于按语种区分单字实体的合法性）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def is_reporting_phrase(name: str | None) -> bool:
    """判断实体名是否为“衔接语/证据句/报告性短语”（应被过滤）。

    单字过滤按语种区分（P2-1）：中文单字术语（水/酶/铁/金/氮）是合法实体，
    只有拉丁文的单字符才视为噪声。
    """
    t = (name or "").strip()
    if not t:
        return True
    if len(t) < 2 and not _CJK_RE.search(t):
        return True
    low = t.lower()
    if any(low.startswith(p) for p in REPORTING_PHRASE_PREFIXES):
        return True
    head = " ".join(low.split()[:7])
    if _REPORT_VERBS.search(head):
        return True
    return False

RELATION_VOCAB = """关系类型必须从以下列表选择（若确无匹配才可新造 CamelCase 类型，且需在输出后解释原因，但尽量不新造）：
- uses(使用/采用)
- evaluates(评估/在…上评测)
- compares(比较)
- part_of(属于/组成部分)
- improves_upon(改进自/优于)
- based_on(基于/源自)
- causes(导致/促成/诱发；指因果)
- promotes(促进/增强/加速，如促进成骨、增强血管化)
- regulates(调控/调节)
- activates(激活)
- releases(释放/缓释，如药物/离子缓释)
- differentiates_into(分化为)
- inhibits(抑制)
- treats(治疗)
- targets(靶向/结合/作用于)
- has_property(具有属性/表现出)
- made_of(由…制成/组成)
- produced_by(由…产生/合成)
- related_to(相关/关联)
- cites(引用)
- published_in(发表于)
- authored_by(作者为)
- developed_by(由…开发)
- correlates_with(与…相关/随…变化；仅用于监测/共现，非因果)
- enables(使能/实现/支持某应用或功能)
- complicates(并发/加重某并发症)
- risk_factor_for(是…的风险因素)
- results_in(导致…结果：过程/干预 → 组织/临床结果)
- is_a(是…的一种：类型层级/上下位)

同义归一规则：以下表述必须归一到左侧词表词，禁止使用多个变体制造“假新关系”：
- employ / utilize / apply → uses
- assess / benchmark / test on / validate → evaluates
- consist of / composed of → made_of
- lead to / contribute to / trigger → causes
- promote / enhances / facilitate / accelerate / boost / induce / induced → promotes
- up-regulate / upregulate → regulates
- activate / activates → activates
- release / releases / elute / sustained release → releases
- differentiate into / differentiate to → differentiates_into
- suppress / downregulate → inhibits
- exhibit / possess / show → has_property
- derived from → based_on
- outperform / better than → improves_upon
- act on / bind / interact with → targets
- associated with / relate to → related_to
- compare with / versus → compares
- treat / cure → treats
- part of / belong to → part_of
- cite / reference → cites
- publish in / appear in → published_in
- author by / written by → authored_by
- develop / create / design → developed_by
- correlate / correlate with / track → correlates_with
- enable / allow / make possible → enables
- complicate / complication of → complicates
- risk factor for / predispose to → risk_factor_for
- result in / resulting in → results_in
- is a / is an / kind of / type of / subclass of → is_a

注意：同义归一后，type 字段必须使用左侧规范词（如 uses、evaluates、promotes），不得使用右侧原词。
predicate 字段可补充具体内容，但避免重复动词。
避免关系语义过宽：只要语义能落到某个具体词（如 promotes/inhibits/releases/differentiates_into），
就不要退回 related_to 或 causes；related_to 仅作兜底。"""


ATTRIBUTE_HINT = """属性（attributes）规范：
1. 只写有意义的值：值为 None/空串/空列表时省略该键；
2. 键名用 snake_case（如 fabrication_method、defect_site、cell_types_involved）；
3. 数值必须拆成 {"value": 数字, "unit": 单位}（如 "5 wt%" → {"value":5,"unit":"wt%"}）；
   时间用相对时长（P2D/PT16H）或写明 timepoint，不写自由长句；
4. 按类型尽量给出模板字段：
   - Material: composition/components/fabrication_method/architecture/application
   - Model: species/defect_site/model_type
   - Disease: etiology/pathology_site/related_signs
   - BiologicalProcess: regulators/cell_types_involved/downstream_outcome
   - Property: metric_type（值拆为 value+unit 或 rating）
5. 不要用“excellent/controllable”这类形容词冒充定量；形容词仅放 rating 并在 evidence 保留原文。"""


ERROR_LIST_HINT = """硬性禁区（ERROR LIST——违反任一条都必须改正后才能输出，不允许用“接近/相关”之类的说法蒙混）：
1. 【禁止报告语/衔接语当实体】实体 name 必须是被陈述的“事物名词/专名”。
   ✗ 错误：name="These results suggest ..." / "The histological analysis showed ..." /
     "In this study, we ..." / "We demonstrated that ..."
   ✓ 正确：name="Calcium phosphate cement"，把这些报告语表达放进 evidence/predicate。
2. 【禁止合并丢失细节】只有指向“同一个具体事物”（含其别名/缩写）才复用规范名。
   带实质修饰的不同对象必须分别建实体：Magnesium-doped CPC、Strontium-doped CPC、
   β-TCP/HA 复合支架 与 泛称 Calcium phosphate cement 是不同实体；
   组成/配比/掺杂/工艺写入 attributes。宁可多建实体，禁止“并入泛称”造成信息丢失。
3. 【禁止兜底关系放大】related_to 只在“原文语义确实没有更具体关系”时兜底；
   能落到 promotes/inhibits/releases/differentiates_into/enables/regulates/activates/
   results_in/risk_factor_for 等具体词，就必须用具体词；同义必须归一，禁止同一语义换词造“假新关系”。
4. 【禁止同一概念名漂移】同一概念在本文、与库中规范名之间必须统一字符串；
   不同写法放 aliases，不得在本篇内或跨篇使用变体造成重复节点。
5. 【禁止属性无证据/形容词冒充定量】数值属性要有原句支撑并拆 {value, unit}；
   "excellent/controllable" 之类形容词不得作为定量属性值，只能作 rating 并保留 evidence。
6. 【禁止把推测写成确定】"may/could/might/possibly" 等推测断言 confidence 不得 ≥0.8；
   review/commentary 类文献的断言整体下调一档。
7. 【禁止断连/字符串不一致】relation.subject/object 与 event.participants 必须与
   entities 或「库中已有规范名」字符串完全一致（含大小写与空格），不许用变体或缩写。
8. 【禁止伪造证据】evidence 必须引用原文句子（可节选，≤300 字符），不得改写/总结/编造。"""


SELF_CHECK_HINT = """输出前请逐条自查（违反硬性禁区或下面任一条，先修正再输出；不要带着问题交付）：
1. entity.name 均为名词性术语/专名：非句子、非衔接语、非报告语（对照 ERROR LIST #1）；
2. 同一概念已复用「库中已有规范名」，不同配方/掺杂/比例/工艺未误并入泛称（#2）；
3. relation.type 来自受控词表且已同义归一，无兜底关系滥用；subject/object 与 entities 或库中名称完全一致（#3/#4/#7）；
4. event.trigger 是精简名词短语，participants ≤5 且指向实体；
5. attributes 无 None/空值，数值已拆 {value, unit}，键名为 snake_case（#5）；
6. evidence 引用原文句子；推测性断言与 review/commentary 文献已下调 confidence（#6/#8）。"""


GENERIC_FALLBACK_TYPES = frozenset({"related_to"})


def _paper_header(meta: dict[str, Any] | None) -> str:
    meta = meta or {}
    return (
        f"论文: {meta.get('title', '未知')} | 期刊: {meta.get('venue', '未知')} "
        f"| 年份: {meta.get('pub_year', '未知')}"
    )


def _domain_profile_hint(domain_profile: dict[str, Any] | None) -> str | None:
    if not domain_profile:
        return None
    kind = str(domain_profile.get("domain_kind") or "").strip()
    if kind in ("", "general"):
        return None
    label = domain_profile.get("label") or kind
    status = str(domain_profile.get("schema_status") or "candidate")
    lines = [
        f"本任务领域画像：{kind}（{label}）",
        f"schema_status：{status}",
    ]
    if status == "frozen":
        lines.append("以下为本领域已冻结 schema，只允许使用其中的类型，不得新增：")
    else:
        lines.append("以下为本领域候选 schema，首次抽取可参考；确需新增时需说明理由：")
    entities = [str(x) for x in domain_profile.get("candidate_entity_types") or []]
    relations = [str(x) for x in domain_profile.get("candidate_relation_types") or []]
    if entities:
        lines.append("候选/冻结实体类型: " + ", ".join(entities))
    if relations:
        lines.append("候选/冻结关系类型: " + ", ".join(relations))
    return "\n".join(lines)


def build_prompt(paragraphs: list[str], paper_meta: dict[str, Any] | None = None,
                 existing_entities: list[str] | None = None,
                 recent_generic_warning: str | None = None,
                 domain_profile: dict[str, Any] | None = None) -> str:
    header = _paper_header(paper_meta)
    text = "\n\n".join(paragraphs)
    parts = [SYSTEM_HINT, SCHEMA_HINT, ATTRIBUTE_HINT, RELATION_VOCAB,
             ERROR_LIST_HINT,
             SELF_CHECK_HINT, header]
    if existing_entities:
        parts.append(
            "库中已有（尽量复用的）规范实体（Type: Name）：\n"
            + "\n".join(existing_entities[:150])
            + "\n（若本文出现同一概念，请复用其规范名并把本文写法加入 aliases）"
        )
    if recent_generic_warning:
        parts.append(recent_generic_warning)
    domain_hint = _domain_profile_hint(domain_profile)
    if domain_hint:
        parts.append(domain_hint)
    parts.append(f"----------------\n论文段落：\n{text}\n----------------\n输出 JSON:")
    return "\n\n".join(parts)


def parse_model_json(raw: str) -> dict[str, Any]:
    """容忍地解析模型输出为 dict：剥除代码围栏、截取首个 {…}。"""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text).strip()
    text = re.sub(r"\s*```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("模型输出中未找到 JSON 对象")
    fragment = text[start:end + 1]
    try:
        data = json.loads(fragment)
    except json.JSONDecodeError:
        # 某些中文长文本会带回车等控制字符，宽松模式仍可解析。
        data = json.loads(fragment, strict=False)
    if not isinstance(data, dict):
        raise ValueError("JSON 根节点不是对象")
    data.setdefault("entities", [])
    data.setdefault("relations", [])
    data.setdefault("events", [])
    data.setdefault("hyperedges", [])
    return data


def _conf(value: Any) -> float:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, c))


def blend_confidence(model_conf: float, quality_q: float | None,
                     flagged: bool, settings=None) -> float:
    """融合置信度 = 0.6*模型自评 + 0.4*文献质量权重。

    文献质量权重 = Q（若为“标记后发送”，按 q_flag_penalty 折扣），体现
    “根据质量评估结果和文本逻辑给出置信度”。
    """
    settings = settings
    if settings is None:
        from research_agent.config import settings as _s
        settings = _s
    q = quality_q if quality_q is not None else 0.5
    qw = q * (settings.q_flag_penalty if flagged else 1.0)
    return round(max(0.0, min(1.0, 0.6 * model_conf + 0.4 * qw)), 3)


def flag_issues(data: dict[str, Any], min_conf: float = 0.6,
                fallback_types=(GENERIC_FALLBACK_TYPES,),
                max_items: int = 12) -> list[str]:
    """找出需要二次精修的问题（低置信 / 泛化兜底关系 / 报告语实体）。"""
    fb: set[str] = set()
    for ft in fallback_types:
        fb |= set(ft or ())
    if not fb:
        fb = set(GENERIC_FALLBACK_TYPES)
    issues: list[str] = []
    for i, e in enumerate(data.get("entities") or []):
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        if not name:
            issues.append(f"实体[{i}] name 为空")
            continue
        if is_reporting_phrase(name):
            issues.append(
                f"实体[{i}] name 疑似报告语/整句（ERROR LIST #1），"
                f"应改为被陈述的核心事物名词: {name[:90]}")
            continue
        c = _conf(e.get("confidence"))
        if c < min_conf:
            issues.append(
                f"实体[{i}] 「{name[:70]}」置信度偏低({c})：确为原文直接陈述则提高并保留证据，"
                f"确为推断则如实低置信或删除")
    for i, r in enumerate(data.get("relations") or []):
        if not isinstance(r, dict):
            continue
        rt = str(r.get("type") or "").strip().lower()
        subj = str(r.get("subject") or "")[:50]
        obj = str(r.get("object") or "")[:50]
        c = _conf(r.get("confidence"))
        if rt in fb:
            issues.append(
                f"关系[{i}] 「{subj} --{rt}--> {obj}」语义偏泛化（ERROR LIST #3）："
                f"请重读原文，能落到 promotes/inhibits/releases/differentiates_into/enables/"
                f"regulates/activates/results_in 等具体关系就替换")
            continue
        if c < min_conf:
            issues.append(f"关系[{i}] 「{subj} -{rt}-> {obj}」置信度偏低({c})")
    for i, ev in enumerate(data.get("events") or []):
        if not isinstance(ev, dict):
            continue
        c = _conf(ev.get("confidence"))
        if c < min_conf:
            tr = str(ev.get("trigger") or "")[:60]
            issues.append(f"事件[{i}] 「{tr}」置信度偏低({c})")
    _flag_missing_quantities(data, issues)
    return issues[: max(0, int(max_items))]


# 出现这些线索说明原文里有可量化信息，conditions/measurements 不该为空
_QUANTITY_HINTS = (
    "°c", "℃", " mol%", "mol %", " equiv", " h ", " min", "toluene", "thf",
    "dcm", "dce", "dmf", "dioxane", "yield", "收率", "产率", "%", "ee", "dr ",
    "p <", "p =", "p=", "n =", "n=", "小时", "当量", "催化剂", "溶剂", "温度",
)


def _flag_missing_quantities(data: dict[str, Any], issues: list[str]) -> None:
    """超边有量化线索但没有 conditions/measurements 时，点名要求补填。"""
    for i, h in enumerate(data.get("hyperedges") or []):
        if not isinstance(h, dict):
            continue
        conditions = h.get("conditions") or []
        measurements = h.get("measurements") or []
        if conditions and measurements:
            continue
        blob = " ".join([
            str(h.get("label") or ""), str(h.get("evidence") or ""),
            " ".join(str(m.get("name") or "") for m in h.get("members") or []
                     if isinstance(m, dict)),
        ]).lower()
        if not any(hint in blob for hint in _QUANTITY_HINTS):
            continue
        missing = []
        if not conditions:
            missing.append("conditions")
        if not measurements:
            missing.append("measurements")
        issues.append(
            f"超边[{i}] 「{str(h.get('label') or '')[:50]}」含可量化信息但 "
            f"{'/'.join(missing)} 为空（SCHEMA #13）：请从原文抽出温度/时间/溶剂/"
            f"催化剂/当量/产率/选择性并补齐；数值必须来自原文")


REFINE_PROMPT = """你正在对一次知识抽取结果做“定向精修”（科研知识抽取流水线的反思环节，第二遍）。
{header}

原文（与首遍抽取相同的输入）：
{text}

首遍抽取结果（JSON——只处理下面被点名的问题，未点名的条目一律原样保留）：
{first_json}

首遍质检发现的问题：
{issues}

精修要求：
1. 只处理上面点名的条目；其它条目的 type/name/confidence/evidence 一个字符都不许改。
2. 粘住首遍抽取的意图（spirit）；除非有明显错误，否则不要重构、不要新增实体/关系/事件。
3. 常见修复方式（按需采用）：
   - 实体名是报告语/衔接语/整句（如 "These results suggest ..."）→ 改为被陈述的核心事物名词，
     把原句放进 evidence；
   - 泛称材料实际涵盖多种配方/掺杂/比例/工艺 → 拆分成独立实体，组成细节写入 attributes，
     禁止把不同配方并入泛称造成细节丢失；
   - related_to 等兜底关系若能落到更具体关系 → 替换为具体关系；
   - 置信度偏低 → 确为原文直接陈述则提高并保留 evidence；确为推断则如实标低置信或删除；
   - 同一概念与库中规范名不一致 → 改为规范名并补 aliases；
   - conditions/measurements 为空但原文有温度、时间、溶剂、催化剂、当量、产率、
     选择性、p 值等数字 → 按 SCHEMA 第 13 条补齐，数值必须来自原文。
4. 没有可改进项时：原样输出首遍 JSON（不增删改任何字符），并在 JSON 之后另起一行输出：
   I am done
5. 只输出精修后的 JSON 对象；不要代码块、不要解释、不要额外文字
   （"I am done" 只能出现在 JSON 之后）。

输出精修后的 JSON："""


def _dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


class KnowledgeExtractor:
    """按文本块调用模型并解析结构化结果。model 需有 invoke([HumanMessage])。"""

    def __init__(self, model, settings=None) -> None:
        self.model = model
        if settings is None:
            from research_agent.config import settings as _s
            settings = _s
        self.settings = settings

    def _run(self, prompt: str) -> dict[str, Any]:
        """调用模型并解析 JSON；解析失败抛异常（由调用方决定回退策略）。"""
        msg = self.model.invoke([HumanMessage(content=prompt)])
        raw = msg.content if isinstance(msg, AIMessage) else str(getattr(msg, "content", msg))
        return parse_model_json(raw)

    def extract(self, paragraphs: list[str],
                paper_meta: dict[str, Any] | None = None,
                existing_entities: list[str] | None = None,
                recent_generic_warning: str | None = None,
                domain_profile: dict[str, Any] | None = None) -> dict[str, Any]:
        """单遍抽取（无精修），返回结构化 JSON。"""
        prompt = build_prompt(paragraphs, paper_meta, existing_entities,
                              recent_generic_warning, domain_profile)
        try:
            return self._run(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 抽取失败: %s", exc)
            return {"entities": [], "relations": [], "events": [], "error": str(exc)}

    def extract_with_refine(self, paragraphs: list[str],
                            paper_meta: dict[str, Any] | None = None,
                            existing_entities: list[str] | None = None,
                            recent_generic_warning: str | None = None,
                            domain_profile: dict[str, Any] | None = None,
                            ) -> tuple[dict[str, Any], dict[str, Any]]:
        """首遍抽取 + 低置信/泛化关系定向精修（v0.0.6）。

        返回 (最终 JSON, refine_stats)。精修解析失败/无改进时回退首遍结果，保证鲁棒。
        """
        s = self.settings
        stats: dict[str, Any] = {
            "refined": False, "issues": 0, "attempts": 0,
            "converged": False, "failed_attempts": 0,
        }
        base_prompt = build_prompt(paragraphs, paper_meta, existing_entities,
                                   recent_generic_warning, domain_profile)
        try:
            first = self._run(base_prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 抽取失败(首遍): %s", exc)
            return {"entities": [], "relations": [], "events": [],
                    "error": str(exc)}, stats

        if not getattr(s, "knowledge_refine_enabled", True):
            return first, stats

        issues = flag_issues(
            first,
            min_conf=getattr(s, "refine_min_conf", 0.6),
            fallback_types=(getattr(s, "generic_fallback_types",
                                     GENERIC_FALLBACK_TYPES),),
            max_items=getattr(s, "refine_max_items", 12),
        )
        stats["issues"] = len(issues)
        if not issues:
            return first, stats

        text = "\n\n".join(paragraphs)
        current = first
        max_attempts = max(1, int(getattr(s, "refine_max_attempts", 2)))
        for _ in range(max_attempts):
            prompt = REFINE_PROMPT.format(
                header=_paper_header(paper_meta),
                text=text,
                first_json=_dump_json(current),
                issues="\n".join(f"- {it}" for it in issues),
            )
            try:
                refined = self._run(prompt)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM 精修失败，回退首遍: %s", exc)
                stats["failed_attempts"] += 1
                break
            stats["attempts"] += 1
            if refined == current:
                stats["converged"] = True
                break
            current = refined
            stats["refined"] = True
            next_issues = flag_issues(
                refined,
                min_conf=getattr(s, "refine_min_conf", 0.6),
                fallback_types=(getattr(s, "generic_fallback_types",
                                         GENERIC_FALLBACK_TYPES),),
                max_items=getattr(s, "refine_max_items", 12),
            )
            if not next_issues or set(next_issues) == set(issues):
                break
            issues = next_issues
        if stats["attempts"] and not stats["refined"] and not stats["converged"]:
            # 出现精修调用但既未改进也未收敛（理论上不会走到这），如实记录
            stats["converged"] = True
        return current, stats
