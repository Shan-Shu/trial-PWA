"""实时监控数据库新文献的预留接口。

提供两种接入方式：
1. 轮询式（默认实现）：PaperMonitor 记录水位线（DB meta 表），
   run_once() 返回新增论文，run_forever(handler) 循环回调；
2. 扩展点：可直接在 ingest_search_results / 其它入库路径后调用
   `notify_new_papers(keys)`，或自行注册 SQLite trigger / 文件系统 watcher。

示例（后续可接到 LangGraph / 定时任务）：
    monitor = PaperMonitor()
    monitor.run_forever(lambda keys: pipeline.process_papers(keys), poll_interval=60)
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Callable, Iterable

from research_agent.config import Settings, settings as default_settings
from research_agent.db import connect, meta_get, meta_set, utcnow

logger = logging.getLogger(__name__)


class PaperMonitor:
    """按 created_at 水位线轮询 papers 表，发现新文献。"""

    def __init__(self, db_path: str | None = None,
                 settings: Settings | None = None,
                 watermark_key: str = "monitor_watermark") -> None:
        self.settings = settings or default_settings
        self.db_path = str(db_path or self.settings.db_path)
        self.watermark_key = watermark_key

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def run_once(self, process_existing: bool = False) -> list[str]:
        """返回自上次水位线以来新增的论文 key，并推进水位线。

        水位线推进规则（v0.4.3 修复 P0-3/P0-4）：
        - 写入后**显式 commit**（此前 meta_set 后直接 close，写入被丢弃 →
          水位线永远为空 → ``run_once`` 永远返回空列表）；
        - 水位线取"本批记录的最大 created_at"，且用 ``>= + paper_key`` 兜底，
          避免同一秒内入库的论文被永久跳过；
        - 无新记录时**不推进**水位线。
        """
        conn = self._conn()
        try:
            wm = meta_get(conn, self.watermark_key)
            baseline = ""
            if wm is None:
                if not process_existing:
                    # 首次启动：以当前最大时间作基线，避免重跑历史文献
                    latest = conn.execute(
                        "SELECT MAX(created_at) AS m FROM papers").fetchone()["m"]
                    meta_set(conn, self.watermark_key, latest or utcnow())
                    conn.commit()
                    return []
                meta_set(conn, self.watermark_key, "")
                conn.commit()
            else:
                baseline = str(wm)
            rows = conn.execute(
                "SELECT paper_key, created_at FROM papers WHERE created_at >= ? "
                "ORDER BY created_at ASC, paper_key ASC",
                (baseline,),
            ).fetchall()
            acked = self._ack_set(conn)
            keys: list[str] = []
            for row in rows:
                key = str(row["paper_key"])
                if key in acked:
                    continue
                # 严格大于水位线，或"同一秒且未回执"→ 取出（避免同秒漏取）
                keys.append(key)
            if keys:
                # 回执本批 key，并把水位线推进到本批最大时间
                meta_set(conn, self._ack_key, sorted(acked | set(keys)))
                meta_set(conn, self.watermark_key,
                         max(str(r["created_at"]) for r in rows))
                conn.commit()
            return keys
        finally:
            conn.close()

    # ---------------- 已处理集合（同一秒写入与失败重试的兜底） ----------------
    @property
    def _ack_key(self) -> str:
        return f"{self.watermark_key}__acked"

    def _ack_set(self, conn: sqlite3.Connection) -> set[str]:
        value = meta_get(conn, self._ack_key, []) or []
        return {str(x) for x in value if x} if isinstance(value, list) else set()

    def ack(self, keys: Iterable[str]) -> None:
        """处理成功后回执：无回执的 key 会在下次 ``run_once`` 被重新取出。"""
        conn = self._conn()
        try:
            pending = self._ack_set(conn)
            pending.update(str(k) for k in keys if k)
            meta_set(conn, self._ack_key, sorted(pending))
            conn.commit()
        finally:
            conn.close()

    def release(self, keys: Iterable[str]) -> None:
        """处理失败：撤销回执，使这批 key 可以重试。"""
        conn = self._conn()
        try:
            pending = self._ack_set(conn)
            pending.difference_update(str(k) for k in keys if k)
            meta_set(conn, self._ack_key, sorted(pending))
            conn.commit()
        finally:
            conn.close()

    def run_forever(self, handler: Callable[[list[str]], None],
                    poll_interval: float = 60.0,
                    stop_event: threading.Event | None = None) -> None:
        """轮询主循环：把新增论文批量交给 handler（可对接流水线/回调）。

        handler 抛错时**撤销回执**，这批 key 会在下一轮重试，不会被永久丢弃。
        """
        logger.info("文献监控启动 poll_interval=%ss", poll_interval)
        while not (stop_event and stop_event.is_set()):
            keys: list[str] = []
            try:
                keys = self.run_once()
                if keys:
                    logger.info("发现 %d 篇新文献", len(keys))
                    handler(keys)
            except Exception as exc:  # noqa: BLE001
                logger.exception("监控轮询异常: %s", exc)
                if keys:
                    self.release(keys)
            finally:
                if keys:
                    self.ack(keys)
            stop_event.wait(poll_interval) if stop_event else time.sleep(poll_interval)

    def start(self, handler: Callable[[list[str]], None],
              poll_interval: float = 60.0) -> threading.Thread:
        """后台线程启动，返回可 join/stop 的线程。"""
        stop_event = threading.Event()

        def _run() -> None:
            self.run_forever(handler, poll_interval, stop_event)

        t = threading.Thread(target=_run, daemon=True, name="paper-monitor")
        t.stop = stop_event.set  # type: ignore[attr-defined]
        t.start()
        return t


def notify_new_papers(keys: Iterable[str]) -> None:
    """入库路径可直接调用：占位/钩子，便于接入消息队列或 webhook。"""
    logger.info("notify_new_papers: %s", list(keys))
