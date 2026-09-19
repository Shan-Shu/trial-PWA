# 三开源科研 Agent 学习报告

> 项目：research-agent（LangGraph 三节点：文献检索 → 质量评估 → 知识提取 → 动态本体）
> 学习对象：`gpt-researcher`（assafelovic）· `AI-Scientist`（SakanaAI，v1）· `AI-Scientist-v2`（SakanaAI）
> 目的：对照开源项目的提示词工程 / skills 封装 / 工程纪律，为 research-agent 提炼可迁移设计
> 本轮落地：知识提取节点优化，并入 v0.0.6；质量节点与金样本暂不处理（用户指示）
> 日期：2026-09-08

---

## 0. 结论先行（TL;DR）

| # | 从开源项目学到的可迁移模式 | 落到 research-agent 的动作 | 状态 |
|---|---|---|---|
| 1 | **显式“错误清单 + 大写祈使句硬约束”**（AI-Scientist 的 `error_list`、`DO NOT …`；gpt-researcher 的 `MUST`） | 知识提取提示词增加「硬性禁区清单」，把已暴露问题（衔接语实体、配方合并丢失、兜底关系过泛、属性无证据）写成“绝对禁止 + 反例/正例” | ✅ v0.0.6 |
| 2 | **反思/精修循环 + 收敛信号**（“Stick to the spirit… I am done”） | 知识提取增加「低置信/泛化关系二次精修」：仅对触发条件的块追加一轮精修，无改进则原样输出 | ✅ v0.0.6 |
| 3 | **“已产出”存档回喂，防重复/防放大**（idea archive；citation “DO NOT ADD AGAIN”） | 知识提取节点把“库中已有规范实体 + 近期语料高频兜底关系”注入提示词，抑制同类泛化关系持续膨胀 | ✅ v0.0.6 |
| 4 | **锚点化评分 rubric + Confidence + 集成/元评审**（AI-Scientist NeurIPS 评审表、bias 评审员、Area Chair 元评审） | 质量评估节点：先给 A/T/Q 分数档文字锚点 + 独立置信度；后续再考虑 bias 集成 | ⏸ 质量节点暂缓（用户指示） |
| 5 | **少样本金样本**（v1 用真实会议评审做 few-shot） | 从高质量语料挑 gold 抽取示例 | ⏸ 金样本暂缓（用户指示） |
| 6 | **执行反馈闭环 / Agent 自主调用工具**（实验代码→跑→看 stderr→修；检索→看结果→再检索） | 需要受控执行环境与工具回路，暂不引入 | ⏸ 后续版本 |

---

## 1. 三项目概览

| 维度 | gpt-researcher | AI-Scientist v1 | AI-Scientist v2 |
|---|---|---|---|
| 作者/仓库 | assafelovic / gpt-researcher | SakanaAI / AI-Scientist | SakanaAI / AI-Scientist_v2 |
| 定位 | **检索增强的研究助手**：把“调研问题→带引用综述报告”做深做全 | **全自动科研生命周期**：想法→实验→论文→评审（模板化、单域模板起步） | v1 的泛化升级：**去掉人工模板**，跨 ML 域，Agentic Tree Search（渐进式树搜索）+ 实验经理 Agent |
| 核心产物 | Markdown 研究报告（分级子主题、深度研究） | LaTeX 论文 + 实验代码 + 评审意见 | LaTeX/ICLR workshop 论文 + 实验 + 树搜索轨迹 |
| 主要模型 | 任意（支持多 provider/模型特化提示词家族） | 多模型分工（主实验 Sonnet/GPT-4.1，评审固定 GPT-4o） | 多模型 + VLM（评审图） |
| 是否执行 LLM 写的代码 | 否（只检索+写作） | 是（明确风险警示） | 是（明确风险警示，建议沙箱） |
| 对我们的直接价值 | 查询规划/引用纪律/子主题分解/skills 化 | 反思循环/评分锚点/评审集成/错误清单 | 分层模块化/评估器 FunctionSpec/日志审计 |

