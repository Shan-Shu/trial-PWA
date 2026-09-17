# research-agent Skills

节点级“技能”封装：每份 `SKILL.md` 说明对应节点的使用时机、输入/输出契约、
约束与自检，并指向代码中的提示词常量（单一事实源仍在代码，本目录供人/Agent 快速消费）。

| Skill | 对应节点 | 代码提示词源 |
|---|---|---|
| retrieval-planner | 文献检索·查询规划 | `retrieval/llm.py::PLAN_PROMPT_TEMPLATE` |
| metadata-normalizer | 文献检索·元数据规整 | `retrieval/llm.py::CLEAN_PROMPT_TEMPLATE` |
| quality-assessor | 质量评估 | `quality/llm.py::QUALITY_PROMPT_TEMPLATE` |
| knowledge-extractor | 知识提取 | `knowledge/extractor.py`（SYSTEM/SCHEMA/ATTRIBUTE/RELATION/ERROR_LIST/SELF_CHECK/REFINE） |
| evidence-gap-retrieval | 研究/检索层：证据缺口补强 | `retrieval/skills.py::plan_evidence_gap_queries` |
| iterative-query-expansion | 检索层：单领域深挖 | `retrieval/skills.py::derive_expanded_queries` |
| reference-tracing | 检索层：引用溯源 | `retrieval/skills.py::trace_references` |
| quality-control | 质量控制：原质量评估 + 领域词典全局归并 | `quality/control.py::maybe_global_merge` |

当前版本：v0.3.1（提示词改动请同步更新代码常量，本目录为说明副本）。
