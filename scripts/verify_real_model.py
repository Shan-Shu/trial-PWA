"""真实模型验证：自然语言解析 + 派工 + 一步写作，并打印统一日志。

不放进 tests/：它需要网络与 API Key，回归测试不能依赖这些。
用法：
    .venv\\Scripts\\python.exe scripts/verify_real_model.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from research_agent.db import connect                       # noqa: E402
from research_agent.logging import (bind_trace, configure,   # noqa: E402
                                    new_trace, read_file)
from research_agent.writing import dispatch as dp          # noqa: E402
from research_agent.writing import dispatch_planner as dpl  # noqa: E402
from research_agent.writing.service import create_project  # noqa: E402


def banner(text: str) -> None:
    print(f"\n{'=' * 68}\n{text}\n{'=' * 68}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ra-real-"))
    db_path = tmp / "real.db"
    log_file = tmp / "dsh.jsonl"
    configure(path=log_file, to_stderr=False, mirror_to_db=False)

    db = connect(db_path)
    project_id = create_project(
        db, "炔酰胺（真实模型验证）", genre="frontier_review",
        topic="gold-catalyzed ynamide annulation")
    trace = new_trace()

    banner("① 自然语言 → 派工方案（真实模型解析）")
    t0 = time.monotonic()
    try:
        with bind_trace(trace):
            plans = dpl.build_dispatch_plan(
                request="把库里的炔酰胺文献抽成知识，并重建一次本体视图",
                conn=db, project_id=project_id,
                topic="gold-catalyzed ynamide annulation", use_model=True)
        print(f"解析用时 {time.monotonic() - t0:.1f}s · 来源 {plans['parsed_by']}")
        print(f"识别意图 {plans['intents']}｜丢弃 {plans['dropped']}")
        print(f"说明：{plans['note']}")
        for plan in plans["plans"]:
            print(f"  方案{plan['id']}：{plan['label']}")
            for step in plan["plan"]:
                extra = (f"（将跳过：{step['skip_reason']}）"
                         if step.get("skip_reason") else "")
                print(f"      {step['task']} @ {step['node']} {step['args']}{extra}")
    except Exception as exc:  # noqa: BLE001 —— 验证脚本要打印失败原因而不是栈
        print(f"解析阶段失败：{type(exc).__name__}: {exc}")
        plans = {"plans": []}

    banner("② 派工执行（只跑不碰网络的任务，验证协议与回报）")
    safe = [s for s in (plans["plans"][0]["plan"] if plans["plans"] else [])
            if s["task"] == "rebuild_ontology"]
    if not safe:
        safe = [{"task": "rebuild_ontology", "node": "ontology"}]
    print(f"本次执行步骤：{[s['task'] for s in safe]}")
    with bind_trace(trace):
        record = dp.run_dispatch(
            plan=safe, conn=db, project_id=project_id, origin="user_direct",
            reason="真实模型验证", trace=trace,
            budget={"rounds": 1, "max_seconds": 180})
    print(f"派工单 {record['dispatch_id']} · 状态 {record['status']}")
    for step in record["steps"]:
        print(f"   {step['task']}@{step['node']} → {step['status']}"
              f" {step.get('seconds', '')}s {step.get('skip_reason', '')}"
              f" {step.get('error', '')}")

    banner("③ 统一日志（同一 trace，按时间顺序）")
    rows = read_file(200, trace=trace)
    for row in rows:
        data = row.get("data") or {}
        bits = " ".join(f"{k}={v}" for k, v in list(data.items())[:4])
        ms = f" {row['ms']}ms" if row.get("ms") is not None else ""
        print(f"  {row['ts'][11:23]} {row['level'][:4]:4} {row['evt']:22}"
              f" node={str(row.get('node', '')):18}{ms} {bits}")
    print(f"\n日志文件：{log_file}")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
