"""写作台：**工作规划节点的唯一交互界面**。

形态是访谈闭环，不是"选章节 → 生成 → 润色"（那是 PWA 原版的 80 行薄壳）：

    选/建项目 → 一轮前置问答（体裁 → 主题 → 要写哪些部分）
      → 逐部分闭环：拟 3 个方案 → 你选一个 → 判定支撑够不够
        → 不够就问你怎么处理（保留缺口 / 补检 / 自定义任务）
        → 协作补齐 → 写作这部分 → 下一部分

职责边界（引擎侧定好，界面只透传）：

- **工作规划节点**负责规划与协作——问答、判定、组织补齐、**把需求下发成派工单**；
- **写作**由知识消费节点与内容形成节点完成；
- 「对节点下指令」面板让用户用自然语言**直接对规划节点下需求**，
  规划节点解析成"对哪些节点下什么单"（3 个方案供选），再由派工执行器分发。

**进度可见**：作业驱动阶段用 `st.fragment(run_every=1.0)` 局部轮询，
显示百分比、当前消息与「停止这一步」；完成后自动续跑到下一步。
连续三次没有进展就停下并说明——否则会一直烧配额。
"""
from __future__ import annotations

from typing import Any

import streamlit as st

#: 阶段 → 中文（引擎 `interview.STAGE_LABEL` 是权威来源，这里只兜底）
_STAGE_FALLBACK = {
    "pending": "待访谈", "drafting_options": "正在拟方案",
    "awaiting_choice": "等你选方案", "judging": "正在判定支撑",
    "awaiting_gap_decision": "等你决定缺口",
    "awaiting_custom_plan_choice": "等你确认任务",
    "collaborating": "正在补齐支撑", "writing": "正在写作",
    "done": "已完成", "failed_writable": "写作失败",
}

#: 判定维度 → 中文
_DIM_LABELS = {
    "papers": "命中文献", "knowledge": "知识覆盖", "conditions": "量化条件",
    "comparison": "可比证据", "requirements": "指令要求", "evidence": "可用证据",
}

#: 连续多少次"同一步无进展"就停止自动推进
_MAX_STALL = 3


# ====================================================================== 入口
def render(ctx) -> None:
    st.subheader("写作台")

    # 两页结构：**项目列表页 → 项目详情页**。
    #
    # 此前只有一页，且"选中项目"是在折叠区里用一个下拉**隐式**完成的：用户既
    # 看不到进入某个项目的入口，也看不出当前在哪个项目里；更糟的是选中后
    # `_advance` 会立刻自动起作业并 return，页面主体被进度条顶掉——实测真实库上
    # 渲染出来只有「两个折叠区 + 0% 排队中」，对话/部分/指令区一片空白。
    pid = st.session_state.get("desk_pid")
    view = st.session_state.get("desk_view") or ("detail" if pid else "list")
    if view == "detail" and not pid:
        view = "list"
    st.session_state["desk_view"] = view

    if view == "list":
        _render_project_list(ctx)
    else:
        _render_project_detail(ctx, pid)


def _enter(pid: Any) -> None:
    """进入某个项目的详情页。"""
    st.session_state["desk_pid"] = pid
    st.session_state["desk_view"] = "detail"
    st.rerun()


def _back_to_list() -> None:
    st.session_state["desk_view"] = "list"
    st.rerun()


