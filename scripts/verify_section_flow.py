"""在**真实运行的服务**上走一遍大纲节点工作流（不依赖 PowerShell 的编码怪癖）。

用法：
    1. 另开一个终端起服务：uv run python -m research_agent.dashboard.app \
           --db data/dashboard_demo.db --port 8000
    2. uv run python scripts/verify_section_flow.py --base http://127.0.0.1:8000

与 ``scripts/smoke_dashboard.py`` 的区别：后者自起一个临时服务 + 临时库，
用于回归；本脚本面向**当前正在用的库**，用来肉眼验证判定结果与正文内容，
并把完整 JSON 打到 stdout，方便核对"界面到底会显示什么"。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def request(url: str, payload: dict | None = None, method: str = "GET",
            timeout: float = 30.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, {"_http_error": body[:400]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--title", default="Gold-catalysed ynamide annulation review")
    ap.add_argument("--topic", default="gold catalysis ynamide annulation")
    ap.add_argument("--section", default="introduction")
    ap.add_argument("--instruction", default=(
        "summarise gold catalysed annulation of ynamides, "
        "cite at least 1 source, highlight regioselectivity"))
    ap.add_argument("--keep", action="store_true", help="保留测试项目（默认删除）")
    args = ap.parse_args(argv)
    base = args.base.rstrip("/")

    status, health = request(f"{base}/api/health")
    if status != 200:
        print(f"服务不可用: {status} {health}")
        return 2
    print(f"服务: {health}")

    status, created = request(f"{base}/api/writing/projects",
                              {"title": args.title, "topic": args.topic}, "POST")
    if not created.get("ok"):
        print(f"建项目失败: {created}")
        return 2
    project_id = created["project_id"]
    print(f"项目 project_id={project_id}")

    status, detail = request(f"{base}/api/writing/projects/{project_id}")
    keys = [s["section_key"] for s in detail.get("sections") or []]
    section_key = args.section if args.section in keys else (keys[0] if keys else "")
    print(f"节点: {section_key}（可用: {', '.join(keys)}）")

    print("\n=== ① 征求意见（只规划 + 判定，不检索）===")
    status, plan = request(
        f"{base}/api/writing/projects/{project_id}/sections/{section_key}/plan",
        {"instruction": args.instruction}, "POST")
    if not plan.get("ok"):
        print(f"规划失败: {plan}")
        return 2
    verdict = plan.get("sufficiency") or {}
    print(f"  decision={verdict.get('decision')} confidence={verdict.get('confidence')}"
          f" threshold={verdict.get('threshold')}")
    print(f"  judged_by={plan.get('judged_by')} · {plan.get('sufficiency_mode')}")
    print(f"  planner_mode={plan.get('planner_mode')}")
    print(f"  维度: {json.dumps(verdict.get('dimensions'), ensure_ascii=False)}")
    print(f"  统计: {json.dumps(verdict.get('counts'), ensure_ascii=False)}")
    for gate in verdict.get("gates") or []:
        mark = "✓" if gate.get("passed") else "✗"
        print(f"   {mark} {gate.get('gate')}: {gate.get('detail')}")
    print(f"  检索计划: {((plan.get('plan') or {}).get('retrieval_plan') or {}).get('query_variants')}")
    if verdict.get("suggested_queries"):
        print(f"  建议补检: {verdict['suggested_queries']}")

    print("\n=== ② 撰写此段（异步作业）===")
    status, job = request(
        f"{base}/api/writing/projects/{project_id}/sections/{section_key}/compose",
        {"instruction": args.instruction}, "POST")
    if not job.get("ok"):
        print(f"提交失败: {job}")
        return 2
    job_id = job["job_id"]
    snap = {}
    for _ in range(90):
        time.sleep(0.5)
        status, snap = request(f"{base}/api/writing/section-jobs/{job_id}")
        if snap.get("status") in ("done", "error", "cancelled"):
            break
    print(f"  job={snap.get('status')} outcome={snap.get('outcome')} "
          f"needs_data={snap.get('needs_data')}")
    if snap.get("error"):
        print(f"  error={snap['error']}")
    result = snap.get("result") or {}
    print(f"  result: {json.dumps({k: result.get(k) for k in ('status', 'decision', 'rounds', 'generated_by', 'invalid_indices')}, ensure_ascii=False)}")
    if result.get("missing"):
        print(f"  缺口: {result['missing']}")

    print("\n=== ③ 决策轨迹 ===")
    status, trace = request(
        f"{base}/api/writing/projects/{project_id}/sections/{section_key}/trace")
    print(f"  round_count={trace.get('round_count')} status={trace.get('status')}")
    for rd in trace.get("rounds") or []:
        sv = rd.get("sufficiency") or {}
        print(f"  第 {rd.get('round')} 轮: stage={rd.get('stage')} "
              f"decision={rd.get('decision')} confidence={sv.get('confidence')} "
              f"chars={rd.get('content_chars')} by={rd.get('generated_by')}")
        for reason in (sv.get("reasons") or [])[:3]:
            print(f"      - {reason}")

    print("\n=== ④ 节点正文与溯源 ===")
    status, content = request(
        f"{base}/api/writing/projects/{project_id}/sections/{section_key}/content")
    if not content.get("ok"):
        print(f"  读取失败: {content}")
    else:
        body = content.get("content") or ""
        print(f"  status={content.get('status')} 字符数={len(body)}")
        grounded = content.get("grounded_on") or {}
        print(f"  支撑文献: {grounded.get('paper_keys')}")
        print(f"  成段方式: {grounded.get('generated_by')} "
              f"素材 {grounded.get('material_count')} 条")
        for binding in (grounded.get("bindings") or [])[:6]:
            print(f"    [{binding.get('index')}] {binding.get('paper_key')} "
                  f"证据 {binding.get('evidence_ids')}")
        if grounded.get("invalid_indices"):
            print(f"  ⚠ 越界引文: {grounded['invalid_indices']}")
        print("  --- 正文 ---")
        print(body[:1200])

    if not args.keep:
        request(f"{base}/api/writing/projects/{project_id}", None, "DELETE")
        print(f"\n已删除测试项目 {project_id}")
    else:
        print(f"\n保留项目 {project_id}（可在「写作台」页看到）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
