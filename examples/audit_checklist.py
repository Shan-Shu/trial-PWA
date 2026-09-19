# -*- coding: utf-8 -*-
"""按《清单汇总》对 v0.0.4 本体逐类核查。"""
import json, re, sqlite3
from collections import Counter, defaultdict

DB = r"D:\Desktop\trial\data\ontology_v04.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
N = [dict(r) for r in conn.execute("SELECT * FROM ontology_nodes ORDER BY node_id")]
E = [dict(r) for r in conn.execute("SELECT * FROM ontology_edges ORDER BY edge_id")]
nmap = {n["node_id"]: n for n in N}
EVENT = {"Experiment", "Study", "Discovery", "Observation", "ClinicalTrial"}
def J(raw):
    try:
        return json.loads(raw or "[]") if raw is not None else []
    except Exception:
        return []
def AJ(raw):
    try:
        return json.loads(raw or "{}") if raw is not None else {}
    except Exception:
        return {}
def prov_papers(n):
    return [p.get("paper") for p in J(n.get("provenance"))]
def name(i):
    n = nmap.get(i)
    return (n["name"] if n else "?")
def typ(i):
    n = nmap.get(i)
    return (n["node_type"] if n else "?")

print("== 总览 ==", len(N), "nodes /", len(E), "edges")

# 1 事件命名
ev = [n for n in N if n["node_type"] in EVENT]
deng = [n for n in ev if "等 " in (n["name"] or "") or " 等" in (n["name"] or "")]
print("\n[1] 事件类节点:", len(ev), "| 名称含“等N项”:", len(deng),
      "| 示例:", [n["name"][:50] for n in deng[:4]])

# 2 关系分布
rc = Counter(e["relation_type"] for e in E)
total = len(E)
print("\n[2] 关系分布(前10):", rc.most_common(10))
for rt in ("involves", "related_to", "promotes", "regulates", "uses"):
    sub = [e for e in E if e["relation_type"] == rt]
    if not sub:
        continue
    ot = Counter(typ(e["target_node"]) for e in sub)
    st = Counter(typ(e["source_node"]) for e in sub)
    print(f"  {rt}({len(sub)}) 主语类型top:{st.most_common(4)} 宾语类型top:{ot.most_common(4)}")

# 3 总称/评价词节点
umbrella = [n for n in N if (n["name"] or "").strip().lower() in
            {"biomaterial", "inorganic biomaterial", "polymeric biomaterial",
             "composite biomaterial", "emerging biomaterial", "biological response",
             "conventional material", "conventional biomaterial"}]
graded = [n for n in N if re.search(r"^(good|excellent|superior|enhanced|high|great)\s+", (n["name"] or ""), re.I)]
print("\n[3] 总称类节点:", len(umbrella), [n["name"] for n in umbrella[:8]])
print("  评价词开头的Property/Material节点:", len(graded), [n["name"] for n in graded[:8]])

# 4 同名不同类
norm = defaultdict(set)
for n in N:
    norm[(n["node_type"], (n["name"] or "").strip().lower())].add(n["node_id"])
name2types = defaultdict(set)
for (t, nm), ids in norm.items():
    name2types[nm].add(t)
multi = {nm: ts for nm, ts in name2types.items() if len(ts) > 1}
print("\n[4] 同名映射到>=2种类型:", len(multi))
for nm, ts in list(multi.items())[:15]:
    print("   -", nm, "->", sorted(ts))

# 5 相关/方向 & predicate 冗余
preds = [AJ(e.get("attributes")).get("predicate", "") for e in E]
nonempty = sum(1 for p in preds if p)
cjk = sum(1 for p in preds if re.search(r"[\u4e00-\u9fff]", p))
longp = sum(1 for p in preds if len(p) > 80)
print("\n[5] predicate: 非空", nonempty, "| 含中文", cjk, "| 超80字符", longp)

# 6 置信度
confs = [float(e["confidence"]) for e in E]
nconf = [float(n["confidence"]) for n in N]
low = [e for e in E if float(e["confidence"]) < 0.72 and e["relation_type"] in
       {"promotes", "regulates", "activates", "inhibits", "causes", "differentiates_into", "treats", "targets"}]
