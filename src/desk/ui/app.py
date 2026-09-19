from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

# 允许未安装时直接 `streamlit run`（editable 安装下这行是幂等的）
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from desk.backend import BackendAdapter  # noqa: E402
from desk.tasks import TaskManager  # noqa: E402
from desk.ui.views import DEFAULT_PAGE, PAGES  # noqa: E402


@dataclass
class UIContext:
    """界面能拿到的全部东西。

    `service` 是**引擎门面**（`desk.backend.BackendAdapter`），方法名与 PWA 的
    `PaperWritingAssistant` 一致，因此 10 个页面一行不用改。
    """

    service: BackendAdapter
    settings: object
    task_manager: TaskManager


def _db_path() -> str:
    """用哪个库：环境变量 RA_DESK_DB 优先，否则引擎的默认库。"""
    import os

    from research_agent.config import settings as default_settings

    return str(os.getenv("RA_DESK_DB") or default_settings.db_path)


@st.cache_resource
def get_service() -> BackendAdapter:
    return BackendAdapter(db_path=_db_path())


@st.cache_resource
def get_task_manager() -> TaskManager:
    return TaskManager(_db_path())


def _inject_css() -> None:
    st.markdown(
        """
        <style>
        :root {
            --bg-deep: #0A0E1A;
            --bg-side: #0F1629;
            --bg-card: #162040;
            --bg-hover: #1E2A4A;
            --bg-active: #263456;
            --text-main: #E8ECF4;
            --text-sub: #A0AEC8;
            --text-muted: #6B7A9E;
            --border: #1E2A4A;
            --border-strong: #2D3A5C;
            --accent: #3B82F6;
            --cyan: #06B6D4;
            --green: #10B981;
            --amber: #F59E0B;
            --red: #EF4444;
            --violet: #8B5CF6;
        }

        html, body, [data-testid="stAppViewContainer"] {
            background: var(--bg-deep);
            color: var(--text-main);
        }

        [data-testid="stHeader"] {
            background: rgba(10, 14, 26, 0.72);
            backdrop-filter: blur(8px);
        }

        [data-testid="stSidebar"] {
            background: var(--bg-side);
            border-right: 1px solid var(--border);
        }

        .block-container {
            padding-top: 1.25rem;
            padding-bottom: 4rem;
            max-width: 1500px;
        }

        h1, h2, h3, h4, h5, h6, p, span, label {
            color: var(--text-main);
        }

        .stApp {
            font-family: "Inter", "Segoe UI", "Microsoft YaHei", sans-serif;
            background:
                radial-gradient(circle at 12% 12%, rgba(59, 130, 246, 0.08), transparent 28%),
                radial-gradient(circle at 88% 18%, rgba(139, 92, 246, 0.06), transparent 30%),
                radial-gradient(circle at 72% 88%, rgba(6, 182, 212, 0.05), transparent 28%),
                linear-gradient(135deg, #0A0E1A 0%, #0E1628 52%, #0A0E1A 100%);
        }

        .stApp::before {
            content: "";
            position: fixed;
            inset: 0;
            pointer-events: none;
            background-image:
                linear-gradient(rgba(59, 130, 246, 0.025) 1px, transparent 1px),
                linear-gradient(90deg, rgba(59, 130, 246, 0.025) 1px, transparent 1px);
            background-size: 48px 48px;
            mask-image: radial-gradient(circle at 50% 30%, rgba(0,0,0,0.6), transparent 75%);
        }

        div[data-testid="stMetric"] {
            background: linear-gradient(145deg, #111A33, #0F1629);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 12px 14px;
            box-shadow: 0 8px 20px rgba(0,0,0,0.18);
            transition: border-color 150ms ease, background 150ms ease;
        }

        div[data-testid="stMetric"]:hover {
            border-color: var(--accent);
            background: linear-gradient(145deg, #14203F, #101A34);
        }

        div[data-testid="stMetricLabel"] p {
            color: var(--text-sub) !important;
        }

        div[data-testid="stMetricValue"] {
            color: var(--text-main);
        }

        .stButton > button {
            background: #162040;
            border: 1px solid var(--border-strong);
            color: var(--text-main);
            border-radius: 8px;
            transition: all 100ms ease;
        }

        .stButton > button:hover {
            background: var(--bg-hover);
            border-color: var(--accent);
            color: #fff;
            transform: translateY(-1px);
        }

        .stButton > button[kind="primary"] {
            background: var(--accent);
            border-color: var(--accent);
            color: #fff;
        }

        .stButton > button[kind="primary"]:hover {
            background: #2563EB;
            border-color: #2563EB;
        }

        .stTextInput input,
        .stTextArea textarea,
        .stNumberInput input,
        .stSelectbox div[data-baseweb="select"] > div {
            background: #0F1629;
            border-color: var(--border-strong);
            color: var(--text-main);
            border-radius: 8px;
        }

        .stTextInput input:focus,
        .stTextArea textarea:focus,
        .stNumberInput input:focus {
            border-color: var(--accent);
            box-shadow: 0 0 0 1px var(--accent);
        }

        [data-testid="stExpander"] {
            background: #101A34;
            border: 1px solid var(--border);
            border-radius: 10px;
            overflow: hidden;
        }

        [data-testid="stExpander"] summary {
            color: var(--text-main);
        }

        .stTabs [data-baseweb="tab-list"] {
            background: #0F1629;
            border-bottom: 1px solid var(--border);
            border-radius: 8px;
        }

        .stTabs [data-baseweb="tab"] {
            color: var(--text-sub);
        }

        .stTabs [aria-selected="true"] {
            color: var(--accent) !important;
        }

        [data-testid="stDataFrame"] {
            border: 1px solid var(--border);
            border-radius: 10px;
            overflow: hidden;
        }

        [data-testid="stProgress"] > div > div > div {
            background: var(--accent);
        }

        [data-testid="stProgress"] > div > div {
            background: #1E2A4A;
        }

        .global-status-bar {
            position: fixed;
            left: 0;
            right: 0;
            bottom: 0;
            height: 30px;
            padding: 0 16px;
            display: flex;
            align-items: center;
            gap: 18px;
            background: rgba(15, 22, 41, 0.92);
            border-top: 1px solid var(--border);
            backdrop-filter: blur(8px);
            color: var(--text-sub);
            font-size: 12px;
            z-index: 2147483647;
            pointer-events: none;
        }

        .global-status-bar span {
            color: var(--text-sub);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(
        page_title="research-desk · 研究工作台",
        page_icon="🧪",
        layout="wide",
    )
    _inject_css()

    service = get_service()
    ctx = UIContext(
        service=service,
        settings=service.settings,
        task_manager=get_task_manager(),
    )

    with st.sidebar:
        st.title("🧪 research-desk")
        st.caption("研究工作台 · 界面取自 PWA，引擎来自 trial")
        choices = {f"{p.icon} {p.title}": p.key for p in PAGES.values()}
        selected_label = st.radio("导航", list(choices))
        selected_key = choices[selected_label]

    page = PAGES.get(selected_key) or PAGES[DEFAULT_PAGE]
    page.render(ctx)

    # 底部全局状态栏：计数来自适配器（引擎侧没有 PWA 的 `ontology_store` 对象）
    overview = service.library_overview()
    st.markdown(
        f"""
        <div class="global-status-bar">
            <span style="color:#10B981;">●</span> 引擎在线
            <span>文献 {overview['total']}</span>
            <span>本体术语 {overview['ontology_terms']}</span>
            <span>本体关系 {overview['ontology_relations']}</span>
            <span>已抽取知识 {overview['knowledge_extracted']}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()