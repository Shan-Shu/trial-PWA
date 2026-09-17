/* 文献库页：PWA 的"10 页工作流"里最完整的一页，重写为 PWA 形态：
 * 筛选矩阵（卡片）→ 批量操作条 → 表格 → 点击行进详情抽屉（四子页）。
 */
"use strict";

import { api, qs } from "../common/api.js";
import { $, h, fmtFull, fmtTs, fmtNumber, trunc } from "../common/dom.js";
import { toast, toastError } from "../common/ui.js";
import {
  card, metrics, badge, decisionBadge, statusBadge, progressRow, chips,
  empty, loading, errorBox, subtabs, bindSubtabs, openDrawer, setDrawerBody, kv,
} from "../ui/design.js";

const PAGE_SIZE = 50;
let host = null;
const filters = {
  q: "", status: "", source: "", decision: "", tag: "", folder_id: "",
  favorite: false, low_signal: "", year_mode: "", year_from: "", year_to: "",
  sort_by: "quality", sort_desc: true,
};
let offset = 0;
let facets = null;
let result = null;
const selection = new Set();

export const libraryPage = {
  title: "文献库",
  subtitle: "筛选 · 标签 · 批量抽取 · 引用导出",
  group: "资产",
  icon: "▤",

  mount(container) {
    host = container;
    host.innerHTML = `
      <div class="page-head">
        <h1>文献库</h1>
        <div class="desc">服务端筛选与分页；支持「年份未知」与「低信号入库」两个档位；
          批量操作走后台作业（可取消）。</div>
      </div>
      <div id="libStats"></div>
      <div id="libFilters"></div>
      <div id="libActions"></div>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th style="width:34px"><input type="checkbox" id="libSelAll"></th>
            <th style="width:44px">#</th>
            <th>标题</th><th>期刊 / 库</th>
            <th style="width:66px">年份</th>
            <th style="width:74px">Q</th>
            <th style="width:112px">决策</th>
            <th style="width:92px">状态</th>
            <th style="width:150px">标签</th>
            <th style="width:74px"></th>
          </tr></thead>
          <tbody id="libTbody"></tbody>
        </table>
      </div>
      <div class="row" style="margin-top:10px">
        <span id="libTotal" class="muted"></span>
        <span class="spacer" style="flex:1"></span>
        <button class="btn small ghost" id="libPrev">上一页</button>
        <span id="libPage" class="muted"></span>
        <button class="btn small ghost" id="libNext">下一页</button>
      </div>
      <div id="libJob" class="card flat hidden" style="margin-top:12px"></div>`;
    bindStatic();
  },

  async refresh() {
    if (!host) return;
    if (!facets) facets = await api.get("/api/library/facets");
    await Promise.all([renderStats(), load()]);
  },
};

/* ------------------------------------------------------------------ 统计与筛选 */

async function renderStats() {
  const box = $("#libStats");
  if (!box) return;
  try {
    const stats = await api.get("/api/library/stats");
    box.innerHTML = metrics([
      ["文献总数", stats.papers ?? 0, `收藏 ${stats.favorites ?? 0}`, "accent"],
      ["已抽取", stats.extracted ?? 0, `已评估 ${stats.assessed ?? 0}`, "violet"],
      ["需人工复核", stats.needs_review ?? 0, "", "red"],
      ["低信号入库", stats.low_signal ?? 0, "门控放行并标注", "amber"],
      ["年份未知", stats.unknown_year ?? 0, "可用筛选档位单独查看", "cyan"],
      ["标签 / 文件夹", `${stats.tags ?? 0} / ${stats.folders ?? 0}`, "", "green"],
    ]);
  } catch (err) {
    box.innerHTML = errorBox(err);
  }
}

