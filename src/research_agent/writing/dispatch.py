"""派工协议：工作规划节点"下单"→ 各节点执行 → 逐步回报。

**为什么需要它**：在它之前，"派工"只有检索与抽取两条硬编码的通道
（`collaboration.py`），其余接了模型的节点（质量、消费、成段、审核、核查）
没有任何发令口，用户只能逐页手动点。派工协议把"下单 → 执行 → 回报"
做成唯一机制，登记表（`node_registry`）则回答"能派什么工"。

一次派工单的形态（对应方案 v8 §A2）::

    {
      "origin": "gap_decision|user_direct|interview",
      "section_key": "parameters",
      "reason": "判定缺量化条件，命中文献不足",
      "plan": [
        {"task": "retrieve", "node": "retrieval",
         "args": {"seed_terms": ["temperature"], "max_results": 20}},
        {"task": "extract_knowledge", "node": "knowledge",
         "args": {"paper_keys": "$step0.paper_keys"}}
      ],
      "budget": {"rounds": 2, "max_seconds": 900}
    }

四个必需性质：

- **幂等**：`dispatch_id` 已存在且非 failed 时直接返回既有结果，不重复执行；
- **可取消**：每步开始前与 `$stepN` 展开后都查 `cancel_event`；
- **收敛**：轮数上限 + 连续零新增即停（沿用 `collaboration` 的策略），
  并由 `budget.max_seconds` 兜住总时长；
- **可回报**：每步的产出 / 错误 / 跳过原因都落在 `steps` 里，写回 `dispatch_runs`。

步骤之间用 ``$step0.paper_keys`` 这样的引用传值——这是"检索 → 抽取"能串成
一条链、而不是两步各干各的关键。
"""
from __future__ import annotations

import contextvars
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from research_agent.config import settings as default_settings
from research_agent.db import connect
from research_agent.logging import bind_trace, log_event
from research_agent.writing import node_registry as reg

logger = logging.getLogger(__name__)

__all__ = [
    "new_dispatch_id",
    "run_dispatch",
    "dispatch_tasks",
    "get_dispatch",
    "list_dispatches",
    "current_context",
    "check_cancelled",
    "report_progress",
    "dispatch_entry",
]

#: 终态
TERMINAL = {"done", "failed", "cancelled", "skipped"}

#: 单次派工的硬上限（超出即停，避免一次派工把配额烧光）
MAX_STEPS = 12
MAX_SECONDS_HARD = 1800


class _DispatchContext:
    """当前派工的上下文：让登记表里的执行函数能拿到取消与进度。

    用 contextvars 而不是改签名：登记项指向的是**既有函数**
    （``process_papers``、``make_quality_node`` 等），强行加参数会污染它们的接口；
    而"能取消、能报进度"是派工的要求，不是那些函数的要求。
    """

    def __init__(self, dispatch_id: str, cancel_event: Any = None,
                 progress_cb: Any = None, trace: str = "") -> None:
        self.dispatch_id = dispatch_id
        self.cancel_event = cancel_event
        self.progress_cb = progress_cb
        self.trace = trace
        self.step = 0

    @property
    def cancelled(self) -> bool:
        return bool(self.cancel_event is not None
                    and self.cancel_event.is_set())


_CTX: contextvars.ContextVar[_DispatchContext | None] = contextvars.ContextVar(
    "dispatch_ctx", default=None)


def current_context() -> _DispatchContext | None:
    return _CTX.get()


def check_cancelled() -> bool:
    """供执行函数在长循环里主动查取消。返回 True 表示应尽快退出。"""
    ctx = _CTX.get()
    return bool(ctx and ctx.cancelled)


def report_progress(percent: float, message: str = "") -> None:
    """供执行函数上报进度（没有派工上下文时静默忽略）。"""
    ctx = _CTX.get()
    if ctx is None or ctx.progress_cb is None:
        return
    try:
        ctx.progress_cb(percent, message)
    except Exception:  # noqa: BLE001 —— 进度上报失败不该影响执行
        pass


def dispatch_entry(**kwargs: Any) -> dict[str, Any]:
    """执行登记表里任务的统一入口（派工执行器用）。

    额外把派工上下文的取消与进度传下去；`entry` 本身仍是既有函数。
    """
    task = str(kwargs.pop("task") or "")
    found = reg.get_task(task)
    if not found:
        return {"skipped": True, "skip_reason": f"未登记的任务: {task}"}
    _node, spec = found
    if spec.entry is None:
        return {"skipped": True, "skip_reason": f"任务未实现: {task}"}
    return spec.entry(**kwargs) or {}


def new_dispatch_id() -> str:
    return f"d-{uuid.uuid4().hex[:8]}"


# ------------------------------------------------------------------ 单次派工

