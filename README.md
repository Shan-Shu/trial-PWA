# research-agent

基于 **LangGraph** 的多模型协作科研辅助 Agent —— 项目环境骨架与最小可运行演示。

用多个大模型（如 OpenAI / DeepSeek / 通义千问 / Claude / Gemini）分工协作：
研究员A、研究员B 并行产出不同视角草稿，主持人模型综合成稿。
后续可在该骨架上挂接科研工具（arXiv / Tavily / PubMed / PDF 解析 / RAG 记忆）。

---

> ## 合并说明（本工作副本）
>
> 本副本在 **v0.4.4 引擎**之上，并入了 `paper_writing_assistant` 的**文献库工作流与引用导出**，
> 详见 [`docs/MERGE_NOTES.md`](docs/MERGE_NOTES.md)。要点：
>
> - 看板由 **6 tab 扩到 9 tab**：新增 **文献库**（标签/文件夹/收藏/筛选/批量/导出）、
>   **写作台**、**系统状态**；原有 tab 行为不变；
> - 新增 `library/`（侧车表 + 引用 + 批量作业）与 `writing/`（体裁/大纲/章节/润色）两层的后端；
> - 引用样式与期刊缩写落在 `packs/skills/journal-quartiles/content/citation_styles.json`，
>   写作体裁落在 `packs/skills/writing/`，**代码里不再出现学科词表或章节骨架**；
> - 迁移旧库：`uv run python scripts/migrate_from_pwa.py --src <pwa.db> --dry-run`（确认后加 `--apply`）；
> - 端到端冒烟：`uv run python scripts/smoke_dashboard.py`；全量测试：`uv run python -m unittest discover -s tests`。
>
> 状态：**260 单元测试 + 56 项端到端冒烟全绿**；未在真实浏览器中人工点检（见 MERGE_NOTES 已知限制）。

---

## 1. 环境总览

| 组件 | 版本 / 说明 |
|---|---|
| Python | 3.12.14（由 uv 管理，位于 `%APPDATA%\uv\python\`） |
| uv | 0.12.10（`%USERPROFILE%\.local\bin\uv.exe`，已加入用户 PATH） |
| 虚拟环境 | `.venv`（项目内，`uv` 自动创建） |
| LangGraph | 1.2.11 |
| LangChain | 1.4.0（core 1.6.2） |
| 多模型适配 | langchain-openai 1.6.0 / langchain-anthropic 1.7.1 / langchain-google-genai 4.4.0 / langchain-community 0.4.2 |
| 科研工具 | arxiv、tavily-python、wikipedia、pymupdf、pypdf、requests |

> 说明：本项目通过 **OpenAI 兼容接口**（`langchain-openai` 的 `ChatOpenAI`）即可接入
> DeepSeek、通义千问、Kimi、GLM 等国内模型，无需额外 SDK。

## 2. 镜像 / 下载源配置（已生效）

所有网络下载均走镜像源，配置位置如下：

- **PyPI 包镜像（清华 TUNA）**
  - 用户级 uv：`%APPDATA%\uv\uv.toml`（`[[index]]` 默认源指向 TUNA）
  - 普通 pip：`%APPDATA%\pip\pip.ini`
- **Python 运行时 / uv 本体**（GitHub Release 加速镜像）
  - `uv.toml` 中 `python-install-mirror = "https://ghfast.top/https://github.com/astral-sh/python-build-standalone/releases/download"`

如需临时切换官方源，可显式指定环境变量，例如：

```powershell
$env:UV_DEFAULT_INDEX = "https://pypi.org/simple"
```

## 3. 目录结构

```
D:\Desktop\trial
├── .env.example          # API Key / 角色配置模板（复制为 .env 后填写）
├── pyproject.toml        # 项目与依赖声明（uv 管理）
├── data\                 # 本地库：research_agent.db（论文/PDF BLOB/动态本体/日志）
├── src\research_agent\
│   ├── __init__.py       # CLI 入口 main()（多模型演示）
│   ├── models.py         # 多模型工厂：按 provider 构建 ChatModel
│   ├── demo.py           # LangGraph 多模型演示图（A/B 并行 -> 主持人综合）
│   ├── config.py         # 全局配置（评分权重/阈值/路径，可用环境变量覆盖）
│   ├── db.py             # SQLite：论文元数据 / PDF BLOB / 清洗文本 / 质量结果
│   ├── pipeline.py       # 三节点 LangGraph 流水线 + CLI（entry: research-agent-pipeline）
│   ├── retrieval\        # 文献检索节点
│   │   ├── api_clients.py    # 多源聚合（PubMed/arXiv/OpenAlex/Crossref）+ PDF 下载
│   │   ├── pubmed.py         # PubMed 源：NCBI E-utilities 检索 + Europe PMC 全文
│   │   ├── pdf_cleaner.py    # 行级页眉/页脚/页码剔除 → 精校重排文本
│   │   ├── node.py           # 检索节点（search / enrich 回补 / load 三模式）
│   │   └── monitor.py        # 实时监控数据库新文献的轮询接口
│   ├── quality\          # 质量控制节点（评估 + 领域词典归并）
│   │   ├── scoring.py        # A/T/Q 公式 + 元数据完整性判定
│   │   └── node.py           # 质量节点 + 人工审核节点
│   ├── knowledge\        # 知识提取节点
│   │   ├── preprocess.py     # 分句 / 分段 / 切块
│   │   ├── extractor.py      # LLM 结构化抽取 + 置信度融合
│   │   ├── node.py           # 知识节点（预处理 → 抽取 → 动态本体写入）
│   │   └── fake.py           # 离线静态 JSON 假模型（--model smoke）
│   ├── dashboard\        # 图形化看板（FastAPI + vis-network 本体图谱）
│   │   ├── app.py            # Web 服务（entry: research-agent-dashboard）
│   │   ├── api.py            # REST 数据接口（本体/文献/质量/日志）
│   │   └── static\           # 前端：本体图谱 / 智能体工作台 / 文献详情
│   └── ontology\
│       └── store.py          # 动态本体图存储（类型注册/节点边合并/溯源/版本）
├── tests\
│   ├── test_pipeline_offline.py  # 流水线离线测试
│   └── test_dashboard_api.py     # 看板 REST API 测试
└── examples\
    └── research_tools.py # 科研工具示例：arXiv 检索 / PDF 全文解析
