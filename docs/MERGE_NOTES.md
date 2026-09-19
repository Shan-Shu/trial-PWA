# 合并说明：PWA 界面形态 × research-agent 引擎

> 合并基线：`Shan-Shu/trial` / research-agent **v0.4.4**（169 测试全绿）
> 并入来源：`JoeTrump22/-paper_writing_assistant`（PWA）
> 方案依据：`merge_plan_v2.md`
> 结果：**328 单元测试 + 47 项前端模块加载 + 119 项端到端 HTTP + 56 处接口配对全部通过**

---

## 〇、形态与能力的分工（重要）

合并原则是 **形态取自 PWA，能力取自 trial**：

| 维度 | 来源 | 落地 |
|---|---|---|
| **信息架构**（10 个任务导向页面 + 左侧导航） | PWA | `static/js/pages/registry.js` + `shell.js` |
| **视觉系统**（深蓝渐变 + 网格纹理 + 卡片 + 徽章 + 进度条） | PWA（`ui/app.py::_inject_css` 的令牌原值） | `static/css/theme.css` + `shell.css` + `js/ui/design.js` |
| **交互模式**（筛选矩阵 / 详情抽屉四子页 / metric 栅格） | PWA | 各页面模块 |
| **引擎与内容**（LangGraph 六节点、19 表本体、超边、算子链、事实核查、packs） | trial | 既有后端 + 新增/重写的前端页面 |

**没有做的事**：没有让 PWA 的弱集合（标签/文件夹/收藏约 630 行）去替换 trial 的强引擎；
也没有把 trial 的 6-tab 单页结构照搬。前端是**为 trial 的能力重新设计的 10 页工作台**。

### 9 个页面

| 页 | 承载的 trial 能力 |
|---|---|
| 总览 | 库规模、链路漏斗、覆盖率、分区表健康度、低信号/年份未知计数 |
| **规划与检索** | **Planner 任务契约**（retrieval_plan / design_contract / stop_conditions / must_cover）+ 执行后的**知识包**（超边角色/条件/测量/证据）。两件事合成一页，因为它们是同一条链的两端 |
| 文献库 | 侧车表工作流 + 服务端筛选 + 批量作业 + 6 种引用导出 |
| 知识抽取 | 批量抽取（后台作业可取消）+ 单篇核查 + `extract_failed` 可见 |
| 动态本体 | vis-network 图谱 + 类型/置信度/域筛选 + 节点详情（别名/属性/证据/邻居） |
| 研究流程 | 六节点流水线 + `study_runs` 中间产物（design_context / 算子链候选 / 审核 / 事实核查） |
| 写作台 | 体裁（来自 pack）/ 大纲 / 章节 / 润色，模型不可用时显式标注降级**并给出原因** |
| 审核中心 | 待审队列 + 预设动作 + 审核历史 |
| 系统状态 | packs 加载、分区表来源与未命中期刊、生效 `RA_*`、低信号策略 |

### 与 Planner 的交互（入口在哪）

「规划与检索」页左侧：

- **发送给规划节点** → `POST /api/planner/run`，只生成任务契约（快、不检索），右侧渲染完整契约；
- **后台运行完整研究流程** → `POST /api/study/run`，六节点链后台跑，状态在「研究流程」页。

**修复的上游缺陷**：原 `POST /api/planner/run` 里 `build_role_model("planner")` 在缺 Key 时直接抛异常，
被 `except` 吞成 `{"ok": false}` —— 而 planner 节点本身**支持 `model=None` 走确定性任务单**
（`planner.py` 的 `offline_fallback` 分支）。结果是：无 Key 时这个入口永远失败，
用户既拿不到契约、也看不出是缺 Key。现改为：模型构建失败 → 降级为确定性任务单 → 返回
`planner_mode="offline_fallback"` 与 `planner_model_error`，界面照实展示。

### 写作台的"骨架降级"是什么

无模型时**不假装生成**，而是把该体裁判定的章节标题、目标字数、以及**带编号的真实素材清单**
拼成草稿，并在正文里写明降级原因。原因经 `model_unavailable_reason` 透传
（例如"缺少 DEEPSEEK_API_KEY"），可与"模型构建失败"区分。素材来自库中质量最高的前 8 篇，
编号写入 `writing_sections.citation_ids`，供后续按编号引用。

