---
name: iterative-query-expansion
description: 从广泛检索结果中提取新的子方向术语并继续检索，仅用于单领域精深挖掘，不做无界递归
---

# Iterative Query Expansion（检索式迭代扩展）

## When to use
- 工作规划节点的 `plan.retrieval.strategy == "deep_single_domain"`；
- 用户明确要深挖某个单一领域，而不是做多领域宽泛综述。

## Input
- 首轮 `seed_terms` 与已命中论文的标题/摘要/关键词；
- 领域边界约束和轮次上限。

## Output
- 下一轮受控检索式；
- 每轮新增候选、去重数、与主题相关度评估。

## Rules
- 只允许从命中文摘/标题/关键词提炼术语，不允许把任意语句作为检索式；
- 新检索式必须保留原领域核心词，最多只替换/新增一个子方向；
- 每轮设去重新增下限；新增去重论文明显下降时必须停止；
- 默认最多 2 轮；领域边界变化过大时丢弃该轮；
- 该 skill 不以参考文献作为证据来源，只负责扩大检索面。

## 自检
- 第二轮查询是否仍能被领域核心词约束？
- 是否只是换了几个同义词而不会带来新论文？
- 是否已经出现跨出原领域的查询？

代码入口：`src/research_agent/retrieval/skills.py::derive_expanded_queries`。
