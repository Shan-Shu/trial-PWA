# 更新日志：v0.0.5 → v0.0.6（含 v0.0.6.1 提速补丁）

> 项目：research-agent（LangGraph 三节点：文献检索 → 质量评估 → 知识提取 → 动态本体）
> 版本区间：`v0.0.5`（744edd6）→ `v0.0.6`（6be77fb）→ `v0.0.6.1`（e20df84，当前）
> 净变更：16 个文件，+1235 / -17 行（对比 v0.0.5 tag）
> 日期：2026-09-08

---

## 一、版本与提交

| 版本 | Commit | 说明 |
|---|---|---|
| v0.0.6 | `cafb20c` + 文档 `6be77fb` | 知识提取节点优化（硬性禁区/二次精修/语料提醒）+ 三开源学习报告 + skills 首次入库 |
| v0.0.6.1 | `7009c50` + 文档 `e20df84` | 提速补丁：并行批量提取脚本 + 知识节点 `run_init` 开关 |

## 二、变更总览

本次版本主题：**围绕知识提取节点的提示词工程升级与抽取质量控制**，并把「学习 gpt-researcher / AI-Scientist v1 / v2」的结论落到实现。质量评估节点未改动（用户指示：其在动态本体构建中的作用暂不关键）；金样本 few-shot 未添加（用户指示暂缓）。

按模块的改动规模：

| 模块 | 文件 | 性质 |
|---|---|---|
| 知识提取 | `knowledge/extractor.py`、`knowledge/node.py` | 核心：+276 行 |
| 全局配置 | `config.py` | 新增精修开关/阈值 |
| 文献检索 | `retrieval/llm.py` | 提示词并入 PubMed 语法/日期（源自 gpt-researcher 学习） |
| 质量评估 | — | **无改动** |
| 文档 | `docs/open_source_agents_learning_report.md`（新增 333 行） | 学习报告 |
| Skills | `skills/`（README + 4 份 SKILL.md） | 首次入库 |
| 工具脚本 | `examples/compare_report_v5.py`、`examples/run_knowledge_batch.py` | 新增 |
| 测试 | `tests/test_extractor_refine.py` | 新增 7 例 |
| 版本 | `pyproject.toml`、`uv.lock`、`VERSIONS.md` | 0.0.6 / 0.0.6.1 |

## 三、按模块详细变更

### 3.1 知识提取节点（核心）

针对此前自查暴露的三大历史问题——**衔接语/报告语实体**、**不同配方被并入泛称（细节丢失）**、**兜底关系过泛/放大**——做了三层加固：

**a. 硬性禁区清单 `ERROR_LIST_HINT`（提示词）**
在抽取提示词中新增 8 条「绝对禁止」级规则，每条带反例/正例：
1. 禁止报告语/衔接语/整句当实体；
2. 禁止合并丢失细节（不同配方/掺杂/比例/工艺必须分实体）；
3. 禁止兜底关系放大（能落具体词就不用 related_to）；
4. 禁止同一概念名漂移（须复用库中规范名，变体进 aliases）；
5. 禁止属性无证据/形容词冒充定量；
6. 禁止把推测写成确定断言（confidence 限制）；
7. 禁止断连/字符串不一致（subject/object/participants 严格对齐）；
8. 禁止伪造/改写 evidence。

**b. 自检清单强化 `SELF_CHECK_HINT`**
由 6 条升级为与 ERROR LIST 编号一一对应的输出前自查，要求「违反先修正再输出」。

**c. 低置信/泛化关系二次精修（v0.0.6 新增机制）**
- `KnowledgeExtractor.extract_with_refine()`：首遍抽取后由 `flag_issues()` 判定是否存在 ① 置信度 < 阈值 ② 兜底泛化关系（默认 `related_to`）③ 疑似报告语实体；
- 命中则追加一轮「定向精修」（`REFINE_PROMPT`，借鉴 AI-Scientist reflection 循环）：只改点名条目、粘住原意、无改进则原样输出并带收敛信号 `I am done`；
- 精修解析失败自动回退首遍结果，保证鲁棒；每轮最多点名 12 个问题、默认最多 2 轮。

**d. 语料兜底关系提醒 `_generic_relation_warning`（v0.0.6 新增）**
知识节点运行时统计库内 `related_to` 类兜底边数量并注入提示词，要求「除非证据明显更强，否则不要继续添加同类泛化关系」，抑制跨文献自我放大。

**e. `make_knowledge_node(..., run_init=False)`（v0.0.6.1 新增）**
允许调用方跳过节点内部的重复建表 DDL（由并行 runner 预建连接），消除多 worker 并发建表互相等锁问题。

**f. 配置项（`config.py`）**
| 配置 | 默认 | 说明 | 环境变量 |
|---|---|---|---|
| `knowledge_refine_enabled` | True | 是否启用二次精修 | `RA_KNOWLEDGE_REFINE` |
| `refine_min_conf` | 0.6 | 低置信触发阈值 | `RA_REFINE_MIN_CONF` |
| `refine_max_items` | 12 | 单轮最多点名问题 | `RA_REFINE_MAX_ITEMS` |
| `refine_max_attempts` | 2 | 精修最大轮数 | `RA_REFINE_MAX_ATTEMPTS` |
| `generic_fallback_types` | `("related_to",)` | 视为泛化的兜底关系 | — |

