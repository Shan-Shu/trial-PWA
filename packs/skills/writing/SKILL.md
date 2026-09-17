# writing —— 论文写作体裁与模板

## 何时用

用户在「写作台」新建/续写论文时，写作节点需要知道：

- 用什么**体裁**（研究论文 / 前沿综述 / 实验方案 / 基金申请书 / 审稿回复）；
- 该体裁的**章节骨架**（key + 中文标题 + 目标字数 + 写作要求）；
- 生成大纲、章节草稿、润色的**提示词模板**；
- 单个大纲节点收指令时，如何写成"只引用给出编号"的可回溯正文（见下文
  `section_compose_user`）。

## 数据契约

`content/data.json`：

```json
{
  "default_genre": "research_article",
  "genres": {
    "<genre-id>": {
      "label": "体裁中文名",
      "language": "zh",
      "sections": [{"key": "abstract", "heading": "摘要", "words": 300}],
      "section_notes": {"abstract": "该章节的写作要求"}
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

占位符（`{topic}` / `{heading}` / `{materials}` / `{materials_digest}` / `{note}` /
`{words}` / `{genre_label}`）由写作节点填充；缺失的键必须显式报错，不得静默回退到
代码内硬编码骨架。

### `section_compose_user`：单节点成段（大纲节点指令工作流）

写作台的每个大纲节点都能接收一句指令，走「规划 → 充分性判定 →（补检）→ 消费 → 成段」。
`section_compose_user` 是**该链路最后一步**的提示词；缺省时使用代码内兜底模板，但建议在
pack 里覆盖，以便按学科调整引用纪律。可用占位符：

| 占位符 | 含义 |
|---|---|
| `{topic}` | 论文主题（Planner 给出的 goal / domain） |
| `{heading}` | 本节标题 |
| `{note}` | 该体裁下这一节的写作要求（`section_notes`） |
| `{instruction}` | 用户在该节点输入的指令 |
| `{words}` | 目标字数 |
| `{materials}` | **带编号的素材清单**（`[1]` 起，编号即正文可用的引文下标） |
| `{basis}` | 充分性判定的依据（为什么判定"够写"） |

纪律要求（模板与代码共同保证，改模板时不要放宽）：

1. 正文里的引文必须写成 `[编号]`，编号只能来自 `{materials}`；
2. 清单外的编号会被后端的 `bind_citations()` 判为**越界引文**并在界面红标，
   不会静默过滤——所以模板应显式要求"不得引用清单外的编号"；
3. 不得把 `{basis}` 复述进正文（那是判定依据，不是论文内容）。

## 为什么放在 pack 而不是代码里

来源项目 `paper_writing_assistant` 的 `writing/writer.py` 把 7 个中文小节写死为
`DEFAULT_OUTLINE`，导致：换语言、换体裁、改章节顺序都必须改代码。本包只承载数据，
代码里不再出现任何章节标题。

## 扩展

1. 在 `genres` 下加一个体裁（`sections` 至少含 `key`/`heading`）；
2. 需要新语言的提示词就覆盖 `prompts` 里的模板（或另建 `<lang>` 变体）；
3. 想让某个学科的成段正文更严格（例如强制标注方法学条件编号），覆盖
   `section_compose_user` 即可，不必改代码；
4. 不需要改任何 Python 代码。
