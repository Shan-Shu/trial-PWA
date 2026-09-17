---
name: evidence-gap-retrieval
description: 根据内容形成节点给出的低支持边，为同一实体对/关系生成补强检索，目标是提高跨论文支持数
---

# Evidence Gap Retrieval（证据缺口反向检索）

## When to use
- 工作规划节点的 `plan.retrieval.strategy == "evidence_gap"`；
- 或用户要求对本体已有结论进行多源验证、补强、共识检查；
- 存在由内容形成节点返回的 `edge_gaps`，即“当前证据较弱但值得补证”的边。

## Input
- `edge_gaps[]`：`pattern_id / source_type / source_name / relation_type / target_type / target_name / support_count / target_support / reason`。
- `domain_profile` 与分析维度；
- `topic/seed_terms`：用来限制检索不能漂移出当前领域。

## Output
- 一组经过相关性门控的检索计划或候选文献；
- 每个任务标明触发它的 `pattern_id`、目标关系、期望支持数和检索理由。

## Rules
- 只选择高优先级缺口：一般优先 `support_count < 2`、非 `is_a/cites/published_in/authored_by`、
  置信度不低、且与任务维度相关。
- 一次不要补几千条边；默认取 5-20 条缺口，按置信度、证据层级、任务相关性排序。
- 每条检索式以原实体名/规范名或别名为主体，并夹带目标/同义词，不能只放宽泛领域词。
- 检索结果必须与触发缺口相关；标题/摘要不含源实体或目标实体的记录应被丢弃。
- 检索新增论文后需重新抽取并重算 `support_count`；不满足期望支持时可保留为证据缺口，
  但不能把“找到相关论文”当作“关系已被验证”。

## 自检
- 每个缺口是否都记录了原 `pattern_id`？
- 查询是否会从“A 影响 B”漂移到泛泛的“A 领域综述”？
- 预期支持数、实际新增论文数、重新合并后的支持数是否可审计？

代码入口：`src/research_agent/retrieval/skills.py::plan_evidence_gap_queries`。