---

## 2. gpt-researcher 深度解析

### 2.1 架构

新一代 gpt-researcher 已演化为“技能（skills）+ 多智能体 + 多检索器 + MCP”的复合系统，核心仍是 **“规划查询 → 检索/爬取 → 筛选与压缩上下文 → 大纲/成稿”**：

```text
查询/任务
  ├─ Planner（多级子问题/子主题，详细报告会递归展开）
  ├─ 并行检索（web / 本地文档 / MCP 工具）→ 抓取正文
  ├─ 来源筛选 curate + 上下文压缩（context/compression）
  └─ 大纲 → 分节写作 / 深度研究逐层综合 → 引用 + 结论
```

关键目录（本地副本 `D:\Desktop\Auto-Research Tools\gpt-researcher`）：

- `gpt_researcher/prompts.py`（750+ 行，提示词家族 `PromptFamily`）
- `gpt_researcher/skills/`（`researcher.py`、`writer.py`、`deep_research.py`、`curator.py`、`context_manager.py`、`browser.py`、`image_generator.py`）
- `multi_agents/agents/`（orchestrator、planner、researcher、writer、reviewer、reviser、editor、fact_checker、fact_review、publisher、draft_review、plan_review、visualizer、human）
- `gpt_researcher/context/`（`compression.py`、`retriever.py`）

### 2.2 提示词工程要点（已核对源码）

**a. 查询规划：分而治之 + 硬约束 + 当前日期**

`generate_search_queries_prompt`：
- 一次给一个父任务，生成 `max_iterations` 条查询；
- **明确禁止搜索引擎操作符**：“Do not use search operator syntax such as site:, filetype:, inurl:, intitle:, OR, AND, or NOT — these operators are not universally supported and will return empty results on many search backends.”——这是“针对后端特性做约束”的范例（我们 v0.0.6 之前已在检索规划里给 PubMed 语法做了同类约束）；
- 注入“Assume the current date is {…} if required.”；
- 有上下文（此前检索的实时信息）时，要求“用上下文来 refine 查询”；
- 输出格式：“You must respond with a list of strings in the following format: […]. The response should contain ONLY the list.”

**b. 写作/报告：引用纪律写成 MUST 级清单**

`generate_report_prompt` 的亮点：
- “You MUST determine your own concrete and valid opinion … Do NOT defer to general and meaningless conclusions.”（**禁止正确的废话**——与用户对我们“关系语义过宽”的批评同构）；
- “Every substantive claim, figure or quote MUST carry an in-text citation to the source it came from. Do NOT cite sources that do not appear in the provided information.”（**证据纪律**：只能用给到的信息）；
- “You must also prioritize new articles over older articles if the source can be trusted.”（时效偏好）；
- 引用去重：“make sure to not add duplicated sources, but only one reference for each”；
- 报告类型可参数化（格式、字数、语气、语言），提示词由函数生成而非写死。

**c. 来源筛选（curate）：宁全勿删 + 保留原文**

`curate_sources`：评估相关/可信/时效/客观/“定量价值”，且明确 **“DO NOT rewrite, summarize, or condense any source content”**，只清垃圾不过度改写；剔除标准从严（“only if entirely irrelevant…”）。返回必须是原 JSON 列表格式。

**d. 深度研究：分层综合**

`generate_deep_research_prompt` 面向“分层研究结果（learnings with citations）”，要求把不同深度的研究分支综合成叙事（“Prioritize insights that emerged from deeper levels of research; Highlight connections between different research branches”）。

**e. 动态人格选择（auto agent）**

`auto_agent_instructions`：根据主题自动选“角色人格 + 提示词”（Finance Agent、Business Analyst Agent、Travel Agent…），每个角色带 emoji 与角色描述——**同一个框架，按主题切换 persona**。

**f. 摘要提示词**

