/* 写作台：项目 / 大纲（工作规划节点）/ 节点指令 / 决策轨迹 / 成段正文。
 *
 * 与"直接生成整章"的区别（本页的核心）：
 *
 * 1. **节点是指令入口**：每个大纲节点都能写一句指令（"只写金催化部分，
 *    引用不少于 3 条，强调区域选择性"），指令走完整编排链；
 * 2. **先判后检**：点「征求意见」只会规划 + 判定当前库够不够写，
 *    **不检索、不花钱**；判定结果（含五个维度与缺口）原样展示；
 * 3. **不足不硬写**：库撑不住时返回"缺数据"，列出缺口与建议补检词，
 *    由人决定是否「继续补检一轮」——绝不悄悄产出一段无法回溯的正文；
 * 4. **决策轨迹全留痕**：每轮判定的维度得分、理由、模型是否参与都可见；
 * 5. **引文可点**：正文里的 [n] 绑定真实 paper_key / hyperedge_id，
 *    越界编号会被红标出来（模型编造引文时必须看得见）。
 *
 * 诚实约定：无模型时正文是骨架草稿（generated_by="skeleton_fallback"），
 * 界面一律如实标注，不把确定性兜底说成模型产物。
 */
"use strict";

import { api, qs } from "../common/api.js";
import { $, h, fmtTs, trunc } from "../common/dom.js";
import { store } from "../common/state.js";
import { card, metrics, badge, empty, loading, errorBox, progressRow } from "../ui/design.js";
import { toast, toastError } from "../common/ui.js";

let host = null;
let genres = [];
let projects = [];
let current = null;
let sections = [];
let states = {};
/** section_key -> 该部分的模板（字段、必考维度、职责） */
let templates = {};
/** 当前项目的工作规划（唯一规划节点的产物） */
let workPlan = null;
/** section_key -> {jobId, timer} */
const running = new Map();

const GEN_META = {
  llm: ["模型生成", "green"],
  pack_skeleton: ["pack 骨架", "blue"],
  skeleton_fallback: ["骨架降级（模型不可用）", "amber"],
};

/** 部分状态徽标：draft/planning/needs_data/written/written_with_gaps/stale */
const STATE_META = {
  draft: ["未撰写", "gray", ""],
  planning: ["判定中", "blue", "pulse"],
  insufficient: ["证据不足", "amber", ""],
  needs_data: ["缺数据", "amber", ""],
  written: ["已成段", "green", ""],
  written_with_gaps: ["带缺口成文", "violet", ""],
  stale: ["证据已变", "violet", ""],
  failed: ["失败", "red", ""],
};

const DECISION_META = {
  sufficient: ["充足", "green"],
  insufficient: ["不足", "amber"],
  exhausted: ["预算用尽", "red"],
};

const DIM_LABELS = {
  papers: "命中文献",
  knowledge: "知识覆盖",
  conditions: "量化条件",
  requirements: "指令要求",
  evidence: "可用证据",
  comparison: "可比证据",
};

/** 证据类型的中文名（与后端 section_template.EVIDENCE_TYPE_LABELS 对齐） */
const EVIDENCE_LABELS = {
  citation: "可引用文献",
  mechanism: "机制证据",
  quantitative: "量化条件/数值",
  comparative: "可比研究或对照",
  protocol: "可复现流程",
  risk: "风险与负结果",
};

export const writingPage = {
  title: "写作台",
  subtitle: "大纲节点 · 指令编排 · 决策轨迹",
  group: "研究",
  icon: "✎",

  mount(container) {
    host = container;
    host.innerHTML = `
      <div class="page-head">
        <h1>写作台</h1>
        <div class="desc">每个大纲节点都是一个<b>工作规划节点</b>：写下指令 → 规划并判定当前库是否够写
          → 不足则补检扩库、足够则消费数据成段，全过程留下决策轨迹。
          骨架来自 <code>packs/skills/writing</code>，引用强制写成 <code>[编号]</code>。</div>
      </div>
      <div class="grid-2">
        <div>
          ${card(`
            <div class="stack">
              <input type="text" id="wrTitle" placeholder="项目标题（必填）" />
              <input type="text" id="wrTopic" placeholder="研究主题" />
              <select id="wrGenre"></select>
              <button class="btn primary" id="wrCreate">新建项目</button>
            </div>
            <div class="section-title">已有项目</div>
            <div id="wrList" class="stack"></div>`,
            { title: "项目", subtitle: "一个项目 = 一个体裁 + 一套大纲节点" })}
        </div>
        <div id="wrMain"></div>
      </div>`;
    bind();
  },

  async refresh() {
    if (!genres.length) {
      genres = await api.get("/api/writing/genres");
      const select = $("#wrGenre");
      if (select) {
        select.innerHTML = genres.map((g) => `<option value="${h(g.key)}" ${g.is_default ? "selected" : ""}>
          ${h(g.label)} · ${h((g.sections || []).length)} 节</option>`).join("");
      }
    }
    await loadProjects();
    if (current) await openProject(current.project_id, { silent: true });
  },

  unmount() {
    running.forEach((entry) => clearInterval(entry.timer));
    running.clear();
    host = null;
    current = null;
    store.writingProjectId = null;
  },
};

function bind() {
  $("#wrCreate")?.addEventListener("click", async () => {
    const title = $("#wrTitle")?.value.trim();
    if (!title) { toast("请填写项目标题", "warn"); return; }
    try {
      const res = await api.post("/api/writing/projects", {
        title, topic: $("#wrTopic")?.value.trim() || "",
        genre: $("#wrGenre")?.value || undefined,
      });
      if (!res.ok) throw new Error(res.error || "创建失败");
      $("#wrTitle").value = "";
      $("#wrTopic").value = "";
      toast("项目已创建");
      await loadProjects();
      await openProject(res.project_id);
    } catch (err) { toastError(err); }
  });
}

