# research-agent 全部 LLM 提示词审阅稿

> 版本：v0.0.6.1（tag `v0.0.6.1`，main `e20df84`）
> 范围：文献检索 / 质量评估 / 知识提取 三节点**当前生效的全部 LLM 提示词**
> 说明：所有提示词均以「单条 HumanMessage」发送（无独立 system 消息）；模型输出由代码做 JSON 容错解析，失败回退确定性实现。本文「原文」块与源码常量逐字一致（由脚本校验）。

## 模型绑定（当前 .env / 代码默认）

| 节点 | Provider | 模型 | 所需 Key | 代码源 |
|---|---|---|---|---|
| 文献检索 | deepseek | `deepseek-v4-pro`（注释建议可换 flash） | `DEEPSEEK_API_KEY` | `models.py` |
| 质量评估 | glm | `glm-4.7-flash` | `ZHIPU_API_KEY` | `models.py` |
| 知识提取 | deepseek | `deepseek-v4-pro`（临时替代 gpt-5.6） | `DEEPSEEK_API_KEY` | `models.py` |

> 可用环境变量覆盖：`RETRIEVAL_MODEL / QUALITY_MODEL / KNOWLEDGE_MODEL`、`ROLE_PROVIDER_*`。
> 检索/知识节点若缺 Key 或输出不可解析，回退到确定性实现，不影响主流程。

---

## 一、文献检索节点（DeepSeek V4 Pro）

文件：`src/research_agent/retrieval/llm.py`
该节点共 2 个 LLM 提示词：**查询规划** 与 **元数据规整**。真正检索/下载由确定性 API（PubMed/Crossref 等）执行，LLM 只负责「动脑」。

### 1.1 查询规划 `PLAN_PROMPT_TEMPLATE`

用途：把简短研究主题拆成 4–6 条覆盖多子领域的英文检索式，供 PubMed 轮询。
运行时注入：`{topic}` ← 用户主题；`{date}` ← 当天日期 `YYYY-MM-DD`（v0.0.6 加入 PubMed 语法提示）。

```text
你是科研文献检索规划器。用户会给出一个简短的研究主题（可能只有几个词，如“新型骨修复生物材料”）。你需要基于该主题，生成一组英文检索式，每个检索式聚焦一个不同的研究子领域或维度，以便系统逐个执行检索（轮询），全面获取相关文献。

你需要考虑以下子领域维度（根据主题自动选择相关项，尽可能覆盖更多，但每条检索式只聚焦1-2个维度，避免混合过多概念导致结果不相关）：
- 核心关键词：主题本身、同义词、近义词、上下位概念。
- 方法与技术：涉及的主要方法、技术、模型、工具、工艺。
- 应用与场景：主要应用领域、适应症、使用场景、目标对象。
- 机理与原理：作用机制、原理、理论、结构-功能关系。
- 性能与特性：活性、稳定性、生物相容性、力学性能、耐久性等（根据主题调整性能指标）。
- 变种与拓展：不同材料类型、改进型、衍生技术、复合体系。
- 优化与调控：性能优化、配方调整、工艺改进、参数优化。
- 产业化与转化：生产工艺、规模化、成本、临床转化、商业化、审评审批。
- 评价与标准：安全性评价、质量控制、标准、测试方法。

要求：
1. 生成 4-6 条英文检索式（如果主题较窄，至少3条；如果主题宽泛，可适当增加，但不超过6条）。
2. 每条检索式必须明确对应上述一个或两个紧密相关的子领域，用核心主题词与子领域术语组合，例如：
   - 主题词 AND 子领域关键词
   - 主题词 AND (子领域关键词1 OR 关键词2)
3. 整体上，这些检索式应覆盖核心关键词、方法、应用，并至少触及其他两个子领域（如性能、机理、产业化等），确保检索的全面性。
4. 每条检索式应为完整的英文查询字符串，可使用布尔运算符（AND、OR、NOT）、双引号短语、通配符（*）等常规检索语法。
5. 避免不同检索式之间关键词大量重复，每个子领域的检索应相对独立。
6. 只输出 JSON 数组字符串，数组元素为字符串，例如：["bone repair biomaterials AND bioactivity", "osteogenic scaffolds AND mechanical properties", "biodegradable bone graft AND clinical translation", "bone regeneration AND osteoinductive mechanism"]。
7. 不要输出任何解释、注释或额外文本，确保输出可直接被 JSON 解析。
8. 目标文献库为 PubMed：AND/OR/NOT、双引号短语、通配符* 与字段限定 [Title/Abstract] 均受支持；
   如需突出近期进展可在检索式中加 [dp] 年份过滤。当前日期：{date}。

主题：{topic}
```

