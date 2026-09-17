# 《"金标准"构建操作指南》

> 项目：research-agent（文献检索 → 质量评估 → 知识提取 → 动态本体）
> 文档版本：draft v0.1（面向多人协作启动版）
> 适用语料域：骨修复新材料 / 骨再生（bone repair biomaterials），可推广到其它主题域
> 配套版本：以 v0.0.6 提示词与本体 schema 为标注基准

---

## 0. 一句话目标

组织 **3–10 名成员**，用**双人独立标注 + 专家仲裁**的方式，为一批论文产出**人工可复现、可追溯、有版本**的"正确答案"（实体 / 关系 / 属性 / 事件 / 质量评分），用于：① 客观评测知识提取节点（P/R/F1）；② 校准置信度阈值；③ 沉淀领域合并/命名规则；④ 为将来的"金样本 few-shot"积累素材。

---

## 1. 术语与定位（先对齐，避免歧义）

| 术语 | 定义 | 在本项目中的载体 |
|---|---|---|
| **语料（corpus）** | 待标注的论文集合 | PubMed 文献 + 精校文本（当前 v0.0.5 同语料 50 篇可作起点） |
| **金标准（gold standard）** | 经过多人标注+仲裁、冻结发布的"权威答案"整体 | `gold/` 目录 + 冻结 tag（如 `gold-v0.1`） |
| **金样本（few-shot examples）** | 从金标准里挑选、用于喂给提示词的少量示范（论文片段→期望 JSON） | 后续从 gold 中导出（本版暂不做） |
| **评测集（test set）** | 与调参/开发隔离的 gold 子集 | 发布时按篇随机切分（如 8:1:1 train/dev/test，标注阶段先不切） |
| **标注单元（unit）** | 一次标注的最小对象 | 建议**逐句/逐段**（对应抽取的 chunk） |

> ⚠️ 关键纪律：**金标准建设阶段，参与人员不接触模型在某篇上的输出**（避免锚定），或采用"先人工、后对照模型"的两阶段法并在记录里注明。

---

## 2. 建设原则

1. **可追溯**：每条标注都记录 `paper_key → sentence_id → annotator → 版本 → 依据句`。
2. **独立优先**：同一单元至少 2 人独立标注，先不互相看结果。
3. **仲裁终审**：不一致项由仲裁员（组长/领域专家）裁决，裁决理由写进决议表。
4. **版本冻结**：任何发布版带 tag；发布后只增不改（修订走新版本）。
5. **防泄漏**：gold 不参与任何模型"学习/示例"直到评测方案明确；参与评测时用与开发隔离的切分。
6. **可复现**：从语料版本、标注指南版本、标注软件版本到仲裁记录全部记录。
7. **宁缺毋滥**：不确定的断言不硬标（记录为"存疑"），宁少勿错。

---

## 3. 组织与角色

建议最小 5 人；可一人多岗，但**标注员与仲裁员不得同人**（仲裁员不参与被仲裁条目的独立标注）。

| 角色 | 人数建议 | 主要职责 | 需要的能力 |
|---|---|---|---|
| **负责人/组长（PM）** | 1 | 定范围、排期、看板、发布 | 项目管理 + 领域常识 |
| **领域专家（顾问）** | 1–2 | 制定/维护标注细则；仲裁疑难项 | 骨修复材料/组织工程/生物材料研究背景 |
| **标注员 A/B/C…** | 3–6 | 独立标注实体/关系/属性/事件 | 可培训的科研人员/研究生 |
| **交叉复核员** | 标注员互任 | 检查对方标注的漏标/错标 | 同上 |
| **仲裁员（组长或专家）** | 1–2 | 合并/裁决不一致，写决议 | 领域专家优先 |
| **工程/质检（可 PM 兼任）** | 1 | 工具、格式校验、一致性计算、冻结发布 | 会用 SQLite/JSON/脚本 |

**沟通约定**
- 每周 1 次同步会：对标准则、疑难案例、进度。
- 建一个"疑难案例库"：标注中发现的边界案例统一收集，由专家周会定夺，并**回写标注细则**（细则随案例迭代）。

---

## 4. 建设对象与粒度（分三层）

金标准按流水线分层建设，**核心是第 3 层（知识抽取）**；前两层工作量小、先做，作为抽取评测的过滤条件。

