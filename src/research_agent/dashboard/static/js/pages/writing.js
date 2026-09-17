/* 写作台：唯一的交互块 = 工作规划节点的访谈面板。
 *
 * 形态（方案 v6）：
 *
 *   ① 选项目 → ② 一轮问答把整篇定下来 → ③ 执行摘要（只读）
 *
 * 与旧版的区别：旧版每个大纲节点一套表单/指令框/五个按钮，用户被迫逐节点操作。
 * 现在**整页只有一个可交互区**——对话流 + 随题型切换的输入区。
 *
 * 核心是"逐部分闭环"：访谈一个部分 → 判定支撑 → （不足则问用户怎么办）
 * →（必要时协作补齐）→ 写作这个部分 → 下一个部分。前一个部分的正文会成为
 * 后一个部分的上下文。
 *
 * 诚实约定：模型未参与时（拟方案走通用方向兜底、成段走骨架降级），
 * 界面一律如实标注来源，不把兜底说成模型产物。
 */
"use strict";

import { api } from "../common/api.js";
import { $, h, trunc } from "../common/dom.js";
import { card, badge, empty, loading, errorBox } from "../ui/design.js";
import { toast, toastError } from "../common/ui.js";

let host = null;
let projects = [];
let current = null;
let snap = null;
let busy = false;
let pollTimer = null;

/** 离线开关：hash 里带 `offline=1` 时不注入任何模型（拟方案走通用方向、成段走骨架）。
 *
 * 存在的理由：浏览器交互冒烟必须**可复现且快**——真实模型（v4-pro）拟 3 个方案
 * 可能要几十秒到几分钟，把回归验证变成看运气。生产默认仍是联网用模型。
 *
 * 读 **hash** 而不是 search：这个 SPA 用 hash 路由（`#writing`），
 * `?offline=1` 在页面切换时会被丢掉（实测发现）。
 */
const OFFLINE = (() => {
  try {
    return new URLSearchParams(location.hash.split("?")[1] || "").get("offline") === "1";
  } catch {
    return false;
  }
})();

/** 各阶段的用户可读文案 */
const STAGE_LABEL = {
  pending: "待访谈",
  drafting_options: "正在拟方案",
  awaiting_choice: "等你选方案",
  judging: "正在判定支撑",
  awaiting_gap_decision: "等你决定缺口",
  awaiting_custom_plan_choice: "等你确认任务",
  collaborating: "正在补齐支撑",
  writing: "正在写作",
  done: "已完成",
  failed_writable: "写作失败",
};

const STAGE_TONE = {
  done: "green", writing: "blue", collaborating: "blue",
  judging: "blue", drafting_options: "blue",
  awaiting_choice: "amber", awaiting_gap_decision: "amber",
  awaiting_custom_plan_choice: "amber", failed_writable: "red",
};

/** 判定维度的中文名 */
const DIM_LABELS = {
  papers: "命中文献", knowledge: "知识覆盖", conditions: "量化条件",
  comparison: "可比证据", requirements: "指令要求", evidence: "可用证据",
};

export const writingPage = {
  title: "写作台",
  subtitle: "工作规划节点 · 逐部分闭环",
  group: "研究",
  icon: "✎",

  mount(container) {
    host = container;
    host.innerHTML = `
      <div class="page-head">
        <h1>写作台</h1>
        <div class="desc">整篇由一个<b>工作规划节点</b>通过一轮问答定下来：先确认体裁与要写的部分，
          再逐个部分拟 3 个方案让你选；每个部分选定后<b>先判定支撑</b>——不够就问你是保留缺口、
          执行检索补全，还是给一个自定义任务。写作由知识消费与内容形成节点完成。</div>
      </div>
      <div id="wrBody"></div>`;
  },

  async refresh() {
    if (current) {
      await loadSnapshot();
      render();
      return;
    }
    await loadProjects();
    renderPicker();
  },

  unmount() {
    stopPolling();
    host = null;
    current = null;
    snap = null;
  },
};

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// ------------------------------------------------------------------ 项目选择

