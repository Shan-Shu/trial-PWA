/* 研究流程页：**登记表驱动的节点总览** + 当前派工 + 本轮中间产物。
 *
 * 这一页以前为什么没用：
 *
 *   1. 节点清单写死在后端，只有 10 个，写作台的访谈节点不在其中；
 *   2. 状态是"有事件就算 done"推出来的 ⇒ 绝大多数节点显示"已完成"，
 *      包括**从未真正执行过**的；
 *   3. 界面上看不到"谁在给谁下单"，出问题时只能翻库。
 *
 * 现在三件事都改了：清单来自登记表（新增节点自动出现）、状态语义明确
 * （未执行/执行中/已完成/部分完成/失败/结果过期）、派工单独成板。
 */
"use strict";

import { api } from "../common/api.js";
import { $, h, fmtFull, fmtTs, trunc } from "../common/dom.js";
import { card, metrics, badge, empty, loading, errorBox, kv, openDrawer } from "../ui/design.js";
import { toastError } from "../common/ui.js";

let host = null;
let overview = null;
let status = null;
let groupFilter = "全部";

/** 状态 → 徽标色调（标签用后端给的中文，避免两处漂移）。 */
const STATUS_TONES = {
  idle: "gray", skipped: "gray", cancelled: "gray",
  running: "blue", done: "green", partial: "amber",
  failed: "red", stale: "amber", waiting: "red", error: "red",
};

export const experimentPage = {
  title: "研究流程",
  subtitle: "节点登记表 · 派工 · 本轮产物",
  group: "研究",
  icon: "◐",

  mount(container) {
    host = container;
  },

  async refresh() {
    if (!host) return;
    if (!overview) host.innerHTML = loading("正在读取节点与派工状态…");
    try {
      // 两个接口各司其职：登记表给"节点清单与状态"，study 给"本轮产物"。
      const [nodes, study] = await Promise.all([
        api.get("/api/status/nodes"),
        api.get("/api/study/status").catch(() => null),
      ]);
      overview = nodes;
      status = study;
      host.innerHTML = render();
      bind();
    } catch (err) {
      host.innerHTML = errorBox(err);
    }
  },
};

function render() {
  const nodes = overview.nodes || [];
  const dispatches = overview.dispatches || [];
  const groups = overview.groups || [];
  const labels = overview.status_labels || {};
  const counts = tally(nodes, labels);
  const running = nodes.filter((n) => n.status === "running").length;

  const head = metrics([
    ["节点", nodes.length, `${groups.length} 组 · ${overview.llm_nodes?.length || 0} 个接模型`],
    ["已完成", counts.done || 0, `未执行 ${counts.idle || 0}`,
      (counts.done || 0) ? "green" : "gray"],
    ["进行中", running, running ? "有节点正在执行" : "当前无执行",
      running ? "blue" : "gray"],
    ["派工", dispatches.length, dispatches.length ? "最近的派工单" : "暂无派工记录", "violet"],
  ]);

  const tabs = ["全部", ...groups];
  const tabHtml = tabs.map((name) => `
    <button class="btn small ${name === groupFilter ? "primary" : "ghost"}"
            data-group="${h(name)}">${h(name)}</button>`).join("");

  const shown = groupFilter === "全部"
    ? groups : groups.filter((g) => g === groupFilter);
  const sectionsHtml = shown.map((group) => card(
    `<div class="stack">${nodes.filter((n) => n.group === group)
      .map(nodeCard).join("")}</div>`,
    { title: group, subtitle: `${nodes.filter((n) => n.group === group).length} 个节点` },
  )).join("");

  const summary = (status && status.summary) || {};
  const events = (status && status.events) || [];

  return `
  <div class="page-head">
    <h1>研究流程</h1>
    <div class="desc">节点清单来自<b>能力登记表</b>：新增节点会自动出现在这里。
      状态如实反映记录——<b>没有记录就是「未执行」</b>，不再用"有事件就算完成"糊过去。</div>
  </div>
  ${head}
  ${card(`<div class="row" style="gap:6px;flex-wrap:wrap">${tabHtml}</div>`,
    { flat: true, title: "分组", subtitle: "按登记表分组筛选节点" })}
  <div class="stack" style="margin-top:14px">${sectionsHtml}</div>
  <div class="grid-2" style="margin-top:14px">
    ${card(dispatchPane(dispatches),
      { title: "当前派工", subtitle: "规划节点下单 → 各节点执行 → 逐步回报" })}
    ${card(summaryPane(summary),
      { title: "本轮中间产物", subtitle: "来自 study_runs 的落库快照" })}
  </div>
  ${card(eventsPane(events), { title: "本轮事件", subtitle: "按时间倒序（最多 40 条）" })}`;
}

