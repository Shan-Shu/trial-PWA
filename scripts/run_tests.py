"""跑引擎安全网，并**保证绝不碰真实库**；跑完还会对账。

为什么需要它：直接 `python -m unittest discover -s tests` 时，少数用例在构造
节点时没传 `conn`/`settings`，节点便回落到**默认库** `data/research_agent.db`
并往里写 `processing_log`。实测一轮往默认库写 20 行——对一个把该文件当正式库
用的用户来说，这就是"跑个测试把自己的库改了"。已逐处修掉那些调用点，但这类
回归太容易再犯，所以这里再上一道结构性的网：

1. 把 `RA_DB_PATH` 指向临时文件，让"默认库"整体落在临时目录（`config.settings`
   通过 `Settings.from_env()` 构建，因此会认这个变量）；
2. 跑完**对账**真实库的行数快照 —— 一旦有变化就明确报错，而不是静默放行。

用法：
    uv run python scripts/run_tests.py                 # 全部
    uv run python scripts/run_tests.py -p test_foo.py   # 单个文件
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL_DEFAULT_DB = ROOT / "data" / "research_agent.db"

# stdout 被管道接走时 Python 取系统 ANSI 码页（cp936），末尾的 ✓/✗ 编码失败
# 会让"全部通过"的脚本以 exit 1 收场（本脚本第一版就踩了，把通过误报成失败）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace",
                            line_buffering=True)
    except (AttributeError, ValueError):
        pass


def _snapshot(path: Path) -> dict[str, int] | None:
    """只读地取全库行数快照；库不存在时返回 None。"""
    if not path.exists():
        return None
    uri = "file:%s?mode=ro" % str(path).replace("\\", "/")
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        counts: dict[str, int] = {}
        for name in tables:
            try:
                counts[name] = conn.execute(
                    'SELECT COUNT(*) FROM "%s"' % name).fetchone()[0]
            except sqlite3.Error:
                continue
        return counts
    finally:
        conn.close()


def _diff(before: dict[str, int], after: dict[str, int]) -> list[str]:
    out: list[str] = []
    for name in sorted(set(before) | set(after)):
        was, now = before.get(name, 0), after.get(name, 0)
        if was != now:
            out.append(f"  {name}: {was} → {now}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--pattern", default="test_*.py")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    before = _snapshot(REAL_DEFAULT_DB)
    if before is None:
        print(f"（未找到默认库 {REAL_DEFAULT_DB.name}，跳过对账）")

    env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="ra-test-db-") as tmp:
        env["RA_DB_PATH"] = str(Path(tmp) / "research_agent.db")
        # 统一事件日志也指到临时目录，避免污染 data/
        env.setdefault("RA_LOG_STDERR", "0")
        print(f"默认库已重定向到临时目录：{env['RA_DB_PATH']}")
        cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests",
               "-p", args.pattern]
        if args.verbose:
            cmd.append("-v")
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env)

    code = proc.returncode
    after = _snapshot(REAL_DEFAULT_DB)
    if before is not None and after is not None:
        changed = _diff(before, after)
        if changed:
            print("\n✗ 真实库被改动了 —— 有测试写进了默认库：")
            print("\n".join(changed))
            print("  请让相关用例显式传入 conn/settings（临时库）。")
            return 1
        print("\n✓ 真实库快照未变（未被测试写入）")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
