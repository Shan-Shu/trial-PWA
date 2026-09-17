/* Toast 与轻量 UI 片段（徽章、进度条、空态）。 */
"use strict";

import { h, $ } from "./dom.js";

export function toast(message, kind = "info") {
  let box = $("#toast");
  if (!box) {
    box = document.createElement("div");
    box.id = "toast";
    box.className = "toast hidden";
    document.body.appendChild(box);
  }
  box.textContent = String(message ?? "");
  box.dataset.kind = kind;
  // 视觉区分：错误/警告走不同描边（CSS 里定义了 .toast.error / .toast.warn）
  box.classList.toggle("error", kind === "error");
  box.classList.toggle("warn", kind === "warn");
  box.classList.remove("hidden");
  clearTimeout(box._timer);
  box._timer = setTimeout(() => box.classList.add("hidden"), 5000);
}

export function toastError(err) {
  toast(err && err.message ? err.message : String(err), "error");
}

const DECISION_META = {
  direct: { label: "直接→提取", cls: "b-knowledge" },
  flagged: { label: "标记→提取", cls: "b-flagged" },
  enrich: { label: "元数据回补", cls: "b-enrich" },
  human: { label: "人工审核", cls: "b-human" },
};

export function decisionBadge(decision) {
  const meta = DECISION_META[decision] || { label: decision || "未评估", cls: "b-none" };
  return `<span class="badge ${meta.cls}">${h(meta.label)}</span>`;
}

const STATUS_LABEL = {
  raw: "原始",
  ingested: "已入库",
  human_review: "待人工",
  quality_passed: "质量通过",
  quality_rejected: "质量拒绝",
  collected: "已采集",
};

export function statusLabel(status) {
  return STATUS_LABEL[status] || status || "-";
}

export function progressBar(ratio, label = "") {
  const value = Math.max(0, Math.min(1, Number(ratio) || 0));
  return `<div class="progress" title="${h(label)}">
    <div class="progress-fill" style="width:${(value * 100).toFixed(1)}%"></div>
  </div>`;
}

export function emptyState(text) {
  return `<div class="summary-card muted">${h(text)}</div>`;
}

export function chipList(items, cls = "") {
  if (!items || !items.length) return '<span class="muted">无</span>';
  return items
    .map((item) => `<span class="chip-tag ${cls}">${h(item)}</span>`)
    .join("");
}

/** 表格骨架：传入列定义与行渲染器，统一转义责任在调用方。 */
export function renderTable(tbody, rows, columns, rowRenderer) {
  if (!rows || !rows.length) {
    tbody.innerHTML = `<tr><td colspan="${columns.length}"
      class="muted" style="padding:18px;text-align:center">没有符合条件的记录</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(rowRenderer).join("");
}
