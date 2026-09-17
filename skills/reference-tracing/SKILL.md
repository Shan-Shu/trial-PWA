---
name: reference-tracing
description: 沿种子文献的参考文献/引文扩展同一领域的原始文献，需要严格相关性门控与深度限制
---

# Reference Tracing（参考文献/引文溯源）

## When to use
- 工作规划节点的 `plan.retrieval.strategy == "deep_single_domain"`；
- 需要寻找领域经典文献、方法源头，或对某一原始研究做向前/向后引用扩展。

## Input
- `seed_paper_keys[]`：内容节点或检索节点确认的锚点论文；
- 领域/主题边界；
- 扩展方向：`backward`（参考文献）、`forward`（引文）或 `both`。

## Output
- 候选引用记录（论文 key、标题、DOI、触发来源、扩展深度）；
- 通过/拒绝理由，用于控制相关性。

## Rules
- 默认最大深度为 1-2，禁止对所有节点无限递归；
- 优先扩展综述/元分析锚点，它们常指向同一命题的多篇原始研究；
- 候选记录必须与领域边界或触发锚点的主要实体重叠，否则不进入正文抽取；
- 单独参考文献不等于关系被复现：即使候选入库，仍需重新抽取并重算支持数；
- NCPSSD 等无稳定引用接口的源不应走此 skill。

## 自检
- 是否只返回了引用网络可达的文献，而不是“所有被引文献”？
- 是否混入通用方法学/数学/统计等领域外引用？
- 是否有足够候选可追踪、又不会指数爆炸？

代码入口：`src/research_agent/retrieval/skills.py::trace_references`。
