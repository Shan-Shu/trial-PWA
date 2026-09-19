"""任务适配：把引擎的作业管理器包成界面期望的 `TaskManager`。

**界面要什么**（PWA `ui/task_ui.py`，46 行）：

- ``start(task_id, func, *args)`` —— 由**调用方给定** task_id
- ``status(task_id)`` → ``{"status", "percent", "message", "error", ...}``
- ``cancel(task_id)``

**引擎有什么**（`research_agent/library/jobs.py`，227 行）：

- ``start(action, runner, *args, total=..., **kwargs)`` —— **job_id 由引擎生成**
- ``status(job_id)`` → 形态几乎一致（status/percent/message/error/result/elapsed）
- ``cancel(job_id)``
- runner 会被以 ``runner(*args, progress_cb=…, cancel_event=…, **kwargs)`` 调用

所以这一层只做两件事：**task_id ↔ job_id 的映射**，以及复用引擎那套
（超时、取消、进度、64 条上限清理）而不重写一遍。

**刻意不并入 PWA 的 `task_manager.py`**：它有非可重入锁自锁的缺陷
（见 `docs/MERGE_NOTES.md` 的记录），而引擎这套是跑在生产上的。
"""
from __future__ import annotations

import threading
from typing import Any, Callable

__all__ = ["TaskManager", "ManagerForDesk"]

#: 引擎作业的终态
_TERMINAL = {"done", "error", "cancelled"}


class TaskManager:
    """按界面习惯（调用方给 task_id）驱动引擎作业管理器。"""

    def __init__(self, db_path: str | None = None) -> None:
        from research_agent.library.jobs import LibraryJobManager

        self._manager = LibraryJobManager(db_path)
        self._lock = threading.Lock()
        #: task_id → 最近一次 job_id。界面会重复 start 同一个 task_id（重跑），
        #: 所以每次 start 都要覆盖映射，而不是只记第一次。
        self._jobs: dict[str, str] = {}

    # ------------------------------------------------------------------ 对外
    def start(self, task_id: str | None, func: Callable,
              *args: Any, **kwargs: Any) -> str:
        """起一个任务，返回 task_id（与界面约定一致）。"""
        task_id = str(task_id or "")
        action = kwargs.pop("action", None) or task_id or "task"
        job_id = self._manager.start(action, func, *args,
                                     total=int(kwargs.pop("total", 100) or 100),
                                     **kwargs)
        with self._lock:
            self._jobs[task_id or job_id] = job_id
        return task_id or job_id

    def status(self, task_id: str) -> dict[str, Any] | None:
        job_id = self._job_id(task_id)
        if not job_id:
            return None
        snap = self._manager.status(job_id)
        if snap is None:
            return None
        # 引擎的字段名与界面一致，直接透传即可；只补一个 total
        return snap

    def cancel(self, task_id: str) -> bool:
        job_id = self._job_id(task_id)
        if not job_id:
            return False
        return bool(self._manager.cancel(job_id))

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._manager.list_jobs(limit=limit)

    # ------------------------------------------------------------------ 内部
    def _job_id(self, task_id: str) -> str:
        with self._lock:
            direct = self._jobs.get(str(task_id))
            if direct:
                return direct
        # 也允许直接拿 job_id 查（引擎侧入口与界面入口共存时有用）
        return str(task_id or "")


def ManagerForDesk(db_path: str | None = None) -> TaskManager:
    """构造一个新的任务管理器（界面里用 `@st.cache_resource` 缓存住）。"""
    return TaskManager(db_path)
