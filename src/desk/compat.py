"""形状桥接：让 PWA 的界面代码能直接调用 trial 引擎。

**为什么需要这一层**：两个项目的数据模型不同名也不同形——

======================  ==============================  ==========================
PWA 的概念               PWA 的形状                       trial 引擎的对应
======================  ==============================  ==========================
本体术语 / 关系          `terms[]` / `relations[]`        `ontology_nodes` / `ontology_edges`
知识实体 / 三元组        `entities` / `triples`           `ontology_nodes` / `ontology_edges` + 超边
知识对象                 `sdo`（每篇一个）                 知识抽取产物（节点/边/超边/条件/测量）
质量六维                 authority/completeness/…          A/T/Q 三维 + 元数据完整性
引用导出                 export_markdown/bibtex/citation   `library.export()` / `format_citation()`
图渲染                   terms+relations → vis-network     graph dict → `viz.render_*`
======================  ==============================  ==========================

界面按 PWA 的形状写好了（10 个页面、1340 行），所以**这一层的职责是把引擎的
形状转成界面期望的形状**，而不是反过来改界面。

**刻意不做全知全能**：引擎算不出来的维度就**如实留空**（界面显示 0），
并在对应函数里写明"引擎无此概念"，不编造数据。哪些是真数据、哪些是缺口，
必须一眼可辨。
"""
from __future__ import annotations

from typing import Any, Iterable

__all__ = [
    "INTENT_LABELS",
    "assess_quality",
    "export_markdown",
    "format_bibtex",
    "format_citation",
    "format_experiment_design",
    "graph_from_terms",
    "render_interactive_graph",
    "render_ontology_svg",
]

#: 检索意图的中文名。引擎侧对应 `retrieval/skills.py` 的策略与
#: `study/planner.py` 的 intent 分类；键与引擎产出的 intent 值对齐。
INTENT_LABELS: dict[str, str] = {
    "general": "一般文献检索",
    "literature": "文献综述检索",
    "mechanism": "机制/机理检索",
    "method": "方法学检索",
    "evidence_gap": "证据缺口检索",
    "comparison": "对比研究检索",
    "clinical": "临床研究检索",
    "materials": "材料与表征检索",
    "catalyst": "催化与反应检索",
    "review": "综述与进展检索",
}


# ------------------------------------------------------------------ 质量评分
def assess_quality(record: dict[str, Any], settings: Any = None) -> dict[str, Any]:
    """把引擎的 A/T/Q 评分映射成界面期望的六维明细。

    **只有三维是真数据**（引擎只算这些）：

    - ``authority``   ← 引擎 A（venue 分区 × h 因子 × 引用数）
    - ``timeliness``  ← 引擎 T（年份 × 学科迭代速度）
    - ``completeness`` ← 由引擎 ``check_metadata_completeness`` 的缺失项折算

    ``consistency`` / ``uniqueness`` / ``provenance`` **引擎没有这三个概念**，
    因此不返回（界面 ``.get(..., 0.0)`` 会显示 0）。这是**缺口而不是 0 分**，
    需要时应在引擎侧真正实现，而不是在这里编一个数出来。
    """
    from research_agent.quality.scoring import (check_metadata_completeness,
                                                quality_assess)

    out = quality_assess(record, settings)
    ok, missing = check_metadata_completeness(record)
    # 四个必查项（作者/单位/发表情况/DOI）缺一项扣一分之一
    completeness = 1.0 if ok else max(0.0, 1.0 - len(missing) / 4.0)
    return {
        "authority": float(out.get("authority") or 0.0),
        "timeliness": float(out.get("timeliness") or 0.0),
        "completeness": round(completeness, 3),
        "quality": float(out.get("quality") or 0.0),
        "decision": out.get("decision"),
        "venue_quartile": out.get("venue_quartile"),
        "venue_note": out.get("venue_note"),
        "missing_fields": missing,
    }


# ------------------------------------------------------------------ 引用与导出
def format_citation(record: dict[str, Any], style: str | None = None) -> str:
    """单条引用格式化（引擎支持 GB/T 7714 / APA / ACS / BibTeX / RIS）。"""
    from research_agent.library import citation as cit

    return cit.format_citation(record, style)


def export_markdown(records: Iterable[dict[str, Any]]) -> str:
    """导出为 Markdown 清单（引擎 `library.export(style="markdown")`）。"""
    from research_agent.library import citation as cit

    return cit.export(list(records), "markdown")


def format_bibtex(records: Iterable[dict[str, Any]] | dict[str, Any]) -> str:
    """导出 BibTeX。PWA 是按条目调用（`format_bibtex(record)`），
    这里同时接受单条与多条。"""
    from research_agent.library import citation as cit

    items = [records] if isinstance(records, dict) else list(records)
    return cit.export(items, "bibtex")


