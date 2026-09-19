from __future__ import annotations

import streamlit as st

from desk.ui.components import render_paper_list
from desk.compat import INTENT_LABELS
from desk.ui.task_ui import ensure_started, task_progress


ALL_SOURCES = ["pubmed", "crossref", "openalex", "arxiv"]
MODE_OPTIONS = {"自动识别": None, "一般检索": "general", "标题检索": "title", "精确标识符": "exact", "最新进展": "recent", "系统综述": "systematic", "临床证据": "clinical", "实验与方法": "experiment"}
SOURCE_LABELS = {"pubmed": "PubMed", "crossref": "Crossref", "openalex": "OpenAlex", "arxiv": "arXiv"}


def render(ctx) -> None:
    st.subheader("智能检索")
    st.caption("输入一句自然语言，系统会自动分析研究主题、年份范围、期望篇数和数据源。")

    with st.form("search_form", clear_on_submit=False):
        raw_query = st.text_area(
            "研究需求",
            value=st.session_state.get("last_query", ""),
            placeholder="例如：检索近5年有关糖尿病分子机制的相关论文30篇",
            height=120,
        )
        st.caption("示例：检索近5年有关糖尿病分子机制的相关论文30篇")

        with st.expander("高级选项", expanded=False):
            source_mode = st.radio("数据源模式", ["自动分析", "手动指定"], horizontal=True)
            manual_sources = st.multiselect(
                "手动指定数据源",
                options=ALL_SOURCES,
                default=[],
                disabled=source_mode == "自动分析",
            )
            mode_label = st.selectbox("检索模式", list(MODE_OPTIONS))
            max_override = st.number_input(
                "手动限制最终文献总数（0 表示自动识别）",
                min_value=0,
                max_value=100,
                value=0,
            )

        submitted = st.form_submit_button("开始检索", type="primary")

    recent = ctx.service.recent_searches(10)
    if recent:
        with st.expander("最近检索", expanded=False):
            for row in recent:
                if st.button(row.get("raw_query") or "", key=f"recent_{row.get('id')}"):
                    st.session_state["last_query"] = row.get("raw_query") or ""
                    st.rerun()

    if submitted:
        if not raw_query.strip():
            st.warning("请输入研究需求。")
            return
        sources = None if source_mode == "自动分析" else manual_sources
        if source_mode == "手动指定" and not sources:
            st.warning("请选择至少一个数据源，或切换为“自动分析”。")
            return
        st.session_state["last_query"] = raw_query.strip()
        st.session_state["search_task_args"] = (
            raw_query.strip(),
            int(max_override) if int(max_override) > 0 else None,
            sources,
            MODE_OPTIONS[mode_label],
        )
        st.session_state["start_search_task"] = True

    if st.session_state.get("start_search_task"):
        args = st.session_state.get("search_task_args")
        if args:
            ensure_started(ctx, "search_task", ctx.service.search, *args)

    status = task_progress(ctx, "search_task")

    if status and status["status"] == "done" and status.get("result"):
        result = status["result"]
        _show_result(ctx, result)


def _show_result(ctx, result) -> None:
    plan = result.get("plan", {})
    stats = result.get("stats", {})

    st.divider()
    st.markdown("### 关键词分析结果")
    st.write("查询式：", "；".join(plan.get("queries") or []))
    st.write(
        "年份范围：",
        f"{plan.get('year_from') or '不限'} - {plan.get('year_to') or '不限'}",
    )
    st.write("数据源：", "、".join(SOURCE_LABELS.get(s, s) for s in (plan.get("sources") or [])))
    st.write("检索模式：", plan.get("mode") or "自动识别")
    st.write("期望总篇数：", plan.get("max_results"))
    if plan.get("focus"):
        st.write("研究焦点：", plan["focus"])
    if plan.get("tags"):
        st.write("标签：", "、".join(plan["tags"]))
    intent = plan.get("intent") or {}
    if intent:
        st.markdown("### 查询意图与关键要素")
        st.write("意图：", INTENT_LABELS.get(intent.get("intent"), intent.get("intent") or "一般文献检索"))
        pico = intent.get("pico") or {}
        if any(pico.values()):
            st.write(
                "PICO：",
                f"人群={pico.get('population') or '未识别'}；干预={pico.get('intervention') or '未识别'}；对照={pico.get('comparator') or '未识别'}；结局={pico.get('outcome') or '未识别'}",
            )
        if intent.get("study_type"):
            st.write("研究类型：", intent["study_type"])
        if intent.get("keywords"):
            st.write("关键要素：", "、".join(intent["keywords"]))
        if intent.get("constraints"):
            st.write("约束条件：", "；".join(intent["constraints"]))

    st.markdown("### 检索与质量评估结果")
    cols = st.columns(6)
    cols[0].metric("期望", plan.get("max_results", 0))
    cols[1].metric("获取", stats.get("fetched", 0))
    cols[2].metric("去重", stats.get("duplicates", 0))
    cols[3].metric("通过", stats.get("passed", 0))
    cols[4].metric("待复核", stats.get("rejected", 0))
    cols[5].metric("查询式", stats.get("query_count", 0))

    rows = ctx.service.list_papers(query_id=result.get("query_id"))
    st.markdown("#### 本次入库文献")
    render_paper_list(rows)