async function loadProjects() {
  try {
    projects = await api.get("/api/writing/projects");
  } catch (err) {
    projects = [];
    toastError(err);
  }
}

function renderPicker() {
  const body = $("#wrBody");
  if (!body) return;
  const listHtml = projects.length ? projects.map((p) => `
    <div class="card flat" data-project="${h(p.project_id)}" style="cursor:pointer">
      <div class="row" style="justify-content:space-between">
        <b style="font-size:12.5px">${h(trunc(p.title, 34))}</b>
        ${badge(`${p.genre}`, "gray")}
      </div>
      <div class="muted" style="font-size:11px;margin-top:3px">
        ${h(p.topic || "无主题")} · ${h(p.filled_sections)}/${h(p.total_sections)} 节有内容</div>
    </div>`).join("") : empty("还没有项目，先在左侧新建");

  body.innerHTML = `
    <div class="grid-2">
      <div>
        ${card(`
          <div class="stack">
            <input type="text" id="wrTitle" placeholder="新项目标题（必填）" />
            <input type="text" id="wrTopic" placeholder="研究主题" />
            <button class="btn primary" id="wrCreate">新建并开始访谈</button>
          </div>`, { title: "新建项目", subtitle: "标题 + 主题就够，其余在访谈里确认" })}
      </div>
      <div>
        ${card(`<div class="stack">${listHtml}</div>`,
               { title: "已有项目", subtitle: "点一个进入访谈" })}
      </div>
    </div>`;

  $("#wrCreate")?.addEventListener("click", async () => {
    const title = $("#wrTitle")?.value.trim();
    if (!title) { toast("请填写项目标题", "warn"); return; }
    try {
      const res = await api.post("/api/writing/projects", {
        title, topic: $("#wrTopic")?.value.trim() || "",
      });
      if (!res.ok) throw new Error(res.error || "创建失败");
      projects = await api.get("/api/writing/projects");
      await enterProject(res.project_id, { start: true });
    } catch (err) { toastError(err); }
  });

  body.querySelectorAll("[data-project]").forEach((el) => {
    el.onclick = () => enterProject(Number(el.dataset.project));
  });
}

async function enterProject(projectId, { start = false } = {}) {
  try {
    const detail = await api.get(`/api/writing/projects/${projectId}`);
    if (!detail.ok) throw new Error(detail.error || "项目不存在");
    current = detail.project;
    await loadSnapshot({ start });
    if (!snap || !snap.ok) {
      // 静默什么都不显示会让交互"点了没反应"——把原因显式说出来
      const msg = (snap && snap.error) || "访谈状态读取失败";
      toastError(new Error(msg));
      renderPicker();
      return;
    }
    render();
  } catch (err) {
    console.error("[writing] 进入项目失败", err);
    toastError(err);
    renderPicker();
  }
}

// ------------------------------------------------------------------ 访谈数据

async function loadSnapshot({ start = false } = {}) {
  snap = await api.get(`/api/writing/projects/${current.project_id}/interview`);
  if (start || !snap || !snap.ok) {
    snap = await api.post(
      `/api/writing/projects/${current.project_id}/interview/start`, {});
  }
}

/** 回答之后：若还有待执行的步骤，起作业并轮询 */
async function answerAndAdvance(payload) {
  if (busy) return;
  busy = true;
  try {
    const res = await api.post(
      `/api/writing/projects/${current.project_id}/interview/answer`, payload);
    if (!res.ok) throw new Error(res.error || "作答失败");
    snap = res;
    render();
    if (res.next_action) {
      await runStep();
    }
  } catch (err) {
    toastError(err);
  } finally {
    busy = false;
  }
}