### 1.2 元数据规整 `CLEAN_PROMPT_TEMPLATE`

用途：把多源原始元数据规整为规范字段（标题/期刊/作者/机构/DOI/发表情况），供质量节点校验；字段缺漏再由质量节点发回检索节点补全。
运行时注入：`{payload}` ← 代码挑选的元数据子集 JSON（title/doi/venue/venue_issn/source_type/publication_status/pub_year/pub_date/authors）。

```text
你是文献元数据规整器。请根据下面的原始元数据 JSON，输出规范化后的 JSON 对象。

输出格式必须严格符合以下结构：
{
  "title": "标准标题",
  "venue": "期刊/会议/预印本库名",
  "venue_issn": "ISSN 或 null",
  "source_type": "journal|repository|proceedings",
  "publication_status": "Published|Preprint|In Press",
  "pub_year": 2025,
  "pub_date": "YYYY-MM-DD 或 null",
  "doi": "DOI 或 null",
  "authors": [{"name": "...", "orcid": null, "affiliations": ["机构全称"]}]
}

处理规则：
1. 标题和期刊名标准化大小写：标题采用每个主要单词首字母大写（Title Case），保留专有名词和缩写原样；期刊名保持官方大小写（如已知），否则使用 Title Case。
2. 作者信息解析：
   - 若原始元数据中作者缺失或为空，则输出 [{"name": "Unknown", "orcid": null, "affiliations": []}]。
   - 若作者姓名格式为 "Last, First" 或 "First Last"，统一转换为 "First Last"（姓名顺序）。
   - 作者 affiliations 应提取为字符串数组，每个元素为机构全称；若有多位作者，保持原始顺序。
   - orcid 仅在明确提供时填写，否则为 null。
3. 对于 venue_issn 和 doi：只使用原始元数据中明确给出的值；若无法确定，必须输出 null，不得编造。
4. source_type 判断优先级：若元数据包含期刊信息且 DOI 以 10.xxxx 开头且发布在期刊平台，则为 "journal"；若来自 arXiv/bioRxiv/medRxiv 等预印本库，则为 "repository"；若来自会议论文集（如 ACM/IEEE 会议），则为 "proceedings"；无法判断时根据元数据中的 container 字段推断，仍然不确定则输出 "journal"（或 null？建议选择最可能的并可在后续人工复核）。
5. publication_status 判断：若元数据中有明确的出版状态标签（如 "Published"、"Preprint"、"In Press"），直接采用；否则根据发布日期和 DOI 是否存在推断：有 DOI 且有正式卷期页码 → "Published"；来自预印本库且无 DOI → "Preprint"；有接收日期无出版日期 → "In Press"。
6. pub_year 必须为四位整数，若无法确定年份则输出 null（而非 0）。
7. pub_date 格式为 "YYYY-MM-DD"，若只有年份或月份，可补全为当年1月1日或当月1日，并在后续人工校验；完全缺失则 null。
8. 只输出 JSON 对象，不要包含任何额外文字、注释或代码块标记。

原始元数据：{payload}
```

---

## 二、质量评估节点（GLM 4.7 Flash）

文件：`src/research_agent/quality/llm.py`

### 2.1 评分因子输出 `QUALITY_PROMPT_TEMPLATE`

