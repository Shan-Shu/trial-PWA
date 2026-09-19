# v0.4.0 下一步迭代规划：先统一编排，再升级规划与知识消费

> 规划日期：2026-09-11  
> 输入依据：`D:\Desktop\问题与修改方向.md`、`D:\Desktop\两个开源项目启示.md`  
> 当前基线：v0.3.1

## 零、本次确认的三项硬性要求

### 1. 顶层运行顺序必须改为“先规划，再检索”

当前存在两条并行入口：

- `research-agent-pipeline`：用户输入直接进入检索，然后评估、提取知识；
- `research-agent-study`：虽然有 Planner，但检索仍被隐藏在知识消费节点的可选 collector 中，不是独立、明确的执行阶段。

下一版必须统一为一个正式主链路：

```text
用户输入
  → 工作规划节点
  → 生成检索与分析计划
  → 文献检索
  → 质量评估
  → 知识提取
  → 知识消费
  → 内容形成
  → 审核校对
```

约束：

- 用户原话不能直接作为检索输入；
- 检索只能消费 Planner 输出的 `retrieval_plan`；
- `research-agent-pipeline` 保留为内部数据构建子流程，不再作为顶层研究入口；
- Dashboard 的 `/api/study/run` 必须走统一主链路；
- 初始检索后如果发现证据缺口，可以回到检索子流程，但必须仍然遵循 Planner 的计划和补充原因。

### 2. 知识消费节点必须改为 LLM 驱动

当前知识消费节点由代码完成：

- 代码查询 `ontology_edges` 和 `ontology_hyperedges`；
- 代码统计组件、关系、支持数和覆盖度；
- 代码生成固定的 `combination_space_hint`；
- 节点本身不接受也未调用 LLM。

下一版必须新增独立的 `consumer_model`，由 LLM 负责语义判断和设计上下文生成。

但仍应保留确定性代码作为边界控制层：

| 职责 | 代码 | LLM |
|---|---|---|
| 数据库查询、去重、编号、证据映射 | 是 | 否 |
| 证据存在性与引用校验 | 是 | 否 |
| 机制状态理解、冲突识别、机会发现 | 否 | 是 |
| 反应原语选择、算子链设计 | 否 | 是 |
| 检索缺口判断和补检请求 | 提供 schema | 是 |
| 文本归纳、候选方案生成 | 否 | 是 |
| 硬门槛和格式校验 | 是 | 否 |

生产模式不允许静默回退为纯代码消费者。无模型时只能进入明确的 `offline_fallback`，并在结果和日志中标记，不能伪装成正常 LLM 消费。

### 3. 工作规划节点必须同步重构升级

Planner 不能再只是“生成几个 seed_terms 和 creative_operations”，而应成为整个研究任务的统一调度入口。它至少要输出：

```text
任务意图
交付物约束
研究目标和成功标准
检索计划
资料质量标准
分析维度
证据政策
生成任务的设计契约（仅 generative）
停止条件和预算
```

Planner 是入口，不是可选前置步骤。没有有效 Planner 输出时，检索和质量评估不得启动。

## 一、v0.4.0 的目标

v0.4.0 只聚焦三项基础架构改造：

1. 建立唯一的 Planner-first 编排入口；
2. 将知识消费节点改造为 LLM 驱动节点；
3. 将 Planner 升级为检索、分析、生成和评价的统一契约源。

本版本暂不把“大候选池、反思和机制算子库”全部塞入同一批改造。它们是 v0.4.0 完成后的 v0.4.1 核心工作。

## 二、目标执行架构

```text
START
  ↓
planner
  ↓
collection_planner
  ↓
retrieval
  ↓
quality
  ↓
knowledge_extractor
  ↓
knowledge_consumer_llm
  ↓
content_builder
  ↓
reviewer
  ├─ pass → END
  ├─ revise → content_builder
  ├─ need_more_data → collection_planner
  └─ manual_review → END
```

其中：

