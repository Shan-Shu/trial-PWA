---
name: relation-lexicon
description: 本体关系词表：种子节点/关系类型、同义归一、强断言集合
---

# Relation Lexicon（关系词表技能）

## When to use
- 知识提取节点写入本体时，需要把自由写法归一为受控关系词；
- 初始化本体类型注册表时，需要种子类型；
- 判断某条关系是否属于"强断言"（低置信时需要降级为 candidate）。

## Input / Output
- 输入：模型输出的原始关系词（如 `utilizes`、`lead to`、`composed of`）；
- 输出：受控词（如 `uses`、`results_in`、`made_of`）；无法归一则原样返回。

## Data
- `content/data.json`
  - `seed_node_types`：`[[TypeKey, 中文标签], ...]`；
  - `seed_relation_types`：同上，用于类型注册表；
  - `synonyms`：原始写法 → 受控词（**键必须小写**）；
  - `strong_relations`：强断言集合。

## Rules
- 词表是"受控"的：新增受控词要同时补 `synonyms`，否则同一关系会被拆成多种写法、摊薄支持度；
- 领域包可用 `domains/<kind>/domain.json` 的 `extra_seed_node_types` /
  `extra_seed_relation_types` 追加**领域专用**类型，不要往通用表里塞学科词。

## 代码入口
- `src/research_agent/packs.py::relation_lexicon` / `canonical_relation_type`
- `src/research_agent/ontology/store.py::canonical_relation_type`
