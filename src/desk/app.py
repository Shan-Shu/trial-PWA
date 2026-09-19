"""research-desk 启动入口：拉起 Streamlit 界面。

    uv run research-desk            # 或双击 启动助手.bat

为什么是拉起子进程而不是直接 import：Streamlit 需要一个**脚本路径**来启动
（它自己管理会话、热重载与 WebSocket），不是普通的 `main()` 调用。
`sys.executable -m streamlit run <脚本>` 是官方支持的方式。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

__all__ = ["main", "ui_script"]

UI_SCRIPT = Path(__file__).resolve().parent / "ui" / "app.py"


def ui_script() -> Path:
    return UI_SCRIPT


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="research-desk",
                                 description="research-desk 研究工作台（Streamlit 界面）")
    ap.add_argument("--port", type=int, default=8501)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default="", help="数据库路径（缺省用引擎默认库）")
    ap.add_argument("--headless", action="store_true",
                    help="不自动开浏览器（由启动脚本负责开）")
    args = ap.parse_args(argv)

    if not UI_SCRIPT.is_file():
        print(f"找不到界面入口：{UI_SCRIPT}", file=sys.stderr)
        return 1

    cmd = [sys.executable, "-m", "streamlit", "run", str(UI_SCRIPT),
           "--server.port", str(args.port),
           "--server.address", args.host,
           "--server.headless", "true" if args.headless else "false",
           "--browser.gatherUsageStats", "false"]
    if args.db:
        cmd += ["--", "--db", args.db]
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:      # Ctrl+C 正常退出
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
