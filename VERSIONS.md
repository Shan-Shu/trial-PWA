# Versions

research-desk 的版本登记。**引擎来源**单独标注：本项目 = PWA 界面 × trial 引擎。

| 版本 | 说明 |
|---|---|
| v0.1.0 | **research-desk 起步。** 新建独立项目（旧仓库 `research-agent` 冻结，tag `frozen-before-research-desk`，引擎来源 v0.4.5 @ 36a14fe）。界面：从 PWA（`JoeTrump22/-paper_writing_assistant`）原样搬入 `ui/` 共 1340 行（10 页 + Streamlit 外壳 + `st.fragment` 实时进度 + 深色主题 token），写作台页保留、后续按访谈闭环重做；引擎：`src/research_agent/` 原样搬入（约 21636 行），FastAPI 数据面保留为可选，旧 JS 前端（约 4372 行）刻意未搬入；PWA 后端 5113 行经逐项核对能力已被引擎覆盖，一行未搬。搬入时修掉一个真缺陷：派工的执行函数会落到全局默认库（**对 A 库下单却写进 B 库**），现改为注入本次派工所在库与连接，并让本体重建任务自建 schema。测试 462 → 461（去掉已退役 JS 前端的用例与依赖未搬脚本的 PWA 迁移用例）。 |

## 来源对照

| 部分 | 来源 | 备注 |
|---|---|---|
| `src/research_agent/` | trial（原 `Shan-Shu/trial`，后为 `research-agent`）v0.4.5 @ `36a14fe` | 引擎本体，逻辑未改 |
| `src/desk/ui/` | PWA `JoeTrump22/-paper_writing_assistant` | 界面本体，仅改数据来源引用 |
| `packs/` | trial | 领域内容（零硬编码的来源） |
| `tests/` | trial | 引擎安全网 |

## 刻意未搬入

| 部分 | 行数 | 原因 |
|---|---:|---|
| `dashboard/static/`（旧 JS 前端） | 约 4372 | 界面改用 Streamlit，此套退役 |
| PWA 后端（`paper_assistant/` 除 `ui/`） | 5113 | 能力已被引擎覆盖（含本体可视化：引擎 `render_interactive_html` 有本地 vendor 挂载点 + 静态 SVG + 文件导出） |
| `tests/test_migration.py`、`tests/test_frontend_assets.py` | — | 分别依赖未搬入的 PWA 迁移脚本、已退役的 JS 前端 |
