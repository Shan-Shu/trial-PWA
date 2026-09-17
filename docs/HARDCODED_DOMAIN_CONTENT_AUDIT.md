# 硬编码专业领域内容审计（v0.4.1 工作树）

> **处理结果（v0.4.2）**：本文列出的 16 处全部处理——
> 内容迁到可加载的包层 `packs/`（4 个技能包 + 5 个领域包），
> 加载器 `src/research_agent/packs.py`，使用与扩展说明见 `packs/README.md`，
> 回归测试 `tests/test_packs.py`（14 项）。
> 下方为审计时的原始记录，保留作对照。

> 审计日期：2026-09-11
> 审计范围：`src/research_agent/**`、`examples/**`、`src/research_agent/config.py`
> 方法：常量表扫描（模块级大写常量）+ 领域关键词密度扫描 + 逐个调用点确认
> 结论：领域信息已从**提示词模板**基本清除（v0.1.1 的目标达成），但仍残留在**常量表、示例与默认值**中，
> 其中 3 处会**实际影响本研究（炔酰胺化学）的运行结果**。

## 一、总览

| 级别 | 数量 | 含义 |
|---|---:|---|
| 🔴 高 | 3 | 直接影响当前化学任务的检索/评分/机制识别结果 |
| 🟠 中 | 7 | 影响跨领域泛化；换领域时需改代码或提示词 |
| 🟡 低 | 6 | 示例、默认值、命名；不改也能跑，但会误导读者/使用者 |

---

## 二、🔴 高：实际影响本研究结果

