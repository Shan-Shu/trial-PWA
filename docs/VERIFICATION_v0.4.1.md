# v0.4.1 验证报告：机制信息能否真正进入候选方案

> 验证日期：2026-09-11
> 对照基线：`docs/STATUS_AND_NEXT_STEPS_v0.4.0.md`（v0.4.0 工作树核查）
> 语料：`data/ynamide_multiazabicycle_v020.db`（384 篇 / 2821 节点 / 3012 边 / 3851 超边）
> 工具：`examples/dump_knowledge_pack.py`、`examples/run_v040_baseline.py`

## 一、结论

| 上一版结论 | 当前状态 | 判定依据 |
|---|---|---|
| 机制信息能被提炼却被两条硬编码路径丢弃 | ✅ 已修复 | 被误杀的 3 条算子链现在保留；`design_context` 五键全部进入内容节点 |
| 截断后机制超边只剩 1/80 | ✅ 已修复 | **81/90**（含机制关键词的超边占比） |
| 提示词 25.8 万字符 / 8.6 万 tokens | ✅ 已修复 | **6.1 万字符 / ≈2.0 万 tokens** |
| 离线兜底仍在生产旧的组件组合上下文 | ✅ 已修复 | 五键同结构，`combination_space_hint` 已移除 |
| 候选只有"组合/迁移/替换/扩展" | ✅ 已修复 | 候选核心字段改为算子链，低阶算子被限制为 L1/L2 并会被审核拦下 |
| 无候选池/反思/排序 | ✅ 已实现 | 两阶段候选池 + 聚类去重 + 评分排序 + 首选/备选/不建议 |
| `revision_actions` 断链 | ✅ 已修复 | 审核与事实核查意见合并后注入下一次内容生成，要求逐条回应 |
| Reviewer 不检查创新层级 | ✅ 已修复 | 8 项设计契约检查，纯 L1–L2 候选直接判 critical |
| 无 Fact Checker / 无落库 | ✅ 已实现 | `fact_checker` 节点 + `study_runs` 表 |
| 条件/测量数据几乎为空 | ⚠️ 通路已修，数据待重抽 | 透传链路修复并加确定性追问；既有库仍是旧数据，需重跑知识提取才会增长 |
| 语料跨域污染 | ✅ 已加硬门 | 检索入库前 `apply_topic_relevance_gate` |

## 二、知识包指标（`dump_knowledge_pack.py` 实测）

```json
{
  "mined": { "patterns": 200, "evidence": 204, "hyperedges": 150 },
  "prompt": { "patterns": 24, "hyperedges": 90,
              "chars": 60991, "approx_tokens": 20330 },
  "mechanism_signal": {
    "mined_hyperedges_with_mechanism": 81,
    "kept_hyperedges_with_mechanism": 81,
    "kept_total": 90
  },
  "design_context": {
    "mechanism_states": 12, "reaction_primitives": 6,
    "opportunity_gaps": 8, "operator_candidates": 8,
    "constraint_conflicts": 9,
    "traceability": { "total_items": 43, "traceable_items": 43, "ratio": 1.0 }
  }
}
```

对照上一版：

| 指标 | v0.4.0 | v0.4.1 | 变化 |
|---|---:|---:|---|
| 截断后含机制关键词的超边 | 1 / 80 | **81 / 90** | ×81 |
| 消费提示词字符数 | 258,322 | **60,991** | −76% |
| ≈tokens | 86,107 | **20,330** | −76% |
| 离线 design_context 键 | 旧 6 键（组件组合空间） | **五键 + traceability** | 结构替换 |
| 机制状态（可溯源） | 无 | 12（ratio 1.0） | 新增 |
| 算子链候选 | 0（离线无、LLM 输出被误杀） | 8 | 新增 |
| 单次消费耗时 | 90 s | 120 s（提示词更小但思考更长） | 持平 |

## 三、真实 LLM 试跑（消费节点）

一次真实 `deepseek-v4-pro` 消费（384 篇语料，26K tokens 提示词）结果：

- `mechanism_states`：5 条，含"Au(I) π-酸性活化 → 乙烯基阳离子 / α-亚胺金卡宾"，
  `bond_changes` 为 `["C-N formation","C-C formation","1,2-迁移（可选）"]`，
  并给出 `selectivity_control`、`catalyst_cycle`、`termination`；
- `operator_candidates`：5 条，算子链为真实词表算子
  （`cascade_termination` + `carbene_migration`，均 L3）；
- `opportunity_gaps`：4 条，其中 `GAP-0001` 明确指向 Planner 硬约束
  "氮原子数≥3 的多元氮杂环"并给出缺失环节；
