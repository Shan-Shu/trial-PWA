"""检查前端页面调用的每个接口是否真的存在于后端路由上。

动机：前端调用写错路径时，`node --check`、模块加载冒烟、甚至静态检查都发现不了，
只有用户点下去才会 404。这里把 `api.get/post/del/request` 的字面路径抽出来，
与 FastAPI 的实际路由表做匹配，把"点击才爆炸"的错配提前到测试期。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from research_agent.dashboard.app import create_app  # noqa: E402

PAGES = ROOT / "src" / "research_agent" / "dashboard" / "static" / "js"

#: ``api.xxx(`` 或 ``api.request(`` 的起点
_CALL_START_RE = re.compile(r"api\.(get|post|del|request)\(\s*")
#: 调用里显式声明的方法（``api.request(path, { method: "PUT" })``）
_METHOD_RE = re.compile(r"method:\s*[\"'](\w+)[\"']")


def _read_first_arg(source: str, start: int) -> str:
    """从 ``start`` 读第一个参数的字面值（尊重模板字符串里的 ``${...}`` 嵌套）。

    朴素正则会在 ``${qs({`` 的第一个 ``}`` 处截断，把合法调用误判成 404
    （实测：library.js / ontology.js 各被误报一次）。所以这里手动扫描括号层级。
    """
    i = start
    while i < len(source) and source[i] in " \t\r\n":
        i += 1
    if i >= len(source):
        return ""
    quote = source[i]
    if quote not in "`\"'":
        return ""
    i += 1
    out: list[str] = []
    while i < len(source):
        ch = source[i]
        if ch == "\\":
            out.append(source[i:i + 2])
            i += 2
            continue
        if quote == "`" and ch == "$" and source[i:i + 2] == "${":
            # 找匹配的 }：必须**同时**照顾括号层级，否则 ``${encodeURIComponent(key)}``
            # 里的 ")" 会被当成多余的 "}" 提前收敛，路径尾巴被吃掉
            # （实测误报：/api/library/paper/{x}/citation/{x} 被截成 /api/library/paper）。
            depth = 1
            parens = 0
            j = i + 2
            while j < len(source) and depth:
                cj = source[j]
                if cj == "(":
                    parens += 1
                elif cj == ")":
                    parens -= 1
                elif cj == "{" and parens <= 0:
                    depth += 1
                elif cj == "}" and parens <= 0:
                    depth -= 1
                j += 1
            # 判断这段插值在路径里扮演什么角色：
            # - 紧邻路径分隔符（``/``/字符串结尾/收尾反引号）→ **路径段** {x}；
            # - 前面是 ``?``/``&``，或表达式本身是查询串（``qs(...)``）→ **查询串** ?{x}。
            # 关键：**只看插值前面**。"后面紧跟 ?" 意味着它是路径段末尾
            # （``/citation/${key}?style=...``），据此判断会把路径段误吞成查询串。
            expr = source[i + 2:j - 1].strip()
            prev_literal = "".join(out)
            as_query = (prev_literal.endswith("?") or prev_literal.endswith("&")
                        or expr.startswith(("qs(", "query", "params", "search")))
            out.append("?{x}" if as_query else "{x}")
            i = j
            continue
        if ch == quote:
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _calls_in(source: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for match in _CALL_START_RE.finditer(source):
        literal = _read_first_arg(source, match.end())
        if not literal:
            continue
        if match.group(1) == "request":
            window = source[match.end():match.end() + 200]
            method = _METHOD_RE.search(window)
            out.append(((method.group(1) if method else "GET").upper(), literal))
        else:
            out.append((match.group(1).upper().replace("DEL", "DELETE"), literal))
    return out


def routes(app) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path or not path.startswith("/api"):
            continue
        for method in methods:
            if method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                out.append((method, path))
    return out


def placeholder(text: str) -> str:
    """只比较"路由形状"：砍掉查询串，把路径参数与插值都归一成 ``{x}``。"""
    text = text.split("?")[0].split("#")[0]
    text = re.sub(r"\{[^}/]*\}", "{x}", text)
    text = re.sub(r"\{x\}+", "{x}", text)
    # 路径段后面紧跟的字面量 "?" 在此已无意义，去掉以免形状多一截
    text = text.rstrip("/").rstrip("?&")
    return text or "/"


def main() -> int:
    app = create_app(str(ROOT / "data" / "dashboard_demo.db"))
    table = routes(app)
    known = {placeholder(path) for _, path in table}
    failures: list[str] = []
    total = 0
    for page in sorted(PAGES.rglob("*.js")):
        source = page.read_text(encoding="utf-8")
        calls = _calls_in(source)
        if not calls:
            continue
        rel = page.relative_to(ROOT).as_posix()
        for method, path in calls:
            total += 1
            shape = placeholder(path)
            if shape in known:
                continue
            if any(method == m and placeholder(rp) == shape for m, rp in table):
                continue
            failures.append(f"{rel}: {method} {shape}   (raw: {path!r})")

    print(f"检查了 {total} 处前端接口调用，后端共有 {len(table)} 条 /api 路由")
    if failures:
        print("\n以下调用在后端找不到对应路由（点下去会 404）：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("全部匹配 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