用途：GLM 依据文献元数据给出权威性子项评分与学科速度判断。注意：**LLM 只输出因子，A/T/Q 由代码按固定公式计算与路由**，保证可复现：
`A = 0.5*venue_factor + 0.3*h_factor + 0.2*citation_factor`；`Q = 0.6*A + 0.4*T`；T 由出版年份+学科半衰期计算。
运行时注入：`{payload}` ← title/venue/venue_issn/source_type/pub_year/citation_count/avg_h_index/author_h_indices/author_count。

```text
你是科研文献质量评估专家。请基于下面的文献元数据 JSON，输出一个严格的 JSON 对象，不要包含任何额外文字、代码块标记或注释。字段与取值要求如下：

{
  "venue_quartile": "JCR/SCI 分区，Q1-Q4；若无法确定（如预印本、非 SCI 期刊）填 null",
  "venue_factor": 0-1 小数，表示期刊/出版社权威性，预印本默认 0.45-0.55,
  "h_factor": 0-1 小数，表示作者团队学术影响力（综合 H 指数、团队规模、机构声誉）,
  "citation_factor": 0-1 小数，表示被引情况（结合该领域同年份论文的相对被引位置）,
  "field_velocity": "fast|medium|slow"（该文所属学科前沿迭代速度）,
  "venue_note": "一句话说明分区判断依据，若缺失填 'Not available'",
  "rationale": "两句话以内的评估理由，简要解释各因子取值依据"
}

评分规则与标尺（请遵循以下基准，但不必机械照搬，可根据元数据实际情况微调）：

1. venue_factor：
   - 0.90-0.95：顶刊/顶会/学会旗舰（如 Nature、Science、NeurIPS 等）
   - 0.78-0.88：领域主流 Q1 期刊或 A 类会议
   - 0.65-0.75：一般 Q2 期刊或中等会议
   - 0.50-0.60：预印本、未知来源或低影响期刊
   - 若无任何信息，默认 0.5

2. h_factor：
   - 0.9 ≈ H≥60 的资深团队或知名机构
   - 0.7 ≈ H 20-40 的中坚团队
   - 0.5 ≈ H 5-15 的普通团队
   - 0.4 ≈ 新团队、信息缺失或作者列表未提供
   - 可结合作者数量、机构排名微调 ±0.05

3. citation_factor：
   - 前 1% ≈ 0.95，前 10% ≈ 0.8，前 50% ≈ 0.6，接近 0 引用 ≈ 0.3
   - 若元数据未提供被引次数，按论文发表年份和期刊水平估计：顶刊新论文可暂给 0.6-0.7，预印本新论文给 0.4，较早论文若无被引数据则给 0.3-0.5
   - 注意不同领域引用速度不同，fast 领域早期引用多，slow 领域引用积累慢，可适当调整

4. field_velocity：
   - fast：AI/CS、量子计算等快速迭代领域
   - medium：生物医药/材料/化学等
   - slow：数学/基础理论/传统工程等
   - 依据期刊名称、标题关键词、会议主题综合判断

5. 重要约束：
   - 所有分值必须在 0-1 范围内，保留两位小数
   - 不得编造缺失信息（如 DOI、被引次数、H 指数）；若元数据缺失关键字段，给出合理默认值并在 rationale 中说明
   - 输出必须是合法 JSON，键名与顺序如上，不要有尾逗号

文献元数据：{payload}
```

---

## 三、知识提取节点（DeepSeek V4 Pro）

文件：`src/research_agent/knowledge/extractor.py`
该节点提示词由多个常量**按顺序拼接**为一个完整用户消息，另含一个**二次精修（reflection）提示词**。运行时先做确定性文本预处理（分句/分段/分块），再调用 LLM。

### 3.0 拼接顺序（`build_prompt`）

```text
[SYSTEM_HINT] + [SCHEMA_HINT] + [ATTRIBUTE_HINT] + [RELATION_VOCAB]
+ [ERROR_LIST_HINT] + [SELF_CHECK_HINT]
+ 论文头（标题 | 期刊 | 年份）
+ [库中已有规范实体块]（若库中已有）
+ [语料兜底关系提醒]（v0.0.6 起，若库内有 related_to 类边）
+ "----------------\n论文段落：\n<正文>\n----------------\n输出 JSON:"
```

