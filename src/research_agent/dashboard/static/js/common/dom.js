/* 公共工具：DOM、转义、格式化。
 *
 * 合并方案 v2 §3.1：新增模块一律使用 `h()` 做 HTML 转义，禁止裸插值。
 * `h()` 等价于旧 app.js 的 `esc()`，两者可混用（同一套转义规则）。
 */
"use strict";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const ESCAPE_MAP = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

/** HTML 转义；所有进入 innerHTML 的动态值必须经过它。 */
export function h(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ESCAPE_MAP[c]);
}

/** 带引号属性的转义（与 h 相同规则，单独命名以表达意图）。 */
export const attr = h;

export function fmtTs(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? String(iso)
    : d.toLocaleTimeString("zh-CN", { hour12: false });
}

export function fmtFull(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? String(iso)
    : d.toLocaleString("zh-CN", { hour12: false });
}

export function fmtBytes(n) {
  if (n == null) return "-";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(2) + " MB";
}

export function trunc(text, n) {
  const s = String(text || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

export function fmtNumber(value, digits = 3) {
  if (value == null || value === "") return "-";
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(digits) : String(value);
}

export function basename(path) {
  const parts = String(path || "").split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

/** 简易防抖，用于搜索框 */
export function debounce(fn, wait = 250) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}