async function loadProjects() {
  projects = await api.get("/api/writing/projects");
  const list = $("#wrList");
  if (!list) return;
  list.innerHTML = projects.length ? projects.map((p) => `
    <div class="card flat" data-project="${h(p.project_id)}" style="cursor:pointer;
         ${current && current.project_id === p.project_id ? "border-color:var(--accent)" : ""}">
      <div class="row" style="justify-content:space-between">
        <b style="font-size:12.5px">${h(trunc(p.title, 30))}</b>
        ${badge(`${p.filled_sections}/${p.total_sections} 节`, p.filled_sections ? "green" : "gray")}
      </div>
      <div class="muted" style="font-size:11px;margin-top:3px">
        ${h(p.genre)} · ${h(p.topic || "无主题")} · ${h(fmtTs(p.updated_at))}</div>
    </div>`).join("") : empty("还没有项目，先在左侧新建");
  list.querySelectorAll("[data-project]").forEach((el) => {
    el.onclick = () => openProject(Number(el.dataset.project));
  });
}

async function openProject(projectId, { silent = false } = {}) {
  const main = $("#wrMain");
  if (!main) return;
  if (!silent) main.innerHTML = loading("读取项目…");
  try {
    const data = await api.get(`/api/writing/projects/${projectId}`);
    if (!data.ok) throw new Error(data.error || "项目不存在");
    current = data.project;
    sections = data.sections || [];
    store.writingProjectId = current.project_id;
    await loadStates();
    loadWorkPlan(data.work_plan);
    main.innerHTML = renderProject();
    bindProject();
  } catch (err) {
    main.innerHTML = errorBox(err);
  }
}

/** 取工作规划并索引成 templates（每部分的模板信息） */
function loadWorkPlan(plan) {
  workPlan = plan && plan.ok ? plan : null;
  templates = {};
  ((workPlan && workPlan.sections) || []).forEach((s) => {
    templates[String(s.section_key)] = {
      template: s.template,
      role: s.role,
      focus: s.focus,
      focus_source: s.focus_source,
      required_dimensions: s.required_dimensions || [],
      evidence_types: s.evidence_types || [],
      fields: s.fields || [],
      generates_body: s.generates_body,
    };
  });
  // 还没有工作规划时，退回逐部分模板信息（项目详情已带上）
  if (!workPlan) {
    (sections || []).forEach((s) => {
      templates[String(s.section_key)] = {
        template: s.template,
        role: s.role,
        required_dimensions: s.required_dimensions || [],
        evidence_types: s.evidence_types || [],
        fields: s.fields || [],
        generates_body: s.generates_body,
      };
    });
  }
}

async function loadStates() {
  try {
    const data = await api.get(`/api/writing/projects/${current.project_id}/section-states`);
    states = (data && data.states) || {};
  } catch { states = {}; }
}

function renderProject() {
  const genre = genres.find((g) => g.key === current.genre);
  const filled = sections.filter((s) => (s.content || "").trim()).length;
  const needsData = Object.values(states).filter((s) => s.status === "needs_data").length;
  const gapCount = Object.values(states).filter(
    (s) => s.status === "written_with_gaps").length;
  const sectionCardsHtml = sections.map((s) => sectionCard(s)).join("");
  // renderWorkPlan 返回的是**已由 h() 逐值转义**的 HTML 片段，命名带 Html 后缀
  const workPlanHtml = renderWorkPlan(workPlan);
  return `
  ${card(`
    <div class="row" style="justify-content:space-between;align-items:flex-start">
      <div>
        <h2 style="margin:0">${h(current.title)}</h2>
        <div class="muted" style="font-size:12px">
          ${h(genre ? genre.label : current.genre)} · 主题 ${h(current.topic || "—")}
          · ${h(filled)}/${h(sections.length)} 节已成段</div>
      </div>
      <div class="row tight">
        <button class="btn small" id="wrOutline">生成/刷新大纲</button>
        <button class="btn small primary" id="wrPlan">生成工作规划</button>
        <button class="btn small ghost" id="wrExport">导出 Markdown</button>
        <button class="btn small ghost danger" id="wrDelete">删除项目</button>
      </div>
    </div>
    <div id="wrPlanBox" style="margin-top:10px">${workPlanHtml}</div>
    ${needsData ? `<div class="muted" style="font-size:11.5px;margin-top:8px">
      ⚠ ${h(needsData)} 个部分被判「缺数据」且未生成正文，请按缺口补检或换主题。</div>` : ""}
    ${gapCount ? `<div class="muted" style="font-size:11.5px;margin-top:6px">
      ℹ ${h(gapCount)} 个部分带缺口成文——正文顶部已标注缺少哪类证据，可继续补检后重跑。</div>` : ""}`,
    { flat: true })}
  <div class="stack" style="margin-top:12px">
    ${sections.length ? sectionCardsHtml : empty("还没有大纲节点，点「生成/刷新大纲」")}
  </div>`;
}

