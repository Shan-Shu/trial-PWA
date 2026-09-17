---
name: retrieval-planner
description: 把简短研究主题拆解为 4-6 条互补的英文检索式（覆盖核心/方法/应用/机理/性能/变种/优化/产业化/评价等子领域）
---

# Retrieval Planner（文献检索·查询规划）

## When to use
- 用户给出简短主题（可能只有几个词，如“新型骨修复生物材料”）需要按子领域轮询检索时。

## Input
- `topic`：研究主题（可为中文）。

## Output（只输出）
- JSON 数组字符串，4-6 条（窄主题 ≥3、宽主题 ≤6）英文检索式。

## Constraints
- 每条只聚焦 1-2 个紧密相关子领域；避免关键词大量重复。
- 目标库 PubMed：允许 `AND/OR/NOT`、双引号短语、`*`、`[Title/Abstract]`、`[dp]` 年份过滤。
- 注入当前日期以支持“近期进展”类检索；不输出解释/注释。

## 自检
- 覆盖了核心关键词 + 方法 + 应用，且至少触及性能/机理/产业化等另两个子领域？
- 均可被 `json.loads` 直接解析？

提示词源：`src/research_agent/retrieval/llm.py::PLAN_PROMPT_TEMPLATE`（含 `{topic}`、`{date}` 占位）。
