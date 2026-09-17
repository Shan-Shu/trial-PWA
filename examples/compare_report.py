"""生成「旧提示词本体 vs 新提示词本体」的结构化对比与可视化页面。

输出到 output/compare/：
- index_compare.html  指标对比页（内联 SVG 条形图 + 表格）
- old_graph.html      旧库本体交互图谱（vis-network）
- new_graph.html      新库本体交互图谱（vis-network）
- compare_metrics.json
"""
from __future__ import annotations

import json
import shutil
from collections import Counter, deque
from pathlib import Path

from research_agent.db import connect
from research_agent.ontology.store import seeded_relation_types
from research_agent.packs import relation_lexicon

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "compare"
OLD_DB = ROOT / "data" / "research_agent.db"
NEW_DB = ROOT / "data" / "ontology_v02.db"

# v0.4.2 起 SEED_RELATION_TYPES / RELATION_SYNONYMS 外置到 packs
CANONICAL_REL = {k for k, _ in seeded_relation_types()} | set(
    relation_lexicon()["synonyms"].values())
CANONICAL_REL.add("involves")

PALETTE = ["#4f8cff", "#2ecc8f", "#f5a623", "#ff5a6e", "#9b6bff", "#2dd4bf",
           "#f472b6", "#a3e635", "#60a5fa", "#fb923c", "#c084fc", "#34d399"]


def type_color(t: str) -> str:
    h = 0
    for ch in str(t):
        h = (h * 31 + ord(ch)) % (2 ** 32)
    return PALETTE[h % len(PALETTE)]


def load(db: Path) -> dict:
    c = connect(db)
    nodes = [dict(r) for r in c.execute(
        "SELECT node_id, node_type, name, confidence, provenance FROM ontology_nodes")]
    edges = [dict(r) for r in c.execute(
        "SELECT edge_id, relation_type, source_node, target_node FROM ontology_edges")]
    runs = c.execute("SELECT COUNT(DISTINCT paper_key) FROM ontology_runs"
                     ).fetchone()[0]
    papers = c.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    types = c.execute("SELECT COUNT(*) FROM ontology_type_registry").fetchone()[0]
    c.close()

    ids = {n["node_id"] for n in nodes}
    adj = {i: [] for i in ids}
    deg = Counter()
    rel_counter = Counter()
    for e in edges:
        s, t = e["source_node"], e["target_node"]
        rel_counter[e["relation_type"]] += 1
        deg[s] += 1
        deg[t] += 1
        if s in adj and t in adj:
            adj[s].append(t)
            adj[t].append(s)

    # 连通分量
    seen = set()
    comps = []
    for i in ids:
        if i in seen:
            continue
        q = deque([i])
        seen.add(i)
        sz = 0
        while q:
            u = q.popleft()
            sz += 1
            for v in adj[u]:
                if v not in seen:
                    seen.add(v)
                    q.append(v)
        comps.append(sz)

    isolated = sum(1 for i in ids if deg[i] == 0)
    multi_paper = 0
    for n in nodes:
        try:
            prov = {p.get("paper") for p in json.loads(n["provenance"] or "[]")}
        except Exception:
            prov = set()
        if len(prov) >= 2:
            multi_paper += 1
    node_type_count = len({n["node_type"] for n in nodes})
    non_canonical = {rt: v for rt, v in rel_counter.items() if rt not in CANONICAL_REL}
    return {
        "papers": papers, "runs": runs, "types_registry": types,
        "nodes": len(nodes), "edges": len(edges),
        "node_types": node_type_count,
        "relation_types": len(rel_counter),
        "non_canonical_relations": non_canonical,
        "isolated": isolated,
        "isolated_ratio": round(isolated / len(ids), 4) if ids else None,
        "components": len(comps),
        "largest_comp_ratio": round(max(comps) / len(ids), 4) if ids else None,
        "multi_paper_nodes": multi_paper,
        "multi_paper_ratio": round(multi_paper / len(ids), 4) if ids else None,
        "avg_degree": round(sum(deg.values()) / len(ids), 3) if ids else None,
        "per_paper_nodes": round(len(nodes) / runs, 2) if runs else None,
        "per_paper_edges": round(len(edges) / runs, 2) if runs else None,
        "top_node_types": Counter(n["node_type"] for n in nodes).most_common(10),
        "top_relations": rel_counter.most_common(12),
    }


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_graph_page(filename: str, title: str, db: Path) -> None:
    c = connect(db)
    nodes = [dict(r) for r in c.execute(
        "SELECT node_id, node_type, name, confidence FROM ontology_nodes")]
    edges = [dict(r) for r in c.execute(
        "SELECT edge_id, relation_type, source_node, target_node FROM ontology_edges")]
    c.close()
    data = {
        "nodes": [{"id": n["node_id"], "label": n["name"], "type": n["node_type"],
                   "conf": round(float(n["confidence"] or 0), 3)} for n in nodes],
        "edges": [{"id": e["edge_id"], "from": e["source_node"], "to": e["target_node"],
                   "type": e["relation_type"]} for e in edges],
    }
    html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>__TITLE__</title>
