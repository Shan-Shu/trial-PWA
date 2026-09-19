# v0.4.0 现状核查与下一步优化方案

> **后续进展（v0.4.1）**：本文列出的 P0/P1 问题已在 v0.4.1 全部处理，
> 实测数据与端到端接线验证见 `docs/VERIFICATION_v0.4.1.md`，
> 变更清单见 `docs/CHANGELOG_v0.4.0_v0.4.1.md`。
> 本文保留为问题诊断的历史记录（第 3 节的根因定位仍然有效）。

> 核查日期：2026-09-11
> 核查对象：工作树（未提交）的 v0.4.0 改动 + `D:\Desktop\问题与修改方向.md`、`D:\Desktop\两个开源项目启示.md`
> 核查方式：读代码 + 跑真实语料（`data/ynamide_multiazabicycle_v020.db`，384 篇 / 2821 节点 / 3012 边 / 3851 超边）+ 真实 LLM 试跑消费节点
> 核查结论：**两份文档提出的"结构层"问题已按 `NEXT_ITERATION_PLAN_v0.4.0.md` 基本落地；"机制—机会—算子"这一核心缺陷一行未动，而且现在有了更精确的故障定位。**

---

## 一、结论速览

| 文档提出的问题 | 当前状态 | 证据 |
|---|---|---|
| 顶层运行顺序不是"先规划再检索" | ✅ 已解决 | `study/graph.py` 图入口为 `START → planner → collection → consumer → content_builder → reviewer`；`collection` 节点在 `retrieval_plan` 为空时直接 `planning_failed` 并拒绝检索 |
| Planner 只输出 seed_terms + 四种创造操作 | ✅ 已解决（契约层） | `planner.py` 已规范化 `retrieval_plan / analysis_plan / evidence_policy / design_contract / stop_conditions / budget` |
| 知识消费节点由代码写死、不用 LLM | ✅ 已解决 | 新增 `consumer` 角色（`models.py`，`CONSUMER_MODEL=deepseek-v4-pro`）；LLM 不可用时返回 `consumer_failed`，不静默回退 |
| `combination_space_hint` 只有"组合/替换/迁移" | ⚠️ 部分解决 | LLM 路径不再产出该字段；但离线兜底 `build_design_context()` 仍在生成它，且只有这一条路径被内容节点真正消费 |
| 候选方案只有四种低级操作 | ❌ 未解决 | 内容提示词仍写"创造操作（组合、迁移、替换、扩展…）"；离线路径实测 4 个候选的 `creative_operation` 就是四种低级动作原文 |
| Content 不消费 `revision_actions`，反馈不闭环 | ❌ 未解决 | `content.py` 全文不出现 `revision_actions`、不读取 `state["review"]`；审核 `revise` 后内容节点只重跑同一提示词 |
| Reviewer 不检查创新层级 | ❌ 未解决 | `reviewer.py` 全文无 `innovation_floor`、无 `target_constraints`；一级维度仍是 80% 指令符合度 + 20% 证据正确性 |
| 缺少机制状态/反应原语/机会缺口/算子链 | ❌ 未解决（有容器无内容） | 见第三节：LLM 能产出机制状态与机会缺口，但**产物被丢弃或不许使用** |
| 缺少候选池（30–50）、反思去重、评分排序 | ❌ 未解决 | `min_candidates` 直接写进提示词要求"至少 N 个"，无候选池、无聚类去重、无排序 |
| 缺少 Fact Checker / 修订历史 | ❌ 未解决 | 无独立事实核查节点；四节点运行结果不落库，只有 `processing_log` 事件 |
| 缺少 L0–L4 创新等级体系 | ❌ 未解决 | 全仓仅 `planner.py` 里 `innovation_floor: "L3"` 一个字符串常量，无评分规则、无判定代码 |

**一句话**：v0.4.0 解决的是"谁来驱动、按什么顺序、按什么契约运行"，没有解决"知识里有没有机制、候选为什么低级"。

---

## 二、已处理的问题（v0.4.0 真实完成项）

### 2.1 主链路统一为 Planner-first

`run_study()` 是唯一完整入口；`collection` 节点只执行 Planner 的 `retrieval_plan`（空计划拒绝检索），内部经 `collect_mission()` 调用旧 `run_topic()`，旧 pipeline 降级为数据构建子流程。Dashboard `/api/study/run` 走同一张图并注入四个真实模型。这一条与 `NEXT_ITERATION_PLAN` 第三节完全对齐，验收条件"未规划不得检索"已在代码中实现（`graph.py` 的 `planning_failed` 分支）。