- `planner`：解析用户意图，生成完整任务契约和 `retrieval_plan`；
- `collection_planner`：把 `retrieval_plan` 转换为可执行的检索请求；
- `retrieval`：只执行检索计划，不自行决定研究范围；
- `quality`：按 Planner 的质量政策评估文献；
- `knowledge_extractor`：从通过质量门槛的文献中提取实体、关系、条件和超边；
- `knowledge_consumer_llm`：消费结构化知识，生成机制状态、机会缺口、设计上下文和补检请求；
- `content_builder`：基于消费结果形成内容或候选方案；
- `reviewer`：检查指令符合度、证据正确性和设计约束，并能触发定向补检或修订。

## 三、工作包 WP1：统一 Planner-first 编排

### 3.1 改造内容

- 将 `run_study()` 定义为唯一顶层研究入口；
- 将现有 `run_topic()` 降级为内部 collection service；
- 在 StudyGraph 中显式增加检索、评估、提取节点或 collection 子图；
- Dashboard `/api/study/run` 注入 Planner、Consumer、Content、Reviewer 模型和 collector；
- `research-agent-pipeline` 增加兼容说明：它是数据构建工具，不是完整研究任务入口；
- 统一状态字段：
  - `request`
  - `plan`
  - `retrieval_plan`
  - `collection_report`
  - `quality_report`
  - `extraction_report`
  - `knowledge`
  - `design_context`
  - `draft`
  - `review`
  - `decision`

### 3.2 Planner 输出的检索计划

```json
{
  "retrieval_plan": {
    "objective": "需要收集什么证据",
    "query_variants": ["英文或中文检索词"],
    "source_mix": ["europepmc", "arxiv", "semantic_scholar"],
    "dimensions": ["mechanism", "substrate scope", "selectivity"],
    "recency_window": "2018-01-01:2026-09-11",
    "max_results_per_query": 20,
    "min_quality": 0.6,
    "must_cover": ["目标骨架", "关键中间体", "反例"],
    "stop_conditions": ["核心机制有至少两条独立证据", "主路线覆盖达到阈值"]
  }
}
```

### 3.3 验收

- 空 Planner 输出时不能进入检索；
- 检索请求中的查询词全部能追溯到 `retrieval_plan`；
- 用户原话不会未经规划直接发给检索节点；
- 从 UI、CLI、服务层启动完整任务时都走同一张图；
- 旧数据构建工具仍可运行，但不再被描述为完整研究流程。

## 四、工作包 WP2：LLM 驱动知识消费节点

### 4.1 改造内容

- `StudyServices` 新增 `consumer_model`；
- `make_knowledge_consumer_node()` 新增 `model` 参数；
- 新增 `consumer` 角色模型绑定和 `CONSUMER_MODEL` 配置；
- 代码先构建可追溯知识包，再交给 LLM；
- LLM 只接收结构化知识包，不直接查询数据库；
- LLM 返回严格 JSON，代码执行 schema 校验、引用校验和异常处理；
- LLM 可按需要返回 `retrieval_requests`，由图路由回 collection 子流程。

### 4.2 LLM 消费输出

第一阶段至少输出：

```json
{
  "summary": "当前知识包的主要机制与研究边界",
  "mechanism_clusters": [],
  "known_conflicts": [],
  "evidence_gaps": [],
  "retrieval_requests": [],
  "design_context": {
    "mechanism_states": [],
    "reaction_primitives": [],
    "opportunity_gaps": [],
    "operator_candidates": []
  },
  "confidence": 0.0
}
```

过渡期可以保留旧字段：

- `component_types`
- `composable_relations`
- `candidate_components`
- `combination_space_hint`

但新逻辑不能继续只依赖这些字段。

### 4.3 验收

- 生产路径中 LLM 调用有明确日志和模型绑定；
- LLM 不可用时状态明确为 `consumer_model_unavailable` 或 `offline_fallback`；
- 不允许静默退化成旧代码逻辑；
- LLM 返回内容不能新增不存在的 pattern/evidence/hyperedge ID；
- 所有输出经过确定性 schema 和引用校验；
- 至少能针对一个真实知识包输出机制冲突、机会缺口和补检请求。

## 五、工作包 WP3：工作规划节点升级

