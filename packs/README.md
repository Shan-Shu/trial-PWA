# packs —— 可加载的领域包与技能包

本目录承载**所有专业领域内容**：代码里不再内联任何学科词表。
加载器为 `src/research_agent/packs.py`。

## 目录结构

```text
packs/
├── skills/<skill-id>/
│   ├── SKILL.md                # 何时用、输入/输出契约、规则（给人/Agent 读）
│   └── content/
│       ├── data.json           # 主数据
│       └── terms.jsonl         # 词表（每行一条 JSON）
└── domains/<domain-kind>/
    ├── domain.json             # 领域画像 + 关键词 + 领域追加的种子类型
    └── vocab/
        ├── terms.jsonl         # 外部身份词表（如 ChEBI / Gold Book）
        └── mechanism.jsonl     # 机制词表增补（按 section 追加）
```

## 查找与覆盖

| 环境变量 | 作用 |
|---|---|
| `RA_PACKS_DIR` | 指定包根目录（`;` 分隔多个）。**一旦设置即为权威**，不再读取随代码发布的 `packs/` |
| `RA_PACKS_FALLBACK` | 配合 `RA_PACKS_DIR` 使用的兜底目录 |
| `RA_JOURNAL_QUARTILES` | 指向一个 JSON 分区表，覆盖 `journal-quartiles` 技能包内的条目 |

缺失时的行为：**显式告警 + 返回空/缺省值**，绝不用隐藏的内联表兜底——
避免出现"看起来在跑、其实机制层是空的"这种静默失效。

## 内置技能包

| 技能 | 承载内容 | 代码入口 |
|---|---|---|
| [journal-quartiles](skills/journal-quartiles/SKILL.md) | 期刊分区与分区得分 | `quality/scoring.py::venue_factor` |
| [relation-lexicon](skills/relation-lexicon/SKILL.md) | 种子类型、关系同义归一、强断言集合 | `ontology/store.py` |
| [mechanism-keywords](skills/mechanism-keywords/SKILL.md) | 机制/条件词表、触发式抽取规则、算子关键词 | `study/consumer.py`、`study/mechanism_lexicon.py` |
| [task-kind-hints](skills/task-kind-hints/SKILL.md) | 任务类型/内容类型判定关键词 | `study/planner.py` |

## 内置领域包

| 领域 | 内容 |
|---|---|
| `chemistry` | 化学画像、化学关键词、化学追加种子类型（Reaction/Catalyst/…）、ChEBI 与 Gold Book 词表、化学机制规则（`vocab/mechanism.jsonl`） |
| `biomedicine` | 生物医学画像与关键词 |
| `materials` | 材料/工程画像与关键词 |
| `humanities_social_science` | 人文社科画像与关键词 |
| `general` | 兜底领域（`"fallback": true`）：未命中任何领域关键词时使用 |

## 新增一个领域

1. 建 `packs/domains/<kind>/domain.json`：

```json
{
  "kind": "quantum",
  "keywords": ["qubit", "量子比特", "纠缠"],
  "profile": {
    "label": "量子信息",
    "dimensions": ["相干性", "门保真度", "纠错"],
    "candidate_entity_types": ["Qubit", "Gate", "NoiseModel"],
    "candidate_relation_types": ["couples_to", "decoheres_via"]
  },
  "extra_seed_node_types": [["Qubit", "量子比特"]],
  "extra_seed_relation_types": [["couples_to", "与…耦合"]]
}
```

2. 需要外部身份词表就加 `vocab/terms.jsonl`；
3. 需要机制级抽取就加 `vocab/mechanism.jsonl`（`section` 取值见
   `study/mechanism_lexicon.py` 的 `_LIST_SECTIONS` / `_RULE_SECTIONS`）；
4. 不需要改任何 Python 代码。

## 校验

```powershell
uv run python -m unittest tests.test_packs -v
```

该测试覆盖：内置包存在性、领域判定、画像、种子类型合并、关系归一、
机制词表增补、期刊分区与覆盖、缺包时的显式降级。