```

## 4. 快速开始

### 4.1 激活环境

```powershell
# 方式一：uv 直接运行（自动使用 .venv）
uv run python --version

# 方式二：手动激活
.\.venv\Scripts\Activate.ps1
python --version
```

> Windows 控制台中文乱码提示：若终端输出中文为乱码，先执行一次
> `chcp 65001` 或 `$env:PYTHONIOENCODING = "utf-8"` 再运行脚本。

### 4.2 离线演示（无需任何 API Key）

验证 LangGraph 编排 + 并行数据流是否正常：

```powershell
uv run research-agent
# 或
uv run python -m research_agent.demo
```

### 4.3 接入真实多模型

1. 复制 `.env.example` 为 `.env` 并填入至少一组 Key：

```powershell
Copy-Item .env.example .env
# 编辑 .env：填 OPENAI_API_KEY / DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / ANTHROPIC_API_KEY / GOOGLE_API_KEY
```

2. 在 `.env` 中开启真实模式并指定各角色提供商：

```ini
RESEARCH_AGENT_REAL=1
WORKER_A_PROVIDER=openai     # 研究员A：GPT
WORKER_B_PROVIDER=deepseek   # 研究员B：DeepSeek
CHAIR_PROVIDER=qwen          # 主持人：通义千问
```

3. 运行：

```powershell
uv run research-agent
```

各角色也可复用同一提供商；提供商列表见 `SUPPORTED_PROVIDERS`。

## 5. 常用命令备忘

```powershell
uv add <package>                     # 安装依赖（走 TUNA 镜像）
uv add langchain-google-genai        # 示例：新增 Gemini 适配
uv remove <package>                  # 移除依赖
uv sync                              # 按 pyproject.toml 同步环境
uv python install 3.13               # 额外安装其他 Python 版本
uv pip list                          # 查看已装包
```

## 6. 科研工具示例

```powershell
uv run python examples\research_tools.py "graph neural network"
```

- `arxiv_search(query)`：arXiv 检索，无需 Key
- `extract_pdf_text(source)`：本地/URL PDF 全文解析（PyMuPDF）
- 联网搜索：申请 [Tavily](https://tavily.com) Key 填入 `.env` 后，可用
  `langchain_community.tools.tavily_search.tool.TavilySearchResults`

## 7. 下一步建议（按需扩展）

- **工具节点**：用 `@tool` 包装 arxiv/tavily/PDF 解析，经 `ToolNode` 挂到 researcher 节点
- **记忆 / 持久化**：`langgraph-checkpoint`（SQLite/Postgres）+ LangGraph 的 `MemorySaver`
- **动态并行**：用 `Send` API 按问题数动态分发 worker，而非固定两个角色
- **主管路由**：加入 supervisor 节点，用 LLM 决定下一步调用哪个模型/工具
- **RAG**：`chromadb` / `faiss` 对论文建索引，检索增强回答
- **评测**：接 LangSmith 做链路追踪与评测


---

## 8. 三节点科研文献流水线（基于动态本体）

`research-agent-pipeline`：**文献检索 → 质量控制 → 知识提取** 的内部数据构建状态机。
v0.4.0 起它不再作为完整研究任务的顶层入口，完整任务统一从 `research-agent-study` 的 Planner 开始。

```
START ──> retrieval ──> quality ──┬─(knowledge / flagged)──> knowledge ──> END
        (search|load|enrich)      ├─(enrich)────────────────> retrieval  ① 元数据回补
                                  └─(human)─────────────────> human_review ──> END
