/* 总览页：一屏说清"库里有什么、引擎跑到哪、下一步该干什么"。
 * 信息密度对齐 PWA 的 dashboard（6 列 metric + 分块卡片），数据来自 trial 的既有端点。
 */
"use strict";

import { api } from "../common/api.js";
import { $, h, fmtTs, fmtFull, basename } from "../common/dom.js";
import { metrics, card, badge, statusBadge, progress, empty, loading, errorBox } from "../ui/design.js";

let host = null;

export const dashboardPage = {
  title: "总览",
  subtitle: "文献库 / 引擎状态 / 最近活动",
  group: "总览",

  mount(container) {
    host = container;
  },

  async refresh() {
    if (!host) return;
    host.innerHTML = loading("正在汇总…");
    try {
      const [overview, libStats, health, patches] = await Promise.all([
        api.get("/api/overview"),
        api.get("/api/library/stats"),
        api.get("/api/health"),
        api.get("/api/system/packs").catch(() => null),
      ]);
      host.innerHTML = render(overview, libStats, health, patches);
      host.querySelectorAll("[data-goto]").forEach((el) => {
        el.onclick = () => { location.hash = `#${el.dataset.goto}`; };
      });
    } catch (err) {
      host.innerHTML = errorBox(err);
    }
  },
};

