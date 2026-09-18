/* 浏览器交互冒烟：用真实 headless 浏览器跑一遍写作台，并留下截图。
 *
 * 为什么需要它：模块加载冒烟与端到端 HTTP 都验不到「点下去会不会炸」——
 * DOM 事件绑定、选择器拼写、渲染顺序这些只有真浏览器能验。
 * 本脚本补上这一层，输出：
 *   - 每个关键步骤的截图（供人工看外观）
 *   - 页面 console 报错与未捕获异常（任一即失败）
 *
 * 用法：
 *   node scripts/browser_smoke.mjs                     # 自起服务 + 临时库（默认）
 *   node scripts/browser_smoke.mjs --base http://127.0.0.1:8000   # 复用已有服务
 *   node scripts/browser_smoke.mjs --out D:\shots       # 指定截图目录
 */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright-core");

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const argOf = (name, fallback = "") => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const OUT_DIR = path.resolve(argOf("--out", path.join(ROOT, "data", "browser-shots")));
const GIVEN_BASE = argOf("--base", "");
const KEEP = args.includes("--keep");

const results = [];
function check(ok, label, detail = "") {
  results.push({ ok: Boolean(ok), label });
  console.log(`[${ok ? "PASS" : "FAIL"}] ${label}${ok || !detail ? "" : `  -- ${detail}`}`);
}

function freePort() {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

async function waitForServer(base, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`${base}/api/health`);
      if (resp.ok) return true;
    } catch { /* 还没起来 */ }
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

