from __future__ import annotations

import streamlit as st


DECISIONS = {
    "批准": "approved",
    "拒绝": "rejected",
    "修改": "revised",
}


def render(ctx) -> None:
    st.subheader("审核中心")
    st.caption("管理低置信度候选知识，记录批准、拒绝和修改反馈。")

    if st.button("从当前本体生成待审核项"):
        result = ctx.service.create_review_candidates()
        st.success(f"已生成 {result['created']} 条待审核项。")

    prefs = ctx.service.review_preferences()
    if prefs:
        with st.expander("用户评分偏好", expanded=False):
            st.dataframe(
                [
                    {
                        "维度": p["dimension"],
                        "偏好": p["preference"],
                        "平均调整": round(p["avg_adjustment"] or 0, 3),
                        "次数": p["count"],
                    }
                    for p in prefs
                ],
                width="stretch",
                hide_index=True,
            )

    items = ctx.service.list_review_items(status="pending")
    st.markdown(f"#### 待审核：{len(items)} 条")

    if not items:
        st.info("当前没有待审核项。")
        return

    options = {f"#{i['id']} · {i['title']}": i for i in items}
    label = st.selectbox("选择审核项", list(options))
    item = options[label]

    if item.get("item_type") == "paper_quality":
        paper = ctx.service.get_paper(item["item_id"])
        if paper:
            st.markdown(f"### {paper.get('title') or '未命名文献'}")
            authors = paper.get("authors") or []
            if isinstance(authors, str):
                authors = [authors]
            if authors:
                st.write("作者：", "；".join(str(a) for a in authors))
            st.write("期刊：", paper.get("journal") or "—")
            st.write("年份：", paper.get("year") or "—")
            st.write("DOI：", paper.get("doi") or "—")
            st.write("质量评分：", paper.get("quality_score"), " 等级：", paper.get("quality_level"))
            if paper.get("abstract"):
                with st.expander("摘要"):
                    st.write(paper["abstract"])

    evidence = item.get("evidence") or []
    if evidence:
        st.markdown("#### 证据")
        for e in evidence:
            st.write(f"- {e}")
    else:
        st.info("暂无具体证据。")

    col1, col2, col3 = st.columns(3)
    decision_label = col1.radio("审核决定", list(DECISIONS), horizontal=True)
    feedback = col2.text_area("反馈意见", height=90)
    if col3.button("提交审核结果"):
        ctx.service.decide_review_item(
            item["id"],
            DECISIONS[decision_label],
            feedback.strip(),
        )
        st.success("审核结果已提交。")