function renderFilters() {
  const box = $("#libFilters");
  if (!box) return;
  const f = facets || {};
  box.innerHTML = card(`
    <div class="row">
      <input type="search" id="fQ" placeholder="标题 / 摘要 / 期刊 / DOI 搜索…"
             value="${h(filters.q)}" style="flex:1;min-width:220px" />
      <select id="fStatus"><option value="">全部状态</option>
        ${(f.statuses || []).map((s) => `<option value="${h(s.value)}" ${filters.status === s.value ? "selected" : ""}>${h(s.value)} (${h(s.count)})</option>`).join("")}
      </select>
      <select id="fSource"><option value="">全部来源</option>
        ${(f.sources || []).map((s) => `<option value="${h(s.value)}" ${filters.source === s.value ? "selected" : ""}>${h(s.value)} (${h(s.count)})</option>`).join("")}
      </select>
      <select id="fDecision"><option value="">全部决策</option>
        ${(f.decisions || []).map((d) => `<option value="${h(d.value)}" ${filters.decision === d.value ? "selected" : ""}>${h(d.value)} (${h(d.count)})</option>`).join("")}
      </select>
    </div>
    <div class="row" style="margin-top:8px">
      <select id="fTag"><option value="">全部标签</option>
        ${(f.tags || []).map((t) => `<option value="${h(t.tag)}" ${filters.tag === t.tag ? "selected" : ""}>${h(t.tag)} (${h(t.count)})</option>`).join("")}
      </select>
      <select id="fFolder"><option value="">全部文件夹</option>
        ${(f.folders || []).map((d) => `<option value="${h(d.folder_id)}" ${String(filters.folder_id) === String(d.folder_id) ? "selected" : ""}>${h(d.name)} (${h(d.count)})</option>`).join("")}
      </select>
      <select id="fSort">
        ${[["quality", "质量评分"], ["year", "发表年份"], ["citations", "被引次数"],
           ["title", "标题"], ["created", "入库时间"], ["source", "来源"]]
          .map(([v, label]) => `<option value="${h(v)}" ${filters.sort_by === v ? "selected" : ""}>${h(label)}</option>`).join("")}
      </select>
      <select id="fDir">
        <option value="desc" ${filters.sort_desc ? "selected" : ""}>降序</option>
        <option value="asc" ${!filters.sort_desc ? "selected" : ""}>升序</option>
      </select>
      <label class="switch"><input type="checkbox" id="fFav" ${filters.favorite ? "checked" : ""}/> 只看收藏</label>
      <label class="switch"><input type="checkbox" id="fLow" ${filters.low_signal === "yes" ? "checked" : ""}/> 只看低信号</label>
      <label class="switch"><input type="checkbox" id="fUnknown" ${filters.year_mode === "unknown" ? "checked" : ""}/> 只看年份未知${f.year_unknown ? ` (${h(f.year_unknown)})` : ""}</label>
      <input type="number" id="fYearFrom" placeholder="起始年" value="${h(filters.year_from)}" style="width:96px" />
      <input type="number" id="fYearTo" placeholder="结束年" value="${h(filters.year_to)}" style="width:96px" />
      <button class="btn small ghost" id="fReset">重置</button>
    </div>`,
    { title: "检索、筛选与排序", subtitle: "筛选下推 SQL，不受内存上限影响" });
  bindFilters();
}

function bindFilters() {
  const reload = () => { readFilters(); offset = 0; load(); };
  $("#fQ")?.addEventListener("keydown", (e) => { if (e.key === "Enter") reload(); });
  ["fStatus", "fSource", "fDecision", "fTag", "fFolder", "fSort", "fDir",
   "fFav", "fLow", "fUnknown", "fYearFrom", "fYearTo"].forEach((id) => {
    const el = $(`#${id}`);
    if (el) el.onchange = reload;
  });
  $("#fReset")?.addEventListener("click", () => {
    Object.assign(filters, {
      q: "", status: "", source: "", decision: "", tag: "", folder_id: "",
      favorite: false, low_signal: "", year_mode: "", year_from: "", year_to: "",
      sort_by: "quality", sort_desc: true,
    });
    selection.clear();
    offset = 0;
    renderFilters();
    load();
  });
}

