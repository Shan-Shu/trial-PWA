/* 研究流程页：Planner-first 六节点流水线 + 本轮中间产物。
 * 这是 trial 引擎最有辨识度的能力（设计契约 → 机制消费 → 候选池 → 审核 → 事实核查），
 * 在 PWA 形态下以"流程卡片 + 阶段产物"的方式呈现。
 */
"use strict";

import { api } from "../common/api.js";
import { $, h, fmtFull, fmtTs, trunc } from "../common/dom.js";
import { card, metrics, badge, statusBadge, empty, loading, errorBox, kv, openDrawer, setDrawerBody } from "../ui/design.js";
import { toast, toastError } from "../common/ui.js";

let host = null;
let status = null;

export const experimentPage = {
  title: "研究流程",
  subtitle: "Planner → 消费 → 候选 → 审核 → 事实核查",
  group: "研究",
  icon: "◐",

  mount(container) {
    host = container;
  },

  async refresh() {
    if (!host) return;
    if (!status) host.innerHTML = loading("正在读取研究流程状态…");
    try {
      status = await api.get("/api/study/status");
      host.innerHTML = render();
      bind();
    } catch (err) {
      host.innerHTML = errorBox(err);
    }
  },
};

function render() {
  const nodes = status.nodes || [];
  const events = status.events || [];
  const summary = status.summary || {};
  const done = nodes.filter((n) => n.status === "done").length;
  const running = nodes.filter((n) => n.status === "running").length;

  const head = metrics([
    ["运行状态", status.status || "idle",
      status.run_id ? `run ${status.run_id}` : "暂无运行记录",
      status.status === "running" ? "blue" : (status.status === "idle" ? "gray" : "green")],
    ["节点进度", `${done}/${nodes.length}`, running ? `${running} 个运行中` : "", "accent"],
    ["本轮事件", events.length, status.started_at ? `开始 ${fmtTs(status.started_at)}` : "", "cyan"],
    ["请求", trunc(status.request || "-", 28), "", "violet"],
  ]);

  const flowHtml = nodes.map((n, index) => `
    <div class="card flat" style="flex:1;min-width:158px">
      <div class="row" style="justify-content:space-between">
        <span class="muted" style="font-size:11px">${h(index + 1)}</span>
        ${statusBadge(n.status)}
      </div>
      <div style="font-weight:650;margin-top:6px">${h(n.label || n.id)}</div>
      <div class="muted" style="font-size:11.5px;margin-top:3px">${h(n.desc || "")}</div>
      <div class="muted" style="font-size:11px;margin-top:6px">
        事件 ${h(n.count ?? 0)}${n.last_ts ? ` · ${h(fmtTs(n.last_ts))}` : ""}
      </div>
    </div>`).join("");

  return `
  <div class="page-head">
    <h1>研究流程</h1>
    <div class="desc">Planner 是唯一入口：先生成 <code>retrieval_plan</code> 与
      <code>design_contract</code>；候选方案以<b>算子链</b>为单位，算子只能取自封闭词表。</div>
  </div>
  ${head}
  ${card(`<div class="row" style="align-items:stretch;gap:10px">${flowHtml}</div>`,
    { flat: true, title: "六节点流水线", subtitle: "审核与事实核查失败会回流内容节点定向量改" })}
  <div class="grid-2" style="margin-top:14px">
    ${card(summaryPane(summary), { title: "本轮中间产物", subtitle: "来自 study_runs 的落库快照" })}
    ${card(eventsPane(events), { title: "本轮事件", subtitle: "按时间倒序（最多 80 条）" })}
  </div>`;
}

function summaryPane(summary) {
  const plan = summary.plan || {};
  const consumer = summary.consumer || {};
  const draft = summary.draft || {};
  const review = summary.review || {};
  const fact = summary.fact_check || {};
  const candidates = draft.candidates || draft.strategies || [];
  const design = consumer.design_context || {};

  if (!Object.keys(summary).length) {
    return empty("本轮还没有落库产物（运行一次完整研究流程后可见）");
  }
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
  return `<div class="stack" style="max-height:420px;overflow:auto">${events.slice(0, 40).map((e) => `
    <div class="row" style="justify-content:space-between;font-size:12px;border-bottom:1px dashed var(--border);padding:4px 0">
      <span>${badge(e.node || "study")} ${h(e.event)}</span>
      <span class="muted">${h(fmtFull(e.ts))}</span>
    </div>`).join("")}</div>`;
}

function bind() {
  const button = document.createElement("button");
  button.className = "btn small primary";
  button.textContent = "刷新状态";
  button.onclick = () => experimentPage.refresh();
  const head = host.querySelector(".page-head");
  if (head) head.appendChild(button);
}