/** 工作规划表：每个部分写什么、要什么证据、有哪些可填字段 */
function renderWorkPlan(plan) {
  if (!plan || !plan.ok) {
    return `<div class="muted" style="font-size:11.5px">
      还没有工作规划。点「生成工作规划」——只给主题时由系统自拟，填了指令则按你的指令走。</div>`;
  }
  const modeLabel = plan.mode === "user" ? "按你的指令" : "系统自拟";
  const rowsHtml = (plan.sections || []).map((s) => {
    const dims = (s.required_dimensions || []).map((d) => DIM_LABELS[d] || d);
    const ev = (s.evidence_types || []).map((t) => EVIDENCE_LABELS[t] || t);
    const srcTag = { plan: "规划拟定", template: "模板默认", user: "用户指定" }[s.focus_source] || "";
    return `<tr>
      <td><b>${h(s.heading || s.section_key)}</b>${s.generates_body ? "" : ' <span class="chip-tag off">不生成正文</span>'}</td>
      <td>${dims.length ? h(dims.join("、")) : '<span class="muted">—</span>'}</td>
      <td>${ev.length ? h(ev.join("、")) : '<span class="muted">—</span>'}</td>
      <td>${h(s.min_support || 1)}</td>
      <td>${s.focus && s.focus.length ? h(s.focus.slice(0, 3).join("；")) : '<span class="muted">—</span>'}
        ${srcTag ? `<span class="chip-tag">${h(srcTag)}</span>` : ""}</td>
    </tr>`;
  }).join("");
  return `<div class="card flat">
    <div class="row" style="justify-content:space-between">
      <div class="row tight" style="align-items:center">
        <span class="badge b-${plan.mode === "user" ? "blue" : "green"}">${h(modeLabel)}</span>
        <span class="muted" style="font-size:11.5px">${h(plan.section_count)} 个部分 ·
          ${h(plan.generates_body_count)} 个生成正文 · 规划模式 ${h(plan.planner_mode || "—")}</span>
      </div>
      <span class="muted" style="font-size:11px">主题：${h(trunc(plan.topic || "—", 40))}</span>
    </div>
    <div class="table-wrap" style="margin-top:8px">
      <table class="data">
        <thead><tr><th>部分</th><th>必考维度</th><th>证据类型</th><th>保全</th><th>写作要点</th></tr></thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>
    ${plan.model_unavailable_reason ? `<div class="muted" style="font-size:11px;margin-top:6px">
      模型不可用：${h(plan.model_unavailable_reason)}（规划由模板与确定性规则给出）</div>` : ""}
  </div>`;
}

/** 部分卡片：模板表单（可选填）+ 两个动作 + 状态 + 正文 + 轨迹容器 */
function sectionCard(s) {
  const key = String(s.section_key);
  const st = states[key] || {};
  const status = st.status || s.status || "draft";
  const [label, tone, extra] = STATE_META[status] || [status, "gray", ""];
  const grounded = st.grounded_on || {};
  const invalid = grounded.invalid_indices || [];
  const content = s.content || "";
  const charCount = content.length;
  const citations = (s.citation_ids || []).length;
  const tpl = templates[key] || st.template || null;
  const unmet = grounded.unmet_dimensions || [];
  const dims = (tpl && tpl.required_dimensions) || [];
  const dimLabels = dims.map((d) => DIM_LABELS[d] || d);
  return `<div class="card" data-section="${h(key)}">
    <div class="row" style="justify-content:space-between;align-items:flex-start">
      <div class="row tight" style="align-items:center">
        <b>${h(s.heading || key)}</b>
        <span class="badge b-${tone} ${extra}" data-role="stateBadge">${h(label)}</span>
        ${invalid.length ? `<span class="badge b-red">越界引文 ${h(invalid.length)} 处</span>` : ""}
        ${unmet.length ? `<span class="badge b-amber" title="允许带缺口写作，已在正文标注">缺口 ${h(unmet.length)} 项</span>` : ""}
      </div>
      <div class="row tight">
        <span class="muted" style="font-size:11.5px" data-role="stat">
          ${h(charCount)} 字符 · 引用 ${h(citations)} 条${grounded.rounds ? ` · ${h(grounded.rounds)} 轮判定` : ""}</span>
      </div>
    </div>

    <div class="tpl-meta">
      ${dims.length ? `<span class="muted" style="font-size:11.5px">本节要求：
        <b>${h(dimLabels.join("、"))}</b></span>` : `<span class="muted" style="font-size:11.5px">本节未配模板（按体裁默认判定）</span>`}
      ${(tpl && tpl.role) ? `<div class="muted" style="font-size:11px;margin-top:3px">${h(tpl.role)}</div>` : ""}
    </div>

    <div class="node-instruct" style="margin-top:9px">
      <div data-role="fields"></div>
      <input type="text" data-role="instruction" placeholder="补充指令（可留空 → 由系统按主题自拟）" style="margin-top:6px" />
      <div class="row tight" style="margin-top:6px">
        <button class="btn small ghost" data-act="plan">预判本节够不够写</button>
        <button class="btn small primary" data-act="compose">撰写本部分</button>
        <button class="btn small ghost" data-act="trace">决策轨迹</button>
        <span class="spacer"></span>
        <button class="btn small ghost" data-act="save" title="仅保存编辑框内容，不调用模型">保存修改</button>
        <button class="btn small ghost" data-act="polish">润色</button>
      </div>
      <div class="muted" data-role="hint" style="font-size:11.5px;margin-top:6px"></div>
    </div>

    <div data-role="planbox" class="hidden" style="margin-top:8px"></div>
    <div data-role="jobbox" class="hidden" style="margin-top:8px"></div>
    <div data-role="tracebox" class="hidden" style="margin-top:8px"></div>

    <div class="section-title" style="margin-top:10px">正文</div>
    <textarea rows="6" data-role="content" style="margin-top:6px">${h(content)}</textarea>
    <div data-role="preview" style="margin-top:8px"></div>
  </div>`;
}