`generate_summary_prompt`：如果问题无法由文本回答，则要求如实概括文本；并“Include all factual information such as numbers, stats, quotes, etc if available.”（保真优先）。

### 2.3 skills 封装方式

skills/ 下每个文件即一个“技能”，职责单一、可被主 Agent 调度：

| Skill | 职责 |
|---|---|
| researcher | 检索与信息收集 |
| writer | 按大纲成稿 |
| deep_research | 多轮子问题递归深挖 + 逐层综合 |
| curator | 来源筛选 |
| context_manager / compression | 上下文管理压缩 |
| browser / image_generator | 浏览器与配图 |

另有 **PromptFamily 家族化**：同一套方法名（`generate_search_queries_prompt`…）按模型（Granite 等）做子类覆写，把“模型差异”隔离在提示词层，而不是散落在业务代码里。

### 2.4 多智能体模式（multi_agents）

面向“长篇论文级研究”的编排：orchestrator 调度 → planner 拆解 → researcher 并行收集 → writer 初稿 → reviewer/reviser/editor 多轮打磨 → fact_checker/fact_review 事实核查 → publisher 交付。人物表里有 `draft_review`、`plan_review` 等**中间产物评审节点**，说明它把“质量门”拆在多个环节而不是只在最后。

### 2.5 工程机制

- 上下文压缩与来源上限（top_n）控制 token；
- 实时信息回注（前一轮检索结果进下一轮查询规划）；
- 报告类型与模型提示词家族解耦（`get_prompt_by_report_type`）。

### 2.6 对本项目的启示

1. **查询规划要“针对检索后端语法 + 当前日期”给硬约束**（已在 v0.0.6 前的检索提示词落地，并入 v0.0.6）；
2. **“禁止正确的废话 / 只用给定证据 / MUST 级引用纪律”** 的表达方式，可直接迁移到知识提取的“禁止泛化结论”；
3. 来源筛选“只清垃圾、不改写、宁全勿删”的原则，可用于我们对 PDF 清洗/证据句保留的校验；
4. skills 单文件单职责 + 提示词家族化，与 research-agent 已建的 `skills/` 目录思路一致，可在后续把“按模型覆写提示词”也做进去。

---

## 3. AI-Scientist v1 深度解析

### 3.1 流水线

```text
构思（50 个 idea，种子+存档，自评 Interestingness/Feasibility/Novelty）
  → 新颖性检查（Agent 自主决定查不查文献，≤10 轮，Decision made: novel/not novel）
  → 逐 idea：实验（aider 改代码 → 跑 `python experiment.py --out_dir=run_i` → 错误回喂，≤5 run）
  → 写作（LaTeX 分节填充 + 两遍 refinement + Agentic 引用补全）
  → 评审（5 个评审员集成 + Area Chair 元评审 + 数值平均）
  → 可选改进（把评审丢给 coder 改稿 → 再评）
```

（学习用源码以 raw.githubusercontent 拉取核心文件精读：`launch_scientist.py`、`ai_scientist/{generate_ideas,perform_experiments,perform_writeup,perform_review,llm}.py`）

### 3.2 值得逐条记录的机制

**a. 构思：先想后写 + 存档防重 + 反思收敛**

- 首轮提示词要求先给 `THOUGHT:`（动机/计划/与已有 idea 的差异），再给 `NEW IDEA JSON`；
- 字段含自评 `Interestingness/Feasibility/Novelty`（1-10），并强调 “Be cautious and realistic on your ratings.”——**让模型自评，但限制其乐观**；
- 把所有已生成 idea 作为 `prev_ideas_string` 喂回，避免重复；
- 反思提示（关键句）：
  - “Stick to the spirit of the original idea unless there are glaring issues.”（无大问题不许跑题）
  - “If there is nothing to improve, simply repeat the previous JSON EXACTLY … include ‘I am done’.”（**收敛信号**：无改进就原样重复，天然防止无限“改进”与 token 浪费）