# ================================================================ 项目列表页
def _render_project_list(ctx) -> None:
    st.caption("整篇由一个**工作规划节点**通过一轮问答定下来：先定体裁与要写的"
               "部分，再逐部分拟 3 个方案；选定后先判定支撑——不够就问你怎么处理。"
               "点某个项目的「打开」进入它的写作页。")
    projects = ctx.service.list_writing_projects()

    if not projects:
        st.info("还没有写作项目。在下面建一个即可开始。")
    else:
        st.markdown(f"##### 项目列表（{len(projects)} 个）")
        head = st.columns([4, 2, 1, 2, 1])
        for col, text in zip(head, ("标题 / 主题", "体裁", "已写/总节",
                                    "更新时间", "")):
            col.caption(f"**{text}**")
        for project in projects:
            title_col, genre_col, prog_col, time_col, act_col = st.columns(
                [4, 2, 1, 2, 1])
            title_col.markdown(
                f"**#{project['id']} · {project['title'] or '（无标题）'}**")
            topic = str(project.get("topic") or "").strip()
            if topic:
                title_col.caption(topic[:44])
            genre_col.caption(project.get("genre") or "—")
            filled = int(project.get("filled_sections") or 0)
            total = int(project.get("total_sections") or 0)
            prog_col.caption(f"{filled}/{total}")
            time_col.caption(str(project.get("updated_at")
                                 or project.get("created_at") or "")[:19])
            if act_col.button("打开", key=f"desk_open_{project['id']}"):
                _enter(project["id"])

    st.divider()
    st.markdown("##### 新建写作项目")
    c1, c2 = st.columns(2)
    title = c1.text_input("新项目标题", key="desk_new_title")
    topic = c2.text_input("研究主题", key="desk_new_topic")
    if st.button("创建并开始访谈", key="desk_create"):
        if not title.strip():
            st.warning("标题不能为空")
        else:
            new_id = ctx.service.create_writing_project(title.strip(),
                                                        topic.strip())
            ctx.service.interview_start(new_id)
            _enter(new_id)

    st.divider()
    _project_manager(ctx)


# ================================================================ 项目详情页
def _render_project_detail(ctx, pid: Any) -> None:
    projects = ctx.service.list_writing_projects()
    project = next((p for p in projects if p["id"] == pid), None)
    if project is None:
        # 项目刚被删掉（可能在管理区删的）：回列表，别停在一个空壳上
        st.warning("这个项目已不存在（可能刚被删除），已回到项目列表。")
        st.session_state.pop("desk_pid", None)
        _back_to_list()
        return

    if st.button("← 全部项目", key="desk_back_to_list"):
        _back_to_list()

    # **抬头必须有**：任何时候都要能看出"我在哪个项目里"。以前没有这一行，
    # 自动推进时页面只剩一个进度条，看起来就像项目页消失了。
    filled = int(project.get("filled_sections") or 0)
    total = int(project.get("total_sections") or 0)
    st.markdown(f"#### #{project['id']} · {project['title'] or '（无标题）'}")
    st.caption(f"体裁 {project.get('genre') or '—'}"
               f" · 主题 {project.get('topic') or '（未填）'}"
               f" · 已写 {filled}/{total} 节"
               f" · 更新 {str(project.get('updated_at') or project.get('created_at') or '')[:19]}")

    snap = ctx.service.interview_snapshot(pid)
    if not snap.get("ok"):
        st.error(f"访谈状态读取失败：{snap.get('error') or '未知原因'}")
        return

    # 有未完成的步骤就自动推进（作业驱动）。
    #
    # **但不因此把详情页整块顶掉**：以前这里是 `return`，于是"进入项目"看到的
    # 只有进度条，对话/部分/指令区一片空白——看起来就像项目页消失了。
    # 现在进度与内容并存：推进时多一行说明，下面的内容照常可看。
    advancing = _advance(ctx, pid, snap)
    if advancing:
        st.caption("这一步正在推进，完成后会自动刷新；下面的内容照常可以查看。")

    left, right = st.columns([3, 2])
    with left:
        _conversation(ctx, pid, snap)
    with right:
        _sections(ctx, pid, snap)
        _directive(ctx, pid, snap)


# ================================================================== 项目管理
def _blocked_by_running_job(chosen_ids: list[Any], running_pid: Any
                            ) -> tuple[list[Any], Any]:
    """从待删列表里摘掉"正在跑写作作业"的项目。

    不摘的后果很具体：作业跑到落库那一步会往 `writing_sections` 写一行
    `project_id` 指向**已删项目**的记录，撞外键约束直接失败——用户看到的是
    "写作失败"，而不是"你删早了"。所以宁可拒绝并说清楚。

    返回 ``(可删列表, 被挡下的 id 或 None)``。抽成纯函数是为了能被确定性地
    单测：在界面里伪造一个在跑的作业会牵动 `st.rerun`，测不稳。
    """
    if running_pid is None:
        return list(chosen_ids), None
    blocked = running_pid if running_pid in chosen_ids else None
    return [i for i in chosen_ids if i != running_pid], blocked