def run_dispatch(
    *,
    plan: list[dict[str, Any]],
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
    dispatch_id: str = "",
    project_id: int = 0,
    section_key: str = "",
    origin: str = "user_direct",
    reason: str = "",
    budget: dict[str, Any] | None = None,
    cancel_event: Any = None,
    progress_cb: Any = None,
    trace: str = "",
    settings: Any = None,
    task_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行一张派工单，返回 `dispatch` 记录（含逐步报告）。

    ``task_kwargs`` 是给所有任务共享的公共参数（如 `settings`、`model`）。
    """
    s = settings or default_settings
    own_conn = conn is None
    db = conn or connect(db_path or s.db_path)
    dispatch_id = dispatch_id or new_dispatch_id()
    budget = budget or {}
    trace = trace or ""
    try:
        existing = _load(db, dispatch_id)
        if existing and existing.get("status") not in ("failed", None):
            # 幂等：同一单号不重复执行（失败的单允许重跑）
            log_event("dispatch.created", node="planner", dispatch=dispatch_id,
                      db=db, data={"outcome": "idempotent_hit",
                                   "status": existing.get("status")})
            return existing

        _save(db, {
            "dispatch_id": dispatch_id, "project_id": int(project_id or 0),
            "section_key": section_key or "", "origin": origin,
            "reason": reason or "", "plan_json": json.dumps(plan, ensure_ascii=False),
            "steps_json": "[]", "status": "running",
            "rounds": int(budget.get("rounds") or 0), "added": 0, "error": "",
            "trace": trace, "ts": _now(), "ended_ts": None,
        })
        log_event("dispatch.created", node="planner", dispatch=dispatch_id,
                  trace=trace, db=db,
                  data={"origin": origin, "section_key": section_key,
                        "steps": [str(st.get("task") or "") for st in plan][:MAX_STEPS],
                        "reason": reason[:200],
                        "rounds": int(budget.get("rounds") or 0)})

        ctx = _DispatchContext(dispatch_id, cancel_event, progress_cb, trace)
        # **把本次派工所在的库注入每一步**。
        #
        # 登记项的执行函数各自 `connect(db_path or 默认库)`，如果不告诉它们
        # "这次派工在哪个库上"，它们就会落到全局默认库上——即"对 A 库下单一，
        # 结果写进 B 库"。实测踩到过：单元测试里派工 `rebuild_ontology` 实际
        # 操作的是生产的 `data/research_agent.db`（所以那个测试其实一直在偷偷
        # 读写真实库，换到没有该库的目录才暴露成 `no such table`）。
        step_kwargs = dict(task_kwargs or {})
        step_kwargs.setdefault("conn", db)
        step_kwargs.setdefault("db_path", str(db_path or s.db_path))
        deadline = time.monotonic() + min(
            MAX_SECONDS_HARD, int(budget.get("max_seconds") or MAX_SECONDS_HARD))
        steps: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        status = "done"
        error = ""
        added_total = 0

        _ctx_token = _CTX.set(ctx)
        _trace_ctx = bind_trace(trace) if trace else None
        try:
            with (_trace_ctx or _NullCtx()):
                for index, item in enumerate(list(plan)[:MAX_STEPS]):
                    ctx.step = index
                    step = _run_step(db, index, item, results,
                                     step_kwargs, dispatch_id,
                                     section_key, s)
                    steps.append(step)
                    results.append(step.get("produced") or {})
                    added_total += int(step.get("added") or 0)
                    if step.get("status") == "cancelled":
                        status = "cancelled"
                        break
                    if step.get("status") == "failed":
                        status = "failed"
                        error = str(step.get("error") or "")[:300]
                        break
                    if time.monotonic() > deadline:
                        status = "done"
                        error = "已达本次派工时长上限，剩余步骤未执行"
                        steps.append({"status": "skipped",
                                      "skip_reason": error})
                        break
        finally:
            _CTX.reset(_ctx_token)

        record = {
            "dispatch_id": dispatch_id, "project_id": int(project_id or 0),
            "section_key": section_key or "", "origin": origin,
            "reason": reason or "", "plan_json": json.dumps(plan, ensure_ascii=False),
            "steps_json": json.dumps(steps, ensure_ascii=False),
            "status": status, "rounds": int(budget.get("rounds") or 0),
            "added": added_total, "error": error, "trace": trace,
            "ts": (existing or {}).get("ts") or _now(), "ended_ts": _now(),
        }
        _save(db, record)
        log_event("dispatch.done", node="planner", dispatch=dispatch_id,
                  trace=trace, db=db,
                  level="INFO" if status == "done" else "WARN",
                  data={"status": status, "added": added_total,
                        "steps": len(steps), "error": error[:200]})
        return _load(db, dispatch_id) or record
    finally:
        if own_conn:
            db.close()


class _NullCtx:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> None:
        return None


def _run_step(db: sqlite3.Connection, index: int, item: dict[str, Any],
              results: list[dict[str, Any]], base_kwargs: dict[str, Any],
              dispatch_id: str, section_key: str,
              settings: Any) -> dict[str, Any]:
    """执行一步：解析引用 → 查取消 → 执行 → 回收回报。"""
    task = str(item.get("task") or "")
    node = str(item.get("node") or "")
    started = time.monotonic()
    ctx = current_context()
    if ctx is not None and ctx.cancelled:
        return {"index": index, "task": task, "node": node,
                "status": "cancelled", "skip_reason": "用户取消"}

    # 计划方（`dispatch_planner`）在解析阶段就能看出某些步骤跑不了，
    # 并写下原因。这里如实回报，不假装执行过。
    preset_skip = str(item.get("skip_reason") or "")
    if preset_skip:
        step = {"index": index, "task": task, "node": node,
                "status": "skipped", "skip_reason": preset_skip,
                "seconds": 0.0}
        log_event("dispatch.step.skipped", node=node, dispatch=dispatch_id,
                  db=db, data=step)
        return step

    found = reg.get_task(task)
    if not found:
        return {"index": index, "task": task, "node": node,
                "status": "failed", "error": f"未登记的任务: {task}",
                "seconds": 0.0}
    spec_node, spec = found
    node = node or spec_node.node
    if spec.entry is None:
        return {"index": index, "task": task, "node": node,
                "status": "skipped", "skip_reason": f"任务尚未实现: {task}",
                "seconds": 0.0}

    try:
        args = _resolve_args(item.get("args") or {}, results)
    except _Unresolved as exc:
        step = {"index": index, "task": task, "node": node,
                "status": "skipped", "skip_reason": str(exc), "seconds": 0.0}
        log_event("dispatch.step.skipped", node=node, dispatch=dispatch_id,
                  db=db, data=step)
        return step

    log_event("dispatch.step.start", node=node, dispatch=dispatch_id,
              trace=(ctx.trace if ctx else ""),
              data={"task": task, "index": index,
                    "args": _brief(args)})
    kwargs = dict(base_kwargs)
    kwargs.update(args)
    kwargs.setdefault("dispatch_id", dispatch_id)
    kwargs.setdefault("section_key", section_key)
    try:
        produced = dispatch_entry(task=task, **kwargs) or {}
    except Exception as exc:  # noqa: BLE001 —— 单步失败不该炸掉整张单
        seconds = round(time.monotonic() - started, 2)
        step = {"index": index, "task": task, "node": node,
                "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300],
                "seconds": seconds}
        logger.warning("派工步骤失败 %s/%s: %s", dispatch_id, task, exc)
        log_event("dispatch.step.end", node=node, dispatch=dispatch_id,
                  level="ERROR", ms=seconds * 1000, db=db,
                  data={"task": task, "status": "failed",
                        "error": step["error"]})
        return step

    seconds = round(time.monotonic() - started, 2)
    skipped = bool(produced.get("skipped"))
    step = {
        "index": index, "task": task, "node": node,
        "status": "skipped" if skipped else "done",
        "seconds": seconds,
        "produced": _brief(produced),
        "added": int(produced.get("count") or produced.get("added")
                     or produced.get("extracted") or 0),
    }
    if skipped:
        step["skip_reason"] = str(produced.get("skip_reason") or "跳过")
    log_event("dispatch.step.end" if not skipped else "dispatch.step.skipped",
              node=node, dispatch=dispatch_id, ms=seconds * 1000, db=db,
              data={"task": task, "status": step["status"],
                    "seconds": seconds,
                    "skip_reason": step.get("skip_reason", ""),
                    "produced": step["produced"]})
    return step


# ------------------------------------------------------------------ 引用解析

class _Unresolved(Exception):
    """``$stepN.field`` 指不到值时抛出——比传一个空值诚实。"""


def _resolve_args(args: dict[str, Any],
                  results: list[dict[str, Any]]) -> dict[str, Any]:
    """展开 ``$step0.paper_keys`` 形式的引用。

    这是"检索 → 抽取"能自动串起来的关键：抽取的 `paper_keys` 不必由调用方
    预先知道，直接引用上一步的产出即可。
    """
    out: dict[str, Any] = {}
    for key, value in args.items():
        out[key] = _resolve_value(value, results)
    return out


def _resolve_value(value: Any, results: list[dict[str, Any]]) -> Any:
    if isinstance(value, str) and value.startswith("$step"):
        return _lookup(value, results)
    if isinstance(value, list):
        return [_resolve_value(v, results) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_value(v, results) for k, v in value.items()}
    return value


def _lookup(ref: str, results: list[dict[str, Any]]) -> Any:
    body = ref[len("$step"):]
    index_text, _, field = body.partition(".")
    try:
        index = int(index_text)
    except ValueError as exc:
        raise _Unresolved(f"引用格式不对: {ref}") from exc
    if index >= len(results):
        raise _Unresolved(f"引用不到上一步: {ref}")
    value = results[index]
    for part in [p for p in field.split(".") if p]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise _Unresolved(f"引用不到字段: {ref}")
    if value in (None, [], ""):
        raise _Unresolved(f"引用到的值为空: {ref}")
    return value


def _brief(value: Any, limit: int = 400) -> Any:
    """把产出压成可入库的摘要（长正文只留长度，避免 dispatch_runs 膨胀）。"""
    if not isinstance(value, dict):
        return value if isinstance(value, (int, float, bool)) else str(value)[:limit]
    out: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, str) and len(item) > 200:
            out[key] = f"<{len(item)}字符>"
        elif isinstance(item, list):
            if item and isinstance(item[0], str) and len(item) > 8:
                out[key] = list(item[:8]) + [f"…共{len(item)}项"]
            else:
                out[key] = item[:8]
        elif isinstance(item, dict):
            out[key] = _brief(item, limit=120)
        else:
            out[key] = item
    return out


# ------------------------------------------------------------------ 落库/读取

def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(
        timespec="seconds")


def _save(db: sqlite3.Connection, record: dict[str, Any]) -> None:
    db.execute(
        "INSERT INTO dispatch_runs(dispatch_id, project_id, section_key, origin,"
        " reason, plan_json, steps_json, status, rounds, added, error, trace,"
        " ts, ended_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(dispatch_id) DO UPDATE SET project_id=excluded.project_id,"
        " section_key=excluded.section_key, origin=excluded.origin,"
        " reason=excluded.reason, plan_json=excluded.plan_json,"
        " steps_json=excluded.steps_json, status=excluded.status,"
        " rounds=excluded.rounds, added=excluded.added, error=excluded.error,"
        " trace=excluded.trace, ended_ts=excluded.ended_ts",
        (record.get("dispatch_id"), int(record.get("project_id") or 0),
         record.get("section_key") or "", record.get("origin") or "",
         (record.get("reason") or "")[:500],
         record.get("plan_json") or "[]", record.get("steps_json") or "[]",
         record.get("status") or "", int(record.get("rounds") or 0),
         int(record.get("added") or 0), (record.get("error") or "")[:500],
         record.get("trace") or "", record.get("ts") or _now(),
         record.get("ended_ts")))
    db.commit()


def _load(db: sqlite3.Connection, dispatch_id: str) -> dict[str, Any] | None:
    try:
        row = db.execute("SELECT * FROM dispatch_runs WHERE dispatch_id=?",
                         (dispatch_id,)).fetchone()
    except sqlite3.Error:
        return None
    return _row_to_dict(row) if row else None


def get_dispatch(db: sqlite3.Connection, dispatch_id: str
                 ) -> dict[str, Any] | None:
    return _load(db, dispatch_id)


def list_dispatches(db: sqlite3.Connection, *, project_id: int = 0,
                    section_key: str = "", limit: int = 20
                    ) -> list[dict[str, Any]]:
    """最近的派工单（倒序）。界面上的「当前派工」看板用它。"""
    clauses, params = [], []
    if project_id:
        clauses.append("project_id=?")
        params.append(int(project_id))
    if section_key:
        clauses.append("section_key=?")
        params.append(section_key)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        rows = db.execute(
            f"SELECT * FROM dispatch_runs{where} ORDER BY ts DESC LIMIT ?",
            (*params, max(1, int(limit)))).fetchall()
    except sqlite3.Error:
        return []
    return [_row_to_dict(row) for row in rows]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for field in ("plan_json", "steps_json"):
        raw = item.pop(field, None)
        key = field.replace("_json", "")
        try:
            item[key] = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            item[key] = []
    return item


# ------------------------------------------------------------------ 便捷入口

def dispatch_tasks(tasks: list[str], *, dispatch_id: str = "",
                   **kwargs: Any) -> dict[str, Any]:
    """按任务名列表下单（不写 plan 的便捷入口，测试与直连场景用）。

    参数会**整体**传给每个任务；需要"上一步产出喂给下一步"时请用
    ``run_dispatch(plan=[...])`` 并以 ``$stepN.field`` 引用。
    """
    plan = []
    for task in tasks:
        found = reg.get_task(str(task))
        node = found[0].node if found else ""
        plan.append({"task": str(task), "node": node})
    if kwargs.pop("with_args", False):
        plan = [dict(item, args=dict(kwargs)) for item in plan]
    return run_dispatch(plan=plan, dispatch_id=dispatch_id, **kwargs)
