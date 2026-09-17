"""动态本体图谱可视化。

产物：
1. 交互式 HTML（默认）：内嵌本地 vis-network，支持拖拽/缩放/悬停详情/
   按类型显示隐藏/搜索高亮，单文件、可离线双击打开；
2. 静态 SVG：同目录 .svg 缩略预览。

数据源：
- demo（默认）：内置 4 篇论文的合成知识图谱，用于演示；
- db：导出 data/research_agent.db 的 ontology_* 表。

用法：
    python -m research_agent.ontology.viz --dataset demo
    python -m research_agent.ontology.viz --dataset db [--db path]
    python -m research_agent.ontology.viz --dataset demo --open
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import webbrowser
from pathlib import Path
from typing import Any

from research_agent.config import PROJECT_ROOT

TYPE_COLORS: dict[str, str] = {
    "Method": "#4e79a7", "Dataset": "#f28e2b", "Metric": "#e15759",
    "Task": "#76b7b2", "Concept": "#59a14f", "Tool": "#edc948",
    "Person": "#b07aa1", "Organization": "#ff9da7", "Material": "#9c755f",
    "Disease": "#d37295", "Drug": "#8cd17d", "Gene": "#a0cbe8",
    "Event": "#bab0ab", "Experiment": "#f7b3ad",
}
_FALLBACK = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
             "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ab"]
VENDOR_JS = PROJECT_ROOT / "assets" / "vendor" / "vis-network.min.js"


def color_for(node_type: str) -> str:
    if node_type in TYPE_COLORS:
        return TYPE_COLORS[node_type]
    h = int(hashlib.md5(node_type.encode("utf-8")).hexdigest()[:6], 16)
    return _FALLBACK[h % len(_FALLBACK)]


# ====================================================================== 导出
def export_graph_from_db(db_path: Path | str) -> dict[str, Any]:
    """把本地本体库导出为 {nodes, edges, meta}。"""
    from research_agent.db import connect
    from research_agent.ontology.store import graph_summary, init_ontology

    conn: sqlite3.Connection = connect(db_path)
    try:
        init_ontology(conn)
        nodes = []
        for r in conn.execute(
                "SELECT node_id, node_type, name, confidence, aliases, "
                "attributes, provenance FROM ontology_nodes"):
            nodes.append({
                "id": str(r["node_id"]), "type": r["node_type"],
                "label": r["name"],
                "confidence": round(float(r["confidence"] or 0.5), 3),
                "aliases": json.loads(r["aliases"] or "[]"),
                "attributes": json.loads(r["attributes"] or "{}"),
                "provenance": json.loads(r["provenance"] or "[]"),
            })
        edges = []
        for r in conn.execute(
                "SELECT edge_id, relation_type, source_node, target_node, "
                "confidence, attributes, provenance FROM ontology_edges"):
            attrs = json.loads(r["attributes"] or "{}")
            label = r["relation_type"]
            if attrs.get("predicate"):
                label = f"{label}: {attrs['predicate']}"
            edges.append({
                "id": str(r["edge_id"]), "source": str(r["source_node"]),
                "target": str(r["target_node"]), "label": label,
                "type": r["relation_type"],
                "confidence": round(float(r["confidence"] or 0.5), 3),
                "attributes": attrs,
                "provenance": json.loads(r["provenance"] or "[]"),
            })
        papers = conn.execute("SELECT COUNT(*) AS c FROM papers").fetchone()["c"]
        summary = graph_summary(conn)
        meta = {
            "title": "本地动态本体库",
            "db": str(db_path), "papers": papers,
            "schema_version": summary["schema_version"],
            "instance_version": summary["instance_version"],
            "node_by_type": summary["node_by_type"],
            "edge_by_type": summary["edge_by_type"],
        }
    finally:
        conn.close()
    return {"nodes": nodes, "edges": edges, "meta": meta}


# ====================================================================== demo 数据
def _node(nid: int, ntype: str, name: str, conf: float,
          attrs: dict | None = None, aliases: list[str] | None = None,
          prov: list[dict] | None = None) -> dict:
    return {"id": str(nid), "type": ntype, "label": name, "confidence": conf,
            "aliases": aliases or [], "attributes": attrs or {},
            "provenance": prov or []}


def build_demo_graph() -> dict[str, Any]:
    """演示数据集：4 篇论文 × 知识提取后合并的本体（约 27 节点/24 边）。"""
    p1 = [{"paper": "P1 · RAG for Knowledge-Intensive NLP (arXiv 2020)",
           "evidence": "RAG 结合参数记忆与检索模块并端到端训练。"}]
    p2 = [{"paper": "P2 · Semi-Supervised Classification with GNN (ICLR 2017)",
           "evidence": "GCN 在 Cora 上做半监督节点分类并刷新 SOTA。"}]
    p3 = [{"paper": "P3 · ReAct: Synergizing Reasoning and Acting (ICLR 2023)",
           "evidence": "ReAct 让 LLM 交替推理与调用工具，HotpotQA 上超过基线。"}]
    p4 = [{"paper": "P4 · LoRA: Low-Rank Adaptation (ICLR 2022)",
           "evidence": "LoRA 冻结预训练权重、以低秩增量微调大模型。"}]

    nodes = [
        _node(1, "Method", "Retrieval-Augmented Generation", 0.93,
              {"paradigm": "retrieval + generation"}, ["RAG"], p1),
        _node(2, "Method", "Graph Convolutional Network", 0.90,
              {"layer": "spectral"}, ["GCN", "GNN"], p2),
        _node(3, "Method", "ReAct", 0.88, {"strategy": "reason + act"},
              ["Reasoning and Acting"], p3),
        _node(4, "Method", "Low-Rank Adaptation", 0.86, {"rank": 8}, ["LoRA"], p4),
        _node(5, "Tool", "LLaMA-2", 0.82, {"params": "7B/13B/70B"}, [], p4),
        _node(6, "Tool", "BERT", 0.80, {"params": "110M"}, [], p2),
        _node(7, "Dataset", "Natural Questions", 0.90, {"lang": "English"},
              ["NQ"], p1),
        _node(8, "Dataset", "Cora", 0.84, {"nodes": 2708}, [], p2),
        _node(9, "Dataset", "HotpotQA", 0.85, {"multihop": True}, [], p3),
        _node(10, "Dataset", "GLUE", 0.80, {"tasks": 9}, [], p1),
        _node(11, "Metric", "Exact Match", 0.86, {}, ["EM"], p1),
        _node(12, "Metric", "Accuracy", 0.82, {}, [], p2),
        _node(13, "Metric", "F1", 0.84, {}, [], p3),
        _node(14, "Task", "Open-domain QA", 0.88, {}, [], p1),
        _node(15, "Task", "Semi-supervised node classification", 0.86, {}, [], p2),
        _node(16, "Task", "Tool-augmented reasoning", 0.85, {}, [], p3),
        _node(17, "Task", "Parameter-efficient fine-tuning", 0.84, {}, [], p4),
        _node(18, "Concept", "Knowledge-intensive NLP", 0.90,
              {"requires": "external knowledge"}, [], p1),
        _node(19, "Concept", "Low-rank approximation", 0.80, {}, [], p4),
        _node(20, "Person", "Patrick Lewis", 0.75, {}, [], p1),
        _node(21, "Person", "Thomas Kipf", 0.78, {}, [], p2),
        _node(22, "Person", "Shunyu Yao", 0.76, {}, [], p3),
        _node(23, "Organization", "Meta AI", 0.85, {}, [], p1),
        _node(24, "Organization", "Stanford University", 0.82, {}, [], p3),
        _node(25, "Event", "Evaluate RAG on Natural Questions", 0.91,
              {"time": "2020"}, [], p1),
        _node(26, "Event", "GCN beats prior on Cora", 0.87,
              {"time": "2017"}, [], p2),
        _node(27, "Event", "ReAct tool-use on HotpotQA", 0.88,
              {"time": "2023"}, [], p3),
    ]
    raw_edges = [
        ("Retrieval-Augmented Generation", "uses", "Natural Questions", 0.92, p1),
        ("Retrieval-Augmented Generation", "evaluates", "Exact Match", 0.90, p1),
        ("Retrieval-Augmented Generation", "solves", "Open-domain QA", 0.91, p1),
        ("Retrieval-Augmented Generation", "part_of", "Knowledge-intensive NLP", 0.89, p1),
        ("Retrieval-Augmented Generation", "authored_by", "Patrick Lewis", 0.88, p1),
        ("Retrieval-Augmented Generation", "authored_by", "Meta AI", 0.85, p1),
        ("Patrick Lewis", "affiliated_with", "Meta AI", 0.82, p1),
        ("Evaluate RAG on Natural Questions", "involves",
         "Retrieval-Augmented Generation", 0.90, p1),
        ("Evaluate RAG on Natural Questions", "involves", "Natural Questions", 0.90, p1),
        ("Open-domain QA", "evaluates", "Natural Questions", 0.87, p1),
        ("Graph Convolutional Network", "uses", "Cora", 0.90, p2),
        ("Graph Convolutional Network", "evaluates", "Accuracy", 0.88, p2),
        ("Graph Convolutional Network", "solves",
         "Semi-supervised node classification", 0.89, p2),
        ("Graph Convolutional Network", "authored_by", "Thomas Kipf", 0.86, p2),
        ("GCN beats prior on Cora", "involves", "Graph Convolutional Network", 0.88, p2),
        ("ReAct", "uses", "HotpotQA", 0.89, p3),
        ("ReAct", "evaluates", "F1", 0.87, p3),
        ("ReAct", "solves", "Tool-augmented reasoning", 0.90, p3),
        ("ReAct", "authored_by", "Shunyu Yao", 0.87, p3),
        ("Shunyu Yao", "affiliated_with", "Stanford University", 0.84, p3),
        ("ReAct tool-use on HotpotQA", "involves", "ReAct", 0.89, p3),
        ("Low-Rank Adaptation", "based_on", "Low-rank approximation", 0.85, p4),
        ("Low-Rank Adaptation", "uses", "LLaMA-2", 0.86, p4),
        ("Low-Rank Adaptation", "solves",
         "Parameter-efficient fine-tuning", 0.90, p4),
    ]
    name2id = {n["label"]: n["id"] for n in nodes}
    edges = []
    for i, (src, rel, tgt, conf, prov) in enumerate(raw_edges, start=1):
        if src in name2id and tgt in name2id:
            edges.append({
                "id": f"e{i}", "source": name2id[src], "target": name2id[tgt],
                "label": rel, "type": rel, "confidence": conf,
                "attributes": {}, "provenance": prov,
            })
    by_type: dict[str, int] = {}
    for n in nodes:
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
    rel_count: dict[str, int] = {}
    for e in edges:
        rel_count[e["type"]] = rel_count.get(e["type"], 0) + 1
    return {
        "nodes": nodes, "edges": edges,
        "meta": {"title": "演示数据集（4 篇论文 · 知识合并）", "db": "(demo)",
                 "papers": 4, "schema_version": 1, "instance_version": 4,
                 "node_by_type": by_type, "edge_by_type": rel_count},
    }


# ====================================================================== HTML
def _json_embed(data: dict) -> str:
    """JSON 内嵌 <script>：把 </ 转义为 <\\/（合法 JSON，防提前闭合）。"""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@ · 动态本体图谱</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body { font: 14px/1.5 "Segoe UI", "Microsoft YaHei", sans-serif;
       background: #10141c; color: #e6e9ef; overflow: hidden; }
#app { display: flex; flex-direction: column; height: 100vh; }
header { padding: 10px 16px; background: #171c27; border-bottom: 1px solid #2a3142;
         display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
h1 { font-size: 16px; margin: 0; white-space: nowrap; }
.stats { color: #9aa4b8; font-size: 12px; }
.toolbar { margin-left: auto; display: flex; gap: 8px; align-items: center; }
input[type=search] { background: #0f1420; color: #e6e9ef; border: 1px solid #343d52;
                     border-radius: 6px; padding: 5px 10px; width: 220px; }
button { cursor: pointer; border-radius: 6px; border: 1px solid #343d52;
         background: #202838; color: #e6e9ef; padding: 5px 10px; }
button:hover { background: #2a3550; }
.legend { padding: 8px 16px; border-bottom: 1px solid #2a3142; display: flex;
          flex-wrap: wrap; gap: 6px; align-items: center; }
.legend .hint { color: #9aa4b8; font-size: 12px; margin-right: 4px; }
.chip { font-size: 12px; display: inline-flex; align-items: center; gap: 5px;
        padding: 3px 9px; border: 1px solid var(--c); color: #dfe5f0;
        background: color-mix(in srgb, var(--c) 18%, transparent); }
.chip.off { opacity: .25; }
#graph { flex: 1; background: radial-gradient(circle at 50% 40%, #1a2130, #10141c 75%); }
.vis-tooltip { position: absolute; background: #0d1220; border: 1px solid #3a4560;
               border-radius: 8px; padding: 8px 10px; font-size: 12px;
               max-width: 360px; box-shadow: 0 6px 18px rgba(0,0,0,.45); }
.vis-tooltip b { color: #fff; }
.vis-tooltip .t { color: #8fa1c3; font-size: 11px; }
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>动态本体图谱 · @@TITLE@@</h1>
    <span class="stats">@@STATS@@</span>
    <div class="toolbar">
      <input type="search" id="search" placeholder="搜索节点 / 别名…">
      <button id="physics">暂停布局</button>
      <button id="reset">复位视角</button>
    </div>
  </header>
  <div class="legend"><span class="hint">点击类型可显示/隐藏：</span>@@LEGEND@@</div>
  <div id="graph"></div>
</div>
<script type="application/json" id="graph-data">@@DATA@@</script>
@@CDN@@
<script>
@@VENDOR@@
</script>
<script>
(function () {
  var data = JSON.parse(document.getElementById('graph-data').textContent);
  var COLOR = @@COLORS@@;
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; });
  }
  function nodeTip(n) {
    var a = Object.keys(n.attributes || {}).map(function (k) {
      return esc(k + '=' + JSON.stringify(n.attributes[k])); }).join(', ');
    var p = (n.provenance || []).slice(0, 2).map(function (x) {
      return '<div class="t">' + esc(x.paper) + '</div>' + esc(x.evidence || ''); }).join('');
    return '<b>' + esc(n.label) + '</b> <span class="t">[' + esc(n.type) +
      ' · conf ' + (n.confidence || 0).toFixed(2) + ']</span><br>' +
      ((n.aliases && n.aliases.length) ? '别名: ' + esc(n.aliases.join(', ')) + '<br>' : '') +
      (a ? '属性: ' + a + '<br>' : '') + (p ? '来源:<br>' + p : '');
  }
  function edgeTip(e) {
    var p = (e.provenance || []).slice(0, 1).map(function (x) {
      return esc(x.paper) + (x.evidence ? ': ' + esc(x.evidence) : ''); }).join('');
    return '<b>' + esc(e.label) + '</b> · conf ' + (e.confidence || 0).toFixed(2) +
      (p ? '<br><span class="t">' + p + '</span>' : '');
  }
  var nodes = new vis.DataSet(data.nodes.map(function (n) {
    return { id: n.id, label: n.label, group: n.type,
             color: { background: COLOR[n.type] || '#86bcb6', border: '#ffffff33' },
             value: Math.max(6, (n.confidence || 0.5) * 40), title: nodeTip(n) };
  }));
  var edges = new vis.DataSet(data.edges.map(function (e) {
    return { id: e.id, from: e.source, to: e.target, label: e.label,
             color: { color: '#7b8aa8', opacity: 0.55 },
             width: Math.max(0.6, (e.confidence || 0.5) * 2.2),
             arrows: 'to', font: { size: 9, color: '#a7b2c6', strokeWidth: 0 },
             title: edgeTip(e) };
  }));
  var groups = {};
  data.nodes.forEach(function (n) { if (!groups[n.type]) { groups[n.type] = {}; } });
  var network = new vis.Network(document.getElementById('graph'),
      { nodes: nodes, edges: edges }, {
    groups: groups,
    physics: { stabilization: true, barnesHut: { gravitationalConstant: -2800,
      springLength: 130, springConstant: 0.045, damping: 0.6 } },
    interaction: { hover: true, tooltipDelay: 120, hideEdgesOnDrag: true },
    nodes: { shape: 'dot', size: 16,
             font: { color: '#dbe2ee', size: 13,
                     face: 'Segoe UI, Microsoft YaHei, sans-serif' } },
    edges: { smooth: { type: 'dynamic', roundness: 0.4 } }
  });
  network.once('stabilizationIterationsDone', function () {
    network.setOptions({ physics: { stabilization: false } });
  });
  var on = {};
  data.nodes.forEach(function (n) { on[n.type] = true; });
  document.querySelectorAll('.chip').forEach(function (chip) {
    chip.addEventListener('click', function () {
      var t = chip.dataset.type;
      on[t] = !on[t];
      chip.classList.toggle('off', !on[t]);
      nodes.forEach(function (n) {
        var visible = on[n.group] === true;
        if (n.hidden !== !visible) { nodes.update({ id: n.id, hidden: !visible }); }
      });
    });
  });
  document.getElementById('search').addEventListener('input', function () {
    var v = this.value.trim().toLowerCase();
    var hitIds = [];
    nodes.forEach(function (n) {
      var hit = !v || n.label.toLowerCase().indexOf(v) >= 0 ||
        (n.aliases || []).some(function (a) { return a.toLowerCase().indexOf(v) >= 0; });
      if (hit && on[n.group]) {
        nodes.update({ id: n.id, hidden: false });
        hitIds.push(n.id);
      }
    });
    network.selectNodes(hitIds);
  });
  document.getElementById('physics').onclick = function () {
    var cur = network.physics.enabled;
    network.setOptions({ physics: { enabled: !cur } });
    this.textContent = cur ? '继续布局' : '暂停布局';
  };
  document.getElementById('reset').onclick = function () {
    network.fit({ animation: true });
  };
})();
</script>
</body>
</html>
"""