async function runStep() {
  const res = await api.post(
    `/api/writing/projects/${current.project_id}/interview/step`,
    { use_model: !OFFLINE });
  if (!res.ok) throw new Error(res.error || "无法推进");
  if (!res.job_id) { await loadSnapshot(); return; }
  setProgress("正在处理…");
  await new Promise((resolve) => {
    stopPolling();
    pollTimer = setInterval(async () => {
      try {
        const job = await api.get(`/api/writing/section-jobs/${res.job_id}`);
        if (job.message) setProgress(job.message);
        if (job.status === "running" || job.status === "cancelling") return;
        stopPolling();
        if (job.status === "error") toastError(new Error(job.error || "步骤失败"));
        await loadSnapshot();
        render();
        resolve();
      } catch (err) {
        stopPolling();
        toastError(err);
        resolve();
      }
    }, 1200);
  });
}

function setProgress(text) {
  const el = $("#wrProgress");
  if (el) el.textContent = text;
}

// ------------------------------------------------------------------ 渲染

function render() {
  const body = $("#wrBody");
  if (!body) return;
  if (!snap || !snap.ok) {
    body.innerHTML = card(errorBox(new Error("访谈状态读取失败")), { flat: true });
    return;
  }
  // 这三个都是**已由 h() 逐值转义**的 HTML 片段，命名带 Html 后缀以示区别
  const conversationHtml = renderConversation();
  const sectionsHtml = renderSections();
  const questionHtml = renderQuestion();
  body.innerHTML = `
    ${renderHeader()}
    <div class="grid-2" style="margin-top:12px;align-items:start">
      <div>${conversationHtml}</div>
      <div>${sectionsHtml}</div>
    </div>`;
  bindConversation();
}

function renderHeader() {
  const done = snap.completed || 0;
  const total = snap.total || 0;
  const pct = total ? Math.round((done / total) * 100) : 0;
  return card(`
    <div class="row" style="justify-content:space-between;align-items:flex-start">
      <div>
        <h2 style="margin:0">${h(current.title)}</h2>
        <div class="muted" style="font-size:12px">
          ${h(snap.intake.genre || "未定体裁")} · 主题 ${h(snap.intake.topic || "未定")}
          · 进度 ${h(done)}/${h(total)} 部分</div>
      </div>
      <div class="row tight">
        <span class="muted" style="font-size:11.5px" id="wrProgress">${
          snap.finished ? "全部完成" : (snap.next_action ? "处理中…" : "等待你的回答")}</span>
        <button class="btn small ghost" id="wrSwitch">换项目</button>
        <button class="btn small ghost danger" id="wrRestart">重新访谈</button>
      </div>
    </div>
    <div class="progress" style="margin-top:8px"><div class="progress-fill"
      style="width:${pct}%"></div></div>`, { flat: true });
}

function renderConversation() {
  const historyHtml = (snap.history || []).map((m) => `
    <div class="chat-msg ${m.role === "user" ? "me" : "ai"}">
      <span class="chat-role">${h(m.role === "user" ? "你" : "AI")}</span>
      <div class="chat-text">${h(m.text)}</div>
    </div>`).join("");
  // 注意：renderQuestion() 返回的是**已由 h() 逐值转义**的 HTML 片段。
  // 它在 render() 里也有同名局部变量；两处必须各自计算，不能跨函数引用
  // （曾因此在浏览器里抛 ReferenceError: questionHtml is not defined——
  //  静态检查/HTTP 冒烟都发现不了，只有真浏览器能抓到）。
  const inputHtml = renderQuestion();
  return card(`
    <div class="chat-flow" id="wrChat">${historyHtml || '<div class="muted">还没有对话</div>'}</div>
    <div class="chat-input" id="wrInput">${inputHtml}</div>`,
    { title: "工作规划节点", subtitle: "整页只有这一个可交互区" });
}

/** 按题型渲染输入区——这是"随题型切换"的地方 */
function renderQuestion() {
  if (snap.finished) {
    return `<div class="muted" style="font-size:12.5px">所有选中的部分都已完成。
      右侧可查看每个部分的正文与决策轨迹。</div>
      <div class="row tight" style="margin-top:8px">
        <button class="btn small ghost" id="wrExport">导出 Markdown</button>
      </div>`;
  }
  const q = snap.question || {};
  if (q.kind === "intake") return renderIntake(q);
  if (q.kind === "section_choice") return renderSectionChoice(q);
  if (q.kind === "gap_decision") return renderGapDecision(q);
  if (q.kind === "custom_plan_choice") return renderCustomPlan(q);
  return `<div class="muted">无可交互的问题。</div>`;
}