function tally(nodes, labels) {
  const out = {};
  nodes.forEach((n) => {
    const key = n.status || "idle";
    out[key] = (out[key] || 0) + 1;
  });
  if (labels) out._labels = labels;
  return out;
}

/** 单个节点卡片：状态 + 一句"为什么" + 任务/成本/最近派工。 */
function nodeCard(node) {
  const tone = STATUS_TONES[node.status] || "gray";
  const label = node.status_label || node.status || "未执行";
  const tasks = node.tasks || [];
  const last = node.last_dispatch;
  const taskText = tasks.length
    ? tasks.map((t) => t.label || t.task).join(" / ")
    : "（无派工任务）";
  const modelText = node.needs_model
    ? `${node.model_label || "模型"} · ${node.model || "-"}`
    : "无模型";
  // 逐项 taskRow 内部全部经 h()；命名以 Html 结尾，转义检查器据此识别
  const taskRowsHtml = tasks.map(taskRow).join("");

  return `
  <div class="card flat" data-node="${h(node.node)}">
    <div class="row" style="justify-content:space-between;align-items:flex-start">
      <div>
        <div style="font-weight:650">
          ${h(node.label)}
          ${node.auto_dispatch ? "" : `<span class="muted" style="font-size:11px">（不自动派工）</span>`}
        </div>
        <div class="muted" style="font-size:11.5px;margin-top:2px">${h(node.desc || "")}</div>
      </div>
      <div class="row" style="gap:6px;flex-wrap:wrap;justify-content:flex-end">
        <span class="badge b-${tone}">${h(label)}</span>
        <span class="muted" style="font-size:11px">${h(node.node)}</span>
      </div>
    </div>
    <div class="muted" style="font-size:11.5px;margin-top:6px">
      ${h(node.status_note || "")}
    </div>
    <div class="muted" style="font-size:11px;margin-top:6px">
      任务：${h(taskText)} · ${h(modelText)}
      ${node.count ? ` · 历史事件 ${h(node.count)}` : ""}
      ${node.paper_count ? ` · 涉及文献 ${h(node.paper_count)}` : ""}
      ${node.last_ts ? ` · 最近 ${h(fmtTs(node.last_ts))}` : ""}
    </div>
    ${last ? `<div class="muted" style="font-size:11px;margin-top:4px">
      最近派工 ${h(last.dispatch_id)}（${h(last.task || "")}）：
      ${h(dispatchStatusLabel(last.status))}${last.skip_reason ? ` · ${h(last.skip_reason)}` : ""}
    </div>` : ""}
    ${tasks.length ? `<details style="margin-top:6px">
      <summary class="muted" style="cursor:pointer;font-size:11.5px">任务与成本</summary>
      <div class="stack" style="margin-top:6px">${taskRowsHtml}</div>
    </details>` : ""}
  </div>`;
}

function taskRow(task) {
  const cost = { network: "网络", llm: "模型", db: "数据库", compute: "计算" }[task.cost] || task.cost;
  const span = (task.typical_seconds || [0, 0]);
  return `<div class="muted" style="font-size:11.5px">
    <b>${h(task.label || task.task)}</b>（<code>${h(task.task)}</code>）
    · 参数 ${h((task.accepts || []).join("、") || "无")}
    · 产出 ${h((task.produces || []).join("、") || "无")}
    · ${h(cost)} ${h(span[0])}~${h(span[1])}s
    ${task.implemented ? "" : " · <b>尚未实现</b>"}
  </div>`;
}

function dispatchStatusLabel(value) {
  return {
    running: "执行中", done: "已完成", failed: "失败",
    skipped: "未执行", cancelled: "已取消",
  }[value] || value || "-";
}

