/* 前端冒烟：在 Node 里真实 import 所有 ES module 并校验页面契约。
 *
 * 目的：`node --check` 只能证明"语法正确"，证明不了"模块能加载、依赖都存在、
 * 页面导出了约定的字段"。本脚本用一个最小 DOM stub 把模块真正跑一遍导入过程。
 *
 * 用法：node scripts/frontend_smoke.mjs <static 目录>
 * 退出码：0 通过；1 有失败。
 */
import { pathToFileURL } from "node:url";
import path from "node:path";

const staticDir = process.argv[2];
if (!staticDir) {
  console.error("用法: node scripts/frontend_smoke.mjs <static 目录>");
  process.exit(2);
}

const results = [];
const check = (ok, label, detail = "") => {
  results.push([ok, label]);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${label}${!ok && detail ? "  -- " + detail : ""}`);
};

/* ---------------- 最小 DOM stub ---------------- */
function makeElement(tag = "div") {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    classList: {
      _set: new Set(),
      add(...names) { names.forEach((n) => this._set.add(n)); },
      remove(...names) { names.forEach((n) => this._set.delete(n)); },
      toggle(name, on) {
        const want = on === undefined ? !this._set.has(name) : Boolean(on);
        if (want) this._set.add(name); else this._set.delete(name);
        return want;
      },
      contains(name) { return this._set.has(name); },
    },
    style: {},
    dataset: {},
    innerHTML: "",
    textContent: "",
    value: "",
    checked: false,
    appendChild(child) { this.children.push(child); return child; },
    removeChild(child) { this.children = this.children.filter((c) => c !== child); },
    remove() {},
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    scrollIntoView() {},
    click() {},
    setAttribute() {},
    getAttribute() { return null; },
    focus() {},
    insertAdjacentHTML() {},
  };
  return el;
}

const registry = new Map();
globalThis.window = globalThis;
globalThis.document = {
  readyState: "complete",
  hidden: false,
  body: makeElement("body"),
  documentElement: makeElement("html"),
  activeElement: null,
  createElement: (tag) => makeElement(tag),
  createTextNode: (t) => ({ text: t }),
  getElementById(id) { return registry.get(id) || null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  removeEventListener() {},
};
// 预置 index.html 中存在的容器，便于页面 mount 时找到宿主
["nav", "pageHost", "pageTitle", "pageSub", "dbSelect", "dbLabel",
 "refreshBtn", "autoRefresh", "lastRefresh", "toast"].forEach((id) => {
  registry.set(id, makeElement("div"));
});
globalThis.location = { hash: "", reload() {} };
globalThis.history = { replaceState() {}, pushState() {} };
// Node 24 的 navigator 是只读 getter，用 defineProperty 覆盖
try {
  Object.defineProperty(globalThis, "navigator", {
    value: { clipboard: { writeText: async () => {} } },
    configurable: true,
    writable: true,
  });
} catch (err) {
  console.warn("navigator 覆盖失败（忽略）:", err.message);
}
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
globalThis.URL.createObjectURL = () => "blob:stub";
globalThis.URL.revokeObjectURL = () => {};
globalThis.Blob = class { constructor(parts) { this.parts = parts; } };
globalThis.confirm = () => false;
globalThis.prompt = () => null;
globalThis.fetch = async () => ({
  ok: true,
  status: 200,
  text: async () => "{}",
  headers: { get: () => "application/json" },
});
globalThis.vis = { DataSet: class { constructor(items) { this.items = items; } },
                   Network: class { constructor() {} destroy() {} on() {} moveTo() {} } };
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.setTimeout = (fn) => 0;

/* ---------------- 逐个导入模块 ---------------- */
const INFRA = ["js/app.js", "js/pages/shell.js", "js/common/dom.js",
               "js/common/api.js", "js/common/state.js", "js/common/ui.js",
               "js/ui/design.js"];
const PAGES = ["dashboard", "planretrieve", "library", "knowledge", "ontology",
               "experiment", "writing", "review", "system"];

const loaded = new Map();
for (const rel of INFRA) {
  const url = pathToFileURL(path.join(staticDir, rel)).href;
  try {
    await import(url);
    check(true, `import ${rel}`);
  } catch (err) {
    check(false, `import ${rel}`, err.message);
  }
}

for (const key of PAGES) {
  const rel = `js/pages/${key}.js`;
  const url = pathToFileURL(path.join(staticDir, rel)).href;
  try {
    const mod = await import(url);
    loaded.set(key, mod);
    check(true, `import ${rel}`);
  } catch (err) {
    check(false, `import ${rel}`, err.message);
  }
}

/* ---------------- 页面契约 ---------------- */
for (const key of PAGES) {
  const mod = loaded.get(key);
  if (!mod) continue;
  const page = mod[`${key}Page`];
  if (!page) {
    check(false, `${key}: 导出 ${key}Page`, `实际导出: ${Object.keys(mod).join(", ")}`);
    continue;
  }
  const missing = ["title", "group", "mount", "refresh"].filter((f) => !(f in page));
  check(missing.length === 0, `${key}: 页面契约完整`, `缺少 ${missing.join(", ")}`);
  check(typeof page.mount === "function" && typeof page.refresh === "function",
        `${key}: mount/refresh 是函数`);
  // 真正调用 mount（stub DOM 下不应抛错）
  try {
    page.mount(makeElement("section"));
    check(true, `${key}: mount() 未抛错`);
  } catch (err) {
    check(false, `${key}: mount() 未抛错`, err.message);
  }
}

/* ---------------- 注册表一致性 ---------------- */
const registryUrl = pathToFileURL(path.join(staticDir, "js/pages/registry.js")).href;
try {
  const { PAGES: entries, DEFAULT_PAGE } = await import(registryUrl);
  check(Array.isArray(entries) && entries.length === PAGES.length,
        `registry 页面数 = ${PAGES.length}`, `实际 ${entries?.length}`);
  const keys = entries.map((e) => e.key);
  check(PAGES.every((k) => keys.includes(k)), "registry 覆盖全部页面",
        `缺少 ${PAGES.filter((k) => !keys.includes(k)).join(", ")}`);
  check(PAGES.includes(DEFAULT_PAGE), `DEFAULT_PAGE=${DEFAULT_PAGE} 合法`);
  const badMount = entries.filter((e) => typeof e.page?.mount !== "function");
  check(badMount.length === 0, "registry 每个条目都有 mount()",
        badMount.map((e) => e.key).join(", "));
} catch (err) {
  check(false, "import registry.js", err.message);
}

/* ---------------- 汇总 ---------------- */
const failed = results.filter(([ok]) => !ok);
console.log(`\n合计 ${results.length} 项，通过 ${results.length - failed.length}，失败 ${failed.length}`);
if (failed.length) {
  console.log("失败项：");
  failed.forEach(([, label]) => console.log(`  - ${label}`));
  process.exit(1);
}
console.log("前端模块加载与页面契约全部通过 ✅");
