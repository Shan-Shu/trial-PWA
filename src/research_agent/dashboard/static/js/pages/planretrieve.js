/* 规划与检索页：Planner 契约 + 执行后的知识包（合并原 search.js 与 retrieval.js）。
 *
 * 为什么合成一页：规划产出契约、执行产出知识包，本就是同一条链的两端。
 * 原 trial 前端把「规划指令」单列一页，而知识包只能看接口，两头都不完整。
 */
"use strict";

import { api } from "../common/api.js";
import { $, h, fmtFull, fmtNumber, trunc } from "../common/dom.js";
import {
  card, metrics, badge, statusBadge, chips, empty, loading, errorBox, kv,
  openDrawer, setDrawerBody,
} from "../ui/design.js";
import { toast, toastError } from "../common/ui.js";

let host = null;
let lastPlan = null;
let ontology = null;
let activeTab = "contract";
let typeFilter = "";

export const planretrievePage = {
  title: "规划与检索",
  subtitle: "Planner 任务契约 → 执行 → 知识包",
  group: "研究入口",
  icon: "◎",

  mount(container) {
    host = container;
    host.innerHTML = shell();
    bindShell();
    paintContract();
  },

  async refresh() {
    // 进入页面时刷新知识包（契约保留上次结果，避免每次重算）
    await loadOntology();
    if (activeTab === "knowledge") paintKnowledge();
  },
};

/* ------------------------------------------------------------------ 页面骨架 */

function shell() {
  return `
  <div class="page-head">
    <h1>规划与检索</h1>
    <div class="desc">Planner 是唯一入口：先生成 <code>retrieval_plan</code> /
      <code>design_contract</code> / <code>stop_conditions</code>，未规划不检索；
      执行后可在「知识包」子页查看抽出的超边与量化条件。</div>
  </div>
  <div class="grid-2">
    ${card(`
      <textarea id="prRequest" rows="5"
        placeholder="示例：尝试提出一种炔酰胺合成多元氮杂化合物的新方法，必须含两个以上环内氮原子"></textarea>
      <div class="row" style="margin-top:10px">
        <button class="btn primary" id="prPlan">发送给规划节点</button>
        <button class="btn" id="prRun">后台运行完整研究流程</button>
        <button class="btn ghost" id="prClear">清空</button>
      </div>
      <div class="muted" style="font-size:11.5px;margin-top:8px">
        「发送给规划节点」只产出任务契约（快、不检索）；「后台运行完整研究流程」会走
        collection → 消费 → 候选 → 审核 → 事实核查，状态见「研究流程」页。
      </div>`, { title: "研究需求", subtitle: "自然语言即可，无需手写检索式" })}
    <div>
      <div class="subtabs" id="prTabs">
        <button class="subtab active" data-tab="contract">任务契约</button>
        <button class="subtab" data-tab="knowledge">知识包</button>
      </div>
      <div id="prContractPane"></div>
      <div id="prKnowledgePane" class="hidden"></div>
    </div>
  </div>`;
}

function bindShell() {
  $("#prPlan")?.addEventListener("click", () => submit(false));
  $("#prRun")?.addEventListener("click", () => submit(true));
  $("#prClear")?.addEventListener("click", () => {
    const box = $("#prRequest");
    if (box) box.value = "";
    lastPlan = null;
    paintContract();
  });
  document.querySelectorAll("#prTabs [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      activeTab = btn.dataset.tab;
      document.querySelectorAll("#prTabs [data-tab]").forEach((b) =>
        b.classList.toggle("active", b === btn));
      $("#prContractPane")?.classList.toggle("hidden", activeTab !== "contract");
      $("#prKnowledgePane")?.classList.toggle("hidden", activeTab !== "knowledge");
      if (activeTab === "knowledge") paintKnowledge();
    };
  });
}

/* ------------------------------------------------------------------ 规划 */

