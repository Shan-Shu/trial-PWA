"""基于现有动态本体快照生成文献综述，不触发外部检索。

输出到 output/reviews_versions/。默认覆盖 v0.0.1(v01 历史库)/v0.0.2-v0.0.5；
v0.0.6 本体若仍在并行写入，则需其稳定后再单独加入。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from research_agent.config import Settings
from research_agent.models import build_role_model
from research_agent.study.graph import StudyServices, run_study

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "reviews_versions"

REQUEST = (
    "基于当前动态本体库，撰写一份骨修复/骨再生生物材料领域的文献综述。"
    "只使用本库中已有、可溯源的模式卡与证据卡，不执行外部检索，"
    "不得自造论文或证据。"
)

DATABASES = [
    ("v01_legacy", ROOT / "data" / "research_agent.db", "v0.0.1 历史库"),
    ("v02", ROOT / "data" / "ontology_v02.db", "v0.0.2"),
    ("v03", ROOT / "data" / "ontology_v03.db", "v0.0.3"),
    ("v04", ROOT / "data" / "ontology_v04.db", "v0.0.4"),
    ("v05", ROOT / "data" / "ontology_v05.db", "v0.0.5"),
    ("v06", ROOT / "data" / "ontology_v06.db", "v0.0.6"),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="按动态本体快照生成综述（不触发外部检索）")
    ap.add_argument("--skip-review-llm", action="store_true",
                    help="审核使用确定性门控，不调用 GLM")
    ap.add_argument("--start", type=int, default=0, help="从第几个数据库开始")
    ap.add_argument("--end", type=int, default=999, help="到第几个数据库结束(不含)")
    args = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    models = {
        role: build_role_model(role)
        for role in ("planner", "content", "review")
    }
    if args.skip_review_llm:
        models["review"] = None
    review_backend = "glm-4.7-flash" if models["review"] is not None else "deterministic_gate"
    failures: list[str] = []
    selected = DATABASES[args.start:args.end]
    for key, db_path, label in selected:
        if not db_path.exists():
            failures.append(f"{key}: db not found")
            continue
        print(f"[{label}] mining {db_path.name} ...", flush=True)
        settings = Settings(db_path=db_path)
        services = StudyServices(
            planner_model=models["planner"],
            content_model=models["content"],
            review_model=models["review"],
            settings=settings,
        )
        try:
            out = run_study(REQUEST, services)
        except Exception as exc:  # noqa: BLE001
            print(f"[{label}] ERROR {exc}", flush=True)
            failures.append(f"{key}: {exc}")
            continue
        if out.get("status") == "needs_collection":
            failures.append(f"{key}: needs_collection")
            continue
        draft = out.get("draft") or {}
        review = out.get("review") or {}
        knowledge = out.get("knowledge") or {}
        markdown = (draft.get("markdown") or "").strip()
        if not markdown:
            failures.append(f"{key}: empty draft")
            continue
        corpus = knowledge.get("corpus") or {}
        header = "\n".join([
            f"> 数据源: `{db_path.name}` ({label})",
            f"> 快照规模: papers={corpus.get('papers', 0)} | "
            f"nodes={corpus.get('nodes', 0)} | edges={corpus.get('edges', 0)}",
            f"> 审核: {review.get('decision', 'unknown')} "
            f"({review_backend})",
            f"> 生成时间: {datetime.now().isoformat(timespec='seconds')}",
            "",
        ])
        target = OUT / f"{key}_literature_review.md"
        target.write_text(header + markdown + "\n", encoding="utf-8")
        summary = OUT / f"{key}_evidence.json"
        summary.write_text(
            json.dumps({
                "database": str(db_path),
                "label": label,
                "decision": review.get("decision"),
                "review_backend": review_backend,
                "issues": review.get("issues", []),
                "corpus": corpus,
                "patterns": len(knowledge.get("patterns") or []),
                "evidence": len(knowledge.get("evidence") or []),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[{label}] saved {target.name}", flush=True)
    if failures:
        print("failures:", *failures, sep="\n- ", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