def _project_manager(ctx) -> None:
    """项目管理：看概况 → 重命名 → 删除（单个 / 批量，二次确认）。

    为什么要"概况"：实测真实库 27 个项目里只有 1 个写过正文，其余是历史冒烟残留
    （`aaa`、`浏览器冒烟`、`probe-*`）。只给标题根本分不出哪条能删——所以这里把
    **已写节数 / 更新时间**摆出来，让"不用的项目"一眼可辨。
    """
    projects = ctx.service.list_writing_projects()
    with st.expander(f"项目管理（重命名 / 删除）· 共 {len(projects)} 个项目",
                     expanded=False):
        if not projects:
            st.caption("还没有写作项目。")
            return

        by_id = {p["id"]: p for p in projects}
        st.dataframe(
            [{
                "项目": f"#{p['id']}",
                "标题": p["title"] or "（无标题）",
                "体裁": p["genre"] or "",
                "已写/总节": f"{p.get('filled_sections') or 0}/"
                             f"{p.get('total_sections') or 0}",
                "主题": (p.get("topic") or "")[:40],
                "更新时间": str(p.get("updated_at")
                                or p.get("created_at") or "")[:19],
            } for p in projects],
            width="stretch", hide_index=True,
        )
        st.caption("「已写/总节」为 0/N 的项目通常是创建后没写过的，可优先清理。")

        # ---------------------------------------------------------- 重命名
        st.markdown("##### 重命名")
        labels = {f"#{p['id']} · {p['title'] or '（无标题）'}": p["id"]
                  for p in projects}
        picked = st.selectbox("要改的项目", list(labels), key="desk_pm_rename_pick")
        target = by_id.get(labels[picked]) or {}
        # key 里带 pid：换项目时拿到与该项目绑定的输入框，默认值才是它自己的现值
        c1, c2 = st.columns(2)
        new_title = c1.text_input("标题", value=str(target.get("title") or ""),
                                  key=f"desk_pm_title_{target.get('id')}")
        new_topic = c2.text_input("主题", value=str(target.get("topic") or ""),
                                  key=f"desk_pm_topic_{target.get('id')}")
        if st.button("保存修改", key="desk_pm_rename"):
            if not str(new_title).strip():
                st.warning("标题不能为空")
            else:
                ctx.service.rename_writing_project(
                    target.get("id"), title=new_title, topic=new_topic)
                st.success(f"已更新 #{target.get('id')}")
                st.rerun()

        # ---------------------------------------------------------- 删除
        st.markdown("##### 删除")
        victims = st.multiselect(
            "要删除的项目（可多选）", list(labels), key="desk_pm_victims")
        chosen_ids = [labels[v] for v in victims]
        if chosen_ids:
            picked_projects = [by_id[i] for i in chosen_ids if i in by_id]
            lost_sections = sum(int(p.get("total_sections") or 0)
                                for p in picked_projects)
            lost_written = sum(int(p.get("filled_sections") or 0)
                               for p in picked_projects)
            st.warning(
                f"将删除 {len(chosen_ids)} 个项目，连同其大纲 {lost_sections} 节"
                f"（其中已写正文 {lost_written} 节）、决策轨迹与派工单记录。"
                "**此操作不可恢复**。")
            if lost_written:
                st.error(f"注意：所选项目里有 {lost_written} 节**已写好的正文**"
                         "会被一并删除。如要留档，先到导出功能存一份。")

        # 正在跑的作业属于哪个项目：删它会让作业写到一半撞外键失败，先说清楚
        running = (st.session_state.get("desk_pid")
                   if st.session_state.get("desk_job") else None)
        chosen_ids, blocked = _blocked_by_running_job(chosen_ids, running)
        if blocked is not None:
            st.error(f"#{blocked} 有正在跑的写作作业：请先「停止这一步」"
                     "或等它结束，再删除该项目。")

        confirmed = st.checkbox("我确认删除以上项目（不可恢复）",
                                key="desk_pm_confirm")
        if st.button("删除选中项目", key="desk_pm_delete",
                     disabled=not (chosen_ids and confirmed)):
            result = ctx.service.batch_delete_writing_projects(chosen_ids)
            removed = result.get("removed") or {}
            st.success(
                f"已删除 {len(result.get('deleted') or [])} 个项目："
                f"大纲 {removed.get('sections', 0)} 节、"
                f"决策轨迹 {removed.get('runs', 0)} 行、"
                f"派工单 {removed.get('dispatches', 0)} 条。")
            if result.get("missing"):
                st.info(f"另有 {len(result['missing'])} 个 id 已不存在，跳过。")
            # 删掉的正好是当前打开的项目：清掉选中态并回列表，否则详情页会
            # 指向一个已不存在的 id（或者停在空壳上）
            if st.session_state.get("desk_pid") in set(chosen_ids):
                st.session_state.pop("desk_pid", None)
                st.session_state["desk_view"] = "list"
            st.session_state.pop("desk_pm_victims", None)
            st.rerun()


