# 更新日志：v0.4.0 → v0.4.1

> 版本主题：机制—机会—算子闭环，把"知识消费"升级为"科研机会挖掘与反应设计引擎"。
> 输入依据：`docs/STATUS_AND_NEXT_STEPS_v0.4.0.md`（对 v0.4.0 工作树的核查报告）。

## 一、修复的既有缺陷

### 1. 机制产物被引用校验误杀

- `_sanitize_references()` 只把**形状正确**的字符串当引用（`P-/E-/H-/MS-/OP-/GAP-` 编号）；
- 形状正确但不存在的编号才进 `invalid_references`；
- 描述性文字被误放进 `*ids` 字段时不再静默删除，而是回收到 `consumer_analysis.unattributed_notes`；
- 实测：上一版被误杀的 3 条算子链设想现在全部保留。

### 2. 机制超边被置信度截断挤掉

- 新增 `ontology.store.list_hyperedge_briefs()`（轻量索引）与 `load_hyperedges()`（批量完整加载），
  把"逐条联表加载 1000 条超边"改为"先筛选、后加载"，查询次数从数千降到约 6 次；
- 超边按类型配额选取（event 120 / relation 30 / mechanism 40 / reaction 40），event 型优先；
- 相关性打分改为词元命中 + 机制关键词加权 + event 型加权 − 低信息量结构关系降权，
  修复了"整句短语子串匹配恒为 0"导致排序退化为随机的问题；
- 每条入选超边可携带多条证据句，超边证据句同时进入 `knowledge.evidence`（编号 `H-xxxx-n`）；
- 实测（384 篇 / 3851 超边语料）：截断后仍含机制关键词的超边 **1/80 → 81/90**；
  消费提示词 **25.8 万字符 → 6.1 万字符**（≈8.6 万 → 2.0 万 tokens）。

### 3. design_context 有容器无内容

- 五键固定：`mechanism_states` / `reaction_primitives` / `opportunity_gaps` /
  `operator_candidates` / `constraint_conflicts`；
- 每条机制状态必须绑定真实 `evidence_ids` / `hyperedge_ids`，代码层标记 `traceable`，
  并输出整体 `traceability.ratio`；
- 离线兜底路径（`deterministic_design_context`）与 LLM 路径同结构，不再输出旧的
  `component_types / candidate_components / combination_space_hint`；
- LLM 未给出机制状态与算子链时，用确定性层补齐并标记 `mode="llm+deterministic_fill"`。

### 4. 条件/测量数据整体丢失

- `knowledge/node.py` 的超边适配层此前丢弃 `conditions` / `measurements`，现已原样透传；
- 事件投影路径补上 time 以外的 conditions 与 measurements；
- `SCHEMA_HINT` 新增第 13 条：领域无关地要求填写可量化条件与测量（温度/时间/溶剂/
  催化剂/当量/产率/ee/dr/p 值等），并给出标准键名与取值格式；
- `flag_issues()` 新增确定性追问：超边文本含可量化线索却缺 conditions/measurements 时，
  在二次精修中被点名补填。

### 5. 路由丢结果

- 消费节点返回补检请求但无 collector（或轮数用尽）时，不再走 `manual_review → END`
  丢掉机制理解，而是继续到内容节点；
- 多条补检请求合并成一条（不丢任何一条的 query 词），`request_count` 记录合并数量。

## 二、新增能力

### 1. 自研反应设计算子库 `study/reaction_operators.py`

- 20 个算子（15 个机制级 L3 + 4 个低阶 L1/L2 + 未归类），每个都带中文标签、创新等级、
  输入/输出契约与语义说明；
- 中英文别名归一（`umpolung` / `极性反转` / `polarity reversal` → `polarity_reversal`）；
- `normalize_operator_chain()` 校验并返回：词表外算子、链衔接断裂位置、
  由算子序列推出的创新等级与依据；
- 词表注入消费与内容两个提示词，LLM 只能选不能造。

### 2. 候选池与排序

- 内容节点先要骨架候选（`budget.max_candidates`，默认 36，单次调用上限 16），
  再按"算子序列 + 目标"指纹聚类去重，保留信息最全者；
- 两阶段生成：阶段 1 只产出骨架（目标/算子链/等级/硬约束回应/差异），
  阶段 2 仅对入选候选补全风险、验证计划与可行性论证，且不允许改写算子链；
- 评分 = 3×等级分 + 2.5×硬约束满足率 + 1.5×证据支持 − 1×链断裂 − 1×纯低阶算子；
- 输出 `candidate_pool`（生成/去重/同构组）与 `selection`（首选/备选/不建议优先实施/
  达标候选列表）；
- 兜底路径在没有机制素材时显式生成 L1/L2 候选并标注"未达创新等级下限"，
  不再让低阶候选静默通过；
- 兜底路径在提供模型时只对入选候选做一次深化，并标记
  `generated_by=deterministic_skeleton_expanded`，供审核节点判断是否值得继续修订。

### 3. 候选的算子链与硬约束回应

