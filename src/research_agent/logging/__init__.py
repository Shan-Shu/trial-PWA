"""统一事件日志：一条链路一个 trace，实时可 tail、可 grep、失败自带现场。

**为什么要它**：排查"点了没反应"这类问题时，此前只能靠临时加 print 再重新运行，
每次都要重新定位。有了固定事件名与 trace，一次复现就能看出断在哪一段：

    有 ui.click，无 interview.answer.received  → 前端（点击/渲染/状态机）
    两者都有，但状态未变                        → 后端（状态机/幂等/校验）

三个落点，各司其职：

- ``data/logs/dsh.jsonl``：**实时 tail / grep**，一行一条 JSON，不进数据库；
- ``processing_log`` 表：结构化查询与界面展示（只镜像"里程碑"事件，避免写放大）；
- **进程内环形缓冲**：最近 N 条；作业失败时自动附上同 trace 的最近若干条，
  失败自带现场，不必重新复现。

**脱敏是硬要求**：密钥一律替换；长文本截断；模型提示词**不落全文**。
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "EVENTS",
    "LEVELS",
    "MILESTONE_EVENTS",
    "new_trace",
    "current_trace",
    "bind_trace",
    "log_event",
    "log_http",
    "log_llm",
    "logged_invoke",
    "recent",
    "recent_for_trace",
    "tail",
    "log_path",
    "truncate",
    "redact",
    "configure",
    "reset",
    "NULL_CTX",
    "NULL_CONTEXT",
    "trace_var",
]

# ------------------------------------------------------------------ 常量

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

#: 固定事件名（枚举）。新增事件请加到这里，避免日志里出现一次性名字。
EVENTS = {
    # 前后端
    "http.req": "API 请求进入",
    "http.res": "API 请求返回",
    "ui.click": "前端点击",
    "ui.state": "前端状态变化",
    # 访谈
    "interview.question": "生成问题",
    "interview.answer.received": "收到作答",
    "interview.answer.rejected": "作答被拒",
    "interview.step": "推进一步",
    # 派工
    "dispatch.created": "创建派工",
    "dispatch.step.start": "派工步骤开始",
    "dispatch.step.end": "派工步骤结束",
    "dispatch.step.skipped": "派工步骤被跳过",
    "dispatch.done": "派工结束",
    # 模型与检索
    "llm.call.start": "模型调用开始",
    "llm.call.end": "模型调用结束",
    "llm.call.error": "模型调用失败",
    "collect.round": "检索轮次",
    "sufficiency.judge": "充分性判定",
    # 研究流程节点
    "study.node.enter": "节点进入",
    "study.node.exit": "节点退出",
    "study.node.skipped": "节点被跳过",
    # 作业
    "job.start": "作业开始",
    "job.end": "作业结束",
}

#: 需要镜像到 ``processing_log`` 表的事件（里程碑，避免写放大）
MILESTONE_EVENTS = {
    "dispatch.created", "dispatch.done", "dispatch.step.skipped",
    "study.node.skipped", "sufficiency.judge",
}

#: 脱敏：任何看起来像密钥的串
_SECRET_RE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{20,}|"
                        r"AKIA[0-9A-Z]{16})\b")
#: 敏感键名（值直接打码）
_SECRET_KEYS = ("api_key", "apikey", "token", "password", "secret",
                "authorization", "key")

_MAX_TEXT = 500           # 单字段最大字符数
_RING_SIZE = 500          # 环形缓冲条数


# ------------------------------------------------------------------ 配置

class _Config:
    enabled: bool = True
    level: str = os.getenv("RA_LOG_LEVEL", "INFO").upper()
    path: Path | None = None
    #: 默认**不**往 stderr 写：进程本身已有一套 logging 输出，重复写会把
    #: 服务端控制台和测试输出淹掉。需要跟随时用 ``RA_LOG_STDERR=1`` 或
    #: ``research-agent-logs``（读文件，跨进程/跨重启）。
    mirror_to_db: bool = True
    to_stderr: bool = os.getenv("RA_LOG_STDERR", "").strip().lower() in {
        "1", "true", "yes", "on"}


_CFG = _Config()
_LOCK = threading.Lock()
_RING: deque[dict[str, Any]] = deque(maxlen=_RING_SIZE)
_trace_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "dsh_trace", default="")

#: 兼容别名（基础设施模块直接引用它来设置/重置 trace）
trace_var = _trace_var


class NULL_CTX:
    """无 trace 时的空上下文（省掉一次 contextvar 设置）。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> None:
        return None