/** 模板表单：字段来自模板声明，预设值直接可用；每个字段标出来源 */
function renderFields(container, tpl) {
  if (!container) return;
  const fields = (tpl && tpl.fields) || [];
  if (!fields.length) {
    container.innerHTML = `<div class="muted" style="font-size:11.5px">
      本部分没有可填字段——直接点「撰写本部分」即由系统按主题自拟。</div>`;
    return;
  }
  container.innerHTML = fields.map((f) => {
    const preset = f.preset === null || f.preset === undefined ? "" : String(f.preset);
    const hasPreset = preset !== "";
    const input = f.type === "textarea"
      ? `<textarea rows="2" data-field="${h(f.key)}" placeholder="${h(f.placeholder || "")}"></textarea>`
      : `<input type="${f.type === "number" ? "number" : "text"}" data-field="${h(f.key)}"
           placeholder="${h(hasPreset ? `默认：${preset}` : (f.placeholder || ""))}" />`;
    return `<div class="tpl-field" data-fieldrow="${h(f.key)}">
      <label class="muted" style="font-size:11.5px">${h(f.label)}</label>
      ${input}
      ${hasPreset ? `<span class="chip-tag" data-role="preset">默认 ${h(preset)}</span>
        <button class="btn small ghost" data-role="usepreset" style="padding:1px 7px;font-size:11px">用默认值</button>` : ""}
      <span class="chip-tag off" data-role="srcmark">${hasPreset ? "未改" : "留空"}</span>
    </div>`;
  }).join("");
  // "用默认值"把预设填进输入框（等价于用户接受默认；来源仍记为模板默认）
  container.querySelectorAll("[data-role='usepreset']").forEach((btn) => {
    btn.onclick = () => {
      const row = btn.closest("[data-fieldrow]");
      const input = row?.querySelector("[data-field]");
      const preset = btn.previousElementSibling?.textContent?.replace("默认 ", "") || "";
      if (input) {
        input.value = preset;
        input.dataset.acceptedPreset = "1";
      }
      const mark = row?.querySelector("[data-role='srcmark']");
      if (mark) { mark.textContent = "已用默认"; mark.classList.remove("off"); }
    };
  });
  container.querySelectorAll("[data-field]").forEach((input) => {
    input.addEventListener("input", () => {
      const row = input.closest("[data-fieldrow]");
      const mark = row?.querySelector("[data-role='srcmark']");
      if (mark) {
        const touched = input.value.trim() !== "";
        mark.textContent = touched ? "已改" : "留空";
        mark.classList.toggle("off", !touched);
      }
    });
  });
}

/** 读表单：只提交用户真正填写的字段（留空的交给规划/模板） */
function readFields(box, tpl) {
  const out = {};
  const fields = (tpl && tpl.fields) || [];
  // 用 dataset 逐项取值，不做选择器拼接：字段名来自 pack 数据，
  // 拼进 `[data-field="..."]` 会因为转义规则不同而取不到（甚至注入）。
  const inputs = [...box.querySelectorAll("[data-field]")];
  fields.forEach((f) => {
    const input = inputs.find((el) => el.dataset.field === String(f.key));
    if (!input) return;
    const value = String(input.value || "").trim();
    // 显式点了"用默认值"也算用户确认，但不当作"用户修改"提交：
    // 提交空值即可让后端回落到模板默认（纯并行，语义最清晰）
    if (value !== "" && input.dataset.acceptedPreset !== "1") {
      out[f.key] = value;
    } else if (value !== "" && f.type === "number") {
      out[f.key] = Number(value);
    }
  });
  return out;
}

function bindProject() {
  $("#wrPlan")?.addEventListener("click", async () => {
    const box = $("#wrPlanBox");
    if (box) box.innerHTML = loading("正在生成工作规划…（只规划，不检索）");
    try {
      // 不传指令 → 系统自拟模式；后端会按主题与体裁部分表排出各部分要写什么
      const res = await api.post(
        `/api/writing/projects/${current.project_id}/plan`, {});
      if (!res.ok) throw new Error(res.error || "生成失败");
      loadWorkPlan(res);
      if (box) box.innerHTML = renderWorkPlan(res);
      toast(res.mode === "user" ? "已按你的指令生成工作规划"
                                : "已由系统自拟工作规划");
    } catch (err) {
      if (box) box.innerHTML = errorBox(err);
      toastError(err);
    }
  });

  $("#wrOutline")?.addEventListener("click", async () => {
    try {
      const res = await api.post(`/api/writing/projects/${current.project_id}/outline`,
        { topic: current.topic || "" });
      if (!res.ok) throw new Error(res.error || "生成失败");
      const [label] = GEN_META[res.generated_by] || [res.generated_by];
      toast(`大纲已更新（${label}，共 ${res.sections.length} 节）`);
      await openProject(current.project_id);
    } catch (err) { toastError(err); }
  });

  $("#wrExport")?.addEventListener("click", async () => {
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
  });

  $("#wrDelete")?.addEventListener("click", async () => {
    if (!window.confirm(`确认删除项目「${current.title}」及其全部节点？`)) return;
    try {
      await api.del(`/api/writing/projects/${current.project_id}`);
      current = null; sections = []; states = {};
      toast("项目已删除");
      await loadProjects();
      const main = $("#wrMain");
      if (main) main.innerHTML = card(empty("请选择或新建一个项目"), { flat: true });
    } catch (err) { toastError(err); }
  });

  host.querySelectorAll("[data-section]").forEach((box) => bindSection(box));
}

