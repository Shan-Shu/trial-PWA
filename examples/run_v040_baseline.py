"""v0.4.1 端到端基线脚本：计划 → 消费 → 内容 → 审核 → 事实核查。

与旧 `output/run_v031_test.py` 的差别：
1. 注入五个真实模型（含 consumer 与 fact_check）；
2. 可选注入 collector（--collect）真正执行 Planner 的检索计划；
3. 落盘 plan / consumer_analysis / design_context / draft / review / fact_check
   以及逐候选的机制与等级对照，便于和 v0.3.1 做同请求对照；
4. 中间态同时写入数据库 `study_runs` 表。

用法::

    python examples/run_v040_baseline.py \
        --db data/ynamide_multiazabicycle_v020.db \
        --out-dir output/v040_baseline \
        --request "提出几个炔酰胺合成多元氮杂环化合物的新方法..."

    # 只消费本地已有语料（不联网检索）
    python examples/run_v040_baseline.py --no-collect
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from research_agent.config import Settings  # noqa: E402
from research_agent.models import build_role_model, role_model_binding  # noqa: E402
from research_agent.study.graph import StudyServices, run_study  # noqa: E402

DEFAULT_REQUEST = (
    "提出几个炔酰胺合成多元氮杂环化合物的新方法，按 ACS 格式相关要求形成结果。"
    "请提出至少 4 个差异化候选，并给出依据、风险评估和最小验证实验。")


def build_services(settings: Settings, *, collect: bool,
                   source: str) -> StudyServices:
    roles = ("planner", "consumer", "content", "review", "fact_check")
    models: dict[str, object] = {}
    errors: list[str] = []
    for role in roles:
        try:
            models[role] = build_role_model(role)
        except Exception as exc:  # noqa: BLE001
            models[role] = None
            errors.append(f"{role}: {exc}")
    if errors:
        print("[LLM 绑定失败]")
        for error in errors:
            print(f"  - {error}")
        raise SystemExit(2)

    collector = None
    if collect:
        from research_agent.pipeline import Services as PipelineServices
        from research_agent.retrieval.api_clients import ApiHub
        from research_agent.study.collection import collect_mission

        pipeline_services = PipelineServices(
            api=ApiHub(source=source),
            retriever_model=build_role_model("retriever"),
            quality_model=build_role_model("quality"),
            knowledge_model=build_role_model("knowledge"),
            settings=settings,
        )
        collector = lambda req: collect_mission(  # noqa: E731
            req, services=pipeline_services)

    return StudyServices(
        planner_model=models["planner"],
        consumer_model=models["consumer"],
        content_model=models["content"],
        review_model=models["review"],
        fact_check_model=models["fact_check"],
        settings=settings,
        collector=collector,
    )


def summarize(out: dict) -> dict:
    knowledge = out.get("knowledge") or {}
    design = knowledge.get("design_context") or {}
    draft = out.get("draft") or {}
    review = out.get("review") or {}
    fact = out.get("fact_check") or {}
    strategies = [s for s in (draft.get("strategies") or []) if s.get("rank")]
    return {
        "status": out.get("status"),
        "decision": review.get("decision"),
        "overall_score": review.get("overall_score"),
        "fact_check": fact.get("decision"),
        "fact_check_issues": len(fact.get("issues") or []),
        "collection_rounds": out.get("collection_rounds"),
        "collection_count": (out.get("collection_report") or {}).get("count"),
        "knowledge": {
            "patterns": len(knowledge.get("patterns") or []),
            "evidence": len(knowledge.get("evidence") or []),
            "hyperedges": len(knowledge.get("hyperedges") or []),
            "coverage_score": knowledge.get("coverage_score"),
        },
        "design_context": {
            "mechanism_states": len(design.get("mechanism_states") or []),
            "reaction_primitives": len(design.get("reaction_primitives") or []),
            "opportunity_gaps": len(design.get("opportunity_gaps") or []),
            "operator_candidates": len(design.get("operator_candidates") or []),
            "constraint_conflicts": len(design.get("constraint_conflicts") or []),
            "traceability": design.get("traceability"),
            "mode": design.get("mode"),
        },
        "candidate_pool": draft.get("candidate_pool") or {},
        "selection": draft.get("selection") or {},
        "candidates": [
            {
                "rank": s.get("rank"),
                "id": s.get("id"),
                "title": s.get("title"),
                "innovation_level": s.get("innovation_level"),
                "operator_chain": [x.get("operator") for x in
                                   (s.get("operator_chain") or [])],
                "chain_level_basis": (s.get("operator_chain_validation") or {}).get(
                    "level_basis"),
                "chain_breaks": len((s.get("operator_chain_validation") or {}).get(
                    "chain_breaks") or []),
                "differentiation": s.get("differentiation"),
                "satisfies_constraints": s.get("satisfies_constraints"),
            }
            for s in strategies
        ],
        "review_design_failures": [
            {"requirement": r.get("requirement"), "reason": r.get("reason")}
            for r in (review.get("instruction_compliance") or {}).get(
                "requirements") or []
            if r.get("category") == "design" and r.get("status") != "met"
        ],
        "revision_actions": review.get("revision_actions") or [],
        "revision_responses": draft.get("revision_responses") or [],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="v0.4.1 端到端基线（真实 LLM）")
    ap.add_argument("--request", default=DEFAULT_REQUEST)
    ap.add_argument("--db", default="data/ynamide_multiazabicycle_v020.db")
    ap.add_argument("--out-dir", default="output/v040_baseline")
    ap.add_argument("--collect", action="store_true",
                    help="执行 Planner 的检索计划（联网抓取并建库）")
    ap.add_argument("--source", default="fulltext")
    ap.add_argument("--max-results", type=int, default=None)
    ap.add_argument("--max-candidates", type=int, default=None,
                    help="覆盖候选池种子数（默认取 Planner budget.max_candidates）")
    ap.add_argument("--content-mode", choices=["llm", "deterministic"],
                    default="llm",
                    help="内容形成节点用真实模型还是确定性兜底（后者用于快速验证图接线）")
    ap.add_argument("--persist", action="store_true", default=True)
    args = ap.parse_args(argv)

    settings = Settings.from_env()
    settings.db_path = Path(args.db)
    services = build_services(settings, collect=args.collect, source=args.source)
    if args.content_mode == "deterministic":
        services.content_model = None
        print("[内容形成节点] 确定性兜底（--content-mode deterministic）")
    for binding in services.binding_summary():
        print(f"[{binding['label']}] {binding['provider']}/{binding['model']}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now().isoformat(timespec="seconds")
    print(f"[{started}] 开始 | DB={settings.db_path} | collect={args.collect}",
          flush=True)
    try:
        out = run_study(args.request, services=services,
                        max_results_override=args.max_results,
                        persist=args.persist,
                        budget_override=(
                            {"max_candidates": args.max_candidates}
                            if args.max_candidates else None))
    except Exception:
        traceback.print_exc()
        raise

    (out_dir / "result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    for name, key in (("plan", "plan"), ("consumer", "knowledge"),
                      ("review", "review"), ("fact_check", "fact_check")):
        payload = out.get(key)
        if key == "knowledge":
            payload = {
                "consumer_analysis": (payload or {}).get("consumer_analysis"),
                "design_context": (payload or {}).get("design_context"),
                "coverage_score": (payload or {}).get("coverage_score"),
            }
        (out_dir / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
    draft = out.get("draft") or {}
    (out_dir / "draft.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    header = "\n".join([
        "# v0.4.1 基线成稿",
        "",
        f"- 运行时间: {started}",
        f"- 数据库: `{settings.db_path}`",
        f"- run_id: `{out.get('run_id')}`",
        f"- 请求: {args.request}",
        f"- 状态: {out.get('status')}",
        f"- 审核结论: {(out.get('review') or {}).get('decision')}",
        f"- 事实核查: {(out.get('fact_check') or {}).get('decision')}",
        "",
        "---",
        "",
    ])
    (out_dir / "draft.md").write_text(
        header + (draft.get("markdown") or "（无内容）") + "\n", encoding="utf-8")
    summary = summarize(out)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"\n产物目录: {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