function renderIntake(q) {
  if (q.step === "genre") {
    const optsHtml = (q.options || []).map((o) => `
      <button class="btn small ${o.suggested ? "primary" : ""}" data-genre="${h(o.id)}">
        ${h(o.label)}${o.suggested ? "（建议）" : ""}
        <span class="muted" style="font-size:10.5px">${h(o.sections)} 节</span>
      </button>`).join("");
    return `<div class="q-title">${h(q.question)}</div>
      <div class="row tight" style="margin-top:8px">${optsHtml}</div>`;
  }
  if (q.step === "topic") {
    const preset = (q.input || {}).preset || "";
    return `<div class="q-title">${h(q.question)}</div>
      <input type="text" id="wrTopicInput" value="${h(preset)}"
        placeholder="${h((q.input || {}).placeholder || "")}" style="margin-top:8px" />
      <div class="row tight" style="margin-top:8px">
        <button class="btn small primary" id="wrTopicOk">就用这个主题</button>
      </div>`;
  }
  // sections
  const itemsHtml = (q.items || []).map((it) => `
    <label class="pick-item ${it.generates_body ? "" : "off"}">
      <input type="checkbox" data-section="${h(it.key)}"
        ${it.selected ? "checked" : ""} ${it.generates_body ? "" : "disabled"} />
      <span>${h(it.heading)}</span>
      <span class="muted" style="font-size:10.5px">${
        it.generates_body ? `${h(it.words)} 字` : "不生成正文"}</span>
    </label>`).join("");
  return `<div class="q-title">${h(q.question)}</div>
    <div class="pick-list" style="margin-top:8px">${itemsHtml}</div>
    <div class="muted" style="font-size:11px;margin-top:4px">${h(q.note || "")}</div>
    <div class="row tight" style="margin-top:8px">
      <button class="btn small primary" id="wrSectionsOk">确认，开始逐部分访谈</button>
    </div>`;
}

function renderSectionChoice(q) {
  const optsHtml = (q.options || []).map((o) => {
    if (o.id === "ai" || o.id === "self") {
      return `<button class="btn small ghost" data-choice="${h(o.id)}">${h(o.label || o.id)}</button>`;
    }
    return `<button class="btn small" data-choice="${h(o.id)}" style="text-align:left;
      display:block;width:100%;margin-bottom:6px">
      <b>方案 ${h(o.id)}</b><div class="muted" style="font-size:11.5px;margin-top:2px">
      ${h(o.summary)}</div></button>`;
  }).join("");
  return `<div class="q-title">「${h(q.heading)}」要写什么内容？</div>
    ${q.role ? `<div class="muted" style="font-size:11.5px;margin:4px 0">职责：${h(q.role)}</div>` : ""}
    <div style="margin-top:6px">${optsHtml}</div>
    <div class="row tight" style="margin-top:6px">
      <input type="text" id="wrSelfText" placeholder="我自己写：填在这里，再点右边" />
      <button class="btn small ghost" id="wrSelfOk">用我写的内容</button>
    </div>`;
}