### 大纲节点 = 工作规划节点（本节工作流）

写作台的结构是 **一个规划节点 + N 套固定模板**：

```
用户输入：主题（必需） + 各部分的模板表单（全部可选）
        ↓
工作规划（唯一的规划节点）  mode=auto 系统自拟 ／ mode=user 遵照用户
        ↓ 工作规划：每部分的职责 / 要点 / 证据类型 / 必考维度
   模板(范围)  模板(版图)  模板(参数)  模板(设计) …      ← 不是节点，是模板
        ↓
   充分性判定（按该模板的 required_dimensions）
        ↓
   充足 → 成段 ／ 不足 → 补检 ↺ ／ 预算用尽 → 带缺口成文（显式标注）
```

**部分不是节点，而是数据里的模板**（`packs/skills/writing/content/data.json`
的 `genres.<genre>.templates`）。模板承载"这一部分要什么"：

| 模板字段 | 作用 |
|---|---|
| `role` / `focus` | 该部分职责与写作要点（渲染进提示词） |
| `required_dimensions` | **决定这一部分考哪几个维度**——未被列出的维度权重为 0、不设闸门 |
| `evidence_types` | 应依据的证据类型（用于缺口标注与补检建议） |
| `min_support` | 证据保全下限 |
| `allow_gaps` | 证据不足时是否允许"带缺口成文（必须标注）" |
| `fields` | 该部分暴露的可填字段与预设值 |

**双模并行（纯并行，字段级）**：用户填了 → `user`；规划节点给了 → `plan`；
否则模板预设 → `template`。**用户填过的字段，规划不再覆盖**。

**全自动模式**：只给主题、不给任何指令与字段时，规划节点依主题自拟任务单，
本模块把 `analysis_plan` / `analysis_targets` 按"部分职责"映射下发——
逐部分调模型意味着 7 个部分 7 次调用，成本与可解释性都不可接受，因此
LLM 只出一份任务单，映射是确定性的。

**带缺口写作**（用户明确要求）：模板 `allow_gaps=True` 时，判定不足仍照常成文，
但**正文顶部插入显式标注**（列出缺哪类证据、哪些内容是推断），轨迹里
`unmet_dimensions` 与 `status="written_with_gaps"` 都保留。最低门槛是
"至少有一点可引用的东西"——完全空白时仍返回 `needs_data`。
未挂模板的部分（如尚未配模板的 `research_article`）**不允许**带缺口写作，
退回旧约定。

**两套模板已落地**：

| 体裁 | 部分数 | 判定侧重 |
|---|---|---|
| `frontier_review`（文献综述） | 5 个模板 + 摘要/参考文献 | 文献量、可比研究、约束可复现；**不要求**量化条件 |
| `experiment_protocol`（实验设计） | 7 个模板 + 参考文献 | 量化条件、分组对照、可复现流程；**不要求**大量文献 |

实测（同一个库、同一时刻）：`objective` / `background` 判 `sufficient`，
`parameters` / `readouts` / `design` 因缺量化条件、`groups` / `risks` 因缺可比证据
判 `insufficient`——各部分因模板不同而结论不同。

**为什么把判定放在检索之前**：既有 `study/graph.py` 是"先检索一轮、消费时才发现缺口"，
在写作台上意味着用户点一次就白烧一轮配额。先判后检，不足才花钱。

**五维加权 + 硬闸门**（避免加权平均被饱和维度稀释）：

| 维度 | 默认权重 | 说明 |
|---|---|---|
| 命中文献 | 0.30 | 绝对比例，避免"刚达标=满分" |
| 知识覆盖 | 0.20 | 只算命中文献里真的抽取过的 |
| 量化条件 | 0.20 | 带条件/测量的超边占比 |
| 可比证据 | 0.15 | 同类型超边/关系 ≥2 条才算"可两两比较" |
| 指令要求 | 0.15 | 只取指令与规划要点，标题不算要求 |
| 可用证据 | 0.10 | 供引文绑定 |

挂了模板的部分只考 `required_dimensions` 里的维度；未挂模板的部分沿用全部闸门，
避免"换个体裁就悄悄放宽判定"。任一必考维度未达标即把置信度封顶到阈值以下。