- `constraint_conflicts`：2 条，直接比对硬约束"氮原子数≥2"与现有证据的实际覆盖；
- `invalid_references`：`['E-3762-1','E-3802-1']`（形状正确的超边外层编号，
  已通过超边证据句纳入 `evidence` 列表，下一轮不再误判）。

> 对照：v0.4.0 同一次试跑的 `operator_candidates` 为 **0**，且 3 条真正的算子链设想
> 被当作 `invalid_references` 删除。

## 四、端到端运行的吞吐观察与对策

真实 LLM 端到端运行时暴露了一个 v0.4.0 未暴露的新问题：
**一次性要求 30–50 个"完整"候选（含风险、验证计划、可行性论证）会产生超长 JSON，
单次请求可能挂起数十分钟。**

对策（本次已实现）：

1. 候选池改为**两阶段**：

```text
阶段 1（宽松）：生成 ≤16 个骨架候选（目标 + 算子链 + 等级 + 硬约束回应 + 差异）
   ↓ 代码：指纹聚类去重 → 评分排序 → 选出 4–6 个
阶段 2（详细）：只对入选候选补全风险、验证计划、可行性论证
```

2. 新增 `run_study(budget_override=...)` 与
   `examples/run_v040_baseline.py --max-candidates N`，可在入口处下调候选池规模；
3. 阶段 2 不允许改写算子链（机制方向由阶段 1 决定，避免深化步骤偷偷换机制）；
4. 新增 `--content-mode deterministic`：骨架由代码生成、只对入选候选做一次深化，
   用于在真实语料上快速验证图接线，避免长请求阻塞验证。

仍未解决的问题：即使 16 个骨架候选，单次输出仍偏长。后续可进一步
把"骨架生成"按机制簇拆成 2–3 次并行调用。

## 五、端到端接线验证（真实 Planner + 真实 Consumer + 确定性内容骨架）

运行命令：

```powershell
python examples\run_v040_baseline.py --db data\ynamide_multiazabicycle_v041.db \
    --out-dir output\v040_baseline_wiring --max-candidates 10 --content-mode deterministic
```

结果（`study_runs` 表实测，run `e62e537a7fff`）：

| 环节 | 实测结果 |
|---|---|
| Planner | 真实 LLM 生成 `retrieval_plan`（query 含 dizaine/pyrimidine 等目标骨架词）、`design_contract`、`budget` |
| collection | `collection_skipped`（未配 collector，符合预期） |
| Consumer（真实 LLM） | 200 模式卡 / 354 证据 / 150 超边；`mechanism_states=4`、`reaction_primitives=4`、`opportunity_gaps=4`、`operator_candidates=3`、`constraint_conflicts=3`；`traceability ratio=0.889` |
| 内容骨架（代码）+ 候选深化（LLM） | 3 个候选，算子链分别为 `strain_release+intermediate_capture`、`dearomatization+intermediate_capture`、`carbene_migration+intermediate_capture`，等级均判为 **L4** |
| 排序 | 首选 = `strain_release+intermediate_capture`；`innovation_floor=L3`，全部候选 `floor_met` |
| Reviewer（真实 LLM） | 判定 4 项设计契约状态（1 met / 3 partial），并给出 7 条可执行修订意见；因骨架由代码生成，按新规则一轮后转人工，不再空转 |
| 落库 | `study_runs` 写入 plan / consumer / draft / review（含候选、算子链、选择结果） |

关键点：**候选的机制标签不再是"组合/替换/迁移/扩展"**，
而是 `strain_release`、`dearomatization`、`carbene_migration`、`intermediate_capture`
这类机制级算子，并且每个候选都带可校验的算子序列与推出等级。

> 注意：审核节点的 4 项设计契约状态来自**模型自身**的逐项判定；
> 代码层的确定性设计检查（8 项）在模型审核通过时才会与模型结果合并展示，
> 且"关键失败项"始终以代码层结论为准（`deterministic_review` 的 critical 会覆盖模型 `pass`）。

## 五之二、离线全链路验收（真实语料 + 全确定性路径）

同一语料上跑 Planner → collection → Consumer（确定性层）→ 内容骨架 →
Reviewer → Fact Checker 的完整图（不调用任何 LLM）：

```text
status: reviewed | decision: pass
knowledge: patterns=200 evidence=396 hyperedges=150
design_context: mechanism_states=12, reaction_primitives=5, opportunity_gaps=8,
                operator_candidates=8, constraint_conflicts=9
                traceability: 42/42 (ratio 1.0) | mode: deterministic
candidate_pool: generated=6 after_dedupe=6 selected=6 duplicates=0
  [1] L4 ['carbene_migration', 'intermediate_capture']
  [2] L3 ['cascade_termination']
  [3] L3 ['carbene_migration']
  [4] L3 ['selectivity_lock']
  [5] L3 ['selectivity_lock']
  [6] L3 ['selectivity_lock']
fact_check: pass
确定性设计检查（7 项）: 全部 met
```

