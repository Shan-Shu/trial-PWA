from __future__ import annotations

import pandas as pd
import streamlit as st

from desk.compat import export_markdown, format_bibtex, format_citation
from desk.compat import assess_quality
from desk.ui.components import paper_dataframe


STATUS_LABELS = {
    "全部": None,
    "通过": "quality_passed",
    "待审核": "quality_review",
    "已审核": "quality_reviewed",
    "待复核": "quality_rejected",
    "重复": "duplicate",
    "已采集": "collected",
}

SOURCE_LABELS = {
    "全部": None,
    "PubMed": "pubmed",
    "Crossref": "crossref",
    "OpenAlex": "openalex",
    "arXiv": "arxiv",
}

SORT_LABELS = {
    "质量评分": "quality_score",
    "发表年份": "year",
    "被引次数": "citation_count",
    "标题": "title",
    "入库 ID": "id",
}


def render(ctx) -> None:
    st.subheader("文献库")
    st.caption("文献管理、知识探索、素材调用与状态监控的统一入口。")

    overview = ctx.service.library_overview()
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("文献总数", overview["total"])
    c2.metric("质量通过", overview["passed"])
    c3.metric("待复核", overview["rejected"])
    c4.metric("已知识抽取", overview["knowledge_extracted"])
    c5.metric("本体术语", overview["ontology_terms"])
    c6.metric("本体关系", overview["ontology_relations"])

    st.divider()

    tab_manage, tab_detail, tab_status, tab_batch = st.tabs(
        ["文献管理", "文献详情", "状态监控", "批量操作"]
    )

    filters = _render_filters(ctx)
    rows = ctx.service.library_query(**filters)
    row_by_id = {r["id"]: r for r in rows}

    with tab_manage:
        _render_manage(ctx, rows)

    with tab_detail:
        _render_detail(ctx, rows, row_by_id)

    with tab_status:
        _render_status(ctx, rows, row_by_id)

    with tab_batch:
        _render_batch(ctx, rows, row_by_id)


def _render_filters(ctx) -> dict:
    st.markdown("#### 检索、筛选与排序")
    q = st.text_input("标题 / 摘要 / 期刊搜索", "")

    col1, col2, col3, col4 = st.columns(4)
    status_label = col1.selectbox("质量状态", list(STATUS_LABELS))
    source_label = col2.selectbox("来源", list(SOURCE_LABELS))
    sort_label = col3.selectbox("排序字段", list(SORT_LABELS))
    sort_desc = col4.selectbox("排序方向", ["降序", "升序"]) == "降序"

    tags = ctx.service.list_tags()
    folders = ctx.service.list_folders()
    col5, col6, col7, col8 = st.columns(4)
    tag = col5.selectbox("标签", ["全部"] + tags)
    folder_label = col6.selectbox(
        "文件夹",
        ["全部"] + [f["name"] for f in folders],
    )
    favorite = col7.checkbox("只看收藏")
    year_from = col8.number_input("起始年份", 1900, 2100, 1900)
    year_to = st.number_input("结束年份", 1900, 2100, 2100)

    folder_id = None
    if folder_label != "全部":
        folder_id = next(f["id"] for f in folders if f["name"] == folder_label)

    return {
        "q": q.strip() or None,
        "status": STATUS_LABELS[status_label],
        "source": SOURCE_LABELS[source_label],
        "year_from": int(year_from),
        "year_to": int(year_to),
        "tag": None if tag == "全部" else tag,
        "folder_id": folder_id,
        "favorite": bool(favorite),
        "sort_by": SORT_LABELS[sort_label],
        "sort_desc": bool(sort_desc),
    }


def _render_manage(ctx, rows) -> None:
    st.markdown("#### 文献列表")
    if not rows:
        st.info("没有符合条件的文献。")
        return

    with st.expander("文献列表", expanded=True):
        df = paper_dataframe(rows)
        st.dataframe(df, width="stretch", hide_index=True)
        st.caption(f"当前显示 {len(rows)} 篇文献")


def _paper_label(row) -> str:
    title = row.get("title") or "未命名文献"
    return f"#{row.get('id')} · {title}"


def _render_detail(ctx, rows, row_by_id) -> None:
    if not rows:
        st.info("没有可查看的文献。")
        return

    label = st.selectbox("选择文献", [_paper_label(r) for r in rows])
    paper = next(r for r in rows if _paper_label(r) == label)
    paper_id = paper["id"]

    _render_paper_header(paper)
    _render_paper_actions(ctx, paper_id)

    sub_tab_summary, sub_tab_quality, sub_tab_knowledge, sub_tab_material = st.tabs(
        ["摘要", "质量报告", "知识标注", "引用与素材"]
    )

    with sub_tab_summary:
        if paper.get("abstract"):
            st.write(paper["abstract"])
        else:
            st.info("该文献暂无摘要。")

    with sub_tab_quality:
        _render_quality_report(ctx, paper)

    with sub_tab_knowledge:
        _render_knowledge_panel(ctx, paper_id)

    with sub_tab_material:
        _render_material_panel(paper)