function render(o, s, health, packs) {
  const qd = o.quality_decisions || {};
  const ont = o.ontology || {};
  const journal = packs?.journal || {};
  const overrideOk = journal.override
    ? journal.override.files.every((f) => f.exists) : null;

  const head = metrics([
    ["文献总数", o.papers ?? 0, `来源 ${Object.keys(o.paper_status || {}).length} 类状态`, "accent"],
    ["质量通过", (qd.direct || 0) + (qd.flagged || 0), `直接 ${qd.direct || 0} · 标记 ${qd.flagged || 0}`, "green"],
    ["待人工审核", qd.human || 0, `库状态 human_review ${(o.paper_status || {}).human_review || 0}`, "red"],
    ["已抽取知识", s.extracted ?? 0, `全文 ${s.with_fulltext ?? 0} · 仅摘要 ${s.abstract_only ?? 0}`, "violet"],
    ["本体节点 / 关系", `${ont.nodes ?? 0} / ${ont.edges ?? 0}`, `类型 ${ont.types ?? 0}`, "cyan"],
    ["科研超边", ont.hyperedges ?? 0, `域 ${ont.domains ?? 0} · 通道 ${ont.channels ?? 0}`, "amber"],
  ]);

  const pipeline = [
    ["文献", o.papers ?? 0, "papers"],
    ["已评估", s.assessed ?? 0, null],
    ["已抽取", s.extracted ?? 0, "knowledge"],
    ["节点", ont.nodes ?? 0, "ontology"],
    ["超边", ont.hyperedges ?? 0, "ontology"],
  ];
  const maxPipeline = Math.max(1, ...pipeline.map(([, v]) => v));
  const pipelineHtml = pipeline.map(([label, value, goto]) => `
    <div class="row" style="gap:10px;margin:5px 0">
      <span class="muted" style="width:56px;font-size:12px">${h(label)}</span>
      ${progress(maxPipeline ? value / maxPipeline : 0)}
      <span style="width:52px;text-align:right;font-size:12px">${h(value)}</span>
    </div>`).join("");

  const statusEntries = Object.entries(o.paper_status || {});
  const statusHtml = statusEntries.length
    ? statusEntries.map(([k, v]) => `<span class="chip-tag">${h(k)} ${h(v)}</span>`).join("")
    : empty("暂无文献状态");

  return `
  <div class="page-head">
    <h1>总览</h1>
    <div class="desc">数据库 <b>${h(basename(health.db))}</b> · 最近活动 ${h(fmtTs(o.last_activity))}
      · 事件 ${h(o.logs ?? 0)} 条</div>
  </div>
  ${head}
  <div class="grid-2">
    ${card(`
      <div class="stack">
        <div><span class="muted" style="font-size:12px">链路规模</span>${pipelineHtml}</div>
        <div><span class="muted" style="font-size:12px">文献状态分布</span>
          <div style="margin-top:6px">${statusHtml}</div></div>
      </div>`, { title: "数据构建链路", subtitle: "检索 → 质量 → 知识 → 本体" })}
    ${card(`
      <div class="stack">
        <div class="row" style="justify-content:space-between">
          <span class="muted" style="font-size:12px">抽取覆盖率</span>
          <span>${h(coverage(s.extracted, o.papers))}</span>
        </div>
        ${progress(o.papers ? (s.extracted || 0) / o.papers : 0)}
        <div class="row" style="justify-content:space-between">
          <span class="muted" style="font-size:12px">需人工复核</span>
          <span>${h(s.needs_review ?? 0)} 篇</span>
        </div>
        <div class="row" style="justify-content:space-between">
          <span class="muted" style="font-size:12px">低信号入库（门控放行）</span>
          <span>${h(s.low_signal ?? 0)} 条</span>
        </div>
        <div class="row" style="justify-content:space-between">
          <span class="muted" style="font-size:12px">年份未知</span>
          <span>${h(s.unknown_year ?? 0)} 篇</span>
        </div>
        <div class="muted" style="font-size:11.5px">
          低信号策略：<b>${h(packs?.config?.relevance_gate_low_signal || "-")}</b>
          （warn = 放行并标注，不丢弃）
        </div>
      </div>`, { title: "质量与语料卫生", subtitle: "阈值与门控策略的当前影响" })}
  </div>
  <div class="grid-2" style="margin-top:14px">
    ${card(`
      <div class="stack">
        <div class="row" style="justify-content:space-between">
          <span class="muted" style="font-size:12px">生效分区条目</span>
          <span><b>${h(journal.entries ?? 0)}</b> 条</span>
        </div>
        <div class="row" style="justify-content:space-between">
          <span class="muted" style="font-size:12px">内置分科子集</span>
          <span>${h(journal.default_subset ?? 0)} 条</span>
        </div>
        ${journal.override ? `<div class="row" style="justify-content:space-between">
          <span class="muted" style="font-size:12px">RA_JOURNAL_QUARTILES</span>
          <span>${overrideOk ? badge("已加载", "green") : badge("文件缺失 → 已退回子集", "red")}</span>
        </div>` : `<div class="muted" style="font-size:12px">未设置 RA_JOURNAL_QUARTILES</div>`}
        ${(journal.unmatched_top || []).length ? `
          <div class="muted" style="font-size:12px;margin-top:4px">
            未命中分区的期刊（Top ${journal.unmatched_top.length}）：
            ${h(journal.unmatched_top.slice(0, 5).map((r) => r.venue).join("、"))}
          </div>` : ""}
      </div>`, {
      title: "期刊分区数据源",
      subtitle: "决定权威性因子 A 的命中率",
      actions: `<button class="btn small ghost" data-goto="system">查看系统状态</button>`,
    })}
    ${card(`
      <div class="stack">
        <div class="row" style="justify-content:space-between">
          <span class="muted" style="font-size:12px">可用技能包 / 领域包</span>
          <span>${h(Object.keys(packs?.packs?.loaded?.skills || {}).length)} / ${h(Object.keys(packs?.packs?.loaded?.domains || {}).length)}</span>
        </div>
        <div class="row" style="justify-content:space-between">
          <span class="muted" style="font-size:12px">引用样式</span>
          <span>${h((packs?.citation?.styles || []).length)} 种</span>
        </div>
        <div class="row" style="justify-content:space-between">
          <span class="muted" style="font-size:12px">加载告警</span>
          <span>${(packs?.packs?.warnings || []).length
            ? badge(`${packs.packs.warnings.length} 条`, "amber") : badge("无", "green")}</span>
        </div>
        <div class="muted" style="font-size:11.5px">
          论文写作与领域词表全部来自 packs，代码内无学科硬编码。
        </div>
      </div>`, {
      title: "领域层（packs）",
      subtitle: "零硬编码的领域内容加载状态",
      actions: `<button class="btn small ghost" data-goto="system">详情</button>`,
    })}
  </div>
  <div class="grid-2" style="margin-top:14px">
    ${card(quickLinks(), { title: "下一步", subtitle: "按当前库状态推荐的动作" })}
  </div>`;
}

function quickLinks() {
  const links = [
    ["智能检索", "search", "用自然语言描述需求，生成检索计划并入库"],
    ["文献库", "library", "筛选、打标签、批量抽取、导出引用"],
    ["知识包", "retrieval", "查看机制状态、机会缺口与算子候选"],
    ["研究流程", "experiment", "Planner 契约 → 消费 → 候选 → 审核 → 事实核查"],
    ["系统状态", "system", "包加载、分区表来源、生效配置"],
  ];
  return `<div class="grid-3">${links.map(([label, key, desc]) => `
    <div class="card flat" data-goto="${h(key)}" style="cursor:pointer">
      <div style="font-weight:650">${h(label)}</div>
      <div class="muted" style="font-size:11.5px;margin-top:3px">${h(desc)}</div>
    </div>`).join("")}</div>`;
}

function coverage(extracted, total) {
  if (!total) return "-";
  return `${((extracted || 0) / total * 100).toFixed(1)}%`;
}