### 层 1：元数据层（客观性高，双人校对即可）
字段：title / venue / pub_year / pub_date / doi / authors（name, orcid）/ affiliations / publication_status / source_type。
判定：以论文题名页、PubMed 原始记录为准；**校对而非主观打分**。不一致极少，由工程员直接修正。

### 层 2：质量评估层（A/T/Q 人工评分）
对照现有 rubric（venue_factor/h_factor/citation_factor/field_velocity）给出：
`authority A`、`timeliness T`、`quality Q=0.6A+0.4T`，并给 `confidence_human`（1–5）。
判定：以期刊分区表、作者团队检索、被引统计为准；注明"依据版本/检索日期"。
> 该层用于验证质量节点是否可靠；如团队认为价值有限，可先只做 10–20 篇抽检。

### 层 3：知识抽取层（核心）
对象：对每篇（逐 chunk/逐句）标注：
- **entities**：type / name（canonical）/ aliases / attributes / evidence_sentence / confidence_human；
- **relations**：type（受控词表）/ subject / predicate / object / evidence_sentence / confidence_human；
- **events**：type（Experiment/Study/Discovery/ClinicalTrial/Observation）/ trigger / participants / time / attributes / evidence_sentence / confidence_human；
- **不标注项**：跨句推理的泛化结论、报告语（These results suggest…）、无原句支撑的推断——与抽取提示词 ERROR LIST 对齐。

---

## 5. 语料抽样与范围

### 5.1 范围建议（可裁剪）
- 起点：`data/ontology_v05.db` / `ontology_v06.db` 中同一批 50 篇 PubMed（骨修复新材料）；
- 覆盖维度：期刊层级（Q1–Q4）、年份（近 5 年为主）、文献类型（research / review / clinical / in vitro / in vivo / animal model）、材料体系（CPC、水凝胶、nHA、PCL、掺杂/复合等）均衡。
- 目标：第一阶段 **50 篇**；每篇标注窗口建议取正文 1–4 个 chunk（与 `max_extract_chunks` 对齐，当前语料为摘要级文本，通常 1 个 chunk）。

### 5.2 抽样规则
用分层随机抽样（按 journal 分区 × 文献类型分层），避免标注员只遇到同类论文导致细则偏差。抽样表由工程员生成并在组内公示（含 paper_key + 分层标签）。

---

## 6. 标注流程（每篇/每单元）

```text
① 培训/校准（首次 5–10 篇全员同标，对照细则对答案）
   ↓
② 独立标注（A/B 两两背靠背，互不查看）
   ↓
③ 合并比对（脚本自动 diff：新增/缺失/不一致清单）
   ↓
④ 交叉复核（对不一致项先由标注员互相解释，能收敛则直接改）
   ↓
⑤ 仲裁（仍不一致 → 仲裁员裁决并写决议；决议回写细则）
   ↓
⑥ 抽检（仲裁后按 10% 抽检，由未参与该篇的第三人复核）
   ↓
⑦ 冻结（每批达成一致性门禁 → 写 gold 版本并 tag）
```

**一致性门禁（建议）**
- 实体级 pairwise F1 ≥ **0.85**；关系级（规范化后）pairwise F1 ≥ **0.80**；
- Cohen's κ ≥ **0.70**（实体类型级）；
- 抽检误标率 ≤ **5%**；
- 未达门禁的单元退回标注员补标（不修改他人已冻结记录，只新增修订记录）。

---

## 7. 标注细则（核心速查）

### 7.1 实体标注
- type 从受控类型选：Material / Method / Model / BiologicalProcess / Chemical / Property / Disease / Application / CellLine / Organism / Device / Drug / Target / Parameter / Metric / …（见 `ENTITY_TYPES`）；确无匹配才新造，且需在备注说明。
- name 用**规范名**：优先领域标准写法 + 全称，缩写放 aliases（如 name="Poly(lactic-co-glycolic acid)", aliases=["PLGA"]）。
- **材料配方粒度**：不同组成/掺杂/比例/工艺 = 不同实体（Magnesium-doped CPC ≠ Strontium-doped CPC ≠ CPC），禁止并入泛称。
- 报告语/衔接语/整句**不得**成为实体（These results suggest…、The histological analysis showed…）。
- attributes 只放有原句支撑的事实；数值拆 `{value, unit}`。

