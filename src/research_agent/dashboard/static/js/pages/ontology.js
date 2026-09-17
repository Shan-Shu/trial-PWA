/* 动态本体页：vis-network 图谱 + 类型/置信度/搜索筛选 + 节点详情抽屉。
 * 修复了 trial 原实现的三处缺陷：截断提示、clusterByType 名实不符、keepView 未实现。
 */
"use strict";

import { api, qs } from "../common/api.js";
import { $, h, fmtNumber, trunc } from "../common/dom.js";
import { card, metrics, badge, empty, loading, errorBox, openDrawer, setDrawerBody, kv } from "../ui/design.js";
import { toast, toastError } from "../common/ui.js";

let host = null;
let graphData = null;
let network = null;
const state = { types: new Set(), minConf: 0, q: "", domain: "" };

const PALETTE = ["#3B82F6", "#06B6D4", "#10B981", "#F59E0B", "#8B5CF6", "#EF4444",
                 "#2DD4BF", "#F472B6", "#A3E635", "#60A5FA", "#FB923C", "#C084FC"];

export const ontologyPage = {
  title: "动态本体",
  subtitle: "节点 · 关系 · 超边 · 语义域",
  group: "资产",
  icon: "◉",

  mount(container) {
    host = container;
    host.innerHTML = `
      <div class="page-head">
        <h1>动态本体</h1>
        <div class="desc">节点/边按规范化名合并（置信度取 max、别名与证据增补）；类型自动注册；
          超边承载科研过程的角色与量化条件。</div>
      </div>
      <div id="ontStats"></div>
      <div class="grid-2">
        <div>
          ${card(`
            <div class="row" style="margin-bottom:8px">
              <div class="tfilters" id="ontTypes" style="display:flex;flex-wrap:wrap;gap:6px"></div>
            </div>
            <div class="row">
              <label class="muted" style="font-size:12px">最低置信度 <span id="ontConfVal">0</span></label>
              <input type="range" id="ontConf" min="0" max="1" step="0.05" value="0" style="flex:1" />
              <input type="search" id="ontQ" placeholder="按名称搜索…" style="width:180px" />
              <button class="btn small" id="ontApply">应用</button>
              <button class="btn small ghost" id="ontReset">重置</button>
            </div>
            <div id="graphBox" style="height:560px;border:1px solid var(--border);border-radius:12px;margin-top:10px;background:var(--bg-deep)"></div>
            <div class="muted" style="font-size:11.5px;margin-top:6px" id="graphStats"></div>`,
            { flat: true, title: "本体图谱", subtitle: "点击节点查看别名 / 属性 / 证据 / 相邻关系" })}
        </div>
        <div id="ontSide"></div>
      </div>`;
    bind();
  },

  async refresh() {
    if (!host) return;
    const params = qs({
      limit: 2000,
      min_confidence: state.minConf || 0,
      types: state.types.size ? [...state.types].join(",") : "",
      q: state.q || "",
      domain: state.domain || "",
    });
    try {
      graphData = await api.get(`/api/ontology${params}`);
      renderStats();
      renderTypeFilters();
      drawGraph();
      renderSide();
    } catch (err) {
      const box = $("#graphBox");
      if (box) box.innerHTML = errorBox(err);
    }
  },
};

function renderStats() {
  const box = $("#ontStats");
  if (!box || !graphData) return;
  box.innerHTML = metrics([
    ["节点", graphData.shown_nodes ?? 0, `共 ${graphData.total ?? 0}`, "accent"],
    ["关系", graphData.shown_edges ?? 0, "", "green"],
    ["超边", graphData.shown_hyperedges ?? 0, "", "violet"],
    ["语义域", graphData.shown_domains ?? 0, "", "cyan"],
    ["关系通道", graphData.shown_channels ?? 0, "", "amber"],
    ["截断", graphData.truncated ? "是" : "否", graphData.truncated ? "提高阈值或按域筛选" : "完整", graphData.truncated ? "red" : "green"],
  ]);
}