# ====================================================================== 推进
def _stage_of(snap: dict[str, Any]) -> str:
    question = snap.get("question") or {}
    if question.get("stage"):
        return str(question["stage"])
    key = question.get("section_key") or ""
    for sec in snap.get("sections") or []:
        if sec.get("section_key") == key:
            return str(sec.get("stage") or "")
    return ""


def _advance(ctx, pid: Any, snap: dict[str, Any]) -> bool:
    """有待执行步骤就跑一步并显示进度。返回 True 表示本次已处理完毕。"""
    job_id = st.session_state.get("desk_job")
    if job_id:
        _poll_job(ctx, pid, job_id)
        return True

    if not snap.get("next_action"):
        return False

    # 同一步反复无进展就停：否则会一直烧检索/模型配额
    signature = f"{snap.get('next_action')}:{_stage_of(snap)}"
    if signature == st.session_state.get("desk_sig"):
        st.session_state["desk_stall"] = st.session_state.get("desk_stall", 0) + 1
    else:
        st.session_state["desk_sig"] = signature
        st.session_state["desk_stall"] = 0
    if st.session_state.get("desk_stall", 0) >= _MAX_STALL:
        st.warning("这一步没有产生新的进展，已停止自动推进——继续重试只会消耗配额。")
        st.caption("可以换个部分、改用「保留缺口照常撰写」，"
                   "或用右侧「对节点下指令」换个口径。")
        if st.button("再试一次", key="desk_retry"):
            st.session_state["desk_stall"] = 0
            st.rerun()
        return True

    result = ctx.service.interview_step(pid)
    if not result.get("ok"):
        st.error(f"推进失败：{result.get('error') or '未知原因'}")
        return True
    if result.get("job_id"):
        st.session_state["desk_job"] = result["job_id"]
    st.rerun(scope="app")
    return True


def _poll_job(ctx, pid: Any, job_id: str) -> None:
    """局部轮询作业：只刷新进度区，不重跑整页。"""

    @st.fragment(run_every=1.0)
    def _progress() -> None:
        status = ctx.service.section_job_status(job_id)
        if not status.get("ok"):
            st.session_state.pop("desk_job", None)
            st.rerun(scope="app")
            return
        state = str(status.get("status") or "")
        percent = float(status.get("percent") or 0.0)
        message = str(status.get("message") or "")
        if state in ("running", "cancelling"):
            st.progress(min(1.0, max(0.0, percent / 100.0)))
            st.caption(f"{percent:.0f}% · {message or '处理中…'}")
            if st.button("停止这一步", key=f"desk_stop_{job_id}"):
                ctx.service.cancel_section_job(job_id)
                st.session_state.pop("desk_job", None)
                st.rerun(scope="app")
            return
        # 终态：清掉作业并整页重跑，让快照刷新
        st.session_state.pop("desk_job", None)
        if state == "error":
            st.error(f"这一步失败：{status.get('error') or '未知错误'}")
        st.rerun(scope="app")

    _progress()