这条路径证明：**在没有模型参与的情况下，候选方案也已经由机制词表算子构成，
并且通过设计契约的全部硬检查**——而不是像 v0.4.0 那样退回"组合/替换/迁移/扩展"。

> 已处理：`normalize_operator_chain()` 现在会丢弃相邻重复算子并记入
> `duplicate_operators`（此前会出现 `selectivity_lock + selectivity_lock` 这种占位链）。
> 仍可改进：兜底路径的多个候选举同一算子（如上表 4–6），
> 去重按"算子序列 + 目标"分组所以不会被合并；后续应在兜底生成时按算子去重。

## 七、真实 LLM 内容路径：延迟定位与对策（本轮新增）

上一轮报告"内容节点真实 LLM 调用长时间无响应"。本轮定位到具体原因并处理：

| 实验 | 提示词 | 设置 | 结果 |
|---|---|---:|---|
| 采集节点（对照） | 26K tokens | `reasoning_effort=low` | 100 s 返回 ✅ |
| 内容节点（旧配置） | 60K 字符 | `reasoning_effort=low, max_tokens=8000` | **>15 分钟无响应** ❌ |
| 内容节点（省略 reasoning_effort） | 同一条 60K 字符提示词 | 不传 `reasoning_effort`，`max_tokens=8000` | **353 s 返回，输出 15,376 字符** ✅ |
| 内容节点（缩小提示词） | 48K 字符 | 手工构造 | 88 s 返回，输出 10,730 字符 ✅ |

结论：瓶颈不是"提示词太长"本身，而是 **`reasoning_effort=low` 与长结构化输出组合**时
DeepSeek 侧长时间不返回。处理方式：

1. `models.py` **默认不再发送 `reasoning_effort`**（可用 `DEEPSEEK_REASONING_EFFORT` 重新启用）；
2. 新增 `study/model_call.py`：所有研究节点模型调用改为**带超时**的守护线程调用，
   超时即返回 `None` 并由节点降级到确定性实现；
3. 超时可经 `RA_STUDY_CONSUMER_TIMEOUT` / `RA_STUDY_CONTENT_TIMEOUT` /
   `RA_STUDY_REVIEW_TIMEOUT` / `RA_STUDY_FACT_CHECK_TIMEOUT` 调整（默认 900/900/600/600 秒）；
4. `_compact_knowledge()` 增加硬预算（55000 字符）与逐级削减，内容提示词从
   9.1 万字符降到 **6.4 万字符**。

超时降级实测（真实模型 + 90 s 超时）：

```text
elapsed: 290.6s（其中阶段 1 在 90s 触发超时，阶段 2 继续尝试）
status: drafted
generated_by: deterministic_skeleton_expanded
model_error: 模型调用超时（>90s）
candidates: 6  | levels: ['L4','L3','L3','L3','L3','L3'] | markdown: 14,297 字符
```

即：**单个模型不返回不再能卡死研究链路**，图会继续产出带机制算子链的候选。

### 7.1 完整真实 LLM 端到端跑通（本轮关键证据）

在真实 384 篇语料上跑完整链路（Planner 与 Consumer 与内容与审核全部真实 `deepseek-v4-pro`）：

```powershell
python examples\run_v040_baseline.py --db data\diag_content.db \
    --out-dir output\v040_baseline_llm --max-candidates 6
```

`summary.json` 实测：

```text
status: manual_review | overall_score: 0.59
knowledge: patterns=200 evidence=354 hyperedges=150
design_context: mechanism_states=8, reaction_primitives=10, opportunity_gaps=6,
                operator_candidates=6, constraint_conflicts=4
                traceability: 34/34 (ratio 1.0) | mode: llm
candidate_pool: generated=6 after_dedupe=6 selected=6
入选候选（全部达到 L3 下限，floor_met=6/6）：
  [1] L3 ['carbene_migration','intermediate_capture','selectivity_lock']
  [2] L3 ['polarity_reversal','intermediate_capture','dearomatization']
  [3] L3 ['radical_polar_crossover','ring_contraction_expansion']
  [4] L3 ['dearomatization','heteroatom_insertion','selectivity_lock']
  [5] L3 ['strain_release','intermediate_capture','selectivity_lock']
  [6] L3 ['transient_directing','dearomatization','selectivity_lock']
```

对照 v0.4.0 的同一任务：候选的核心标签是"组合已有方案/跨域迁移/替换组件/扩展对象范围"。
现在每个候选都是**机制级算子序列**（极性反转、卡宾迁移、去芳构化、杂原子插入、
张力释放、瞬态导向、选择性锁定…），并带硬约束回应与算子链校验结果。