print("\n[6] 边置信 min/mean/max: %.3f/%.3f/%.3f" % (min(confs), sum(confs)/len(confs), max(confs)))
print("  低置信(<0.72)强类型边:", len(low), [(e["edge_id"], e["relation_type"], e["confidence"]) for e in low[:8]])
print("  节点置信 min/mean/max: %.3f/%.3f/%.3f" % (min(nconf), sum(nconf)/len(nconf), max(nconf)))
same = sum(1 for n in N if n["first_seen_at"] == n["last_seen_at"])
print("  first==last 节点:", same, "/", len(N))

# 7 论文类型(评述/勘误)溯源
papers = {r["paper_key"]: r["title"] or "" for r in conn.execute("SELECT paper_key, title FROM papers")}
ptype_hits = Counter()
for n in N:
    for p in prov_papers(n):
        t = papers.get(p, "").lower()
        if t.startswith(("comment", "corrigendum", "erratum", "editorial")):
            ptype_hits["comment/corr"] += 1
        elif "review" in t[:60]:
            ptype_hits["review"] += 1
print("\n[7] 节点溯源命中 comment/corrigendum/editorial:", ptype_hits["comment/corr"],
      "| review:", ptype_hits["review"])

# 8 类型误分类模式
def cnt(pat, types_ok, field="name"):
    return [n for n in N if re.search(pat, n[field] or "", re.I) and n["node_type"] in types_ok]
m_rna = cnt(r"\bmrna\b|\bmiR-\d+|microRNA", {"Chemical"})
ev_mat = cnt(r"exosome|extracellular vesicle|secretome", {"Material"})
cl_odd = [n for n in N if n["node_type"] == "CellLine" and re.search(
    r"mesenchymal|stem cell|osteoblast|macrophage|huv\b|huv?ec|hADSC|BMSC|primary", n["name"] or "", re.I)]
print("\n[8] mRNA/miRNA误标Chemical:", len(m_rna), [n["name"] for n in m_rna[:6]])
print("  EV/exosome标Material:", len(ev_mat), [n["name"] for n in ev_mat[:6]])
print("  CellLine疑似原代/类型:", len(cl_odd), [n["name"] for n in cl_odd[:8]])

# 9 别名过度(以 hub 为例)
alias_cnt = [n for n in N if len(J(n.get("aliases"))) >= 5]
print("\n[9] 别名>=5的节点:", len(alias_cnt), [ (n["node_type"], n["name"][:40]) for n in alias_cnt[:8]])

# 10 数值/单位埋在文本
num_edges = [e for e in E if re.search(r"\d+(\.\d+)?\s*(%|kPa|MPa|Pa|fold|µg|mg|ng|mm|µm|nm|day|°C|℃|mM|μM|pg)", AJ(e.get("attributes")).get("predicate", "") or "")]
num_attr_nodes = [n for n in N if re.search(r"\d+(\.\d+)?\s*(%|kPa|MPa|fold|µg|mg|day|°C|℃|mM|μM)", AJ(n.get("attributes")).__str__())]
print("\n[10] predicate含数值+单位 的边:", len(num_edges), "| attributes含数值单位节点:", len(num_attr_nodes))

# 11 同句证据复用多条边
sent = Counter()
for e in E:
    sent[prov_text := "|".join(p.get("evidence", "")[:120] for p in J(e.get("provenance")))] += 1
reused = sum(1 for k, v in sent.items() if v >= 2 and k)
print("\n[11] 同句证据被>=2条边复用 的句子数:", reused)

# 12 hub 语境
multi_paper = [(n["node_id"], n["node_type"], n["name"], len(set(prov_papers(n)))) for n in N if len(set(prov_papers(n))) >= 8]
print("\n[12] 支持>=8篇的hub节点:", len(multi_paper), [(x[0], x[1], x[2][:40]) for x in multi_paper[:8]])

# 13 边重复同predicate
pred_multi = Counter((e["relation_type"], AJ(e.get("attributes")).get("predicate", "")[:80]) for e in E)
dup = {k: v for k, v in pred_multi.items() if v >= 2 and k[1]}
print("\n[13] 同(关系,predicate前缀)被复用>=2次:", len(dup), list(dup.items())[:5])
conn.close()
