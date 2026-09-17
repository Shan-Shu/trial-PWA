---
name: quality-assessor
description: 依据文献元数据输出权威性/时效性子项评分与理由（A/T/Q 由代码公式计算）
---

# Quality Assessor（质量评估）

## When to use
- 文献入库后、进入知识提取前，对其权威性(A)与时效性(T)打分并路由。

## Output（JSON）
`venue_quartile / venue_factor / h_factor / citation_factor / field_velocity / venue_note / rationale`。

## Rubric（节选）
- venue_factor：0.90-0.95 顶刊；0.78-0.88 Q1；0.65-0.75 Q2；0.50-0.60 预印本/未知。
- h_factor：0.9≈H≥60；0.7≈H20-40；0.5≈H5-15；0.4≈缺失/新团队。
- citation_factor：前1%≈0.95、前10%≈0.8、前50%≈0.6、≈0≈0.3；缺失按年份/期刊估计并说明。
- field_velocity：fast=AI/CS；medium=生物医药/材料；slow=数学/理论。

## 约束
- 分值 0-1 保留两位小数；不得编造缺失字段，默认值在 rationale 说明；仅输出合法 JSON。
- 不要自行计算 A/T/Q（由代码按公式与阈值路由）。

提示词源：`src/research_agent/quality/llm.py::QUALITY_PROMPT_TEMPLATE`（`{payload}`）。