function bindSection(box) {
  const key = box.dataset.section;
  const textarea = box.querySelector('[data-role="content"]');
  const instruction = box.querySelector('[data-role="instruction"]');
  const hint = box.querySelector('[data-role="hint"]');
  const planbox = box.querySelector('[data-role="planbox"]');
  const jobbox = box.querySelector('[data-role="jobbox"]');
  const tracebox = box.querySelector('[data-role="tracebox"]');
  const preview = box.querySelector('[data-role="preview"]');
  const setHint = (html) => { if (hint) hint.innerHTML = html; };
  const show = (el, html) => { if (!el) return; el.classList.remove("hidden"); el.innerHTML = html; };

  const section = () => sections.find((s) => String(s.section_key) === String(key)) || {};
  const tpl = templates[String(key)] || section().template || null;
  const fieldsBox = box.querySelector('[data-role="fields"]');
  renderFields(fieldsBox, tpl);

  renderPreview(preview, section());

  box.querySelector('[data-act="plan"]')?.addEventListener("click", async () => {
    const text = instruction?.value.trim() || "";
    const fields = readFields(box, tpl);
    setHint("预判中…（只做规划与判定，<b>不检索</b>）");
    planbox.classList.remove("hidden");
    planbox.innerHTML = loading("正在按本部分的模板判定当前库是否够写…");
    try {
      const res = await api.post(
        `/api/writing/projects/${current.project_id}/sections/${encodeURIComponent(key)}/plan`,
        { instruction: text, fields });
      if (!res.ok) throw new Error(res.error || "规划失败");
      planbox.innerHTML = renderPlan(res);
      setHint("");
    } catch (err) {
      planbox.innerHTML = errorBox(err);
      setHint("");
    }
  });

  box.querySelector('[data-act="compose"]')?.addEventListener("click", async () => {
    // 指令与字段**都可留空** → 系统自拟（工作规划 + 模板默认）
    const text = instruction?.value.trim() || "";
    const fields = readFields(box, tpl);
    const modeHint = (text || Object.keys(fields).length)
      ? "按你的输入" : "由系统自拟";
    setHint(`已提交撰写作业（${modeHint}）…`);
    try {
      const res = await api.post(
        `/api/writing/projects/${current.project_id}/sections/${encodeURIComponent(key)}/compose`,
        { instruction: text, fields });
      if (!res.ok) throw new Error(res.error || "提交失败");
      jobbox.classList.remove("hidden");
      jobbox.innerHTML = loading("作业已启动，等待充分性判定…");
      toast(`撰写作业已启动（${modeHint}）`);
      pollJob(key, res.job_id, box);
    } catch (err) { toastError(err); setHint(""); }
  });

  box.querySelector('[data-act="trace"]')?.addEventListener("click", async () => {
    if (!tracebox.classList.contains("hidden")) {
      tracebox.classList.add("hidden");
      return;
    }
    tracebox.classList.remove("hidden");
    tracebox.innerHTML = loading("读取决策轨迹…");
    try {
      const res = await api.get(
        `/api/writing/projects/${current.project_id}/sections/${encodeURIComponent(key)}/trace`);
      tracebox.innerHTML = renderTrace(res);
    } catch (err) { tracebox.innerHTML = errorBox(err); }
  });

  box.querySelector('[data-act="polish"]')?.addEventListener("click", async () => {
    setHint("润色中…");
    try {
      const res = await api.post(`/api/writing/projects/${current.project_id}/polish`,
        { section_key: key, content: textarea.value });
      if (!res.ok) throw new Error(res.error || "润色失败");
      if (res.polished) {
        textarea.value = res.content;
        setHint('<span class="badge b-green">已润色</span><span class="muted">（覆盖编辑框内容）</span>');
        updateStat(box);
        toast("已润色");
      } else {
        setHint(`<span class="badge b-amber">未润色</span>
          <span class="muted">${h(res.reason || "")}</span>`);
      }
    } catch (err) { toastError(err); setHint(""); }
  });

  box.querySelector('[data-act="save"]')?.addEventListener("click", async () => {
    try {
      const res = await api.request(
        `/api/writing/projects/${current.project_id}/sections`,
        { method: "PUT", body: { section_key: key, heading: section().heading, content: textarea.value } });
      if (!res.ok) throw new Error(res.error || "保存失败");
      sections = res.sections || sections;
      setHint('<span class="badge b-blue">已保存（未调用模型）</span>');
      updateStat(box);
      renderPreview(preview, section());
      toast("已保存");
      await loadProjects();
    } catch (err) { toastError(err); }
  });
}

function updateStat(box) {
  const textarea = box.querySelector('[data-role="content"]');
  const stat = box.querySelector('[data-role="stat"]');
  if (stat && textarea) stat.textContent = `${textarea.value.length} 字符`;
}

