from __future__ import annotations

import pandas as pd
import streamlit as st

from desk.compat import render_interactive_graph


def render(ctx) -> None:
    st.subheader("动态本体构建")
    st.caption("从已抽取的知识中聚合术语、别名和关系，形成知识图谱。")

    if st.button("重新构建动态本体", type="primary"):
        with st.spinner("动态本体构建中..."):
            try:
                result = ctx.service.build_ontology()
                st.session_state["ontology_build_result"] = result
                st.success("本体构建完成。")
            except Exception as exc:
                st.error(f"本体构建失败：{exc}")

    data = ctx.service.ontology_data()
    stats = data["stats"]
    c1, c2 = st.columns(2)
    c1.metric("本体术语", stats["terms"])
    c2.metric("本体关系", stats["relations"])

    terms = data["terms"]
    relations = data["relations"]

    if terms:
        st.markdown("### 知识图谱")
        st.components.v1.html(
            render_interactive_graph(terms, relations, max_nodes=80),
            height=780,
            scrolling=True,
        )

    if terms:
        with st.expander("本体术语", expanded=False):
            rows = [
                {
                    "术语": t.get("term"),
                    "类型": t.get("term_type"),
                    "别名": "；".join(t.get("aliases") or []),
                    "置信度": t.get("confidence"),
                    "来源文献数": len(t.get("source_paper_ids") or []),
                }
                for t in terms
            ]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    if relations:
        with st.expander("本体关系", expanded=False):
            rows = [
                {
                    "主语": r.get("subject_term"),
                    "关系": r.get("relation"),
                    "宾语": r.get("object_term"),
                    "置信度": r.get("confidence"),
                    "来源文献数": len(r.get("source_paper_ids") or []),
                }
                for r in relations
            ]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)