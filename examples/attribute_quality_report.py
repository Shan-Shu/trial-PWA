# -*- coding: utf-8 -*-
"""属性质量扫描器：对任意本体库输出属性质量报告(JSON+控制台)。
用法: bundled_python attribute_quality_report.py <db_path> [out_json]
"""
import json, sys, sqlite3
from collections import Counter
from pathlib import Path

def audit(db):
    conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
    N = [dict(r) for r in conn.execute("SELECT node_id, node_type, name, attributes FROM ontology_nodes")]
    conn.close()
    total = len(N)
    def load(x):
        try: return json.loads(x) if isinstance(x, str) and x else {}
        except Exception: return {}
    empty = 0
    by_type = Counter(); by_type_empty = Counter()
    value_types = Counter(); none_rows = 0
    leaf_keys = Counter()
    material_common = Counter()
    def walk(k, v):
        nonlocal none_rows
        if isinstance(v, dict):
            if "value" in v and "unit" in v:
                value_types["num{value,unit}"] += 1
            else:
                for kk, vv in v.items():
                    walk(f"{k}.{kk}", vv)
            leaf_keys[k] += 1
        elif isinstance(v, list):
            value_types["list"] += 1; leaf_keys[k] += 1
            if not v: none_rows += 1
        elif v is None:
            none_rows += 1; leaf_keys[k] += 1; value_types["None"] += 1
        else:
            value_types[type(v).__name__] += 1; leaf_keys[k] += 1
    for n in N:
        by_type[n["node_type"]] += 1
        a = load(n["attributes"])
        if not a:
            empty += 1; by_type_empty[n["node_type"]] += 1
            continue
        for k, v in a.items():
            walk(k, v)
        for key in ("composition", "components", "fabrication_method", "architecture", "application"):
            if key in a: material_common[key] += 1
    core_empty = {t: [by_type_empty.get(t, 0), by_type[t]] for t in
                  ("Material", "BiologicalProcess", "Disease", "Method", "Chemical", "Property") if by_type.get(t)}
    return {
        "db": str(db), "nodes": total,
        "empty_ratio": round(empty / total, 4) if total else 0,
        "empty_nodes": empty, "by_type_empty": core_empty,
        "value_types": dict(value_types), "none_rows": none_rows,
        "top_keys": leaf_keys.most_common(20),
        "material_common_keys": dict(material_common),
    }

def main():
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"D:\Desktop\trial\data\ontology_v04.db")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(r"D:\Desktop\trial\output\attribute_quality_report.json")
    r = audit(db)
    out.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print("报告已写入:", out)
    print("空属性率: %.1f%% (%d/%d)" % (100 * r["empty_ratio"], r["empty_nodes"], r["nodes"]))
    print("None 值行:", r["none_rows"], "| 值类型:", r["value_types"])
    print("核心类型空属性:", r["by_type_empty"])
    print("Material 通用键:", r["material_common_keys"])

if __name__ == "__main__":
    main()