耗时曲线（同一 run 的事件时间戳）：

| 阶段 | 耗时 |
|---|---|
| Planner | ~100 s |
| Consumer（真实 LLM，26K tokens 提示词） | ~350 s |
| 内容阶段 1 骨架 + 阶段 2 深化（真实 LLM） | ~700 s |
| 每轮审核 + 每轮修订内容 | ~180 s / ~420 s |
| 3 轮后转人工（`manual_review`），总时长约 24 分钟 | — |

结论：**内容路径不再是"无响应"，而是"可完成但慢"**；慢的根因是推理型模型在长提示词 +
长结构化输出上的单次延迟（约 6 分钟/次）。这属于后续优化项（按机制簇拆分并行），
不影响本轮目标的达成。

## 八、条件/测量：真实 LLM 重抽验证（本轮新增）

用真实 `deepseek-v4-pro` 对 384 篇语料重跑知识提取
（`examples/run_knowledge_batch.py --src data/ynamide_multiazabicycle_v020.db --db data/cond_recheck.db --model pro`）。

| 指标 | 存量库（旧 schema/旧链路） | 重抽库（本次改动后） | 变化 |
|---|---:|---:|---|
| 超边数 | 3851 | 39 | 规模不同，看密度 |
| conditions | 34 | **120** | — |
| measurements | **0** | **44** | 0 → 44 |
| conditions / 超边 | 0.009 | **3.08** | ×340 |
| measurements / 超边 | 0.000 | **1.13** | 0 → 1.13 |

抽出的条件键与测量指标（真实数据抽样）：

```text
condition_key: temperature, solvent, mol%, duration, catalyst, ...
metric:        yield, enantiomeric_excess, diastereomeric_ratio, ...
```

对照存量库的 34 条条件全部只有 `time` 一种键、0 条测量，可以看出
**P0-5 的槽位修复在真实模型上生效**：温度/溶剂/催化剂负载/时间与收率/ee/dr
这些"第三层条件兼容性与第六节成本评估"必需的字段，现在真正落库了。

> 说明：重抽库只跑了少量论文（受单篇 5–8 分钟的真实模型耗时限制），
> 因此绝对数量小；全量 384 篇需要跑完整批处理。

## 九、测试与回归

```text
Ran 90 tests in 6.1s
OK
```

新增测试覆盖：

| 文件 | 覆盖点 |
|---|---|
| `tests/test_study_feedback.py` | 事实核查 5 项（伪造引用/无来源断言/数值缺证据/模型不可覆盖）、修订意见进入内容提示词、`study_runs` 落库 |
| `tests/test_hyperedge_conditions.py` | 条件与测量落库、消费知识包携带条件/测量、缺量化信息的确定性追问 |
| `tests/test_domain_profiles.py` | 低阶候选被审核拦下、有算子链时达到 L3 且审核通过 |
| `tests/test_retrieval_skills_control.py` | 领域相关性硬门 6 项（跨域丢弃/短文本放行/中文匹配/报告字段） |
| `tests/_tmpdir.py` | 受限沙箱下临时目录可写回退（修复 42 个既有测试无法运行的问题） |

## 十、待验证项（诚实标注）

1. **条件/测量存量数字**：链路与提示词已修复，并在少量论文上经真实模型验证
   （见第八节）；但 384 篇全量库仍是旧数据，需要跑完整批处理才会整体更新。
2. **完整 LLM 内容路径的耗时**：瓶颈已定位（`reasoning_effort` 组合）并修复，
   真实模型下内容阶段单次调用从"无响应"变为约 6 分钟返回（实测 353 s/15,376 字符）；
   但一轮完整生成仍需多次这样的调用，端到端总时长在真实语料上依然是分钟到十几分钟级，
   后续应按机制簇拆分并行化。
3. **候选创新等级的"真实含金量"**：等级由算子链序列推出（代码规则），
   只保证"用了机制级算子"，不保证该机制组合在化学上成立；仍需人工/实验判断。
4. **跨文献支持度**：384 篇中 96% 的边只有单篇支持，跨文献共识类判断仍不成立。
5. **确定性设计检查在模型审核通过时的展示**：目前合并在指令符合度里，
   若模型自行产出了 `design` 类条目，代码层的条目会被 `normalize_review` 覆盖，
   仅在 critical 层面生效；后续应把两者并集呈现。
6. **兜底候选的算子分布**：确定性兜底路径会为多个候选举同一算子
   （如 3 个候选都用 `selectivity_lock`），去重按"算子序列 + 目标"分组不会合并它们；
   后续应在兜底生成时按算子去重。
