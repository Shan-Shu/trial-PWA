"""就绪探测：给「启动助手.bat」用。退出码 0 = 目标已在监听。

支持两种目标（本项目同时具备）：

- **Streamlit 界面**：`/_stcore/health` 返回 `ok`
- **引擎 HTTP 数据面**：`/api/health` 返回 `{"ok": true, ...}`

为什么不让 .bat 自己解析 HTTP 响应：批处理里嵌引号、比较 JSON 极易出错
（`\\"ok\\":true` 这种写法在不同 cmd 版本行为不一致）。交给 Python 判断，
.bat 只看退出码。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

#: (路径, 期望匹配的字符串或 None 表示只看状态码)
PROBES = (("/_stcore/health", "ok"), ("/api/health", None))


def is_ready(host: str, port: int, timeout: float = 2.0) -> bool:
    for path, marker in PROBES:
        url = f"http://{host}:{port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace").strip()
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            continue
        if marker is not None:
            if marker in body:
                return True
            continue
        try:
            if json.loads(body).get("ok") is True:
                return True
        except (TypeError, ValueError):
            continue
    return False


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    try:
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8501
    except ValueError:
        return 1
    return 0 if is_ready(host, port) else 1


if __name__ == "__main__":
    raise SystemExit(main())