```

### 8.1 文献检索节点（retrieval）
- 检索 arXiv（可扩展 Crossref/OpenAlex），下载 PDF 以 **BLOB 存入本地库**（同时保留文件级 sha256）；
- 元数据补全：全部作者、作者单位（OpenAlex/Crossref）、发表情况（期刊/分区/年份）、DOI；
- PDF 行级清洗：剔除页眉/页脚/页码/© 等，产出**精校重排文本**（clean_text + paragraphs）；
- 预留**实时监控接口**：`retrieval/monitor.py` 的 `PaperMonitor` 按水位线轮询新文献，
  可对接定时任务/消息回调（`python -m research_agent.pipeline --monitor`）。

### 8.2 质量控制节点（quality）
- 权威性 `A = 0.5*期刊/出版社分级(JCR/SCI分区) + 0.3*作者H指数 + 0.2*被引次数`；
- 时效性 `T = 0.7*exp(-ln2*age/学科半衰期) + 0.3*(1-age/25)`（学科半衰期：fast 2 / medium 4 / slow 8 年）；
- `Q = 0.6*A + 0.4*T`（A、T、Q ∈ [0,1]）；
- 路由：Q ≥ 0.8 直接送知识提取；0.5 ≤ Q < 0.8 标记后送知识提取；Q < 0.5 人工审核；
- 全局控制：本体新增节点每满 300 个，按 IUPAC Gold Book / ChEBI 做一次领域词典归并；
- 元数据缺漏 → 发回检索节点回补（≤ 3 轮），仍无法补全 → 人工审核。

### 8.3 知识提取节点（knowledge）
- 文本预处理：分段 → 分句 → 按窗口切块；
- LLM 抽取 **实体/关系/属性/事件**，每条自带模型自评置信度；
- 最终置信度 = `0.6*模型自评 + 0.4*质量权重`（质量权重 = Q，标记文献按 0.85 折扣），
  实现“根据质量评估结果和文本逻辑给出置信度”；
- 写入**动态本体**：节点/边按规范化名去重合并（置信度取 max、别名/属性/来源证据增补），
  未注册的新实体/关系类型自动注册，schema 版本号递增；
- **科研超边**：实验、声明、过程和跨学科多角色关系写入 `ontology_hyperedges`，
  角色、条件、测量值、原文位置分别保存在附属表，不额外创建 Reaction/Event 节点。

### 8.4 使用

```powershell
# 真实模式（联网检索 + 下载 PDF + 质量控制 + LLM 知识提取）
uv run research-agent-pipeline --query "retrieval augmented generation" --max-results 5 --model openai

# 无 Key 冒烟（知识提取用静态 JSON 假模型，验证整条链路）
uv run research-agent-pipeline --query "graph neural network" --max-results 2 --model smoke

