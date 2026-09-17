/* 应用入口：装配 PWA 形态的外壳与 10 个页面。
 *
 * 与旧前端的关系：原 6-tab 的 app.js 已不再加载（其图谱/文献/审核/规划能力
 * 已分别由 pages/ontology.js、library.js、review.js、search.js 重写并增强）。
 */
"use strict";

import { bootstrap } from "./pages/shell.js";
import { $, h } from "./common/dom.js";
import { toast } from "./common/ui.js";
import { closeDrawer } from "./ui/design.js";

function start() {
  const host = $("#pageHost");
  if (!host) {
    console.error("[app] 缺少 #pageHost 容器");
    return;
  }
  bootstrap().catch((err) => {
    console.error("[app] 启动失败", err);
    host.innerHTML = `<div class="card"><h3 style="color:var(--red)">启动失败</h3>
      <div class="muted">${h(err && err.message ? err.message : String(err))}</div></div>`;
  });
  // 快捷键：Esc 关闭抽屉（design.js 已处理），r 刷新当前页
  document.addEventListener("keydown", (e) => {
    if (e.key === "r" && !e.metaKey && !e.ctrlKey
        && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) {
      $("#refreshBtn")?.click();
    }
  });
  window.__dashboard = { toast, closeDrawer, refresh: () => $("#refreshBtn")?.click() };
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}
