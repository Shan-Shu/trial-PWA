# 更新日志：v0.1.0 → v0.1.1

> 项目：research-agent
> 版本区间：`v0.1.0`（b752162）→ `v0.1.1`（fe89594）
> 提交：2230cd7 + docs fe89594
> 净变更：36 个文件，+2033 / -126

## 一、版本主题

v0.1.1 同时包含“规模化全流程基建”与“P1 领域画像优化”两部分：

1. 支持数百篇 OA 文献的检索、全文入库、并行质量/知识提取与流式研究；
2. 将领域维度从“检索节点内置模板”前移到“工作规划节点领域画像”；
3. 质量节点不再要求 LLM 估计缺失的被引/H 指数等数据；
4. 知识提取提示词开始支持领域候选/冻结 schema 注入。

## 二、新增能力

| 模块 | 说明 |
|---|---|
| Europe PMC 分页检索 | 修正 `cursorMark` 分页，可从 339 条 OA ynamide 记录中取 320 条唯一候选 |
| Semantic Scholar / Unpaywall / OpenAlex 搜索 | 扩展全文/OA PDF 检索与定位 |
| Dashboard 研究流程视图 | 实时查看 planner/consumer/content/reviewer 状态与事件 |
| 研究流程事件写入 | `study/events.py`，节点运行状态进入 processing_log |
| 流式全流程 | `run_stream_study.py`，检索与提取并行推进 |
| 规模化流式 | `run_scale_ynamide.py`，数百篇边入库边提取 |
| 断点续跑 | `resume_scale_ynamide.py`，复用已入库全文继续提取 |
| 领域画像 | `domains.py`，支持 chemistry/biomedicine/materials/general |
| 图谱大库修复 | 节点超过 1600 时仍能返回前 800 节点间边 |

## 三、P1 优化

### 工作规划节点

- 输出新增 `domain_profile`；
- 化学领域示例维度：反应类型、催化剂与试剂、底物范围、区域/立体选择性、机理、产率等；
- 生物医药领域示例维度：分子机制、生物相容性、免疫、适应症、安全性、临床转化。

### 文献检索节点

- 删除固定“适应症/临床转化/审评审批/生物相容性”维度；
- `RetrievalLLM.plan_queries(topic, dimensions)` 由规划节点维度驱动；
- 未提供维度时不套用固定领域模板。

### 质量评估节点

- 被引次数、H 指数等缺失时输出 `null`；
- 不再让模型“按期刊水平估计”真实数据；
- 缺失项由代码使用中性默认值。

### 知识提取节点

- `domain_profile` 可注入抽取提示词；
- `schema_status=candidate`：允许首轮试用候选类型；
- `schema_status=frozen`：只允许使用冻结 schema；
- 当前为链路预留，尚未切换到完整化学冻结 schema。

## 四、测试

新增测试：

- `tests/test_domain_profiles.py`
- `tests/test_retrieval_sources.py`

全量测试：

```text
Ran 46 tests ... OK
```

## 五、后续说明

v0.1.1 之后又进行了同语料 320 篇新本体重建，输出库：

```text
data/ynamide_320_v011.db
```

该库的节点/边清单见全流程审阅文档。
