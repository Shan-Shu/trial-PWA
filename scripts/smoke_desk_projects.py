"""写作台「项目管理」的界面级回归：**真的点重命名、真的删项目**。

为什么单独一个脚本：10 页冒烟只能证明"页面不报错"，证明不了改名与删除真的生效。
这里用 AppTest 真交互，并在**临时库**上对账数据库结果。

覆盖的坑：
- 删除必须连**没有外键的 `dispatch_runs`** 一起清（否则留孤儿派工单）；
- 有正文的项目不能被误删（所选项目里另一个必须还在）；
- 删除当前选中项目后，选择器不能指向已删的 id。

**绝不动正式库**：全程只用临时库 `data/projects_smoke.db`。

用法：
    uv run python scripts/smoke_desk_projects.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace",
                            line_buffering=True)
    except (AttributeError, ValueError):
        pass

os.environ["RA_DESK_OFFLINE"] = "1"
os.environ.setdefault("RA_LOG_STDERR", "0")

SCRIPT = ROOT / "src" / "desk" / "ui" / "app.py"
DB = ROOT / "data" / "projects_smoke.db"

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


def _seed() -> dict[str, int]:
    """临时库 + 3 个项目：1 个有正文，2 个是"测试残留"。"""
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(DB) + suffix)
        if stale.exists():
            stale.unlink()
    from research_agent.db import connect, utcnow
    from research_agent.writing import service as writing

    conn = connect(DB)
    try:
        keeper = writing.create_project(conn, "有正文的稿子", "gold catalysis",
                                        "experiment_protocol")
        writing.save_section(conn, keeper, "objective", "研究目标",
                             "本节有真实正文 [1]。")
        junk_a = writing.create_project(conn, "测试残留A", "aaa")
        junk_b = writing.create_project(conn, "测试残留B", "bbb")
        # 给将被删的项目挂一张派工单：验证删除会清掉这个**没有外键**的孤儿
        conn.execute(
            "INSERT INTO dispatch_runs(dispatch_id, project_id, status, ts) "
            "VALUES(?,?,?,?)", ("d-ui-orphan", junk_a, "done", utcnow()))
        conn.commit()
        return {"keeper": keeper, "junk_a": junk_a, "junk_b": junk_b}
    finally:
        conn.close()


def _title(project_id: int) -> str:
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute(
            "SELECT title FROM writing_projects WHERE project_id=?",
            (project_id,)).fetchone()
        return str(row[0]) if row else ""
    finally:
        conn.close()


def _exists(project_id: int) -> bool:
    conn = sqlite3.connect(DB)
    try:
        return conn.execute(
            "SELECT 1 FROM writing_projects WHERE project_id=?",
            (project_id,)).fetchone() is not None
    finally:
        conn.close()


def _dispatch_rows(project_id: int) -> int:
    conn = sqlite3.connect(DB)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM dispatch_runs WHERE project_id=?",
            (project_id,)).fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    from streamlit.testing.v1 import AppTest

    ids = _seed()
    os.environ["RA_DESK_DB"] = str(DB)
    print(f"写作台「项目管理」冒烟（离线）· 库 {DB.name}"
          f" · 项目 {ids}\n")

    at = AppTest.from_file(str(SCRIPT), default_timeout=180)
    at.run()
    goto_writing(at)
    check(not at.exception, "进入写作台不报错",
          str(at.exception[0].value)[:200] if at.exception else "")

    # ① 管理区渲染出概况表
    victims = None
    for widget in at.multiselect:
        if widget.key == "desk_pm_victims":
            victims = widget
    check(victims is not None, "管理区渲染出「要删除的项目」多选框")
    frames = list(at.dataframe)
    check(bool(frames), "管理区渲染出项目概况表",
          f"{len(frames)} 个表格")
    if frames:
        try:
            rows = int(len(frames[0].value))
        except Exception:  # noqa: BLE001
            rows = -1
        check(rows == 3, "概况表列出全部 3 个项目", f"{rows} 行")

    # ①-b 导航：列表页 →（打开）→ 项目详情页 →（返回）→ 列表页
    #
    # 这一段是回归"用户找不到进入项目具体页面的入口"。此前写作台只有一页，
    # 项目是在折叠区里用下拉**隐式**选中的：既没有入口，也没有抬头说明你在
    # 哪个项目里；选中后自动推进一起作业，页面主体还会被进度条顶掉。
    check(at.session_state.get("desk_view") == "list", "进入写作台默认是项目列表页",
          f"view={at.session_state.get('desk_view')}")
    open_buttons = [b for b in at.button if str(b.key).startswith("desk_open_")]
    check(len(open_buttons) == 3, "列表页每个项目都有一个「打开」入口",
          f"{len(open_buttons)} 个")
    keeper_btn = next((b for b in open_buttons
                       if b.key == f"desk_open_{ids['keeper']}"), None)
    check(keeper_btn is not None, "有正文项目的「打开」按钮存在")
    if keeper_btn is not None:
        keeper_btn.click().run()
        check(at.session_state.get("desk_view") == "detail",
              "点「打开」进入项目详情页",
              f"view={at.session_state.get('desk_view')}")
        header = " ".join(str(m.value) for m in at.markdown)
        check(f"#{ids['keeper']} · 有正文的稿子" in header,
              "详情页抬头写明当前是哪个项目")
        check(any(b.key == "desk_back_to_list" for b in at.button),
              "详情页有「← 全部项目」返回入口")
        check(bool(at.chat_message) or bool(at.expander)
              or any("规划节点" in str(m.value) for m in at.markdown),
              "详情页渲染出项目内容（不再只剩进度条）",
              f"chat_message {len(at.chat_message)} 条")
        back = next((b for b in at.button if b.key == "desk_back_to_list"), None)
        if back is not None:
            back.click().run()
        check(at.session_state.get("desk_view") == "list", "「← 全部项目」回到列表页")

    # ② 重命名：把「测试残留A」改掉，并到库里对账
    picks = [w for w in at.selectbox if w.key == "desk_pm_rename_pick"]
    check(bool(picks), "管理区渲染出「要改的项目」选择框")
    if picks:
        label = next(o for o in picks[0].options if "测试残留A" in str(o))
        picks[0].set_value(label).run()
        field = [w for w in at.text_input
                 if w.key == f"desk_pm_title_{ids['junk_a']}"]
        check(bool(field), "改名输入框带出该项目当前标题",
              field[0].value if field else "")
        if field:
            field[0].set_value("改过名的项目").run()
        click(at, "desk_pm_rename")
        check(_title(ids["junk_a"]) == "改过名的项目",
              "重命名真的写进了库", _title(ids["junk_a"]))

    # ③ 批量删除：勾掉两个残留，保留有正文的那个
    victims = next((w for w in at.multiselect if w.key == "desk_pm_victims"), None)
    if victims is not None:
        wanted = [o for o in victims.options
                  if "改过名的项目" in str(o) or "测试残留B" in str(o)]
        victims.set_value(wanted).run()
    confirm = next((c for c in at.checkbox if c.key == "desk_pm_confirm"), None)
    check(confirm is not None, "出现二次确认勾选框")
    if confirm is not None:
        confirm.set_value(True).run()
    delete_btn = find_button(at, "desk_pm_delete")
    check(delete_btn is not None and not delete_btn.disabled,
          "确认后删除按钮可用")
    click(at, "desk_pm_delete")
    check(not at.exception, "删除过程不报错",
          str(at.exception[0].value)[:200] if at.exception else "")

    check(not _exists(ids["junk_a"]), "选中的项目 A 已被删除")
    check(not _exists(ids["junk_b"]), "选中的项目 B 已被删除")
    check(_exists(ids["keeper"]), "**有正文的项目没被误删**")
    check(_dispatch_rows(ids["junk_a"]) == 0,
          "无外键的派工单孤儿被一并清掉")
    check(_title(ids["keeper"]) == "有正文的稿子", "未选中的项目未被改名")

    # ④ 删掉当前选中项后，页面仍能正常渲染（选择器不该指向已删 id）
    at.run()
    check(not at.exception, "删除后重跑页面不报错",
          str(at.exception[0].value)[:200] if at.exception else "")

    failed = [c for c in checks if not c[0]]
    print(f"\n合计 {len(checks)} 项，通过 {len(checks) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：")
        for _, label, detail in failed:
            print(f"  - {label}  {detail}")
        return 1
    print("项目管理全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