### 2.2 Planner 契约升级

`normalize_plan()` 已输出并兜底：

- `retrieval_plan`：`query_variants / source_mix / dimensions / recency_window / max_results_per_query / min_quality / must_cover / stop_conditions`
- `analysis_plan`、`evidence_policy`、`stop_conditions`、`budget`
- `design_contract`（仅 generative）：`objective / target_constraints / innovation_floor / min_candidates / differentiation_axes / evaluation_criteria`

`creative_operations` 已降级为兼容字段，提示词也明确写了"不能把组合/替换/迁移当作核心创新"。**契约层做到位了，问题是没人读它**（见 3.1）。

### 2.3 知识消费节点 LLM 化

`study/consumer.py` 现在的分工与规划文档一致：

- 代码：`mine_ontology_evidence()` 查询 + 模式卡编号（`P-xxxx`）+ 证据编号（`E-xxxx-n`）+ 超边编号（`H-xxxx`）+ `_sanitize_references()` 引用过滤
- LLM：`CONSUMER_PROMPT` 产出 `summary / mechanism_clusters / known_conflicts / evidence_gaps / retrieval_requests / design_context / confidence`
- 失败语义清晰：模型异常 → `consumer_failed`；无模型 → `consumer_mode="offline_fallback"`；空语料 → `needs_collection` 并给出补检请求

### 2.4 提示词文档与模型绑定同步

`docs/llm_prompts_current.md` 已升到 v0.4.0 并新增"五、知识消费节点"；`.env` 中四个研究角色均为 `deepseek-v4-pro`，三个数据构建角色为 `deepseek-v4-flash`。

### 2.5 离线回归测试

`tests/test_study_nodes.py` 新增了：LLM Consumer 的 `design_context` 生成与非法引用过滤、collector 按 `retrieval_plan` 触发、空语料补检请求。v0.4.0 的 70 个测试与文档描述一致（本次因沙箱限制未能完整复跑，见附录 A）。

---

## 三、本轮新发现（比原文档更精确的根因）

以下每条都是本轮在真实数据/真实模型上跑出来的，不是推断。

### 3.1 【最致命】机制信息能被提炼出来，但被两条硬编码路径丢弃

**证据 A：内容节点根本不读 `design_contract`。**
对真实语料离线跑完整生成链路，Planner 契约含 `innovation_floor=L3`、`min_candidates=4`、硬约束"氮原子数≥2"，结果：

```
候选数: 4
候选字段: components / creative_operation / evidence_ids / id / novelty_source /
          pattern_ids / rationale / risks / status / target / title / validation_plan
creative_operation 取值: ['组合已有方案/方法', '把已有方法迁移到新的对象或场景',
                        '替换或改造成分/组件/条件', '扩展原有方案到更一般情形']
含 operator_chain 字段: False
```

`content.py` 全文不含 `design_contract`；`GENERATIVE_BLOCK` 只从 `creative_contract.min_candidates` 取数量。**Planner 定的创新下限、目标硬约束、差异轴，没有任何一个进入生成阶段。**

**证据 B：LLM 消费节点产出的高阶机会被引用校验当垃圾删掉。**
用真实模型（`deepseek-v4-pro`）跑消费节点，它正确输出了三条真正有价值的算子链设想：

```
- 将N-sulfonyl ynamide的DKR与B(C6F5)3催化的(5+1)-annulation结合
- 将Pd-catalyzed Larock annulation与亲核性氮杂环化串联
- 用铜催化环化生成乙烯基阳离子后与双氮亲核试剂捕获成环
```

这三条进入了 `invalid_references` 被过滤。原因：模型把"算子链描述"放进了含 `id` 的字段（`evidence_ids` / `operator_candidate_ids` 一类），而 `_sanitize_references()` 的判定规则是"key 含 `id` 或 `evidence` 的字符串列表 = 引用 ID 列表"，只保留能对上 `P-/E-/H-` 编号的值，其余全部丢弃。**结果是：系统能想出来的好东西，被自己的引用校验误杀，然后在 `mechanism_states` 里只剩三条描述性字符串。**

