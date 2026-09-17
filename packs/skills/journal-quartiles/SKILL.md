---
name: journal-quartiles
description: 期刊/会议分区、分区得分、引用格式模板与期刊缩写/BibTeX 写法；权威性评分与引用导出的共同数据源，支持 JCR 导入、分科子集与外部覆盖
---

# Journal Quartiles & Citation（期刊分区 + 引用样式技能）

## When to use
- 质量评估节点的权威性因子 A 需要判定期刊分区时；
- 文献库导出引用（GB/T 7714 / APA / ACS / BibTeX / RIS / Markdown）时需要期刊缩写与写法；
- 需要为某个学科补充/修正分区表，或补充期刊缩写时（不要改代码，改数据）。

> **为什么两者同包**：期刊身份、分区、缩写、BibTeX 写法是**同一实体的四个属性**，
> 共用 `packs.normalize_journal_key()` 这一个键。合并方案 v2 决策 D2：
> 不新建 citation 包，避免两份期刊表需要同步。

## Input / Output
- 输入：期刊名（任意大小写与标点，内部按 `packs.normalize_journal_key` 归一：
  大小写/连字符/标点/副标题括号/`the`/`and` 统一）；
- 输出：`Q1..Q4` 或 `None`（**缺失时不猜测**，由 `venue_factor` 按来源类型给中性值）；
  引用层则输出缩写 / BibTeX 写法 / 规范名，未命中时回退原始字符串。

## Data
- `content/data.json`
  - `quartiles`：跨学科顶刊种子（人工维护）；
  - `quartile_scores`：分区 → 权威性分值（默认 Q1 0.95 / Q2 0.80 / Q3 0.65 / Q4 0.50）；
- `content/quartiles_<tag>.json`：**分科子集**，由 JCR 名单生成（见下）；
- `content/citation_styles.json`：
  - `styles`：引用样式定义（`plain` / `bibtex` / `ris` / `markdown` 四种 kind）；
  - `venue_overrides`：按归一键维护期刊的 `abbrev` / `bibtex_venue` / `canonical`。


## 从 JCR 名单生成（推荐做法）
```powershell
# 子集进仓库（默认 tag=chemistry，按学科类别过滤），全量表落到 data/（不入库）
uv run python examples/import_jcr_xlsx.py \
    --xlsx "2026年度JCR期刊名单（完整版）.xlsx" --tag chemistry
```
产出：
- `packs/skills/journal-quartiles/content/quartiles_chemistry.json`（默认生效，489 条）
- `data/jcr/jcr_quartiles_full.json`（20337 条；用 `RA_JOURNAL_QUARTILES` 启用）

两者的格式都是**平铺字典** `{"journal name": "Q1"}`，
也兼容 `{"quartiles": {...}}` 包装与 `{"data": ...}` 之外的其它键（非字符串值忽略）。

## Override
- `RA_JOURNAL_QUARTILES` 可指向一个或多个 JSON 文件（`;` 分隔），后加载者覆盖前者；
- 典型用法：`.env` 里指向全量 JCR 表，本地生效但不进仓库。

## Rules
- 表里没有的期刊**不加分也不减分**；
- 查询一律走 `packs.journal_quartile(name)`，不要自己拼 key；
- 引用层取缩写/写法一律走 `library.citation.venue_label(rec, abbreviated=...)`，
  同样不要自己 `lower()` 后查表；
- **数据来源声明**：`quartiles_chemistry.json` 与全量表由使用者提供的 JCR 名单导入，
  JCR 为 Clarivate 授权数据，仓库内只保留本领域子集，全量表放在被忽略的 `data/`。

## 代码入口
- `src/research_agent/packs.py::journal_quartiles` / `journal_quartile` /
  `normalize_journal_key` / `quartile_scores`
- `src/research_agent/quality/scoring.py::venue_factor`
- `src/research_agent/library/citation.py::venue_label` / `format_citation` / `export`
- `examples/import_jcr_xlsx.py`

## 回归测试
- `tests/test_packs.py`（分区与覆盖）
- `tests/test_citation.py`（四种样式、期刊缩写、缺字段、中文 cite key）
