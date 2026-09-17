# “金标准”构建指南

> 

---

## 一、这份指南是什么

金标准 = 一组「论文原文 → 理想抽取结果」的人工权威标注。它有三个用途，优先级由高到低：

1. **评估**：拿模型抽取结果与金标准对齐，算实体/关系/事件的 Precision / Recall / F1；
2. **调优**：校准置信度阈值、二次精修触发条件、合并规则；
3. **解锁 few-shot**：从高一致金标准中挑 1–2 篇进提示词

金标准追求的不是“模型现在会怎么抽”，而是“**这段文本里，该领域专家认为哪些事实应当被记成什么结构**”。

## 二、验收口径（先对齐再干活）

一条金标准记录只有在满足以下条件时才成立：

- 只基于**给定原文**，不靠领域外的背景脑补（可以在讨论里说明背景，但写入标注必须有原文支撑）；
- 结构字段齐全：类型/名称/关系词/置信度/evidence 都按本指南；
- evidence 能定位到原文句子（允许节选 ≤300 字符）；
- 



## 五、本体 schema 速览

标注输出与知识提取节点输出同构（便于直接对齐）：

```json
{
  "entities": [
    {"type": "...", "name": "...", "aliases": ["..."],
     "attributes": {}, "confidence": 0.0, "evidence": "原句"}
  ],
  "relations": [
    {"type": "...", "subject": "...", "predicate": "...", "object": "...",
     "confidence": 0.0, "evidence": "原句"}
  ],
  "events": [
    {"type": "Experiment|Study|Discovery|ClinicalTrial|Observation",
     "trigger": "...", "participants": ["..."], "time": null,
     "attributes": {}, "confidence": 0.0, "evidence": "原句"}
  ]
}
```

## 六、实体标注规则

### 6.1 抽什么

把“被陈述的核心事物”记成实体：材料、方法、细胞/动物模型、生物学过程、性能、疾病、设备等（类型清单见附录 A）。

### 6.2 硬规则（对照 ERROR LIST）

| #   | 规则               | ✗ 错误                                                   | ✓ 正确                                |
| --- | ---------------- | ------------------------------------------------------ | ----------------------------------- |
| E1  | name 必须是名词性术语/专名 | `These results suggest that CPC promotes osteogenesis` | `Calcium phosphate cement`          |
| E2  | 不同配方/掺杂/比例不得并入泛称 | 把 Mg-CPC、Sr-CPC 并入 `Calcium phosphate cement`          | 各自独立实体，组成写 attributes               |
| E3  | 同一概念全文统一         | 一处 `hydroxyapatite`、一处 `HA` 且分开建                       | 主实体 `Hydroxyapatite`，`HA` 进 aliases |
| E4  | 推断 vs 直接陈述       | 把“推测”记成确定                                              | 依文本调整 confidence（见第十节）              |
| E5  | 不抽纯结论语           | 把“显著促进成骨”整句当实体                                         | 抽 `osteogenesis`，促进关系另记             |

### 6.3 实体命名

- 优先领域标准全称：`Poly(lactic-co-glycolic acid)`（别名 `PLGA`）；
- 缩写放 aliases，name 尽量放无歧义标准形；
- 实体之间“粒度对齐”：方法拆到可复用程度即可（如 `Dual-crosslinking strategy` 与其两个子工艺可同时存在，用 `part_of`/属性说明关系，而不是只留一个）。

## 七、关系标注规则

### 7.1 从受控词表选词（附录 B 全表）

示例常用词：`promotes` / `inhibits` / `regulates` / `enables` / `releases` / `made_of` / `uses` / `evaluates` / `has_property` / `results_in` / `is_a` / `part_of` …

### 7.2 同义归一

同义动词必须归一到词表词：`enhance/facilitate/accelerate → promotes`；`consist of/composed of → made_of`；`lead to/contribute to → causes` 等。禁止“一个意思换着词造新关系”。

### 7.3 硬规则

