# -*- coding: utf-8 -*-
"""对照《报告.txt》核查 v0.0.4 节点属性质量。"""
import json, re, sqlite3
from collections import Counter, defaultdict
from pathlib import Path

DB = Path(r"D:\Desktop\trial\data\ontology_v04.db")
conn = sqlite3.connect(str(DB)); conn.row_factory = sqlite3.Row
N = [dict(r) for r in conn.execute("SELECT * FROM ontology_nodes ORDER BY node_id")]

def load(x, default=None):
    try:
        return json.loads(x) if isinstance(x, str) and x else default
    except Exception:
        return default

empty = 0
attr_keys = Counter()
none_rows = 0
typed = Counter()
long_trigger = []
described = []
material_common = defaultdict(int)
by_type = defaultdict(int); by_type_empty = defaultdict(int)
for n in N:
    t = n["node_type"]; by_type[t] += 1
    a = load(n["attributes"], {})
    if not a:
        empty += 1; by_type_empty[t] += 1
        continue
    def walk(k, v):
        global none_rows
        if isinstance(v, dict):
            if "value" in v and "source" in v:
                typed["dict{value,source}"] += 1
                attr_keys[k] += 1
                return
            for kk, vv in v.items():
                walk(f"{k}.{kk}", vv)
        elif isinstance(v, list):
            typed["list"] += 1
            attr_keys[k] += 1
            if v == []:
                none_rows += 1
            for item in v:
                if isinstance(item, dict) and set(item) <= {"value", "source"}:
                    typed["list{value,source}"] += 1
        elif v is None:
            none_rows += 1
            attr_keys[k] += 1
            typed["None"] += 1
        else:
            typed[type(v).__name__] += 1
            attr_keys[k] += 1
    for k, v in a.items():
        walk(k, v)
    if t in ("Experiment", "Discovery", "Observation", "Study") and isinstance(a, dict):
        tr = a.get("trigger")
        if isinstance(tr, str) and len(tr) > 80:
            long_trigger.append((n["node_id"], n["name"][:40], len(tr)))
    if t == "Property" and isinstance(a, dict) and "described_as" in a:
        described.append((n["node_id"], n["name"], a["described_as"]))
    for key in ("composition", "processing_method", "architecture", "application", "filler", "matrix"):
        if key in a:
            material_common[key] += 1

print("节点总数:", len(N), "| 空属性节点:", empty, "(%.1f%%)" % (100*empty/len(N)))
print("各类型空属性占比(前10):")
for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
    if t in by_type_empty:
        print("  %-22s 空 %d/%d" % (t, by_type_empty[t], c))
print("值类型分布:", dict(typed.most_common()))
print("None 值属性行:", none_rows)
print("最长 trigger>80:", len(long_trigger), long_trigger[:5])
print("Property.described_as:", len(described), described[:6])
print("Material 通用键出现(composition/processing/architecture/application/filler/matrix):", dict(material_common))
print("顶层属性键 Top30:", attr_keys.most_common(30))
conn.close()
