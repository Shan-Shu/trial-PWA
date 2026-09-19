from __future__ import annotations

import streamlit as st


def render(ctx) -> None:
    st.subheader("写作台")
    st.caption("创建写作项目、生成大纲、调用推荐素材并生成章节草稿。")

    with st.form("create_writing_project", clear_on_submit=False):
        title = st.text_input("论文标题")
        topic = st.text_input("研究主题", placeholder="例如：糖尿病分子机制")
        if st.form_submit_button("创建写作项目"):
            if title.strip():
                project_id = ctx.service.create_writing_project(title.strip(), topic.strip())
                st.session_state["writing_project_id"] = project_id
                st.success("写作项目已创建。")

    projects = ctx.service.list_writing_projects()
    if not projects:
        st.info("还没有写作项目。")
        return

    options = {f"#{p['id']} · {p['title']}": p for p in projects}
    label = st.selectbox("选择写作项目", list(options))
    project = options[label]
    project_id = int(project["id"])

    topic = st.text_input("当前研究主题", value=project.get("topic") or project.get("title"), key=f"topic_{project_id}")
    if st.button("重新生成大纲", key=f"outline_{project_id}"):
        ctx.service.generate_outline(project_id, topic.strip())
        st.success("大纲已生成。")

    project = ctx.service.get_writing_project(project_id)
    outline = project.get("outline") or []

    materials = ctx.service.recommend_materials(topic.strip(), limit=10)
    st.markdown("### 推荐素材")
    if materials:
        for m in materials:
            title = m.get("title") or "未命名素材"
            st.write(f"- [{m.get('type')}] {title}")
    else:
        st.info("当前没有足够知识用于推荐素材。")

    if outline:
        st.markdown("### 论文大纲")
        section_labels = [f"{s.get('heading')}" for s in outline]
        selected_label = st.selectbox("选择章节", section_labels)
        section = next(s for s in outline if s.get("heading") == selected_label)
        section_key = section.get("key") or selected_label

        if st.button("生成此节草稿", key=f"gen_{project_id}_{section_key}"):
            with st.spinner("生成章节草稿..."):
                result = ctx.service.generate_section(
                    project_id,
                    section_key,
                    selected_label,
                    topic.strip(),
                    materials,
                )
                st.success("章节草稿已生成。")

        sections = ctx.service.list_writing_sections(project_id)
        current = next((s for s in sections if s.get("section_key") == section_key), None)
        if current:
            st.markdown(f"#### {current.get('heading')}")
            content = st.text_area(
                "章节内容",
                value=current.get("content") or "",
                height=280,
                key=f"section_{project_id}_{section_key}",
            )
            c1, c2 = st.columns(2)
            if c1.button("保存修改", key=f"save_{project_id}_{section_key}"):
                ctx.service.polish_section(project_id, section_key, content)
                st.success("已保存。")
            if c2.button("润色当前章节", key=f"polish_{project_id}_{section_key}"):
                polished = ctx.service.polish_section(project_id, section_key, content)
                st.success("润色完成，可重新查看。")