def _render_paper_header(paper) -> None:
    st.markdown(f"### {paper.get('title') or '未命名文献'}")
    authors = paper.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    if authors:
        st.write("**作者**：", "；".join(str(a) for a in authors))
    st.write("**期刊/来源**：", paper.get("journal") or paper.get("source") or "—")
    st.write(
        "**年份**：", paper.get("year") or "—",
        "  **DOI**：", paper.get("doi") or "—",
    )
    if paper.get("citation_count") is not None:
        st.write("**被引**：", paper.get("citation_count"))
    st.write("**质量评分**：", paper.get("quality_score"), "  **等级**：", paper.get("quality_level"))
    st.write("**标签**：", "、".join(paper.get("tags") or []) or "无")


def _render_paper_actions(ctx, paper_id: int) -> None:
    paper = ctx.service.get_paper(paper_id)
    tags = ctx.service.list_tags()
    folders = ctx.service.list_folders()

    c1, c2, c3, c4 = st.columns(4)
    new_tag = c1.text_input("添加标签", key=f"tag_{paper_id}")
    if c1.button("添加标签", key=f"add_tag_{paper_id}"):
        if new_tag.strip():
            ctx.service.add_tag(paper_id, new_tag.strip())
            st.success("标签已添加。")
    folder_name = c2.selectbox(
        "选择文件夹",
        ["—"] + [f["name"] for f in folders],
        key=f"folder_{paper_id}",
    )
    if c2.button("加入文件夹", key=f"folder_add_{paper_id}"):
        if folder_name != "—":
            folder_id = next(f["id"] for f in folders if f["name"] == folder_name)
            ctx.service.add_to_folder(paper_id, folder_id)
            st.success("已加入文件夹。")
    is_fav = bool(paper.get("favorite")) if paper is not None else False
    if c3.button("取消收藏" if is_fav else "收藏", key=f"fav_{paper_id}"):
        ctx.service.set_favorite(paper_id, not is_fav)
        st.success("已更新收藏状态。")
    if c4.button("删除该文献", key=f"delete_{paper_id}"):
        ctx.service.delete_paper(paper_id)
        st.success("已删除，请刷新页面。")


def _render_quality_report(ctx, paper) -> None:
    score = paper.get("quality_score")
    level = paper.get("quality_level") or "—"
    if score is None:
        st.info("该文献尚未完成质量评估。")
        return

    st.write(f"**综合得分：{score}（{level}级）**")
    st.progress(min(1.0, max(0.0, float(score) / 100.0)))

    record = {
        "title": paper.get("title"),
        "abstract": paper.get("abstract"),
        "venue": paper.get("journal"),
        "pub_year": paper.get("year"),
        "citation_count": paper.get("citation_count"),
        "authors": paper.get("authors"),
        "doi": paper.get("doi"),
    }
    try:
        dims = assess_quality(record, ctx.settings)
    except Exception:
        dims = {}
    dims_map = [
        ("权威性", dims.get("authority", 0.0)),
        ("完整性", dims.get("completeness", 0.0)),
        ("时效性", dims.get("timeliness", 0.0)),
        ("一致性", dims.get("consistency", 0.0)),
        ("唯一性", dims.get("uniqueness", 0.0)),
        ("可追溯性", dims.get("provenance", 0.0)),
    ]

    st.markdown("#### 维度得分")
    for label, value in dims_map:
        st.write(f"{label}：{value:.2f}")
        st.progress(value)

    reasons = paper.get("quality_reasons") or []
    if reasons:
        st.markdown("#### 说明")
        for reason in reasons:
            st.write(f"- {reason}")


def _render_knowledge_panel(ctx, paper_id: int) -> None:
    data = ctx.service.knowledge_data(paper_id)
    if not data["sdo"] and not data["entities"]:
        st.info("该文献尚未进行知识抽取。可前往“知识抽取”页执行。")
        return

    c1, c2 = st.columns(2)
    c1.metric("实体", data["stats"]["entities"])
    c2.metric("三元组", data["stats"]["triples"])

    if data["sdo"]:
        with st.expander("标准化文献对象（SDO）"):
            st.json(data["sdo"])

    if data["entities"]:
        st.markdown("#### 实体")
        rows = [
            {
                "名称": e.get("name"),
                "规范名称": e.get("canonical_name"),
                "类型": e.get("entity_type"),
                "置信度": e.get("confidence"),
            }
            for e in data["entities"]
        ]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    if data["triples"]:
        st.markdown("#### 关系 / 属性 / 事件")
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


