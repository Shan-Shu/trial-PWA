from __future__ import annotations

import pandas as pd
import streamlit as st

from desk.ui.task_ui import ensure_started, task_progress


def render(ctx) -> None:
    st.subheader("知识抽取")
    st.caption("选择文献库中的一篇文献，构建标准化文献对象（SDO）并抽取实体与三元组。")

    papers = ctx.service.list_papers(limit=1000)
    if not papers:
        st.info("文献库为空，请先到“智能检索”页检索文献。")
        return

    with st.expander("批量抽取", expanded=False):
        st.write("对文献库中所有“质量通过”的文献执行知识抽取。")
        if st.button("批量抽取全部已通过文献", type="primary"):
            st.session_state["start_knowledge_batch_task"] = True
        if st.session_state.get("start_knowledge_batch_task"):
            ensure_started(
                ctx,
                "knowledge_batch_task",
                ctx.service.extract_all_knowledge,
                "quality_passed",
            )
        batch_status = task_progress(ctx, "knowledge_batch_task")
        if batch_status and batch_status["status"] == "done" and batch_status.get("result"):
            result = batch_status["result"]
            st.success(
                f"批量抽取完成：共 {result['total']} 篇，"
                f"成功 {result['ok']} 篇，跳过已抽取 {result.get('skipped', 0)} 篇。"
            )

    st.divider()

    options = {f"#{p.get('id')} · {p.get('title') or '未命名文献'}": p for p in papers}
    label = st.selectbox("选择文献", list(options))
    paper = options[label]
    paper_id = paper["id"]

    st.write("**标题**：", paper.get("title"))
    if paper.get("abstract"):
        with st.expander("摘要"):
            st.write(paper["abstract"])

    if st.button("构建 SDO 并抽取知识", type="primary"):
        st.session_state["start_knowledge_paper_task"] = paper_id
    if st.session_state.get("start_knowledge_paper_task") == paper_id:
        task_id = f"knowledge_paper_{paper_id}"
        ensure_started(ctx, task_id, ctx.service.extract_knowledge, paper_id)
        paper_status = task_progress(ctx, task_id)
        if paper_status and paper_status["status"] == "done":
            st.success("知识抽取完成。")

    data = ctx.service.knowledge_data(paper_id)
    stats = data["stats"]
    c1, c2 = st.columns(2)
    c1.metric("实体数量", stats["entities"])
    c2.metric("三元组数量", stats["triples"])

    if data["sdo"]:
        with st.expander("标准化文献对象（SDO）"):
            st.json(data["sdo"])

    if data["entities"]:
        with st.expander("抽取实体", expanded=False):
            rows = [
                {
                    "名称": e.get("name"),
                    "规范名称": e.get("canonical_name"),
                    "类型": e.get("entity_type"),
                    "置信度": e.get("confidence"),
                    "别名": "；".join(e.get("aliases") or []),
                }
                for e in data["entities"]
            ]
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    if data["triples"]:
        with st.expander("抽取三元组", expanded=False):
            rows = [
                {
                    "主语": t.get("subject"),
                    "谓词": t.get("predicate"),
                    "宾语": t.get("object"),
                    "类型": t.get("kind"),
                    "置信度": t.get("confidence"),
                }
                for t in data["triples"]
            ]
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)