function renderGapDecision(q) {
  const v = q.verdict || {};
  const unmet = (v.unmet_dimensions || []).map((d) => DIM_LABELS[d] || d);
  const counts = v.counts || {};
  const opt = (q.options || []).find((o) => o.id === "collect") || {};
  const rounds = opt.rounds || {};
  return `<div class="q-title">「${h(q.heading)}」的支撑不足，怎么办？</div>
    <div class="warnbox" style="margin-top:6px">
      未达标：<b>${h(unmet.join("、") || "—")}</b>
      <div class="muted" style="font-size:11px;margin-top:3px">
        命中文献 ${h(counts.matched_papers ?? 0)} 篇 · 证据 ${h(counts.evidence_ids ?? 0)} 条
        ${(q.suggested_queries || []).length
          ? " · 建议检索词：" + h((q.suggested_queries || []).join("、")) : ""}</div>
    </div>
    <div class="pick-list" style="margin-top:8px">
      <label class="pick-item"><input type="radio" name="gap" value="keep_gap" checked />
        <span>保留缺口，照常撰写</span>
        <span class="muted" style="font-size:10.5px">正文顶部会插入显式缺口标注</span></label>
      <label class="pick-item"><input type="radio" name="gap" value="collect" />
        <span>执行检索补全，够了再写</span></label>
      <label class="pick-item"><input type="radio" name="gap" value="custom" />
        <span>自定义任务</span>
        <span class="muted" style="font-size:10.5px">用你自己的话说明要补什么</span></label>
    </div>
    <div class="row tight" style="margin-top:6px">
      <span class="muted" style="font-size:11.5px">补检轮数上限：</span>
      <input type="number" id="wrRounds" min="${h(rounds.min ?? 1)}"
        max="${h(rounds.max ?? 5)}" value="${h(rounds.default ?? 2)}"
        style="width:70px" title="默认 2 轮，可直接改" />
      <label class="muted" style="font-size:11.5px">
        <input type="checkbox" id="wrRoundsAi" /> 让 AI 决定</label>
      <span class="muted" style="font-size:11px">
        （默认 ${h(rounds.default ?? 2)} 轮，上限 ${h(rounds.max ?? 5)}）</span>
    </div>
    <input type="text" id="wrCustomText" placeholder="自定义任务：例如「补 3 篇讲区域选择性的最新文献」"
      style="margin-top:6px" />
    <div class="row tight" style="margin-top:8px">
      <button class="btn small primary" id="wrGapOk">就这么办</button>
    </div>`;
}

function renderCustomPlan(q) {
  const plansHtml = (q.plans || []).map((p) => `
    <label class="pick-item">
      <input type="radio" name="cplan" value="${h(p.id)}" />
      <span><b>方案 ${h(p.id)}</b>：${h(p.label)}</span>
      <span class="muted" style="font-size:10.5px">${h(p.hint || "")} · 补齐 ${h((p.fills || []).map((d) => DIM_LABELS[d] || d).join("、"))}</span>
    </label>`).join("");
  return `<div class="q-title">我把你的要求理解成三套执行方案，你选一个：</div>
    <div class="muted" style="font-size:11.5px;margin:4px 0">你的要求：${h(q.custom_input || "")}</div>
    <div class="pick-list" style="margin-top:6px">${plansHtml}
      <label class="pick-item"><input type="radio" name="cplan" value="ai" />
        <span>让 AI 自己决定</span></label>
    </div>
    <input type="text" id="wrRefineText" placeholder="再明确一点：补充说明后我会重新解析出 3 个方案"
      style="margin-top:6px" />
    <div class="row tight" style="margin-top:8px">
      <button class="btn small primary" id="wrCustomOk">用选中的方案</button>
      <button class="btn small ghost" id="wrRefineOk">按补充说明重新解析</button>
    </div>`;
}