function readFilters() {
  Object.assign(filters, {
    q: $("#fQ")?.value.trim() || "",
    status: $("#fStatus")?.value || "",
    source: $("#fSource")?.value || "",
    decision: $("#fDecision")?.value || "",
    tag: $("#fTag")?.value || "",
    folder_id: $("#fFolder")?.value || "",
    favorite: Boolean($("#fFav")?.checked),
    low_signal: $("#fLow")?.checked ? "yes" : "",
    year_mode: $("#fUnknown")?.checked ? "unknown" : "",
    year_from: $("#fYearFrom")?.value ? Number($("#fYearFrom").value) : "",
    year_to: $("#fYearTo")?.value ? Number($("#fYearTo").value) : "",
    sort_by: $("#fSort")?.value || "quality",
    sort_desc: ($("#fDir")?.value || "desc") === "desc",
  });
}

/* ------------------------------------------------------------------ 表格 */

async function load() {
  const tbody = $("#libTbody");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="10" class="muted" style="text-align:center;padding:20px">加载中…</td></tr>`;
  try {
    result = await api.get(`/api/library/papers${qs({
      ...filters, limit: PAGE_SIZE, offset,
    })}`);
    facets = result.facets || facets;
    renderFilters();
    renderRows();
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="10">${errorBox(err)}</td></tr>`;
  }
}