### 3.1 系统/角色原则 `SYSTEM_HINT`

```text
你是科研知识抽取与本体构建引擎。你的任务是从单篇论文中抽取结构化事实，输出可合并进统一科研知识图谱的 JSON。
核心原则：
- 只抽取文中明确陈述或直接可推断的内容，禁止臆造、补全或泛化。
- 优先复用库中已有规范实体（运行时提供），确保同一概念在不同文献中使用相同规范名，避免重复创建。
- 抽取粒度应足够细，能支持后续推理（如方法-材料-性能-应用之间的关联），而非仅概括主题。
- 同时识别并抽取论文中的关键事件（如实验、发现、临床试验），它们可能表达重要的过程性知识。
```

### 3.2 实体类型候选表 `ENTITY_TYPES`

```text
Method, Material, Device, Drug, Disease, Model, Metric, Dataset, Task, Theory, Parameter, Property, Application, Organism, CellLine, Chemical, Target, BiologicalProcess, Technology, Tool, Standard, Regulation, Institution, Researcher
```

### 3.3 输出 schema + 硬性要求 `SCHEMA_HINT`

```text
请严格输出一个 JSON 对象（不要输出其它文字、不要 markdown 代码块），结构如下：
{
  "entities": [
    {
      "type": "受控类型；从下方类型列表选择，若确有必要才新造英文 CamelCase",
      "name": "规范名：优先复用库中已有规范名；否则使用该领域最标准、无歧义的写法",
      "aliases": ["该实体在文中出现的其它写法/缩写，如 RAG、additive manufacturing"],
      "attributes": {"属性名": "值", "属性名2": "值2"},
      "confidence": 0.0,
      "evidence": "支撑该实体的原句（可截断，≤300字符）"
    }
  ],
  "relations": [
    {
      "type": "受控词表中的关系词（必须按同义归一规则选择）",
      "subject": "entities.name 或库中已有规范名（必须与 entities 列表中 name 完全一致）",
      "predicate": "一句话补述，不要与 type 重复表达同一动词，例如 type=uses 时 predicate 可为 '用于合成骨支架'",
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
  ]
}

实体类型建议列表（优先选择，若都不匹配再自造）：
Method, Material, Device, Drug, Disease, Model, Metric, Dataset, Task, Theory,
Parameter, Property, Application, Organism, CellLine, Chemical, Target,
BiologicalProcess, Technology, Tool, Standard, Regulation, Institution, Researcher

硬性要求：
1. 连通性：每条 relation 的 subject 和 object，以及每个 event 的 participants，
   必须与 entities 列表中的 name 或「库中已有规范名」字符串完全相同（包括大小写和空格）。
   不要使用变体或缩写。
2. 每个实体尽量至少出现在一条 relation 或 event 中；确实无法关联的才作为孤立实体输出。
3. 同一概念合并：若论文中的某个概念与库中已有规范名是同一事物（包括其别名、缩写），
   则 name 必须直接复用库中规范名，并将本文中的写法加入 aliases 数组。例如库中已有
   "Method: Retrieval-Augmented Generation"，论文中写 "RAG"，则 name 应为
   "Retrieval-Augmented Generation"，aliases 包含 "RAG"。
4. 实体命名：优先使用领域通用、无歧义的标准名称；缩写需在 name 或 aliases 中给出全称。
   例如 name 可为 "Poly(lactic-co-glycolic acid)"，aliases 含 "PLGA"。
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
```

### 3.4 关系词表与同义归一 `RELATION_VOCAB`

```text
关系类型必须从以下列表选择（若确无匹配才可新造 CamelCase 类型，且需在输出后解释原因，但尽量不新造）：
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
就不要退回 related_to 或 causes；related_to 仅作兜底。
```

### 3.5 属性规范 `ATTRIBUTE_HINT`

```text
属性（attributes）规范：
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
5. 不要用“excellent/controllable”这类形容词冒充定量；形容词仅放 rating 并在 evidence 保留原文。
```

### 3.6 硬性禁区 `ERROR_LIST_HINT`（v0.0.6 新增）

