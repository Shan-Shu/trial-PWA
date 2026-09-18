"""打开界面前的自检：写作台与研究流程页依赖的接口是否都通。"""
import sys

import requests

BASE = "http://127.0.0.1:8000"
checks = [
    ("总览", "GET", "/api/overview"),
    ("写作项目列表", "GET", "/api/writing/projects"),
    ("体裁与模板", "GET", "/api/writing/templates"),
    ("节点总览（研究流程页）", "GET", "/api/status/nodes"),
    ("研究状态", "GET", "/api/study/status"),
    ("登记表", "GET", "/api/registry/nodes"),
    ("派工单列表", "GET", "/api/dispatches"),
    ("文献库统计", "GET", "/api/library/stats"),
    ("系统状态", "GET", "/api/system/packs"),
]
bad = 0
for name, method, path in checks:
    try:
        resp = requests.request(method, BASE + path, timeout=30)
        flag = "OK " if resp.status_code == 200 else "!! "
        if resp.status_code != 200:
            bad += 1
        print(f"  {flag} {name:24} {path:32} {resp.status_code}")
    except Exception as exc:
        bad += 1
        print(f"  !!  {name:24} {path:32} {type(exc).__name__}: {exc}")

# 访谈快照（炔酰胺项目 #27）
try:
    snap = requests.get(f"{BASE}/api/writing/projects/27/interview",
                        timeout=30).json()
    print(f"\n访谈快照 #27 «炔酰胺»:")
    print(f"  体裁={snap.get('intake', {}).get('genre')} "
          f"主题={snap.get('intake', {}).get('topic')}")
    print(f"  进度 {snap.get('completed')}/{snap.get('total')} "
          f"intake_done={snap.get('intake_done')} finished={snap.get('finished')}")
    print(f"  当前问题类型={ (snap.get('question') or {}).get('kind') } "
          f"next_action={snap.get('next_action') or '（等作答）'}")
    for sec in snap.get("sections") or []:
        print(f"    · {sec['section_key']:16} stage={sec['stage']:22}"
              f" 字数={sec.get('content_chars') or 0}")
except Exception as exc:
    bad += 1
    print(f"  访谈快照失败: {type(exc).__name__}: {exc}")

print(f"\n{'全部可用' if not bad else str(bad) + ' 项异常'}")
sys.exit(1 if bad else 0)