# 实时监控模式：轮询 data\research_agent.db 的新文献并自动处理
uv run research-agent-pipeline --monitor --poll-interval 60 --model openai
```

配置项（可选环境变量）：`RA_Q_WEIGHT_A/T`、`RA_THRESHOLD_DIRECT/FLAG`、`RA_MAX_META_ATTEMPTS`、
`RA_DB_PATH` 等，见 `config.py`。

### 8.5 本地库表
`papers`（元数据+PDF BLOB+精校文本）、`quality_results`、`ontology_type_registry`
（动态类型）、`ontology_nodes / ontology_edges`（去重合并+溯源）、`ontology_runs`
（版本/统计）、`processing_log`。

### 8.6 离线测试
```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 24 个用例：PDF 清洗 / A-T-Q 路由 / 元数据回补循环 / 动态本体合并 /
#           三节点端到端 / 三节点 LLM 驱动 / PubMed 接入 / 看板 REST API
```

## 9. 图形化看板（动态本体 + 智能体工作状态）

本地 Web 看板，直连 `data\research_agent.db` 实时查看：

- **动态本体图谱**：节点/关系、科研超边、节点域与关系通道可视化，支持按类型勾选、
  最低置信度过滤、名称搜索；点击节点查看 属性/别名/来源证据/相邻关系；
- **智能体工作状态**：文献检索 / 质量控制 / 知识提取 / 人工审核 四个工作台卡片，
  展示事件数、处理的文献数与最近事件；
- **输入输出**：文献列表（含 Q 值/决策/提取计数）→ 点击查看详情：
  元数据与作者单位（输入）、A/T/Q 构成、精校正文、处理日志（输出）；
- 顶部统计条 + 最近活动流，**自动刷新（6s）**，随流水线运行实时更新。

```powershell
# 启动看板（默认 http://127.0.0.1:8000）
uv run research-agent-dashboard

# 自定义端口 / 数据库
uv run research-agent-dashboard --port 9000 --db data\research_agent.db
```

打开浏览器访问 `http://127.0.0.1:8000` 即可。
前端依赖（vis-network）已本地化到 `dashboard\static\vendor\`，无需外网 CDN。

REST API（同一服务提供）：`/api/overview`、`/api/papers`、`/api/papers/{key}`、
`/api/agents`、`/api/logs`、`/api/ontology`、`/api/ontology/nodes/{id}`、`/api/health`。

---

## 9. 图谱可视化演示

把动态本体（SQLite 的 ontology_* 表）导出为**单文件交互式图谱**（内嵌本地 vis-network，
离线双击即用）及静态 SVG/PNG 缩略图。

```powershell
# 内置演示数据集（27 节点 / 24 关系，覆盖 4 篇论文）
uv run research-agent-viz --dataset demo

# 导出当前真实本体库（data\research_agent.db）
uv run research-agent-viz --dataset db

# 生成后用默认浏览器打开
uv run research-agent-viz --dataset demo --open
```

产物默认写入 `output\`：
- `ontology_graph_demo.html`：交互图谱 —— 拖拽/缩放、悬停看「属性/别名/来源证据/置信度」、
  顶部类型图例点击可显示/隐藏、搜索框按名称/别名高亮、可暂停布局/复位视角；
- `ontology_graph_demo.png / .svg`：静态预览（节点按类型着色、大小随置信度）。

`ontology/viz.py` 同时提供 API：`export_graph_from_db(db)`、`build_demo_graph()`、
`render_interactive_html(graph)`、`render_static_svg/png(graph)` 可复用到其它界面/报告。

## 10. 三节点 LLM 化（数据构建子流程）

数据构建子流程的三个节点由 LLM 驱动，可通过 `.env` 覆盖：

| 节点 | 默认模型 | provider / 接口 | 所需 API Key |
|---|---|---|---|
| 文献检索（retriever） | `deepseek-v4-flash` | deepseek | `DEEPSEEK_API_KEY` |
| 质量控制（quality） | `deepseek-v4-flash` | deepseek | `DEEPSEEK_API_KEY` |
| 知识提取（knowledge） | `deepseek-v4-flash` | deepseek | `DEEPSEEK_API_KEY` |

### 各节点如何“用 LLM 实现”
- **检索节点**：LLM 负责把主题拆解为互补检索式，并规整多源原始元数据；实际联网、下载和解析仍由检索适配器完成。
- **质量控制节点**：LLM 给出期刊分区、venue、h-index、被引量和领域速度判断，A/T/Q 公式与路由规则保持确定性。
- **知识节点**：LLM 把正文抽取为实体、关系、属性、事件和科研超边，再与文献质量权重融合后写入动态本体。

无 Key / 模型不可用时，流水线可按各节点的确定性实现继续运行；完整研究任务层不采用静默降级策略。

### 使用
```powershell
uv run research-agent-pipeline --query "graph neural network" --max-results 5
uv run research-agent-pipeline --query "RAG" --retriever-llm deepseek `
    --quality-llm deepseek --knowledge-llm deepseek
uv run research-agent-pipeline --query "knowledge graph construction" --llm-smoke
```

