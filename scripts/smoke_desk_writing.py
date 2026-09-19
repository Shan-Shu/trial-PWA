"""写作台访谈闭环的界面级回归：**在 Streamlit 里把一整轮访谈跑完**。

为什么单独一个脚本：逐页渲染冒烟只能证明"页面不报错"，证明不了
"访谈闭环真的能走完"。这里用 AppTest 真的点按钮、填输入、推进作业，
断言从建项目一路走到**正文落库**。

默认离线（RA_DESK_OFFLINE=1）：拟方案走通用方向兜底、成段走骨架降级，
因此不依赖 API Key、也不烧配额；这正是它能当回归用的原因。

用法：
    uv run python scripts/smoke_desk_writing.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# stdout 被管道接走时 Python 取的是系统 ANSI 码页（本机 cp936），末尾的 ✅
# 在 cp936 里无编码 → 明明 11/11 全过，脚本却以 exit 1 收场（曾把回归误判成失败）。
# 钉成 UTF-8，与本项目 .bat 里的 chcp 65001 一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# 必须**在导入应用之前**设好：适配器按环境变量决定是否用真模型
os.environ["RA_DESK_OFFLINE"] = "1"
os.environ.setdefault("RA_LOG_STDERR", "0")

SCRIPT = ROOT / "src" / "desk" / "ui" / "app.py"

checks: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    checks.append((bool(ok), label, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def find_button(at, key: str):
    for button in at.button:
        if button.key == key:
            return button
    return None


def click(at, key: str) -> bool:
    button = find_button(at, key)
    if button is None:
        return False
    button.click().run()
    return True


def goto_writing(at) -> None:
    radio = at.sidebar.radio[0]
    option = next(o for o in radio.options if str(o).endswith("写作台"))
    radio.set_value(option).run()


def main() -> int:
    from streamlit.testing.v1 import AppTest

    db = ROOT / "data" / "writing_smoke.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)          # 每次从空库开始
    os.environ["RA_DESK_DB"] = str(db)

    print(f"写作台访谈闭环冒烟（离线）· 库 {db.name}\n")

    at = AppTest.from_file(str(SCRIPT), default_timeout=180)
    at.run()
    goto_writing(at)
    check(not at.exception, "进入写作台不报错",
          str(at.exception[0].value)[:160] if at.exception else "")

    # ① 建项目
    at.text_input(key="desk_new_title").set_value("界面冒烟").run()
    at.text_input(key="desk_new_topic").set_value(
        "gold catalysis ynamide annulation").run()
    click(at, "desk_create")
    check(not at.exception, "创建项目成功",
          str(at.exception[0].value)[:160] if at.exception else "")

    # ② 前置三问：体裁 → 主题 → 部分
    # **必须挑有模板的体裁**：部分有没有模板决定它能不能生成正文，也决定它
    # 的勾选框是否可用。本仓库里 `research_article`（恰是 default_genre）、
    # `grant_proposal`、`rebuttal` 都没有模板，选它们会得到一个"什么都写不了"
    # 的死局；`experiment_protocol` 与 `frontier_review` 的模板是齐的。
    genre_buttons = [b for b in at.button if str(b.key).startswith("desk_genre_")]
    check(bool(genre_buttons), "Q1 问创作类型（出现体裁按钮）",
          f"{len(genre_buttons)} 个")
    target = next((b for b in genre_buttons
                   if b.key == "desk_genre_experiment_protocol"), None) \
        or next((b for b in genre_buttons if "建议" in b.label),
                genre_buttons[0] if genre_buttons else None)
    if target is not None:
        target.click().run()
    check(any(t.key == "desk_topic_ok" for t in at.button), "Q2 问主题")
    click(at, "desk_topic_ok")
    check(any(str(c.key).startswith("desk_sec_") for c in at.checkbox),
          "Q3 选部分（出现部分勾选）",
          f"{len(at.checkbox)} 个")

    # 只留一个部分，便于快速跑完。
    # 注意：未挂模板的部分（如参考文献）是**禁用**的勾选框，
    # 对它 set_value 会直接抛 AppTestError，必须先过滤掉。
    boxes = [c for c in at.checkbox
             if str(c.key).startswith("desk_sec_") and not c.disabled]
    check(bool(boxes), "至少有一个可勾选的部分（体裁有模板）",
          f"{len(boxes)} 个可勾选，共 {len(at.checkbox)} 个")
    for index, box in enumerate(boxes):
        box.set_value(index == 0).run()
    click(at, "desk_sections_ok")
    advanced = bool(at.session_state.get("desk_job")) or any(
        b.key in ("desk_choice_ok", "desk_gap_ok", "desk_cplan_ok")
        for b in at.button)
    check(advanced, "选完部分即进入逐部分闭环（起了作业或出现选项）",
          f"job={at.session_state.get('desk_job')}")
    check(not at.exception, "选择部分后不报错",
          str(at.exception[0].value)[:160] if at.exception else "")

    # ③ 作业驱动：等自动推进（拟方案 → 判定）稳定下来
    deadline = time.time() + 180
    picks = 0
    while time.time() < deadline:
        # 有选项就选第一个（方案三选一），有缺口决定就保留缺口
        choice_buttons = [b for b in at.button if b.key == "desk_choice_ok"]
        gap_buttons = [b for b in at.button if b.key == "desk_gap_ok"]
        if choice_buttons:
            choice_buttons[0].click().run()
            picks += 1
            continue
        if gap_buttons:
            # 默认选中「保留缺口」，直接办
            gap_buttons[0].click().run()
            picks += 1
            continue
        if at.session_state.get("desk_job"):
            time.sleep(1.0)
            at.run()
            continue
        break

    check(picks >= 2, "访谈闭环推进了多步（选项 + 缺口决定）", f"{picks} 步")

    from research_agent.db import connect
    conn = connect(str(db))
    try:
        sections = conn.execute(
            "SELECT section_key, LENGTH(TRIM(COALESCE(content,''))) AS n "
            "FROM writing_sections").fetchall()
        body = [(r["section_key"], int(r["n"] or 0)) for r in sections]
        written = [s for s, n in body if n > 0]
        check(bool(written), "正文已落库（访谈闭环走到底）",
              f"{len(body)} 节，其中有正文的 {len(written)} 节")
        steps = conn.execute(
            "SELECT COUNT(*) FROM section_states").fetchone()[0] \
            if _has_table(conn, "section_states") else 0
        if steps:
            check(True, "决策轨迹已落库", f"{steps} 行")
    finally:
        conn.close()

    check(not at.exception, "全流程无未捕获异常",
          str(at.exception[0].value)[:200] if at.exception else "")

    failed = [c for c in checks if not c[0]]
    print(f"\n合计 {len(checks)} 项，通过 {len(checks) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：")
        for _, label, detail in failed:
            print(f"  - {label}  {detail}")
        return 1
    print("写作台访谈闭环全部通过 ✅")
    return 0


def _has_table(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


if __name__ == "__main__":
    raise SystemExit(main())
