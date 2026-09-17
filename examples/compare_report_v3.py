"""三版本提示词动态本体对比（v0.0.1 旧库 / v0.0.2 / v0.0.3 同语料 50 篇）。输出 output/compare3/。"""
from __future__ import annotations
import importlib.util, json, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "compare3"
SPEC = [
    ("v01", "v0.0.1 旧提示词", ROOT / "data" / "research_agent.db", "v01_graph.html"),
    ("v02", "v0.0.2 提示词", ROOT / "data" / "ontology_v02.db", "v02_graph.html"),
    ("v03", "v0.0.3 提示词", ROOT / "data" / "ontology_v03.db", "v03_graph.html"),
]
_crtmp = importlib.util.spec_from_file_location("compare_report_mod", ROOT / "examples" / "compare_report.py")
cr = importlib.util.module_from_spec(_crtmp)
_crtmp.loader.exec_module(cr)


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "vendor").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "assets" / "vendor" / "vis-network.min.js", OUT / "vendor" / "vis-network.min.js")
    cr.OUT = OUT
    data = {}
    for key, title, db, page in SPEC:
        data[key] = cr.load(db)
        cr.write_graph_page(page, title, db)
    (OUT / "compare_metrics_v3.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    rows_meta = [
        ("已提取文献(篇)", "runs"), ("本体节点", "nodes"), ("关系边", "edges"),
        ("节点类型数", "node_types"), ("关系类型数", "relation_types"),
        ("孤立节点率", "isolated_ratio"), ("连通分量数", "components"),
        ("最大分量占比", "largest_comp_ratio"), ("跨文献共享节点率", "multi_paper_ratio"),
        ("平均度", "avg_degree"), ("单篇节点", "per_paper_nodes"), ("单篇边", "per_paper_edges"),
    ]
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
        cards.append(f"""
<div class="card"><h3>{esc(title)}</h3>
<p class="sub">节点 {m['nodes']} · 边 {m['edges']} · 已提取 {m['runs']} 篇</p>
<a target="_blank" href="{page}">打开交互图谱</a>
<h4>Top 关系</h4>{tops(key)}</div>""")

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>三版本提示词动态本体对比</title>
<style>body{{margin:0;background:#0f1420;color:#dbe4f5;font:14px 'Segoe UI','Microsoft YaHei',sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:20px}}
table{{border-collapse:collapse;width:100%;margin:10px 0}}
th,td{{padding:8px;border-bottom:1px solid #2a3550;text-align:left}}
th{{color:#8aa0c0}}td b{{color:#fff}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}
.card{{background:#171e2e;border:1px solid #2a3550;border-radius:10px;padding:12px}}
.mini2{{display:flex;justify-content:space-between;font-size:13px;padding:3px 0;border-bottom:1px dashed #2a3550}}
a{{color:#4f8cff}}.sub{{color:#8aa0c0;font-size:12px}}h3,h4{{margin:6px 0}}
h2{{color:#8aa0c0;border-bottom:1px solid #2a3550;padding-bottom:6px}}</style></head><body><div class="wrap">
<h1>三版本动态本体对比（骨修复新材料 · v0.0.2/v0.0.3 同语料 50 篇）</h1>
<table><thead><tr><th style="width:190px">指标</th><th>v0.0.1 旧提示词</th><th>v0.0.2</th><th>v0.0.3</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p class="sub">v0.0.1 为历史积累（语料不完全一致）；v0.0.2 与 v0.0.3 用同一批 50 篇独立构建，可直接比较。</p>
<h2>拓扑分布</h2><div class="grid">{''.join(cards)}</div>
</div></body></html>"""
    (OUT / "index_compare3.html").write_text(html, encoding="utf-8")
    print("已生成:", OUT)
    for key, *_ in SPEC:
        m = data[key]
        print(f"{key}: nodes={m['nodes']} edges={m['edges']} iso={m['isolated_ratio']} comps={m['components']} "
              f"largest={m['largest_comp_ratio']} rel_types={m['relation_types']} "
              f"non_canon={sum(m['non_canonical_relations'].values())} multi={m['multi_paper_ratio']} "
              f"avg_deg={m['avg_degree']}")


if __name__ == "__main__":
    main()
