"""研究任务节点运行事件写入，供看板实时观察各节点状态。"""
from __future__ import annotations

import sqlite3
from typing import Any

from research_agent.config import Settings
from research_agent.db import connect, log_event


def log_study_event(conn: sqlite3.Connection | None,
                    settings: Settings,
                    event: str,
                    run_id: str | None,
                    status: str,
                    details: dict[str, Any] | None = None) -> None:
    """向 processing_log 写入一条 node='study' 事件。"""
    own = conn is None
    db = conn or connect(settings.db_path)
    try:
        payload = {"run_id": run_id, "status": status}
        if details:
            payload.update(details)
        log_event(db, "study", event, None, payload)
    finally:
        if own:
            db.close()
