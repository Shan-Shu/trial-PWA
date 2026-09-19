"""桌面启动器用的 Streamlit 入口：配合 `pythonw.exe` 即可**无窗口**后台运行。

**为什么不用 `python -m streamlit run`**：那样得在 `.bat` 里拼一长串参数，
而且 `pythonw.exe` 下没有任何控制台，Streamlit 的启动横幅与异常会全部消失
——出问题时用户只看到"浏览器打不开"。所以这里自己重定向输出。

做法与旧项目的 `serve.py` 一致（那套验证过）：接管 stdout/stderr → 开
`faulthandler` 接住硬崩溃 → 以 headless 模式启动 Streamlit。

用法：
    pythonw.exe scripts\\serve_desk.py --port 8501
"""
from __future__ import annotations

import faulthandler
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

UI_SCRIPT = _SRC / "desk" / "ui" / "app.py"

#: 超过这个大小就轮转一次，避免日志无限增长
MAX_LOG_BYTES = 4 * 1024 * 1024


def _redirect_output(log_path: Path) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > MAX_LOG_BYTES:
            backup = log_path.with_suffix(".log.1")
            backup.unlink(missing_ok=True)
            log_path.rename(backup)
        handle = log_path.open("a", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = handle
    sys.stderr = handle
    try:
        faulthandler.enable(handle)
    except (AttributeError, ValueError):  # pragma: no cover —— 平台差异
        pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="serve_desk")
    ap.add_argument("--port", type=int, default=8501)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    _redirect_output(_ROOT / "data" / "logs" / "serve.log")
    print(f"\n===== research-desk 界面启动 port={args.port} =====")

    if not UI_SCRIPT.is_file():
        print(f"找不到界面入口：{UI_SCRIPT}", file=sys.stderr)
        return 1

    # Streamlit 的 CLI 读 sys.argv，所以这里构造好再交给它
    sys.argv = [
        "streamlit", "run", str(UI_SCRIPT),
        "--server.port", str(args.port),
        "--server.address", args.host,
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--server.fileWatcherType", "none",   # 后台常驻不需要热重载
    ]
    from streamlit.web import cli as stcli

    return int(stcli.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