### 7.2 关系标注
- type 从 `RELATION_VOCAB` 受控词表选（uses/evaluates/compares/promotes/inhibits/releases/regulates/activates/differentiates_into/targets/has_property/made_of/…共 30 类）；
- 同义必须归一（"enhance/facilitate/accelerate/induce"→ promotes）；**禁止**自造变体关系（如 associated_with → 应归 related_to；indicates/suggests 属于报告语，不作关系）；
- 兜底关系 `related_to` 仅在确实无更具体关系时使用；subject/object 必须是实体的规范名，字符串级一致；
- 方向（subject→object）按语义方向，如 "CPC promotes osteogenesis"，不是反过来。

### 7.3 事件标注
- 只标"做了什么的实验/过程/发现"（Experiment/Study/Observation/Discovery/ClinicalTrial）；
- trigger 用精简名词短语（如 "in vivo calvarial defect repair study"）；participants ≤5 且指向实体；
- 可给 time（如 P8W / day 25）与 attributes（endpoints/markers/observations 等，可选）。

### 7.4 置信度（human）
沿用现有标尺，但由人给：
- 0.9+：多句/多段交叉印证；
- 0.75–0.89：单句直接支持；
- 0.6–0.74：上下文明确推断；
- <0.6：存疑，倾向不输出（标注为"候选/存疑"）。

### 7.5 evidence
- 必须能引用原文句子（复制粘贴，≤300 字符）；不能凭印象概括。

### 7.6 边界情形速查

| 情形 | 处理 |
|---|---|
| review/综述的断言 | 可标注，但 confidence_human 下调一档，备注 source=review |
| "may/could 可能促进" | 只标方向性候选关系，confidence 不高于 0.6 |
| 摘要里说"另有数据未显示" | 不标注（无原句支撑） |
| 同一材料不同写法 | 归并到既有规范名，另一写法进 aliases |
| 同一配方不同文献表述 | 以配方级实体对齐；配比不同则分开 |
| 动物/体外实验结论与临床 | 分开记录 model_type / evidence_tier，不混用 |
| 有争议/团队不会 | 标记 unsure，走疑难案例库，绝不猜 |

---

## 8. 数据格式与存储

### 8.1 目录规划（建议在仓库新建 `gold/`）
```text
gold/
  README.md                 # 本指南速读 + 当前状态
  GUIDE.md                  # 本操作指南副本（或链接到 docs/）
  schema.md                 # 字段字典与枚举
  corpus/
    sample_plan.json        # 分层抽样表
  annotations/              # 每篇一个 JSONL（一行一个标注单元）
    <paper_key>.jsonl
  conflicts/                # 不一致清单（脚本生成）
  decisions/                # 仲裁决议表（CSV/MD）
  releases/
    gold-v0.1/              # 冻结版快照（只读）
  tools/                    # diff/一致性/IAA/导出脚本（后续可放 examples/）
```

### 8.2 标注记录字段（JSONL 示例）
```json
{
  "schema_version": "gold-0.1",
  "paper_key": "pubmed:42643270",
  "sentence_id": "s012",
  "kind": "entity",
  "type": "Material",
  "name": "Carboxymethyl chitosan hydrogels with gradient nano-hydroxyapatite",
  "aliases": ["CMCS/nHA hydrogels"],
  "attributes": {"components": "carboxymethyl chitosan; nano-hydroxyapatite"},
  "confidence_human": 0.9,
  "evidence": "In this study, ... nHA-containing hydrogels were prepared ...",
  "status": "annotated",
  "annotators": ["A", "B"],
  "resolved_by": "arbitrator1",
  "decision_note": "两标注一致，仲裁通过",
  "updated_at": "2026-09-08T10:00:00+08:00"
}
```

### 8.3 Git 约定（多人协作）
- 每人一个分支：`gold/<name>`；提交信息带 `paper_key`；
- `conflicts/` 与 `annotations/` 通过 PR 合入 `gold/main`；
- **冻结发布**：组长创建 tag `gold-v0.1`；发布后文件只读（用 git tag + 发布说明）。

---

## 9. 一致性（IAA）与质量统计

**匹配规则（先定义再算数）**
- 实体：`normalized_name` 相同即命中（忽略大小写/空格/全半角）；aliases 匹配算命中；
- 关系：subject–type–object（均规范化）三元组相同算命中；
- 属性：键名 + value+unit 均相同算命中；
- 事件：trigger 归一后相同 + participants 集合一致算命中。