**b. 新颖性检查 = 最小化 Agentic 工具循环**

- 系统人格：“You are an ambitious AI PhD student … Be a harsh critic for novelty.”（**故意从严**）；
- 每轮：模型输出 `{"Query": …}`（还没决定才给查询）→ 系统真的去 Semantic Scholar/OpenAlex 搜 top10（标题/作者/venue/年/被引/摘要）→ 回填给模型；
- 终止词：文本含 “Decision made: novel.” 或 “Decision made: not novel.” 即停；
- 这是“LLM 决定要不要用工具、用几次”的经典实现：**查询是手段，决策才是有界目标**。

**c. 实验：执行反馈闭环 + 命令格式强约束**

- 用 aider `Coder` 改代码，固定命令 `python experiment.py --out_dir=run_i`；
- 提示词里大写强制：“YOUR PROPOSED CHANGE MUST USE THIS COMMAND FORMAT, DO NOT ADD ADDITIONAL COMMAND LINE ARGS.”；
- 失败把 stderr（截断到 1500 字符）喂回去自动修，每个 run 最多 4 次尝试；成功则回填结果并提示“Decide if you need to re-plan your experiments”；
- 完成信号：模型输出 `ALL_COMPLETED`；
- **跨 Agent 交接**：要求把“所有对写作有用的细节”写进 `notes.txt`（“Be as verbose as necessary. Someone else will be using notes.txt to perform a writeup”）。

**d. 写作：分节 + 每节“写作提示 + 精修 + 错误清单”**

- 每节（Abstract/Intro/…/Conclusion）先给 `per_section_tips`（该节该写什么的 check），写完立刻跑 `refinement_prompt`（“criticize and refine only the {section} … do not leave any placeholders”）；
- 全稿完成后还有 `second_refinement_prompt`（“recall the advice… identify redundancies… save space without weakening”）；
- **错误清单 `error_list`**：把常见错误列成 bullet（数值未来自实验、引用不在 .bib、重复图/节、未转义符号、环境未闭合、HTML 语法混入 LaTeX 等），精修时“Pay particular attention to fixing any errors such as: …”；
- Agentic 引用补全：`citation_system_msg` 里“DO NOT ADD A CITATION THAT ALREADY EXISTS!”、改稿指令里“ABSOLUTELY DO NOT ADD IT AGAIN!!!”；每轮只加“最重要的一条引用”，且必须来自 API 搜索结果（防幻觉引用）。

**e. 评审：锚点化评分表 + 偏置集成 + 元评审**

- 把 **NeurIPS 评审表原文**整段放进提示词：每个分数档都有文字定义（Overall 1-10 逐档、Confidence 1-5 逐档、Soundness/Presentation/Contribution 1-4），这是“calibration（标定）”的关键；
- 5 个评审员 = 混合 `reviewer_system_prompt_neg`（“If a paper is bad or you are unsure, give it bad scores and reject it.”）与 `_pos`（镜像）→ 用**故意偏置**制造视角多样性；temperature 调高鼓励差异；
- 再由“Area Chair”人格 `get_meta_review` 汇成 meta-review，数值分数取各评审均值——**用“委员会+主席”对抗单次抽样的随机性**；
- 评审也有反思轮 + `I am done` 收敛；
- few-shot：真实会议评审（`fewshot_examples/*.json`）垫在提示词里。

### 3.3 llm.py 工程细节

- 多 provider 统一 OpenAI 兼容 client（Anthropic/OpenAI/Bedrock/Vertex/DeepSeek/OpenRouter/Gemini）；
- `@backoff` 指数退避重试（RateLimit/Timeout）；
- JSON 解析容错 `extract_json_between_markers`：先找 ` ```json … ``` `，再回退到首个 `{…}`，并清洗非法控制字符；
- `seed=0` + 可控 temperature；支持 `n_responses` 一次性取多条做集成。