function renderSections() {
  const rowsHtml = (snap.sections || []).map((s) => {
    const stage = STAGE_LABEL[s.stage] || s.stage;
    const tone = STAGE_TONE[s.stage] || "gray";
    const v = s.verdict || {};
    const unmet = (v.unmet_dimensions || []).map((d) => DIM_LABELS[d] || d);
    const collab = s.collaboration || {};
    return `<div class="card flat" data-sec="${h(s.section_key)}" style="margin-bottom:8px">
      <div class="row" style="justify-content:space-between">
        <b style="font-size:12.5px">${h(s.heading)}</b>
        <span class="badge b-${tone}">${h(stage)}</span>
      </div>
      <div class="muted" style="font-size:11px;margin-top:3px">
        ${s.content_chars ? `${h(s.content_chars)} 字符` : "尚无正文"}
        ${v.decision ? ` · 判定 ${h(v.decision)}` : ""}
        ${unmet.length ? ` · 缺 ${h(unmet.join("、"))}` : ""}
        ${collab.summary ? ` · 协作：${h(collab.summary)}` : ""}
        ${s.error ? ` · <span style="color:var(--red)">${h(s.error)}</span>` : ""}</div>
      <div class="row tight" style="margin-top:6px">
        <button class="btn small ghost" data-view="${h(s.section_key)}">看正文</button>
        <button class="btn small ghost" data-trace="${h(s.section_key)}">决策轨迹</button>
      </div>
      <div class="hidden" data-detail="${h(s.section_key)}"></div>
    </div>`;
  }).join("");
  return card(`<div class="muted" style="font-size:11.5px;margin-bottom:8px">
      这里只展示结果与只读入口——问答全部发生在左侧那一个块里。</div>
    ${rowsHtml || empty("还没有选定的部分")}`,
    { title: "执行摘要" });
}

// ------------------------------------------------------------------ 交互

function bindConversation() {
  // 用**事件委托**绑在持久容器上，而不是逐个按钮绑在会被重建的子节点上。
  // 为什么：render() 会整块替换 #wrBody，若绑定的是那一刻查到的子元素，
  // 而 DOM 里存在的其实是另一个同类元素（曾经 #wrInput 被创建过两次），
  // 就会出现"按钮看得见、点了毫无反应、也不报错"——实测踩过这个坑，
  // 只有真浏览器能发现（HTTP 与静态检查都看不见）。
  const root = $("#wrBody");
  if (!root) return;

  root.onclick = async (event) => {
    const el = event.target instanceof Element ? event.target : null;
    if (!el) return;
    const pick = (attr) => el.closest(`[${attr}]`)?.getAttribute(attr);
    const q = snap.question || {};

    // 前置：体裁
    const genre = pick("data-genre");
    if (genre) return answerAndAdvance({ kind: "intake", step: "genre", value: genre });

    // 前置：主题
    if (el.closest("#wrTopicOk")) {
      const value = $("#wrTopicInput")?.value.trim();
      if (!value) { toast("请填写主题", "warn"); return; }
      return answerAndAdvance({ kind: "intake", step: "topic", value });
    }
    // 前置：部分
    if (el.closest("#wrSectionsOk")) {
      const picked = [...root.querySelectorAll("[data-section]:checked")]
        .map((node) => node.dataset.section);
      if (!picked.length) { toast("至少要选一个部分", "warn"); return; }
      return answerAndAdvance({ kind: "intake", step: "sections", value: picked });
    }
    // 部分：选方案 / 让 AI 定 / 我自己写
    const choice = pick("data-choice");
    if (choice) {
      return answerAndAdvance({ kind: "section_choice",
                                section_key: q.section_key, choice });
    }
    if (el.closest("#wrSelfOk")) {
      const text = $("#wrSelfText")?.value.trim();
      if (!text) { toast("请先写下内容", "warn"); return; }
      return answerAndAdvance({ kind: "section_choice", section_key: q.section_key,
                                choice: "self", text });
    }
    // 缺口决定
    if (el.closest("#wrGapOk")) {
      const decided = root.querySelector('[name="gap"]:checked')?.value || "keep_gap";
      const payload = { kind: "gap_decision", section_key: q.section_key,
                        decision: decided };
      if (decided === "collect") {
        payload.rounds = $("#wrRoundsAi")?.checked ? "ai" : ($("#wrRounds")?.value || 2);
      }
      if (decided === "custom") {
        const text = $("#wrCustomText")?.value.trim();
        if (!text) { toast("自定义任务需要写一句说明", "warn"); return; }
        payload.text = text;
      }
      return answerAndAdvance(payload);
    }
    // 自定义方案
    if (el.closest("#wrCustomOk")) {
      const picked = root.querySelector('[name="cplan"]:checked')?.value;
      if (!picked) { toast("请选一个方案", "warn"); return; }
      return answerAndAdvance({ kind: "custom_plan", section_key: q.section_key,
                                choice: picked });
    }
    if (el.closest("#wrRefineOk")) {
      const text = $("#wrRefineText")?.value.trim();
      if (!text) { toast("请写出要补充的说明", "warn"); return; }
      return answerAndAdvance({ kind: "custom_plan", section_key: q.section_key,
                                choice: "refine", text });
    }
    // 头部动作
    if (el.closest("#wrSwitch")) {
      stopPolling();
      current = null;
      snap = null;
      return refresh();
    }
    if (el.closest("#wrRestart")) {
      if (!window.confirm("重新访谈会清空本次问答进度（已写好的正文保留），继续？")) return;
      try {
        snap = await api.post(
          `/api/writing/projects/${current.project_id}/interview/start`,
          { reset: true });
        render();
        toast("已重置访谈");
      } catch (err) { toastError(err); }
      return undefined;
    }
    if (el.closest("#wrExport")) {
      try {
        const res = await api.get(`/api/writing/projects/${current.project_id}/export`);
        if (!res.ok) throw new Error(res.error || "导出失败");
        const blob = new Blob([res.content], { type: res.mime });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = res.filename;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
      } catch (err) { toastError(err); }
      return undefined;
    }
    // 只读入口
    const view = pick("data-view");
    if (view) return toggleDetail(view, "content");
    const trace = pick("data-trace");
    if (trace) return toggleDetail(trace, "trace");
    return undefined;
  };

  // 对话滚动到底
  const chat = $("#wrChat");
  if (chat) chat.scrollTop = chat.scrollHeight;
}

