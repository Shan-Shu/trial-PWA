# writing —— 论文写作体裁与模板

## 何时用

用户在「写作台」新建/续写论文时，写作节点需要知道：

- 用什么**体裁**（研究论文 / 前沿综述 / 实验方案 / 基金申请书 / 审稿回复）；
- 该体裁的**部分骨架**（key + 中文标题 + 目标字数 + 所用模板）；
- 每个部分的**模板**：写什么、要什么证据、考哪几个判定维度、有哪些可填字段
  ——用户只给主题时，系统靠它自己排出工作规划；
- 生成大纲、部分草稿、成段、润色的**提示词模板**。

## 数据契约

`content/data.json`：

```json
{
  "default_genre": "research_article",
  "dimensions": ["papers", "knowledge", "conditions", "comparison",
                 "requirements", "evidence"],
  "evidence_types": ["citation", "mechanism", "quantitative", "comparative",
                     "protocol", "risk"],
  "genres": {
    "<genre-id>": {
      "label": "体裁中文名",
      "language": "zh",
      "sections": [{"key": "abstract", "heading": "摘要", "words": 300,
                    "template": "review_scope"}],
      "section_notes": {"abstract": "该章节的写作要求"},
      "dimension_weights": {"papers": 0.30, "conditions": 0.20},
      "templates": {
        "review_scope": {
          "role": "该部分在论文里的职责",
          "focus": ["要点1", "要点2"],
          "required_dimensions": ["papers", "comparison"],
          "evidence_types": ["citation", "protocol"],
          "min_support": 3,
          "allow_gaps": true,
          "fields": [
            {"key": "year_from", "label": "时间窗起点", "type": "number",
             "preset": 2019, "placeholder": ""}
          ]
        }
      }
    }
  },
  "prompts": {
    "outline_system": "...", "outline_user": "...",
    "section_system": "...", "section_user": "...",
    "section_compose_user": "...",
    "polish_system": "...", "polish_user": "..."
  },
  "citation_style": "gb7714"
}
```

占位符（`{topic}` / `{heading}` / `{role}` / `{materials}` / `{materials_digest}` /
`{note}` / `{instruction}` / `{gap_hint}` / `{basis}` / `{words}` / `{genre_label}`）
由写作节点填充；**模板里出现引擎不认识的占位符会被替换成空串而不是报错**，
因此你可以自由加自己的标记。缺失的键必须显式报错，不得静默回退到代码内硬编码骨架。

### 部分模板（`templates`）：这是"一个规划节点 + N 套模板"的落地

用户只给主题，系统要能自己排出"每个部分写什么、要什么证据"。这件事由模板承载：

| 字段 | 必填 | 作用 |
|---|---|---|
| `role` | ✅ | 该部分的职责，渲染进提示词 |
| `focus` | ✅ | 写作要点；模型不可用时它就是系统自拟的要点 |
| `required_dimensions` | ✅ | **决定这一部分考哪几个维度**。未被列出的维度权重为 0、不设闸门——所以"实验的参数节"考量化条件，而"研究目标节"不考 |
| `evidence_types` | ✅ | 应依据的证据类型；缺口标注与补检建议用它 |
| `min_support` | | 证据保全下限（默认 1） |
| `allow_gaps` | | 证据不足时是否允许带缺口成文（默认 true；成文时正文顶部会插入显式标注） |
| `fields` | | 暴露给用户的输入项 `{key,label,type,preset,placeholder}`；`type` 取 `text`/`textarea`/`number` |

`required_dimensions` 的可用值见顶层的 `dimensions`；`evidence_types` 见
`evidence_types`。未在 `sections[].template` 里挂模板的部分（如"参考文献"）
不生成正文。

### 双模并行（纯并行）

每个字段各自决定来源，**用户填过的字段规划不再覆盖**：

| 情形 | 来源标记 |
|---|---|
| 用户在该字段填了内容 | `user` |
| 规划节点给出了该部分的要点/字段 | `plan` |
| 都没有 → 用模板 `preset` | `template` |

界面对每个字段标出来源，提示词里也会写明"标为「用户指定」的条目优先于任何默认"。

### `section_compose_user`：单部分成段提示词

模板渲染出的**结构化字段块**通过 `{instruction}` 传给模型（替代原来的单条自由指令）。
可用占位符：

| 占位符 | 含义 |
|---|---|
| `{topic}` | 论文主题（Planner 给出的 goal / domain） |
| `{heading}` | 本部分标题 |
| `{role}` | 该部分的职责（来自模板） |
| `{note}` | 该体裁下这一部分的写作要求（`section_notes`） |
| `{instruction}` | **结构化字段块**：每项标注了来源（用户指定/规划拟定/模板默认） |
| `{gap_hint}` | 证据缺口说明；无缺口时是"无（证据齐备）" |
| `{words}` | 目标字数 |
| `{materials}` | **带编号的素材清单**（`[1]` 起，编号即正文可用的引文下标） |
| `{basis}` | 充分性判定的依据（为什么判定"够写"） |

纪律要求（模板与代码共同保证，改模板时不要放宽）：

1. 正文里的引文必须写成 `[编号]`，编号只能来自 `{materials}`；
2. 清单外的编号会被后端的 `bind_citations()` 判为**越界引文**并在界面红标，
   不会静默过滤——所以模板应显式要求"不得引用清单外的编号"；
3. 不得把 `{basis}` 复述进正文（那是判定依据，不是论文内容）；
4. 证据有缺口时，推断必须写成「（推断）」——正文顶部还会由引擎再插一段缺口标注。

## 为什么放在 pack 而不是代码里

来源项目 `paper_writing_assistant` 的 `writing/writer.py` 把 7 个中文小节写死为
`DEFAULT_OUTLINE`，导致：换语言、换体裁、改章节顺序都必须改代码。本包只承载数据，
代码里不再出现任何章节标题。

## 扩展

1. 在 `genres` 下加一个体裁（`sections` 至少含 `key`/`heading`）；
2. 给 `sections[].template` 指向 `templates` 里的键；
   **不挂模板的部分不生成正文**（如参考文献）；
3. 想让某个学科的部分判定更严（例如实验设计必须给量化条件），
   在对应模板的 `required_dimensions` 里加上 `conditions` 即可，不必改代码；
4. 想调整"薄弱证据也允许成文"的尺度，改模板的 `allow_gaps`；
5. 需要新语言的提示词就覆盖 `prompts` 里的模板（或另建 `<lang>` 变体）；
6. 当前已配模板的体裁：`frontier_review`（文献综述，5 个模板）与
   `experiment_protocol`（实验设计，7 个模板）。其余体裁落回旧行为
   （无模板 → 全部既有闸门 + 不允许带缺口写作），可按同一格式补齐。
