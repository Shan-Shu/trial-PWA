"""四版本动态本体对比（v01 旧 / v02 / v03 / v04 同语料50篇）。输出 output/compare4/。"""
from __future__ import annotations
import importlib.util, json, re, shutil
from pathlib import Path
from research_agent.db import connect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "compare4"
SPEC = [
    ("v01", "v0.0.1 旧提示词", ROOT / "data" / "research_agent.db", "v01_graph.html"),
    ("v02", "v0.0.2 提示词", ROOT / "data" / "ontology_v02.db", "v02_graph.html"),
    ("v03", "v0.0.3 提示词", ROOT / "data" / "ontology_v03.db", "v03_graph.html"),
    ("v04", "v0.0.4 提示词", ROOT / "data" / "ontology_v04.db", "v04_graph.html"),
]
_crtmp = importlib.util.spec_from_file_location("compare_report_mod", ROOT / "examples" / "compare_report.py")
cr = importlib.util.module_from_spec(_crtmp)
_crtmp.loader.exec_module(cr)

EVENT_TYPES = ("Experiment", "Study", "Discovery", "Observation", "ClinicalTrial")
CLAUSE = re.compile(r"\b(show(s|ed)?|suggest(s|ed)?|demonstrat(e|es|ed)?|reveal(s|ed)?|found|observed|describe(s|d)?|summariz(e|es|ed)?|propose(s|d)?|result(ed|ing|s)? in|lead(s|ing)? to|was|were|using|performed|conducted|examined|investigated|aim(s|ed)?|developed|fabricated|we|here)\b", re.I)


def extra(db: Path):
    c = connect(db)
    edges = c.execute("SELECT relation_type FROM ontology_edges").fetchall()
    total = len(edges)
    inv = sum(1 for r in edges if r["relation_type"] == "involves")
    rows = c.execute("SELECT node_type, name FROM ontology_nodes WHERE node_type IN ('Experiment','Study','Discovery','Observation','ClinicalTrial')").fetchall()
    pseudo = 0
    for r in rows:
        name = r["name"] or ""
        if not any(name.startswith(t + ":") for t in EVENT_TYPES):
            pseudo += 1
        elif CLAUSE.search(name):
            pseudo += 1
    c.close()
    return {"involves_share": round(inv / total, 4) if total else 0, "event_nodes": len(rows), "pseudo_events": pseudo}


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "vendor").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "assets" / "vendor" / "vis-network.min.js", OUT / "vendor" / "vis-network.min.js")
    cr.OUT = OUT
    data = {}
    for key, title, db, page in SPEC:
        m = cr.load(db)
        m.update(extra(db))
        data[key] = m
        cr.write_graph_page(page, title, db)
    (OUT / "compare_metrics_v4.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    rows_meta = [
        ("已提取文献(篇)", "runs"), ("本体节点", "nodes"), ("关系边", "edges"),
        ("孤立节点率", "isolated_ratio"), ("连通分量数", "components"),
        ("最大分量占比", "largest_comp_ratio"), ("跨文献共享节点率", "multi_paper_ratio"),
        ("平均度", "avg_degree"), ("节点类型数", "node_types"), ("关系类型数", "relation_types"),
        ("非受控关系边数", "non_canon_sum"), ("involves 占比", "involves_share"),
        ("事件节点数", "event_nodes"), ("伪事件(整句)数", "pseudo_events"),
    ]
    for key, *_ in SPEC:
        data[key]["non_canon_sum"] = sum(data[key]["non_canonical_relations"].values())
    rows = []
    for label, k in rows_meta:
        vals = [data[x][k] for x, *_ in SPEC]
        cells = "".join(f"<td><b>{v}</b></td>" if v is not None else "<td>-</td>" for v in vals)
        rows.append(f"<tr><td>{label}</td>{cells}</tr>")

    def tops(key):
        return "".join(f"<div class='mini2'><span>{esc(t)}</span><b>{n}</b></div>"
                       for t, n in data[key]["top_relations"])

    cards = []
    for key, title, db, page in SPEC:
        m = data[key]
        cards.append(f"<div class='card'><h3>{esc(title)}</h3>"
                     f"<p class='sub'>节点 {m['nodes']} · 边 {m['edges']} · 提取 {m['runs']} 篇</p>"
                     f"<a target='_blank' href='{page}'>打开交互图谱</a><h4>Top 关系</h4>{tops(key)}</div>")

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>四版本动态本体对比</title>
<style>body{{margin:0;background:#0f1420;color:#dbe4f5;font:14px 'Segoe UI','Microsoft YaHei',sans-serif}}
.wrap{{max-width:1280px;margin:0 auto;padding:20px}}
table{{border-collapse:collapse;width:100%;margin:10px 0}}th,td{{padding:8px;border-bottom:1px solid #2a3550;text-align:left}}
th{{color:#8aa0c0}}td b{{color:#fff}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.card{{background:#171e2e;border:1px solid #2a3550;border-radius:10px;padding:12px}}
.mini2{{display:flex;justify-content:space-between;font-size:12px;padding:3px 0;border-bottom:1px dashed #2a3550}}
a{{color:#4f8cff}}.sub{{color:#8aa0c0;font-size:12px}}h3,h4{{margin:6px 0}}
h2{{color:#8aa0c0;border-bottom:1px solid #2a3550;padding-bottom:6px}}</style></head><body><div class="wrap">
<h1>四版本动态本体对比（骨修复新材料 · v0.0.2/3/4 同语料 50 篇）</h1>
<table><thead><tr><th style="width:200px">指标</th><th>v0.0.1 旧</th><th>v0.0.2</th><th>v0.0.3</th><th>v0.0.4</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p class="sub">v0.0.1 为历史库（语料不完全一致）；v0.0.2-0.0.4 为同一批 50 篇、各自提示词独立构建。</p>
<h2>拓扑分布</h2><div class="grid">{''.join(cards)}</div>
</div></body></html>"""
    (OUT / "index_compare4.html").write_text(html, encoding="utf-8")
    print("已生成:", OUT)
    for key, *_ in SPEC:
        m = data[key]
        print(f"{key}: nodes={m['nodes']} edges={m['edges']} iso={m['isolated_ratio']} comps={m['components']} "
              f"largest={m['largest_comp_ratio']} rel_types={m['relation_types']} non_canon={m['non_canon_sum']} "
              f"inv_share={m['involves_share']} events={m['event_nodes']} pseudo={m['pseudo_events']}")


if __name__ == "__main__":
    main()
