"""端到端冒烟：起服务、发真实 HTTP 请求校验新增端点与静态资源。

用法（作为脚本运行）：
    uv run python scripts/smoke_dashboard.py --db data/smoke.db
退出码：0 全部通过；1 有失败（详情打印在 stdout）。
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), label))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f"  -- {detail}" if detail and not ok else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def get(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = resp.read()
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            return resp.status, json.loads(body.decode("utf-8"))
        return resp.status, body.decode("utf-8", errors="replace")


def post(url: str, payload: dict, method: str = "POST", timeout: float = 20.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def seed(db_path: Path) -> None:
    """写入种子数据：3 篇文献 + 质量结果 + 超边 + 一条低信号日志。

    第 3 篇与第 1 篇同主题（金催化炔酰胺环化）且带抽取记录与超边，目的是让
    写作台的「充分性判定」有足够证据走通 **充足 → 成段** 分支——否则冒烟里
    只能看到"缺数据"这一条路径，成段与引文绑定就没人守了。
    """
    from research_agent.db import connect, log_event, save_quality_result, upsert_paper
    conn = connect(db_path)
    papers = [
        {
            "paper_key": "smoke:1", "source": "europepmc",
            "title": "Gold-catalysed annulation of ynamides",
            "abstract": "A vinyl cation intermediate is captured by a nitrile.",
            "doi": "10.1000/smoke1",
            "venue": "Angewandte Chemie (International ed. in English)",
            "source_type": "journal", "pub_year": 2024, "citation_count": 15,
            "authors": [{"name": "Zhang Wei"}, {"name": "Li Na"}],
            "volume": "63", "issue": "12", "pages": "e202312345",
            "fulltext_source": "abstract", "status": "ingested",
        },
        {
            "paper_key": "smoke:2", "source": "pubmed",
            "title": "Silver catalysis in heterocycle synthesis",
            "abstract": "A review of silver-mediated cyclisations.",
            "doi": "10.1000/smoke2", "venue": "Some Unlisted Journal",
            "source_type": "journal", "pub_year": None, "citation_count": 2,
            "authors": [{"name": "Wang Lei"}], "fulltext_source": "abstract",
            "status": "human_review",
        },
        {
            "paper_key": "smoke:3", "source": "europepmc",
            "title": "Regioselectivity in gold-catalysed ynamide annulation",
            "abstract": ("Bulky ligands switch the regioselectivity of gold "
                         "catalysed ynamide annulation towards the "
                         "keteniminium pathway."),
            "doi": "10.1000/smoke3", "venue": "Chemistry – A European Journal",
            "source_type": "journal", "pub_year": 2024, "citation_count": 12,
            "authors": [{"name": "Ivanova Daria"}], "volume": "30",
            "issue": "5", "pages": "e202400777",
            "fulltext_source": "abstract", "status": "ingested",
        },
    ]
    for rec in papers:
        upsert_paper(conn, rec)
    save_quality_result(conn, {
        "paper_key": "smoke:1", "venue_factor": 0.95, "h_factor": 0.6,
        "citation_factor": 0.5, "authority": 0.86, "timeliness": 0.9,
        "quality": 0.88, "decision": "direct", "needs_review": False,
        "meta_missing": [], "rationale": "ok",
    })
    save_quality_result(conn, {
        "paper_key": "smoke:2", "venue_factor": 0.4, "h_factor": 0.4,
        "citation_factor": 0.4, "authority": 0.4, "timeliness": 0.3,
        "quality": 0.36, "decision": "human", "needs_review": True,
        "meta_missing": ["doi"], "rationale": "low",
    })
    save_quality_result(conn, {
        "paper_key": "smoke:3", "venue_factor": 0.9, "h_factor": 0.55,
        "citation_factor": 0.5, "authority": 0.85, "timeliness": 0.9,
        "quality": 0.86, "decision": "direct", "needs_review": False,
        "meta_missing": [], "rationale": "ok",
    })
    # 抽取记录 + 超边：充分性判定的"知识覆盖""可用证据""量化条件"三个维度需要它们。
    # 注意**必须先 init_ontology**：超边三张表由本体层建，核心 schema 里没有，
    # 漏了这一步会让超边数恒为 0（判定里被 sqlite3.Error 静默吞掉，
    # 表现为"量化条件闸门永不触发"这种很难查的假阴性）。
    for key in ("smoke:1", "smoke:3"):
        log_event(conn, "knowledge", "extracted", key,
                  {"entities": 5, "relations": 4})
    from research_agent.ontology.store import (
        init_ontology, upsert_edge, upsert_hyperedge, upsert_node)
    init_ontology(conn)
    ynamide_id, _ = upsert_node(conn, node_type="Substrate",
                                name="N-sulfonyl ynamide", confidence=0.9)
    catalyst_id, _ = upsert_node(conn, node_type="Catalyst",
                                 name="Au(I) phosphine complex", confidence=0.88)
    annul_id, _ = upsert_node(conn, node_type="Reaction",
                              name="Intermolecular annulation", confidence=0.92)
    for src, tgt, rel in ((annul_id, catalyst_id, "catalyzed_by"),
                          (annul_id, ynamide_id, "uses")):
        upsert_edge(conn, relation_type=rel, src_id=src, tgt_id=tgt,
                    confidence=0.9,
                    provenance=[{"paper": "smoke:1", "evidence": "冒烟种子"}])
    # 这里**刻意只建两条边、不建超边**：
    # 一条无条件超边会让既有的"量化条件"闸门判不足，从而挡住旧体裁本该走通的
    # "充足 → 成段"分支（那不是 bug，是闸门在正确工作）。需要真实覆盖
    # "带缺口写作"的用例改为在 experiment_protocol 体裁上做——见
    # "预判：缺量化条件被识别为未达标维度" 与 "带缺口写作" 两组检查，
    # 那里库内确实没有量化条件可用。
    log_event(conn, "retrieval", "relevance-gate-low-signal", "smoke:2",
              {"low_signal": "topic_token_overlap_zero"})
    conn.commit()
    conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "smoke_dashboard.db"))
    ap.add_argument("--keep", action="store_true", help="结束后保留数据库")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    seed(db_path)

    import uvicorn
    from research_agent.config import Settings
    from research_agent.dashboard.app import create_app

    port = free_port()
    # 节级写作的阈值显式给到种子库的规模：种子库只有 3 篇，默认"命中 ≥ 6 篇"
    # 会让成段分支永远走不到（那样"成段 + 引文绑定"就没被冒烟覆盖）。
    smoke_settings = Settings(db_path=str(db_path))
    smoke_settings.section_sufficiency_min_papers = 2
    app = create_app(str(db_path), inject_llms=False, settings=smoke_settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if getattr(server, "started", False):
            break
        time.sleep(0.1)
    time.sleep(0.3)

    try:
        # ---------------- 基础 ----------------
        status, health = get(f"{base}/api/health")
        check(status == 200 and health.get("ok"), "GET /api/health")

        status, index = get(f"{base}/")
        check(status == 200 and "research-agent" in index, "GET / 返回 index.html")
        check('id="nav"' in index and 'id="pageHost"' in index,
              "index.html 含侧边导航与页面容器")
        for asset in ("css/theme.css", "css/shell.css", "js/app.js",
                      "vendor/vis-network.min.js"):
            check(f"/static/{asset}" in index, f"index.html 引用 /static/{asset}")

        for rel in ("static/css/theme.css", "static/css/shell.css",
                    "static/js/app.js", "static/js/common/dom.js",
                    "static/js/common/api.js", "static/js/common/ui.js",
                    "static/js/ui/design.js", "static/js/pages/shell.js",
                    "static/js/pages/registry.js",
                    "static/js/pages/dashboard.js",
                    "static/js/pages/planretrieve.js",
                    "static/js/pages/library.js",
                    "static/js/pages/knowledge.js", "static/js/pages/ontology.js",
                    "static/js/pages/experiment.js", "static/js/pages/writing.js",
                    "static/js/pages/review.js", "static/js/pages/system.js"):
            status, body = get(f"{base}/{rel}")
            check(status == 200 and len(body) > 200, f"GET /{rel}")

        # ---------------- 文献库 ----------------
        status, data = get(f"{base}/api/library/papers?limit=10")
        check(status == 200 and data["total"] == 3, "GET /api/library/papers 总数=3")
        check(len(data["items"]) == 3, "文献库返回 3 条")
        check("facets" in data and "tags" in data["facets"], "列表带 facets")
        first = data["items"][0]
        check("pdf_blob" not in first and "clean_text" not in first,
              "列表不返回大字段")

        status, data2 = get(f"{base}/api/library/papers?year_mode=unknown")
        check([i["paper_key"] for i in data2["items"]] == ["smoke:2"],
              "year_mode=unknown 命中无年份记录")
        status, data3 = get(f"{base}/api/library/papers?low_signal=yes")
        check([i["paper_key"] for i in data3["items"]] == ["smoke:2"],
              "low_signal=yes 过滤低信号")
        status, data4 = get(f"{base}/api/library/papers?decision=human")
        check([i["paper_key"] for i in data4["items"]] == ["smoke:2"],
              "decision=human 过滤")

        status, facets = get(f"{base}/api/library/facets")
        check(status == 200 and facets["year_unknown"] == 1, "facets.year_unknown=1")

        status, stats = get(f"{base}/api/library/stats")
        check(status == 200 and stats["papers"] == 3, "stats.papers=3")

        # ---------------- 侧车写入 ----------------
        status, res = post(f"{base}/api/library/tags",
                           {"paper_key": "smoke:1", "tag": "催化"})
        check(res.get("ok") and "催化" in res.get("tags", []), "POST 添加标签")

        status, res = post(f"{base}/api/library/folders", {"name": "精读"})
        check(res.get("ok") and res.get("folder_id"), "POST 新建文件夹")
        folder_id = res["folder_id"]

        status, res = post(f"{base}/api/library/folders/{folder_id}/papers",
                           {"paper_keys": ["smoke:1"]})
        check(res.get("ok") and res.get("changed") == 1, "POST 文献入文件夹")

        status, res = post(f"{base}/api/library/favorites",
                           {"paper_keys": ["smoke:1"], "value": True})
        check(res.get("ok") and res.get("changed") == 1, "POST 收藏")

        status, sidecar = get(f"{base}/api/library/paper/smoke:1")
        check(sidecar["tags"] == ["催化"] and sidecar["favorite"] is True
              and sidecar["folder_names"] == ["精读"], "侧车聚合读取")
        check(sidecar.get("detail", {}).get("paper", {}).get("title", "").startswith("Gold"),
              "详情含论文元数据")

        status, sc2 = get(f"{base}/api/library/paper/smoke:1")
        check(len(sc2.get("folders") or []) >= 1, "侧车返回文件夹列表")

        # ---------------- 引用与导出 ----------------
        status, cite = get(f"{base}/api/library/citation/smoke:1")
        check(cite.get("ok") and "@article" in cite["rendered"]["bibtex"],
              "引用端点含 BibTeX")
        check("Angew. Chem. Int. Ed." in cite["rendered"]["acs"],
              "ACS 样式用了期刊缩写（来自 pack）")
        check("Q1" not in cite["text"], "引文文本不含分区标签")

        status, export = get(f"{base}/api/library/export?keys=smoke:1&style=gb7714")
        check(export.get("ok") and export["count"] == 1, "导出 gb7714")
        status, export2 = get(f"{base}/api/library/export?keys=smoke:1,missing&style=bibtex")
        check(export2.get("ok") and export2["missing"] == ["missing"],
              "导出报告缺失 key")
        status, export3 = get(f"{base}/api/library/export?keys=smoke:1&style=bogus")
        check(export3.get("ok") is False, "未知导出样式返回错误而非静默回落")

        # ---------------- 批量作业 ----------------
        status, started = post(f"{base}/api/library/batch",
                               {"action": "favorite", "paper_keys": ["smoke:2"],
                                "payload": {"value": True}})
        check(started.get("ok") and started.get("job_id"), "POST 批量作业返回 job_id")
        job_id = started["job_id"]
        snapshot = {}
        for _ in range(60):
            status, snapshot = get(f"{base}/api/library/jobs/{job_id}")
            if snapshot and snapshot["status"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.15)
        check(snapshot.get("status") == "done", "作业完成", str(snapshot))
        status, jobs = get(f"{base}/api/library/jobs")
        check(isinstance(jobs, list) and jobs, "作业列表可读（无自锁）")
        status, fav2 = get(f"{base}/api/library/paper/smoke:2")
        check(fav2["favorite"] is True, "批量收藏已生效")

        # ---------------- 规划入口（无 Key 时必须可用） ----------------
        status, plan_res = post(f"{base}/api/planner/run",
                                {"request": "提出一种炔酰胺合成多元氮杂环的新方法"})
        check(plan_res.get("ok") is True,
              "Planner 端点在无 Key 时仍返回 ok", str(plan_res)[:200])
        check(plan_res.get("planner_mode") in ("llm", "offline_fallback"),
              "Planner 返回 planner_mode", str(plan_res.get("planner_mode")))
        check(bool((plan_res.get("plan") or {}).get("task_kind")),
              "Planner 产出任务单（含 task_kind）")
        if plan_res.get("model_used") is False:
            check(bool(plan_res.get("planner_model_error")),
                  "未用模型时给出原因（便于判断是否缺 Key）")

        # ---------------- 写作台（无 Key 时显式降级并给出原因） ----------------
        status, genres = get(f"{base}/api/writing/genres")
        check(status == 200 and any(g["key"] == "research_article" for g in genres),
              "GET /api/writing/genres")
        status, created = post(f"{base}/api/writing/projects",
                               {"title": "冒烟项目", "topic": "炔酰胺环化"})
        check(created.get("ok"), "创建写作项目")
        project_id = created["project_id"]
        status, outline = post(f"{base}/api/writing/projects/{project_id}/outline",
                               {"use_model": False})
        check(outline.get("ok") and outline["generated_by"] == "pack_skeleton",
              "大纲来自 pack 骨架（无模型）")
        status, detail = get(f"{base}/api/writing/projects/{project_id}")
        check(detail.get("ok") and detail["sections"], "项目详情含章节")
        first = detail["sections"][0]
        status, gen = post(f"{base}/api/writing/projects/{project_id}/sections",
                           {"section_key": first["section_key"],
                            "heading": first["heading"], "use_model": False})
        check(gen.get("ok") and gen["generated_by"] == "skeleton_fallback"
              and "骨架草稿" in gen["content"], "章节显式标注骨架降级")
        check(bool(gen.get("model_unavailable_reason")),
              "骨架降级带回原因（区分缺 Key / 构建失败）",
              str(gen.get("model_unavailable_reason")))
        status, saved = post(f"{base}/api/writing/projects/{project_id}/sections",
                             {"section_key": first["section_key"],
                              "heading": first["heading"], "content": "手工内容"},
                             method="PUT")
        check(saved.get("ok"), "PUT 保存章节")
        status, exp = get(f"{base}/api/writing/projects/{project_id}/export")
        check(exp.get("ok") and "手工内容" in exp["content"], "导出项目 Markdown")

        # ---------------- 大纲节点指令工作流（充分性判定 → 成段 → 轨迹） ----------------
        # 所有涉及模型注入的写入端点都传 use_model=False：
        # **冒烟必须离线可复现**——本机一旦配好 .env，这些端点会真的去调
        # deepseek-v4-pro，导致 20s 超时、整轮冒烟挂掉（模型可用与否不该决定回归成败）。
        # 真实模型的验证另有一套：见 _probe_real_llm.py 与 docs/MERGE_NOTES.md 的说明。
        offline = {"use_model": False}
        node_key = first["section_key"]
        node_instr = ("summarise gold catalysed annulation of ynamides, "
                      "cite at least 1 source")
        status, plan_res = post(
            f"{base}/api/writing/projects/{project_id}/sections/{node_key}/plan",
            {"instruction": node_instr, **offline})
        check(plan_res.get("ok") is True, "节点指令：规划端点可用",
              str(plan_res)[:200])
        verdict = plan_res.get("sufficiency") or {}
        check(verdict.get("decision") == "sufficient",
              "节点指令：种子库被判为充足",
              f"decision={verdict.get('decision')} gates={verdict.get('failed_gates')}")
        check(bool(verdict.get("gates")), "节点指令：判定给出逐项闸门明细")
        check(verdict.get("counts", {}).get("matched_papers", 0) >= 2,
              "节点指令：命中文献数被统计")
        check(bool((plan_res.get("plan") or {}).get("retrieval_plan")),
              "节点指令：产出本节检索计划")
        check(isinstance(verdict.get("suggested_queries"), list),
              "节点指令：补检词字段存在且为列表")
        check("sufficiency_mode" in plan_res and "judged_by" in plan_res,
              "节点指令：如实标注判定是否由模型参与")

        status, job = post(
            f"{base}/api/writing/projects/{project_id}/sections/{node_key}/compose",
            {"instruction": node_instr, **offline})
        check(job.get("ok") is True and job.get("job_id"),
              "节点指令：撰写作业已受理", str(job)[:200])
        job_id = job.get("job_id")
        snap = {}
        for _ in range(60):
            time.sleep(0.4)
            status, snap = get(f"{base}/api/writing/section-jobs/{job_id}")
            if snap.get("status") in ("done", "error", "cancelled"):
                break
        check(snap.get("status") == "done", "节点指令：作业跑到终态",
              f"status={snap.get('status')} error={snap.get('error')}")
        check(snap.get("outcome") == "written",
              "节点指令：证据充足时产出正文",
              f"outcome={snap.get('outcome')}")
        result = snap.get("result") or {}
        check(result.get("generated_by") in ("llm", "skeleton_fallback"),
              "节点指令：如实标注成段方式（模型 / 骨架降级）",
              str(result.get("generated_by")))
        check(result.get("invalid_indices") == [],
              "节点指令：正文无越界引文编号")

        status, content_res = get(
            f"{base}/api/writing/projects/{project_id}/sections/{node_key}/content")
        check(content_res.get("ok") and content_res.get("content"),
              "节点指令：正文已落库")
        check(content_res.get("status") == "written",
              "节点指令：节点状态转为已成段", str(content_res.get("status")))
        grounded = content_res.get("grounded_on") or {}
        check(bool(grounded.get("paper_keys")),
              "节点指令：正文记录了支撑文献（可回溯）")
        check(bool(grounded.get("bindings")),
              "节点指令：引文编号绑定到具体文献")
        check(grounded.get("run_id") == result.get("run_id"),
              "节点指令：正文与作业同源（run_id 一致）")

        status, trace = get(
            f"{base}/api/writing/projects/{project_id}/sections/{node_key}/trace")
        check(trace.get("ok") and trace.get("round_count", 0) >= 2,
              "节点指令：决策轨迹逐轮留痕",
              f"rounds={trace.get('round_count')}")
        decisions = [r.get("decision") for r in (trace.get("rounds") or [])]
        check("sufficient" in decisions, "节点指令：轨迹记录了充足判定")
        check(any((r.get("sufficiency") or {}).get("dimensions")
                  for r in (trace.get("rounds") or [])),
              "节点指令：轨迹含逐维度得分")

        status, states = get(
            f"{base}/api/writing/projects/{project_id}/section-states")
        check(states.get("ok") and node_key in (states.get("states") or {}),
              "节点指令：节点状态徽标接口可用")

        # ---------------- 工作规划（唯一的规划节点）+ 部分模板 ----------------
        # **一律 use_model=False**：冒烟必须离线可复现，不依赖外部模型。
        # 早期没传这个参数，于是在本机配好 .env 之后，这些检查会真的去调
        # deepseek-v4-pro，导致 20s 超时、整轮冒烟挂掉（模型可用与否不该决定回归成败）。
        status, tpls = get(f"{base}/api/writing/templates?genre=experiment_protocol")
        check(status == 200 and tpls.get("ok") and tpls.get("templates"),
              "模板：实验设计体裁有部分模板")
        check(any(t["key"] == "parameters" for t in tpls.get("sections") or []),
              "模板：实验设计含参数与条件节")
        params_tpl = next((t for t in tpls.get("sections") or []
                           if t.get("key") == "parameters"), {})
        check(bool(params_tpl.get("fields")),
              "模板：参数节声明了可填字段且带预设值")

        status, tpls_review = get(f"{base}/api/writing/templates?genre=frontier_review")
        check(status == 200 and len(tpls_review.get("templates") or []) >= 5,
              "模板：文献综述有 5 个以上部分模板")

        # 只给主题、不给任何指令：系统应能自拟工作规划
        status, plan_created = post(f"{base}/api/writing/projects",
                                    {"title": "工作规划冒烟",
                                     "topic": "gold catalysis ynamide annulation",
                                     "genre": "experiment_protocol"})
        plan_pid = plan_created.get("project_id")
        status, wplan = post(f"{base}/api/writing/projects/{plan_pid}/plan", offline)
        check(wplan.get("ok") is True, "工作规划：只给主题即可生成", str(wplan)[:200])
        check(wplan.get("mode") == "auto",
              "工作规划：无指令时进入系统自拟模式", str(wplan.get("mode")))
        plan_sections = wplan.get("sections") or []
        check(len(plan_sections) >= 7, "工作规划：覆盖全部部分",
              f"count={len(plan_sections)}")
        combos = {tuple(sorted(s.get("required_dimensions") or []))
                  for s in plan_sections}
        check(len(combos) >= 3,
              "工作规划：各部分的必考维度彼此不同（不是全篇一套）",
              f"combos={combos}")
        by_key = {s.get("section_key"): s for s in plan_sections}
        check("conditions" in (by_key.get("parameters", {})
                               .get("required_dimensions") or []),
              "工作规划：参数节必考量量化条件")
        check("conditions" not in (by_key.get("objective", {})
                                   .get("required_dimensions") or []),
              "工作规划：目标节不考量量化条件（按部分特征区分）")

        # 用户填了字段 → user 模式并优先
        status, wplan_user = post(
            f"{base}/api/writing/projects/{plan_pid}/plan",
            {"instruction": "只关注配体效应", **offline})
        check(wplan_user.get("mode") == "user",
              "工作规划：给了指令即进入用户模式", str(wplan_user.get("mode")))

        # 预判：参数节应因缺量化条件判不足，且指出缺的是 conditions
        status, pre_plan = post(
            f"{base}/api/writing/projects/{plan_pid}/sections/parameters/plan",
            {"fields": {"params": "无水无氧，-20 °C"}, **offline})
        check(pre_plan.get("ok") is True, "预判：参数节可预判", str(pre_plan)[:200])
        check(pre_plan.get("template_key") == "proto_parameters",
              "预判：取到参数节的模板", str(pre_plan.get("template_key")))
        pv = pre_plan.get("sufficiency") or {}
        check("conditions" in (pv.get("unmet_dimensions") or []),
              "预判：缺量化条件被识别为未达标维度",
              f"unmet={pv.get('unmet_dimensions')} gates={pv.get('gates')}")
        check(pre_plan.get("field_sources", {}).get("user") == ["params"],
              "预判：用户填写的字段被标为 user 来源",
              str(pre_plan.get("field_sources")))

        # 带缺口写作：正文有内容且顶部有标注
        status, gap_job = post(
            f"{base}/api/writing/projects/{plan_pid}/sections/parameters/compose",
            {"fields": {"params": "无水无氧，-20 °C"}, **offline})
        check(gap_job.get("ok") is True, "带缺口写作：作业已受理")
        gap_snap = {}
        for _ in range(60):
            time.sleep(0.4)
            status, gap_snap = get(
                f"{base}/api/writing/section-jobs/{gap_job.get('job_id')}")
            if gap_snap.get("status") in ("done", "error", "cancelled"):
                break
        gap_result = gap_snap.get("result") or {}
        check(gap_snap.get("outcome") == "written_with_gaps",
              "带缺口写作：证据不足但模板允许 → 产出正文并标注",
              f"outcome={gap_snap.get('outcome')} err={gap_snap.get('error')}")
        status, gap_content = get(
            f"{base}/api/writing/projects/{plan_pid}/sections/parameters/content")
        check("本节证据缺口" in (gap_content.get("content") or ""),
              "带缺口写作：正文顶部有显式缺口标注")
        check(gap_content.get("grounded_on", {}).get("template_key")
              == "proto_parameters",
              "带缺口写作：轨迹记录了所用模板")

        status, del_plan = post(f"{base}/api/writing/projects/{plan_pid}", {},
                                method="DELETE")
        check(del_plan.get("ok") is True, "清理：工作规划冒烟项目")

        status, cancelled = post(
            f"{base}/api/writing/section-jobs/does-not-exist/cancel", {})
        check(cancelled.get("ok") is False, "不存在的作业取消返回明确失败")

        # ---------------- 访谈：唯一的交互节点 + 逐部分闭环（离线） ----------------
        status, ivp = post(f"{base}/api/writing/projects",
                           {"title": "访谈冒烟",
                            "topic": "gold catalysis ynamide annulation"})
        iv_pid = ivp.get("project_id")
        status, s = post(f"{base}/api/writing/projects/{iv_pid}/interview/start", {})
        check(s.get("ok") and (s.get("question") or {}).get("kind") == "intake",
              "访谈：启动后进入前置问答")
        check((s["question"] or {}).get("step") == "genre",
              "访谈：第一个问题问创作类型", str(s.get("question"))[:160])
        status, s = post(f"{base}/api/writing/projects/{iv_pid}/interview/answer",
                         {"kind": "intake", "step": "genre",
                          "value": "experiment_protocol"})
        check((s["question"] or {}).get("step") == "topic",
              "访谈：第二个问题问主题", str(s.get("question"))[:160])
        check(bool((s["question"].get("input") or {}).get("preset")),
              "访谈：主题预填了项目主题")
        status, s = post(f"{base}/api/writing/projects/{iv_pid}/interview/answer",
                         {"kind": "intake", "step": "topic",
                          "value": "gold catalysis ynamide annulation"})
        items = (s["question"] or {}).get("items") or []
        check((s["question"] or {}).get("step") == "sections" and bool(items),
              "访谈：第三个问题让用户选部分", str(s.get("question"))[:160])
        check(any(not i.get("generates_body") for i in items),
              "访谈：不生成正文的部分被标出（如参考文献）")
        status, s = post(f"{base}/api/writing/projects/{iv_pid}/interview/answer",
                         {"kind": "intake", "step": "sections",
                          "value": ["objective", "parameters"]})
        check(s.get("total") == 2
              and (s["question"] or {}).get("kind") == "section_choice",
              "访谈：前置问完即进入逐部分闭环", str(s.get("question"))[:160])
        check(s.get("next_action") == "draft_options",
              "访谈：快照告知界面该起作业（拟方案）")

        status, step = post(f"{base}/api/writing/projects/{iv_pid}/interview/step",
                            offline)
        check(step.get("action") == "draft_options" and step.get("job_id"),
              "访谈：推进一步返回作业 id", str(step)[:160])
        iv_snap = {}
        for _ in range(60):
            time.sleep(0.3)
            status, iv_snap = get(
                f"{base}/api/writing/section-jobs/{step['job_id']}")
            if iv_snap.get("status") in ("done", "error", "cancelled"):
                break
        check(iv_snap.get("status") == "done", "访谈：拟方案作业完成",
              f"status={iv_snap.get('status')} err={iv_snap.get('error')}")
        status, s = get(f"{base}/api/writing/projects/{iv_pid}/interview")
        opts = (s.get("sections") or [{}])[0].get("options") or []
        check(len(opts) == 3, "访谈：给出 3 个内容方案", f"got={len(opts)}")
        check(len({o.get("summary") or "" for o in opts}) == 3,
              "访谈：3 个方案的摘要彼此不同（不是同一句话的三种说法）")

        status, s = post(f"{base}/api/writing/projects/{iv_pid}/interview/answer",
                         {"kind": "section_choice", "section_key": "objective",
                          "choice": "A"})
        check(s.get("next_action") == "judge", "访谈：选完方案下一步是判定支撑")

        # 通用推进循环：判定 →（可能问缺口）→ 写作，直到第一个部分完成。
        # 刻意不写死分支：判定可能直接充足（直接进写作），也可能不足（先问缺口），
        # 早期把两者当成必然走缺口分支，导致"充足"路径没人覆盖。
        saw_gap_question = False
        for _ in range(8):
            status, s = get(f"{base}/api/writing/projects/{iv_pid}/interview")
            q = s.get("question") or {}
            first = (s.get("sections") or [{}])[0]
            if first.get("stage") == "done":
                break
            if q.get("kind") == "gap_decision":
                if not saw_gap_question:
                    gap_opts = {o.get("id") for o in q.get("options") or []}
                    check(gap_opts == {"keep_gap", "collect", "custom"},
                          "访谈：缺口决定给出三条路", str(gap_opts))
                    collect = next((o for o in q["options"]
                                    if o.get("id") == "collect"), {})
                    rounds = collect.get("rounds") or {}
                    check(rounds.get("default") == 2 and rounds.get("max") == 5,
                          "访谈：补检轮数默认 2、上限可调", str(rounds))
                    saw_gap_question = True
                status, s = post(
                    f"{base}/api/writing/projects/{iv_pid}/interview/answer",
                    {"kind": "gap_decision", "section_key": q["section_key"],
                     "decision": "keep_gap"})
            if not s.get("next_action"):
                break
            status, step = post(
                f"{base}/api/writing/projects/{iv_pid}/interview/step", offline)
            if not step.get("job_id"):
                break
            for _ in range(80):
                time.sleep(0.3)
                status, iv_snap = get(
                    f"{base}/api/writing/section-jobs/{step['job_id']}")
                if iv_snap.get("status") in ("done", "error", "cancelled"):
                    break
            check(iv_snap.get("status") == "done",
                  f"访谈：步骤作业完成（{step.get('action')}）",
                  f"status={iv_snap.get('status')} err={iv_snap.get('error')}")

        status, s = get(f"{base}/api/writing/projects/{iv_pid}/interview")
        verdict = ((s.get("sections") or [{}])[0]).get("verdict") or {}
        check(verdict.get("decision") in ("sufficient", "insufficient", "exhausted"),
              "访谈：判定给出结论", str(verdict.get("decision")))
        first = (s.get("sections") or [{}])[0]
        check(first.get("stage") in ("done", "failed_writable"),
              "访谈：一个部分走完闭环（含写作）", str(first.get("stage")))
        if first.get("stage") == "done":
            check(s.get("completed") == 1, "访谈：进度推进到 1/2",
                  str(s.get("completed")))
            check((s.get("question") or {}).get("section_key") == "parameters",
                  "访谈：自动进入下一个部分",
                  str((s.get("question") or {}).get("section_key")))
        status, iv_content = get(
            f"{base}/api/writing/projects/{iv_pid}/sections/objective/content")
        check(bool(iv_content.get("content")),
              "访谈：正文已落库（写作由执行链完成）",
              f"stage={first.get('stage')} err={first.get('error')}")
        post(f"{base}/api/writing/projects/{iv_pid}", {}, method="DELETE")

        # ---------------- 系统状态 ----------------
        status, packs_data = get(f"{base}/api/system/packs")
        check(status == 200 and packs_data["packs"]["loaded"]["skills"].get("writing"),
              "系统状态：技能包加载可见")
        check(packs_data["journal"]["entries"] > 0, "系统状态：分区表条目数")
        check(packs_data["journal"]["unknown_year_papers"] == 1,
              "系统状态：年份未知计数")
        unmatched = [row["venue"] for row in packs_data["journal"]["unmatched_top"]]
        check("Some Unlisted Journal" in unmatched, "系统状态：未命中分区期刊清单")
        check("gb7714" in packs_data["citation"]["styles"], "系统状态：引用样式清单")
        check(packs_data["config"]["relevance_gate_low_signal"] == "warn",
              "系统状态：低信号策略默认 warn")

        # ---------------- 旧端点未回归 ----------------
        for path in ("/api/overview", "/api/papers", "/api/agents",
                     "/api/study/status", "/api/logs", "/api/ontology",
                     "/api/status/nodes", "/api/reviews"):
            status, _ = get(f"{base}{path}")
            check(status == 200, f"旧端点 {path} 仍可用")
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        # 收尾删库：Windows 上只要还有任何句柄（未关闭的连接、WAL 文件、还没退完的
        # 服务线程）就会 WinError 32，而**删不掉库会把整轮冒烟判成失败**——
        # 检查全过了却因为清理失败报错，很容易误导。这里做成容错 + 重试：
        # 清理不成功只提示，不影响结论。
        if not args.keep and db_path.exists():
            import gc
            gc.collect()
            time.sleep(0.5)
            leftover: list[str] = []
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(db_path) + suffix)
                for attempt in range(5):
                    if not p.exists():
                        break
                    try:
                        p.unlink()
                        break
                    except OSError:
                        gc.collect()
                        time.sleep(0.4 * (attempt + 1))
                if p.exists():
                    leftover.append(p.name)
            if leftover:
                print(f"[提示] 临时库未能删除（被占用，不影响结论）：{leftover}")

    failed = [label for ok, label in RESULTS if not ok]
    print(f"\n合计 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：")
        for label in failed:
            print(f"  - {label}")
        return 1
    print("端到端冒烟全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
