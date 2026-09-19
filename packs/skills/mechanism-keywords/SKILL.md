---
name: mechanism-keywords
description: 机制/条件词表与触发式抽取规则，用于超边相关性加权、机制状态抽取、机会缺口与算子链候选
---

# Mechanism Keywords（机制词表技能）

## When to use
- 知识消费节点对知识包做**机制相关性加权**时（哪些超边属于机制描述）；
- 从 event 类超边的 label/证据句归纳 `mechanism_states` 时；
- 挖掘 `opportunity_gaps` / `constraint_conflicts` / `operator_candidates` 时；
- 判定"低信息量结构关系"（`is_a`/`part_of` 等）以做降权时。

## Input / Output
- 输入：超边或证据句文本；
- 输出：机制命中数、规则命名（如 `Lewis acid activation` / `vinyl cation`）、
  键变化列表（`C-N formation`…）、选择性来源（`chiral ligand control`…）。

## Data
- `content/data.json`
  - `mechanism_keywords`：机制关键词（用于加权与命中计数）；
  - `condition_keywords`：化学/实验条件线索词；
  - `activation_rules` / `intermediate_rules` / `selectivity_rules` / `risk_rules`：
    `[[规则名, [关键词...]], ...]`，按顺序匹配，命中首个即返回；
  - `bond_change_rules`：成键/断键短语；
  - `operator_keywords`：算子名 → 关键词（推导算子链用）；
  - `low_value_relation_labels`：低信息量关系标签（降权）。

## 领域覆盖
- 领域包可用 `domains/<kind>/vocab/mechanism.jsonl` 提供**领域增补/覆盖**条目，
  每行形如 `{"section": "intermediate_rules", "name": "...", "keywords": [...]}`；
- 通用包内的规则是"化学向"的默认值；非化学领域应提供自己的 vocab 文件，
  否则机制层会命中很少 → 加载器会记录告警而不是静默通过。

## 代码入口
- `src/research_agent/packs.py::skill_data("mechanism-keywords")`
- `src/research_agent/study/consumer.py`（机制层）
