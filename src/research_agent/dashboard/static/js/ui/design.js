/* ============================================================================
 * 设计系统：把 PWA 的视觉语言固化成可复用的 HTML 片段构造器。
 * 所有动态值必须经过 h() / esc()；本模块只产出字符串。
 * ========================================================================== */
"use strict";

import { h } from "../common/dom.js";

/** 卡片 */
export function card(bodyHtml, { title = "", subtitle = "", actionsHtml = "", flat = false, cls = "" } = {}) {
  return `<section class="card ${flat ? "flat" : ""} ${cls}">
    ${title || actionsHtml ? `<div class="row" style="justify-content:space-between;align-items:flex-start">
      <div>
        ${title ? `<h3 style="margin:0">${h(title)}</h3>` : ""}
        ${subtitle ? `<div class="muted" style="font-size:11.5px">${h(subtitle)}</div>` : ""}
      </div>
      <div class="row tight">${actionsHtml}</div>
    </div>` : ""}
    ${bodyHtml}
  </section>`;
}

/** 指标卡栅格：items = [[key, value, hint?, tone?]] */
export function metrics(items, { cols } = {}) {
  const cellsHtml = items.map(([key, value, hint, tone]) => `
    <div class="metric ${tone || ""}">
      <div class="k">${h(key)}</div>
      <div class="v">${h(value)}</div>
      ${hint ? `<div class="h">${h(hint)}</div>` : ""}
    </div>`).join("");
  const style = cols ? ` style="grid-template-columns:repeat(${cols},minmax(0,1fr))"` : "";
  return `<div class="metric-grid"${style}>${cellsHtml}</div>`;
}

const TONES = ["blue", "green", "amber", "red", "violet", "cyan", "gray"];

/** 徽章；tone 省略时按文本哈希取色，保证同类文本颜色稳定 */
export function badge(text, tone) {
  const color = tone || TONES[hash(String(text)) % (TONES.length - 1)];
  return `<span class="badge b-${color}">${h(text)}</span>`;
}

export function decisionBadge(decision) {
  const map = {
    direct: ["直接→提取", "green"],
    flagged: ["标记→提取", "amber"],
    enrich: ["元数据回补", "blue"],
    human: ["人工审核", "red"],
  };
  const [label, tone] = map[decision] || [decision || "未评估", "gray"];
  return `<span class="badge b-${tone}">${h(label)}</span>`;
}

export function statusBadge(status) {
  const map = {
    raw: ["原始", "gray"], ingested: ["已入库", "green"],
    human_review: ["待人工", "red"], quality_passed: ["质量通过", "green"],
    quality_rejected: ["质量拒绝", "red"], collected: ["已采集", "blue"],
    pending: ["等待", "gray"], running: ["运行中", "blue"],
    done: ["已完成", "green"], error: ["错误", "red"],
    needs_collection: ["等待补集", "amber"], cancelled: ["已取消", "gray"],
    reviewed: ["已审核", "green"], pass: ["通过", "green"],
    revise: ["需修订", "amber"], manual_review: ["转人工", "red"],
  };
  const [label, tone] = map[status] || [status || "-", "gray"];
  return `<span class="badge b-${tone}">${h(label)}</span>`;
}

export function progress(ratio, { warn = false, label = "" } = {}) {
  const value = Math.max(0, Math.min(1, Number(ratio) || 0));
  return `<div class="progress" title="${h(label)}">
    <div class="progress-fill ${warn ? "warn" : ""}" style="width:${(value * 100).toFixed(1)}%"></div>
  </div>`;
}

/** 带标签的进度行（PWA 的维度得分形态） */
export function progressRow(label, ratio, valueText) {
  return `<div class="row" style="gap:10px;margin:5px 0">
    <span class="muted" style="width:104px;font-size:12px">${h(label)}</span>
    ${progress(ratio)}
    <span style="width:56px;text-align:right;font-size:12px">${h(valueText)}</span>
  </div>`;
}

export function chips(items, { cls = "" } = {}) {
  if (!items || !items.length) return `<span class="muted">无</span>`;
  return items.map((item) => `<span class="chip-tag ${cls}">${h(item)}</span>`).join("");
}