- 候选 JSON 改为 `candidates`，核心字段为 `operator_chain` / `innovation_level` /
  `innovation_basis` / `differentiation` / `satisfies_constraints` / `mechanism_evidence`；
- 规范化时自动把 `design_contract.target_constraints.hard_constraints` 补齐为
  "未回应"条目，使"漏答硬约束"可被审核发现；
- markdown 渲染新增算子链、链校验结论、硬约束核对结果与候选优先级。

### 4. 审核节点的设计契约硬校验

- 新增 8 项确定性检查（并入指令符合度，保持 8:2 双维度不变）：候选数量、算子链存在性、
  词表与衔接、创新等级下限、纯低阶算子、硬约束逐条回应、候选池扩充去重、差异轴可解释；
- 模型自评的 `innovation_level` 高于算子链实际等级时，以算子链为准；
- 审核日志新增 `design_checks` 与 `design_failures` 字段。

### 5. 事实核查节点 `study/fact_check.py`

- 图新增 `fact_checker`：`reviewer --pass--> fact_checker --> END`；
- 确定性核查三类问题：伪造引用、无来源断言（supported 无引用）、数值在证据中不存在；
- 模型只能追加问题，不能把确定性 high 级问题判 `pass`；
- 事实核查失败时回流内容节点定向修订，由 `review_rounds` 封顶避免死循环。

### 6. 修订闭环

- 内容节点合并审核 `revision_actions` / `issues` 与事实核查 `issues`，拼进下一次提示词；
- 要求逐条给出 `revision_responses`（resolved + 理由），模型漏答的条目由代码补记为"未解决"；
- markdown 新增"修订回应"章节。

### 7. 中间态落库

- 新增 `study_runs` 表（`run_id, round, request, status, decision, plan, consumer, draft, review, fact_check, ts`）；
- `run_study()` 结束时写入，`get_study_runs()` 可按 run_id 读取全部轮次；
- 此前四节点产物只存在于内存，无法审计"改了什么、为什么改"。

### 8. 语料卫生

- 新增 `apply_topic_relevance_gate()`：长文本记录与主题词元零重叠即判定跨域并丢弃，
  短文本（<30 字符）放行避免误杀，中英文均支持；
- `collect_mission` 把 seed_terms + 领域维度 + `must_cover` 作为门控词，
  检索报告记录 `relevance_gate`（丢弃数与样例）并写入事件日志。

## 三、工程与测试

- 新增 `tests/_tmpdir.py`：`tempfile` 创建的目录在受限沙箱下不可写时（Windows 报
  `PermissionError` / SQLite 报 `unable to open database file`），回退到仓库内 `.test-tmp/`，
  使 72 个既有测试在沙箱内也能跑通；
- 新增测试：`tests/test_hyperedge_conditions.py`（4）、`tests/test_study_feedback.py`（8）、
  检索门控与设计契约回归（6）；
- 测试总数 72 → 86，全部通过；
- 新增工具：`examples/dump_knowledge_pack.py`（知识包健康检查）、
  `examples/run_v040_baseline.py`（端到端基线，落盘 plan/consumer/draft/review/fact_check）。

## 四、模型调用稳定性（本轮新增）

实测定位到一次真实故障：内容形成节点在 6 万字符提示词 + `reasoning_effort=low` 组合下
**超过 15 分钟无响应**；同一提示词不传 `reasoning_effort` 时约 353 s 返回（输出 15,376 字符）。

- `models.py` 默认不再发送 `reasoning_effort`（可用 `DEEPSEEK_REASONING_EFFORT` 重新启用）；
- `DEEPSEEK_MAX_TOKENS` 可配置（默认 8000）；
- 新增 `study/model_call.py`：所有研究节点模型调用改为带超时的守护线程调用，
  超时返回 `None` 并由节点降级到确定性实现，单次请求不再能卡死整条链路；
- 四类超时可通过 `RA_STUDY_CONSUMER_TIMEOUT` / `RA_STUDY_CONTENT_TIMEOUT` /
  `RA_STUDY_REVIEW_TIMEOUT` / `RA_STUDY_FACT_CHECK_TIMEOUT` 调整（默认 900/900/600/600 秒）；
- `content._compact_knowledge()` 增加 55000 字符硬预算与逐级削减，内容提示词从
  9.1 万字符降到 6.4 万字符；
- 审核节点在"内容骨架由代码生成"时不再空转多轮（1 轮后转人工）。

## 五、已知限制

1. 机制状态抽取目前是"LLM + 规则兜底"混合，规则只覆盖常见活化方式/中间体/选择性来源，
   生僻机制仍依赖模型；
2. 语料相关性硬门是词元级判定，同义词库外的表述可能被误判为跨域（短文本已放行）；
3. `study_runs` 只保存最终一轮的结构化快照，尚未按审核轮次逐轮记录；
4. 支持度分布仍然偏低（实测 384 篇中 96% 的边只有单篇支持），
   "跨文献共识"类判断依然不成立，需在补检策略上继续投入。