# ------------------------------------------------------------------ 本体可视化
def graph_from_terms(terms: list[dict[str, Any]],
                     relations: list[dict[str, Any]],
                     max_nodes: int = 80) -> dict[str, Any]:
    """把界面的 ``terms``/``relations`` 组装成引擎 ``viz`` 要的 graph dict。

    引擎的形状（`ontology/viz.py::export_graph_from_db`）::

        {"nodes": [{"id","type","label","confidence","aliases",...}],
         "edges": [{"id","source","target","label","type","confidence",...}],
         "meta":  {"node_by_type": {type: count}, "edge_by_type": {...}}}

    注意引擎的边用 ``source``/``target`` 而不是 ``from``/``to``——
    照抄 PWA 的 ``from``/``to`` 会让图渲染不出来（vis-network 收不到端点）。
    """
    nodes = list(terms or [])[: max(1, int(max_nodes))]
    by_name = {str(t.get("term") or ""): str(t.get("id")) for t in nodes}
    node_by_type: dict[str, int] = {}
    out_nodes = []
    for term in nodes:
        ntype = str(term.get("term_type") or "Concept")
        node_by_type[ntype] = node_by_type.get(ntype, 0) + 1
        out_nodes.append({
            "id": str(term.get("id")),
            "type": ntype,
            "label": str(term.get("term") or ""),
            "confidence": float(term.get("confidence") or 0.0),
            "aliases": list(term.get("aliases") or []),
            "attributes": {},
            "provenance": [],
        })

    edge_by_type: dict[str, int] = {}
    out_edges = []
    for rel in relations or []:
        source = by_name.get(str(rel.get("subject_term") or ""))
        target = by_name.get(str(rel.get("object_term") or ""))
        if not source or not target:
            continue          # 端点不在可见节点内：丢掉，不画半条边
        etype = str(rel.get("relation") or "related_to")
        edge_by_type[etype] = edge_by_type.get(etype, 0) + 1
        out_edges.append({
            "id": f"e{rel.get('id')}",
            "source": source, "target": target,
            "label": etype, "type": etype,
            "confidence": float(rel.get("confidence") or 0.0),
            "attributes": {}, "provenance": [],
        })

    return {
        "nodes": out_nodes, "edges": out_edges,
        "meta": {"title": "本地动态本体", "node_by_type": node_by_type,
                 "edge_by_type": edge_by_type},
    }


def render_interactive_graph(terms: list[dict[str, Any]],
                             relations: list[dict[str, Any]],
                             max_nodes: int = 80) -> str:
    """交互式本体图（引擎 `viz.render_interactive_html`，vis-network）。"""
    from research_agent.ontology import viz

    return viz.render_interactive_html(graph_from_terms(terms, relations, max_nodes))


def render_ontology_svg(terms: list[dict[str, Any]],
                        relations: list[dict[str, Any]],
                        width: int = 1200) -> str:
    """静态 SVG 本体图（引擎 `viz.render_static_svg`）。"""
    from research_agent.ontology import viz

    return viz.render_static_svg(graph_from_terms(terms, relations, max_nodes=120),
                                 width=width)


# ------------------------------------------------------------------ 实验设计
def format_experiment_design(design: dict[str, Any]) -> str:
    """把实验设计渲染成 Markdown。

    PWA 原版在 `experiment/designer.py` 里自带格式化；那套后端已退役，
    这里改为通用渲染：**引擎的实验设计产物是 `design_context` 五键**
    （机制状态 / 反应基元 / 机会缺口 / 算子候选 / 约束冲突），
    因此按这五键渲染，遇到未知结构就退化为键值列表。
    """
    if not isinstance(design, dict) or not design:
        return "_（无内容）_"

    titles = {
        "mechanism_states": "机制状态",
        "reaction_primitives": "反应基元",
        "opportunity_gaps": "机会缺口",
        "operator_candidates": "算子候选",
        "constraint_conflicts": "约束冲突",
        "goal": "研究目标",
        "title": "标题",
        "summary": "摘要",
    }
    lines: list[str] = []
    for key, value in design.items():
        heading = titles.get(key, key)
        if isinstance(value, (list, tuple)):
            lines.append(f"### {heading}（{len(value)} 项）")
            for item in value[:20]:
                if isinstance(item, dict):
                    label = (item.get("label") or item.get("title")
                             or item.get("name") or item.get("gap_id")
                             or item.get("state_id") or "")
                    extra = []
                    for field in ("semantics", "missing_link", "operator_label",
                                  "innovation_level", "confidence"):
                        if item.get(field) not in (None, "", []):
                            extra.append(f"{field}={item[field]}")
                    chain = item.get("operator_chain") or []
                    if chain:
                        extra.append("算子链=" + " → ".join(
                            str(c.get("operator") or c) for c in chain))
                    lines.append(f"- **{label}**" + (f"：{'；'.join(extra)}" if extra else ""))
                else:
                    lines.append(f"- {item}")
        elif isinstance(value, dict):
            lines.append(f"### {heading}")
            lines.extend(f"- {k}：{v}" for k, v in list(value.items())[:20])
        else:
            lines.append(f"**{heading}**：{value}")
        lines.append("")
    return "\n".join(lines).strip()
