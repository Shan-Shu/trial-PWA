/* 审核中心页：待人工审核队列 + 预设动作 + 审核历史。
 * 决策会写回 papers.status 并记录 human_reviews（可审计）。
 */
"use strict";

import { api } from "../common/api.js";
import { $, h, fmtFull, fmtNumber, trunc } from "../common/dom.js";
import { card, metrics, badge, decisionBadge, statusBadge, progressRow, empty, loading, errorBox, openDrawer, setDrawerBody, kv, chips } from "../ui/design.js";
import { toast, toastError } from "../common/ui.js";

let host = null;
let data = null;

export const reviewPage = {
  title: "审核中心",
  subtitle: "待审队列 · 预设决策 · 历史",
  group: "运行",
  icon: "✓",

  mount(container) {
    host = container;
  },

  async refresh() {
    if (!host) return;
    if (!data) host.innerHTML = loading("正在读取审核队列…");
    try {
      data = await api.get("/api/reviews?history=true");
      host.innerHTML = render();
      bind();
    } catch (err) {
      host.innerHTML = errorBox(err);
    }
  },
};

function render() {
  const pending = data.pending || [];
  const history = data.history || [];
  const presets = data.presets || {};

  const head = metrics([
    ["待审核", pending.length, "", pending.length ? "amber" : "green"],
    ["已完成", history.length, "", "green"],
    ["平均 Q", pending.length
      ? fmtNumber(pending.reduce((s, p) => s + (p.quality || 0), 0) / pending.length, 3)
      : "-", "待审队列均值", "accent"],
    ["预设动作", Object.keys(presets).length, "", "violet"],
  ]);

  const rowsHtml = pending.map((p) => `
    <tr>
      <td><div style="font-weight:600">${h(trunc(p.title || "未命名", 66))}</div>
        <div class="muted" style="font-size:11px">${h(p.paper_key)}</div></td>
      <td class="muted" style="font-size:12px">${h(trunc(p.venue || "-", 22))}</td>
      <td>${p.pub_year == null ? '<span class="muted">未知</span>' : h(p.pub_year)}</td>
      <td>${h(fmtNumber(p.quality, 3))}</td>
      <td>${decisionBadge(p.decision)}</td>
      <td style="font-size:12px">${h(trunc(p.rationale || "-", 44))}</td>
      <td><button class="btn small primary" data-review="${h(p.paper_key)}">审核</button></td>
    </tr>`).join("");

  const historyRowsHtml = history.slice(0, 50).map((item) => `
    <tr>
      <td class="muted" style="font-size:11.5px">${h(fmtFull(item.reviewed_at))}</td>
      <td>${h(item.paper_key)}</td>
      <td>${badge(item.action || "-")}</td>
      <td>${badge(item.decision || "-")}</td>
      <td style="font-size:12px">${h(trunc(item.rationale || "-", 52))}</td>
    </tr>`).join("");

  return `
  <div class="page-head">
    <h1>审核中心</h1>
    <div class="desc">低质量或元数据无法补全的文献进入此队列；决策写回 <code>papers.status</code>
      并记录审核历史，便于事后审计。</div>
  </div>
  ${head}
  <div class="grid-2">
    ${card(pending.length ? `
      <div class="table-wrap" style="max-height:460px">
        <table>
          <thead><tr><th>标题</th><th style="width:150px">期刊</th><th style="width:64px">年份</th>
            <th style="width:62px">Q</th><th style="width:110px">判定</th>
            <th>说明</th><th style="width:70px"></th></tr></thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>` : empty("没有待审核文献 🎉"),
      { title: `待审核队列（${pending.length}）`, subtitle: "点击「审核」查看质量构成并提交决策" })}
    ${card(history.length ? `
      <div class="table-wrap" style="max-height:460px">
        <table><thead><tr><th style="width:150px">时间</th><th>文献</th>
          <th style="width:120px">动作</th><th style="width:110px">决策</th><th>理由</th></tr></thead>
          <tbody>${historyRowsHtml}</tbody></table>
      </div>` : empty("暂无审核历史"),
      { title: `审核历史（${history.length}）`, subtitle: "human_reviews 表" })}
  </div>
  ${card(Object.keys(presets).length ? `<div class="row">${Object.entries(presets).map(([key, spec]) => `
    <div class="card flat" style="min-width:210px">
      <div class="row" style="justify-content:space-between">
        <b style="font-size:12.5px">${h(spec.label || key)}</b>
        ${badge(key, "gray")}
      </div>
      <div class="muted" style="font-size:11.5px;margin-top:4px">
        决策 ${h(spec.decision || "-")} · 状态 ${h(spec.status || "-")}
        · 需复核 ${spec.needs_review ? "是" : "否"}</div>
    </div>`).join("")}</div>` : empty("无预设动作"),
    { title: "预设动作", subtitle: "来自后端 HUMAN_REVIEW_PRESETS" })}
  `;
}