#: 复用的空上下文实例（无 trace 时用它，写法与 ``bind_trace`` 一致）
NULL_CONTEXT = NULL_CTX()


def configure(*, enabled: bool | None = None, level: str | None = None,
              path: str | Path | None = None, mirror_to_db: bool | None = None,
              to_stderr: bool | None = None) -> None:
    """调整日志行为（测试里主要用来关掉文件写入与 stderr 输出）。"""
    if enabled is not None:
        _CFG.enabled = bool(enabled)
    if level is not None:
        _CFG.level = str(level).upper()
    if path is not None:
        _CFG.path = Path(path)
    if mirror_to_db is not None:
        _CFG.mirror_to_db = bool(mirror_to_db)
    if to_stderr is not None:
        _CFG.to_stderr = bool(to_stderr)


def reset() -> None:
    """清空环形缓冲（测试隔离用；不影响文件与配置）。"""
    with _LOCK:
        _RING.clear()


def log_path() -> Path:
    if _CFG.path is not None:
        return _CFG.path
    from research_agent.config import PROJECT_ROOT
    return Path(os.getenv("RA_LOG_FILE")
                or (PROJECT_ROOT / "data" / "logs" / "dsh.jsonl"))


# ------------------------------------------------------------------ trace

def new_trace() -> str:
    """生成 trace id。格式与前端的一致（`t-` + 8 位小写十六进制），
    这样两边的 trace 在日志里长得一样，肉眼与正则都能统一处理。"""
    import uuid
    return f"t-{uuid.uuid4().hex[:8]}"


def current_trace() -> str:
    return _trace_var.get()


class bind_trace:
    """``with bind_trace(trace): ...`` —— 在块内让所有日志带上同一个 trace。"""

    def __init__(self, trace: str | None = None) -> None:
        self.trace = trace or new_trace()
        self._token = None

    def __enter__(self) -> str:
        self._token = _trace_var.set(self.trace)
        return self.trace

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            _trace_var.reset(self._token)


# ------------------------------------------------------------------ 脱敏/截断

def redact(value: Any, *, depth: int = 0) -> Any:
    """递归脱敏 + 截断。日志里绝不出现密钥或超长文本。"""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if any(s in name.lower() for s in _SECRET_KEYS):
                out[name] = "***"
            else:
                out[name] = redact(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, depth=depth + 1) for v in value[:50]]
    if isinstance(value, str):
        return _SECRET_RE.sub("***", truncate(value))
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return truncate(str(value))


def truncate(text: str, limit: int = _MAX_TEXT) -> str:
    """超长文本截断：保留头尾，中间省略，并标出原始长度。"""
    s = str(text or "")
    if len(s) <= limit:
        return s
    head = limit // 2
    tail = limit - head
    return f"{s[:head]}…[{len(s)}字符]…{s[-tail:]}"


# ------------------------------------------------------------------ 写日志

def log_event(evt: str, *, level: str = "INFO", node: str = "",
              section: str = "", job: str = "", dispatch: str = "",
              trace: str = "", ms: float | None = None,
              data: dict[str, Any] | None = None,
              db: Any = None) -> dict[str, Any]:
    """写一条事件。返回落下的记录（便于测试断言）。"""
    record: dict[str, Any] = {
        "ts": datetime.now(timezone(timedelta(hours=8))).isoformat(
            timespec="milliseconds"),
        "level": str(level).upper(),
        "evt": str(evt),
        "trace": trace or current_trace() or "",
        "node": node, "section": section, "job": job, "dispatch": dispatch,
    }
    if ms is not None:
        record["ms"] = round(float(ms), 1)
    if data:
        record["data"] = redact(data)

    if not _CFG.enabled:
        return record
    if LEVELS.get(record["level"], 20) < LEVELS.get(_CFG.level, 20):
        # 级别不够：仍进环形缓冲（便于失败时回看），但不落文件/库
        with _LOCK:
            _RING.append(record)
        return record

    with _LOCK:
        _RING.append(record)
    _write_file(record)
    if _CFG.to_stderr:
        _write_stderr(record)
    if _CFG.mirror_to_db and db is not None and evt in MILESTONE_EVENTS:
        _mirror_db(db, record)
    return record


