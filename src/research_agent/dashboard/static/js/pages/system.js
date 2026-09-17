/* 系统状态页：把"引擎实际在用哪份数据、哪些内容没加载"变成界面可见。
 * 这是 trial 引擎的可观测性在 PWA 形态下的呈现（对应方案风险 R2/R3/R4）。
 */
"use strict";

import { api } from "../common/api.js";
import { $, h } from "../common/dom.js";
import { card, metrics, badge, empty, loading, errorBox, kv } from "../ui/design.js";

let host = null;

export const systemPage = {
  title: "系统状态",
  subtitle: "包加载 · 数据源 · 生效配置",
  group: "运行",
  icon: "⚙",

  mount(container) {
    host = container;
  },

  async refresh() {
    if (!host) return;
    host.innerHTML = loading("正在读取运行时状态…");
    try {
      const data = await api.get("/api/system/packs");
      host.innerHTML = render(data);
    } catch (err) {
      host.innerHTML = errorBox(err);
    }
  },
};

function render(data) {
  const packs = data.packs || {};
  const loaded = packs.loaded || {};
  const journal = data.journal || {};
  const citation = data.citation || {};
  const config = data.config || {};
  const warnings = packs.warnings || [];
  const override = journal.override;
  const overrideOk = override ? override.files.every((f) => f.exists) : null;

  const head = metrics([
    ["生效分区条目", journal.entries ?? 0,
      `内置子集 ${journal.default_subset ?? 0}`, journal.entries > (journal.default_subset || 0) ? "green" : "amber"],
    ["未命中分区期刊", (journal.unmatched_top || []).length,
      "Top 25 去重后", (journal.unmatched_top || []).length ? "amber" : "green"],
    ["年份未知文献", journal.unknown_year_papers ?? 0, "可用筛选档位查看", "cyan"],
    ["引用样式", (citation.styles || []).length, `默认 ${citation.default_style || "-"}`, "violet"],
    ["期刊写法覆盖", citation.venue_overrides ?? 0, "缩写 / BibTeX 写法", "blue"],
    ["加载告警", warnings.length, warnings.length ? "见下方明细" : "无", warnings.length ? "red" : "green"],
  ]);

  const skillChipsHtml = Object.entries(loaded.skills || {}).map(([name, ok]) => chip(name, ok));
  const domainChipsHtml = Object.entries(loaded.domains || {}).map(([name, ok]) => chip(name, ok));

  return `
  <div class="page-head">
    <h1>系统状态</h1>
    <div class="desc">领域内容全部来自 packs；这里显示<b>实际生效</b>的数据源与配置，
      避免"看起来在跑、其实分区表退回了子集"这类静默降级。</div>
  </div>
  ${head}
  <div class="grid-2">
    ${card(`
      <div class="section-title" style="margin-top:0">技能包</div>
      <div class="row">${skillChipsHtml.join("") || empty("无")}</div>
      <div class="section-title">领域包</div>
      <div class="row">${domainChipsHtml.join("") || empty("无")}</div>
      <div class="section-title">搜索路径</div>
      <div class="pre">${h((packs.roots || []).join("\n"))}</div>
      <div class="section-title">引用样式包</div>
      <div class="row">${chip("citation_styles", loaded.citation_styles)}
        <span class="muted" style="font-size:11.5px">${h((citation.styles || []).join(" / "))}</span></div>`,
      { title: "领域层（packs）", subtitle: "缺包会显式告警并返回空值，不做内联兜底" })}

    ${card(`
      ${override ? `
        <div class="row" style="margin-bottom:8px">
          ${overrideOk ? badge("覆盖文件已加载", "green") : badge("覆盖文件缺失 → 已退回内置子集", "red")}
        </div>
        <div class="section-title" style="margin-top:0">RA_JOURNAL_QUARTILES</div>
        <div class="pre">${h(override.value)}</div>
        <div class="stack" style="margin-top:8px">
          ${override.files.map((f) => `<div class="row" style="font-size:12px">
            <span>${f.exists ? "✅" : "❌"}</span><code>${h(f.path)}</code></div>`).join("")}
        </div>` : `<div class="muted">未设置 RA_JOURNAL_QUARTILES，使用随仓库发布的化学子集。</div>`}
      <div class="section-title">未命中分区的期刊（建议补进 packs 或换成全量表）</div>
      ${(journal.unmatched_top || []).length ? `
        <div class="table-wrap" style="max-height:240px">
          <table><thead><tr><th>期刊</th><th style="width:80px">文献数</th></tr></thead>
          <tbody>${journal.unmatched_top.map((row) => `
            <tr><td>${h(row.venue)}</td><td>${h(row.count)}</td></tr>`).join("")}</tbody></table>
        </div>` : empty("没有未命中的期刊（或库中暂无 venue）")}`,
      { title: "期刊分区数据源", subtitle: "权威性因子 A 的命中率取决于这份表" })}
  </div>
  <div class="grid-2" style="margin-top:14px">
    ${card(`
      <div class="table-wrap" style="max-height:420px">
        <table><tbody>
          ${Object.entries(config).map(([key, value]) => `
            <tr><td class="muted" style="width:230px">${h(key)}</td>
                <td>${h(typeof value === "object" ? JSON.stringify(value) : value)}</td></tr>`).join("")}
        </tbody></table>
      </div>
      <div class="muted" style="font-size:11.5px;margin-top:8px">
        修改 <code>.env</code> 后需重启服务；这里显示的是<b>覆盖后</b>的实际值。</div>`,
      { title: "生效配置", subtitle: "RA_* 环境变量覆盖后的真实取值" })}
    ${card(`
      ${warnings.length
        ? `<div class="stack">${warnings.map((w) => `<div class="row"><span class="badge b-amber">警告</span><code>${h(w)}</code></div>`).join("")}</div>`
        : empty("没有加载告警")}
      <div class="section-title">低信号策略说明</div>
      <div class="muted" style="font-size:12px">
        当前 <code>relevance_gate_low_signal = ${h(config.relevance_gate_low_signal || "-")}</code>：
        <b>warn</b> 表示词元不可判定（如 2–5 字母缩写主题、中文主题配英文语料）时
        <b>放行并标注</b>，而不是整批丢弃；这些记录可在文献库用「只看低信号」筛出。
      </div>`,
      { title: "加载告警与策略", subtitle: "packs 与门控策略的运行时反馈" })}
  </div>`;
}

function chip(name, ok) {
  return `<span class="chip-tag ${ok ? "on" : "off"}" title="${ok ? "已加载" : "未加载"}">
    ${ok ? "●" : "○"} ${h(name)}</span>`;
}