async function submit(runPipeline) {
  const text = $("#prRequest")?.value.trim();
  if (!text) { toast("请先输入研究需求", "warn"); return; }
  const btn = runPipeline ? $("#prRun") : $("#prPlan");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "执行中…";
  try {
    const res = await api.post(runPipeline ? "/api/study/run" : "/api/planner/run",
                               { request: text });
    if (!res.ok) {
      lastPlan = null;
      paintContract(errorBox(new Error(res.error || "执行失败")));
      return;
    }
    if (runPipeline) {
      toast(`研究流程已后台提交（job ${res.job_id}）`);
      paintContract(card(`
        <div class="row">${badge("已提交", "green")}
          <span class="muted">job ${h(res.job_id)}</span></div>
        <div class="muted" style="font-size:12px;margin-top:8px">
          六节点链在后台运行：Planner → collection → 知识消费 → 内容形成 → 审核 → 事实核查。
          请到「研究流程」页查看节点状态与 <code>study_runs</code> 中间产物。</div>`,
        { flat: true }));
      switchTo("knowledge");
    } else {
      lastPlan = res.plan || {};
      paintContract();
    }
  } catch (err) {
    paintContract(errorBox(err));
    toastError(err);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function switchTo(tab) {
  const btn = document.querySelector(`#prTabs [data-tab="${h(tab)}"]`);
  if (btn) btn.click();
}

function paintContract(fallbackHtml) {
  const pane = $("#prContractPane");
  if (!pane) return;
  if (fallbackHtml) { pane.innerHTML = fallbackHtml; return; }
  const plan = lastPlan;
  if (!plan) {
    pane.innerHTML = card(empty("尚未生成。左侧输入研究需求后点「发送给规划节点」"),
      { flat: true });
    return;
  }
  if (!Object.keys(plan).length) {
    pane.innerHTML = card(`
      ${badge("planning_failed", "red")}
      <div class="muted" style="font-size:12px;margin-top:8px">
        Planner 未返回有效任务单（模型调用失败且未启用确定性兜底）。</div>`,
      { flat: true });
    return;
  }

  const retrieval = plan.retrieval_plan || {};
  const mission = plan.mission || {};
  const contract = plan.design_contract || plan.creative_contract || {};
  const analysis = plan.analysis_plan || {};
  const evidence = plan.evidence_policy || {};
  const mode = plan.planner_mode || "-";
  const modeTone = mode === "llm" ? "green"
    : (mode === "offline_fallback" ? "amber" : "gray");
  const mustCover = retrieval.must_cover
    || (contract.target_constraints || {}).hard_constraints || [];
  const dims = retrieval.dimensions || analysis.dimensions || [];
  const stops = plan.stop_conditions || [];

  pane.innerHTML = card(`
    <div class="row" style="margin-bottom:10px">
      ${statusBadge(plan.task_kind)} ${badge(plan.content_type || plan.domain || "-")}
      <span class="badge b-${modeTone}">planner_mode: ${h(mode)}</span>
      ${plan.planner_model_error ? badge("模型错误已记录", "amber") : ""}
    </div>
    <div class="stack">
      ${kv([
        ["目标", plan.goal || mission.goal || "-"],
        ["领域", plan.domain || "-"],
        ["任务类型", `${plan.task_kind || "-"} / ${plan.content_type || "-"}`],
        ["检索策略", `${retrieval.strategy || "broad"} · 证据缺口 ${retrieval.evidence_gap_enabled ? "开" : "关"} · 单域深挖 ${retrieval.deep_single_domain_enabled ? "开" : "关"}`],
        ["每查询上限", retrieval.max_results_per_query ?? mission.max_results ?? "-"],
      ])}
      <div><div class="section-title" style="margin:0 0 6px">检索式（query_variants）</div>
        ${(retrieval.query_variants || mission.seed_terms || []).length
          ? `<div class="pre">${h((retrieval.query_variants || mission.seed_terms || []).join("\n"))}</div>`
          : `<span class="muted">无</span>`}</div>
      <div><div class="section-title" style="margin:0 0 6px">分析维度</div>
        ${dims.length ? chips(dims) : `<span class="muted">无</span>`}</div>
      <div><div class="section-title" style="margin:0 0 6px">必须覆盖（硬约束）</div>
        ${mustCover.length ? chips(mustCover.map((c) => typeof c === "string" ? c : JSON.stringify(c)), { cls: "on" })
          : `<span class="muted">无</span>`}</div>
      <div class="grid-2">
        <div><div class="section-title" style="margin:0 0 6px">设计契约</div>
          ${kv([
            ["目标对象", contract.objective || "-"],
            ["创新下限", contract.innovation_floor || plan.innovation_floor || "-"],
            ["候选数下限", contract.min_candidates ?? "-"],
            ["差异轴", (contract.differentiation_axes || []).join("、") || "-"],
          ])}</div>
        <div><div class="section-title" style="margin:0 0 6px">证据策略 / 停止条件</div>
          ${kv([["最低质量", evidence.min_quality ?? "-"],
                ["要求证据类型", (evidence.required_types || []).join("、") || "-"]])}
          ${stops.length ? `<ul style="margin:6px 0 0 16px;padding:0;font-size:12px">
            ${stops.map((s) => `<li>${h(typeof s === "string" ? s : JSON.stringify(s))}</li>`).join("")}</ul>`
            : `<div class="muted" style="font-size:12px">未声明停止条件</div>`}</div>
      </div>
      <details><summary class="muted" style="cursor:pointer">原始 JSON</summary>
        <div class="pre" style="margin-top:6px">${h(JSON.stringify(plan, null, 2))}</div></details>
    </div>`, { flat: true });
}

/* ------------------------------------------------------------------ 知识包 */

async function loadOntology() {
  try {
    ontology = await api.get("/api/ontology?limit=5000&min_confidence=0");
  } catch (err) {
    ontology = null;
    console.warn("知识包加载失败", err);
  }
}

function paintKnowledge() {
  const pane = $("#prKnowledgePane");
  if (!pane) return;
  if (!ontology) {
    pane.innerHTML = card(errorBox(new Error("知识包加载失败或尚无本体数据")), { flat: true });
    return;
  }
  const hyperedges = ontology.hyperedges || [];
  const types = [...new Set(hyperedges.map((x) => x.hyperedge_type))].sort();
  const shown = typeFilter ? hyperedges.filter((x) => x.hyperedge_type === typeFilter) : hyperedges;
  const withConds = hyperedges.filter((x) => (x.conditions || []).length).length;
  const withMeas = hyperedges.filter((x) => (x.measurements || []).length).length;
  const condRows = hyperedges.reduce((n, x) => n + (x.conditions || []).length, 0);
  const measRows = hyperedges.reduce((n, x) => n + (x.measurements || []).length, 0);

  const body = hyperedges.length ? `
    <div class="row" style="margin-bottom:8px">
      <button class="btn small ${typeFilter ? "ghost" : "primary"}" data-type="">全部 (${h(hyperedges.length)})</button>
      ${types.map((t) => {
        const count = hyperedges.filter((x) => x.hyperedge_type === t).length;
        return `<button class="btn small ${typeFilter === t ? "primary" : "ghost"}"
          data-type="${h(t)}">${h(t)} (${h(count)})</button>`;
      }).join("")}
    </div>
    <div class="table-wrap" style="max-height:520px">
      <table>
        <thead><tr>
          <th style="width:150px">超边</th><th style="width:92px">类型</th>
          <th>成员（角色: 名称）</th><th style="width:170px">条件</th>
          <th style="width:140px">测量</th><th style="width:56px">conf</th>
        </tr></thead>
        <tbody>${shown.map((he) => {
          const members = (he.members || []).map((m) => `${m.role || "participant"}: ${m.name}`).join("；");
          const conds = (he.conditions || []).map((c) => `${c.condition_key}${c.operator || ""}${c.value_text}${c.unit || ""}`).join("，");
          const meas = (he.measurements || []).map((m) => `${m.metric}=${m.value_text}${m.unit || ""}`).join("，");
          return `<tr data-he="${h(he.hyperedge_id)}" style="cursor:pointer">
            <td><b>H-${h(String(he.hyperedge_id).padStart(4, "0"))}</b>
              <div class="muted" style="font-size:11px">${h(trunc(he.label || he.hyperedge_type, 30))}</div></td>
            <td>${badge(he.hyperedge_type, typeTone(he.hyperedge_type))}</td>
            <td style="font-size:12px">${h(trunc(members, 56))}</td>
            <td style="font-size:12px">${conds ? h(trunc(conds, 42)) : '<span class="muted">—</span>'}</td>
            <td style="font-size:12px">${meas ? h(trunc(meas, 34)) : '<span class="muted">—</span>'}</td>
            <td>${h(fmtNumber(he.confidence, 2))}</td>
          </tr>`;
        }).join("")}</tbody>
      </table>
    </div>` : empty("当前库没有超边。先在「文献库」执行批量知识抽取");

  pane.innerHTML = metrics([
    ["超边", hyperedges.length, `截断 ${ontology.truncated ? "是" : "否"}`, "accent"],
    ["含条件", withConds, `条件行 ${condRows}`, "cyan"],
    ["含测量", withMeas, `测量行 ${measRows}`, "violet"],
    ["节点 / 关系", `${(ontology.nodes || []).length} / ${(ontology.edges || []).length}`, "", "green"],
  ]) + card(body, { flat: true, title: "科研超边",
    subtitle: "角色 / 条件 / 测量 / 证据绑为一个整体；点击行看明细" });

  pane.querySelectorAll("[data-type]").forEach((btn) => {
    btn.onclick = () => { typeFilter = btn.dataset.type; paintKnowledge(); };
  });
  pane.querySelectorAll("[data-he]").forEach((tr) => {
    tr.onclick = () => openHyperedge(Number(tr.dataset.he));
  });
}

function openHyperedge(id) {
  const he = (ontology?.hyperedges || []).find((x) => Number(x.hyperedge_id) === id);
  if (!he) return;
  // cells 里的每一项都由调用方预先转义（h(...) 或 badge(...)），因此命名 *Html
  const table = (headers, rowsHtml) => rowsHtml.length
    ? `<div class="table-wrap" style="max-height:240px"><table>
        <thead><tr>${headers.map((x) => `<th>${h(x)}</th>`).join("")}</tr></thead>
        <tbody>${rowsHtml.map((cellsHtml) => `<tr>${cellsHtml.map((cellHtml) => `<td>${cellHtml}</td>`).join("")}</tr>`).join("")}</tbody>
      </table></div>`
    : empty("无");
  openDrawer(`超边 H-${String(id).padStart(4, "0")}`, `
    <div class="row" style="margin-bottom:10px">
      ${badge(he.hyperedge_type, typeTone(he.hyperedge_type))}
      ${badge(`conf ${fmtNumber(he.confidence, 3)}`)}
      ${he.evidence_tier ? badge(he.evidence_tier) : ""}
    </div>
    <h3>${h(he.label || "(无标签)")}</h3>
    ${kv([["来源论文", he.paper_key], ["创建时间", fmtFull(he.created_at)]])}
    <div class="section-title">成员与角色</div>
    ${table(["角色", "节点", "类型", "位置"], (he.members || []).map((m) =>
      [badge(m.role || "participant"), h(m.name), h(m.node_type || "-"), h(m.position ?? "-")]))}
    <div class="section-title">量化条件</div>
    ${table(["键", "运算符", "值", "单位"], (he.conditions || []).map((c) =>
      [h(c.condition_key), h(c.operator || ""), h(c.value_text), h(c.unit || "—")]))}
    <div class="section-title">测量值</div>
    ${table(["指标", "值", "单位"], (he.measurements || []).map((m) =>
      [h(m.metric), h(m.value_text), h(m.unit || "—")]))}
    <div class="section-title">证据</div>
    ${(he.provenance || []).length
      ? `<div class="stack">${he.provenance.map((p) =>
          `<div class="pre">${h(`${p.paper || ""}\n${p.evidence || ""}`)}</div>`).join("")}</div>`
      : empty("无证据")}`);
}

function typeTone(type) {
  const map = {
    procedure: "blue", claim: "violet", event: "amber", observation: "cyan",
    causal_relation: "green", comparison: "gray", definition: "gray",
    relation: "gray", mechanism: "violet", reaction: "blue",
  };
  return map[type] || "gray";
}
