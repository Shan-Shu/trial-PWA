"""就绪探测：给「启动助手.bat」用。退出码 0 = 本助手已在监听。

为什么不让 .bat 自己解析 /api/health 的 JSON：批处理里嵌引号、比较 JSON
极易出错（`\"ok\":true` 这种写法在不同 cmd 版本行为不一致）。交给 Python
判断，.bat 只看退出码。

顺带区分"端口被别的程序占了"和"本助手已经起来了"：前者返回 1 但端口是通的，
所以启动脚本还会用 netstat 再看一眼。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def is_ready(host: str, port: int, timeout: float = 2.0) -> bool:
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False
    return payload.get("ok") is True


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    try:
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    except ValueError:
        return 1
    return 0 if is_ready(host, port) else 1


if __name__ == "__main__":
    raise SystemExit(main())