同一份输出的其余部分质量相当高（说明模型能力不是瓶颈）：

- `evidence_gaps` 第 1 条：缺少直接构建含两个环内氮原子的多元氮杂环（如哒嗪、嘧啶、喹唑啉、萘啶）的证据 → 正好命中 Planner 的硬约束"氮原子数≥2"
- `constraint_conflicts`：3-aminoindole 仅环内一个氮，环外 NH2 可能不满足硬约束 → 真正在做约束校验
- `retrieval_requests` 3 条，query 具体可投递
- `mechanism_states` 3 条（乙烯基阳离子、C–N 旋转消旋、C–N 阻转异构）
- `reaction_primitives` 5 条

但 `operator_candidates` 为空（离线路径下该键根本不存在；LLM 路径下模型返回的 3 条链被当成非法引用删掉）→ **规划文档里"第四层：设计算子链"这一层实际是空的。**

**证据 C：离线兜底路径仍在生产旧字段。** `build_design_context()` 实际输出：

```
keys: ['objective_hint', 'component_types', 'composable_relations',
       'candidate_components', 'known_limits', 'combination_space_hint']
mechanism_states: None   opportunity_gaps: None   operator_candidates: None
known_limits: {'weak_patterns': 199, 'strong_patterns': 1}
```

也就是说：只要没绑模型，系统产出的仍是原文档批判的那套"组件组合空间"。生产路径虽然绑了模型，但**代码里那条低级路径没有删除、也没有被断言禁止**。

### 3.2 知识包截断把机制超边几乎全部挤掉

真实语料实测：

```
代码层挖到: patterns 200 / evidence 204 / hyperedges 200
LLM 实际收到: patterns 60 / evidence 120 / hyperedges 80
截断后仍含机制关键词的超边: 1 / 80
```

数据库里 event 型超边共 740 条，其中 190 条（25.7%）含机制关键词（vinyl cation、gold(I)-carbene、DFT calculations on cyclopropyl intermediate、enantioselective cycloisomerization…），**这是本项目最有价值的机制语料，实际进入模型的只剩 1 条。**

根因有三层：

1. `list_hyperedges(limit=1000)` 按 `confidence DESC, hyperedge_id DESC` 截断，3851 条里只取前 1000；relation 型超边（`uses/is_a/made_of` 等按 legacy edge 机械展开）因置信度靠前占掉 790 席。
2. `_relevance()` 用完整短语做子串匹配（`"ynamide annulation" in text`），实测 **1000 条超边相关性全部为 0**，排序退化为"按置信度 + 按主键"的近似随机。
3. 超边没有类型感知：`relation` 型超边是单条边的镜像，信息量为零，却和 `event` 型超边争夺同一配额。`_compact_consumer_knowledge()` 再按顺序切片到 80。

顺带两个放大问题：

- `mine_ontology_evidence()` 的 evidence 上限是 500，但 patterns 上限 200 后紧跟 `patterns[:200]` 再过滤 `evidence`，实测只有 204 条证据可用——**每篇论文只用了 1 条证据句**（384 篇 / 3059 条证据句里绝大多数从未被消费）。
- 消费提示词实测 **25.8 万字符 ≈ 8.6 万 tokens，单次耗时 90 秒**。这个规模下多数 provider 会截断或降质，且成本不可控。

### 3.3 支持度统计失真，`known_limits` 会骗人

实测 pattern 支持度分布：`{1 篇: 197, 2 篇: 2, 3 篇: 1}`——384 篇语料里 **99% 的边只有单篇支持**。这与 `output/ynamide_method_generation_comparison.md` 的结论一致，但也意味着：

- `weak_patterns=199 / strong_patterns=1` 这种输出没有决策价值；
- 任何"跨文献共识"式的机制判断都不成立；
- Planner 的 `evidence_policy.minimum_support=2` 实际上会把几乎全部知识判为不达标。

### 3.4 条件/测量数据基本不存在 → 第三层"条件兼容性"无米下炊

```
ontology_hyperedge_conditions: 34 条（仅 condition_key='time'）
ontology_hyperedge_measurements: 0 条
```

3851 条超边里只有 34 条带条件、0 条带测量。而原文档第三层明确要求输出"条件冲突""条件兼容性"，第六节要求给出"合成步骤数""风险和成本"。**提取层的 schema 没有为化学反应的温度/溶剂/催化剂负载/当量/时间/产率/ee/dr 保留结构化槽位**（只有 `time` 意外落了 34 条）。