| #   | 规则                        | ✗                                                             | ✓                                           |
| --- | ------------------------- | ------------------------------------------------------------- | ------------------------------------------- |
| R1  | 能具体就别兜底                   | `CPC related_to osteogenesis`                                 | `CPC promotes osteogenesis`                 |
| R2  | `related_to` 仅兜底且**克制**   | 大面积铺 related_to                                               | 只有确实无更具体语义时才用                               |
| R3  | 报告语动词不作关系类型               | `X indicates Y` / `X suggests Y`                              | 这类内容写 evidence，不建关系                         |
| R4  | subject/object 必须与实体名完全一致 | `CPC` vs `Calcium phosphate cement`                           | 字符串严格一致（或列在实体清单里）                           |
| R5  | 方向/主客不可颠倒                 | `BMSCs promote osteogenesis` 记成 `osteogenesis promotes BMSCs` | 依原文语义定方向                                    |
| R6  | 跨句可推断要降档                  | 两句拼接的“A→C”                                                    | confidence 降到 0.6–0.74，并留一句直接句作 evidence 基线 |

> 你方在 v0.0.6 同语料对比里已见 `associated_with`/`indicates` 这类非受控残留，金标准里明确禁止，作为反例。

## 八、事件标注规则

事件 = “做了什么”（实验/过程/发现），不是“结论怎么说”。

- `trigger`：精简名词短语，如 `in vivo calvarial defect repair study`；禁止整句/报告语（`we demonstrated…`）。
- `type`：`Experiment | Study | Discovery | ClinicalTrial | Observation`。
- `participants`：只列**直接参与**的关键实体（≤5），禁止同句共现拉郎配。
- `time`：能给就给（`P3D`/`8 weeks`/`day 25`），给不出写 null。
- 同一种“性质”的实验合并不合并？——**按“实验设计是否相同”判断**：同材料同模型不同剂量应分别事件并保留剂量属性；完全同设计只算一次。
- 属性建议：能填就填 `in_vitro/in_vivo`、`endpoints`（检测指标）、`markers`、`observations`、`model`、`dose/timepoint/group`。

## 九、属性与数值规则

1. 只写有意义的值；None/空串省略该键；
2. 键名 snake_case：`fabrication_method`、`defect_site`、`cell_types_involved`；
3. 数值拆 `{"value": 数字, "unit": 单位}`：`5 wt% → {"value": 5, "unit": "wt%"}`；时间用 `P2D`/`day 25` 类写法；
4. 模板字段（尽力而为）：
   - Material：`composition/components/fabrication_method/architecture/application`
   - Model：`species/defect_site/model_type`
   - Disease：`etiology/pathology_site/related_signs`
   - BiologicalProcess：`regulators/cell_types_involved/downstream_outcome`
   - Property：`metric_type` + 数值
5. 形容词（excellent/controllable）不得冒充定量，只能放 `rating` 并在 evidence 保留原文。

## 十、置信度标尺（人工标注同样要打分）

标尺与模型提示词一致，便于对齐：

| 区间        | 含义          |
| --------- | ----------- |
| 0.90+     | 原文多句/多段交叉印证 |
| 0.75–0.89 | 原文单句直接支持    |
| 0.60–0.74 | 由上下文明确推断    |
| <0.60     | 存疑，尽量不标注    |

另：**review/commentary/临床病例类文献**，同一断言整体下调一档；`may/could/possibly` 等推测断言 ≤0.7。

## 十一、完整示例

原文（示意，来自“骨修复新材料”主题）：

> “In this study, a strontium-doped calcium phosphate cement (Sr-CPC) was fabricated. Sr-CPC released strontium ions and promoted osteogenic differentiation of BMSCs in vitro. In vivo, implantation in a rat femoral condyle defect model showed enhanced new bone formation at 8 weeks.”

金标准（节选）：

