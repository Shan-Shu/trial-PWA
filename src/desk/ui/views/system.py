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
    st.subheader("系统状态面板")
    status = ctx.service.system_status()

    st.markdown("### 智能体状态")
    for key in ctx.service.active_phases:
        st.write(f"- {AGENT_LABELS.get(key, key)}")

    st.markdown("### 存储统计")
    papers = status["papers"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("文献总数", papers["total"])
    c2.metric("质量通过", papers["passed"])
    c3.metric("待复核", papers["rejected"])
    c4.metric("平均评分", papers["avg_score"])

    knowledge = status["knowledge"]
    ontology = status["ontology"]
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("知识实体", knowledge["entities"])
    c6.metric("知识三元组", knowledge["triples"])
    c7.metric("本体术语", ontology["terms"])
    c8.metric("本体关系", ontology["relations"])

    experiment = status["experiment"]
    writing = status["writing"]
    review = status["review"]
    c9, c10, c11, c12 = st.columns(4)
    c9.metric("实验知识", experiment["experiment_knowledge"])
    c10.metric("实验设计", experiment["designs"])
    c11.metric("写作项目", writing["projects"])
    c12.metric("待审核", review["pending"])