function dispatchPane(dispatches) {
  if (!dispatches.length) {
    return empty("暂无派工记录。写作台里「执行检索补全」或直接对节点下指令后，这里会出现派工单。");
  }
  return `<div class="stack" style="max-height:460px;overflow:auto">${
    dispatches.slice(0, 12).map((run) => {
      const steps = run.steps || [];
      const tone = STATUS_TONES[run.status] || "gray";
      return `
    <div class="card flat" data-dispatch="${h(run.dispatch_id)}">
      <div class="row" style="justify-content:space-between">
        <div>
          <b style="font-size:12.5px">${h(run.dispatch_id)}</b>
          <span class="muted" style="font-size:11px"> · ${h(run.origin || "")}
            ${run.section_key ? ` · ${h(run.section_key)}` : ""}</span>
        </div>
        <span class="badge b-${tone}">${h(dispatchStatusLabel(run.status))}</span>
      </div>
      ${run.reason ? `<div class="muted" style="font-size:11.5px;margin-top:4px">${h(trunc(run.reason, 160))}</div>` : ""}
      <div class="muted" style="font-size:11px;margin-top:4px">
        ${h(fmtFull(run.ts))} · 步骤 ${h(steps.length)} · 新增 ${h(run.added ?? 0)}
      </div>
      <div class="stack" style="margin-top:6px">${steps.map((s, i) => `
        <div class="muted" style="font-size:11.5px">
          ${h(i + 1)}. <code>${h(s.task || "")}</code> @ ${h(s.node || "")}
          → ${h(dispatchStatusLabel(s.status))}
          ${s.seconds != null ? ` · ${h(s.seconds)}s` : ""}
          ${s.skip_reason ? ` · ${h(s.skip_reason)}` : ""}
          ${s.error ? ` · ${h(trunc(s.error, 120))}` : ""}
        </div>`).join("")}</div>
      ${run.error ? `<div class="muted" style="font-size:11.5px;margin-top:4px;color:var(--danger,#c33)">
        ${h(trunc(run.error, 200))}</div>` : ""}
      <div class="row" style="margin-top:6px">
        <button class="btn small ghost" data-dispatch-detail="${h(run.dispatch_id)}">看原始记录</button>
      </div>
    </div>`;
    }).join("")}</div>`;
}

function summaryPane(summary) {
  if (!summary || !Object.keys(summary).length) {
    return empty("本轮还没有落库产物（运行一次完整研究流程后可见）");
  }
  const plan = summary.plan || {};
  const consumer = summary.consumer || {};
  const draft = summary.draft || {};
  const review = summary.review || {};
  const fact = summary.fact_check || {};
  const candidates = draft.candidates || draft.strategies || [];
  const design = consumer.design_context || {};

  return `
    <div class="stack">
      <div>
        <div class="section-title" style="margin-top:0">Planner 契约</div>
        ${kv([
          ["目标", plan.goal || "-"],
          ["领域", plan.domain || "-"],
          ["任务类型", `${plan.task_kind || "-"} / ${plan.content_type || "-"}`],
          ["创新下限", plan.design_contract?.innovation_floor || plan.innovation_floor || "-"],
        ])}
      </div>
      <div>
        <div class="section-title">知识消费（design_context）</div>
        ${Object.keys(design).length ? `
          <div class="row">
            ${Object.entries(design).map(([key, value]) => {
              const size = Array.isArray(value) ? value.length : 0;
              return `<span class="chip-tag${size ? " on" : ""}">${h(key)} ${h(size)}</span>`;
            }).join("")}
          </div>` : `<span class="muted">无（未运行消费节点）</span>`}
        ${consumer.traceability ? `<div class="muted" style="font-size:11.5px;margin-top:6px">
          可追溯率 ${h(consumer.traceability.ratio ?? "-")}
          （${h(consumer.traceability.traceable_items ?? 0)}/${h(consumer.traceability.total_items ?? 0)} 条绑定真实证据）</div>` : ""}
      </div>
      <div>
        <div class="section-title">候选方案（算子链）</div>
        ${candidates.length ? `<div class="stack">${candidates.slice(0, 6).map((c) => `
          <div>
            <div class="row" style="justify-content:space-between">
              <b style="font-size:12.5px">${h(trunc(c.title || c.target || "候选", 46))}</b>
              ${c.innovation_level ? badge(c.innovation_level, "violet") : ""}
            </div>
            ${(c.operator_chain || []).length ? `<div class="muted" style="font-size:11.5px">
              ${h((c.operator_chain || []).map((s) => s.operator || s).join(" → "))}</div>` : ""}
          </div>`).join("")}</div>` : `<span class="muted">无候选</span>`}
      </div>
      <div class="grid-2">
        <div><div class="section-title">审核</div>
          ${Object.keys(review).length ? kv([
            ["总分", review.overall_score],
            ["决策", review.decision || review.status],
            ["修订意见", (review.revision_actions || review.issues || []).length],
          ]) : `<span class="muted">未审核</span>`}</div>
        <div><div class="section-title">事实核查</div>
          ${Object.keys(fact).length ? kv([
            ["结论", fact.decision],
            ["问题数", (fact.issues || []).length],
            ["高危", (fact.issues || []).filter((i) => i.severity === "high").length],
          ]) : `<span class="muted">未核查</span>`}</div>
      </div>
      <details><summary class="muted" style="cursor:pointer">原始 summary JSON</summary>
        <div class="pre" style="margin-top:6px">${h(JSON.stringify(summary, null, 2))}</div></details>
    </div>`;
}