<style>html,body{margin:0;height:100%;background:#0f1420;color:#dbe4f5;
font:14px 'Segoe UI','Microsoft YaHei',sans-serif}
#graph{width:100%;height:92vh}.bar{background:#1d2638;border-bottom:1px solid #2a3550;
padding:6px 12px;color:#8aa0c0}</style></head>
<body><div class="bar">__TITLE__ · 节点 __NN__ · 边 __EE__（可拖拽/缩放，点节点无面板）</div>
<div id="graph"></div>
<script src="./vendor/vis-network.min.js"></script>
<script>
const DATA = __DATA__;
const PAL=['#4f8cff','#2ecc8f','#f5a623','#ff5a6e','#9b6bff','#2dd4bf','#f472b6','#a3e635'];
function col(t){let h=0;for(const ch of String(t))h=(h*31+ch.charCodeAt(0))>>>0;return PAL[h%PAL.length]}
const nodes=new vis.DataSet(DATA.nodes.map(n=>({id:n.id,label:n.label.length>22?n.label.slice(0,21)+'…':n.label,
  value:5+Math.round(n.conf*18),color:{background:col(n.type)},borderWidth:1,
  title:n.type+'<br>'+esc(n.label)+'<br>conf '+n.conf})));
const edges=new vis.DataSet(DATA.edges.map(e=>({id:e.id,from:e.from,to:e.to,label:e.type,
  arrows:{to:{enabled:true,scaleFactor:0.4}},font:{size:9,color:'#8aa0c0'}})));
