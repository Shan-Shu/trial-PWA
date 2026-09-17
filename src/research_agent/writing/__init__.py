"""写作台（合并新增）：体裁/大纲/章节/润色。

来源：``paper_writing_assistant`` 的 ``writing/``，但章节骨架与提示词来自
``packs/skills/writing``，模型不可用时显式降级而非静默产出。
"""
from __future__ import annotations

from .service import (
    create_project,
    default_model,
    delete_project,
    export_project_markdown,
    generate_outline,
    generate_section,
    get_project,
    list_genres,
    list_projects,
    list_sections,
    polish_section,
    save_section,
    writing_pack,
)

__all__ = [
    "create_project",
    "default_model",
    "delete_project",
    "export_project_markdown",
    "generate_outline",
    "generate_section",
    "get_project",
    "list_genres",
    "list_projects",
    "list_sections",
    "polish_section",
    "save_section",
    "writing_pack",
]
