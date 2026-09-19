# research-desk

**PWA 的可视化界面 × trial 的研究引擎。**

界面取自 `JoeTrump22/-paper_writing_assistant`（Streamlit，10 个页面、深色主题、`st.fragment` 实时进度），
引擎取自 `Shan-Shu/trial`（LangGraph 多模型编排、多源检索、质量控制、知识抽取、动态本体、
研究流程与算子链、写作台访谈闭环）。

```
┌──────────────────────────────────────────────┐
│  界面 = src/desk/ui/   （Streamlit，10 页）    │
└────────────────────┬─────────────────────────┘
                     │  ← 唯一新代码
                     │     desk/backend.py  48 个 service 方法
                     │     desk/tasks.py     3 个任务方法
┌────────────────────▼─────────────────────────┐
│  引擎 = src/research_agent/   （原样复用）      │
└──────────────────────────────────────────────┘
```

## 这个项目怎么来的

- 旧仓库 `../research-agent` 已**冻结**（tag `frozen-before-research-desk`），不再改动，只作只读参考。
- 引擎、测试、`packs/` 原样搬入，**逻辑不动**；FastAPI 数据面（`dashboard/app.py`）保留为可选，
  但旧的 JS 前端（`dashboard/static/`，约 4400 行）**刻意未搬入**。
- PWA 的界面（`ui/`，1340 行）原样搬入 `src/desk/ui/`；PWA 的后端（5113 行）**一行未搬**——
  经逐项核对，其能力已全部被本引擎覆盖（含本体可视化）。
- PWA 界面里对 `paper_assistant.*` 的引用改为指向本引擎与 `desk.*`。

## 怎么跑

```powershell
# 第一次：装依赖
uv sync

# 启动界面（也可直接双击 启动助手.bat）
uv run research-desk
```

浏览器打开 http://127.0.0.1:8501 。

可选：HTTP 数据面（旧界面退役后仍保留，便于脚本调用）

```powershell
uv run research-desk-api --port 8000
```

## 怎么验证

四层回归，全部可离线复跑（`RA_DESK_OFFLINE=1` 时不调模型，也不烧配额）：

```powershell
uv run python scripts/run_tests.py                    # 引擎单测（428 项，自带"真实库未被写入"对账）
uv run python scripts/smoke_desk.py --db data\desk.db   # 10 页逐页渲染不报错
uv run python scripts/smoke_desk_writing.py         # 写作台访谈闭环 → 正文落库
uv run python scripts/smoke_desk_projects.py        # 写作台项目管理（重命名 / 批量删除）
uv run python scripts/smoke_desk_dispatch.py        # 自然语言 → 规划节点 → 分发执行
```

- **单测请用 `scripts/run_tests.py`**：它把 `RA_DB_PATH` 指向临时文件，并在跑完后对账
  真实库快照。直接 `python -m unittest discover -s tests` 曾会让少数用例把
  `processing_log` 写进默认库 `data/research_agent.db`（一轮 20 行）——调用点已修，
  这个运行器是防它再犯的那道网。

- `smoke_desk.py` 用 `streamlit.testing.v1.AppTest` 在进程内真跑页面。**HTTP GET 证明不了界面正常**——
  Streamlit 页面在 WebSocket 会话里执行，普通请求只拿到空壳 HTML。
- `smoke_desk_dispatch.py` 会**整库复制**一份到 `data/dispatch_smoke.db` 再派工，正式库只读；
  它同时守住"对 A 库下单却写进 B 库"和"离线仍真调模型"这两个真踩过的坑。
- `smoke_desk_projects.py` 真点「打开 / 返回」导航、「重命名」与「批量删除」，并在临时库上
  对账数据库结果（含**无外键的 `dispatch_runs` 孤儿**必须被一并清掉）。
- 看真实库数据（不是"能渲染"，而是"有数据"）：`uv run python scripts/show_desk_data.py`。
- 界面卡死或怀疑有残留实例占着后台跑：
  `uv run python scripts/kill_strays.py --dry-run` 只列出，去掉 `--dry-run` 即结束它们
  （`停止助手.bat` 已内置这一步；它按命令行特征查找，因此**不依赖端口**，卡死的实例也能清掉）。
- 看成段**到底拿到了什么**（材料里的真实数值 + 知识消费产物；整库复制、不调模型、不花配额）：
  `uv run python scripts/show_compose_prompt.py`。

## 目录

```
src/desk/            本项目新增：Streamlit 入口 + 适配层 + 界面
  app.py             Streamlit 主入口（导航 / 主题 / 全局状态栏）
  backend.py         BackendAdapter：界面要的 48 个方法 → 引擎
  tasks.py           任务适配：把引擎的作业管理器包成界面要的 3 个方法
  ui/                从 PWA 原样搬入的 10 个页面与组件
src/research_agent/  从 trial 原样搬入的引擎
packs/               领域内容（零硬编码的来源）
tests/               引擎安全网（从 trial 搬入，去掉已退役 JS 前端的用例）
```

## 与旧仓库的关系

| | 旧仓库 `research-agent` | 本项目 `research-desk` |
|---|---|---|
| 界面 | 自写 JS 前端（已退役） | **PWA Streamlit 界面** |
| 引擎 | 同一套 | 同一套（搬入后独立演进） |
| FastAPI | 主线 | 可选数据面 |

引擎的后续改动只在本项目进行。