function esc(s){return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
new vis.Network(document.getElementById('graph'),{nodes,edges},
 {physics:{stabilization:{iterations:300}},nodes:{shape:'dot',font:{color:'#dbe4f5',size:12}},
  edges:{color:'#4c5a78',smooth:{type:'dynamic'}},interaction:{hover:true}});
</script></body></html>"""
    html = html.replace("__TITLE__", title).replace("__NN__", str(len(data["nodes"]))).replace(
        "__EE__", str(len(data["edges"]))).replace(
        "__DATA__", json.dumps(data, ensure_ascii=False))
    (OUT / filename).write_text(html, encoding="utf-8")


def bar(name: str, old: float | None, new: float | None, note: str = "",
        invert: bool = False) -> str:
    mx = max([v for v in (old, new) if v is not None] + [1.0])
    def w(v):
        return f'<div class="bw"><i style="width:{100*v/mx:.0f}%"></i></div>' if v is not None \
            else '<div class="bw off"></div>'
    good = (old or 0) < (new or 0) if invert else (old or 0) > (new or 0)
    return (f'<tr><td>{name}</td><td>{w(old)}<b>{old}</b></td><td>{w(new)}<b>{new}</b></td>'
            f'<td class="note">{note}</td></tr>')


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "vendor").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "assets" / "vendor" / "vis-network.min.js",
                OUT / "vendor" / "vis-network.min.js")
    old = load(OLD_DB)
    new = load(NEW_DB)
    (OUT / "compare_metrics.json").write_text(
        json.dumps({"old": old, "new": new}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    write_graph_page("old_graph.html", "旧提示词 · research_agent.db", OLD_DB)
    write_graph_page("new_graph.html", "新提示词 · ontology_v02.db", NEW_DB)

    rows = [
        bar("处理文献(篇)", old["runs"], new["runs"], "已进入知识提取的文献数"),
        bar("本体节点", old["nodes"], new["nodes"], "归一化去重后的节点总数"),
        bar("关系边", old["edges"], new["edges"], ""),
        bar("孤立节点率", old["isolated_ratio"], new["isolated_ratio"], "无任何关系的节点占比（越低越好）", invert=True),
        bar("连通分量数", old["components"], new["components"], "断裂子图越少越好（越低越好）", invert=True),
        bar("最大连通分量占比", old["largest_comp_ratio"], new["largest_comp_ratio"], "单团聚集度（越高越好）"),
        bar("跨文献共享节点率", old["multi_paper_ratio"], new["multi_paper_ratio"], "同一实体被≥2篇文献支持（越高越好）"),
        bar("平均度", old["avg_degree"], new["avg_degree"], "每个节点的平均连接数（越高越好）"),
        bar("单篇节点产出", old["per_paper_nodes"], new["per_paper_nodes"], "含跨文献合并，新提示词会偏低"),
        bar("单篇边产出", old["per_paper_edges"], new["per_paper_edges"], ""),
        bar("不同关系类型数", old["relation_types"], new["relation_types"], "受控词表下应更收敛（越低越好）", invert=True),
        bar("非受控关系边数", sum(old["non_canonical_relations"].values()),
            sum(new["non_canonical_relations"].values()), "未落在受控词表的关系边（越低越好）", invert=True),
    ]
    def tops(tag, m):
        if tag == "node":
            return "".join(f"<div class='mini2'><span>{esc(k)}</span><b>{v}</b></div>"
                           for k, v in m["top_node_types"])
        return "".join(f"<div class='mini2'><span>{esc(k)}</span><b>{v}</b></div>"
                       for k, v in m["top_relations"])
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>提示词对比 · 旧 vs 新</title>
<style>body{{margin:0;background:#0f1420;color:#dbe4f5;font:14px 'Segoe UI','Microsoft YaHei',sans-serif}}
.wrap{{max-width:1200px;margin:0 auto;padding:20px}}h1{{font-size:20px}}h2{{font-size:16px;color:#8aa0c0;
border-bottom:1px solid #2a3550;padding-bottom:6px}}table{{border-collapse:collapse;width:100%}}
th,td{{padding:8px;border-bottom:1px solid #2a3550;text-align:left}}th{{color:#8aa0c0}}
.bw{{background:#0b1018;border-radius:6px;height:14px;min-width:120px;display:inline-block;margin-right:8px;vertical-align:middle}}
.bw i{{display:block;height:100%;border-radius:6px;background:linear-gradient(90deg,#2ecc8f,#f5a623)}}
.bw.off{{opacity:.15}}td b{{margin-left:4px}}.note{{color:#8aa0c0;font-size:12px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}a{{color:#4f8cff}}
.card{{background:#171e2e;border:1px solid #2a3550;border-radius:10px;padding:12px}}
.mini2{{display:flex;justify-content:space-between;font-size:13px;padding:3px 0;
border-bottom:1px dashed #2a3550}}h3{{margin-top:0;font-size:14px;color:#8aa0c0}}
.tag-old{{color:#f5a623}}.tag-new{{color:#2ecc8f}}</p></style></head>
<body><div class="wrap">
<h1>提示词版本对比报告 <span class="tag-old">旧提示词(research_agent.db)</span> vs
<span class="tag-new">新提示词(ontology_v02.db, 50篇)</span></h1>
<p style="color:#8aa0c0">同主题骨修复新材料；旧库含此前批量入库（约50篇完成提取），新库为隔离的50篇独立构建。</p>
<h2>核心指标</h2>
<table><thead><tr><th style="width:180px">指标</th><th>旧提示词</th><th>新提示词</th><th>说明</th></tr></thead>
<tbody>{rows}</tbody></table>
<h2>本体规模总览</h2>
<table><thead><tr><th></th><th>旧提示词</th><th>新提示词</th></tr></thead><tbody>
<tr><td>论文总数/已提取</td><td>{old['papers']} / {old['runs']}</td><td>{new['papers']} / {new['runs']}</td></tr>
<tr><td>节点/边</td><td>{old['nodes']} / {old['edges']}</td><td>{new['nodes']} / {new['edges']}</td></tr>
<tr><td>节点类型数/类型注册表</td><td>{old['node_types']} / {old['types_registry']}</td>
<td>{new['node_types']} / {new['types_registry']}</td></tr>
<tr><td>孤立节点/分量/最大分量占比</td><td>{old['isolated']} / {old['components']} / {old['largest_comp_ratio']}</td>
<td>{new['isolated']} / {new['components']} / {new['largest_comp_ratio']}</td></tr>
</tbody></table>
<h2>拓扑分布（可交互图谱）</h2>
<div class="grid">
<div class="card"><h3>旧提示词本体 · {old['nodes']} 节点</h3>
<a target="_blank" href="old_graph.html">打开交互图谱(old_graph.html)</a>
<h3 style="margin-top:14px">Top 节点类型 / 关系</h3>{tops('node', old)}{tops('rel', old)}</div>
<div class="card"><h3>新提示词本体 · {new['nodes']} 节点</h3>
<a target="_blank" href="new_graph.html">打开交互图谱(new_graph.html)</a>
<h3 style="margin-top:14px">Top 节点类型 / 关系</h3>{tops('node', new)}{tops('rel', new)}</div>
</div>
<h2>非受控关系（词表外的关系类型）</h2>
<table><thead><tr><th></th><th>类型</th><th>边数</th></tr></thead><tbody>
<tr><td>旧</td><td>{esc(list(old['non_canonical_relations'].keys()))}</td>
<td>{sum(old['non_canonical_relations'].values())}</td></tr>
<tr><td>新</td><td>{esc(list(new['non_canonical_relations'].keys()))}</td>
<td>{sum(new['non_canonical_relations'].values())}</td></tr>
</tbody></table>
</div></body></html>"""
    (OUT / "index_compare.html").write_text(html, encoding="utf-8")
    print("已生成:", OUT)
    print(json.dumps({"old": {k: v for k, v in old.items()
                              if not isinstance(v, (list, dict))},
                      "new": {k: v for k, v in new.items()
                              if not isinstance(v, (list, dict))}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