async function toggleDetail(key, mode) {
  // 用 dataset 匹配而不是拼选择器：key 来自体裁定义（pack 数据），
  // 拼进 `[data-detail="…"]` 会因转义规则不同而取不到，也构成注入面。
  const box = [...host.querySelectorAll("[data-detail]")]
    .find((el) => el.dataset.detail === String(key));
  if (!box) return;
  if (!box.classList.contains("hidden") && box.dataset.mode === mode) {
    box.classList.add("hidden");
    return;
  }
  box.dataset.mode = mode;
  box.classList.remove("hidden");
  box.innerHTML = loading("读取中…");
  try {
    if (mode === "content") {
      const res = await api.get(
        `/api/writing/projects/${current.project_id}/sections/${encodeURIComponent(key)}/content`);
      const body = res.content || "";
      box.innerHTML = `<div class="preview-body" style="margin-top:6px">${h(body)}</div>`;
    } else {
      const res = await api.get(
        `/api/writing/projects/${current.project_id}/sections/${encodeURIComponent(key)}/trace`);
      const rowsHtml = (res.rounds || []).map((rd) => {
        const v = rd.sufficiency || {};
        const dims = v.dimensions || {};
        const dimText = Object.keys(DIM_LABELS)
          .filter((k) => (v.required_dimensions || []).includes(k))
          .map((k) => `${DIM_LABELS[k]} ${Math.round((dims[k] || 0) * 100)}%`)
          .join(" · ");
        return `<div class="trace-round">
          <div class="row tight" style="align-items:center">
            <span class="badge b-gray">第 ${h(rd.round)} 轮</span>
            <span class="badge b-${rd.decision === "sufficient" ? "green" : "amber"}">${h(rd.decision || rd.stage)}</span>
            <span class="muted" style="font-size:11px">${h(rd.template_key || "")}</span>
          </div>
          <div class="muted" style="font-size:11.5px;margin-top:3px">${h(dimText)}</div>
          ${(v.reasons || []).length ? `<ul class="reason-list">${v.reasons
            .map((r) => `<li>${h(r)}</li>`).join("")}</ul>` : ""}
        </div>`;
      }).join("");
      box.innerHTML = `<div style="margin-top:6px">${rowsHtml || empty("无判定记录")}</div>`;
    }
  } catch (err) {
    box.innerHTML = errorBox(err);
  }
}