| # | 位置 | 内容 | 为什么要紧 |
|---|---|---|---|
| H1 | `config.py:101-122` `journal_quartiles_seed` | **34 条期刊分区**：Nature/Science/Cell/Lancet/NEJM/Nature Methods/JMLR/PLOS/Bioinformatics/**European Journal of Cancer/British Journal of Cancer/Frontiers in Oncology/cancer letters/signal transduction and targeted therapy/genome biology/computers in biology and medicine** 等 | 质量节点的权威性因子 A 直接读这张表（`quality/scoring.py:44`）。表里**没有一条有机合成/催化期刊**（JACS、Angew、Org. Lett.、Chem. Sci.、ACS Catal. 全部缺），所以本研究命中的化学期刊一律拿不到分区加分，Q 值被系统性压低 |
| H2 | `quality/scoring.py:21` `QUARTILE_SCORE` + `:56` 会议启发式 | `Q1..Q4 → 0.95/0.80/0.65/0.50`；`source_type=="proceedings"` 或 venue 含 `conference` 时另行降权 | 与 H1 叠加：化学期刊既没有分区加分，还会被"非会议"规则反复走无分区分支；阈值 `threshold_direct=0.8` 是生物医药口径 |
| H3 | `study/consumer.py:48-110` `MECHANISM_KEYWORDS`(34) / `CONDITION_KEYWORDS`(22) / `_ACTIVATION_RULES`(8) / `_INTERMEDIATE_RULES`(11) / `_SELECTIVITY_RULES`(6) / `_BOND_CHANGE_RULES`(11) / `_OPERATOR_KEYWORDS` | 全部是**化学机制词汇**：`umpolung / dearomat / carbene / vinyl cation / keteniminium / Lewis acid activation / mol% / equiv / toluene / THF / ee / dr` 等 | 这是"机制—机会—算子"闭环的打分与抽取层：超边相关性加权、机制状态抽取、机会缺口、算子链候选**全靠这些表**。换成人文社科或材料领域，这些表全部失效，机制层会静默产出空结果 |

## 三、🟠 中：影响跨领域泛化

| # | 位置 | 内容 | 影响 |
|---|---|---|---|
| M1 | `domains.py:126-148` `DOMAIN_HINTS` | 领域判定靠**硬编码关键词**，且含具体课题词：化学组写了 `炔酰胺`、人文社科组写了 `人民战争/人民群众/群众路线/全民族抗战/党史/马克思主义` | 领域画像（维度、候选实体/关系类型）由此决定；新领域不在表内就落到 `general`。这是最"点名"的一处硬编码 |
| M2 | `domains.py:12-123` `DOMAIN_PROFILES` | 固定 5 个领域画像（chemistry / biomedicine / materials / general / humanities_social_science），维度与类型全写死；人文社科画像标签直接写"**马克思主义理论**" | 领域覆盖不可扩展；新增领域必须改代码 |
| M3 | `ontology/store.py` `SEED_NODE_TYPES`(13) | 种子实体类型含 `Disease / Drug / Gene / Material`，**缺** `Reaction / Substrate / Catalyst / Ligand / Solvent / Yield`（而 `domains.py` 的 chemistry 画像里恰好列了这些） | 建库时的类型注册表按生物医药口径预置；化学实体类型只能靠运行时动态注册，早期统计与域归并会偏 |
| M4 | `ontology/store.py` `SEED_RELATION_TYPES`(30) | 含 `promotes / inhibits / regulates / activates / releases / differentiates_into / complicates / risk_factor_for / treats`，**缺** chemistry 画像声明的 `catalyzed_by / affords / requires / occurs_under / gives_yield / tolerates` | 同上；且 `regulates` 在表中重复出现两次 |
| M5 | `ontology/store.py` `RELATION_SYNONYMS`(119) / `STRONG_RELATIONS`(10) | 同义词归一表与"强断言"集合按生物医药语义组织（`promote/enhance/induce/upregulate/inhibit/suppress`…）；`STRONG_RELATIONS` 里 `treats/targets/differentiates_into` 都是生物医药关系 | 化学关系（`affords`、`catalyzed_by`、`gives_yield`）没有同义词归一，会造成同一关系被写成多种形式、支持度被拆散 |
| M6 | `knowledge/extractor.py:43,105-106` | 提示词示例统一用 AI/生物医学：`"aliases": ["如 RAG、additive manufacturing"]`、`"Method: Retrieval-Augmented Generation"，论文中写 "RAG"` | 示例会锚定模型输出风格；化学任务下模型可能把 RAG 式缩写示例当模板 |
| M7 | `ontology/term_dictionaries.py` `CHEBI_TERMS`(8) / `GOLDBOOK_TERMS`(10) | 化学专用外部身份词典（水、过氧化氢、甲醇、乙醇、氨、氯化钠；annulation/catalysis/chemoselectivity/regioselectivity…） | 被 `quality/control.py` 的全局词典归并**实际使用**，但目前是**化学专用**；换到生物医学/材料领域，归并层会"有功能但无词条" |

## 四、🟡 低：示例、默认值与命名

| # | 位置 | 内容 | 影响 |
|---|---|---|---|
| L1 | `retrieval/node.py:111` | 相关性硬门的 docstring 举例"炔酰胺任务里混进脂质体/疟疾文献" | 仅注释；但会让人误以为该门是化学专用（实际是领域无关的） |
| L2 | `study/reaction_operators.py:1-20, 110` | 算子库整体是化学专用（20 个算子、docstring 用炔酰胺举例），却挂在与领域无关的 `study/` 下 | 命名与定位：它其实是**化学领域适配器**，不是通用能力 |
| L3 | `study/planner.py:218-224` | `GENERATIVE_HINTS/SUMMARY_HINTS/FRONTIER_HINTS/EVALUATION_HINTS` 为中英混合硬编码词（"提出/propose/新方法/综述/前沿/评估"） | 兜底任务类型判定；对中文以外的请求（如纯日文/韩文）会失效 |
| L4 | `study/content.py` / `study/reviewer.py` / `study/fact_check.py` 提示词 | 全中文撰写；`REVIEW_BLOCK` 固定了 9 个中文小节标题（摘要/引言/结构特征与反应性…） | 硬编码的是**语言与综述体裁**而非学科；若目标期刊要求英文或不同结构需改代码 |
| L5 | `demo.py:26` 默认主题 | 默认 demo 主题为骨修复支架 | 示例默认值 |
| L6 | `examples/dump_knowledge_pack.py:8-9` | 用法示例硬编码 `data/ynamide_multiazabicycle_v020.db` 与炔酰胺检索词 | 示例默认值（已在 `docs/VERIFICATION_v0.4.1.md` 中作为本研究工具引用） |

## 五、已确认"没有"硬编码的地方（v0.1.1 的成果）

| 位置 | 状态 |
|---|---|
| `retrieval/llm.py` `PLAN_PROMPT_TEMPLATE` | ✅ 明确写"不要套用任何固定的生物医药或材料模板"；只按 Planner 给的维度生成检索式 |
| `quality/llm.py` `QUALITY_PROMPT_TEMPLATE` | ✅ 无领域维度（仅测试断言检查过这点） |
| `knowledge/extractor.py` `SCHEMA_HINT` | ✅ 领域无关；实体类型列表为通用集合；条件/测量第 13 条同时覆盖化学与社科（temperature/solvent/catalyst 与 accuracy/p_value/n） |
| `study/planner.py` `PLANNER_PROMPT` | ✅ 领域画像由模型输出，不预设学科；示例用炔酰胺仅作"检索词写法"示范 |
| `study/consumer.py` `CONSUMER_PROMPT` | ✅ 提示词本身领域无关（算子词表由代码注入） |
| `study/acs_format.py` | ✅ 纯排版，无领域词 |
| `retrieval/{pubmed,arxiv,ncpssd,api_clients}.py` | ✅ 数据源适配器，只有各库语法（`[Title/Abstract]` 之类），非学科硬编码 |

## 六、建议的处理顺序

1. **H1/H2（最高优先，直接影响本研究打分）**：把 `journal_quartiles_seed` 从代码里挪到**外部可加载的文件**（如 `data/journal_quartiles.json` 或环境变量指向的路径），并补入有机合成/催化/材料类期刊；把 `QUARTILE_SCORE` 与阈值改为可配置。
2. **H3**：把机制/条件/算子词表抽成**领域适配器包**（如 `domain_adapters/chemistry/`），`consumer.py` 只按 `domain_profile.domain_kind` 加载，缺省为空表且**显式告警**（避免像现在这样静默失效）。
3. **M1/M2**：`DOMAIN_HINTS` 改为可配置的领域词典文件；`DOMAIN_PROFILES` 改为可加载的 profile 目录，代码里只保留 `general` 兜底。
4. **M3/M4/M5**：`SEED_*` 与 `RELATION_SYNONYMS` 拆成"通用核心 + 领域包"，化学包补 `catalyzed_by/affords/requires/occurs_under/gives_yield/tolerates`；顺手修掉 `regulates` 重复项。
5. **M6/L5/L6**：提示词示例换成领域中立表述；示例脚本默认值改为占位符并在文档里给化学示例。
6. **L2**：`reaction_operators.py` 迁到 `domain_adapters/chemistry/operators.py`，或至少在模块 docstring 里标注"化学领域适配器"。

> 备注：M7 的词典层已有"文件/API 加载"的扩展说明，但当前仍是内联常量；建议与 H1 一起走外部配置化。