# ====================================================================== 对话
def _conversation(ctx, pid: Any, snap: dict[str, Any]) -> None:
    history = snap.get("history") or []
    with st.container(border=True):
        st.markdown("#### 工作规划节点")
        if not history:
            st.caption("还没有对话。")
        for item in history[-40:]:
            mine = item.get("role") == "user"
            with st.chat_message("user" if mine else "assistant"):
                st.markdown(f"**{'你' if mine else 'AI'}**：{item.get('text') or ''}")
        st.divider()
        _question(ctx, pid, snap)


def _question(ctx, pid: Any, snap: dict[str, Any]) -> None:
    """随题型切换的输入区。"""
    if snap.get("finished"):
        st.success("所有选中的部分都已完成。可在右侧查看正文与决策轨迹。")
        return

    question = snap.get("question") or {}
    kind = str(question.get("kind") or "")

    if kind == "intake":
        _intake(ctx, pid, question)
    elif kind == "section_choice":
        _section_choice(ctx, pid, question)
    elif kind == "gap_decision":
        _gap_decision(ctx, pid, question)
    elif kind == "custom_plan_choice":
        _custom_plan(ctx, pid, question)
    elif kind == "working":
        st.info(f"{question.get('label') or '正在处理'}…"
                f"（「{question.get('heading') or ''}」由后端作业执行，无需作答）")
    else:
        st.caption("当前没有需要回答的问题。")


def _answer(ctx, pid: Any, payload: dict[str, Any]) -> None:
    result = ctx.service.interview_answer(pid, payload)
    if not result.get("ok"):
        st.error(f"作答失败：{result.get('error') or '未知原因'}")
        return
    st.rerun(scope="app")


# ---------------------------------------------------------------- 前置三问
def _intake(ctx, pid: Any, question: dict[str, Any]) -> None:
    step = str(question.get("step") or "")
    st.markdown(f"**{question.get('question') or ''}**")

    if step == "genre":
        options = question.get("options") or []
        cols = st.columns(2)
        for index, option in enumerate(options):
            label = option.get("label") or option.get("id")
            suffix = "（建议）" if option.get("suggested") else ""
            if cols[index % 2].button(
                    f"{label}{suffix} · {option.get('sections')} 节",
                    key=f"desk_genre_{option.get('id')}",
                    width="stretch"):
                _answer(ctx, pid, {"kind": "intake", "step": "genre",
                                   "value": option.get("id")})
        return

    if step == "topic":
        spec = question.get("input") or {}
        topic = st.text_input("主题", value=spec.get("preset") or "",
                              placeholder=spec.get("placeholder") or "",
                              key="desk_topic")
        if st.button("就用这个主题", key="desk_topic_ok"):
            if not topic.strip():
                st.warning("主题不能为空")
            else:
                _answer(ctx, pid, {"kind": "intake", "step": "topic",
                                   "value": topic.strip()})
        return

    # sections：勾选要写的部分
    items = question.get("items") or []
    if question.get("note"):
        st.caption(question["note"])
    selectable = [i for i in items if i.get("generates_body")]
    if not selectable:
        # 该体裁没有任何部分挂了模板 ⇒ 一个都写不了。直说原因并给出出路，
        # 否则用户只看到一排灰掉的勾选框，不知道该怎么办。
        st.warning("这个体裁下**没有任何部分配了模板**，因此现在什么都写不了。"
                   "请回到上一步换一个体裁——「文献综述」与「实验方案」"
                   "两套模板是齐的。")
        if st.button("重新选择体裁", key="desk_reselect_genre"):
            # 保留已填主题：reset 会清进度，不该顺带让用户重打一遍主题
            topic = str((_snapshot(ctx, pid).get("intake") or {}).get("topic") or "")
            ctx.service.interview_start(pid, reset=True, topic=topic)
            st.rerun(scope="app")
        return
    chosen: list[str] = []
    for item in items:
        disabled = not item.get("generates_body")
        picked = st.checkbox(
            f"{item.get('heading')}"
            + (f"（{item.get('words')} 字）" if item.get("words") else "")
            + ("" if item.get("generates_body") else " — 本体裁无模板，不生成正文"),
            value=bool(item.get("selected")) and not disabled,
            disabled=disabled, key=f"desk_sec_{item.get('key')}")
        if picked:
            chosen.append(str(item.get("key")))
    if st.button("开始逐部分访谈", key="desk_sections_ok"):
        if not chosen:
            st.warning("至少要选一个部分")
        else:
            _answer(ctx, pid, {"kind": "intake", "step": "sections",
                               "value": chosen})


