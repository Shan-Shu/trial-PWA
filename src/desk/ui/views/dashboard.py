from __future__ import annotations

import streamlit as st


AGENT_LABELS = {
    "keyword": "关键词分析智能体",
    "search": "文献检索智能体",
    "quality": "质量评估智能体",
    "knowledge": "知识抽取智能体",
    "ontology": "动态本体构建智能体",
    "experiment_aggregation": "实验知识聚合智能体",
    "experiment_design": "实验设计建议智能体",
    "writing": "论文写作智能体",
    "review": "审核协调智能体",
    "keyword_analysis": "关键词趋势分析智能体",
}


def render(ctx) -> None:
    st.subheader("项目总览")
    stats = ctx.service.stats()

    cols = st.columns(6)
    cols[0].metric("文献总数", stats["total"])
    cols[1].metric("质量通过", stats["passed"])
    cols[2].metric("待审核", stats.get("review", 0))
    cols[3].metric("待复核", stats["rejected"])
    cols[4].metric("平均评分", stats["avg_score"])
    cols[5].metric("检索次数", stats["queries"])

    st.divider()
    st.markdown("### 多智能体状态")
    st.write("当前已激活：")
    for key in ctx.service.active_phases:
        st.write(f"- {AGENT_LABELS.get(key, key)}")
    st.write("后续预留：关键词趋势分析智能体")