模型标识与平台说明（2026 现状）：DeepSeek V4 家族为 `deepseek-v4-pro/flash`。
如你的平台模型 id 不同，可在 `.env` 修改
`RETRIEVAL_MODEL / QUALITY_MODEL / KNOWLEDGE_MODEL`。

## 11. 多库检索与全文优先（默认 fulltext）

检索统一经过 `ApiHub`，默认 `--source fulltext`，按“可提供全文/PDF”的优先级
组合 Europe PMC、arXiv、Semantic Scholar、OpenAlex；需要时再回退 PubMed。

当前来源：

| 来源 | `--source` | 全文/PDF能力 |
|---|---|---|
| Europe PMC | `europepmc` | OA 全文 XML + OA PDF，优先 |
| arXiv | `arxiv` | PDF 全文 |
| Semantic Scholar | `semantic_scholar` | 检索 + Open Access PDF 定位 |
| OpenAlex | `openalex` | OA PDF 定位 + 元数据 |
| PubMed | `pubmed` | 元数据/摘要，PMCID 后回退 Europe PMC 全文 |
| NCPSSD（国家哲社文献中心） | `ncpssd` | 中文人文社科元数据/摘要，无 Key 开放检索 |
| Europe PMC+arXiv+Semantic+OpenAlex | `fulltext` | 默认全文优先组合 |
| 以上全部 | `all` | 覆盖最大，但非全文记录更多 |

```powershell
# 默认 fulltext：全文源优先
uv run research-agent-pipeline --query "bone regeneration AND scaffold" --max-results 20

# 只检索某一种源，或退回到 PubMed
uv run research-agent-pipeline --query "..." --source europepmc
uv run research-agent-pipeline --query "..." --source semantic_scholar
uv run research-agent-pipeline --query "..." --source pubmed
uv run research-agent-pipeline --query "..." --source ncpssd
uv run research-agent-pipeline --query "..." --source both
uv run research-agent-pipeline --query "..." --source all
```

任一来源无 PDF 时，若记录带 PMCID 会自动尝试 Europe PMC OA XML，仍失败则回退
摘要，并在 `papers.fulltext_source` 记录 `pdf / xml / abstract`。

备注：Semantic Scholar 免费接口无 Key 时可能 429，可设
`SEMANTIC_SCHOLAR_API_KEY` 提升额度；Unpaywall 补 OA PDF 时需设有效邮箱
`UNPAYWALL_EMAIL`；NCBI 仍限速 ≤3 req/s，设 `NCBI_API_KEY` 可提速。

NCPSSD 无独立 API Key，检索接口返回结构化元数据与摘要；历史记录里的
`www.nssd.org` PDF 下载域名已停用，因此该源目前以摘要入库并记录
`fulltext_source=abstract`。NCPSSD 期刊通常缺少 DOI/作者机构，质量节点对
该源放宽为“作者+期刊出版信息”完整即可进入知识提取。

## 12. Planner-first 研究任务层（规划 / 检索 / 消费 / 内容 / 审核 / 事实核查）

v0.4.0 统一了执行结构，v0.4.1 补齐了"机制 → 机会 → 算子 → 候选 → 核查"的闭环：