### 3.2 文献检索节点（小改）

- `PLAN_PROMPT_TEMPLATE` 增加第 8 条：明确目标库为 PubMed（AND/OR/NOT、短语、通配符、`[Title/Abstract]`、`[dp]` 年份过滤均受支持），并注入**当前日期** `{date}`。
- 该改动源自 gpt-researcher 学习（「针对检索后端语法 + 当前日期给硬约束」），先于 v0.0.6 在本地完成，本次一并入库。

### 3.3 质量评估节点

**无改动**。仍为 GLM 4.7 Flash 输出因子 + 代码按公式计算 A/T/Q 并路由。

### 3.4 Skills 目录首次入库

`skills/`（此前工作区未跟踪）正式入版本：`README.md` + `retrieval-planner` / `metadata-normalizer` / `quality-assessor` / `knowledge-extractor` 四份 SKILL.md，作为提示词的「人/Agent 可读副本」（单一事实源仍在代码常量）。

### 3.5 学习报告

新增 `docs/open_source_agents_learning_report.md`（333 行）：gpt-researcher / AI-Scientist v1 / AI-Scientist v2 的架构、提示词工程、skills、工程纪律、横向对比、对 research-agent 的差距与采纳记录。

### 3.6 工具脚本

- `examples/compare_report_v5.py`：v0.0.1–v0.0.5 五版本同语料对比页（此前未跟踪，本次入库）。
- `examples/run_knowledge_batch.py`（v0.0.6.1）：多 worker 并行批量提取。预建连接 + WAL + 分片 + 逐篇进度汇报；支持 `--model pro|flash`、`--workers`、`--longest`、`--refine-attempts`。

### 3.7 测试

`tests/test_extractor_refine.py` 新增 7 例：精修触发/收敛/失败回退/无问题不精修/提示词含 ERROR LIST 与 I am done 等。全量测试 **36 passed**。

## 四、同语料 50 篇验证记录（v0.0.5 基线 vs v0.0.6）

运行参数：同一批 PubMed 50 篇（clean_text 主要为摘要），`deepseek-v4-pro`，v0.0.6 并行 5 workers；对比库 `data/ontology_v05.db` ↔ `data/ontology_v06.db`。

| 指标 | v0.0.5 | v0.0.6 | 解读 |
|---|---|---|---|
| 运行墙钟 | — | 2607s（≈43.5 min，5 并发） | 50/50 完成，0 失败 |
| 本体节点 / 边 | 476 / 533 | 622 / 695 | +31% / +30% |
| 孤立节点率 | 9.0% | 6.75% | 连通性改善 |
| 每篇实体 | ~9.5 | ~12.4 | 细节保留更多 |
| `related_to` 兜底边 | 18 | 2 | 硬禁区+精修+语料提醒生效 |
| `evaluates` | 5 | 21 | 关系更具体 |
| 事件断言 | 79 | 90 | 实验/过程事件增多 |
| 材料登记 lcmat | 106 | 129 | 配方级实体增厚 |
| 空属性节点率 | 67% | 75% | 见「已知问题」 |
| top3 关系占比 | 36.6% | 38.6% | 仍偏高，见「已知问题」 |

精修触发统计：50 篇中 7 篇触发二次精修（refine=1），其余 43 篇一遍通过；无失败/回退。

## 五、已知问题与遗留

1. **7 条非受控关系边**（v0.0.6 库）：`associated_with ×5`、`indicates ×2`。
   - `associated_with`：抽取提示词已要求归一到 `related_to`，但 store 层 `RELATION_SYNONYMS` 未收录该 snake_case 变体（属代码级小修）；
   - `indicates`：模型自造的报告语动词关系（提示词 ERROR LIST 可补一条禁止项）。
   - 建议：`RELATION_SYNONYMS` 增加 `associated_with / associated with → related_to`；ERROR LIST #3 补「indicates/suggests 等报告语动词禁止作为关系类型」；是否需要重跑同语料验证待用户确认。
2. 空属性率上升（67%→75%）：主因是概念型实体增多且 attributes 未填，语义仍落在 evidence；后续「实验设计支撑」改造中补充属性模板。
3. 质量节点优化与金样本 few-shot 按用户指示暂缓。

## 六、兼容性与数据隔离

- v0.0.6 对比运行写入独立库 `data/ontology_v06.db`，**不并入**既有 `data/research_agent.db` / `ontology_v0x.db`；
- `run_init` 默认 `True`，既有调用方（pipeline/tests）行为不变；
- 提示词改动均向后兼容：旧库无需迁移即可用新代码重新提取。

## 七、本地产物状态

| 产物 | 状态 |
|---|---|
| `docs/llm_prompts_review.md`（全部 LLM 提示词审阅稿） | 未提交（供审阅） |
| `examples/compare_report_v6.py`（v05 vs v06 对比生成器） | 未提交 |
| `data/ontology_v06.db`、`output/compare6/` | 本地生成（gitignore） |