def render_interactive_html(graph: dict[str, Any]) -> str:
    vendor = _load_vendor_js()
    cdn = "" if vendor else (
        '<script src="https://cdn.jsdelivr.net/npm/vis-network@9.1.9/'
        'standalone/umd/vis-network.min.js"></script>')
    types = sorted({n["type"] for n in graph["nodes"]})
    by_type = graph["meta"].get("node_by_type") or {}
    legend = "".join(
        f'<button class="chip" data-type="{t}" style="--c:{color_for(t)}">'
        f'{t} <span>{by_type.get(t, 0)}</span></button>' for t in types)
    m = graph["meta"]
    stats = (f"{len(graph['nodes'])} 节点 · {len(graph['edges'])} 关系 · "
             f"{len(types)} 类型 · 文献 {m.get('papers', '-')} 篇 · "
             f"schema v{m.get('schema_version', 1)} · instance v{m.get('instance_version', 0)}")
    html = (_HTML.replace("@@TITLE@@", str(m.get("title", "")))
            .replace("@@STATS@@", stats)
            .replace("@@LEGEND@@", legend)
            .replace("@@DATA@@", _json_embed(graph))
            .replace("@@CDN@@", cdn)
            .replace("@@VENDOR@@", vendor)
            .replace("@@COLORS@@", json.dumps(
                {t: color_for(t) for t in types}, ensure_ascii=False)))
    return html