```text
硬性禁区（ERROR LIST——违反任一条都必须改正后才能输出，不允许用“接近/相关”之类的说法蒙混）：
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
8. 【禁止伪造证据】evidence 必须引用原文句子（可节选，≤300 字符），不得改写/总结/编造。
```

### 3.7 输出前自检 `SELF_CHECK_HINT`

```text
输出前请逐条自查（违反硬性禁区或下面任一条，先修正再输出；不要带着问题交付）：
1. entity.name 均为名词性术语/专名：非句子、非衔接语、非报告语（对照 ERROR LIST #1）；
2. 同一概念已复用「库中已有规范名」，不同配方/掺杂/比例/工艺未误并入泛称（#2）；
3. relation.type 来自受控词表且已同义归一，无兜底关系滥用；subject/object 与 entities 或库中名称完全一致（#3/#4/#7）；
4. event.trigger 是精简名词短语，participants ≤5 且指向实体；
5. attributes 无 None/空值，数值已拆 {value, unit}，键名为 snake_case（#5）；
6. evidence 引用原文句子；推测性断言与 review/commentary 文献已下调 confidence（#6/#8）。
```

### 3.8 二次精修（reflection）`REFINE_PROMPT`（v0.0.6 新增）

触发条件（`flag_issues`）：某块首遍抽取存在 ① 实体/关系/事件置信度 < `refine_min_conf`（默认 0.6）② 兜底泛化关系（默认 `related_to`）③ 疑似报告语实体名。
循环上限：`refine_max_attempts`（默认 2）；无改进（模型原样返回）即收敛；解析失败自动回退首遍结果。
占位：`{header}` 论文头 / `{text}` 原文 / `{first_json}` 上一版 JSON / `{issues}` 点名问题清单。

```text
你正在对一次知识抽取结果做“定向精修”（科研知识抽取流水线的反思环节，第二遍）。
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
   - 同一概念与库中规范名不一致 → 改为规范名并补 aliases。
4. 没有可改进项时：原样输出首遍 JSON（不增删改任何字符），并在 JSON 之后另起一行输出：
   I am done
5. 只输出精修后的 JSON 对象；不要代码块、不要解释、不要额外文字
   （"I am done" 只能出现在 JSON 之后）。

输出精修后的 JSON：
```

### 3.9 运行时注入（非独立提示词，但属于发给 LLM 的上下文）

```text
① 论文头（_paper_header）：
论文: <title> | 期刊: <venue> | 年份: <pub_year>

② 库中已有规范实体块（existing_entities，最近有连接的 ≤150 条）：
库中已有（尽量复用的）规范实体（Type: Name）：
<Type: Name …每行一条>
（若本文出现同一概念，请复用其规范名并把本文写法加入 aliases）

③ 语料兜底关系提醒（recent_generic_warning，v0.0.6，仅当库内存在 related_to 类边时）：
语料提醒：本库已处理约 <N> 篇，以下兜底关系已存在，请勿继续无谓放大：
  - related_to: <K> 条
（ERROR LIST #3）只有当原文语义确实没有更具体关系时，才新增这类兜底关系；
能落到 promotes/inhibits/releases/differentiates_into/enables/regulates/activates/
results_in 等具体关系，就必须用具体关系。

④ 正文包裹：
----------------
论文段落：
<正文>
----------------
输出 JSON:
```

### 3.10 影响输出的确定性守卫（非 LLM，但建议审阅时一并了解）

- 实体名/事件 trigger 的**报告语过滤**：`is_reporting_phrase()` + `REPORTING_PHRASE_PREFIXES`（these results / the histological analysis showed / we demonstrated … 等约 40 前缀）+ 正则 `_REPORT_VERBS`（show/suggest/indicate/demonstrate/reveal/found/…）→ 命中即丢弃（计入 `dropped_garbage`）。
- **置信度融合**：入库置信度 = `0.6*模型自评 + 0.4*文献质量权重`；质量权重 = Q（标记后发送×0.85 折扣）。
- **强断言门控**：promotes/regulates/… 等强关系若融合置信度 < 0.72 → 标记 `candidate`（不直接采信）。
- **证据等级**：按标题判定 review / commentary / clinical / primary_in_vivo / primary_in_vitro / primary，写入边与事件。

