/* 知识抽取页：批量抽取（后台作业）+ 逐篇抽取结果核查。
 * 数据来源：/api/papers（含 extracted 计数与 run 统计）+ 批量作业端点。
 */
"use strict";

import { api, qs } from "../common/api.js";
import { $, h, fmtFull, fmtNumber, trunc } from "../common/dom.js";
import { card, metrics, badge, statusBadge, empty, loading, errorBox, openDrawer, setDrawerBody, kv } from "../ui/design.js";
import { toast, toastError } from "../common/ui.js";

let host = null;
let papers = [];
let jobId = null;
let timer = null;

export const knowledgePage = {
  title: "知识抽取",
  subtitle: "批量作业 · 单篇核查 · 失败可见",
  group: "资产",
  icon: "◍",

  mount(container) {
    host = container;
  },

  async refresh() {
    if (!host) return;
    if (!papers.length) host.innerHTML = loading("正在读取抽取状态…");
    try {
      papers = await api.get("/api/papers");
      host.innerHTML = render();
      bind();
    } catch (err) {
      host.innerHTML = errorBox(err);
    }
  },
};

function render() {
  const extracted = papers.filter((p) => (p.extracted?.entities || 0) > 0).length;
  const failed = papers.filter((p) => p.status === "extract_failed").length;
  const totalEntities = papers.reduce((sum, p) => sum + (p.extracted?.entities || 0), 0);
  const totalRelations = papers.reduce((sum, p) => sum + (p.extracted?.relations || 0), 0);
  const totalEvents = papers.reduce((sum, p) => sum + (p.extracted?.events || 0), 0);

  const rows = papers.map((p) => {
    const ex = p.extracted || {};
    const done = (ex.entities || 0) > 0;
    return `<tr data-key="${h(p.paper_key)}" style="cursor:pointer">
      <td>${statusBadge(p.status)}</td>
      <td><div style="font-weight:600">${h(trunc(p.title || "未命名", 62))}</div>
        <div class="muted" style="font-size:11px">${h(p.paper_key)}</div></td>
      <td class="muted" style="font-size:12px">${h(trunc(p.venue || "-", 24))}</td>
      <td>${p.pub_year == null ? '<span class="muted">未知</span>' : h(p.pub_year)}</td>
      <td>${done ? badge(`${ex.entities} 实体`, "green") : badge("未抽取", "gray")}</td>
      <td>${h(ex.relations ?? 0)}</td>
      <td>${h(ex.events ?? 0)}</td>
      <td>${p.run?.run_at ? `<span class="muted" style="font-size:11px">${h(fmtFull(p.run.run_at))}</span>` : "-"}</td>
    </tr>`;
  }).join("");

  return `
  <div class="page-head">
    <h1>知识抽取</h1>
    <div class="desc">无模型或模型失败时后端会显式标记 <code>extract_failed</code>（不再伪装成"0 实体"）；
      批量抽取走后台作业，可取消。</div>
  </div>
  ${metrics([
    ["已抽取文献", extracted, `共 ${papers.length} 篇`, "green"],
    ["抽取失败", failed, failed ? "见下方状态列" : "无", failed ? "red" : "green"],
    ["实体总数", totalEntities, "", "violet"],
    ["关系总数", totalRelations, "", "cyan"],
    ["事件总数", totalEvents, "", "amber"],
  ])}
  ${card(`
    <div class="row">
      <select id="knFilter">
        <option value="">全部文献</option>
        <option value="todo">仅未抽取</option>
        <option value="failed">仅抽取失败</option>
      </select>
      <button class="btn small primary" id="knBatch">批量抽取（未抽取的）</button>
      <span id="knJobStatus" class="muted" style="font-size:12px"></span>
      <span style="flex:1"></span>
      <span class="muted" style="font-size:11.5px">单篇抽取请到「文献库」批量操作里勾选</span>
    </div>`, { flat: true })}
  <div class="table-wrap" style="margin-top:12px;max-height:560px">
    <table>
      <thead><tr>
        <th style="width:104px">状态</th><th>标题</th><th style="width:170px">期刊</th>
        <th style="width:66px">年份</th><th style="width:104px">抽到实体</th>
        <th style="width:56px">关系</th><th style="width:56px">事件</th>
        <th style="width:150px">抽取时间</th>
      </tr></thead>
      <tbody id="knTbody">${rows || `<tr><td colspan="8">${empty("库中暂无文献")}</td></tr>`}</tbody>
    </table>
  </div>`;
}

