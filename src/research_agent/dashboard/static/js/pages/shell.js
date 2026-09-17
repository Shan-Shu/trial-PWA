/* ============================================================================
 * 应用外壳：左侧页面导航 + 顶栏 + 路由
 * 形态取自 PWA（侧边 10 页 + 顶栏状态条），页面内容来自 trial 引擎的 API。
 * ========================================================================== */
"use strict";

import { api } from "../common/api.js";
import { $, $$, h, basename } from "../common/dom.js";
import { toast, toastError } from "../common/ui.js";
import { closeDrawer, loading, errorBox } from "../ui/design.js";
import { PAGES, DEFAULT_PAGE, pageOf } from "./registry.js";

let current = DEFAULT_PAGE;
let currentPage = null;
let autoTimer = null;

/** 侧边栏徽章：页面 key -> 数值/文本；返回 null 表示不显示 */
const BADGES = {};

export async function bootstrap() {
  renderSidebar();
  bindTopbar();
  window.addEventListener("hashchange", () => {
    const key = location.hash.replace("#", "") || DEFAULT_PAGE;
    navigate(key, { push: false });
  });
  const initial = location.hash.replace("#", "") || DEFAULT_PAGE;
  await navigate(initial, { push: false });
  startAutoRefresh();
}

/* ------------------------------------------------------------------ 侧边栏 */

function renderSidebar() {
  const nav = $("#nav");
  if (!nav) return;
  const groups = new Map();
  PAGES.forEach((entry) => {
    if (!groups.has(entry.group)) groups.set(entry.group, []);
    groups.get(entry.group).push(entry);
  });
  nav.innerHTML = [...groups.entries()].map(([group, items]) => `
    <div class="nav-group-label">${h(group)}</div>
    ${items.map((entry) => {
      const page = entry.page;
      const count = BADGES[entry.key];
      return `<div class="nav-item" data-page="${h(entry.key)}">
        <span class="ico">${h(page.icon || "•")}</span>
        <span>${h(page.title)}</span>
        ${count ? `<span class="badge-count">${h(count)}</span>` : ""}
      </div>`;
    }).join("")}
  `).join("");
  nav.querySelectorAll("[data-page]").forEach((el) => {
    el.onclick = () => navigate(el.dataset.page);
  });
}

function markActive(key) {
  $$("#nav .nav-item").forEach((el) =>
    el.classList.toggle("active", el.dataset.page === key));
}

/** 更新侧栏徽章（例如待人工审核数） */
export function setBadge(key, value) {
  BADGES[key] = value;
  renderSidebar();
  markActive(current);
}

/* ------------------------------------------------------------------ 顶栏 */

function bindTopbar() {
  $("#refreshBtn")?.addEventListener("click", () => refreshCurrent({ silent: false }));
  $("#autoRefresh")?.addEventListener("change", (e) => {
    if (e.target.checked) startAutoRefresh();
    else stopAutoRefresh();
  });
  loadDatabaseList();
}

async function loadDatabaseList() {
  const select = $("#dbSelect");
  if (!select) return;
  try {
    const [list, health] = await Promise.all([
      api.get("/api/databases"),
      api.get("/api/health"),
    ]);
    if (!list.some((d) => d.path === health.db)) {
      list.unshift({ path: health.db, name: basename(health.db), papers: 0, nodes: 0, edges: 0 });
    }
    select.innerHTML = list.map((d) => `
      <option value="${h(d.path)}" ${d.path === health.db ? "selected" : ""}>
        ${h(d.name || basename(d.path))} · P${h(d.papers ?? 0)}/N${h(d.nodes ?? 0)}
      </option>`).join("");
    const label = $("#dbLabel");
    if (label) label.textContent = basename(health.db);
    select.onchange = async () => {
      try {
        const res = await api.post("/api/db/select", { path: select.value });
        if (!res.ok) throw new Error(res.error || "切换失败");
        toast(`已切换到 ${basename(select.value)}`);
        $("#dbLabel").textContent = basename(select.value);
        await refreshCurrent({ silent: false });
      } catch (err) { toastError(err); }
    };
  } catch (err) {
    console.warn("数据库列表加载失败", err);
  }
}

/* ------------------------------------------------------------------ 路由 */

export async function navigate(key, { push = true } = {}) {
  const page = pageOf(key);
  const actualKey = PAGES.find((p) => p.page === page)?.key || DEFAULT_PAGE;
  current = actualKey;
  if (push && location.hash !== `#${actualKey}`) location.hash = `#${actualKey}`;
  markActive(actualKey);
  closeDrawer();

  const titleEl = $("#pageTitle");
  if (titleEl) titleEl.textContent = page.title || actualKey;
  const subEl = $("#pageSub");
  if (subEl) subEl.textContent = page.subtitle || "";

  const main = $("#pageHost");
  if (!main) return;
  // 离开页面前给上一页清理机会（写作台的作业轮询若不停，会在后台一直打接口）
  if (currentPage && currentPage !== page && typeof currentPage.unmount === "function") {
    try { currentPage.unmount(); } catch (err) { console.error("页面卸载失败", err); }
  }
  if (page.mount) page.mount(main);
  currentPage = page;
  await refreshCurrent({ silent: false });
  return actualKey;
}

async function refreshCurrent({ silent = true } = {}) {
  const page = pageOf(current);
  const main = $("#pageHost");
  if (!main || !page.refresh) return;
  try {
    await page.refresh();
    const stamp = $("#lastRefresh");
    if (stamp) stamp.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    await refreshSidebarBadges();
  } catch (err) {
    if (!silent) main.innerHTML = errorBox(err);
    console.error("页面刷新失败", current, err);
  }
}

/** 侧栏徽章所需的轻量统计（只在关键页刷新，避免打爆接口） */
async function refreshSidebarBadges() {
  try {
    const [overview, stats] = await Promise.all([
      api.get("/api/overview"),
      api.get("/api/library/stats").catch(() => null),
    ]);
    const qd = overview.quality_decisions || {};
    const badges = {
      review: qd.human || 0,
      library: stats ? (stats.papers || 0) : (overview.papers || 0),
      ontology: overview.ontology?.hyperedges || 0,
    };
    let changed = false;
    Object.entries(badges).forEach(([key, value]) => {
      if (BADGES[key] !== value) { BADGES[key] = value; changed = true; }
    });
    if (changed) {
      renderSidebar();
      markActive(current);
    }
  } catch (err) {
    /* 徽章失败不影响主流程 */
  }
}

function startAutoRefresh() {
  stopAutoRefresh();
  const box = $("#autoRefresh");
  if (box && !box.checked) return;
  autoTimer = setInterval(() => {
    if (!document.hidden) refreshCurrent({ silent: true });
  }, 6000);
}

function stopAutoRefresh() {
  if (autoTimer) clearInterval(autoTimer);
  autoTimer = null;
}

export function currentPageKey() {
  return current;
}