function bind() {
  host.querySelectorAll("[data-review]").forEach((btn) => {
    btn.onclick = () => openReview(btn.dataset.review);
  });
}

function openReview(paperKey) {
  const item = (data.pending || []).find((p) => p.paper_key === paperKey) || {};
  const presets = data.presets || {};
  const presetOptionsHtml = Object.entries(presets).map(([key, spec]) =>
    `<option value="${h(key)}">${h(spec.label || key)}</option>`).join("");
  openDrawer(`审核 · ${trunc(item.title || paperKey, 40)}`, `
    <div class="row" style="margin-bottom:10px">
      ${decisionBadge(item.decision)} ${statusBadge(item.status)}
      ${item.needs_review ? badge("需复核", "amber") : badge("无需复核", "green")}
    </div>
    <h3>${h(item.title || "未命名文献")}</h3>
    <div class="muted" style="font-size:11.5px;margin-bottom:10px">${h(paperKey)}</div>
    ${kv([
      ["期刊 / 库", item.venue], ["年份", item.pub_year], ["来源", item.source],
      ["评估时间", item.assessed_at ? fmtFull(item.assessed_at) : ""],
    ])}
    <div class="section-title">质量构成</div>
    ${progressRow("综合 Q", item.quality, item.quality == null ? "-" : Number(item.quality).toFixed(3))}
    ${progressRow("权威性 A", item.authority, item.authority == null ? "-" : Number(item.authority).toFixed(3))}
    ${progressRow("时效性 T", item.timeliness, item.timeliness == null ? "-" : Number(item.timeliness).toFixed(3))}
    ${item.rationale ? `<div class="section-title">说明</div><div style="font-size:12.5px">${h(item.rationale)}</div>` : ""}
    ${item.clean_preview ? `<div class="section-title">精校正文（前 1200 字符）</div>
      <div class="pre">${h(item.clean_preview.slice(0, 1200))}</div>`
      : `<div class="muted" style="font-size:12px;margin-top:8px">该文献没有正文（全文来源为摘要）</div>`}
    <div class="section-title">提交决策</div>
    <div class="stack">
      <div class="row">
        <select id="rvPreset" style="flex:1">${presetOptionsHtml}</select>
        <button class="btn primary small" id="rvSubmit">提交</button>
      </div>
      <input type="text" id="rvRationale" placeholder="审核理由（可选，会写入历史）" />
    </div>`);

  $("#rvSubmit")?.addEventListener("click", async () => {
    const action = $("#rvPreset")?.value;
    const rationale = $("#rvRationale")?.value || "";
    try {
      const res = await api.post("/api/reviews/submit",
        { paper_key: paperKey, action, rationale });
      if (!res.ok) throw new Error(res.error || "提交失败");
      toast(`已提交：${res.action || action}`);
      data = null;
      await reviewPage.refresh();
      document.getElementById("drawerClose")?.click();
    } catch (err) { toastError(err); }
  });
}
