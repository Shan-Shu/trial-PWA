---
name: knowledge-extractor
description: 把论文文本抽取为实体/关系/事件 JSON 并写入可合并的动态本体（受控词表、实体复用、属性规范）
---

# Knowledge Extractor（知识提取）

## When to use
- 质量达标的文献进入知识提取时（单篇、按文本块）。
- 需抑制“衔接语实体 / 配方合并丢失 / 兜底关系泛化”等历史质量问题（v0.0.6）。

## Output（JSON）
`entities[] / relations[] / events[]`；每个元素含 type/name/aliases/attributes/confidence/evidence。

## 核心约束
1. name 必须是名词性术语；**硬性禁区**：禁止报告语/衔接语/整句作实体（ERROR LIST #1）。
2. 优先复用运行时注入的“库中已有规范名”；配方/掺杂/比例/工艺细节**禁止并入泛称**（#2）。
3. relation.type 用受控词表并同义归一；能用具体词就必须用，related_to 仅兜底且不得放大（#3）。
4. event.trigger 用精简名词短语，participants ≤5；事件为断言（v0.0.5 起入库到旁路表，不生成节点/involves）。
5. attributes：无 None/空值、snake_case、数值拆 {value,unit}、形容词仅放 rating（#5）。
6. 输出前按自检清单核对；推测性断言与 review/commentary 证据的 confidence 下调一档（#6/#8）。
7. 二次精修（v0.0.6）：首遍结果存在低置信 / 泛化兜底关系 / 报告语实体时，追加一轮定向精修；
   只处理被点名条目，无改进则原样输出（收敛信号 I am done），精修失败回退首遍。

运行时注入：论文头（标题/期刊/年份）+ 库中已有实体（Type: Name，最近有连接的 150 条）+
库内兜底关系统计提醒（抑制泛化关系放大，v0.0.6）。

提示词源：`src/research_agent/knowledge/extractor.py`
（SYSTEM_HINT/SCHEMA_HINT/ATTRIBUTE_HINT/RELATION_VOCAB/ERROR_LIST_HINT/SELF_CHECK_HINT/REFINE_PROMPT），
开关与阈值：`config.py::Settings`（RA_KNOWLEDGE_REFINE / RA_REFINE_MIN_CONF 等）。