def _snapshot(ctx, pid: Any) -> dict[str, Any]:
    """读快照（`_intake` 里重挑体裁时要用，避免跨函数引用局部变量）。"""
    return ctx.service.interview_snapshot(pid) or {}


# ---------------------------------------------------------------- 三选一
def _section_choice(ctx, pid: Any, question: dict[str, Any]) -> None:
    st.markdown(f"**「{question.get('heading')}」要写什么内容？**")
    options = [o for o in (question.get("options") or []) if o.get("summary")]
    if options:
        labels = {f"方案{o.get('id')}：{o.get('summary')}": o.get("id")
                  for o in options}
        picked = st.radio("AI 拟的方向", list(labels), key="desk_choice")
        if st.button("就按这个方向写", key="desk_choice_ok"):
            _answer(ctx, pid, {"kind": "section_choice",
                               "section_key": question.get("section_key"),
                               "choice": labels[picked]})
    else:
        st.caption("（AI 没有拟出方案，可直接让 AI 决定或自己写）")

    c1, c2 = st.columns(2)
    if c1.button("让 AI 自己决定", key="desk_choice_ai"):
        _answer(ctx, pid, {"kind": "section_choice",
                           "section_key": question.get("section_key"),
                           "choice": "ai"})
    with c2.popover("我自己写", width="stretch"):
        text = st.text_area("写好的内容", key="desk_self_text", height=160)
        if st.button("提交我写的内容", key="desk_self_ok"):
            if not text.strip():
                st.warning("内容不能为空")
            else:
                _answer(ctx, pid, {"kind": "section_choice",
                                   "section_key": question.get("section_key"),
                                   "choice": "self", "text": text.strip()})


# ---------------------------------------------------------------- 缺口决定
def _gap_decision(ctx, pid: Any, question: dict[str, Any]) -> None:
    verdict = question.get("verdict") or {}
    unmet = [_DIM_LABELS.get(d, d)
             for d in (verdict.get("unmet_dimensions") or [])]
    counts = verdict.get("counts") or {}
    spent = int(question.get("rounds_spent") or 0)
    budget_spent = bool(question.get("budget_spent"))

    st.markdown(f"**「{question.get('heading')}」的支撑不足，怎么办？**")
    st.warning(f"未达标：{'、'.join(unmet) or '—'}；"
               f"命中文献 {counts.get('matched_papers', 0)} 篇 · "
               f"已抽取 {counts.get('extracted_papers', 0)} 篇 · "
               f"带量化条件的超边 {counts.get('hyperedges_with_quantity', 0)}"
               f"/{counts.get('hyperedges', 0)}"
               + (f" · 已累计补检 {spent} 轮" if spent else ""))
    if budget_spent:
        st.error("补检预算已用尽。再补检不会再产生可用证据——"
                 "建议「保留缺口照常撰写」（正文会显式标注缺口），"
                 "或用「自定义任务」换个口径。")

    options = question.get("options") or []
    by_id = {o.get("id"): o for o in options}
    choices = []
    for option in options:
        if option.get("disabled"):
            choices.append(f"{option.get('label')}"
                           f"（{option.get('hint') or '不可用'}）")
        else:
            choices.append(str(option.get("label")))
    labels = {text: option.get("id") for text, option in zip(choices, options)}
    default = next((text for text, oid in labels.items()
                    if by_id.get(oid, {}).get("recommended")), choices[0])
    picked = st.radio("处理方式", choices, index=choices.index(default),
                      key="desk_gap")

    payload: dict[str, Any] = {"kind": "gap_decision",
                               "section_key": question.get("section_key"),
                               "decision": labels[picked]}
    if labels[picked] == "collect":
        rounds = by_id.get("collect", {}).get("rounds") or {}
        c1, c2 = st.columns([1, 2])
        use_ai = c2.checkbox("让 AI 决定轮数", key="desk_rounds_ai")
        value = c1.number_input("本次最多补检轮数",
                                min_value=int(rounds.get("min") or 1),
                                max_value=int(rounds.get("max") or 5),
                                value=int(rounds.get("default") or 2),
                                key="desk_rounds", disabled=use_ai)
        payload["rounds"] = "ai" if use_ai else int(value)
    elif labels[picked] == "custom":
        text = st.text_input("用你自己的话说明要补什么",
                             placeholder="例如：补 3 篇讲区域选择性的最新文献",
                             key="desk_custom_text")
        payload["text"] = text.strip()

    if st.button("就这么办", key="desk_gap_ok"):
        if labels[picked] == "custom" and not payload.get("text"):
            st.warning("自定义任务需要写一句说明")
            return
        _answer(ctx, pid, payload)


