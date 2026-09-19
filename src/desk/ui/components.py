from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st


def parse_json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return default


def metric_card(label: str, value: Any, help: str = "") -> None:
    st.metric(label=label, value=value, help=help)


def quality_badge(status: str) -> str:
    mapping = {
        "quality_passed": "✅ 通过",
        "quality_review": "🟡 待审核",
        "quality_reviewed": "🔵 已审核",
        "quality_rejected": "⚠️ 待复核",
        "duplicate": "♻️ 重复",
        "collected": "📥 已采集",
    }
    return mapping.get(status, status)


def paper_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()

    data = []
    for row in rows:
        authors = row.get("authors") or []
        if isinstance(authors, str):
            authors = parse_json(authors, [])
        metadata = row.get("metadata") or {}
        relevance = metadata.get("relevance_score")
        matched = metadata.get("matched_fields") or []
        data.append(
            {
                "ID": row.get("id"),
                "标题": row.get("title") or "",
                "年份": row.get("year") or "",
                "来源": row.get("source") or "",
                "期刊": row.get("journal") or "",
                "引用": row.get("citation_count") or 0,
                "相关性": relevance if relevance is not None else "",
                "命中字段": "、".join(matched) if matched else "",
                "评分": row.get("quality_score") if row.get("quality_score") is not None else "",
                "等级": row.get("quality_level") or "",
                "状态": quality_badge(row.get("status") or "collected"),
            }
        )
    return pd.DataFrame(data)


def paper_detail(row: dict[str, Any]) -> None:
    authors = row.get("authors") or []
    if isinstance(authors, str):
        authors = parse_json(authors, [])
    reasons = row.get("quality_reasons") or []
    if isinstance(reasons, str):
        reasons = parse_json(reasons, [])

    st.markdown(f"### {row.get('title') or '未命名文献'}")
    st.caption(
        f"来源：{row.get('source') or '-'}  |  期刊：{row.get('journal') or '-'}  |  "
        f"年份：{row.get('year') or '-'}  |  引用：{row.get('citation_count') or 0}"
    )
    if row.get("doi"):
        st.write("DOI:", row["doi"])
    if row.get("url"):
        st.write("链接:", row["url"])
    if authors:
        st.write("作者:", ", ".join(str(a) for a in authors))
    if row.get("abstract"):
        with st.expander("摘要"):
            st.write(row["abstract"])
    if reasons:
        st.write("质量理由:", "；".join(str(r) for r in reasons))


def render_paper_list(rows: list[dict[str, Any]]) -> None:
    if not rows:
        st.info("暂无文献。")
        return

    df = paper_dataframe(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    options = {f"#{r.get('id')} · {r.get('title') or '未命名文献'}": r for r in rows}
    selected_label = st.selectbox("选择文献查看详情", list(options))
    if selected_label:
        paper_detail(options[selected_label])

