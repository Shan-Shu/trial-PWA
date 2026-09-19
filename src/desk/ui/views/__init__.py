from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import (
    dashboard,
    experiment,
    knowledge,
    library,
    ontology,
    retrieval,
    review,
    search,
    system,
    writing,
)


@dataclass(frozen=True)
class PageSpec:
    key: str
    title: str
    icon: str
    render: Callable


PAGES = {
    "总览": PageSpec("总览", "总览", "📊", dashboard.render),
    "智能检索": PageSpec("智能检索", "智能检索", "🔎", search.render),
    "知识检索": PageSpec("知识检索", "知识检索", "🧭", retrieval.render),
    "文献库": PageSpec("文献库", "文献库", "📚", library.render),
    "知识抽取": PageSpec("知识抽取", "知识抽取", "🧠", knowledge.render),
    "动态本体": PageSpec("动态本体", "动态本体", "🕸️", ontology.render),
    "实验工作台": PageSpec("实验工作台", "实验工作台", "🧪", experiment.render),
    "写作台": PageSpec("写作台", "写作台", "✍️", writing.render),
    "审核中心": PageSpec("审核中心", "审核中心", "✅", review.render),
    "系统状态": PageSpec("系统状态", "系统状态", "🖥️", system.render),
}

DEFAULT_PAGE = "智能检索"