# ---------------------------------------------------------------- 自定义方案
def _custom_plan(ctx, pid: Any, question: dict[str, Any]) -> None:
    st.markdown(f"**你的要求**：{question.get('custom_input') or ''}")
    options = [o for o in (question.get("options") or []) if o.get("tasks")]
    if options:
        labels = {str(o.get("id")): o for o in options}
        picked = st.radio(
            "解析出的执行方案",
            list(labels),
            format_func=lambda key: "方案%s：%s" % (key, labels[key].get("label")),
            key="desk_cplan")
        st.caption(labels[picked].get("hint") or "")
        if st.button("用选中的方案", key="desk_cplan_ok"):
            _answer(ctx, pid, {"kind": "custom_plan",
                               "section_key": question.get("section_key"),
                               "choice": picked})
    c1, c2 = st.columns(2)
    if c1.button("让 AI 自己决定", key="desk_cplan_ai"):
        _answer(ctx, pid, {"kind": "custom_plan",
                           "section_key": question.get("section_key"),
                           "choice": "ai"})
    with c2.popover("我再明确一点", width="stretch"):
        text = st.text_area("补充说明", key="desk_refine_text", height=120)
        if st.button("按补充说明重新解析", key="desk_refine_ok"):
            if not text.strip():
                st.warning("请写出要补充的说明")
            else:
                _answer(ctx, pid, {"kind": "custom_plan",
                                   "section_key": question.get("section_key"),
                                   "choice": "refine", "text": text.strip()})


# ====================================================================== 部分
def _sections(ctx, pid: Any, snap: dict[str, Any]) -> None:
    done = int(snap.get("completed") or 0)
    total = int(snap.get("total") or 0)
    with st.container(border=True):
        st.markdown("#### 执行摘要")
        st.progress(done / total if total else 0.0,
                    text=f"进度 {done}/{total} 部分")
        for sec in snap.get("sections") or []:
            stage = _STAGE_FALLBACK.get(str(sec.get("stage")), sec.get("stage"))
            verdict = sec.get("verdict") or {}
            unmet = [_DIM_LABELS.get(d, d)
                     for d in (verdict.get("unmet_dimensions") or [])]
            collab = sec.get("collaboration") or {}
            bits = [f"{sec.get('content_chars') or 0} 字符"
                    if sec.get("content_chars") else "尚无正文",
                    f"阶段 {stage}"]
            if verdict.get("decision"):
                bits.append(f"判定 {verdict['decision']}")
            if unmet:
                bits.append("缺 " + "、".join(unmet))
            if collab.get("summary"):
                bits.append(f"协作：{collab['summary']}")
            if sec.get("error"):
                bits.append(f"⚠ {sec['error']}")
            st.markdown(f"**{sec.get('heading')}**  \n" + " · ".join(map(str, bits)))
            c1, c2 = st.columns(2)
            with c1.popover("看正文", width="stretch"):
                content = ctx.service.section_content(pid, sec["section_key"])
                st.markdown(content.get("content") or "_（本节尚未生成）_")
            with c2.popover("决策轨迹", width="stretch"):
                trace = ctx.service.section_trace(pid, sec["section_key"])
                rows = trace.get("trace") or trace.get("rounds") or []
                if not rows:
                    st.caption("暂无轨迹记录。")
                for row in rows[:10]:
                    st.markdown(f"- 第 {row.get('round', '?')} 轮："
                                f"{row.get('decision') or row.get('status') or ''}"
                                f"  \n  {str(row.get('reasons') or '')[:200]}")


