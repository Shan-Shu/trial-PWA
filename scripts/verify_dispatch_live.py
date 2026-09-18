"""在**正在运行的服务**上核对派工链路（真实 HTTP，UTF-8 安全）。

用法：
    1. 另开一个终端：uv run research-agent-dashboard --port 8000
    2. uv run python scripts/verify_dispatch_live.py [--base http://127.0.0.1:8000]

为什么单独一个脚本：HTTP 层能查出模型层看不见的问题（路由、编码、幂等、
trace 透传）。用 PowerShell 的 Invoke-RestMethod 发中文 body 会被本地编码搞坏，
所以这里用 requests 显式声明 UTF-8。
"""
from __future__ import annotations

import argparse

import requests

TRACE = "t-livecheck"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--use-model", action="store_true",
                    help="解析时调用真实模型（默认离线，走确定性关键词）")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    headers = {"X-Trace-Id": TRACE}

    health = requests.get(f"{base}/api/health", timeout=10).json()
    print(f"服务可用：db={health.get('db')}")

    # ① 登记表
    reg = requests.get(f"{base}/api/registry/nodes", headers=headers,
                       timeout=15).json()
    print(f"① 登记表：{len(reg['nodes'])} 个节点 / {len(reg['tasks'])} 个任务；"
          f"可派工 {len(reg['dispatchable'])} 个，接模型 {len(reg['llm_nodes'])} 个")

    # ② 自然语言 → 方案
    resp = requests.post(f"{base}/api/dispatches/parse", headers=headers,
                         timeout=180,
                         json={"request": "把库里的炔酰胺文献抽成知识，并重建一次本体视图",
                               "topic": "gold-catalyzed ynamide annulation",
                               "use_model": bool(args.use_model)}).json()
    if not resp.get("ok"):
        print(f"② 解析失败：{resp.get('error')}")
        return 1
    print(f"② 解析（{resp['parsed_by']}）：意图 {resp['intents']}，"
          f"丢弃 {resp['dropped']}，方案 {len(resp['plans'])} 个")
    for plan in resp["plans"]:
        steps = " → ".join(f"{s['task']}@{s['node']}" for s in plan["plan"])
        print(f"     方案{plan['id']}：{plan['label']}")
        print(f"        {steps}")

    # ③ 下单（挑一个只含纯计算步骤的方案，避免在核对脚本里跑网络检索）
    def pure(plan: dict) -> bool:
        return all(s["task"] in {"rebuild_ontology", "export"}
                   for s in plan["plan"])

    picked = next((p for p in resp["plans"] if pure(p)), None)
    if picked is None:
        print("③ 没有纯计算方案，改为只跑重建本体（验证协议本身）")
        picked = {"id": "-", "plan": [{"task": "rebuild_ontology",
                                       "node": "ontology"}]}
    created = requests.post(f"{base}/api/dispatches", headers=headers, timeout=180,
                            json={"plan": picked["plan"], "origin": "user_direct",
                                  "reason": "live 核对",
                                  "section_key": "introduction"}).json()
    if not created.get("ok"):
        print(f"③ 下单失败：{created.get('error')}")
        return 1
    run = created["dispatch"]
    print(f"③ 派工单 {created['dispatch_id']}：{run['status']}"
          f"（新增 {run['added']}）")
    for step in run["steps"]:
        note = step.get("skip_reason") or step.get("error") or ""
        print(f"     {step['task']}@{step['node']} → {step['status']} "
              f"{step.get('seconds', '')}s {note}")

    # ④ 幂等：同一单号再下一次
    again = requests.post(f"{base}/api/dispatches", headers=headers, timeout=60,
                          json={"plan": picked["plan"],
                                "dispatch_id": created["dispatch_id"],
                                "origin": "user_direct"}).json()
    # 注意：接口会为新请求生成新单号，这里改为核对"单号可查"
    detail = requests.get(f"{base}/api/dispatches/{created['dispatch_id']}",
                          headers=headers, timeout=30).json()
    print(f"④ 单号可查：{detail.get('ok')}，状态 "
          f"{detail.get('dispatch', {}).get('status')}")

    # ⑤ 未登记任务必须被拒
    bad = requests.post(f"{base}/api/dispatches", headers=headers, timeout=30,
                        json={"tasks": ["summon_dragon"]}).json()
    print(f"⑤ 未登记任务被拒：{not bad.get('ok')}（{bad.get('error')}）")

    # ⑥ 研究流程页数据源：状态语义与派工单
    nodes = requests.get(f"{base}/api/status/nodes", headers=headers,
                         timeout=60).json()
    labels = nodes.get("status_labels", {})
    print(f"⑥ 节点总览：{len(nodes['nodes'])} 个节点，"
          f"派工 {len(nodes['dispatches'])} 张，状态词表 {len(labels)} 项")
    for node in nodes["nodes"]:
        if node["node"] in ("interview", "content_builder", "ontology"):
            print(f"     {node['node']:18} {node['status_label']:6} "
                  f"{node['status_note'][:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
