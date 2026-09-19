"""界面冒烟：用 Streamlit 官方 AppTest 逐页真跑，断言不抛异常。

**为什么用它而不是 HTTP GET**：Streamlit 的页面是在 WebSocket 会话里执行的，
普通 HTTP 请求只拿到一个空壳 HTML —— 页面代码有没有报错根本看不出来。
`AppTest` 会在进程内真实执行脚本并收集异常，等价于 js 前端的浏览器冒烟。

用法：
    uv run python scripts/smoke_desk.py [--db path\\to.db]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PAGES = ["总览", "智能检索", "知识检索", "文献库", "知识抽取",
         "动态本体", "实验工作台", "写作台", "审核中心", "系统状态"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="")
    args = ap.parse_args()

    # 界面读 RA_DESK_DB 决定用哪个库；不设则用引擎默认库
    if args.db:
        os.environ["RA_DESK_DB"] = str(Path(args.db).resolve())
    # 日志不要往 stderr 刷，否则冒烟输出看不清
    os.environ.setdefault("RA_LOG_STDERR", "0")

    from streamlit.testing.v1 import AppTest

    script = ROOT / "src" / "desk" / "ui" / "app.py"
    db_label = os.environ.get("RA_DESK_DB", "(引擎默认库)")
    print(f"界面冒烟 · 共 {len(PAGES)} 页 · 库 {db_label}\n")

    failures: list[str] = []
    for page in PAGES:
        at = AppTest.from_file(str(script), default_timeout=120)
        try:
            at.run()
            if at.exception:
                raise RuntimeError(at.exception[0].value)
            # 侧栏导航的选项是「图标 + 标题」（如 "📊 总览"），
            # 所以要按后缀匹配真实选项，不能直接塞裸标题。
            radio = at.sidebar.radio[0]
            option = next((o for o in radio.options
                           if str(o).endswith(page)), None)
            if option is None:
                raise RuntimeError(f"侧栏没有这一页：{page}（选项 {radio.options}）")
            radio.set_value(option).run()
            if at.exception:
                raise RuntimeError(at.exception[0].value)
            widgets = (len(at.markdown) + len(at.dataframe) + len(at.metric)
                       + len(at.button) + len(at.text_input))
            print(f"  [PASS] {page:<8} 组件 {widgets} 个"
                  + (f" · 指标 {len(at.metric)}" if at.metric else ""))
        except Exception as exc:  # noqa: BLE001 —— 冒烟要报告而不是崩掉
            failures.append(page)
            print(f"  [FAIL] {page:<8} {type(exc).__name__}: {str(exc)[:200]}")

    print(f"\n合计 {len(PAGES)} 页，通过 {len(PAGES) - len(failures)}，"
          f"失败 {len(failures)}")
    if failures:
        print("失败页：" + "、".join(failures))
        return 1
    print("界面 10 页全部渲染通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