后果直接可见：LLM 消费节点在 `evidence_gaps` 里主动抱怨"缺少具体反应条件（温度、溶剂、催化剂负载、添加剂）和底物范围数据，无法评估候选方法的新颖性和可行性"。**模型自己指出了上游数据缺口。**

### 3.5 图路由会在"要补检"时丢掉结果

`_route_after_consumer()`：当消费节点返回 `retrieval_requests` 且 `collection_available=False`（或轮数用尽），路由到 `manual_review` → `END`。此时 `draft` 为空，`knowledge` 里那份有价值的 `consumer_analysis`（机制簇、机会缺口、冲突）**既不落库也不输出**。Dashboard 默认 `inject_llms=False` 时正是这条路径。

### 3.6 审核通过的含金量太低

离线实测：4 个 L1 级"换底物/组合"候选，`decision=pass`，`overall_score=1.0`，检查项只有三条（至少 4 个候选 / 中文输出 / markdown 格式），`revision_actions=[]`。也就是说 **"5 个方案本质相同"这类问题在审核环节完全不可见**，与 `问题与修改方向.md` 第四节第 4 点的判断一致。

### 3.7 语料本身有噪声，机制抽取会被污染

v020 库 384 篇里混有脂质体阿霉素、疟疾 HSP90 抑制剂、转谷氨酰胺酶点击化学等明显跨域文献；节点类型分布是 `Chemical 1536 / Method 662 / Protein 118 / Drug 51 / Disease 44`——**生物医药类占了相当比例**。机制抽取如果直接在整库跑，会重演"检索一个领域、抽到另一个领域"的老问题。

---

## 四、下一步优化方案

### 4.0 先补一个基线（必须最先做，否则无法验收）

当前磁盘上**没有任何 v0.4.0 生成任务的端到端产物**：`output/run_v031_test.py` 是 v0.3.1 时代的脚本（未注入 `consumer_model`、未传 `collector`），其 `ynamide_multiazabicycle_v031_result/draft/review` 三个产物不存在；`output/version_reviews_3000/` 是横向评测脚本产物，不是流水线产物。

所以第一步是修好脚本并跑出基线（约 4 个真实 LLM 调用）：

1. 注入 `consumer_model=build_role_model("consumer")` 与 `collector`；
2. 落盘 `plan / knowledge.consumer_analysis / design_context / draft / review / 运行元数据`（对齐规划文档 WP1 的统一状态字段）；
3. 用**同一请求、同一库**同时产出"v0.3.1 产物"与"v0.4.0 产物"的对照表。

验收：能回答"这一版 5 个候选分别落在 L0–L4 哪一级、机制各不相同还是同一机制的四种换法"。

### 4.1 P0：让机制产物真正进入生成（改 3 个文件即可见效）

| 序号 | 改动 | 文件 | 验收 |
|---|---|---|---|
| 1 | `_sanitize_references()` 只对"ID 形状"的字符串做校验（正则 `^(P\|E\|H\|MS\|OP\|GAP)-\d`），非 ID 字符串原样保留 | `study/consumer.py` | 3.1 证据 B 的三条算子链不再进 `invalid_references` |
| 2 | 删掉 `build_design_context()` 旧产出，离线兜底也必须输出 `mechanism_states/reaction_primitives/opportunity_gaps/operator_candidates/constraint_conflicts` 五键；`combination_space_hint` 从代码与提示词中彻底移除 | `study/consumer.py` | 离线路径 `design_context` 键集合与 LLM 路径一致 |
| 3 | 内容提示词改为显式消费 `design_context` 与 `design_contract`：候选必须给出 `operator_chain`（算子序列）、`innovation_level`、`satisfies_constraints`；`creative_operation` 降级为备注字段 | `study/content.py` | 候选 JSON 必须含 `operator_chain` 与 `innovation_level`，缺失即代码层判不合格 |
| 4 | `_compact_knowledge()` 增加 `consumer_analysis` 与 `design_context` 的完整透传（不再只透传 `candidate_components`） | `study/content.py` | 提示词中可见完整机会缺口列表 |

### 4.2 P0：修知识包选取（改 3.2 的截断）

