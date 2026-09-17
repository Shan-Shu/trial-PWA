"""写作台 REST 数据层（合并新增）。

来源：``paper_writing_assistant`` 的 ``ui/views/writing.py`` 能力清单
（项目/大纲/章节/润色），但：

- 体裁与章节骨架来自 ``packs/skills/writing``，不硬编码；
- 模型不可用时返回 ``generated_by="skeleton_fallback"`` 并附带原因，
  前端必须如实展示，不得把骨架草稿当成模型产物。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from research_agent.config import settings as default_settings
from research_agent.db import connect
from research_agent.writing import service as writing

__all__ = [
    "genres",
    "list_projects",
    "create_project",
    "delete_project",
    "project_detail",
    "generate_outline",
    "generate_section",
    "save_section",
    "polish_section",
    "export_project",
]


def _open(db_path: str | Path | None = None) -> sqlite3.Connection:
    return connect(Path(db_path) if db_path else default_settings.db_path)


def _model() -> tuple[Any, str]:
    """返回 (model, 不可用原因)；原因会透传到界面，避免"未提供模型"含义不明。"""
    return writing.default_model()


def genres() -> list[dict[str, Any]]:
    return writing.list_genres()


def list_projects(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    conn = _open(db_path)
    try:
        return writing.list_projects(conn)
    finally:
        conn.close()


def create_project(db_path: str | Path | None, title: str, topic: str = "",
                   genre: str | None = None) -> dict[str, Any]:
    conn = _open(db_path)
    try:
        project_id = writing.create_project(conn, title, topic, genre)
        return {"ok": True, "project_id": project_id,
                "project": writing.get_project(conn, project_id)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def delete_project(db_path: str | Path | None, project_id: int) -> dict[str, Any]:
    conn = _open(db_path)
    try:
        writing.delete_project(conn, project_id)
        return {"ok": True, "project_id": int(project_id)}
    finally:
        conn.close()


def project_detail(db_path: str | Path | None,
                   project_id: int) -> dict[str, Any]:
    conn = _open(db_path)
    try:
        project = writing.get_project(conn, project_id)
        if not project:
            return {"ok": False, "error": f"写作项目不存在: {project_id}"}
        # 带上工作规划与逐部分模板信息：界面一次请求即可渲染
        # "本部分要写什么、要什么证据、有哪些可填字段"，不必再多打一轮接口。
        from research_agent.writing import section_template as stpl
        from research_agent.writing.section_service import _load_work_plan
        genre = str(project.get("genre") or "")
        plan = _load_work_plan(conn, int(project_id))
        planned = {str(s.get("section_key")): s
                   for s in (plan.get("sections") or [])}
        sections = writing.list_sections(conn, project_id)
        for section in sections:
            key = str(section.get("section_key"))
            tpl_key, tpl = stpl.template_for_section(genre, key)
            item = planned.get(key) or {}
            section["template"] = tpl_key
            section["required_dimensions"] = list(
                item.get("required_dimensions")
                or tpl.get("required_dimensions") or [])
            section["evidence_types"] = list(
                item.get("evidence_types") or tpl.get("evidence_types") or [])
            section["role"] = str(item.get("role") or tpl.get("role") or "")
            section["fields"] = list(
                item.get("fields") or stpl.collect_section_fields(genre, key))
            section["generates_body"] = bool(tpl_key)
        return {"ok": True, "project": project, "sections": sections,
                "work_plan": plan or None}
    finally:
        conn.close()


def generate_outline(db_path: str | Path | None, project_id: int,
                     topic: str = "", use_model: bool = True) -> dict[str, Any]:
    conn = _open(db_path)
    try:
        model, reason = _model() if use_model else (None, "调用方显式要求不使用模型")
        result = writing.generate_outline(conn, project_id, topic=topic, model=model)
        result["ok"] = True
        result["model_unavailable_reason"] = reason if model is None else ""
        return result
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def generate_section(db_path: str | Path | None, project_id: int,
                     section_key: str, heading: str, topic: str = "",
                     use_model: bool = True) -> dict[str, Any]:
    conn = _open(db_path)
    try:
        model, reason = _model() if use_model else (None, "调用方显式要求不使用模型")
        result = writing.generate_section(
            conn, project_id, section_key, heading, topic=topic,
            model=model,
            db_path=str(db_path) if db_path else None,
            model_reason=reason,
        )
        result["ok"] = True
        result["model_unavailable_reason"] = reason if model is None else ""
        return result
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def save_section(db_path: str | Path | None, project_id: int, section_key: str,
                 heading: str, content: str,
                 citation_ids: list[int] | None = None) -> dict[str, Any]:
    conn = _open(db_path)
    try:
        section_id = writing.save_section(
            conn, project_id, section_key, heading, content, citation_ids)
        return {"ok": True, "section_id": section_id,
                "sections": writing.list_sections(conn, project_id)}
    finally:
        conn.close()


def polish_section(db_path: str | Path | None, project_id: int, section_key: str,
                   content: str, use_model: bool = True) -> dict[str, Any]:
    conn = _open(db_path)
    try:
        result = writing.polish_section(
            conn, project_id, section_key, content,
            model=_model() if use_model else None)
        result["ok"] = True
        return result
    finally:
        conn.close()


def export_project(db_path: str | Path | None,
                   project_id: int) -> dict[str, Any]:
    conn = _open(db_path)
    try:
        data = writing.export_project_markdown(conn, project_id)
        data["ok"] = True
        return data
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()