```json
{
  "entities": [
    {"type": "Material", "name": "Strontium-doped calcium phosphate cement",
     "aliases": ["Sr-CPC"],
     "attributes": {"dopant": "strontium", "base": "calcium phosphate cement"},
     "confidence": 0.95,
     "evidence": "a strontium-doped calcium phosphate cement (Sr-CPC) was fabricated"},
    {"type": "Chemical", "name": "Strontium ion", "aliases": [], "attributes": {},
     "confidence": 0.92, "evidence": "Sr-CPC released strontium ions"},
    {"type": "CellType", "name": "Bone marrow mesenchymal stem cell",
     "aliases": ["BMSC"], "attributes": {"species": "unspecified"},
     "confidence": 0.9, "evidence": "promoted osteogenic differentiation of BMSCs"},
    {"type": "BiologicalProcess", "name": "Osteogenic differentiation", "aliases": [],
     "attributes": {}, "confidence": 0.95, "evidence": "promoted osteogenic differentiation of BMSCs"},
    {"type": "BiologicalProcess", "name": "New bone formation", "aliases": [],
     "attributes": {}, "confidence": 0.9, "evidence": "showed enhanced new bone formation at 8 weeks"},
    {"type": "Model", "name": "Rat femoral condyle defect model",
     "aliases": [], "attributes": {"species": "rat", "defect_site": "femoral condyle"},
     "confidence": 0.95, "evidence": "implantation in a rat femoral condyle defect model"}
  ],
  "relations": [
    {"type": "made_of", "subject": "Strontium-doped calcium phosphate cement",
     "predicate": "以磷酸钙骨水泥为基体、掺锶", "object": "Calcium phosphate cement",
     "confidence": 0.9, "evidence": "a strontium-doped calcium phosphate cement"},
    {"type": "releases", "subject": "Strontium-doped calcium phosphate cement",
     "predicate": "缓释锶离子", "object": "Strontium ion",
     "confidence": 0.92, "evidence": "Sr-CPC released strontium ions"},
    {"type": "promotes", "subject": "Strontium-doped calcium phosphate cement",
     "predicate": "促进 BMSC 成骨分化（体外）", "object": "Osteogenic differentiation",
     "confidence": 0.9, "evidence": "promoted osteogenic differentiation of BMSCs in vitro"}
  ],
  "events": [
    {"type": "Experiment", "trigger": "in vitro osteogenic differentiation assay of BMSCs",
     "participants": ["Strontium-doped calcium phosphate cement",
                      "Bone marrow mesenchymal stem cell",
                      "Osteogenic differentiation"],
     "time": null, "attributes": {"in_vitro": true, "endpoints": ["osteogenic differentiation"]},
     "confidence": 0.9, "evidence": "promoted osteogenic differentiation of BMSCs in vitro"},
    {"type": "Experiment", "trigger": "in vivo implantation in rat femoral condyle defect model",
     "participants": ["Strontium-doped calcium phosphate cement",
                      "Rat femoral condyle defect model", "New bone formation"],
     "time": "8 weeks",
     "attributes": {"in_vivo": true, "observations": ["enhanced new bone formation"]},
     "confidence": 0.9, "evidence": "showed enhanced new bone formation at 8 weeks"}
  ]
}
```

> 注意：`In this study` 这类报告语没有成为实体或事件；“enhanced”落在事件 attributes，而不是造一个 `enhances` 事件。

## 

## 附录 A：实体类型清单

`Method, Material, Device, Drug, Disease, Model, Metric, Dataset, Task, Theory, Parameter, Property, Application, Organism, CellLine, Chemical, Target, BiologicalProcess, Technology, Tool, Standard, Regulation, Institution, Researcher`（确有必要才新造类型，需在裁决记录说明）。

## 附录 B：受控关系词表

`uses, evaluates, compares, part_of, improves_upon, based_on, causes, promotes, regulates, activates, releases, differentiates_into, inhibits, treats, targets, has_property, made_of, produced_by, related_to(兜底), cites, published_in, authored_by, developed_by, correlates_with, enables, complicates, risk_factor_for, results_in, is_a`

同义归一（节选）：employ/utilize/apply→uses；assess/benchmark/validate→evaluates；consist of/composed of→made_of；lead to/trigger→causes；enhance/facilitate/accelerate/induce→promotes；release/elute→releases；suppress/downregulate→inhibits；exhibit/possess→has_property；derived from→based_on；outperform/better than→improves_upon；act on/bind/interact with→targets；associated with→related_to；correlate with→correlates_with；enable/allow→enables；risk factor for/predispose to→risk_factor_for；result in→results_in；is a/kind of/type of→is_a。

## 附录 C：报告语黑名单（不得成为实体/事件/关系词）

`these results / these findings / the histological analysis showed / in this study / we demonstrated / we found / it was observed / results showed / data indicated / this review summarizes / taken together …`；以及动词 `show, suggest, indicate, demonstrate, reveal, found, observe, describe, summarize, propose` 作“关系类型”或实体核心词时同样禁止。