function renderTypeFilters() {
  const box = $("#ontTypes");
  if (!box || !graphData) return;
  const counts = {};
  (graphData.nodes || []).forEach((n) => { counts[n.type] = (counts[n.type] || 0) + 1; });
  const types = Object.keys(counts).sort();
  box.innerHTML = types.map((t) => `
    <span class="chip-tag ${state.types.has(t) ? "on" : ""}" data-type="${h(t)}"
          style="cursor:pointer">${h(t)} ${h(counts[t])}</span>`).join("")
    || '<span class="muted">无节点类型</span>';
  box.querySelectorAll("[data-type]").forEach((el) => {
    el.onclick = () => {
      const t = el.dataset.type;
      if (state.types.has(t)) state.types.delete(t);
      else state.types.add(t);
      el.classList.toggle("on");
      clearTimeout(el._timer);
      el._timer = setTimeout(() => ontologyPage.refresh(), 250);
    };
  });
}

function drawGraph() {
  const box = $("#graphBox");
  if (!box || !graphData) return;
  const nodes = graphData.nodes || [];
  if (!nodes.length) {
    box.innerHTML = `<div class="empty" style="margin:16px">暂无本体节点 —— 先在文献库执行知识抽取</div>`;
    network = null;
    return;
  }
  // keepView：重建前记录视口，重建后还原（原实现声明了参数却从未使用）
  const keep = network
    ? { position: network.getViewPosition(), scale: network.getScale() }
    : null;
  const data = {
    nodes: new vis.DataSet(nodes.map((n) => ({
      id: n.id,
      label: trunc(n.label, 22),
      title: `${n.type} · ${n.label} · conf ${fmtNumber(n.confidence, 2)}`,
      value: 6 + Math.round((n.confidence || 0) * 22),
      color: { background: typeColor(n.type), border: "rgba(255,255,255,.18)" },
      font: { color: "#E8ECF4", size: 12 },
    }))),
    edges: new vis.DataSet((graphData.edges || []).map((e) => ({
      id: e.id, from: e.from, to: e.to, label: e.type,
      title: `${e.type} · conf ${fmtNumber(e.confidence, 2)}`,
      width: 0.6 + (e.confidence || 0) * 2.2,
      color: { color: "rgba(160,174,200,.45)", highlight: "#3B82F6" },
      font: { color: "#6B7A9E", size: 10, strokeWidth: 0 },
      arrows: { to: { enabled: true, scaleFactor: 0.5 } },
    }))),
  };
  if (network) { network.destroy(); network = null; }
  box.innerHTML = "";
  network = new vis.Network(box, data, {
    physics: { stabilization: { iterations: 180 }, barnesHut: { gravitationalConstant: -8000, springLength: 140 } },
    interaction: { hover: true, tooltipDelay: 120 },
    nodes: { shape: "dot", scaling: { min: 8, max: 30 } },
    edges: { smooth: { type: "dynamic" } },
  });
  network.on("click", (params) => {
    if (params.nodes && params.nodes.length) showNode(params.nodes[0]);
  });
  if (keep) network.moveTo({ position: keep.position, scale: keep.scale, animation: false });

  const stats = $("#graphStats");
  if (stats) {
    stats.textContent = `节点 ${graphData.shown_nodes}/${graphData.total} · 边 ${graphData.shown_edges}`
      + (graphData.truncated ? " · 已按 limit 截断（可提高最低置信度或按域/类型筛选）" : "");
  }
}

function renderSide() {
  const side = $("#ontSide");
  if (!side || !graphData) return;
  const hyperedges = (graphData.hyperedges || []).slice(0, 6);
  const domains = (graphData.domains || []).slice(0, 10);
  const channels = (graphData.channels || []).slice(0, 6);
  side.innerHTML = `
    ${card(domains.length
      ? `<div class="row">${domains.map((d) => `
          <span class="chip-tag ${state.domain === d.domain_key ? "on" : ""}"
                data-domain="${h(d.domain_key || "")}" style="cursor:pointer">
            ${h(d.label || d.domain_key)} ${h(d.member_count ?? 0)}</span>`).join("")}</div>`
      : empty("暂无语义域"), { title: "语义节点域", subtitle: "点击按域筛选图谱" })}
    ${card(hyperedges.length ? `<div class="stack">${hyperedges.map((he) => `
      <div style="font-size:12px">
        <b>H-${h(String(he.hyperedge_id).padStart(4, "0"))}</b>
        ${badge(he.hyperedge_type)}
        <div class="muted">${h(trunc(he.label || "", 48))}</div>
      </div>`).join("")}</div>` : empty("暂无超边"),
      { title: "科研超边（前 6 条）", subtitle: "完整明细见「知识包」页" })}
    ${card(channels.length ? `<div class="stack">${channels.map((c) => `
      <div class="row" style="justify-content:space-between;font-size:12px">
        <b>${h(c.relation_family || c.channel_key)}</b>
        <span class="muted">支持 ${h(c.support_count ?? 0)}</span>
      </div>`).join("")}</div>` : empty("暂无关系通道"), { title: "关系通道" })}`;

  side.querySelectorAll("[data-domain]").forEach((el) => {
    el.onclick = () => {
      const key = el.dataset.domain;
      state.domain = state.domain === key ? "" : key;
      ontologyPage.refresh();
    };
  });
}