/** 轮询作业：状态 → 局部刷新正文 → 状态徽标 */
function pollJob(key, jobId, box) {
  const existing = running.get(key);
  if (existing) clearInterval(existing.timer);
  const jobbox = box.querySelector('[data-role="jobbox"]');
  const hint = box.querySelector('[data-role="hint"]');
  const timer = setInterval(async () => {
    // 页面已切走（节点从文档移除）→ 停止轮询，别在后台一直打接口
    if (!box.isConnected) {
      clearInterval(timer);
      running.delete(key);
      return;
    }
    try {
      const snap = await api.get(`/api/writing/section-jobs/${jobId}`);
      if (!snap.ok) throw new Error(snap.error || "作业不存在");
      const tone = snap.status === "error" ? "red"
        : snap.status === "done" ? (snap.needs_data ? "amber" : "green") : "blue";
      jobbox.innerHTML = `
        <div class="row tight" style="align-items:center">
          <span class="badge b-${tone}">${h(snap.status)}</span>
          <span class="muted" style="font-size:11.5px">${h(snap.message || "")}</span>
          <span class="spacer"></span>
          ${snap.status === "running" || snap.status === "cancelling"
            ? `<button class="btn small ghost danger" data-cancel="${h(jobId)}">取消</button>` : ""}
        </div>
        <div class="muted" style="font-size:11.5px;margin-top:5px">${h(snapElapsed(snap))}</div>`;
      const cancelBtn = jobbox.querySelector("[data-cancel]");
      if (cancelBtn) {
        cancelBtn.onclick = async () => {
          await api.post(`/api/writing/section-jobs/${jobId}/cancel`);
          toast("已请求取消");
        };
      }
      if (snap.status === "running" || snap.status === "cancelling") return;

      clearInterval(timer);
      running.delete(key);
      await afterJobFinished(key, box, snap);
      if (hint) hint.innerHTML = "";
    } catch (err) {
      clearInterval(timer);
      running.delete(key);
      if (jobbox) jobbox.innerHTML = errorBox(err);
    }
  }, 1200);
  running.set(key, { jobId, timer });
}

function snapElapsed(snap) {
  const parts = [];
  if (snap.percent !== undefined) parts.push(`进度 ${snap.percent}%`);
  if (snap.elapsed !== undefined) parts.push(`耗时 ${snap.elapsed}s`);
  return parts.join(" · ");
}

async function afterJobFinished(key, box, snap) {
  const result = snap.result || {};
  const jobbox = box.querySelector('[data-role="jobbox"]');
  const preview = box.querySelector('[data-role="preview"]');
  const textarea = box.querySelector('[data-role="content"]');
  const instruction = box.querySelector('[data-role="instruction"]');

  // 刷新节点正文与状态
  try {
    const fresh = await api.get(
      `/api/writing/projects/${current.project_id}/sections/${encodeURIComponent(key)}/content`);
    if (fresh.ok) {
      if (textarea) textarea.value = fresh.content || "";
      const idx = sections.findIndex((s) => String(s.section_key) === String(key));
      const merged = { ...(sections[idx] || {}), ...fresh };
      if (idx >= 0) sections[idx] = merged;
      states[key] = {
        section_key: key, status: fresh.status,
        grounded_on: fresh.grounded_on || {}, last_run_id: fresh.last_run_id,
        citation_ids: fresh.citation_ids || [],
        content_len: (fresh.content || "").length,
      };
      renderHeader(box, sections[idx] || {});
      updateStat(box);
      renderPreview(preview, merged);
    }
  } catch { /* 刷新失败不影响结论展示 */ }

  const outcome = snap.outcome || result.status || snap.status;
  if (outcome === "written" || outcome === "written_with_gaps") {
    const invalid = result.invalid_indices || [];
    const unmet = result.unmet_dimensions || [];
    const withGaps = outcome === "written_with_gaps";
    const verdict = result.sufficiency || {};
    // 置信度提示必须按**实际数值**判断：带缺口成文时置信度是被闸门封顶的
    // （低于阈值），写死"阈值以上"就是在说假话。
    const conf = result.confidence;
    const thr = verdict.threshold;
    const above = typeof conf === "number" && typeof thr === "number"
      ? conf >= thr : null;
    const confHint = above === null ? "—"
      : above ? `阈值 ${thr} 以上`
      : `低于阈值 ${thr}（闸门封顶，已按缺口处理）`;
    const fromModel = result.generated_by === "llm";
    jobbox.innerHTML = `<div class="card flat">
      ${metrics([
        ["判定", withGaps ? "带缺口" : "充足",
         `${result.rounds || 1} 轮判定`,
         withGaps ? "warn" : "ok"],
        ["置信度", String(conf ?? "—"), confHint, above === false ? "amber" : ""],
        ["来源", fromModel ? "模型" : "骨架降级",
         fromModel ? "由模型撰写" : "未调用模型（缺 API Key）",
         fromModel ? "" : "warn"],
      ])}
      ${withGaps ? `<div class="warnbox">⚠ 以下维度未达标，但本部分模板允许带缺口写作：
        <b>${h(unmet.map((d) => DIM_LABELS[d] || d).join("、"))}</b>。
        正文顶部已插入显式标注，标注范围内的推断请勿直接当结论引用。</div>` : ""}
      ${invalid.length ? `<div class="warnbox">⚠ 正文含 <b>${h(invalid.length)}</b> 处越界引文编号
        （${h(invalid.join("、"))}）——模型引用了素材清单外的来源，请核对后再用。</div>` : ""}
      ${fromModel ? "" : `<div class="muted" style="font-size:11.5px;margin-top:6px">
        未调用模型的原因：${h(result.model_error
          || (snap.result || {}).model_error || "未配置 API Key")}。
        骨架降级只是**如实标注**，不是判定失败——判定本身不依赖模型。</div>`}
      <div class="muted" style="font-size:11.5px;margin-top:6px">
        模板 <code>${h(result.template_key || "—")}</code>
        · ${h(result.work_plan_mode === "user" ? "按你的指令" : "系统自拟")}
        · 已写入正文，决策轨迹可点「决策轨迹」查看。</div>
    </div>`;
    toast(fromModel ? "已生成正文" : "已生成骨架草稿（未调用模型）",
          fromModel ? "info" : "warn");
  } else if (outcome === "needs_data") {
    jobbox.innerHTML = renderNeedsData(result);
    const rerun = jobbox.querySelector('[data-act="rerun"]');
    if (rerun) {
      rerun.onclick = () => {
        // 指令在节点上保留，因此这里就是"按同样指令再跑一轮"；
        // 每次重跑都是新作业 → 重新获得完整的补检预算（不沿用已用尽的计数）。
        toast("已按同样指令重新提交");
        box.querySelector('[data-act="compose"]')?.click();
      };
    }
    toast("证据不足：未生成正文（已列出缺口）", "warn");
  } else if (snap.status === "cancelled") {
    jobbox.innerHTML = `<div class="card flat"><span class="badge b-gray">已取消</span>
      <span class="muted">正文未被修改。</span></div>`;
  } else {
    jobbox.innerHTML = `<div class="card flat"><span class="badge b-red">失败</span>
      <div class="muted" style="font-size:12px;margin-top:5px">${h(snap.error || result.error || "未知错误")}</div></div>`;
    toastError(new Error(snap.error || "作业失败"));
  }
  // 保留指令内容：needs_data 之后要"按同样指令再跑一轮"，清空会让该入口失效
  if (instruction) instruction.value = instruction.value.trim();
  await loadProjects();
}

