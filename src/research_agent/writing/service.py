"""写作台服务：体裁/大纲/章节/润色。

来源：``paper_writing_assistant`` 的 ``writing/{store.py,writer.py}``。两处改造：

1. **章节骨架与提示词全部来自** ``packs/skills/writing``（来源版本把 7 个中文小节
   写死为 ``DEFAULT_OUTLINE``，换语言/体裁都要改代码）；
2. 写作项目与章节落 ``writing_projects`` / ``writing_sections`` 两张表，
   素材来自既有 ``recommend_materials``（真实知识库），模型不可用时**显式降级**为
   带素材清单的骨架草稿并标记 ``generated_by``，不伪装成模型产物。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from research_agent import packs
from research_agent.config import Settings, settings as default_settings
from research_agent.db import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "writing_pack",
    "list_genres",
    "create_project",
    "list_projects",
    "get_project",
    "delete_project",
    "generate_outline",
    "list_sections",
    "generate_section",
    "save_section",
    "polish_section",
    "export_project_markdown",
]


def _logged(model: Any, prompt: str, node: str, role: str = "") -> Any:
    """带事件日志地调一次模型（替代裸 ``model.invoke``）。"""
    from research_agent.logging import logged_invoke
    return logged_invoke(model, prompt, node=node, role=role or node)


def writing_pack() -> dict[str, Any]:
    """写作技能包内容（体裁、章节骨架、提示词模板）。"""
    data = packs.skill_data("writing")
    if not data:
        packs.warn_once("writing-pack-missing",
                        "未加载 packs/skills/writing；写作台将不可用")
    return data if isinstance(data, dict) else {}


def list_genres() -> list[dict[str, Any]]:
    genres = writing_pack().get("genres") or {}
    default = str(writing_pack().get("default_genre") or "")
    out: list[dict[str, Any]] = []
    if isinstance(genres, dict):
        for key, spec in genres.items():
            if not isinstance(spec, dict):
                continue
            out.append({
                "key": key,
                "label": spec.get("label") or key,
                "language": spec.get("language") or "zh",
                "sections": spec.get("sections") or [],
                "is_default": key == default,
            })
    return out


def _genre(key: str | None) -> tuple[str, dict[str, Any]]:
    genres = writing_pack().get("genres") or {}
    if not isinstance(genres, dict) or not genres:
        raise ValueError("写作技能包未加载（packs/skills/writing）")
    chosen = str(key or writing_pack().get("default_genre") or "")
    if chosen not in genres:
        chosen = next(iter(genres))
    spec = genres.get(chosen)
    return chosen, spec if isinstance(spec, dict) else {}


def _prompt(name: str, default: str = "") -> str:
    prompts = writing_pack().get("prompts") or {}
    return str(prompts.get(name) or default) if isinstance(prompts, dict) else default


def _row_to_project(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["outline"] = json.loads(data.pop("outline_json") or "[]")
    except json.JSONDecodeError:
        data["outline"] = []
    return data


def _row_to_section(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["citation_ids"] = json.loads(data.pop("citation_ids") or "[]")
    except json.JSONDecodeError:
        data["citation_ids"] = []
    return data


# ------------------------------------------------------------------ 项目

def create_project(conn: sqlite3.Connection, title: str, topic: str = "",
                   genre: str | None = None) -> int:
    clean_title = str(title or "").strip()
    if not clean_title:
        raise ValueError("项目标题不能为空")
    key, spec = _genre(genre)
    outline = spec.get("sections") or []
    cur = conn.execute(
        "INSERT INTO writing_projects(title, topic, genre, language, outline_json, "
        "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (clean_title, str(topic or "").strip(), key,
         str(spec.get("language") or "zh"),
         json.dumps(outline, ensure_ascii=False), "draft", utcnow(), utcnow()),
    )
    project_id = int(cur.lastrowid)
    for index, section in enumerate(outline):
        if not isinstance(section, dict):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO writing_sections(project_id, section_key, heading, "
            "content, citation_ids, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (project_id, str(section.get("key") or f"section_{index}"),
             str(section.get("heading") or ""), "", "[]", "draft", utcnow(), utcnow()),
        )
    conn.commit()
    return project_id


def list_projects(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM writing_sections s "
        "  WHERE s.project_id = p.project_id AND LENGTH(TRIM(s.content)) > 0) "
        "  AS filled_sections, "
        " (SELECT COUNT(*) FROM writing_sections s WHERE s.project_id = p.project_id) "
        "  AS total_sections "
        "FROM writing_projects p ORDER BY p.project_id DESC"
    ).fetchall()
    return [_row_to_project(r) for r in rows]


def get_project(conn: sqlite3.Connection,
                project_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM writing_projects WHERE project_id=?", (int(project_id),)
    ).fetchone()
    return _row_to_project(row) if row else None


def delete_project(conn: sqlite3.Connection, project_id: int) -> None:
    conn.execute("DELETE FROM writing_projects WHERE project_id=?", (int(project_id),))
    conn.commit()


def list_sections(conn: sqlite3.Connection,
                  project_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM writing_sections WHERE project_id=? ORDER BY section_id",
        (int(project_id),),
    ).fetchall()
    return [_row_to_section(r) for r in rows]


# ------------------------------------------------------------------ 素材

def _materials(db_path: str | None, topic: str, limit: int = 8) -> list[dict[str, Any]]:
    """取写作素材：从已入库文献中按年份/质量取最近记录。

    注：这里刻意不调用 LLM，也不做语义检索——素材选择属于检索层职责，
    写作层只负责"把可用证据交给模型并强制标注编号"。
    """
    from research_agent.db import connect
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT p.paper_key, p.title, p.abstract, p.venue, p.pub_year, p.doi, "
            "       p.citation_count FROM papers p "
            "LEFT JOIN quality_results q ON q.paper_key = p.paper_key "
            "ORDER BY (q.quality IS NULL), q.quality DESC, p.pub_year DESC "
            "LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _material_digest(materials: list[dict[str, Any]]) -> str:
    if not materials:
        return "（知识库暂无可引用素材）"
    lines = []
    for index, item in enumerate(materials[:8], 1):
        title = str(item.get("title") or "未命名")
        venue = str(item.get("venue") or "")
        year = item.get("pub_year") or ""
        lines.append(f"[{index}] {title} — {venue} {year}".strip())
    return "\n".join(lines)


def _materials_block(materials: list[dict[str, Any]]) -> str:
    if not materials:
        return "（无素材：本稿必须明确标注为无证据推断）"
    lines = []
    for index, item in enumerate(materials[:8], 1):
        abstract = str(item.get("abstract") or "").strip()
        lines.append(f"[{index}] {str(item.get('title') or '').strip()}\n{abstract[:600]}")
    return "\n\n".join(lines)


# ------------------------------------------------------------------ 大纲

def generate_outline(conn: sqlite3.Connection, project_id: int,
                     topic: str = "", model: Any = None) -> dict[str, Any]:
    """生成/刷新大纲。

    无模型时**不静默**：直接使用 pack 定义的章节骨架，并在返回值里标明
    ``generated_by="pack_skeleton"``。
    """
    project = get_project(conn, project_id)
    if not project:
        raise ValueError(f"写作项目不存在: {project_id}")
    _key, spec = _genre(project.get("genre"))
    sections = [s for s in (spec.get("sections") or []) if isinstance(s, dict)]
    generated_by = "pack_skeleton"
    model_error = None

    if model is not None:
        try:
            prompt = _prompt("outline_user").format(
                genre_label=spec.get("label") or project.get("genre") or "",
                topic=topic or project.get("topic") or project.get("title") or "",
                materials_digest=_material_digest(
                    _materials(None, topic or project.get("topic") or "")),
            )
            msg = _logged(model, prompt, "planner")
            raw = getattr(msg, "content", str(msg))
            parsed = _parse_json(raw)
            proposed = parsed.get("sections") if isinstance(parsed, dict) else None
            if isinstance(proposed, list) and proposed:
                sections = [
                    {"key": str(s.get("key") or f"section_{i}"),
                     "heading": str(s.get("heading") or ""),
                     "words": s.get("words")}
                    for i, s in enumerate(proposed) if isinstance(s, dict)
                    and str(s.get("heading") or "").strip()
                ] or sections
                generated_by = "llm"
        except Exception as exc:  # noqa: BLE001 —— 写作大纲失败不应中断
            logger.warning("大纲生成失败，回退 pack 骨架: %s", exc)
            model_error = f"{type(exc).__name__}: {exc}"

    conn.execute(
        "UPDATE writing_projects SET outline_json=?, updated_at=? WHERE project_id=?",
        (json.dumps(sections, ensure_ascii=False), utcnow(), int(project_id)),
    )
    for index, section in enumerate(sections):
        conn.execute(
            "INSERT OR IGNORE INTO writing_sections(project_id, section_key, heading, "
            "content, citation_ids, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (int(project_id), str(section.get("key") or f"section_{index}"),
             str(section.get("heading") or ""), "", "[]", "draft", utcnow(), utcnow()),
        )
    conn.commit()
    return {
        "project_id": int(project_id),
        "sections": sections,
        "generated_by": generated_by,
        "model_error": model_error,
    }


# ------------------------------------------------------------------ 章节

def save_section(conn: sqlite3.Connection, project_id: int, section_key: str,
                 heading: str, content: str,
                 citation_ids: list[int] | None = None) -> int:
    citations = json.dumps(citation_ids or [], ensure_ascii=False)
    row = conn.execute(
        "SELECT section_id FROM writing_sections WHERE project_id=? AND section_key=?",
        (int(project_id), str(section_key)),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE writing_sections SET heading=?, content=?, citation_ids=?, "
            "status='draft', updated_at=? WHERE section_id=?",
            (heading, content, citations, utcnow(), int(row["section_id"])),
        )
        section_id = int(row["section_id"])
    else:
        cur = conn.execute(
            "INSERT INTO writing_sections(project_id, section_key, heading, content, "
            "citation_ids, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (int(project_id), str(section_key), heading, content, citations,
             "draft", utcnow(), utcnow()),
        )
        section_id = int(cur.lastrowid)
    conn.execute("UPDATE writing_projects SET updated_at=? WHERE project_id=?",
                 (utcnow(), int(project_id)))
    conn.commit()
    return section_id


def generate_section(conn: sqlite3.Connection, project_id: int, section_key: str,
                     heading: str, topic: str = "", model: Any = None,
                     db_path: str | None = None,
                     model_reason: str = "") -> dict[str, Any]:
    """生成章节草稿；模型不可用时输出**显式标注**的骨架草稿。

    ``model_reason`` 由调用方传入"为什么没有模型"，会写进正文与返回值，
    让用户能区分"未配置 Key"与"模型构建失败"。
    """
    project = get_project(conn, project_id)
    if not project:
        raise ValueError(f"写作项目不存在: {project_id}")
    _key, spec = _genre(project.get("genre"))
    notes = spec.get("section_notes") or {}
    note = str(notes.get(section_key) or "") if isinstance(notes, dict) else ""
    words = 0
    for section in spec.get("sections") or []:
        if isinstance(section, dict) and str(section.get("key")) == str(section_key):
            words = int(section.get("words") or 0)
    materials = _materials(db_path, topic or project.get("topic") or "")

    if model is not None:
        try:
            prompt = _prompt("section_user").format(
                topic=topic or project.get("topic") or project.get("title") or "",
                heading=heading, note=note or "无特殊要求", words=words or 800,
                materials=_materials_block(materials),
            )
            msg = _logged(model, prompt, "content_builder", "content")
            content = str(getattr(msg, "content", msg) or "").strip()
            if content:
                save_section(conn, project_id, section_key, heading, content,
                             list(range(1, len(materials) + 1)))
                return {"section_key": section_key, "heading": heading,
                        "content": content, "generated_by": "llm",
                        "material_count": len(materials)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("章节生成失败，回退骨架草稿: %s", exc)
            fallback_error = f"{type(exc).__name__}: {exc}"
        else:
            fallback_error = "模型返回空内容"
    else:
        fallback_error = model_reason or "未提供模型"

    skeleton = [
        f"## {heading}",
        "",
        f"> 本节由**骨架草稿**生成（原因：{fallback_error}）。",
        "> 请基于下方素材补全，引用请写成 [编号]，编号必须来自素材清单。",
        "",
    ]
    if note:
        skeleton.extend([f"写作要求：{note}", ""])
    if words:
        skeleton.extend([f"目标字数：约 {words} 字", ""])
    skeleton.append("可用素材：")
    skeleton.append(_material_digest(materials))
    content = "\n".join(skeleton)
    save_section(conn, project_id, section_key, heading, content,
                 list(range(1, len(materials) + 1)))
    return {"section_key": section_key, "heading": heading, "content": content,
            "generated_by": "skeleton_fallback", "model_error": fallback_error,
            "material_count": len(materials)}


def polish_section(conn: sqlite3.Connection, project_id: int, section_key: str,
                   content: str, model: Any = None) -> dict[str, Any]:
    """润色；模型不可用时原样返回并显式标注。"""
    if model is None:
        return {"content": content, "polished": False,
                "reason": "未提供模型"}
    try:
        prompt = _prompt("polish_user", "{content}").format(content=content)
        msg = _logged(model, prompt, "content_builder", "content")
        polished = str(getattr(msg, "content", msg) or "").strip()
        if not polished:
            return {"content": content, "polished": False, "reason": "模型返回空内容"}
        row = conn.execute(
            "SELECT heading, citation_ids FROM writing_sections "
            "WHERE project_id=? AND section_key=?",
            (int(project_id), str(section_key)),
        ).fetchone()
        if row:
            citations = json.loads(row["citation_ids"] or "[]")
            save_section(conn, project_id, section_key, row["heading"], polished,
                         citations)
        return {"content": polished, "polished": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("润色失败: %s", exc)
        return {"content": content, "polished": False,
                "reason": f"{type(exc).__name__}: {exc}"}


def export_project_markdown(conn: sqlite3.Connection,
                            project_id: int) -> dict[str, Any]:
    project = get_project(conn, project_id)
    if not project:
        raise ValueError(f"写作项目不存在: {project_id}")
    sections = list_sections(conn, project_id)
    lines = [f"# {project.get('title') or '未命名项目'}", ""]
    if project.get("topic"):
        lines.extend([f"> 主题：{project['topic']}", ""])
    for section in sections:
        heading = section.get("heading") or section.get("section_key")
        body = str(section.get("content") or "").strip() or "（本节尚未生成）"
        lines.extend([f"## {heading}", "", body, ""])
    return {
        "project_id": int(project_id),
        "title": project.get("title"),
        "filename": f"writing_project_{int(project_id)}.md",
        "mime": "text/markdown; charset=utf-8",
        "content": "\n".join(lines),
        "section_count": len(sections),
    }


def _parse_json(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def default_model(settings: Settings | None = None) -> tuple[Any, str]:
    """构建写作角色模型。

    返回 ``(model, reason)``：

    - 成功 → ``(实例, "")``；
    - 失败 → ``(None, 具体原因)``，例如"缺少 DEEPSEEK_API_KEY"。

    之所以返回原因：界面上的"未提供模型"原本把**未配置 Key**与**构建失败**
    合并成同一句话，用户无法据此判断该去配 Key 还是该去查依赖。
    """
    return default_role_model("content", settings)


def default_role_model(role: str,
                       settings: Settings | None = None) -> tuple[Any, str]:
    """按**节点角色**构建模型，返回 ``(model, reason)``（失败时 model 为 None）。

    与 :func:`default_model` 同一约定。存在的理由：写作链上不止"成段"一个角色，
    知识消费节点也需要模型；此前接口上有 ``consumer_model`` 参数，但**所有写作
    路径都不传**（只有 ``dashboard/app.py`` 传），消费节点于是永远走
    ``offline_fallback``、``confidence=0.0``，机制状态与算子链退化成纯确定性兜底。
    """
    from research_agent.models import build_role_model
    try:
        return build_role_model(role), ""
    except Exception as exc:  # noqa: BLE001 —— 缺 Key 是预期情况
        reason = f"{type(exc).__name__}: {exc}"
        logger.info("角色 %s 的模型不可用: %s", role, reason)
        return None, reason
