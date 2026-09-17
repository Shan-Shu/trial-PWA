---
name: metadata-normalizer
description: 把多源原始元数据规整为规范记录（作者/单位/期刊/DOI/状态等）
---

# Metadata Normalizer（文献元数据规整）

## When to use
- 检索命中后或质量节点发回“元数据缺漏”时，对单条文献记录做规范化/补全。

## Input / Output
- 输入：原始元数据 JSON（`{payload}`）。
- 输出：严格 JSON：`title/venue/venue_issn/source_type/publication_status/pub_year/pub_date/doi/authors[]`。

## Constraints
- 标题/期刊 Title Case；作者 “Last, First”→“First Last”，缺失→`Unknown`。
- ISSN/DOI 不编造；pub_year 四位整数否则 null；pub_date 缺失补全规则见提示词。
- source_type 按 期刊 DOI→journal / 预印本→repository / 会议→proceedings 判定。

提示词源：`src/research_agent/retrieval/llm.py::CLEAN_PROMPT_TEMPLATE`。