function renderRows() {
  const tbody = $("#libTbody");
  if (!tbody || !result) return;
  const items = result.items || [];
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="10" style="padding:22px">${empty("没有符合条件的文献")}</td></tr>`;
  } else {
    tbody.innerHTML = items.map((item, index) => {
      const checked = selection.has(item.paper_key);
      const tags = (item.tags || []).map((t) => `<span class="chip-tag">${h(t)}</span>`).join("");
      const folders = (item.folder_names || []).length
        ? `<div class="muted" style="font-size:11px">📁 ${h(item.folder_names.join(" / "))}</div>` : "";
      return `<tr data-key="${h(item.paper_key)}" class="${checked ? "row-selected" : ""}"
            style="${checked ? "background:rgba(59,130,246,.12)" : ""}">
        <td><input type="checkbox" class="rowCheck" data-key="${h(item.paper_key)}" ${checked ? "checked" : ""}/></td>
        <td class="muted">${offset + index + 1}</td>
        <td>
          <div style="font-weight:600">${h(trunc(item.title || "未命名文献", 76))}</div>
          <div class="muted" style="font-size:11px">
            ${h(item.paper_key)}${item.favorite ? " ★" : ""}
            ${item.low_signal ? ' <span class="badge b-amber">低信号</span>' : ""}
            ${item.fulltext_source ? ` · 全文:${h(item.fulltext_source)}` : ""}
          </div>
        </td>
        <td>${h(trunc(item.venue || item.source || "-", 30))}</td>
        <td>${item.pub_year == null ? '<span class="muted">未知</span>' : h(item.pub_year)}</td>
        <td>${item.quality == null ? '<span class="muted">-</span>' : h(fmtNumber(item.quality, 3))}</td>
        <td>${decisionBadge(item.decision)}</td>
        <td>${statusBadge(item.status)}</td>
        <td>${tags || '<span class="muted">-</span>'}${folders}</td>
        <td><button class="btn small ghost rowOpen" data-key="${h(item.paper_key)}">详情</button></td>
      </tr>`;
    }).join("");
  }
  tbody.querySelectorAll(".rowCheck").forEach((box) => {
    box.onchange = () => {
      if (box.checked) selection.add(box.dataset.key);
      else selection.delete(box.dataset.key);
      box.closest("tr").style.background = box.checked ? "rgba(59,130,246,.12)" : "";
      updateSelectionBar();
    };
  });
  tbody.querySelectorAll(".rowOpen").forEach((btn) => {
    btn.onclick = () => openDetail(btn.dataset.key);
  });
  const total = result.total || 0;
  const info = $("#libTotal");
  if (info) info.textContent = `共 ${total} 篇 · 显示 ${total ? offset + 1 : 0}-${Math.min(offset + PAGE_SIZE, total)}`;
  const page = $("#libPage");
  if (page) page.textContent = `第 ${Math.floor(offset / PAGE_SIZE) + 1} / ${Math.max(1, Math.ceil(total / PAGE_SIZE))} 页`;
  const all = $("#libSelAll");
  if (all) {
    all.checked = items.length > 0 && items.every((i) => selection.has(i.paper_key));
  }
  updateSelectionBar();
}

function updateSelectionBar() {
  const info = $("#libSel");
  if (info) info.textContent = selection.size ? `已选择 ${selection.size} 篇` : "未选择";
}

/* ------------------------------------------------------------------ 批量条 */

function renderActions() {
  const box = $("#libActions");
  if (!box) return;
  const folders = (facets?.folders || []);
  box.innerHTML = card(`
    <div class="row">
      <span id="libSel" class="muted" style="min-width:96px">未选择</span>
      <button class="btn small primary" id="bExtract">批量知识抽取</button>
      <input type="text" id="bTag" placeholder="标签名" style="width:118px" />
      <button class="btn small" id="bTagBtn">加标签</button>
      <select id="bFolder"><option value="">选择文件夹…</option>
        ${folders.map((f) => `<option value="${h(f.folder_id)}">${h(f.name)}</option>`).join("")}
      </select>
      <button class="btn small" id="bFolderBtn">移入</button>
      <button class="btn small" id="bFav">收藏</button>
      <button class="btn small ghost" id="bUnfav">取消收藏</button>
      <span class="spacer" style="flex:1"></span>
      <select id="bStyle">
        <option value="gb7714">GB/T 7714</option><option value="apa">APA</option>
        <option value="acs">ACS</option><option value="bibtex">BibTeX</option>
        <option value="ris">RIS</option><option value="markdown">Markdown</option>
      </select>
      <button class="btn small" id="bExport">导出选中</button>
      <button class="btn small ghost" id="bNewFolder">＋ 文件夹</button>
      <button class="btn small ghost danger" id="bDelete">删除</button>
    </div>`, { flat: true });
  bindActions();
}

function bindActions() {
  const keys = () => [...selection];
  const guard = (fn) => async () => {
    if (!keys().length) { toast("请先选择文献", "warn"); return; }
    try { await fn(keys()); } catch (err) { toastError(err); }
  };

  $("#bExtract")?.addEventListener("click", guard(async (k) => {
    const res = await api.post("/api/library/batch", { action: "extract_knowledge", paper_keys: k });
    watchJob(res);
  }));
  $("#bTagBtn")?.addEventListener("click", guard(async (k) => {
    const tag = $("#bTag")?.value.trim();
    if (!tag) { toast("请输入标签名", "warn"); return; }
    watchJob(await api.post("/api/library/batch", { action: "tag", paper_keys: k, payload: { tag } }));
  }));
  $("#bFolderBtn")?.addEventListener("click", guard(async (k) => {
    const folderId = $("#bFolder")?.value;
    if (!folderId) { toast("请选择文件夹", "warn"); return; }
    const res = await api.post(`/api/library/folders/${folderId}/papers`, { paper_keys: k });
    toast(`已移入 ${res.changed ?? 0} 篇`);
    await refresh();
  }));
  $("#bFav")?.addEventListener("click", guard(async (k) => {
    const res = await api.post("/api/library/favorites", { paper_keys: k, value: true });
    toast(`已收藏 ${res.changed ?? 0} 篇`);
    await refresh();
  }));
  $("#bUnfav")?.addEventListener("click", guard(async (k) => {
    const res = await api.post("/api/library/favorites", { paper_keys: k, value: false });
    toast(`已取消收藏 ${res.changed ?? 0} 篇`);
    await refresh();
  }));
  $("#bDelete")?.addEventListener("click", guard(async (k) => {
    if (!window.confirm(`确认删除选中的 ${k.length} 篇文献及其标签/收藏/文件夹关系？`)) return;
    watchJob(await api.post("/api/library/batch", { action: "delete", paper_keys: k }));
  }));
  $("#bExport")?.addEventListener("click", guard(async (k) => {
    const style = $("#bStyle")?.value || "gb7714";
    const res = await api.get(`/api/library/export${qs({ keys: k.join(","), style })}`);
    if (!res.ok) throw new Error(res.error || "导出失败");
    download(res.filename, res.content, res.mime);
    toast(res.missing?.length
      ? `已导出 ${res.count} 篇；${res.missing.length} 个 key 不存在`
      : `已导出 ${res.count} 篇（${style}）`);
  }));
  $("#bNewFolder")?.addEventListener("click", async () => {
    const name = window.prompt("新文件夹名称");
    if (!name) return;
    try {
      const res = await api.post("/api/library/folders", { name });
      if (!res.ok) throw new Error(res.error || "创建失败");
      toast(`已创建「${res.name}」`);
      facets = await api.get("/api/library/facets");
      renderFilters();
      renderActions();
    } catch (err) { toastError(err); }
  });
}

async function watchJob(started) {
  if (!started || !started.ok) { toast(started?.error || "提交失败", "error"); return; }
  const box = $("#libJob");
  if (box) box.classList.remove("hidden");
  const tick = async () => {
    const snap = await api.get(`/api/library/jobs/${started.job_id}`);
    if (!snap) { if (box) box.innerHTML = `<span class="muted">作业状态不可用</span>`; return; }
    if (box) {
      box.innerHTML = `<div class="row">
        <span style="font-weight:650">${h(snap.action)}</span>
        ${badge(snap.status, snap.status === "done" ? "green" : snap.status === "error" ? "red" : "blue")}
        <span class="muted">${h(snap.percent)}% · ${h(snap.message || "")} · ${h(snap.done)}/${h(snap.total)}</span>
        <span class="spacer" style="flex:1"></span>
        <button class="btn small ghost" id="libJobCancel">取消</button>
      </div>`;
      $("#libJobCancel")?.addEventListener("click", async () => {
        await api.post(`/api/library/jobs/${started.job_id}/cancel`, {});
        toast("已请求取消");
      });
    }
    if (["done", "error", "cancelled"].includes(snap.status)) {
      const r = snap.result || {};
      toast(snap.status === "done"
        ? `作业完成：成功 ${r.ok ?? r.done ?? "-"} / 共 ${r.total ?? "-"}`
        : snap.status === "cancelled" ? "作业已取消" : `作业失败：${snap.error || ""}`,
        snap.status === "error" ? "error" : "info");
      if (box) setTimeout(() => box.classList.add("hidden"), 4000);
      selection.clear();
      await refresh();
      return;
    }
    setTimeout(tick, 700);
  };
  tick();
}

function download(filename, content, mime) {
  const blob = new Blob([content], { type: mime || "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || "export.txt";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/* ------------------------------------------------------------------ 详情抽屉（四子页） */

async function openDetail(paperKey) {
  openDrawer(paperKey, loading("正在读取文献详情…"));
  try {
    const [sidecar, citation] = await Promise.all([
      api.get(`/api/library/paper/${encodeURIComponent(paperKey)}`),
      api.get(`/api/library/citation/${encodeURIComponent(paperKey)}`),
    ]);
    const detail = sidecar.detail || {};
    const paper = detail.paper || {};
    setDrawerBody(`
      <div style="font-size:15px;font-weight:700;margin-bottom:4px">${h(paper.title || "未命名文献")}</div>
      <div class="muted" style="font-size:11.5px;margin-bottom:12px">
        ${h(paper_key(paper, paperKey))}</div>
      ${subtabs("paper", [
        { key: "meta", label: "元数据", body: metaPane(paper) },
        { key: "quality", label: "质量报告", body: qualityPane(detail) },
        { key: "cite", label: "引用与素材", body: citePane(citation, paperKey) },
        { key: "text", label: "正文与日志", body: textPane(detail) },
      ])}`);
    const drawer = document.getElementById("drawer");
    bindSubtabs(drawer);
    bindDetail(paperKey, sidecar, citation);
  } catch (err) {
    setDrawerBody(errorBox(err));
  }
}

function paper_key(paper, fallback) {
  return paper.paper_key || fallback;
}

function metaPane(paper) {
  const authors = (paper.authors || [])
    .map((a) => (typeof a === "string" ? a : a.name || ""))
    .filter(Boolean);
  return `
    ${kv([
      ["来源", paper.source], ["来源类型", paper.source_type],
      ["期刊 / 库", paper.venue], ["ISSN", paper.venue_issn],
      ["年份", paper.pub_year], ["发表日期", paper.pub_date],
      ["出版状态", paper.publication_status], ["DOI", paper.doi],
      ["PMCID", paper.pmcid],
      ["卷 / 期 / 页", [paper.volume, paper.issue, paper.pages].filter(Boolean).join(" / ")],
      ["被引", paper.citation_count],
      ["平均 H 指数", paper.avg_h_index == null ? null : fmtNumber(paper.avg_h_index, 2)],
      ["全文来源", paper.fulltext_source],
      ["PDF 大小", paper.pdf_size == null ? null : `${paper.pdf_size} B`],
      ["精校文本", paper.clean_chars ? `${paper.clean_chars} 字符` : "无"],
      ["入库", fmtFull(paper.created_at)],
    ])}
    ${authors.length ? `<div class="section-title">作者</div><div>${chips(authors)}</div>` : ""}
    ${paper.abstract ? `<div class="section-title">摘要</div><div style="font-size:12.5px">${h(paper.abstract)}</div>` : ""}`;
}

function qualityPane(detail) {
  const q = detail.quality || {};
  if (q.quality == null) return empty("该文献尚未完成质量评估");
  const dimensionsHtml = [
    ["权威性 A", q.authority], ["时效性 T", q.timeliness],
    ["综合 Q", q.quality], ["期刊因子", q.venue_factor],
    ["H 指数因子", q.h_factor], ["被引因子", q.citation_factor],
  ];
  return `
    <div class="row" style="margin-bottom:10px">${decisionBadge(q.decision)}
      ${q.needs_review ? badge("需要人工复核", "amber") : badge("无需复核", "green")}
      <span class="muted" style="font-size:11.5px">评估于 ${h(fmtTs(q.assessed_at))}</span></div>
    ${dimensionsHtml.map(([label, value]) =>
      progressRow(label, value, value == null ? "-" : Number(value).toFixed(3))).join("")}
    <div class="muted" style="font-size:11.5px;margin-top:8px">
      公式：A = 0.5·期刊分区 + 0.3·H 指数 + 0.2·被引；T 按学科半衰期衰减；Q = 0.6A + 0.4T；
      阈值 直接 ≥0.8 / 标记 ≥0.5 / 低于则转人工。</div>
    ${(q.meta_missing || []).length
      ? `<div class="section-title">缺失元数据</div>${chips(q.meta_missing.map(
          (m) => ({ authors: "作者", affiliations: "作者机构", publication: "出版信息", doi: "DOI" }[m] || m)))}`
      : ""}
    ${q.rationale ? `<div class="section-title">说明</div><div style="font-size:12.5px">${h(q.rationale)}</div>` : ""}`;
}

function citePane(citation, paperKey) {
  const styles = (citation.styles || []).filter((s) => s !== "markdown");
  const rendered = citation.rendered || {};
  return `
    <div class="row" style="margin-bottom:8px">
      <select id="dCiteStyle">
        ${styles.map((s) => `<option value="${h(s)}" ${s === citation.style ? "selected" : ""}>${h(s)}</option>`).join("")}
      </select>
      <button class="btn small" id="dCopy">复制</button>
      <button class="btn small ghost" id="dDownload">下载</button>
      <span class="muted" style="font-size:11.5px">期刊缩写来自 packs 的 venue_overrides</span>
    </div>
    <div class="pre" id="dCiteText">${h(citation.text || "")}</div>
    <details style="margin-top:10px"><summary class="muted" style="cursor:pointer">全部样式</summary>
      <div class="pre" style="margin-top:6px">${h(Object.entries(rendered)
        .map(([k, v]) => `[${k}]\n${v}`).join("\n\n"))}</div></details>
    <div class="section-title">素材编号（写作台引用用）</div>
    <div class="muted" style="font-size:11.5px">引用样式默认 <b>${h(citation.style || "-")}</b>；
      写作台生成的正文要求引用写成 <code>[编号]</code>，编号对应知识库素材顺序。</div>`;
}

function textPane(detail) {
  const paper = detail.paper || {};
  const logs = detail.logs || [];
  return `
    ${paper.clean_preview
      ? `<div class="section-title" style="margin-top:0">精校正文（前 ${paper.clean_preview.length} 字符）</div>
         <div class="pre">${h(paper.clean_preview)}</div>`
      : empty("该文献没有精校正文（全文来源为摘要）")}
    <div class="section-title">处理日志（最近 ${logs.length} 条）</div>
    ${logs.length ? `<div class="stack">${logs.slice(0, 30).map((l) => `
      <div class="row" style="justify-content:space-between;font-size:12px;border-bottom:1px dashed var(--border);padding:3px 0">
        <span>${badge(l.node)} ${h(l.event)}</span>
        <span class="muted">${h(fmtFull(l.ts))}</span>
      </div>`).join("")}</div>` : `<span class="muted">无日志</span>`}`;
}

function bindDetail(paperKey, sidecar, citation) {
  $("#dCiteStyle")?.addEventListener("change", async (e) => {
    try {
      const data = await api.get(`/api/library/citation/${encodeURIComponent(paperKey)}?style=${encodeURIComponent(e.target.value)}`);
      const box = $("#dCiteText");
      if (box) box.textContent = data.ok ? data.text : (data.error || "不可用");
    } catch (err) { toastError(err); }
  });
  $("#dCopy")?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#dCiteText")?.textContent || "");
      toast("已复制引用");
    } catch { toast("浏览器拒绝剪贴板访问，请手动复制", "warn"); }
  });
  $("#dDownload")?.addEventListener("click", async () => {
    try {
      const style = $("#dCiteStyle")?.value || "gb7714";
      const res = await api.get(`/api/library/export?keys=${encodeURIComponent(paperKey)}&style=${encodeURIComponent(style)}`);
      if (!res.ok) throw new Error(res.error || "导出失败");
      download(res.filename, res.content, res.mime);
    } catch (err) { toastError(err); }
  });
}

function bindStatic() {
  $("#libPrev")?.addEventListener("click", () => {
    offset = Math.max(0, offset - PAGE_SIZE);
    load();
  });
  $("#libNext")?.addEventListener("click", () => {
    if (offset + PAGE_SIZE < (result?.total || 0)) {
      offset += PAGE_SIZE;
      load();
    }
  });
  $("#libSelAll")?.addEventListener("change", (e) => {
    const on = e.target.checked;
    (result?.items || []).forEach((item) => {
      if (on) selection.add(item.paper_key);
      else selection.delete(item.paper_key);
    });
    renderRows();
  });
  renderActions();
}
