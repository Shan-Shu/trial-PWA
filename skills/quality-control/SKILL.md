---
name: quality-control
description: 质量控制节点，在原文献 A/T/Q 质量评估之上，每新增 300 个节点按 IUPAC Gold Book / ChEBI 做全局实体/关系归并
---

# Quality Control（质量控制节点）

## When to use
- 每篇文献完成质量评估并进入知识提取前后；
- 本体新增节点数达到阈值（默认每 300 个）后需要消除跨论文同义实体/重复边时。

## Input
- 文献元数据（质量评分输入）；
- 当前 SQLite 动态本体；
- 领域词典身份（轻量本地层，当前收录常用 ChEBI/Gold Book 术语）。

## Output
- 原质量评估结果：A/T/Q、路由 decision；
- 可选 `quality_control`：`triggered/stats/nodes_before/nodes_after`；
- 全局归并写 `merge_journal`，被删除节点记录 reason，保证可审计。

## Rules
- 归并必须满足同一外部身份：`(node_type, external_source, external_id)`；
- 只用领域词典明确给出的术语，不猜测合并不同配方/掺杂/工艺；
- 同一关系在重指节点后出现重复时，合并 provenance/confidence/evidence_tier；
- 不做“A 与 B 语义相近”的 LLM 自由合并，避免泛称吞并细节。

## 自检
- 是否只处理词典命中的 `Chemical/Method/Concept/Property` 节点？
- `ontology_edges` 是否不存在悬空 source/target？
- 每条 merge 是否有 reason 和 from_ids/to_id？
- 阈值是基于“本轮新增节点”而不是“全部历史节点”触发？

代码入口：`src/research_agent/quality/control.py::maybe_global_merge`；
质量评分提示词：`quality/llm.py::QUALITY_PROMPT_TEMPLATE`。