```text
用户输入
  → planner
  → collection（retrieval → quality → knowledge）
  → knowledge_consumer（DeepSeek V4 Pro）
  → content_builder（DeepSeek V4 Pro）
  → reviewer（DeepSeek V4 Pro）
  → fact_checker（DeepSeek V4 Pro）
  → END
```

关键约定：

- Planner 是唯一入口，先生成 `retrieval_plan`、`analysis_plan`、`evidence_policy`、`design_contract` 和停止条件；
- collection 只执行 Planner 的 `retrieval_plan`，不再由 Consumer 隐式触发检索；
- Knowledge Consumer 由 LLM 完成机制理解、冲突识别、机会发现、设计上下文和补检请求，代码只负责数据库查询、编号、引用校验和硬门槛；
- 超边按类型配额检索（event 型优先）并做机制相关性加权，机制超边不再被置信度截断挤掉；
- `design_context` 固定五键：`mechanism_states`（必须绑定真实 `evidence_id` / `hyperedge_id`）、`reaction_primitives`、`opportunity_gaps`、`operator_candidates`、`constraint_conflicts`；
- 候选方案以 **算子链**（`operator_chain`）为基本单位，算子只能取自 `study/reaction_operators.py` 的封闭词表，代码层校验"词表外算子 / 链是否衔接 / 推出等级"；
- 候选池先生成 30–50 个种子，再聚类去重、评分排序，选出 4–6 个最终方案并给出优先级与互斥关系；
- Reviewer 增加"设计契约检查"（并入指令符合度，不新增一级维度）：算子链合法性、创新等级下限、硬约束逐条回应、候选差异是否可解释；
- Reviewer / Fact Checker 的修订意见回流内容节点，逐条给出 `revision_responses`；事实核查含代码层硬结论，模型不能把高危问题判 pass；
- 每次运行的关键中间态写入数据库 `study_runs` 表，可审计"改了什么、为什么改"；
- Planner、Consumer、Content、Reviewer、FactChecker 默认均绑定 `deepseek-v4-pro`，统一使用 `DEEPSEEK_API_KEY`；
- 生产模式下模型不可用会明确失败，不静默回退为纯代码流程；事实核查缺少模型时降级为确定性核查。

目录：

```text
src/research_agent/study/
├── planner.py             # 统一任务契约与 retrieval_plan
├── collection.py          # 检索子流程调度
├── consumer.py            # LLM 机制消费、机会发现与设计上下文
├── reaction_operators.py  # 自研反应设计算子库（封闭词表 + 链校验）
├── content.py             # 内容形成、候选池、去重排序
├── reviewer.py            # 指令符合度 + 证据正确性 + 设计契约
├── fact_check.py          # 事实核查（无来源断言/伪造引用/数值缺证据）
└── graph.py               # Planner-first LangGraph 编排与 CLI
```

使用：

```powershell
# 生产模式：五个研究节点均使用 DeepSeek V4 Pro，并默认先检索
uv run research-agent-study --request "RAG 2024-2026 前沿综述" --db data\ontology_v05.db

# 只消费本地知识，不执行初始检索
uv run research-agent-study --request "RAG 2024-2026 前沿综述" --db data\ontology_v05.db --no-collect

# 离线确定性运行（仅用于测试，不调用 LLM）
uv run research-agent-study --request "骨修复支架前沿" --db data\ontology_v05.db --llm-smoke

# 知识包健康检查：机制超边保留率、提示词预算、可追溯率
uv run python examples\dump_knowledge_pack.py --db data\ynamide_multiazabicycle_v020.db --seed "ynamide annulation"

# v0.4.1 端到端基线（真实 LLM，落盘 plan/consumer/draft/review/fact_check）
uv run python examples\run_v040_baseline.py --db data\ynamide_multiazabicycle_v020.db --out-dir output\v040_baseline
```

Dashboard 默认也注入五个 DeepSeek V4 Pro 角色和完整 collection 子流程；
`create_app(..., inject_llms=False)` 仅用于离线测试。

语料卫生：`collect_mission` 会先过领域相关性硬门（`apply_topic_relevance_gate`），
把与主题词元零重叠的跨域命中挡在入库之前，避免污染机制抽取。检索报告里的
`relevance_gate` 字段会记录被丢弃的记录数与样例。