def log_http(method: str, path: str, status: int, ms: float,
             *, db: Any = None, note: str = "") -> None:
    evt = "http.req" if status == 0 else "http.res"
    log_event(evt, level="INFO" if status < 400 else "WARN", ms=ms,
              data={"method": method, "path": path, "status": status,
                    "note": note} if note else
                   {"method": method, "path": path, "status": status},
              db=db)


def log_llm(evt: str, *, node: str = "", role: str = "", model: str = "",
            ms: float | None = None, prompt_chars: int = 0,
            output_chars: int = 0, error: str = "", db: Any = None) -> None:
    """模型调用事件。**刻意不落提示词全文**——只落字符数与耗时。"""
    level = "ERROR" if evt.endswith("error") else "INFO"
    data = {"role": role, "model": model,
            "prompt_chars": int(prompt_chars), "output_chars": int(output_chars)}
    if error:
        data["error"] = truncate(error, 300)
    log_event(evt, level=level, node=node, ms=ms, data=data, db=db)


def logged_invoke(model: Any, prompt: Any, *, node: str = "", role: str = "",
                  db: Any = None) -> Any:
    """带日志地调一次模型：``logged_invoke(model, prompt, node="content")``。

    每个模型调用点用它替换 ``model.invoke([HumanMessage(content=prompt)])``，
    即可获得耗时、输入输出字符数，以及**失败时带原因的现场**——不必再靠
    临时 print 复现。提示词全文**不落盘**，只记字符数。

    已带代理（``LoggedModel``）的模型不会被重复记账：直接调用它。
    """
    from langchain_core.messages import HumanMessage

    from research_agent.logging.proxy import LoggedModel, resolve_node

    proxy = LoggedModel.wrap(model, role=role or node,
                             node=resolve_node(role, node, depth=4), db=db)
    return proxy.invoke([HumanMessage(content=str(prompt))])


def _write_file(record: dict[str, Any]) -> None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 —— 日志失败绝不能影响业务
        logger.debug("写日志文件失败: %s", exc)


def _write_stderr(record: dict[str, Any]) -> None:
    """实时输出一行摘要（人类可读，可与 stdout 一起 tail）。"""
    bits = [record["ts"][11:23], record["level"][:4], record["evt"]]
    for key in ("trace", "node", "section", "dispatch", "job"):
        if record.get(key):
            bits.append(f"{key}={record[key]}")
    if record.get("ms") is not None:
        bits.append(f"{record['ms']}ms")
    data = record.get("data") or {}
    if data:
        flat = " ".join(f"{k}={truncate(str(v), 60)}"
                        for k, v in list(data.items())[:6])
        bits.append(flat)
    line = " | ".join(str(b) for b in bits)
    try:
        import sys
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def _mirror_db(db: Any, record: dict[str, Any]) -> None:
    try:
        from research_agent.db import log_event as db_log_event
        db_log_event(db, record.get("node") or "app", record["evt"], None,
                     record)
    except Exception as exc:  # noqa: BLE001
        logger.debug("镜像事件到 processing_log 失败: %s", exc)


# ------------------------------------------------------------------ 读取

def recent(limit: int = 100, *, evt: str = "", level: str = "",
           node: str = "", trace: str = "") -> list[dict[str, Any]]:
    """从环形缓冲取最近若干条（支持按事件前缀/级别/节点/trace 过滤）。"""
    with _LOCK:
        items = list(_RING)
    return _filter(items, evt=evt, level=level, node=node, trace=trace)[-limit:]


def recent_for_trace(trace: str, limit: int = 50) -> list[dict[str, Any]]:
    """某个 trace 的最近事件——作业失败时用它自动附上现场。"""
    return recent(limit=limit, trace=trace)


