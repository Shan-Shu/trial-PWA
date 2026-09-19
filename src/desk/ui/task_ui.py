from __future__ import annotations

from typing import Any, Callable

import streamlit as st


def ensure_started(ctx, task_id: str, func: Callable, *args) -> None:
    flag = f"start_{task_id}"
    if st.session_state.get(flag):
        status = ctx.task_manager.status(task_id)
        if not status or status["status"] not in {"running", "cancelling"}:
            ctx.task_manager.start(task_id, func, *args)
        st.session_state[flag] = False


def task_progress(ctx, task_id: str) -> dict[str, Any] | None:
    status = ctx.task_manager.status(task_id)
    if not status:
        return None

    render_flag = f"task_result_rendered_{task_id}"
    if status["status"] == "done" and not st.session_state.get(render_flag):
        st.session_state[render_flag] = True
        st.rerun()

    @st.fragment(run_every=1.0)
    def _progress_fragment():
        current = ctx.task_manager.status(task_id)
        if not current:
            return
        if current["status"] in {"running", "cancelling"}:
            st.progress(min(1.0, max(0.0, float(current["percent"]) / 100.0)))
            st.caption(f"{current['percent']:.0f}% · {current['message']}")
            if st.button("停止任务", key=f"stop_{task_id}"):
                ctx.task_manager.cancel(task_id)
        elif current["status"] == "done":
            st.progress(1.0)
            st.success("任务完成")
        elif current["status"] == "cancelled":
            st.warning("任务已停止")
        elif current["status"] == "error":
            st.error(f"任务失败：{current.get('error') or '未知错误'}")

    _progress_fragment()
    return ctx.task_manager.status(task_id)