function renderHeader(box, section) {
  const st = states[String(section.section_key)] || {};
  const status = st.status || section.status || "draft";
  const [label, tone, extra] = STATE_META[status] || [status, "gray", ""];
  const badgeEl = box.querySelector('[data-role="stateBadge"]');
  if (badgeEl) {
    badgeEl.className = `badge b-${tone} ${extra}`;
    badgeEl.textContent = label;
  }
}

/** 征求意见结果：判定 + 五维 + 缺口 + 检索计划 */
function renderPlan(res) {
  const v = res.sufficiency || {};
  const [dLabel, dTone] = DECISION_META[v.decision] || [v.decision || "未判定", "gray"];
  const counts = v.counts || {};
  const dims = v.dimensions || {};
  const weights = v.weights || {};
  const dimRows = Object.keys(DIM_LABELS).map((k) => progressRow(
    DIM_LABELS[k], dims[k] || 0,
    `${Math.round((dims[k] || 0) * 100)}% / 权重 ${Math.round((weights[k] || 0) * 100)}%`,
  )).join("");
  const missing = counts.requirements_missing || [];
  const queries = ((res.plan || {}).retrieval_plan || {}).query_variants || [];
  return `<div class="card flat">
    <div class="row" style="justify-content:space-between">
      <div class="row tight" style="align-items:center">
        <span class="badge b-${dTone}">${h(dLabel)}</span>
        <span class="muted" style="font-size:11.5px">
          置信度 ${h(v.confidence ?? "—")} / 阈值 ${h(v.threshold ?? "—")} · ${h(res.sufficiency_mode || "")}</span>
      </div>
      <span class="muted" style="font-size:11.5px">规划模式 ${h(res.planner_mode || "—")}</span>
    </div>

    <div class="section-title" style="margin-top:10px">判定维度</div>
    ${dimRows}

    <div class="section-title" style="margin-top:8px">依据</div>
    <ul class="reason-list">${(v.reasons || []).map((r) => `<li>${h(r)}</li>`).join("") || "<li>无</li>"}</ul>

    ${missing.length ? `<div class="section-title" style="margin-top:8px">未覆盖的要求</div>
      <div class="row tight">${missing.map((m) => `<span class="chip-tag warn">${h(m)}</span>`).join("")}</div>` : ""}

    ${(v.suggested_queries || []).length ? `<div class="section-title" style="margin-top:8px">建议补检词</div>
      <div class="row tight">${v.suggested_queries.map((q) => `<span class="chip-tag">${h(q)}</span>`).join("")}</div>` : ""}

    <div class="section-title" style="margin-top:8px">本节检索计划</div>
    <div class="row tight">${queries.length
      ? queries.map((q) => `<span class="chip-tag">${h(trunc(q, 60))}</span>`).join("")
      : '<span class="muted">无检索词</span>'}</div>

    ${res.model_unavailable_reason ? `<div class="muted" style="font-size:11px;margin-top:8px">
      模型不可用：${h(res.model_unavailable_reason)}（判定由确定性阈值完成）</div>` : ""}
  </div>`;
}

/** 缺数据：明确"没有生成正文"，并给出可执行的下一步 */
function renderNeedsData(result) {
  const missing = result.missing || [];
  const queries = result.suggested_queries || [];
  return `<div class="warnbox">
    <b>证据不足，本次没有生成正文。</b>
    <div class="muted" style="font-size:11.5px;margin-top:3px">
      已用尽补检预算（${h(result.rounds ?? 0)} 轮），为避免产出无法回溯的段落，正文保持空白。</div>
  </div>
  <div class="card flat" style="margin-top:8px">
    <div class="section-title">缺口</div>
    <div class="row tight">${missing.length
      ? missing.map((m) => `<span class="chip-tag warn">${h(m)}</span>`).join("")
      : '<span class="muted">无明确缺口（整体置信度不足）</span>'}</div>
    <div class="section-title" style="margin-top:8px">建议下一轮补检词</div>
    <div class="row tight">${queries.length
      ? queries.map((q) => `<span class="chip-tag">${h(q)}</span>`).join("")
      : '<span class="muted">无</span>'}</div>
    <div class="muted" style="font-size:11.5px;margin-top:8px">
      先去「数据构建」按这些词补充文献、或到「知识抽取」把已入库文献抽成知识；
      然后<b>再次点「撰写此段」</b>——每次重跑都会重新获得完整的补检预算，
      不会沿用上一轮已用尽的计数。</div>
    <div class="row tight" style="margin-top:8px">
      <button class="btn small" data-act="rerun">按同样指令再跑一轮</button>
      <span class="muted" style="font-size:11px">补齐文献后点此即可，指令沿用输入框内容</span>
    </div>
  </div>`;
}