### 3.4 优点 / 缺点

**优点**：把“科研流程”拆成有明确目标的阶段，每阶段都有（a）结构化 schema、（b）反思循环与收敛、（c）刻意偏置或从严人格、（d）真实工具反馈；评审部分对“让 LLM 打分可信”的工程化最完整。
**缺点**：依赖人工模板（每域一套 experiment.py/模板 LaTeX）；会执行 LLM 写的代码（作者明确风险提示）；“实验成功”只有自评审，没有外部验证；token/算力开销大。

---

## 4. AI-Scientist v2 深度解析

### 4.1 相对 v1 的定位

README 自述：v2 去掉 v1 的“人工模板依赖”，跨 ML 域泛化；采用 **progressive agentic tree search**，由“实验经理 Agent（AgentManager）”指导探索。官方也承认 v2 不一定比 v1 强——v1 模板化成功率高，v2 更开放、成功率更低。对我们的启示：**“强模板 + 高成功率”与“开放探索”是两种取舍**，research-agent 现阶段走的是 v1 式（强 schema 模板）路线，是合理的。

（学习用源码位于 `%TEMP%\ai-scientist-v2`，含 `ai_scientist/`、`launch_scientist_bfts.py`、`README.md` 等）

### 4.2 模块结构（文件清单核对）

```text
ai_scientist/
  perform_ideation_temp_free.py   # 免模板构思（读 topic .md 生成 proposal JSON）
  perform_writeup.py / perform_icbinb_writeup.py
  perform_llm_review.py / perform_vlm_review.py / vlm.py   # LLM 与视觉评审
  perform_plotting.py
  tools/ (base_tool.py, semantic_scholar.py)               # Agent 工具
  treesearch/  (agent_manager.py, parallel_agent.py, bfts_utils.py,
                journal.py, journal2report.py, log_summarization.py,
                interpreter.py, backend/, utils/)
  utils/token_tracker.py
```

### 4.3 关键机制

**a. 免模板构思（temp-free ideation）**

- 输入是“研究主题 Markdown”（Title/Keywords/TL;DR/Abstract…），而非特定领域的实验模板；
- 输出 proposal JSON（Research Goal / Proposed Experiments / Related Work / Abstract / Risk Factors and Limitations…）；
- 提示词明确要求“**You should perform at least one literature search before finalizing your idea**”——构思阶段就强制用工具（Semantic Scholar）做文献查证；
- 同样的“已生成 proposal 存档回喂” + 反思循环（且反思时可吸收“上一次工具结果 last_tool_results”）。

**b. Agentic Tree Search / AgentManager（treesearch/agent_manager.py，约 1100 行）**

- 把研究拆成“阶段（Stage）→ 子阶段（Substage）”，每个阶段多个 draft（并行 Agent）；
- `AgentManager`：为每个阶段 curate task description（把 Experiments、Risk Factors 等注入），创建 `ParallelAgent`，维护 `journal`（记录各节点的实验轨迹），检查阶段完成度后决定是否推进、何时收敛；
- 用 **FunctionSpec + JSON schema** 定义评估函数（`evaluate_stage_progression` / `evaluate_stage_completion` 等），即“让 LLM 以结构化函数调用的方式输出决策”——比自由文本决策更可编程、可审计；
- VLM 评审图（plot analyses）作为阶段完成的反馈来源之一；
- checkpoint 保存（`_save_checkpoint`），支持中断续跑。

### 4.4 对本项目的启示

1. **评估器结构化（FunctionSpec/JSON schema）**：research-agent 的质量节点与“是否需要精修”的判定，都可做成“固定 JSON 决策 + 理由”，便于看板与审计；
2. **日志/轨迹即资产**：tree search 的 journal、log summarization 说明“过程轨迹”本身要被记录和可回放——research-agent 已有 `log_event`/`ontology_runs`，可继续向“每个抽取结果可追溯到 chunk/句子”强化；
3. **构思/实验阶段强制文献查证**：我们提取阶段已把“evidence 必须引原句”做成了硬约束，方向上与“grounded generation”一致。