1. **按类型配额检索，而不是按置信度切 1000 条**：新增 `list_hyperedges(hyperedge_types=["event"])` 的独立配额（如 event 600 条、relation 200 条），`relation` 型超边不再与 `event` 争抢。
2. **相关性打分改成词元命中 + 类型权重**：`_relevance()` 先按空格/连字符切词再匹配（解决整句短语命中率为 0），并给 `event` 型超边、含机制关键词者加权。
3. **证据按超边聚合，而不是按边取第一条**：允许同一 `pattern/hyperedge` 携带多句 evidence，把 500 条上限用满（当前 384 篇只用了 204 条句）。
4. **控制提示词预算**：目标 ≤ 3 万 tokens。做法：模式卡降到 30 条并只保留机制相关实体类型；`event` 超边只保留 label + 成员名 + 证据句，去掉 `attributes/provenance` 冗余字段。

验收：截断后仍含机制关键词的超边从 **1/80 提升到 ≥ 30/80**，单次消费耗时 < 30 秒。

### 4.3 P0：补条件与测量抽取（否则第三、第六层永远是空的）

在知识提取 schema 中为化学类 event 超边补齐结构化槽位：

```
conditions:  temperature / time / solvent / catalyst / ligand / additive / base / atmosphere
measurements: yield / ee / dr / regioselectivity / conversion / scale
```

这是 `3.4` 的直接对策。注意：**不要**先做"机制状态抽取器"再补条件——实测表明没有条件槽位时，LLM 会把条件塞进 event label（如 `run 1 of PdI2/KI-catalyzed oxidative carbonylation of 1a with 2a in EmimEtSO4`），说明信息在原文里、只是没有槽位。先补槽位，机制层才有可行的输入。

验收：`ontology_hyperedge_conditions` / `measurements` 在炔酰胺库上分别达到 ≥ 300 / ≥ 200 条，LLM 的 `evidence_gaps` 中"缺少反应条件"一项消失。

### 4.4 P1：机制状态抽取层（原文档第一层，收敛版）

在消费节点之前插入确定性 + LLM 混合的机制抽取，但**不要新建图查询接口**，只消费已有的 event 超边 + 证据句：

- 输入：`event` 型超边（label + members + evidence span）
- 输出（带 ID、可被引用、可被校验）：

```
mechanism_states: [{
  "state_id": "MS-0001",
  "start_state": "...", "activation_mode": "...", "intermediate": "...",
  "bond_changes": [...], "selectivity_control": "...", "catalyst_cycle": "...",
  "termination": "...", "known_side_reactions": [...],
  "evidence_ids": ["E-xxxx-n"], "hyperedge_ids": ["H-xxxx"], "confidence": 0.0
}]
```

关键约束：**每条 mechanism_state 必须至少绑定 1 个真实 evidence_id / hyperedge_id**，代码层强校验（当前 `mechanism_states` 是纯字符串列表，无法校验、无法溯源——这是 3.1 证据 B 泄漏的入口）。

### 4.5 P1：自研算子库（原文档第二层，必须自研）

原文档列的 15 个算子是关键资产，但要落成**代码里的封闭词表 + 输入输出契约**，而不是让 LLM 自由发挥：

- `reaction_operators.py`：算子名、中文名、输入态要求、输出态承诺、典型中间体、已知条件约束
- 提示词改为"从给定算子词表中**选择并排序**"，而不是"自己发明创造操作"
- 代码层校验：`operator_chain` 中每个算子必须在词表内，且前一个算子的 `output` 能接上后一个的 `input`（否则标记 `chain_break`）

这一步同时解决两件事：候选不再退化成"组合/替换/迁移/扩展"，以及 `operator_candidates` 为空的问题（模型不再是"无源之水"）。

### 4.6 P1：候选池 + 反思 + 评分排序（借鉴 AI-Scientist）

按 `两个开源项目启示.md` 第四节执行，但落到 v0.4.x 的增量：

1. 生成 **30–50 个种子候选**（一次 LLM 调用，算子链 + 目标 + 一句机制假设）；
2. 聚类去重（按 `operator_chain` 序列 + 目标骨架拓扑指纹），输出"哪些候选本质同构"；
3. 评分（目标匹配 / 创新等级 / 机制可行性 / 文献距离 / 条件兼容性 / 步骤数 / 验证成本）；
4. 选出 4–6 个最终候选 + 明确优先级与互斥关系。

