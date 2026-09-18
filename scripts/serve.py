"""桌面启动器用的服务入口：配合 `pythonw.exe` 即可**无窗口**后台运行。

为什么单独一个文件：`.bat` 里用 `pythonw -c "…"` 传参要处理多层引号，很容易出错；
一个真实的 `.py` 入口让命令行参数原样传递（`argparse` 直接可用）。

**日志自带**：`pythonw.exe` 没有控制台，如果不重定向，uvicorn 的启动横幅与
Python 异常会全部消失——出问题时用户只看到"浏览器打不开"，无从下手。
所以这里把 stdout/stderr 接到 `data/logs/serve.log`，并开 `faulthandler`
接住段错误这类硬崩溃。

用法：
    pythonw.exe scripts\\serve.py --host 127.0.0.1 --port 8000 --db data\\v031_fresh.db
"""
from __future__ import annotations

import faulthandler
import sys
from pathlib import Path

# 允许在"未安装包"的情况下直接运行（例如换了 Python 环境）
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

#: 超过这个大小就轮转一次，避免日志无限增长
MAX_LOG_BYTES = 4 * 1024 * 1024


def _redirect_output(log_path: Path) -> None:
    """把标准输出/错误接到日志文件（幂等，失败也不影响启动）。"""
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
    print(f"\n===== {__file__} 启动 {len(sys.argv)} 参数：{sys.argv[1:]} =====")


def main() -> int:
    _redirect_output(_ROOT / "data" / "logs" / "serve.log")

    from research_agent.dashboard.app import main as dashboard_main

    dashboard_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
