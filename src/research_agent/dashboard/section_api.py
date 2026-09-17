"""节点写作 REST 数据层：规划、撰写、进度、取消、决策轨迹。

与 ``writing_api.py``（PWA 写作台的直接移植）的分工：

- ``writing_api`` 负责**项目与大纲**（体裁、骨架、手工保存/润色）；
- 本模块负责**大纲节点上的指令工作流**：先判库够不够，不够先补检，
  再成段，并把每一轮的判定依据全部留痕。

``compose`` 是**异步作业**（补检可能跑分钟级），因此返回 ``job_id``；
``plan`` 是同步的（只读库 + 可选的确定性规划），供「征求意见」用。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from research_agent.config import settings as default_settings
from research_agent.db import connect
from research_agent.writing import section_service as sections
from research_agent.writing import service as writing

__all__ = [
    "plan_section",
    "project_plan",
    "section_templates",
    "compose_section",
    "job_status",
    "cancel_job",
    "section_trace",
    "section_states",
    "section_content",
]


def _open(db_path: str | Path | None = None) -> sqlite3.Connection:
    return connect(Path(db_path) if db_path else default_settings.db_path)


def plan_section(db_path: str | Path | None, project_id: int, section_key: str,
                 instruction: str = "",
                 settings: Any = None,
                 user_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    """预判本节够不够写：给出检索规划 + 当前库的充分性判定（不检索、不写正文）。

    指令与字段**都可选**：都不给时按工作规划与部分模板的默认值判定。
    """
    conn = _open(db_path)
    try:
        model, reason = writing.default_model()
        result = sections.plan_section(
            conn, project_id=project_id, section_key=section_key,
            instruction=instruction, user_fields=user_fields,
            planner_model=model, settings=settings, evaluate=True,
        )
        result["model_unavailable_reason"] = reason if model is None else ""
        verdict = result.get("sufficiency") or {}
        result["judged_by"] = ("llm" if model is not None
                               and result.get("planner_mode") == "llm"
                               else "deterministic")
        result["llm_used"] = bool(model is not None
                                  and result.get("planner_mode") == "llm")
        result["sufficiency_mode"] = (
            "确定性阈值判定（模型未参与充分性判定）"
            if not verdict.get("llm_verdict")
            else "确定性阈值 + 模型复核")
        return result
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def project_plan(db_path: str | Path | None, project_id: int,
                 topic: str = "", instruction: str = "",
                 settings: Any = None,
                 persist: bool = True) -> dict[str, Any]:
    """生成整篇工作规划（唯一规划节点）。

    不传 ``instruction`` → 系统依主题自拟；传了 → 以用户指令为准。
    """
    conn = _open(db_path)
    try:
        model, reason = writing.default_model()
        result = sections.build_project_plan(
            conn, project_id=int(project_id), topic=topic,
            instruction=instruction, settings=settings, planner_model=model,
            persist=persist)
        result["model_unavailable_reason"] = reason if model is None else ""
        return result
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def section_templates(db_path: str | Path | None, genre: str | None = None
                      ) -> dict[str, Any]:
    """列出某体裁的部分模板（供界面渲染结构与字段）。"""
    from research_agent.writing import section_template as stpl
    key, spec = stpl.genre_definition(genre)
    return {"ok": True, "genre": key,
            "label": str(spec.get("label") or key),
            "templates": stpl.list_templates(genre),
            "sections": [
                {"key": s.get("key"), "heading": s.get("heading"),
                 "words": s.get("words"), "template": s.get("template"),
                 "fields": stpl.collect_section_fields(key, str(s.get("key")))}
                for s in (spec.get("sections") or []) if isinstance(s, dict)
            ]}


def compose_section(db_path: str | Path | None, project_id: int, section_key: str,
                    instruction: str = "",
                    settings: Any = None,
                    user_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    """撰写此部分：启动异步作业，返回 ``job_id``。

    指令与字段都可选——都不给即"系统自拟"模式。
    """
    path = str(db_path) if db_path else None
    try:
        job_id = sections.start_section_run(
            project_id=int(project_id), section_key=str(section_key),
            instruction=str(instruction or ""), user_fields=user_fields,
            db_path=path, settings=settings,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 —— 作业启动失败要回给界面
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "job_id": job_id, "status": "running",
            "project_id": int(project_id), "section_key": str(section_key),
            "mode": "user" if (instruction or user_fields) else "auto"}


def job_status(db_path: str | Path | None, job_id: str) -> dict[str, Any]:
    snapshot = sections.section_job_status(job_id, str(db_path) if db_path else None)
    if not snapshot:
        return {"ok": False, "error": f"作业不存在或已过期: {job_id}"}
    snapshot["ok"] = True
    result = snapshot.get("result") or {}
    # 作业终态为 done 但链路返回 needs_data 时，作业本身不算失败：
    # 那是"证据不足，按约定不硬写"，必须与"报错"区分开。
    snapshot["outcome"] = str(result.get("status") or snapshot.get("status") or "")
    snapshot["needs_data"] = snapshot["outcome"] == "needs_data"
    snapshot["invalid_indices"] = result.get("invalid_indices") or []
    return snapshot


def cancel_job(db_path: str | Path | None, job_id: str) -> dict[str, Any]:
    ok = sections.cancel_section_run(job_id, str(db_path) if db_path else None)
    return {"ok": ok, "job_id": job_id}


def section_trace(db_path: str | Path | None, project_id: int,
                  section_key: str, limit: int = 20) -> dict[str, Any]:
    conn = _open(db_path)
    try:
        data = sections.get_section_trace(conn, project_id, section_key, limit)
        data["ok"] = True
        return data
    finally:
        conn.close()


def section_content(db_path: str | Path | None, project_id: int,
                    section_key: str) -> dict[str, Any]:
    """只取单个节点的正文与溯源信息（撰写完成后局部刷新用，避免拉整项目）。"""
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT heading, content, citation_ids, status, grounded_on, "
            "       last_run_id "
            "FROM writing_sections WHERE project_id=? AND section_key=?",
            (int(project_id), str(section_key)),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"大纲节点不存在: {section_key}"}
        data = dict(row)
        for key, default in (("citation_ids", []), ("grounded_on", {})):
            raw = data.pop(key, None)
            try:
                data[key] = json.loads(raw) if raw else default
            except (json.JSONDecodeError, TypeError):
                data[key] = default
        data.update({"ok": True, "project_id": int(project_id),
                     "section_key": str(section_key)})
        return data
    finally:
        conn.close()


def section_states(db_path: str | Path | None,
                   project_id: int) -> dict[str, Any]:
    """该项目的所有节点状态（不含正文，供列表页渲染徽标）。"""
    conn = _open(db_path)
    try:
        return {"ok": True, "project_id": int(project_id),
                "states": sections.latest_section_state(conn, project_id)}
    finally:
        conn.close()