---

## 5. 三项目横向对比

| 能力/模式 | gpt-researcher | AI-Scientist v1 | AI-Scientist v2 |
|---|---|---|---|
| 主循环 | 检索→综合 | 想法→实验→写作→评审→改进 | 树搜索（阶段/子阶段/分支） |
| 反思循环 | 无显式 | ✅ 构思/评审/写作均有，带 `I am done` | ✅ 构思/阶段评估，带工具结果回注 |
| 评分锚点 rubric | 部分（风格/语气参数） | ✅ NeurIPS 表逐档定义 + Confidence | ✅ FunctionSpec + VLM/LLM 评审 |
| 集成/多评审 | 多子 Agent 分工 | ✅ 5 评审员偏置集成 + Area Chair | ✅ 树搜索多分支 + 实验经理裁决 |
| 少样本 | 少 | ✅ 真实会议评审 few-shot | 沿用 v1 的 fewshot_examples |
| 防重复/防放大 | 查询级去重 | ✅ idea archive / 引用“DO NOT ADD AGAIN” | ✅ proposal archive |
| 工具回路 | 检索+爬取 | ✅ 代码执行反馈 | ✅ 代码执行 + Semantic Scholar + VLM |
| 人类在环 | 部分（可人工） | 基本无（作者警示风险） | 基本无（作者警示风险） |
| 成本控制 | 上下文压缩/来源上限 | 收敛信号/截断 stderr/run 上限 | checkpoint/阶段上限/日志压缩 |
| 可审计性 | 引用列表 | notes.txt + 日志 | journal + tree 导出 + 日志 |

---

## 6. 对照 research-agent（v0.0.5）的差距与机会

### 6.1 research-agent 现状（截至 v0.0.5）

- LangGraph 三节点：检索（PubMed/API + PDF 清洗 + 元数据规整）→ 质量（A/T/Q，Q=0.6A+0.4T，0.8/0.5 阈值路由）→ 知识（分句分段分块 → LLM 抽取实体/关系/属性/事件 → 写入动态本体 SQLite）；
- 模型分工：检索 DeepSeek V4 Pro、质量 GLM 4.7 Flash、知识 DeepSeek V4 Pro（临时替代 gpt-5.6）；
- 本体侧已具备：canonical 实体复用、材料登记（lcmat 本地规范名+external 双轨）、事件旁路化（event_assertions）、细粒度关系词表与同义归一、强断言置信门控、证据等级（evidence_tier）、属性质量优化（snake_case / value+unit / 去空值）、合并/方向队列（就位）；
- 同语料 50 篇对比数据（compare5）：v0.0.5 节点 476 / 边 533 / 孤立 9.0% / 事件节点 0 / involves 0 / 材料登记 106；
- 看板/对比工具/PPT 已具备；`skills/` 已建立四份 SKILL.md（v0.0.5 基线）。

### 6.2 差距 → 机会编号

| 差距（对照开源项目） | 说明 | 机会 |
|---|---|---|
| 提示词约束偏“请”，缺“绝对禁止”级硬约束与显式错误清单 | 知识提取历史问题（衔接语实体、配方合并丢失、top3 关系泛化）都是“约束不够硬”的表现 | 机会 A：错误清单 + 大写祈使句 |
| 单遍抽取，无“低置信再想一次” | v0.0.5 已用置信门控“标记”弱断言，但没有让模型重审 | 机会 B：低置信二次精修（带收敛信号） |
| 同一语料内泛化关系会自我放大 | 只回喂“规范实体”，没回喂“已反复出现的宽泛关系” | 机会 C：语料高频兜底关系提醒 |
| 评分无档位锚点、无独立 Confidence | A/T/Q 是公式，但“0.2 与 0.8 长什么样”未定义 | 机会 D：rubric + Confidence（质量节点） |
| 无金样本 | 缺“理想输出长什么样”的示范 | 机会 E：few-shot gold（知识节点） |
| 无执行/工具回路 | 不能真跑代码或自主连续检索 | 机会 F：Agentic 工具闭环（需执行环境） |
| 元评审/集成缺位 | 单模型单次打分/抽取有随机性 | 机会 G：bias 集成 + 元评审（后续） |

