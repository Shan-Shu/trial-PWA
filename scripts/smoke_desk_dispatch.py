"""派工闭环回归：自然语言需求 → 规划节点解析 → 分发执行 → 对账。

**为什么单独一个脚本**：访谈冒烟只走"逐部分写作"，证明不了用户定义的写作台
主路径 ——「直接向工作规划节点发送需求，然后由工作规划节点分发给其他各个节点
完成」。这条路径是两个真踩过的坑所在：

1. **对 A 库下单却写进 B 库**：派工条目各自 `connect(db_path or 默认库)`，
   曾让针对被测库的派工静默写进正式库。现在断言派工单只落在被测库。
2. **离线开关在规划路径上失效**：`parse_dispatch_request` 曾绕过 `_use_model`，
   使 `RA_DESK_OFFLINE=1` 仍会真调模型（有 Key 就真花钱）。

**绝不动正式库**：整库复制一份到 `data/dispatch_smoke.db` 再派工，正式库只读。
离线跑：解析走确定性兜底、`extract_knowledge` 因缺模型如实跳过；纯本地任务
`rebuild_ontology` 仍会真执行 —— 用它证明"分发到节点且真的跑了"。

用法：
    uv run python scripts/smoke_desk_dispatch.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# stdout 走管道时用系统 ANSI 码页（cp936），中文与勾号会编码失败。
# line_buffering：本脚本会跑真实派工，万一超时被杀，块缓冲会把已打印的
# 证据一起吞掉（踩过一次，整轮输出全丢）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace",
                            line_buffering=True)
    except (AttributeError, ValueError):
        pass

os.environ["RA_DESK_OFFLINE"] = "1"
os.environ.setdefault("RA_LOG_STDERR", "0")

SRC_DB = ROOT / "data" / "desk.db"
TMP_DB = ROOT / "data" / "dispatch_smoke.db"
DEFAULT_DB = ROOT / "data" / "research_agent.db"

#: 单张派工单的时长上限。派工是真的在跑节点，万一某步卡住（网络/模型），
#: 没有这个上限就会一路挂到 600s 被外层杀掉。
BUDGET_SECONDS = 180

checks: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    checks.append((bool(ok), label, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def _sqlite_ids(path: Path, table: str, column: str) -> set[str]:
    """读某库某列的全部值；库或表不存在时返回空集（**只读**，不建表）。"""
    if not path.exists():
        return set()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    try:
        rows = conn.execute(f"SELECT {column} FROM {table}").fetchall()
        return {str(r[0]) for r in rows}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def main() -> int:
    if not SRC_DB.exists():
        print(f"跳过：没有真实库 {SRC_DB}（先跑一次界面或放一份库进去）")
        return 0

    # 整库复制：派工会写本体与派工单，绝不能拿正式库当回归靶子。
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(TMP_DB) + suffix)
        if stale.exists():
            stale.unlink()
    print(f"复制真实库 → {TMP_DB.name}（{SRC_DB.stat().st_size / 1048576:.1f} MB）")
    shutil.copyfile(SRC_DB, TMP_DB)

    default_before = _sqlite_ids(DEFAULT_DB, "dispatch_runs", "dispatch_id")

    from desk.backend import build_adapter
    from research_agent.writing import node_registry as reg

    svc = build_adapter(db_path=str(TMP_DB))

    # ---------------------------------------------------------- 场景 A：自然语言主路径
    print("\n=== A. 用自然语言向规划节点发需求 ===")
    projects = svc.list_writing_projects()
    check(len(projects) > 0, "库里有写作项目", f"{len(projects)} 个")
    pid = int((projects[0] or {}).get("id") or 0) if projects else 0
    project = svc.get_writing_project(pid) if pid else None
    topic = str((project or {}).get("topic") or "")

    request = "把库里还没抽取的文献抽成知识"
    parsed = svc.parse_dispatch_request(request, project_id=pid, topic=topic)
    plans = parsed.get("plans") or []
    check(bool(plans), "规划节点把自然语言解析成了方案", f"{len(plans)} 个方案")
    check(parsed.get("parsed_by") == "fallback",
          "离线时确实没去调模型（_use_model 生效）",
          f"parsed_by={parsed.get('parsed_by')}")

    # 只认登记表里的任务 —— 模型/解析给不认识的任务名必须被丢掉
    tasks = [str(s.get("task") or "") for p in plans for s in (p.get("plan") or [])]
    unknown = sorted({t for t in tasks if t and not reg.get_task(t)})
    check(bool(tasks) and not unknown, "方案里的每一步都是登记过的任务",
          f"共 {len(tasks)} 步" + (f"，未登记 {unknown}" if unknown else ""))

    for plan in plans:
        steps = " → ".join(f"{s.get('task')}@{s.get('node')}"
                           for s in (plan.get("plan") or []))
        print(f"    方案{plan.get('id')}｜{plan.get('label')}｜{steps}")

    # 选"不检索"的那个方案：回归要脱离网络（检索打 arXiv/Tavily，不可复现）。
    # 离线时 extract_knowledge 会因缺模型如实跳过（needs_model），这没关系——
    # 本场景要证的是"解析 → 下单 → 逐步路由 → 落库"这条链路本身。
    local = [p for p in plans
             if p.get("plan")
             and all(s.get("task") != "retrieve" for s in p["plan"])]
    check(bool(local), "存在不依赖网络的方案（回归可脱离外网）",
          f"{len(local)}/{len(plans)} 个方案不含检索")
    picked = (local or plans)[0]

    record = svc.create_dispatch(picked["plan"], project_id=pid,
                                 origin="smoke", reason=request,
                                 budget={"max_seconds": BUDGET_SECONDS})
    did = str(record.get("dispatch_id") or "")
    check(bool(did), "派工执行并拿到派工单号", did)
    step_list = record.get("steps") or []
    check(len(step_list) == len(picked["plan"]),
          "每一步都有回报（含跳过/失败原因）",
          f"{len(step_list)} 步")
    for step in step_list:
        note = step.get("skip_reason") or step.get("error") or ""
        print(f"    {step.get('task')}@{step.get('node')} → {step.get('status')}"
              + (f"（{note}）" if note else ""))

    # 离线时"需要模型且缺模型即不可用"的步骤必须如实跳过 —— 这是刚修掉的那个
    # 洞：model_missing_behavior 只被声明、没人读，离线派工仍真调模型并发起
    # 外部检索（arXiv/Semantic Scholar/OpenAlex + 下载 PDF）。
    unavailable = []
    for step in step_list:
        found = reg.get_task(str(step.get("task") or ""))
        if not found:
            continue
        spec = found[1]
        if spec.needs_model and spec.model_missing_behavior == "unavailable":
            unavailable.append(step)
    ran = [s for s in unavailable if s.get("status") != "skipped"]
    check(not ran, "离线时缺模型不可用的步骤没有真跑",
          f"{len(unavailable)} 步，未跳过 {len(ran)} 步")

    check(did in _sqlite_ids(TMP_DB, "dispatch_runs", "dispatch_id"),
          "派工单落在**被测库**", TMP_DB.name)
    default_after = _sqlite_ids(DEFAULT_DB, "dispatch_runs", "dispatch_id")
    check(not (default_after - default_before),
          "默认库没有被动过（跨库写入不变量）",
          f"默认库派工单 {len(default_before)} → {len(default_after)}")
    check(did in {str(r.get("dispatch_id") or "")
                  for r in svc.list_dispatches(project_id=pid, limit=20)},
          "能在「最近的派工单」里查到")

    # ---------------------------------------------------------- 场景 B：本地节点真执行
    print("\n=== B. 纯本地任务：证明分发到节点并真的执行了 ===")

    def _all_local(steps: list[dict[str, Any]]) -> bool:
        """方案的每一步都不需要模型（= 不碰模型与外部检索，可离线真跑）。"""
        for step in steps:
            found = reg.get_task(str(step.get("task") or ""))
            if not found or found[1].needs_model:
                return False
        return bool(steps)

    # 注意请求措辞：多写"知识"会命中 extract 意图，方案里就会混进 retrieve，
    # 而 retrieve 是"有兜底"类、离线**照样**打 arXiv/OpenAlex —— 回归会挂住。
    parsed_b = svc.parse_dispatch_request("重建本体视图", project_id=pid,
                                          topic=topic)
    plans_b = parsed_b.get("plans") or []
    with_rebuild = [p for p in plans_b
                    if any(s.get("task") == "rebuild_ontology"
                           for s in (p.get("plan") or []))
                    and _all_local(p.get("plan") or [])]
    check(bool(with_rebuild), "解析出全本地的 rebuild_ontology 方案",
          f"{len(plans_b)} 个方案中 {len(with_rebuild)} 个可用")

    if with_rebuild:
        rec_b = svc.create_dispatch(with_rebuild[0]["plan"], project_id=pid,
                                    origin="smoke", reason="重建本体视图",
                                    budget={"max_seconds": BUDGET_SECONDS})
        steps_b = rec_b.get("steps") or []
        for step in steps_b:
            note = step.get("skip_reason") or step.get("error") or ""
            print(f"    {step.get('task')}@{step.get('node')} → {step.get('status')}"
                  + (f"（{note}）" if note else ""))
        done = [s for s in steps_b
                if s.get("task") == "rebuild_ontology" and s.get("status") == "done"]
        check(bool(done), "rebuild_ontology 真的执行到 done（不是只路由不执行）",
              f"耗时 {done[0].get('seconds')}s" if done else
              f"状态 {[s.get('status') for s in steps_b]}")
        if done:
            produced = done[0].get("produced")
            print(f"    节点回报 produced={str(produced)[:300]}"
                  f"  added={done[0].get('added')}")
            check(bool(produced), "节点回报非空（证明真的执行出了产物）")

    # ---------------------------------------------------------- 汇总
    failed = [c for c in checks if not c[0]]
    print(f"\n合计 {len(checks)} 项，通过 {len(checks) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：")
        for _, label, detail in failed:
            print(f"  - {label}  {detail}")
        return 1
    print("派工闭环全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
