"""Run the four-node study layer against an NCPSSD-built ontology database."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from research_agent.config import Settings
from research_agent.models import build_role_model
from research_agent.study.graph import StudyServices, run_study


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--request", default=(
        "如何理解“战争胜利的雄厚伟力，深藏在人民群众之中”"))
    ap.add_argument("--review", choices=["glm", "deterministic"],
                    default="deterministic")
    args = ap.parse_args(argv)

    settings = Settings(db_path=Path(args.db))
    review_model = None
    if args.review == "glm":
        review_model = build_role_model("review")
    services = StudyServices(
        planner_model=build_role_model("planner"),
        content_model=build_role_model("content"),
        review_model=review_model,
        settings=settings,
    )
    out = run_study(args.request, services)
    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    draft = out.get("draft") or {}
    md = draft.get("markdown") or ""
    Path(args.out_md).write_text(md, encoding="utf-8")

    review = out.get("review") or {}
    print("status:", out.get("status"))
    print("decision:", review.get("decision"))
    print("review:", review.get("summary"))
    print("draft_chars:", len(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