def _filter(items: list[dict[str, Any]], *, evt: str = "", level: str = "",
            node: str = "", trace: str = "") -> list[dict[str, Any]]:
    evt_parts = [p.strip() for p in str(evt).split(",") if p.strip()]
    min_level = LEVELS.get(str(level).upper(), 0)
    out: list[dict[str, Any]] = []
    for item in items:
        if evt_parts and not any(str(item.get("evt", "")).startswith(p)
                                for p in evt_parts):
            continue
        if min_level and LEVELS.get(str(item.get("level")), 0) < min_level:
            continue
        if node and item.get("node") != node:
            continue
        if trace and item.get("trace") != trace:
            continue
        out.append(item)
    return out


def read_file(limit: int = 200, *, evt: str = "", level: str = "",
              node: str = "", trace: str = "") -> list[dict[str, Any]]:
    """从 jsonl 文件读取（跨进程/跨重启；tail 命令用）。"""
    path = log_path()
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:  # noqa: BLE001
        logger.debug("读日志文件失败: %s", exc)
        return []
    filtered = _filter(items, evt=evt, level=level, node=node, trace=trace)
    return filtered[-limit:]


def tail(*, evt: str = "", level: str = "", node: str = "", trace: str = "",
         last: int = 0, follow: bool = True, poll_seconds: float = 0.5,
         limit_lines: int = 0) -> None:
    """命令行 tail：先回放最后 ``last`` 条，再持续跟随新行。"""
    import sys
    import time as _time

    path = log_path()
    if last:
        for item in read_file(last, evt=evt, level=level, node=node,
                              trace=trace):
            print(_format(item))
    if not follow:
        return

    pos = 0
    emitted = 0
    # 已存在的文件：从末尾开始跟随，避免重复回放
    if path.exists():
        try:
            pos = path.stat().st_size
        except OSError:
            pos = 0
    sys.stderr.write(f"[logs] 跟随 {path}（Ctrl+C 退出）\n")
    sys.stderr.flush()
    try:
        while True:
            if not path.exists():
                _time.sleep(poll_seconds)
                continue
            try:
                size = path.stat().st_size
                if size < pos:      # 文件被轮转
                    pos = 0
                if size == pos:
                    _time.sleep(poll_seconds)
                    continue
                with path.open("r", encoding="utf-8") as fh:
                    fh.seek(pos)
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not _filter([item], evt=evt, level=level, node=node,
                                       trace=trace):
                            continue
                        print(_format(item), flush=True)
                        emitted += 1
                        if limit_lines and emitted >= limit_lines:
                            return
                    pos = fh.tell()
            except OSError:
                pass
            _time.sleep(poll_seconds)
    except KeyboardInterrupt:
        return


def _format(item: dict[str, Any]) -> str:
    ts = str(item.get("ts", ""))
    bits = [ts[11:23] or ts, str(item.get("level", ""))[:4],
            str(item.get("evt", ""))]
    for key in ("trace", "node", "section", "dispatch", "job"):
        if item.get(key):
            bits.append(f"{key}={item[key]}")
    if item.get("ms") is not None:
        bits.append(f"{item['ms']}ms")
    data = item.get("data") or {}
    if data:
        bits.append(" ".join(f"{k}={truncate(str(v), 60)}"
                             for k, v in list(data.items())[:6]))
    return " | ".join(str(b) for b in bits)


def main(argv: list[str] | None = None) -> int:
    """``research-agent-logs`` 命令入口。"""
    import argparse

    ap = argparse.ArgumentParser(
        prog="research-agent-logs",
        description="研究智能体统一事件日志（实时跟随 / 过滤 / 回看）")
    ap.add_argument("--evt", default="", help="事件名前缀，逗号分隔，如 dispatch,llm")
    ap.add_argument("--level", default="", help="最低级别 DEBUG/INFO/WARN/ERROR")
    ap.add_argument("--node", default="", help="按节点过滤")
    ap.add_argument("--trace", default="", help="按 trace 过滤")
    ap.add_argument("--last", type=int, default=0, help="先回放最近 N 条")
    ap.add_argument("--no-follow", action="store_true", help="只回放不跟随")
    ap.add_argument("--lines", type=int, default=0, help="跟随模式下打印 N 行后退出")
    ap.add_argument("--file", default="", help="指定日志文件")
    args = ap.parse_args(argv)

    if args.file:
        configure(path=args.file)
    tail(evt=args.evt, level=args.level, node=args.node, trace=args.trace,
         last=args.last, follow=not args.no_follow, limit_lines=args.lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
