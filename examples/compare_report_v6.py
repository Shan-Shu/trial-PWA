# -*- coding: utf-8 -*-
"""v0.0.5 vs v0.0.6 同语料对比：本体拓扑 + 关系分布 + 属性质量。输出 output/compare6/。"""
from __future__ import annotations
import importlib.util, json, shutil
from pathlib import Path
from research_agent.db import connect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "compare6"
SPEC = [
    ("v05", "v0.0.5（同语料 50 篇基线）", ROOT / "data" / "ontology_v05.db", "v05_graph.html"),
    ("v06", "v0.0.6（硬性禁区+二次精修+语料提醒）", ROOT / "data" / "ontology_v06.db", "v06_graph.html"),
]
_cr = importlib.util.spec_from_file_location("compare_report_mod", ROOT / "examples" / "compare_report.py")
cr = importlib.util.module_from_spec(_cr); _cr.loader.exec_module(cr)
_aq = importlib.util.spec_from_file_location("attr_quality_mod", ROOT / "examples" / "attribute_quality_report.py")
aq = importlib.util.module_from_spec(_aq); _aq.loader.exec_module(aq)


def extra(db):
    c = connect(db)
    inv = c.execute("SELECT COUNT(*) FROM ontology_edges WHERE relation_type='involves'").fetchone()[0]
    has_ev = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_assertions'").fetchone()
    events = c.execute("SELECT COUNT(*) FROM event_assertions").fetchone()[0] if has_ev else 0
    mats = c.execute("SELECT COUNT(*) FROM material_registry").fetchone()[0]
    c.close()
    return {"involves_edges": inv, "event_assertions": events, "material_registry": mats}


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main():
    OUT.mkdir(parents=True, exist_ok=True); (OUT / "vendor").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "assets" / "vendor" / "vis-network.min.js", OUT / "vendor" / "vis-network.min.js")
    cr.OUT = OUT
    data = {}
    for key, title, db, page in SPEC:
        m = cr.load(db)
        top3 = sum(v for _, v in m["top_relations"][:3])
        m["top3_share"] = round(top3 / m["edges"], 4) if m["edges"] else None
        m.update(extra(db))
        m.update(aq.audit(db))
        data[key] = m
        cr.write_graph_page(page, "动态本体 " + title, db)
    (OUT / "compare_metrics_v6.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    rows_meta = [
        ("处理文献(runs)", "runs"), ("本体节点", "nodes"),
        ("关系边", "edges"), ("每篇节点", "per_paper_nodes"),
        ("每篇边", "per_paper_edges"), ("孤立节点率", "isolated_ratio"),
        ("连通分量数", "components"), ("最大分量占比", "largest_comp_ratio"),
        ("跨文献共享率", "multi_paper_ratio"), ("平均度", "avg_degree"),
        ("top3 关系占比", "top3_share"),
        ("关系类型(非受控边)", "relation_types", "non_canon_sum"),
        ("involves 边", "involves_edges"), ("事件断言(旁路)", "event_assertions"),
        ("材料登记(lcmat)", "material_registry"),
        ("空属性节点率", "empty_ratio"), ("None 值属性行", "none_rows"),
    ]
    for key in SPEC:
        m = data[key[0]]
        m["non_canon_sum"] = sum(m["non_canonical_relations"].values())

    def cell(v):
        return f"<b>{v}</b>" if v is not None else "-"

    rows = []
    for item in rows_meta:
        if len(item) == 2:
            label, k = item
            vals = [data[x][k] for x, *_ in SPEC]
        else:
            label, k1, k2 = item
            vals = [f"{data[x][k1]}({data[x][k2]})" for x, *_ in SPEC]
        cells = "".join(f"<td>{cell(v)}</td>" for v in vals)
        rows.append(f"<tr><td>{label}</td>{cells}</tr>")

    # 关系分布表
    rel_rows = []
    all_types = sorted({rt for x, *_ in SPEC for rt, _ in data[x]["top_relations"]},
                       key=lambda rt: -max(dict(data[x]["top_relations"]).get(rt, 0)
                                           for x, *_ in SPEC))
    for rt in all_types[:15]:
        cells = "".join(f"<td>{dict(data[x]['top_relations']).get(rt, 0)}</td>" for x, *_ in SPEC)
        rel_rows.append(f"<tr><td>{esc(rt)}</td>{cells}</tr>")

    cards = []
    for key, title, db, page in SPEC:
        m = data[key]
        cards.append(f"<div class='card'><h3>{esc(title)}</h3>"
                     f"<p class='sub'>节点 {m['nodes']} · 边 {m['edges']} · 已提取 {m['runs']} 篇</p>"
                     f"<a target='_blank' href='{page}'>打开图谱</a></div>")

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>v0.0.5 vs v0.0.6 同语料对比</title>
<style>body{{margin:0;background:#0f1420;color:#dbe4f5;font:14px 'Segoe UI','Microsoft YaHei',sans-serif}}
.wrap{{max-width:1200px;margin:0 auto;padding:20px}}table{{border-collapse:collapse;width:100%;margin:8px 0}}
th,td{{padding:7px;border-bottom:1px solid #2a3550;text-align:left}}th{{color:#8aa0c0}}td b{{color:#fff}}
.grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}}
.card{{background:#171e2e;border:1px solid #2a3550;border-radius:10px;padding:10px}}
a{{color:#4f8cff}}.sub{{color:#8aa0c0;font-size:12px}}h3{{margin:4px 0}}
h2{{color:#8aa0c0;border-bottom:1px solid #2a3550;padding-bottom:6px}}</style></head><body><div class="wrap">
<h1>v0.0.5 → v0.0.6 同语料 50 篇对比（同一批 PubMed 论文 / deepseek-v4-pro）</h1>
<table><thead><tr><th style="width:230px">指标</th><th>v0.0.5</th><th>v0.0.6</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<h2>Top 关系类型分布</h2>
<table><thead><tr><th style="width:230px">关系</th><th>v0.0.5</th><th>v0.0.6</th></tr></thead>
<tbody>{''.join(rel_rows)}</tbody></table>
<p class="sub">属性质量由 attribute_quality_report 扫描：空属性率越低越好；None 值行越少越好；关系类型列格式为 类型数(非受控边数)。</p>
<h2>拓扑图谱</h2><div class="grid">{''.join(cards)}</div>
</div></body></html>"""
    (OUT / "index_compare6.html").write_text(html, encoding="utf-8")
    print("已生成:", OUT)
    for key, *_ in SPEC:
        m = data[key]
        print(f"{key}: nodes={m['nodes']} edges={m['edges']} iso={m['isolated_ratio']} "
              f"comps={m['components']} largest={m['largest_comp_ratio']} top3={m['top3_share']} "
              f"rel={m['relation_types']}({m['non_canon_sum']}) involves={m['involves_edges']} "
              f"events={m['event_assertions']} matReg={m['material_registry']} "
              f"emptyAttr={m['empty_ratio']} noneRows={m['none_rows']}")


if __name__ == "__main__":
    main()
