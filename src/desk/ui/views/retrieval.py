from __future__ import annotations

import pandas as pd
import streamlit as st

from desk.compat import render_ontology_svg


MODE_OPTIONS = {
    "混合检索": "hybrid",
    "关键词检索": "keyword",
    "语义检索": "semantic",
    "图谱检索": "graph",
}


def render(ctx) -> None:
    st.subheader("知识检索")
    st.caption("同时检索文献、知识实体、三元组、动态本体和实验库。")

    with st.form("knowledge_search_form"):
        query = st.text_input("检索问题或关键词", placeholder="例如：二甲双胍与胰岛素敏感性")
        mode_label = st.selectbox("检索模式", list(MODE_OPTIONS))
        limit = st.slider("结果数量", 10, 100, 30)
        submitted = st.form_submit_button("开始知识检索", type="primary")

    if submitted and query.strip():
        st.session_state["knowledge_search_result"] = ctx.service.hybrid_search(
            query.strip(),
            mode=MODE_OPTIONS[mode_label],
            limit=int(limit),
        )

    result = st.session_state.get("knowledge_search_result")
    if not result:
        st.info("输入检索问题后开始知识检索。")
        return

    clarifications = result.get("clarifications") or []
    if clarifications:
        for question in clarifications:
            st.warning(question)

    st.markdown("### 检索结果")
    items = result.get("items") or []
    if items:
        rows = [
            {
                "类型": _type_label(item.get("type")),
                "标题": item.get("title") or "",
                "综合得分": item.get("final_score"),
                "置信度": item.get("confidence"),
                "来源模式": "、".join(item.get("modes") or [item.get("mode") or ""]),
            }
            for item in items
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        aggregation = ctx.service.aggregate_search_results(items)
        if aggregation:
            with st.expander("结果聚合"):
                st.write("按类型：", aggregation.get("by_type"))
                st.write("按年份：", aggregation.get("by_year"))

        if st.button("推荐关联文献与知识"):
            related = ctx.service.recommend_related(
                query.strip(),
                items,
                limit=10,
            )
            if related:
                st.markdown("#### 关联推荐")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "类型": _type_label(r.get("type")),
                                "标题": r.get("title") or "",
                                "综合得分": r.get("final_score"),
                            }
                            for r in related
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("暂无更多关联推荐。")
    else:
        st.info("没有检索到相关内容，请尝试缩短关键词或切换检索模式。")

    graph = result.get("graph") or {}
    if graph.get("nodes"):
        st.markdown("### 图谱扩展")
        st.write(f"命中种子术语 {graph.get('seed_count', 0)} 个，扩展后节点 {graph.get('node_count', 0)} 个，关系 {graph.get('edge_count', 0)} 条。")
        st.markdown(
            render_ontology_svg(graph["nodes"], graph["edges"], max_nodes=60),
            unsafe_allow_html=True,
        )


def _type_label(item_type: str) -> str:
    return {
        "literature": "文献",
        "entity": "实体",
        "triple": "三元组",
        "ontology": "本体术语",
        "experiment": "实验知识",
        "experiment_design": "实验设计",
    }.get(item_type, item_type)