def _render_material_panel(paper) -> None:
    st.markdown("#### 引用")
    st.code(format_citation(paper), language=None)
    st.markdown("#### BibTeX")
    st.code(format_bibtex(paper), language="bibtex")
    st.markdown("#### Markdown 导出")
    content = export_markdown([paper])
    st.download_button(
        "下载 Markdown",
        data=content.encode("utf-8"),
        file_name=f"paper_{paper.get('id')}.md",
        mime="text/markdown",
    )


def _render_status(ctx, rows, row_by_id) -> None:
    st.markdown("#### 系统处理状态")
    if not rows:
        st.info("没有符合条件的文献。")
        return

    data_rows = []
    for row in rows[:200]:
        pid = row["id"]
        processing = ctx.service.paper_processing(pid)
        data_rows.append(
            {
                "ID": pid,
                "标题": row.get("title") or "未命名文献",
                "质量评分": row.get("quality_score"),
                "质量状态": _status_label(row.get("status") or "collected"),
                "知识抽取": "已抽取" if row.get("has_knowledge") else "未抽取",
                "实体数": processing["entity_count"] if processing else 0,
                "三元组数": processing["triple_count"] if processing else 0,
            }
        )
    st.dataframe(pd.DataFrame(data_rows), width="stretch", hide_index=True)


def _status_label(status: str) -> str:
    return {
        "collected": "已采集",
        "quality_passed": "质量通过",
        "quality_rejected": "待复核",
        "duplicate": "重复",
    }.get(status, status)


def _render_batch(ctx, rows, row_by_id) -> None:
    st.markdown("#### 批量操作")
    if not rows:
        st.info("没有可批量操作的文献。")
        return

    options = {_paper_label(r): r["id"] for r in rows}
    selected_labels = st.multiselect("批量选择文献", list(options))
    selected_ids = [options[label] for label in selected_labels]

    if not selected_ids:
        st.warning("请先在上方选择至少一篇文献。")
        return

    st.write(f"已选择 {len(selected_ids)} 篇文献。")

    c1, c2, c3, c4 = st.columns(4)
    if c1.button("批量知识抽取", type="primary"):
        with st.spinner("批量知识抽取中..."):
            result = ctx.service.batch_extract_knowledge(selected_ids)
            st.success(f"完成：成功 {result['ok']} / 共 {result['total']}。")
    if c2.button("批量收藏"):
        result = ctx.service.batch_favorite(selected_ids, True)
        st.success(f"已收藏 {result['updated']} 篇。")
    if c3.button("取消收藏"):
        result = ctx.service.batch_favorite(selected_ids, False)
        st.success(f"已取消收藏 {result['updated']} 篇。")

    tag = st.text_input("批量添加标签", key="batch_tag_input")
    if st.button("批量添加标签"):
        if tag.strip():
            result = ctx.service.batch_tag(selected_ids, tag.strip())
            st.success(f"已为 {result['updated']} 篇添加标签。")

    folders = ctx.service.list_folders()
    if folders:
        folder_name = st.selectbox("批量加入文件夹", [f["name"] for f in folders])
        if st.button("批量加入文件夹"):
            folder_id = next(f["id"] for f in folders if f["name"] == folder_name)
            result = ctx.service.batch_add_to_folder(selected_ids, folder_id)
            st.success(f"已移动 {result['updated']} 篇到文件夹。")
    else:
        st.caption("还没有文件夹，可在详情页或后续版本中创建。")

    if st.button("导出 Markdown"):
        export = ctx.service.export_papers(selected_ids, "markdown")
        st.download_button(
            "下载 Markdown 导出",
            data=export["content"].encode("utf-8"),
            file_name="selected_papers.md",
            mime="text/markdown",
        )
    if st.button("导出 BibTeX"):
        export = ctx.service.export_papers(selected_ids, "bibtex")
        st.download_button(
            "下载 BibTeX 导出",
            data=export["content"].encode("utf-8"),
            file_name="selected_papers.bib",
            mime="text/plain",
        )

    confirm = st.checkbox("我确认要删除选中的文献")
    if confirm and st.button("批量删除", type="secondary"):
        result = ctx.service.batch_delete_papers(selected_ids)
        st.success(f"已删除 {result['deleted']} 篇，请刷新页面。")