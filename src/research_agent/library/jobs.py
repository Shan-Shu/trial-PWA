"""文献库批量作业：进度、取消与结果。

设计要点（合并方案 v2 决策 D6）：

- 不引入来源项目 PWA 的 ``TaskManager``（其已知缺陷：``st.fragment`` 版本依赖、
  ``list_tasks()`` 非可重入锁自锁、``@st.cache_resource`` 单例 + 固定 task_id
  导致多会话串号）；改为**每作业独立 job_id** 的线程 + 内存态 + ``processing_log`` 事件；
- 进度回调与取消沿用节点已有的 ``progress_cb`` / ``cancel_event`` 协议，
  因此批量作业可以直接驱动既有节点函数，不需要第二套机制；
- 作业态只存内存（服务重启即丢），但**每个作业的关键步骤写入 processing_log**，
  因此重启后仍可从事件流审计"做过什么"。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable, Iterable

from research_agent.db import connect, log_event

logger = logging.getLogger(__name__)

#: 终态集合
TERMINAL = {"done", "error", "cancelled"}


class LibraryJob:
    """单个批量作业的可变状态（线程安全由 LibraryJobManager 的锁保证）。"""

    def __init__(self, job_id: str, action: str, total: int):
        self.job_id = job_id
        self.action = action
        self.total = total
        self.done = 0
        self.percent = 0.0
        self.message = "排队中"
        self.status = "running"
        self.error: str | None = None
        self.result: dict[str, Any] | None = None
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.cancel_event = threading.Event()

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "action": self.action,
            "status": self.status,
            "percent": round(float(self.percent), 1),
            "done": self.done,
            "total": self.total,
            "message": self.message,
            "error": self.error,
            "result": self.result,
            "elapsed": round(
                (self.ended_at or time.time()) - self.started_at, 1
            ),
        }


class LibraryJobManager:
    """批量作业管理器（内存态 + 每作业一线程）。"""

    #: 最多同时保留的作业快照数（超出后清理最旧的终态作业）
    MAX_JOBS = 64

    def __init__(self, db_path: str | None = None):
        self._lock = threading.Lock()
        self._jobs: dict[str, LibraryJob] = {}
        self._threads: dict[str, threading.Thread] = {}
        self.db_path = db_path

    # ------------------------------------------------------------- 对外接口
    def start(
        self,
        action: str,
        runner: Callable[..., dict[str, Any]],
        *args: Any,
        total: int = 0,
        **kwargs: Any,
    ) -> str:
        """启动一个作业。

        ``runner`` 必须接受 ``progress_cb`` 与 ``cancel_event`` 两个关键字参数，
        因此可直接传入既有节点函数（如 ``pipeline.process_papers``）。
        """
        job_id = uuid.uuid4().hex[:12]
        job = LibraryJob(job_id, action, int(total or 0))
        with self._lock:
            self._jobs[job_id] = job
            self._prune_locked()

        def _progress(percent: float, message: str = "") -> None:
            with self._lock:
                current = self._jobs.get(job_id)
                if not current:
                    return
                current.percent = max(0.0, min(100.0, float(percent)))
                if message:
                    current.message = str(message)

        def _run() -> None:
            db = None
            try:
                db = connect(self.db_path)
                log_event(db, "library", f"{action}-start", None,
                          {"job_id": job_id, "total": int(total or 0)})
            except Exception:  # noqa: BLE001 —— 事件日志失败不应影响作业
                logger.warning("作业事件写入失败 job=%s", job_id, exc_info=True)
            try:
                result = runner(*args, progress_cb=_progress,
                                cancel_event=job.cancel_event, **kwargs)
                with self._lock:
                    current = self._jobs[job_id]
                    if current.cancel_event.is_set():
                        current.status = "cancelled"
                        current.message = "已取消"
                    else:
                        current.status = "done"
                        current.percent = 100.0
                        current.message = "完成"
                    current.result = result if isinstance(result, dict) else {"result": result}
                    current.ended_at = time.time()
            except BaseException as exc:  # noqa: BLE001 —— 作业边界必须兜住
                with self._lock:
                    current = self._jobs.get(job_id)
                    if current:
                        cancelled = current.cancel_event.is_set()
                        current.status = "cancelled" if cancelled else "error"
                        current.error = None if cancelled else f"{type(exc).__name__}: {exc}"
                        current.message = "已取消" if cancelled else "失败"
                        current.ended_at = time.time()
                if not isinstance(exc, KeyboardInterrupt):
                    logger.warning("作业失败 job=%s", job_id, exc_info=True)
            finally:
                if db is not None:
                    try:
                        with self._lock:
                            snap = self._jobs[job_id].snapshot()
                        log_event(db, "library", f"{action}-end", None, snap)
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        try:
                            db.close()
                        except Exception:  # noqa: BLE001
                            pass

        thread = threading.Thread(target=_run, name=f"libjob-{job_id}", daemon=True)
        with self._lock:
            self._threads[job_id] = thread
        thread.start()
        return job_id

    def status(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.snapshot() if job else None

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        """列出最近作业。**注意**：先取快照再返回，避免持锁回调自身（PWA 的坑）。"""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)
            snapshots = [job.snapshot() for job in jobs[:max(1, int(limit))]]
        return snapshots

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in TERMINAL:
                return False
            job.cancel_event.set()
            job.status = "cancelling"
            job.message = "正在取消…"
        return True

    # ------------------------------------------------------------- 内部
    def _prune_locked(self) -> None:
        if len(self._jobs) <= self.MAX_JOBS:
            return
        finished = [j for j in self._jobs.values() if j.status in TERMINAL]
        finished.sort(key=lambda j: j.ended_at or j.started_at)
        for job in finished[: max(0, len(self._jobs) - self.MAX_JOBS)]:
            self._jobs.pop(job.job_id, None)
            self._threads.pop(job.job_id, None)


#: 进程级单例（看板使用）；测试请自行构造独立实例
_MANAGER: LibraryJobManager | None = None


def manager_for(db_path: str | None = None) -> LibraryJobManager:
    """按 db_path 取管理器；同一进程内复用，且**不用** ``cache_resource`` 式全局单例语义。"""
    global _MANAGER
    if _MANAGER is None or _MANAGER.db_path != db_path:
        _MANAGER = LibraryJobManager(db_path)
    return _MANAGER


def run_batch_extract(
    paper_keys: Iterable[str],
    *,
    db_path: str | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """批量知识抽取作业体。

    逐篇复用既有 ``pipeline.process_papers``（其内部已做单篇失败隔离与
    「质量→知识」全流程编排），本函数只负责**进度与取消**，不重复实现流程。
    """
    keys = [str(k) for k in paper_keys if k]
    total = max(1, len(keys))
    ok, failed, skipped = 0, 0, 0
    errors: list[dict[str, str]] = []

    from research_agent.pipeline import Services, process_papers

    services = Services()
    for index, key in enumerate(keys, 1):
        if cancel_event is not None and cancel_event.is_set():
            break
        if progress_cb is not None:
            progress_cb((index - 1) / total * 100.0, f"处理 {index}/{len(keys)}")
        db = connect(db_path)
        try:
            results = process_papers([key], services=services, conn=db)
        except Exception as exc:  # noqa: BLE001 —— 作业边界
            results = [{"status": "error", "error": f"{type(exc).__name__}: {exc}"}]
        finally:
            db.close()
        out = results[0] if results else {}
        status = str((out or {}).get("status") or "")
        if status in ("extracted", "knowledge"):
            ok += 1
        elif status in ("skipped-no-model", "skipped-no-paper"):
            skipped += 1
        else:
            failed += 1
            errors.append({
                "paper_key": key,
                "error": str((out or {}).get("error") or status or "unknown"),
            })
    if progress_cb is not None:
        progress_cb(100.0, "完成")
    return {
        "total": len(keys), "ok": ok, "failed": failed, "skipped": skipped,
        "errors": errors[:20],
    }