async function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  let server = null;
  let base = GIVEN_BASE;

  if (!base) {
    const port = await freePort();
    base = `http://127.0.0.1:${port}`;
    const dbPath = path.join(ROOT, "data", "browser_smoke.db");
    for (const suffix of ["", "-wal", "-shm"]) {
      const p = `${dbPath}${suffix}`;
      if (existsSync(p)) rmSync(p, { force: true });
    }
    // 用固定种子库，避免每次跑都依赖已有数据
    spawnSync("uv", ["run", "python", "scripts/seed_dashboard_db.py", "--db", dbPath], {
      cwd: ROOT, stdio: "ignore", shell: true,
    });
    server = spawn("uv", ["run", "python", "-m", "research_agent.dashboard.app",
                          "--db", dbPath, "--port", String(port)],
                   { cwd: ROOT, stdio: "ignore", shell: true, detached: false });
    const up = await waitForServer(base);
    check(up, "自起服务可用", base);
    if (!up) { server.kill(); process.exit(1); }
  } else {
    check(await waitForServer(base), "复用已有服务可用", base);
  }

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  const page = await context.newPage();

  // 收集浏览器侧错误：任一出现即视为失败（这是本脚本的核心价值）
  const consoleErrors = [];
  const pageErrors = [];
  const failedResponses = [];
  const recentRequests = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", (err) => pageErrors.push(String(err && err.message || err)));
  // 记下 4xx/5xx 的**具体 URL**：只报"有 console.error"没法排查，
  // 而 Chromium 的 console 文本里不含 URL（实测过）。
  page.on("response", (resp) => {
    if (resp.status() >= 400) {
      failedResponses.push(`${resp.status()} ${resp.request().method()} ${resp.url()}`);
    }
  });
  page.on("request", (req) => {
    if (req.url().includes("/api/")) {
      recentRequests.push(`${req.method()} ${req.url().split("/api")[1]}`);
      if (recentRequests.length > 40) recentRequests.shift();
    }
  });

  const shot = async (name) => {
    await page.screenshot({ path: path.join(OUT_DIR, `${name}.png`), fullPage: false });
  };

  try {
    // ---------------- ① 打开应用 ----------------
    // `#writing?offline=1`：hash 里的 offline 让访谈步骤不注入模型。
    // 浏览器冒烟必须快且可复现，否则每步等真实模型（v4-pro 拟 3 案可能几分钟）
    // 就没法当回归用。读 hash 而不是 query，因为这个 SPA 用 hash 路由。
    await page.goto(`${base}/#writing?offline=1`, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForTimeout(1200);
    check((await page.title()).includes("research-agent"), "页面标题正确");
    check(await page.locator("#pageHost").isVisible(), "主容器已渲染");

    // ---------------- ② 进入写作台 ----------------
    // 已经用 #writing 直接落地，这里只确认落对了页；导航项是 div.nav-item[data-page=…]
    if (!(await page.locator("#wrBody").count())) {
      await page.click('.nav-item[data-page="writing"]');
      await page.waitForTimeout(1200);
    }
    check(await page.getByText("工作规划节点", { exact: false }).first().isVisible(),
          "写作台已挂载（出现访谈面板标题）");
    await shot("01-writing-picker");

    // ---------------- ③ 新建项目并开始访谈 ----------------
    await page.fill("#wrTitle", "浏览器冒烟");
    await page.fill("#wrTopic", "gold catalysis ynamide annulation");
    await page.click("#wrCreate");
    await page.waitForTimeout(1800);
    check(await page.getByText("这次要创作什么类型？").first().isVisible(),
          "Q1 问创作类型");
    await shot("02-q1-genre");

    // Q1 选实验方案
    await page.getByRole("button", { name: /实验方案/ }).first().click();
    await page.waitForTimeout(1200);
    check(await page.getByText("这篇的主题是什么？").first().isVisible(), "Q2 问主题");
    const topicVal = await page.inputValue("#wrTopicInput");
    check(topicVal.length > 0, "主题已预填", JSON.stringify(topicVal));
    await shot("03-q2-topic");

    // Q2 确认主题
    await page.click("#wrTopicOk");
    await page.waitForTimeout(1200);
    check(await page.getByText("要写哪些部分？").first().isVisible(), "Q3 选部分");
    const boxes = await page.locator("[data-section]").count();
    check(boxes >= 7, "部分清单已渲染", `count=${boxes}`);
    check(await page.locator("[data-section]:disabled").count() >= 1,
          "不生成正文的部分被禁用（如参考文献）");
    await shot("04-q3-sections");

    // Q3 只选两个部分（让闭环跑得快）
    await page.locator("[data-section]").evaluateAll((els) => {
      els.forEach((el) => { el.checked = false; });
    });
    await page.locator('[data-section="objective"]').check();
    await page.locator('[data-section="parameters"]').check();
    await page.click("#wrSectionsOk");
    await page.waitForTimeout(1500);

    // ---------------- ④ 逐部分闭环 ----------------
    // 页面会自动起作业（拟 3 个方案）；等它给出选项
    await page.waitForSelector("[data-choice='A']", { timeout: 120000 });
    check(true, "Q4 给出内容方案");
    const optionCount = await page.locator("[data-choice]").count();
    check(optionCount >= 3, "至少有 3 个方案可选", `count=${optionCount}`);
    const firstSummary = (await page.locator("[data-choice='A']").innerText()).slice(0, 60);
    check(firstSummary.length > 10, "方案摘要非空", firstSummary.replace(/\n/g, " "));
    await shot("05-q4-options");

    // 选方案 A
    await page.locator("[data-choice='A']").click();
    // 等判定；不足会问缺口，充足会直接写
    const gapOrWrite = await page.waitForFunction(() => {
      if (document.querySelector("#wrGapOk")) return "gap";
      const badge = document.querySelector("#wrInput");
      return badge && /所有选中的部分都已完成/.test(badge.textContent || "") ? "done" : null;
    }, { timeout: 180000 }).then((h) => h.jsonValue()).catch(() => "timeout");
    if (gapOrWrite === "timeout") {
      // 超时时把现场打出来，否则只能猜（这一层就是为了让失败可诊断）
      const progress = await page.locator("#wrProgress").innerText().catch(() => "?");
      const inputText = (await page.locator("#wrInput").innerText().catch(() => ""))
        .slice(0, 200).replace(/\n/g, " / ");
      console.log(`      [诊断] progress="${progress}"`);
      console.log(`      [诊断] 输入区="${inputText}"`);
      console.log(`      [诊断] 最近请求=${recentRequests.slice(-6).join(" | ")}`);
      console.log(`      [诊断] 页面错误=${pageErrors.slice(0, 3).join(" | ") || "无"}`);
      console.log(`      [诊断] console=${consoleErrors.slice(0, 3).join(" | ") || "无"}`);
      await shot("98-stuck");
    }
    check(gapOrWrite !== "timeout", "判定已完成并进入下一交互", `state=${gapOrWrite}`);
    await shot("06-after-judge");

    if (gapOrWrite === "gap") {
      const gapText = await page.locator("#wrInput").innerText();
      check(/保留缺口/.test(gapText), "缺口决定给出「保留缺口」选项");
      check(/执行检索补全/.test(gapText), "缺口决定给出「执行检索补全」选项");
      check(/自定义任务/.test(gapText), "缺口决定给出「自定义任务」选项");
      check(await page.locator("#wrRounds").isVisible(), "补检轮数可手填");
      check(await page.locator("#wrRoundsAi").isVisible(), "可选「让 AI 决定」轮数");
      await shot("07-gap-decision");
      // 选"保留缺口"并确认
      await page.locator('[name="gap"][value="keep_gap"]').check();
      await page.click("#wrGapOk");
    }

    // 等第一个部分写完（右侧执行摘要出现"已完成"）
    await page.waitForSelector(".badge.b-green", { timeout: 240000 });
    check(true, "首个部分已完成（执行摘要出现绿色徽标）");
    await shot("08-first-section-done");

    const summaryText = await page.locator("#wrBody").innerText();
    check(/已完成/.test(summaryText), "执行摘要显示已完成状态");

    // ---------------- ⑤ 只读入口 ----------------
    // 详情是**异步**加载再渲染的：点完必须等内容真的出现，
    // 只 waitForTimeout 会偶尔读到"读取中…"而误判为没有正文（实测过）。
    await page.locator("[data-view]").first().click();
    const contentText = await page.waitForFunction(() => {
      const box = document.querySelector("[data-detail]");
      const text = box ? box.innerText.trim() : "";
      return text.length > 20 && !/读取中/.test(text) ? text : null;
    }, { timeout: 30000 }).then((h) => h.jsonValue()).catch(() => "");
    check(contentText.length > 20, "「看正文」能展开正文",
          `${contentText.length} 字符`);
    await shot("09-content-drawer");

    await page.locator("[data-trace]").first().click();
    const traceText = await page.waitForFunction(() => {
      const box = document.querySelector("[data-detail]");
      const text = box ? box.innerText.trim() : "";
      return text.length > 10 && !/读取中/.test(text) ? text : null;
    }, { timeout: 30000 }).then((h) => h.jsonValue()).catch(() => "");
    check(traceText.length > 10, "「决策轨迹」能展开判定记录",
          traceText.slice(0, 80).replace(/\n/g, " "));
    await shot("10-trace-drawer");

    // 「换项目」：此前它调了一个不存在的 `refresh()`，点了完全没反应——
    // 浏览器只报 `refresh is not defined`，静态检查与 HTTP 冒烟都看不见。
    {
      const errorsBefore = pageErrors.length + consoleErrors.length;
      await page.click("#wrSwitch");
      const backToPicker = await page.waitForSelector("[data-project], #wrTitle",
        { timeout: 15000 }).then(() => true).catch(() => false);
      check(backToPicker, "「换项目」能回到项目选择视图");
      check(pageErrors.length + consoleErrors.length === errorsBefore,
            "「换项目」没有静默抛错",
            [...pageErrors, ...consoleErrors].slice(-2).join(" | "));
      // 再点回原项目，继续后面的用例
      await page.locator("[data-project]").first().click();
      await page.waitForSelector("#wrDirectText", { timeout: 30000 });
      await shot("10b-switch-project");
    }

    // ---------------- ⑥ 对节点下指令（自然语言 → 派工方案 → 下单） ----------------
    // 这是方案 v8 里唯一的"直下指令"入口。离线模式下解析走确定性关键词识别，
    // 因此这条回归不依赖模型与网络，却能覆盖"解析→选方案→下单→逐步回报"整链。
    // 注意三点：
    //   ① hash 里的 `?offline=1` 会让首跳落到总览页（SPA 路由不认带查询串的 hash）；
    //   ② 离开写作台会 `unmount()` 并清空 `current`，切回来落在**项目选择**视图；
    //   ③ 重新选项目后，若快照里还有未完成的步骤，界面会**自动续跑**（这正是
    //      "重开页面不再卡住"的行为），所以不该假设一定能看到选择题。
    await page.click('.nav-item[data-page="experiment"]');
    await page.waitForTimeout(800);
    await page.click('.nav-item[data-page="writing"]');
    await page.waitForSelector("#wrBody", { timeout: 30000 });
    if (!(await page.locator("#wrDirectText").count())) {
      await page.locator("[data-project]").first().click();
    }
    await page.waitForSelector("#wrDirectText", { timeout: 30000 });
    check(true, "回到写作台仍能继续访谈并看到指令面板");
    await page.fill("#wrDirectText", "重建本体视图");
    await page.click("#wrDirectParse");
    const planCount = await page.waitForFunction(() => {
      const buttons = document.querySelectorAll("[data-dispatch-plan]");
      return buttons.length ? buttons.length : null;
    }, { timeout: 60000 }).then((h) => h.jsonValue()).catch(() => 0);
    check(planCount >= 2, "自然语言指令解析出可执行方案", `${planCount} 个按钮`);

    // 执行第一个方案（离线、无网络：重建本体是纯计算任务）
    await page.locator("[data-dispatch-plan]:not([data-dispatch-plan='ai'])").first().click();
    const dispatched = await page.waitForFunction(() => {
      const text = document.querySelector("#wrBody")?.innerText || "";
      const match = text.match(/派工单\s+(d-[0-9a-f]+)：(\S+)/);
      return match ? { id: match[1], status: match[2] } : null;
    }, { timeout: 60000 }).then((h) => h.jsonValue()).catch(() => null);
    check(Boolean(dispatched), "下单后有派工单回报",
          dispatched ? `${dispatched.id} ${dispatched.status}` : "未出现");
    await shot("11b-directive-dispatched");

    // ---------------- ⑦ 刷新页面后自动续跑未完成的步骤 ----------------
    // 用户报的"卡住"：作业活在进程内存里，刷新/换页就丢了，但状态里的
    // `next_action` 还在。此前 `runStep()` 只在"刚作答完"那一条路上被调用，
    // 于是重开页面永远显示「处理中…」。现在加载快照后会自动续跑。
    {
      const readState = () => page.evaluate(async () => {
        const list = await (await fetch("/api/writing/projects")).json();
        const pid = list[0] && list[0].project_id;
        const snap = await (await fetch(
          `/api/writing/projects/${pid}/interview`)).json();
        return {
          next: snap.next_action || "",
          kind: (snap.question || {}).kind || "",
          finished: Boolean(snap.finished),
          stage: (snap.question || {}).stage || "",
        };
      });
      const before = await readState();
      // 制造"有未完成步骤"的局面：直接问后端状态，然后刷新页面
      await page.reload({ waitUntil: "domcontentloaded" });
      await page.waitForSelector("#wrBody", { timeout: 30000 });
      if (!(await page.locator("#wrDirectText").count())) {
        await page.locator("[data-project]").first().click();
        await page.waitForSelector("#wrDirectText", { timeout: 30000 });
      }
      // 刷新后不该停在"处理中"不动：要么续跑完成（next 变空/换题），要么问出下一题
      const settled = await page.waitForFunction(async (prevNext) => {
        const list = await (await fetch("/api/writing/projects")).json();
        const pid = list[0] && list[0].project_id;
        const snap = await (await fetch(
          `/api/writing/projects/${pid}/interview`)).json();
        const next = snap.next_action || "";
        const kind = (snap.question || {}).kind || "";
        if (snap.finished) return { next, kind, why: "finished" };
        if (next !== prevNext) return { next, kind, why: "moved-on" };
        if (kind && kind !== "working" && !next) {
          return { next, kind, why: "awaiting-user" };
        }
        return null;
      }, before.next, { timeout: 120000 })
        .then((h) => h.jsonValue()).catch(() => null);
      check(Boolean(settled), "刷新页面后自动续跑未完成的步骤",
            settled ? `next ${before.next || "-"} → ${settled.next || "-"}`
                      + `（${settled.why}，kind=${settled.kind}）`
                    : `仍停在 next=${before.next} kind=${before.kind}`);
    }
    await shot("11c-resume-after-reload");

    // ---------------- ⑦ 研究流程页（登记表驱动 + 派工板） ----------------
    // 这一页以前对几乎每个节点都显示"已完成"（有事件就算完成），
    // 而且写作台的访谈节点根本不在清单里。这里守住三条：
    //   1. 节点清单来自登记表，访谈节点必须在；
    //   2. 状态语义里有诚实的「未执行」；
    //   3. 派工单在我们刚跑过访谈后必须出现。
    await page.goto(`${base}/#experiment`, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForSelector("[data-node]", { timeout: 30000 });
    await page.waitForTimeout(600);
    const nodeIds = await page.$$eval("[data-node]",
      (els) => els.map((el) => el.getAttribute("data-node")));
    check(nodeIds.includes("interview"), "研究流程页含写作台访谈节点", `${nodeIds.length} 个节点`);
    check(nodeIds.includes("content_builder") && nodeIds.includes("fact_checker"),
          "研究流程页含内容形成与事实核查节点");
    const pageText = await page.locator("#pageHost").innerText();
    check(/未执行/.test(pageText), "状态里有诚实的「未执行」",
          (pageText.match(/未执行/g) || []).length + " 处");
    check(/当前派工/.test(pageText), "有「当前派工」看板");
    check(/已完成|执行中|未执行/.test(pageText), "节点状态使用明确语义");
    await shot("11-experiment-nodes");

    // 分组筛选：点一个分组按钮后节点仍在（验证委托绑定在重渲染后仍生效）
    const groupBtn = page.locator("[data-group]").nth(1);
    if (await groupBtn.count()) {
      await groupBtn.click();
      await page.waitForTimeout(400);
      const filtered = await page.$$eval("[data-node]", (els) => els.length);
      check(filtered >= 1, "分组筛选后仍有节点渲染", `${filtered} 个`);
      await page.locator("[data-group]").first().click();
      await page.waitForTimeout(300);
    }

    // 点一个节点卡片应当打开详情抽屉
    await page.locator("[data-node='interview']").first().click();
    const drawerText = await page.waitForFunction(() => {
      const box = document.querySelector(".drawer, [class*=drawer]");
      const text = box ? box.innerText.trim() : "";
      return text.length > 20 ? text : null;
    }, { timeout: 15000 }).then((h) => h.jsonValue()).catch(() => "");
    check(drawerText.length > 20, "点节点卡片能打开详情", drawerText.slice(0, 60).replace(/\n/g, " "));
    await shot("12-experiment-drawer");

    // ---------------- ⑧ 浏览器侧错误 ----------------
    check(pageErrors.length === 0, "无未捕获的页面异常", pageErrors.slice(0, 3).join(" | "));
    check(failedResponses.length === 0, "无 4xx/5xx 资源请求",
          failedResponses.slice(0, 5).join(" | "));
    check(consoleErrors.length === 0, "无 console.error",
          consoleErrors.slice(0, 3).join(" | "));
  } catch (err) {
    check(false, "浏览器交互流程未抛错", String(err && err.message || err));
    await shot("99-failure");
  } finally {
    await browser.close();
    if (server && !KEEP) server.kill();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n合计 ${results.length} 项，通过 ${results.length - failed.length}，失败 ${failed.length}`);
  console.log(`截图目录：${OUT_DIR}`);
  if (failed.length) {
    console.log("失败项：");
    for (const f of failed) console.log(`  - ${f.label}`);
    process.exit(1);
  }
  console.log("浏览器交互冒烟全部通过 ✅");
}

main().catch((err) => { console.error(err); process.exit(1); });