function eventsPane(events) {
  if (!events.length) return empty("本轮没有事件");
  return `<div class="stack" style="max-height:380px;overflow:auto">${events.slice(0, 40).map((e) => `
    <div class="row" style="justify-content:space-between;font-size:12px;border-bottom:1px dashed var(--border);padding:4px 0">
      <span>${badge(e.node || "study")} ${h(e.event)}</span>
      <span class="muted">${h(fmtFull(e.ts))}</span>
    </div>`).join("")}</div>`;
}

function bind() {
  const head = host.querySelector(".page-head");
  if (head && !head.querySelector("[data-refresh]")) {
    const button = document.createElement("button");
    button.className = "btn small primary";
    button.setAttribute("data-refresh", "1");
    button.textContent = "刷新状态";
    button.onclick = () => experimentPage.refresh();
    head.appendChild(button);
  }
  // 分组切换与派工详情：用**委托**绑定在 host 上，只装一次。
  // 为什么不能给按钮单独 addEventListener：innerHTML 重渲染会把它们换掉，
  // 事件会静默失效——写作台正是踩了这个坑（点击"没反应"）。
  if (!bind.delegated) {
    bind.delegated = true;
    host.addEventListener("click", onHostClick);
  }
}

async function onHostClick(event) {
  const el = event.target instanceof Element ? event.target : null;
  if (!el || !overview) return;
  const group = el.closest("[data-group]");
  if (group) {
    groupFilter = group.getAttribute("data-group") || "全部";
    host.innerHTML = render();
    bind();
    return;
  }
  const detail = el.closest("[data-dispatch-detail]");
  if (detail) {
    const id = detail.getAttribute("data-dispatch-detail");
    try {
      const res = await api.get(`/api/dispatches/${encodeURIComponent(id)}`);
      const run = res.dispatch || {};
      openDrawer(`派工 ${id}`, `
        <div class="pre" style="max-height:60vh;overflow:auto">${h(JSON.stringify(run, null, 2))}</div>`);
    } catch (err) {
      toastError(err);
    }
    return;
  }
  const nodeEl = el.closest("[data-node]");
  if (!nodeEl) return;
  const nodeId = nodeEl.getAttribute("data-node");
  const node = (overview.nodes || []).find((n) => n.node === nodeId);
  if (!node) return;
  openDrawer(node.label || node.node, `
    <div class="stack">
      <div>${h(node.label)} <span class="muted">（${h(node.node)}）</span></div>
      <div class="muted">${h(node.desc || "")}</div>
      <div>状态：${h(node.status_label || node.status)} — ${h(node.status_note || "")}</div>
      ${kv([["分组", node.group], ["角色", node.role || "无"],
            ["模型", node.model || "无"],
            ["历史事件", node.count ?? 0],
            ["涉及文献", node.paper_count ?? 0]])}
      <div class="pre" style="max-height:50vh;overflow:auto">${h(JSON.stringify(node, null, 2))}</div>
    </div>`);
}
