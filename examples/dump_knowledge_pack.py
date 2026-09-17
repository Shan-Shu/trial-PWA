"""知识包健康检查：真实语料下"进入模型的机制信息有多少"。

这是把 `docs/STATUS_AND_NEXT_STEPS_v0.4.0.md` 第 3.2 节的人工诊断固化成可回归的工具。

用法::

    python examples/dump_knowledge_pack.py \
        --db data/ynamide_multiazabicycle_v020.db \
        --seed "ynamide annulation" --seed "ynamide nitrogen heterocycle synthesis" \
        --out output/knowledge_pack_report.json

输出指标：
- 代码层挖到的 patterns / evidence / hyperedges；
- LLM 实际收到（截断后）的数量与字符数（提示词预算）；
- 截断后仍含机制关键词的超边比例（目标 ≥ 30/80）；
- 机制状态 / 算子链候选数量与可追溯率。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from research_agent.db import connect  # noqa: E402
from research_agent.study.consumer import (  # noqa: E402
    CONSUMER_PROMPT,
    _compact_consumer_knowledge,
    deterministic_design_context,
    mine_ontology_evidence,
)
from research_agent.study.reaction_operators import (  # noqa: E402
    operator_catalog_for_prompt,
)

MECHANISM_KEYWORDS = (
    "intermediate", "cation", "anion", "carbene", "nitrene", "radical",
    "umpolung", "dearomat", "rearrang", "migrat", "insertion", "cyclization",
    "annulation", "cascade", "tandem", "activation", "oxidat", "reduct",
    "eliminat", "coordinat", "ligand", "enantio", "diastereo", "regio",
    "mechanism", "bond formation", "polarity", "vinyl", "propargyl",
)


def mechanism_hits(items: list[dict], key: str = "label") -> int:
    out = 0
    for item in items:
        text = " ".join([
            str(item.get(key) or ""),
            " ".join(str(x) for x in item.get("members") or []),
        ]).lower()
        if any(k in text for k in MECHANISM_KEYWORDS):
            out += 1
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="知识包健康检查")
    ap.add_argument("--db", default="data/research_agent.db")
    ap.add_argument("--seed", action="append", default=[])
    ap.add_argument("--min-confidence", type=float, default=0.6)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    seeds = args.seed or ["research method"]
    mission = {"seed_terms": seeds, "min_confidence": args.min_confidence,
               "max_results": 80}
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db)
    try:
        bundle = mine_ontology_evidence(conn, mission)
        design = deterministic_design_context(bundle, {"goal": " ".join(seeds)})
    finally:
        conn.close()

    compact = _compact_consumer_knowledge(bundle)
    prompt_chars = len(
        CONSUMER_PROMPT.replace("{operators}", operator_catalog_for_prompt())
        .replace("{plan}", "{}")
        .replace("{knowledge}", json.dumps(compact, ensure_ascii=False)))
    kept_hyperedges = compact["hyperedges"]
    report = {
        "db": args.db,
        "seeds": seeds,
        "mined": {
            "patterns": len(bundle["patterns"]),
            "evidence": len(bundle["evidence"]),
            "hyperedges": len(bundle["hyperedges"]),
            "coverage_score": bundle["coverage_score"],
            "pattern_support_distribution": _support_distribution(
                bundle["patterns"]),
        },
        "prompt": {
            "patterns": len(compact["patterns"]),
            "hyperedges": len(kept_hyperedges),
            "chars": prompt_chars,
            "approx_tokens": prompt_chars // 3,
        },
        "mechanism_signal": {
            "mined_hyperedges_with_mechanism": mechanism_hits(
                [{"label": h.get("label"),
                  "members": h.get("member_names") or []}
                 for h in bundle["hyperedges"]]),
            "kept_hyperedges_with_mechanism": mechanism_hits(kept_hyperedges),
            "kept_total": len(kept_hyperedges),
        },
        "design_context": {
            "mechanism_states": len(design["mechanism_states"]),
            "reaction_primitives": len(design["reaction_primitives"]),
            "opportunity_gaps": len(design["opportunity_gaps"]),
            "operator_candidates": len(design["operator_candidates"]),
            "constraint_conflicts": len(design["constraint_conflicts"]),
            "traceability": design["traceability"],
        },
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"\n报告已写入 {args.out}")
    return 0


def _support_distribution(patterns: list[dict]) -> dict[str, int]:
    dist: dict[str, int] = {}
    for pattern in patterns:
        key = str(int(pattern.get("support_count") or 0))
        dist[key] = dist.get(key, 0) + 1
    return dict(sorted(dist.items(), key=lambda x: int(x[0])))


if __name__ == "__main__":
    raise SystemExit(main())