---

## 〇之二、节级工作流的接口

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/writing/projects/{id}/plan` | **工作规划**：不传 `instruction` → 系统自拟；传了 → 按你的指令。落 `writing_projects.plan_json` |
| `GET` | `/api/writing/templates?genre=…` | 列出该体裁的部分模板（结构 + 可填字段 + 预设值） |
| `POST` | `/api/writing/projects/{id}/sections/{key}/plan` | **预判本节够不够写**：只规划 + 判定，不检索、不写正文 |
| `POST` | `/api/writing/projects/{id}/sections/{key}/compose` | **撰写本部分**：启动异步作业；`instruction` 与 `fields` 都可留空 |
| `GET` | `/api/writing/section-jobs/{job_id}` | 作业进度与结果（`outcome` 区分 `written` / `written_with_gaps` / `needs_data` / `failed`） |
| `POST` | `/api/writing/section-jobs/{job_id}/cancel` | 取消作业 |
| `GET` | `/api/writing/projects/{id}/sections/{key}/trace` | 决策轨迹（逐轮判定 + 模板 + 字段来源 + 引文溯源） |
| `GET` | `/api/writing/projects/{id}/sections/{key}/content` | 仅取该部分正文（撰写完成后局部刷新） |
| `GET` | `/api/writing/projects/{id}/section-states` | 该项目所有部分状态（列表徽标） |

部分状态：`draft` / `planning` / `insufficient` / `needs_data` / `written` /
**`written_with_gaps`** / `stale` / `failed`。

请求体示例（两个字段都可省略）：

```json
{
  "instruction": "可选：额外指令",
  "fields": { "params": "无水无氧，-20 °C", "replicates": 5 }
}
```

**引文必须可回溯**：正文里的 `[n]` 由 `bind_citations()` 绑定到真实
`paper_key` 与 `hyperedge_id`，写入 `writing_sections.grounded_on`；模型引用了素材清单外的
编号时，`invalid_indices` 非空，界面红标提示，绝不静默过滤。


---

## 一、并入的 PWA 能力

| 能力 | 来源 | 落地位置 |
|---|---|---|
| 标签 / 文件夹 / 收藏 | `library/store.py` | `db.py` 四张侧车表 + `library/store.py` |
| 引用格式化（GB/T 7714 / APA / ACS / BibTeX / RIS / Markdown） | `library/citation.py` | `library/citation.py` + `packs/skills/journal-quartiles/content/citation_styles.json` |
| 文献库 UI（筛选 / 批量 / 导出） | `ui/views/library.py`（409 行） | `static/js/pages/library.js` + `dashboard/library_api.py` |
| 写作台（项目 / 大纲 / 章节 / 润色） | `ui/views/writing.py` + `writing/` | `writing/service.py` + `dashboard/writing_api.py` + `packs/skills/writing/` |
| 六维质量展示 | `quality/scoring.py` 的展示结构 | 文献详情抽屉（**展示层，未替换 A/T/Q 决策口径**） |

### 未并入（PWA 后端整体退役）

`pipeline.py` / `coordinator.py` / `agents.py` / `task_manager.py` / `ui/task_ui.py` /
`retrieval/` / `knowledge/` / `ontology/` / `experiment/` / `review/` 与整套 Streamlit UI。
PWA 的 `TaskManager` 刻意不并入（它有 `st.fragment` 版本依赖、`list_tasks()` 非可重入锁自锁、
固定 task_id 多会话串号三个已知缺陷）；本仓库改为每作业独立 `job_id` 的 `library/jobs.py`。

---

## 二、相对来源项目的实质改进

### 侧车层

1. **补外键与级联**：PWA 的侧车表无 FK，绕过主流程即留孤儿行；本仓库四张表全部
   `REFERENCES papers(paper_key) ON DELETE CASCADE`，并补标签/文件夹映射索引。
2. **主键对齐**：PWA 用 `paper_id INTEGER`，本仓库统一 `paper_key TEXT`。
3. **筛选下推 SQL**：PWA 的 `library_query` 先取 5000 行再在 Python 里过滤切片；
   本仓库在 SQL 里完成筛选/排序/分页，**大字段永不进入列表结果集**。
4. **修掉"年份未知文献永久不可见"**：PWA 年份筛选恒传 1900/2100，`year IS NULL`
   在 SQL 比较里恒不成立；本仓库提供显式 `year_mode=unknown` 档位。
5. **新增低信号档位**：v0.4.3 起相关性门控默认 `warn`（放行+标注），需要 `low_signal=yes/no` 筛出。

### 引用层

6. **期刊身份统一**：缩写 / BibTeX 写法 / 规范名 / 分区全部以
   `packs.normalize_journal_key()` 为键。
7. **BibTeX 条目类型按 `source_type`**（PWA 恒 `@article`），补齐
   `volume` / `number`(来自 `papers.issue`) / `pages` / `doi` / `url`。
8. **cite key ASCII 化 + 去重**（PWA 对中文作者会产出中文 key 且可能碰撞）。
9. **未知样式显式报错**（PWA 静默回落 GB/T 7714）；**新增 RIS 输出**；
   ACS 缩写目录（26 条常见化学/催化期刊）落在 pack 里。

### 写作层

10. **章节骨架与提示词外置到 `packs/skills/writing/`**（PWA 把 7 个中文小节写死为
    `DEFAULT_OUTLINE`）；本仓库新增 5 个体裁。
11. **模型不可用时显式降级**：返回 `generated_by="skeleton_fallback"` 与原因，界面如实展示。
12. **素材带编号并强制引用**：提示词要求引用写成 `[编号]` 且编号只能来自素材清单。
13. **大纲节点可交互并可审计**（PWA 只能整章生成，说不出"为什么这段能写"）：
    节点指令 → 规划 → **检索前充分性判定** → 补检 ↺ / 成段，逐轮落
    `section_runs`，正文落 `writing_sections.grounded_on`，引文 `[n]` 绑定到真实
    `paper_key` / `hyperedge_id`（越界编号红标，不静默过滤）。

### 前端工程

13. **ES module 拆分 + 设计系统**：`common/`（dom/api/state/ui）+ `ui/design.js`
    （card/metrics/badge/progress/drawer/subtabs）+ `pages/*`（10 页 + shell + registry）。
14. **统一转义策略并带自检**：`h()` 为准；测试会在 HTML 上下文里扫描未转义插值，
    且带"把已知漏洞喂给规则必须判为不安全"的自检用例。
15. **三层前端验证**（原项目只有语法级的零验证）：
    ① `node --check` 语法；② `scripts/frontend_smoke.mjs` 在 Node 里**真实 import**
    所有模块并调用 `mount()`、校验页面契约与注册表一致性；③ 端到端 HTTP 校验资源可达。

### 可观测性

16. **新增 `/api/system/packs` 与「系统状态」页**：把三件原本只出现在日志里的事做成界面可见——
    包加载状态与告警、**JCR 全量表是否真的被加载**（未加载会静默退回 409 条子集）、
    **未命中分区的期刊 Top-N**（回灌 packs 的入口）。

---

## 三、目录变更一览

```
新增
  packs/skills/journal-quartiles/content/citation_styles.json   引用样式 + 期刊写法覆盖
  packs/skills/writing/{SKILL.md, content/data.json}            写作体裁、提示词、成段提示词
  src/research_agent/library/{__init__,store,citation,jobs}.py   文献库层
  src/research_agent/writing/{__init__,service}.py               写作服务（项目/大纲/章节）
  src/research_agent/writing/sufficiency.py                      检索前的段级充分性判定
  src/research_agent/writing/section_graph.py                    单节点写作链（LangGraph）
  src/research_agent/writing/section_compose.py                  单段成段 + 引文绑定
  src/research_agent/writing/section_service.py                  作业编排 + 决策轨迹落库
  src/research_agent/dashboard/library_api.py                    文献库/系统状态数据层
  src/research_agent/dashboard/writing_api.py                    写作数据层
  src/research_agent/dashboard/section_api.py                    节点指令工作流数据层
  static/css/{theme,shell}.css                                   PWA 视觉规范
  static/js/{app.js, common/*, ui/design.js, pages/*}            ES module 前端（9 页）
  scripts/{migrate_from_pwa,smoke_dashboard,seed_dashboard_db}.py
  scripts/verify_section_flow.py                                 在真实服务上核对节点工作流
  scripts/verify_dispatch_live.py                                在真实服务上核对派工链路
  scripts/verify_real_model.py                                   真实模型端到端（需 Key，不进单测）
  scripts/frontend_smoke.mjs                                     前端模块加载冒烟
  scripts/check_frontend_routes.py                               前端接口 ↔ 后端路由配对
  scripts/browser_smoke.mjs                                      真实浏览器交互冒烟（Playwright + Edge）
  tests/{test_library,test_writing,test_section_flow,test_frontend_assets,test_migration}.py
v0.4.5 新增
  src/research_agent/logging/{__init__,proxy}.py                  统一事件日志（词表/脱敏/trace/tail）
  src/research_agent/writing/node_registry.py                    节点能力登记表（13 节点 12 任务）
  src/research_agent/writing/dispatch.py                         派工协议（幂等/取消/收敛/逐步回报）
  src/research_agent/writing/dispatch_planner.py                 自然语言 → 可执行派工方案
  docs/{LOGGING.md,CHANGELOG_v0.4.4_v0.4.5.md}
  tests/{test_logging,test_dispatch,test_dispatch_planner}.py
改动
  src/research_agent/db.py                 +4 张侧车表 +3 张写作表 +dispatch_runs（含 FK/CASCADE/索引）
  src/research_agent/config.py             +节级工作流阈值（RA_SECTION_*）
  src/research_agent/packs.py              +skill_content()（按名读取 content/<name>.json）
  src/research_agent/dashboard/app.py      路由（文献库/写作/节点工作流/系统状态 + 登记表/派工）
  src/research_agent/dashboard/api.py      nodes_overview()：登记表驱动 + 明确状态语义
  src/research_agent/models.py             build_role_model() 套 LoggedModel 记账代理
  static/js/pages/experiment.js            研究流程页重写（节点总览 + 当前派工）
  static/js/pages/writing.js               前端诊断事件 + 点击未命中留证据
删除（旧前端）
  static/app.js（937 行）、static/style.css、static/js/main.js、static/js/tabs/*
```

---

## 四、怎么用

```powershell
# 1. 生成演示库（可选，幂等）
uv run python scripts/seed_dashboard_db.py

# 2. 启动看板
uv run research-agent-dashboard --port 8000 --db data\dashboard_demo.db

# 3. 从 PWA 迁移旧库（先 dry-run 看报告，确认后 --apply）
uv run python scripts/migrate_from_pwa.py --src "D:\pwa\data\paper_assistant.db" --dry-run

# 4. 三层验证
uv run python -m unittest discover -s tests          # 328 单元测试
node scripts/frontend_smoke.mjs src/research_agent/dashboard/static   # 47 项前端加载
uv run python scripts/smoke_dashboard.py             # 119 项端到端 HTTP

# 4b. 前端接口与后端路由是否配得上（错配只会在点击时 404）
uv run python scripts/check_frontend_routes.py

# 4c. 浏览器交互冒烟（headless，需要 Node 侧依赖；见下）
npm install            # 只装 playwright-core，不下载浏览器
node scripts/browser_smoke.mjs            # 自起服务 + 临时库
node scripts/browser_smoke.mjs --base http://127.0.0.1:8000   # 复用已有服务
```

### 浏览器交互冒烟（headless）

前三层都验不到「点下去会不会炸」——DOM 事件绑定、选择器拼写、渲染顺序、作用域
这些只有真浏览器能验。`scripts/browser_smoke.mjs` 用 Playwright 驱动 **本机已有的 Edge**
（`channel: "msedge"`，不额外下载浏览器），跑一遍写作台并留下截图：

- 输出：`data/browser-shots/*.png`（已被 gitignore），失败时的现场在 `98-stuck.png`
- 收集 **页面异常与 console.error**，任一出现即判失败
- 用 `#writing?offline=1` 走**离线模式**：拟方案走通用方向、成段走骨架。
  浏览器冒烟必须快且可复现，否则每步等真实模型（v4-pro 拟 3 案可能几分钟）
  就没法当回归用

它已经抓到过三类前三层完全看不见的真实缺陷：`questionHtml` 跨函数引用（ReferenceError）、
属性选择器拼错、以及**事件绑定绑到了被重建的旧元素上**（按钮看得见、点了毫无反应、还不报错）。

# 5.（可选）在**正在运行的服务**上核对节点工作流，并打印界面会看到的内容
#     先起服务，再执行；脚本会建一个临时项目、跑完整链路、最后删掉
uv run python scripts/verify_section_flow.py --base http://127.0.0.1:8000

### 部署注意（方案风险 R2）

`.env.example` 里 `RA_JOURNAL_QUARTILES=data/jcr/jcr_quartiles_full.json` 指向
**被 gitignore 的 `data/`**。若没有先跑 `examples/import_jcr_xlsx.py` 生成全量表，
系统会**静默退回**仓库内的化学子集（409 条）。打开「系统状态」页可以看到当前实际生效的
条目数与文件是否存在。

---

## 五、测试与验证矩阵

| 层 | 数量 | 说明 |
|---|---:|---|
| 上游既有 | 169 | 未改动，全部仍通过 |
| `test_library.py` | 52 | 侧车 CRUD/级联/筛选分面/低信号/批量作业/引用四样式/导出/系统状态 |
| `test_writing.py` | 21 | 体裁来自 pack、显式降级标记、引用入库、导出、路由装配 |
| `test_section_flow.py` | 28 | 节点指令工作流：词元/停用词、五维判定与硬度闸门、预算用尽、补检→复审、逐轮留痕、引文绑定与越界报告、取消、提示词纪律 |
| `test_section_templates.py` | 30 | **两套模板与工作规划**：模板完整性、维度按部分区分、系统自拟与用户模式、字段来源（user/plan/template）与纯并行优先级、同一库不同结论、带缺口写作与标注幂等、轨迹记录模板 |
| `test_logging.py` | **27** | **统一日志**：事件词表受控、脱敏与截断、trace 关联、环形缓冲过滤、`LoggedModel` 代理记账与失败现场、**嵌套代理不重复记账**、`/api/log` 白名单与 trace 透传 |
| `test_dispatch.py` | **24** | **派工协议**：登记项全部有实现、幂等（同单号只跑一次、失败可重跑）、取消、`$stepN` 引用串联与不可解析时跳过、进度上报、预算与步数上限、长正文摘要化 |
| `test_dispatch_planner.py` | **25** | **自然语言派工**：意图识别（中英）、方案随意图分族、只认登记表任务（模型编的任务名被丢弃）、`skip_reason` 语义（用户点名的保留、自动追加的删掉）、项目上下文与 `project_id` 注入、离线不碰网络、结果可直接下单 |
| `test_frontend_assets.py` | 18 | 资源存在、**相对导入可解析**、注册表一致性、转义策略+自检、PWA 令牌一致、**前端接口与后端路由逐一配对** |
| `test_migration.py` | 10 | dry-run 不写库、apply 齐全、幂等、状态映射、质量分换算、溯源 |
| **单元测试合计** | **404** | `OK` |
| `frontend_smoke.mjs` | 47 | 真实 import 全部模块、调用 `mount()`、校验 9 页契约与注册表 |
| `check_frontend_routes.py` | 56 处调用 | 前端接口 ↔ 后端 63 条 `/api` 路由逐一配对（错配只会在点击时 404） |
| `smoke_dashboard.py` | 139 | 真实 HTTP：新端点 + **工作规划/模板/带缺口写作** + 节点工作流全链路 + 静态资源 + **旧端点无回归** |
| `browser_smoke.mjs` | **36** | **真实浏览器（Playwright + 本机 Edge）**：写作台问答全链路 + 对节点下指令（解析→选方案→下单）+ 研究流程页（访谈节点在清单、诚实的「未执行」、派工板、分组筛选与详情在重渲染后仍可点）+ 无页面异常/无 4xx/无 console.error |
| `verify_section_flow.py` | 人工核对 | 在**正在运行的库**上跑一遍并打印判定/轨迹/正文，用于肉眼确认界面所见 |
| `verify_real_model.py` | 人工核对（需 Key） | 真实模型把自然语言解析成派工方案，并打印同 trace 的统一日志。**刻意不进单测**：回归不能依赖外网与 Key |
| `verify_dispatch_live.py` | 人工核对 | 在**正在运行的服务**上核对派工链路：登记表 → 解析中文指令 → 下单 → 单号可查 → 未登记任务被拒 → 节点总览 |

---

## 六、已知限制（诚实标注）

1. ~~**没有真实浏览器验证**~~ → **已解决（v0.4.5）**：装了 Playwright + 本机 Edge，
   `scripts/browser_smoke.mjs` 做 33 项真实点击。它在上一版就抓到了三个静态检查与
   HTTP 冒烟都发现不了的真 bug（`questionHtml` 作用域错误、给会被替换的元素单独绑事件、
   错误被静默吞掉），所以这一层不是可选项。
2. **模型相关路径未实跑**：写作台的 LLM 生成、成段、知识消费等需要 API Key；本次验证覆盖的是
   "无模型显式降级"路径（也是默认路径）。**成段链路的模型分支只在单测里用假模型验证**
   ——`test_prompt_carries_numbered_materials_and_instruction` 断言提示词确实带上了编号素材、
   节点指令与引用纪律，但没有真实模型输出可对标。
2b. **工作规划目前是确定性的**：无模型时，各部分的写作要点来自"模板 `focus` +
   按部分职责映射的 `analysis_plan` 维度"；有模型时 LLM 只出**一份**整篇任务单
   （不是逐部分调用）。因此"系统自拟"的质量主要取决于主题词与模板设计，
   配 Key 后建议对照一次真实规划结果。
2c. **`research_article` 体裁尚未配部分模板**：它落回旧行为（无模板 → 全部既有
   闸门 + 不允许带缺口写作）。要让它也享受"按部分判定"，需按同样格式补 `templates`。
3. **充分性判定目前是确定性阈值**：五维加权 + 硬闸门全部由 SQL 统计得出，模型只作为
   "可选的保守复核"（传入 `llm_analysis` 时取更保守的结论）。因此判定质量取决于库的元数据
   完整度（抽取记录、超边条件/测量），元数据缺失会表现为"缺数据"而非"模型说不行"。
4. **`min_papers` 默认阈值的自适应**：默认 6 篇，但若未在指令里声明引用条数且库内已入库总数
   更少，则下调到库的实际规模（否则演示库会被一律判死）。显式收紧到 ≤2 或
   `RA_SECTION_ADAPTIVE_PAPER_FLOOR=0` 时不自适应，便于严格模式。
5. **`stale` 状态已定义但未自动标记**：节点成段后若其支撑文献被删除，状态不会自动变为
   `stale`（需要手动重跑判定）。这是已知缺口，建议按需再补。
6. **迁移只处理 PWA 的 papers + 四张侧车表**：PWA 的 `review_items`（映射语义不清）与
   `writing_projects/sections`（字段差异大）未迁移。
7. **动态本体页的图谱上限**：仍受 `/api/ontology` 的 `limit` 影响，超出时页面会明确提示
   "已按 limit 截断"，可用类型/置信度/域筛选收窄。
8. ~~**自然语言派工尚未接通**~~ → **已完成（v0.4.5）**：`writing/dispatch_planner.py`
   ＋ `POST /api/dispatches/parse` ＋ 写作台的「对节点下指令」面板，让"用你自己的话
   下指令"变成三份可执行方案（只认登记表里的任务，模型编出的任务名会被丢掉）。
   派工来源因此有三条，共用一条执行链：缺口决定 / 自定义任务 / 用户直下指令。
9. **检索源限流与查询构造未处理**：OpenAlex 对"引号 + 通配"的查询会返回
   `400 Bad Request`，Semantic Scholar 会 `429`。目前只是**如实记录错误**
   （`dispatch.step` 的 `errors` 字段与统一日志都能看到），没有退避重试或
   查询改写，因此补检的有效性受源站状态影响。
10. **「作答」与「推进」仍是两次请求**：中间存在一个用户可见的状态窗口
    （已用 `isAdvancing` 挡住自动刷新，但两次请求本身可以合并成一次）。
11. **`stale` 状态仍未自动标记**：见第 5 条——语义与界面已就绪，缺一个
    "支撑文献被删/重跑后置为过期"的自动触发。