/** 决策轨迹：逐轮判定 + 补检结果 */
function renderTrace(res) {
  const rounds = res.rounds || [];
  if (!rounds.length) return `<div class="card flat"><span class="muted">还没有判定记录。</span></div>`;
  const roundsHtml = rounds.map((rd) => {
    const v = rd.sufficiency || {};
    const [dLabel, dTone] = DECISION_META[v.decision] || [rd.decision || "—", "gray"];
    const dims = v.dimensions || {};
    const counts = v.counts || {};
    const dimText = Object.keys(DIM_LABELS)
      .map((k) => `${DIM_LABELS[k]} ${Math.round((dims[k] || 0) * 100)}%`).join(" · ");
    const coll = rd.collection || null;
    return `<div class="trace-round">
      <div class="row" style="justify-content:space-between">
        <div class="row tight" style="align-items:center">
          <span class="badge b-gray">第 ${h(rd.round)} 轮</span>
          <span class="badge b-${dTone}">${h(dLabel)}</span>
          <span class="muted" style="font-size:11.5px">${h(rd.stage || "")}</span>
        </div>
        <span class="muted" style="font-size:11px">${h(fmtTs(rd.ts))}</span>
      </div>
      ${v.confidence !== undefined ? `<div class="muted" style="font-size:11.5px;margin-top:4px">
        置信度 ${h(v.confidence)} / 阈值 ${h(v.threshold)} · 命中文献 ${h(counts.matched_papers ?? 0)} 篇
        · 证据 ${h(counts.evidence_ids ?? 0)} 条</div>` : ""}
      ${dimText ? `<div class="muted" style="font-size:11.5px;margin-top:3px">${h(dimText)}</div>` : ""}
      ${(v.reasons || []).length ? `<ul class="reason-list">${v.reasons.map((r) => `<li>${h(r)}</li>`).join("")}</ul>` : ""}
      ${coll ? `<div class="muted" style="font-size:11.5px;margin-top:3px">
        补检：新增 ${h(coll.count ?? 0)} 篇${coll.skipped ? "（未配置检索器，跳过）" : ""}</div>` : ""}
      ${rd.generated_by ? `<div class="muted" style="font-size:11.5px;margin-top:3px">
        成段方式：${h((GEN_META[rd.generated_by] || [rd.generated_by])[0])} · ${h(rd.content_chars || 0)} 字符</div>` : ""}
      ${rd.error ? `<div class="muted" style="font-size:11.5px;color:var(--red)">${h(rd.error)}</div>` : ""}
    </div>`;
  }).join("");
  const go = (res.section || {}).grounded_on || {};
  const bindings = go.bindings || [];
  const bindingHtml = bindings.length ? `
    <div class="section-title" style="margin-top:8px">引文溯源（${h(bindings.length)} 条）</div>
    <ul class="reason-list">${bindings.map((b) => `<li>[${h(b.index)}] → ${h(b.paper_key || "—")}
      ${(b.evidence_ids || []).length ? ` · 证据 ${h((b.evidence_ids || []).join("、"))}` : ""}</li>`).join("")}</ul>`
    : "";
  return `<div class="card flat">
    <div class="muted" style="font-size:11.5px">共 ${h(res.round_count || rounds.length)} 轮判定
      ${res.last_run_id ? ` · run ${h(res.last_run_id)}` : ""}</div>
    <div style="margin-top:8px">${roundsHtml}</div>
    ${bindingHtml}
  </div>`;
}

/** 正文预览：把 [n] 变成可点标记，并标注越界编号 */
function renderPreview(container, section) {
  if (!container) return;
  const text = (section.content || "").trim();
  if (!text) { container.innerHTML = ""; return; }
  const citations = section.citation_ids || [];
  const grounded = (states[String(section.section_key)] || {}).grounded_on
    || section.grounded_on || {};
  const bindings = grounded.bindings || [];
  const byIndex = {};
  bindings.forEach((b) => { byIndex[b.index] = b; });
  const maxIndex = Math.max(citations.length, bindings.length);

  // 注意：``markedHtml`` 是由**已转义文本**再做标记替换得到的 HTML 片段，
  // 命名带 Html 后缀以表明"此处是有意插入 HTML"，不是漏了转义。
  const markedHtml = h(text).replace(/\[(\d+(?:\s*[,\-–]\s*\d+)*)\]/g, (citeLabelHtml, group) => {
    const nums = [];
    group.split(",").forEach((chunk) => {
      const bounds = chunk.split(/[-–]/).map((x) => x.trim());
      if (bounds.length === 2 && bounds.every((x) => /^\d+$/.test(x))) {
        const [a, b] = bounds.map(Number);
        if (a <= b && b - a <= 200) { for (let i = a; i <= b; i += 1) nums.push(i); }
      } else if (/^\d+$/.test(bounds[0])) nums.push(Number(bounds[0]));
    });
    const outOfRange = nums.filter((n) => n < 1 || n > maxIndex);
    const title = nums.map((n) => {
      const b = byIndex[n];
      return b ? `[${n}] ${b.paper_key || ""} ${(b.evidence_ids || []).join(" ")}` : `[${n}] 未绑定`;
    }).join("  |  ");
    return `<span class="cite ${outOfRange.length ? "bogus" : ""}" title="${h(title)}">${citeLabelHtml}</span>`;
  });
  container.innerHTML = `<div class="section-title">预览</div>
    <div class="preview-body">${markedHtml}</div>
    <div class="muted" style="font-size:11px;margin-top:5px">
      方括号编号可悬停查看绑定的文献与证据；红色=素材清单外的编号。</div>`;
}