# ====================================================================== 派工
def _directive(ctx, pid: Any, snap: dict[str, Any]) -> None:
    """对节点下指令：自然语言 → 3 个方案 → 下单分发。"""
    with st.container(border=True):
        st.markdown("#### 对节点下指令")
        st.caption("用你自己的话说明要补什么，**工作规划节点**会解析成"
                   "「对哪些节点下什么单」并分发执行。")
        text = st.text_input("指令",
                             placeholder="例如：把库里未抽取的炔酰胺文献抽成知识",
                             key="desk_direct_text")
        if st.button("解析成方案（只解析，不执行）", key="desk_direct_parse"):
            if not text.strip():
                st.warning("先写下你要做什么")
            else:
                with st.spinner("规划节点正在解析…"):
                    st.session_state["desk_plans"] = \
                        ctx.service.parse_dispatch_request(
                            text.strip(), project_id=pid,
                            topic=(snap.get("intake") or {}).get("topic") or "",
                            heading=(snap.get("question") or {}).get("heading") or "",
                            verdict=(snap.get("question") or {}).get("verdict") or {})

        plans = (st.session_state.get("desk_plans") or {}).get("plans") or []
        if plans:
            parsed = st.session_state.get("desk_plans") or {}
            note = parsed.get("note")
            st.caption("解析来源：" + ("模型" if parsed.get("parsed_by") == "llm"
                                      else "确定性兜底")
                       + (f" · {note}" if note else ""))
            labels = {}
            for plan in plans:
                steps = " → ".join(f"{s.get('task')}@{s.get('node')}"
                                   for s in plan.get("plan") or [])
                labels[f"方案{plan.get('id')}：{plan.get('label')}"
                       f"（{steps}）"] = plan
            picked = st.radio("执行方案", list(labels), key="desk_plan_pick")
            if st.button("下单执行", key="desk_dispatch"):
                with st.spinner("派工执行中…"):
                    st.session_state["desk_last_dispatch"] = \
                        ctx.service.create_dispatch(
                            labels[picked].get("plan") or [], project_id=pid,
                            section_key=(snap.get("question")
                                         or {}).get("section_key") or "",
                            origin="user_direct", reason=text.strip())

        record = st.session_state.get("desk_last_dispatch")
        if record:
            st.success(f"派工单 {record.get('dispatch_id')}：{record.get('status')}")
            for step in record.get("steps") or []:
                note = step.get("skip_reason") or step.get("error") or ""
                st.markdown(f"- `{step.get('task')}`@{step.get('node')} → "
                            f"{step.get('status')}"
                            + (f"（{note}）" if note else ""))

        runs = ctx.service.list_dispatches(project_id=pid, limit=5)
        if runs:
            with st.expander("最近的派工单", expanded=False):
                for run in runs:
                    st.markdown(f"**{run.get('dispatch_id')}** · {run.get('status')}"
                                f" · {run.get('origin')}"
                                f" · 新增 {run.get('added') or 0}")
                    for step in run.get("steps") or []:
                        st.caption(f"　{step.get('task')}@{step.get('node')} → "
                                   f"{step.get('status')}")