def _load_vendor_js() -> str:
    if VENDOR_JS.exists():
        return VENDOR_JS.read_text(encoding="utf-8")
    return ""


# ====================================================================== SVG
def _xml(s: Any) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_static_svg(graph: dict[str, Any], width: int = 1200,
                      height: int = 800) -> str:
    """圆形布局静态 SVG：节点按类型着色、大小随置信度，悬停看标题。"""
    nodes, edges = graph["nodes"], graph["edges"]
    cx, cy = width / 2, height / 2
    rmax = min(width, height) / 2 - 120
    pos: dict[str, tuple[float, float]] = {}
    n = max(1, len(nodes))
    for i, node in enumerate(nodes):
        ang = 2 * math.pi * i / n - math.pi / 2
        r = rmax * (0.5 + 0.5 * float(node.get("confidence") or 0.5))
        pos[node["id"]] = (cx + r * math.cos(ang), cy + r * math.sin(ang))
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
             f'height="{height}" viewBox="0 0 {width} {height}" '
             'font-family="Segoe UI, Microsoft YaHei, sans-serif">',
             '<defs><marker id="arr" viewBox="0 0 10 10" refX="18" refY="5" '
             'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
             '<path d="M0,0L10,5L0,10z" fill="#8fa1c3" opacity="0.8"/>'
             '</marker></defs>',
             f'<rect width="{width}" height="{height}" fill="#10141c"/>']
    for e in edges:
        x1, y1 = pos[e["source"]]
        x2, y2 = pos[e["target"]]
        parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="#7b8aa8" stroke-opacity="0.5" '
            f'stroke-width="{max(0.6, float(e.get("confidence") or 0.5) * 2):.1f}" '
            'marker-end="url(#arr)"/>')
    for node in nodes:
        x, y = pos[node["id"]]
        rad = 7 + 13 * float(node.get("confidence") or 0.5)
        c = color_for(node["type"])
        tip = _xml(f'{node["label"]} [{node["type"]}] '
                   f'conf={float(node.get("confidence") or 0.5):.2f}')
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rad:.1f}" '
                     f'fill="{c}" stroke="#ffffff55" stroke-width="1">'
                     f'<title>{tip}</title></circle>')
        parts.append(
            f'<text x="{x:.1f}" y="{y + rad + 14:.1f}" text-anchor="middle" '
            f'fill="#dfe6f2" font-size="11">{_xml(node["label"])}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


# ====================================================================== CLI
def save_artifacts(graph: dict[str, Any], out_html: Path) -> dict[str, Path]:
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(render_interactive_html(graph), encoding="utf-8")
    svg = out_html.with_suffix(".svg")
    svg.write_text(render_static_svg(graph), encoding="utf-8")
    return {"html": out_html.resolve(), "svg": svg.resolve()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="动态本体图谱可视化")
    ap.add_argument("--dataset", choices=["demo", "db"], default="demo",
                    help="demo=内置演示数据（默认）；db=导出本地本体库")
    ap.add_argument("--db", default=None,
                    help="本体库路径（dataset=db 时有效，默认 data/research_agent.db）")
    ap.add_argument("--output", default=None,
                    help="输出 HTML 路径（默认 output/ontology_graph_demo|db.html）")
    ap.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")
    args = ap.parse_args(argv)
    if args.dataset == "demo":
        graph, tag = build_demo_graph(), "demo"
    else:
        db = Path(args.db) if args.db else PROJECT_ROOT / "data" / "research_agent.db"
        if not db.exists():
            print(f"[错误] 数据库不存在: {db}", file=sys.stderr)
            return 1
        graph, tag = export_graph_from_db(db), "db"
    out = (Path(args.output) if args.output
           else PROJECT_ROOT / "output" / f"ontology_graph_{tag}.html")
    paths = save_artifacts(graph, out)
    print(f"图谱: {len(graph['nodes'])} 节点 / {len(graph['edges'])} 关系")
    print(f"HTML: {paths['html']}")
    print(f"SVG : {paths['svg']}")
    if args.open:
        webbrowser.open(paths["html"].as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