**指标**
- 每对标注员：实体/关系/事件 **pairwise F1**；
- 多人 ≥3 时取两两均值；
- Cohen's κ（实体类型与关系的分类一致性）；
- 与模型（v0.0.6）对比将在 gold 冻结后统一做（P/R/F1 + 错误类型归因）。

**仲裁决议表（decisions）至少记录**
`paper_key | sentence_id | 冲突类型 | 标注员A取值 | 标注员B取值 | 裁决值 | 理由 | 是否回写细则`

---

## 10. 里程碑与排期建议

| 阶段 | 范围 | 产出 | 门禁 |
|---|---|---|---|
| M0 对齐 | 细则定稿 + 工具就绪 | GUIDE/schema/抽样表 | 全员校准会 |
| M1 Pilot（校准） | 5–10 篇全员同标 | 细则 v0.2 + 疑难案例库 | 全员 pairwise F1≥0.8 |
| M2 Alpha | 20–30 篇双人标注 | gold-alpha 批次 | 实体 F1≥0.85、κ≥0.70 |
| M3 Beta | 至 50 篇 | gold-beta（50 篇） | 全门禁 + 10% 抽检 |
| M4 冻结 | 复审 + 版本化 | `gold-v0.1` tag + 导出评测集 | 冻结只读 |

> 工作量粗估：摘要级 1 篇 ≈ 20–40 分钟/人（含实体+关系）；全文级 1 篇 ≈ 60–120 分钟/人。按 50 篇 × 2 人 × 30 分钟 ≈ 50 人·时（不含仲裁与细则迭代）。

---

## 11. 与 research-agent 的对接（冻结后）

1. **评测**：写 `gold/` 评测脚本（对齐 gold 与 v0.0.6 抽取结果，输出按实体/关系/属性/事件的 P/R/F1 与错误类型分布）；
2. **规则反哺**：gold 暴露的合并/命名/关系分歧 → 更新 `RELATION_VOCAB`、`RELATION_SYNONYMS`、ERROR LIST 与本体合并队列；
3. **阈值校准**：gold 的人工置信度 vs 模型 confidence → 校准 `refine_min_conf`、`strong_edge_min_conf`；
4. **金样本**：从 gold 挑 2–4 个高一致性片段作为 few-shot（用户此前暂缓，待金标准成熟后再启动）；
5. **实验设计支撑**：gold 中方法/模型/终点字段积累到一定量后，可支撑"实验方案可行性粗筛"评估。

---

## 12. 常见风险与规避

| 风险 | 规避 |
|---|---|
| 标注员看模型输出被锚定 | 盲评（先人工后对照）；对照环节单独记录 |
| 多人宽松/严格不一致 | 校准会 + 细则案例化 + κ 门禁 |
| "共识偏误"（少数服从多数压过正确） | 仲裁必须写理由；专家有一票复核权 |
| 疲劳导致漏标 | 单次标注 ≤2h；10% 抽检兜底 |
| 细则与抽取提示词脱节 | 细则版本与提示词版本联动（gold 标注基准注明 v0.0.6） |
| 语料泄漏进模型训练/示例 | 冻结前任何人不得用 gold 做 few-shot/微调 |

---

## 附录 A：标注员速查卡（可打印）

- 实体 5 问：是名词性事物吗？规范名对吗？配方粒度够细吗？有原句证据吗？是报告语吗（是→不标）？
- 关系 3 问：类型在词表吗（同义归一了吗）？方向对吗？能更具体吗（能→别用 related_to）？
- 事件 3 问：是"做了某事"而非结论吗？trigger 精简吗？participants ≤5 且指向实体吗？
- 不确定就标 unsure，绝不猜。

## 附录 B：字段字典（速览）

| 字段 | 必填 | 说明 |
|---|---|---|
| schema_version / paper_key / sentence_id | ✓ | 溯源 |
| kind | ✓ | entity \| relation \| event \| quality |
| type / name / object / trigger … | 视 kind | 见 7 节 |
| attributes | 选 | snake_case；数值 {value, unit} |
| confidence_human | ✓ | 0–1 |
| evidence | ✓ | 原句 ≤300 字符 |
| annotators / resolved_by / decision_note | 流程填 | 仲裁记录 |
| status | ✓ | annotated \| in_review \| resolved \| unsure |