export function kv(pairs) {
  const rowsHtml = pairs
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `<dt>${h(key)}</dt><dd>${h(value)}</dd>`)
    .join("");
  return rowsHtml ? `<dl class="kv">${rowsHtml}</dl>` : `<span class="muted">无</span>`;
}

export function empty(text) {
  return `<div class="empty">${h(text)}</div>`;
}

export function loading(text = "加载中…") {
  return `<div class="card"><div class="skeleton" style="width:38%"></div>
    <div class="skeleton" style="width:72%"></div>
    <div class="skeleton" style="width:55%"></div>
    <div class="muted" style="font-size:12px;margin-top:8px">${h(text)}</div></div>`;
}

export function errorBox(err) {
  const msg = err && err.message ? err.message : String(err || "未知错误");
  return `<div class="card" style="border-color:var(--red)">
    <h3 style="color:var(--red)">出错了</h3>
    <div class="muted" style="font-size:12.5px">${h(msg)}</div></div>`;
}

/** 子 tab 容器：返回 {html, bind}，bind 负责切换与懒加载回调 */
export function subtabs(id, tabs, { active = null } = {}) {
  const first = active || (tabs[0] && tabs[0].key);
  const bar = `<div class="subtabs" data-subtabs="${h(id)}">
    ${tabs.map((t) => `<button class="subtab ${t.key === first ? "active" : ""}"
      data-subtab="${h(t.key)}">${h(t.label)}</button>`).join("")}
  </div>`;
  const panesHtml = tabs.map((t) => `<div class="subpane ${t.key === first ? "" : "hidden"}"
    data-subpane="${h(t.key)}">${t.bodyHtml || ""}</div>`).join("");
  return bar + `<div data-subpanes="${h(id)}">${panesHtml}</div>`;
}

/** 绑定 subtabs 的点击切换（全局委托一次即可，但这里按容器绑定更直观） */
export function bindSubtabs(root, onShow) {
  root.querySelectorAll("[data-subtabs]").forEach((bar) => {
    const id = bar.dataset.subtabs;
    // 注意：这是 DOM 元素（不是 HTML 片段），不要按 *Html 命名
    const panes = root.querySelector(`[data-subpanes="${h(id)}"]`);
    if (!panes) return;
    bar.querySelectorAll("[data-subtab]").forEach((btn) => {
      btn.onclick = () => {
        bar.querySelectorAll("[data-subtab]").forEach((b) =>
          b.classList.toggle("active", b === btn));
        panes.querySelectorAll("[data-subpane]").forEach((pane) =>
          pane.classList.toggle("hidden", pane.dataset.subpane !== btn.dataset.subtab));
        if (onShow) onShow(btn.dataset.subtab, panesHtml);
      };
    });
  });
}

/** 抽屉（详情面板）：PWA 的"点击查看详情"形态 */
export function openDrawer(title, bodyHtml) {
  closeDrawer();
  const scrim = document.createElement("div");
  scrim.className = "scrim";
  scrim.id = "drawerScrim";
  scrim.onclick = closeDrawer;
  const drawer = document.createElement("aside");
  drawer.className = "drawer";
  drawer.id = "drawer";
  drawer.innerHTML = `
    <div class="drawer-head">
      <span>${h(title)}</span>
      <span class="spacer"></span>
      <button class="btn small ghost" id="drawerClose">✕</button>
    </div>
    <div class="drawer-bodyHtml" id="drawerBody">${bodyHtml}</div>`;
  document.body.appendChild(scrim);
  document.body.appendChild(drawer);
  drawer.querySelector("#drawerClose").onclick = closeDrawer;
  document.addEventListener("keydown", escClose);
  return drawer;
}

function escClose(e) {
  if (e.key === "Escape") closeDrawer();
}

export function closeDrawer() {
  document.getElementById("drawer")?.remove();
  document.getElementById("drawerScrim")?.remove();
  document.removeEventListener("keydown", escClose);
}

export function setDrawerBody(html) {
  // 注意：这是 DOM 元素，不是 HTML 片段
  const bodyEl = document.getElementById("drawerBody");
  if (bodyEl) bodyEl.innerHTML = html;
}

function hash(text) {
  let value = 0;
  for (const ch of text) value = (value * 31 + ch.charCodeAt(0)) >>> 0;
  return value;
}
