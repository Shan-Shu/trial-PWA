"""文献库层：侧车表（标签/文件夹/收藏）、引用格式化与批量作业。

合并自 ``paper_writing_assistant`` 的 ``library/`` 与 ``ui/views/library.py`` 能力，
但主键统一为 ``papers.paper_key``（TEXT），并补齐外键级联与索引。
"""
from __future__ import annotations

from .citation import (
    available_styles,
    default_style,
    export,
    format_citation,
    render_many,
    reset_cache,
    venue_label,
)
from .store import LibraryStore

__all__ = [
    "LibraryStore",
    "available_styles",
    "default_style",
    "export",
    "format_citation",
    "render_many",
    "reset_cache",
    "venue_label",
]
