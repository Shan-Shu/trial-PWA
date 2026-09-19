---
name: task-kind-hints
description: 任务类型判定关键词（模型不可用时的确定性兜底），覆盖 generative/summary/frontier/evaluation 与 content_type
---

# Task Kind Hints（任务类型判定技能）

## When to use
- Planner 的 LLM 不可用或解析失败，需要用确定性规则判断任务性质时；
- `normalize_plan` 需要为缺省字段兜底时。

## Input / Output
- 输入：用户请求文本；
- 输出：`task_kind ∈ {generative, summary, frontier, evaluation}` 或 `content_type`
  `∈ {research_report, frontier_review, research_directions, experiment_protocol}`。

## Data
- `content/data.json`
  - `generative` / `summary` / `frontier` / `evaluation`：关键词数组（中英混排）；
  - `content_type`：内容类型 → 关键词数组；无命中时回落到 `research_report`。

## Rules
- 关键词只用于**兜底**；有模型时必须由模型判定，不要用关键词覆盖模型结果；
- 多语言场景请补充对应语种关键词（当前默认中英混排）。

## 代码入口
- `src/research_agent/packs.py::skill_data("task-kind-hints")`
- `src/research_agent/study/planner.py::infer_task_kind` / `infer_content_type`