---

## 7. v0.0.6 采纳记录

> 用户指示：质量节点暂不优化；金样本暂不添加；主要优化知识提取节点；学习报告与实现一并并入 v0.0.6。

### 7.1 采纳（知识提取节点，v0.0.6 实施）

1. **机会 A：硬性禁区清单（ERROR LIST）**
   - 在 `knowledge/extractor.py` 新增“硬性禁区”，逐条给出“禁止 → 反例 → 正例”；
   - 覆盖：报告语/衔接语实体与事件、配方/掺杂/比例合并丢失、兜底关系（related_to 等）放大、同义共指漂移、属性无证据、推测写成确定断言、subject/object 与实体字符串不一致。
2. **机会 B：低置信/泛化关系二次精修（REFLECTION LOOP）**
   - `KnowledgeExtractor.extract_with_refine()`：首遍抽取后，若存在（a）置信度 < 阈值 或（b）使用兜底泛化关系 或（c）疑似报告语实体，则追加一轮精修；
   - 精修提示词借鉴 AI-Scientist：只改被点名问题、粘住原意、无改进则原样输出并 `I am done`；
   - 精修解析失败或未变化时，回退使用首遍结果，保证鲁棒；
   - 开关与阈值进入 `Settings`（环境变量可覆盖，默认开启，成本可控：只对触发块二次调用）。
3. **机会 C：语料高频兜底关系提醒（ARCHIVE WARNING）**
   - 知识节点运行时注入“库中已有规范实体 + 近期高频兜底关系计数”，要求“除非证据明显更强，否则不要继续添加同类泛化关系”。
4. **同步**：`skills/knowledge-extractor/SKILL.md` 更新至 v0.0.6；`VERSIONS.md` 登记；附带检索提示词早前已做的 PubMed 语法/日期约束一并入库。

### 7.2 暂缓（记录原因）

- 质量节点 rubric + Confidence（机会 D）：用户指示当前质量节点在动态本体构建中作用不关键，暂不优化；
- 金样本 few-shot（机会 E）：用户指示暂不添加；
- Agentic 工具闭环（机会 F）：需受控执行环境，后续版本。

### 7.3 不采纳 / 边界

- 不引入“执行 LLM 写代码”；research-agent 是“读文献建本体”，无代码执行面；
- 不把事件重新图节点化（v0.0.5 已决策旁路化）；
- 不改变质量节点阈值与权重。

---

## 8. 参考链接

- gpt-researcher: <https://github.com/assafelovic/gpt-researcher>
- AI-Scientist v1: <https://github.com/SakanaAI/AI-Scientist>
- AI-Scientist v2: <https://github.com/SakanaAI/AI-Scientist_v2>
- v2 论文页（Sakana AI Blog）: <https://sakana.ai/ai-scientist-first-publication/>

## 附录 A：本轮核对过的源码文件

| 项目 | 文件 |
|---|---|
| gpt-researcher | `gpt_researcher/prompts.py`、`gpt_researcher/skills/*`、`multi_agents/agents/*`（结构清单） |
| AI-Scientist v1 | `launch_scientist.py`、`ai_scientist/generate_ideas.py`、`perform_experiments.py`、`perform_writeup.py`、`perform_review.py`、`llm.py`、`templates/nanoGPT/prompt.json` |
| AI-Scientist v2 | `README.md`、`ai_scientist/perform_ideation_temp_free.py`、`treesearch/agent_manager.py`（结构 + FunctionSpec）、模块清单 |