同时引入**历史候选归档**：把上一轮候选一起喂回去，要求"新候选必须与已有候选在算子链序列上不同"。这是原文档点名的机制，也是"五个方案本质相同"的直接解药。

### 4.7 P1：修反馈闭环（借鉴 GPT Researcher）

1. `state["review"]["revision_actions"]` 注入内容节点提示词，内容节点必须逐条回应（`resolved / rejected + 理由`）；
2. 修订历史落库（新增 `study_runs` 表：`run_id / request / plan / consumer_analysis / draft / review / revision_round`），否则"闭环"无法审计；
3. Reviewer 增加第三个**强制**检查块（不是第三个评分维度，权重并入指令符合度，避免破坏 v0.3.0 的 4:1 约定）：`design_compliance`，逐条比对 `design_contract.target_constraints` 与 `innovation_floor`；
4. 独立 `fact_checker` 节点：Writer 之后专门查事实错误、无来源断言、前后矛盾。

### 4.8 P2：语料卫生（3.7 的对策）

- 检索阶段对炔酰胺这类任务启用领域相关性硬门（`relevance_gate=strict` + 领域画像维度），把脂质体/疟疾类文献挡在入库前；
- 或对已有库加"领域成员过滤"：机制抽取只在 `ontology_domains` 命中的论文上跑；
- 支持度阈值按实测分布校准（单篇支持占 99%，`minimum_support=2` 需要改成"单篇可引用、双篇才可下强结论"的分级策略）。

---

## 五、建议的实施顺序与验收门槛

| 阶段 | 内容 | 验收门槛 |
|---|---|---|
| 第 0 步 | 修 `run_v031_test.py` → `run_v040_test.py`，落盘全部中间态 | 磁盘上有 v0.4.0 端到端产物 + 可对照的 v0.3.1 产物 |
| 第 1 步 | 3.1 的 4 项改动（引用校验 + design_context 闭环 + 内容契约） | 候选 JSON 100% 含 `operator_chain` 与 `innovation_level`；3 条被误杀的算子链不再丢失 |
| 第 2 步 | 3.2 知识包选取修复 | 截断后机制超边 1/80 → ≥ 30/80；消费耗时 < 30 s |
| 第 3 步 | 3.4 条件/测量槽位 | 条件 ≥ 300 条、测量 ≥ 200 条；"缺少反应条件"缺口消失 |
| 第 4 步 | 4.4 + 4.5 机制状态层 + 算子词表 | `mechanism_states` 100% 绑定真实 evidence_id；`operator_candidates` 非空且全部在词表内 |
| 第 5 步 | 4.6 候选池 30–50 + 聚类去重 + 排序 | 候选差异轴可解释；至少 2 个候选达 L3；L1 候选不得作为主推 |
| 第 6 步 | 4.7 闭环 + Fact Checker + 落库 | `revise` 轮次中内容确实发生定向变化；悬空引用为 0 |

**不要做的事**（原文档已警告，本轮实测同样支持）：

1. 不要用 GPT Researcher 替换消费节点——它解决报告可靠性，不解决机制创新；
2. 不要照搬 AI-Scientist 的模板执行——它假设实验可代码执行；
3. 不要在补齐条件槽位之前先写"机制状态抽取器"——输入不存在，只会得到更漂亮的空壳；
4. 不要继续用"更长的提示词"代替"结构化中间层"——本轮实测提示词已到 8.6 万 tokens，再加只会更糟。

---

## 附录 A：核查环境说明

- **可复现的诊断脚本**：本轮诊断脚本位于 `.dsh-tmp/`（`diag_bundle.py`、`diag_hyper_rank.py`、`diag_pipeline_gap.py`、`diag_llm_consumer.py`、`diag_refids.py`），均为只读运行，唯一外部影响是调用 1 次消费节点 LLM（90 秒）。建议正式化为 `examples/diag_knowledge_pack.py` 并纳入回归。
- **测试未能完整复跑**：本机 DSH 沙箱下 `TemporaryDirectory()` 创建的目录不可被 SQLite 写入（`unable to open database file`），导致依赖临时库的 42 个测试报错；这是环境限制，不是代码回归。建议在沙箱外复跑 `python -m unittest discover -s tests` 确认 70 passed。
- **真实 LLM 试跑产物**：`.dsh-tmp/consumer_llm_out.json`（消费节点原始输出，可作后续改造的对照基线）。
