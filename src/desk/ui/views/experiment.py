from __future__ import annotations

import pandas as pd
import streamlit as st

from desk.compat import format_experiment_design
from desk.ui.task_ui import ensure_started, task_progress


def render(ctx) -> None:
    st.subheader("实验设计工作台")
    st.caption("聚合已抽取的实验知识，并根据研究目标生成结构化实验设计建议。")

    if st.button("聚合实验知识"):
        st.session_state["start_experiment_aggregate_task"] = True
    if st.session_state.get("start_experiment_aggregate_task"):
        ensure_started(
            ctx,
            "experiment_aggregate_task",
            ctx.service.aggregate_experiments,
        )
    agg_status = task_progress(ctx, "experiment_aggregate_task")
    if agg_status and agg_status["status"] == "done" and agg_status.get("result"):
        result = agg_status["result"]
        st.success(f"已聚合 {result['experiments']} 条实验知识。")

    with st.form("experiment_design_form"):
        goal = st.text_area(
            "实验目标",
            placeholder="例如：评估某药物对糖尿病小鼠血糖和胰岛素敏感性的影响",
            height=100,
        )
        if st.form_submit_button("生成实验设计建议"):
            if goal.strip():
                st.session_state["experiment_design_goal"] = goal.strip()
                st.session_state["start_experiment_design_task"] = True

    if st.session_state.get("start_experiment_design_task"):
        goal = st.session_state.get("experiment_design_goal")
        if goal:
            ensure_started(
                ctx,
                "experiment_design_task",
                ctx.service.generate_experiment_design,
                goal,
            )
    design_status = task_progress(ctx, "experiment_design_task")
    if design_status and design_status["status"] == "done" and design_status.get("result"):
        st.success("实验设计建议已生成。")

    with st.expander("实验知识库", expanded=False):
        knowledge = ctx.service.list_experiment_knowledge(limit=200)
        if knowledge:
            rows = [
                {
                    "实验名称": k.get("experiment_name"),
                    "对象": k.get("object_text"),
                    "方法数": len(k.get("methods") or []),
                    "参数数": len(k.get("parameters") or {}),
                    "置信度": k.get("confidence"),
                }
                for k in knowledge
            ]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("暂无实验知识，请先完成知识抽取和实验知识聚合。")

    with st.expander("实验设计建议", expanded=False):
        designs = ctx.service.list_experiment_designs()
        if not designs:
            st.info("暂无实验设计建议。")
        else:
            options = {f"#{d['id']} · {d['goal'][:60]}": d for d in designs}
            selected = st.selectbox("选择设计建议", list(options))
            design = options[selected]["design"]
            st.markdown(format_experiment_design(design))
            with st.expander("查看结构化 JSON"):
                st.json(design)