async function showNode(id) {
  openDrawer(`节点 #${id}`, loading("读取节点详情…"));
  try {
    const data = await api.get(`/api/ontology/nodes/${id}`);
    const node = data.node || {};
    const neighbors = data.neighbors || [];
    const aliases = node.aliases || [];
    const attributes = Object.entries(node.attributes || {});
    const provenance = node.provenance || [];
    setDrawer(`节点 #${id}`, `
      <div class="row" style="margin-bottom:10px">
        ${badge(node.node_type || "-")}
        ${badge(`conf ${fmtNumber(node.confidence, 3)}`)}
        ${node.evidence_tier ? badge(node.evidence_tier) : ""}
        ${node.external_id ? badge(`${node.external_source || "外部"}:${node.external_id}`, "green") : ""}
      </div>
      <h3>${h(node.name || "")}</h3>
      ${kv([
        ["规范化名", node.normalized_name],
        ["别名", aliases.length ? aliases.join("、") : ""],
        ["首见 / 最近", `${node.first_seen_at || "-"} / ${node.last_seen_at || "-"}`],
        ["术语状态", node.term_status],
      ])}
      <div class="section-title">属性</div>
      ${attributes.length ? kv(attributes.map(([k, v]) => [k, typeof v === "object" ? JSON.stringify(v) : v])) : empty("无属性")}
      <div class="section-title">来源证据（${provenance.length}）</div>
      ${provenance.length ? `<div class="stack">${provenance.slice(0, 8).map((p) => `
        <div class="pre">${h(`${p.paper || ""}\n${p.evidence || ""}`)}</div>`).join("")}</div>` : empty("无证据")}
      <div class="section-title">相邻关系（${neighbors.length}）</div>
      ${neighbors.length ? `<div class="table-wrap" style="max-height:280px"><table>
        <thead><tr><th>方向</th><th>关系</th><th>对端</th><th>conf</th></tr></thead>
        <tbody>${neighbors.map((n) => `<tr>
          <td>${badge(n.direction === "out" ? "→" : "←", "gray")}</td>
          <td>${h(n.relation_type)}</td>
          <td><a href="#" data-node="${h(n.other_id)}">${h(n.other_name)}</a>
              <span class="muted">(${h(n.other_type)})</span></td>
          <td>${h(fmtNumber(n.confidence, 2))}</td></tr>`).join("")}</tbody>
      </table></div>` : empty("无相邻关系")}`);
    document.querySelectorAll("[data-node]").forEach((link) => {
      link.onclick = (e) => { e.preventDefault(); showNode(Number(link.dataset.node)); };
    });
  } catch (err) {
    setDrawer(`节点 #${id}`, errorBox(err));
  }
}

function setDrawer(title, body) {
  setDrawerBody(body);
}

function bind() {
  $("#ontApply")?.addEventListener("click", () => {
    state.minConf = parseFloat($("#ontConf")?.value || "0");
    state.q = $("#ontQ")?.value.trim() || "";
    ontologyPage.refresh();
  });
  $("#ontQ")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("#ontApply").click();
  });
  $("#ontConf")?.addEventListener("input", (e) => {
    const label = $("#ontConfVal");
    if (label) label.textContent = e.target.value;
  });
  $("#ontReset")?.addEventListener("click", () => {
    state.types.clear();
    state.minConf = 0;
    state.q = "";
    state.domain = "";
    const conf = $("#ontConf");
    if (conf) conf.value = "0";
    const q = $("#ontQ");
    if (q) q.value = "";
    const label = $("#ontConfVal");
    if (label) label.textContent = "0";
    ontologyPage.refresh();
  });
}

function typeColor(type) {
  let value = 0;
  for (const ch of String(type)) value = (value * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[value % PALETTE.length];
}