### 5.1 新职责

Planner 从“任务标签生成器”升级为“研究任务总控”，负责：

1. 识别任务性质：summary、frontier、generative、evaluation、proof；
2. 定义研究目标和禁止偏离项；
3. 生成检索计划和质量门槛；
4. 定义分析维度、证据政策和停止条件；
5. 对 generative 任务定义设计契约：
   - 目标对象与拓扑约束；
   - 创新等级下限；
   - 候选数量；
   - 差异轴；
   - 评价维度和硬门槛；
6. 根据反馈决定是否补检或重新设计。

### 5.2 建议结构

```json
{
  "goal": "一句话目标",
  "task_kind": "generative",
  "deliverable": {
    "format": "markdown",
    "language": "zh"
  },
  "retrieval_plan": {},
  "analysis_plan": {
    "dimensions": [],
    "required_comparisons": []
  },
  "evidence_policy": {
    "traceability_required": true,
    "hypothesis_label_required": true
  },
  "design_contract": {
    "objective": "目标方案",
    "target_constraints": {},
    "innovation_floor": "L3",
    "min_candidates": 4,
    "differentiation_axes": [],
    "evaluation_criteria": []
  },
  "stop_conditions": [],
  "budget": {
    "max_collection_rounds": 2
  }
}
```

`creative_operations` 可以保留，但降级为底层实现提示，不能再代表候选方案的核心创新。

### 5.3 验收

- Planner 能独立回答“检索什么、为什么检索、检索到什么程度停止”；
- 生成任务必须包含目标硬约束、创新等级下限和差异轴；
- 检索、Consumer、Content、Reviewer 都直接读取同一份计划，不允许各自重新解释用户需求；
- Planner 输出经 schema 校验后才能开始检索；
- 现有综述任务不因新增字段而回归。

## 六、实施顺序

### 第一轮：统一主链路

1. 绘制并测试新 StudyGraph；
2. 将 collection 子流程接入 Planner 输出；
3. 将 `run_study()` 设为唯一完整任务入口；
4. 保留旧 `run_topic()` 作为内部兼容服务；
5. 增加“未规划不得检索”的测试。

### 第二轮：升级 Planner

1. 修改 Planner Prompt 和规范化函数；
2. 增加 `retrieval_plan`、`analysis_plan`、`design_contract`；
3. 更新 Reviewer 的契约读取；
4. 增加 Planner 结构校验和回归测试。

### 第三轮：接入 LLM Consumer

1. 增加 `consumer_model` 和角色绑定；
2. 将 DB 查询、证据映射保留在代码层；
3. 新增 Consumer Prompt、JSON schema 和引用校验；
4. 增加 `retrieval_requests` 回流路由；
5. 增加模型不可用、解析失败和伪造引用测试。

## 七、v0.4.0 验收门槛

| 指标 | 门槛 |
|---|---|
| 顶层入口 | 所有完整任务均先经过 Planner |
| 检索输入 | 100% 来自 `retrieval_plan` |
| Consumer 模型 | 生产路径必须绑定 LLM |
| Consumer 输出 | 通过严格 schema 和引用校验 |
| 失败状态 | 不允许静默回退纯代码模式 |
| Planner 契约 | 覆盖检索、分析、证据、生成和停止条件 |
| 旧数据构建能力 | 保留，但降级为内部子流程 |
| 现有综述任务 | 不回归 |

## 八、后续版本

- `v0.4.1`：机制状态、反应原语、机会缺口、算子链、30–50 候选池、反思去重、排序；
- `v0.4.2`：Reviewer/Reviser 定向修订闭环、Fact Checker、修订历史；
- `v0.5.0`：第二领域适配器、公开评测集、看板和跨领域验证。

## 九、最终判断

v0.4.0 的首要任务不是马上提升候选方案创新度，而是先修正系统执行结构：

> **Planner 成为唯一入口，检索成为计划的执行结果，Knowledge Consumer 成为真正的 LLM 语义消费节点。**

只有这三项完成后，后续的机制算子和候选池改造才有稳定的承载结构。