---

## 四、提示词相关配置速查

| 配置项 | 默认 | 作用 | 环境变量 |
|---|---|---|---|
| `max_extract_chars` | 8000 | 单次抽取文本窗口 | — |
| `max_extract_chunks` | 4 | 每篇最多抽取块数 | — |
| `knowledge_refine_enabled` | True | 是否启用二次精修 | `RA_KNOWLEDGE_REFINE` |
| `refine_min_conf` | 0.6 | 低置信触发阈值 | `RA_REFINE_MIN_CONF` |
| `refine_max_attempts` | 2 | 精修最大轮数 | `RA_REFINE_MAX_ATTEMPTS` |
| `refine_max_items` | 12 | 单轮最多点名问题数 | `RA_REFINE_MAX_ITEMS` |
| `generic_fallback_types` | `("related_to",)` | 视为泛化的兜底关系 | — |
| 质量阈值 | 0.8 / 0.5 | Q≥0.8 直送；0.5≤Q<0.8 标记；Q<0.5 人工 | `RA_THRESHOLD_*` |
| 质量公式 | — | A=0.5V+0.3H+0.2C；Q=0.6A+0.4T | `RA_Q_WEIGHT_*`、`RA_A_*_W` |

## 五、提示词清单总览（速览表）

| # | 提示词 | 节点 | 输入注入 | 输出 | 代码位置 |
|---|---|---|---|---|---|
| 1.1 | 查询规划 | 检索 | {topic},{date} | JSON 数组(4-6 条检索式) | `retrieval/llm.py::PLAN_PROMPT_TEMPLATE` |
| 1.2 | 元数据规整 | 检索 | {payload} | 规范元数据 JSON | `retrieval/llm.py::CLEAN_PROMPT_TEMPLATE` |
| 2.1 | 质量因子评分 | 质量 | {payload} | 因子 JSON（A/T/Q 由代码算） | `quality/llm.py::QUALITY_PROMPT_TEMPLATE` |
| 3.x | 知识抽取（8 段拼装） | 知识 | 论文头/库中实体/语料提醒/正文 | entities/relations/events JSON | `knowledge/extractor.py::build_prompt` |
| 3.8 | 二次精修 | 知识 | header/text/first_json/issues | 精修后 JSON / I am done | `knowledge/extractor.py::REFINE_PROMPT` |

> 对应 skills 副本：`skills/{retrieval-planner,metadata-normalizer,quality-assessor,knowledge-extractor}/SKILL.md`（说明版，非逐字源；单一事实源在代码常量）。

---

## 六、遗留最小演示（非当前主流水线，`demo.py`）

文件：`src/research_agent/demo.py`
说明：这是早期的 fan-out/join 多模型演示（研究员A/研究员B → 主持人），由入口 `uv run research-agent` 触发；**不参与**当前「文献检索 → 质量评估 → 知识提取」主流水线。三处提示词由 f-string 直接构造（非模板常量），此处收录组装后形态。

### 6.1 研究员 A（理论/方法视角）

```text
科研主题：<topic>

你是一名【理论/方法学】研究员。请从算法架构、prompt 工程、
可解释性与评测方法等角度，给出 3 点有洞察的分析。
```

### 6.2 研究员 B（实验/应用视角）

```text
科研主题：<topic>

你是一名【实验/应用】研究员。请从典型实验设计、落地案例、
数据与计算资源约束等角度，给出 3 点有洞察的分析。
```

### 6.3 主持人（综合成稿）

```text
科研主题：<topic>

你是科研主持/主编。请整合下面两位研究员的草稿，去重、组织为一份
结构化的中文综述（含 小节标题 + 要点 + 简短结论），并标注两视角的分歧点（如有）。

<drafts: 草稿1…草稿N>
```

> 提示：若主入口演示已不再需要，可考虑在后续版本移除或将入口改为三节点流水线，避免与正式提示词混淆。