function bind() {
  $("#knFilter")?.addEventListener("change", (e) => {
    const mode = e.target.value;
    const tbody = $("#knTbody");
    if (!tbody) return;
    const list = papers.filter((p) => {
      const done = (p.extracted?.entities || 0) > 0;
      if (mode === "todo") return !done;
      if (mode === "failed") return p.status === "extract_failed";
      return true;
    });
    tbody.innerHTML = list.map((p) => {
      const ex = p.extracted || {};
      const done = (ex.entities || 0) > 0;
      return `<tr data-key="${h(p.paper_key)}" style="cursor:pointer">
        <td>${statusBadge(p.status)}</td>
        <td><div style="font-weight:600">${h(trunc(p.title || "未命名", 62))}</div>
          <div class="muted" style="font-size:11px">${h(p.paper_key)}</div></td>
        <td class="muted" style="font-size:12px">${h(trunc(p.venue || "-", 24))}</td>
        <td>${p.pub_year == null ? '<span class="muted">未知</span>' : h(p.pub_year)}</td>
        <td>${done ? badge(`${ex.entities} 实体`, "green") : badge("未抽取", "gray")}</td>
        <td>${h(ex.relations ?? 0)}</td><td>${h(ex.events ?? 0)}</td>
        <td>${p.run?.run_at ? `<span class="muted" style="font-size:11px">${h(fmtFull(p.run.run_at))}</span>` : "-"}</td>
      </tr>`;
    }).join("") || `<tr><td colspan="8">${empty("没有匹配的文献")}</td></tr>`;
    bindRows();
  });
  $("#knBatch")?.addEventListener("click", startBatch);
  bindRows();
}

function bindRows() {
  document.querySelectorAll("#knTbody [data-key]").forEach((tr) => {
    tr.onclick = () => openPaper(tr.dataset.key);
  });
}

async function startBatch() {
  const keys = papers
    .filter((p) => (p.extracted?.entities || 0) === 0)
    .map((p) => p.paper_key);
  if (!keys.length) { toast("没有待抽取的文献", "warn"); return; }
  try {
    const res = await api.post("/api/library/batch",
      { action: "extract_knowledge", paper_keys: keys });
    if (!res.ok) throw new Error(res.error || "提交失败");
    jobId = res.job_id;
    toast(`已提交 ${keys.length} 篇的抽取作业`);
    poll();
  } catch (err) { toastError(err); }
}

function poll() {
  clearTimeout(timer);
  const tick = async () => {
    if (!jobId) return;
    try {
      const snap = await api.get(`/api/library/jobs/${jobId}`);
      const label = $("#knJobStatus");
      if (label && snap) {
        label.textContent = `${snap.status} · ${snap.percent}% · ${snap.message || ""} · ${snap.done}/${snap.total}`;
      }
      if (!snap || ["done", "error", "cancelled"].includes(snap.status)) {
        if (snap?.status === "done") toast("批量抽取完成");
        else if (snap?.status === "error") toast(`抽取失败：${snap.error || ""}`, "error");
        jobId = null;
        await knowledgePage.refresh();
        return;
      }
      timer = setTimeout(tick, 900);
    } catch (err) {
      toastError(err);
      jobId = null;
    }
  };
  tick();
}

async function openPaper(key) {
  openDrawer(key, loading("读取抽取结果…"));
  try {
    const [detail, knowledge] = await Promise.all([
      api.get(`/api/papers/${encodeURIComponent(key)}`),
      api.get(`/api/library/paper/${encodeURIComponent(key)}`).catch(() => null),
    ]);
    const paper = detail.paper || {};
    const run = detail.run || {};
    const counts = run.counts?.extracted || {};
    const newTypes = run.new_types || [];
    setDrawerBody(`
      <div style="font-size:15px;font-weight:700">${h(paper.title || "未命名文献")}</div>
      <div class="muted" style="font-size:11.5px;margin-bottom:10px">${h(key)}</div>
      <div class="row" style="margin-bottom:10px">
        ${statusBadge(paper.status)}
        ${counts.entities ? badge(`${counts.entities} 实体`, "green") : badge("未抽取", "gray")}
        ${badge(`${counts.relations ?? 0} 关系`)}
        ${badge(`${counts.events ?? 0} 事件`, "amber")}
      </div>
      ${kv([
        ["抽取时间", run.run_at ? fmtFull(run.run_at) : "-"],
        ["本体版本", run.ontology_version],
        ["新增节点 / 边", `${run.counts?.new_nodes ?? "-"} / ${run.counts?.new_edges ?? "-"}`],
        ["全文来源", paper.fulltext_source],
        ["精校文本", paper.clean_chars ? `${paper.clean_chars} 字符` : "无"],
      ])}
      ${newTypes.length ? `<div class="section-title">本次新增类型</div>
        <div class="row">${newTypes.map((t) => `<span class="chip-tag on">${h(t)}</span>`).join("")}</div>` : ""}
      <div class="section-title">处理日志</div>
      ${(detail.logs || []).length ? `<div class="stack">${detail.logs.slice(0, 20).map((l) => `
        <div class="row" style="justify-content:space-between;font-size:12px">
          <span>${badge(l.node)} ${h(l.event)}</span>
          <span class="muted">${h(fmtFull(l.ts))}</span>
        </div>`).join("")}</div>` : empty("无日志")}
      ${paper.clean_preview ? `<div class="section-title">精校正文（前 2000 字符）</div>
        <div class="pre">${h(paper.clean_preview.slice(0, 2000))}</div>` : ""}`);
  } catch (err) {
    setDrawerBody(errorBox(err));
  }
}
