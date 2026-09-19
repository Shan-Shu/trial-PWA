"""兜底清杀：停掉**本项目 Streamlit 入口**的残留进程。

为什么需要它：「停止助手.bat」是按端口找监听进程来杀的。实例一旦卡死、或健康
探测超时，就会被漏掉——而漏掉的那个仍在后台跑检索与模型调用。实测漏掉过一个
（PID 10384 仍占着 8501）：它持续把「补检」的结果写进**错误的库**、下载 PDF、
烧掉检索与模型配额，而界面上什么都看不出来。所以这里按**命令行特征**兜一遍，
与端口无关，卡死的实例同样能杀掉。

用法：
    python scripts/kill_strays.py --dry-run    # 只列出，不杀
    python scripts/kill_strays.py              # 列出并杀掉
"""
from __future__ import annotations

import argparse
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace",
                            line_buffering=True)
    except (AttributeError, ValueError):
        pass

#: 命令行里出现任一项就认定是"本项目的界面进程"。
#:
#: 三种启动方式对应三个入口，少一个就会漏杀（实测踩过：只匹配
#: `src\desk\ui\app.py`，而「启动助手.bat」实际跑的是 `scripts\serve_desk.py`，
#: 于是运行中的实例一个都没被认出来）。
ENTRY_MARKS: tuple[str, ...] = (
    "scripts\\serve_desk.py",   # 启动助手.bat → serve_desk.py
    "src\\desk\\ui\\app.py",    # streamlit run src/desk/ui/app.py
    "desk\\app.py",             # uv run research-desk → desk.app:main
)

_PS_LIST = (
    "Get-CimInstance Win32_Process | "
    "Where-Object { $_.Name -like 'python*' -and $_.CommandLine } | "
    'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }'
)


def find_strays() -> list[tuple[int, str]]:
    """返回 ``[(pid, commandline)]``；只认本项目入口，避免误杀别人的 python。"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             _PS_LIST],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        print(f"  [!] 无法枚举进程：{exc}")
        return []
    mine = str(__file__).lower()
    out: list[tuple[int, str]] = []
    for line in (proc.stdout or "").splitlines():
        pid_text, _, cmd = line.partition("\t")
        cmd = cmd.strip()
        # 统一成反斜杠再匹配：命令行里可能写正斜杠，而标记按 Windows 习惯写
        normalized = cmd.replace("/", "\\").lower()
        if not any(mark.lower() in normalized for mark in ENTRY_MARKS):
            continue
        if mine in cmd.lower():          # 别把自己算进去
            continue
        try:
            out.append((int(pid_text.strip()), cmd))
        except ValueError:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    strays = find_strays()
    if not strays:
        print("  没有发现残留的界面进程。")
        return 0
    for pid, cmd in strays:
        if args.dry_run:
            print(f"  [dry-run] 将结束 PID {pid}：{cmd[:110]}")
            continue
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
        print(f"  已结束残留进程 {pid}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
