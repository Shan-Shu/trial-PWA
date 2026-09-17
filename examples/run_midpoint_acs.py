"""半程测试：以指定版本数据库生成 ACS 格式的纯净综述论文与新方法报告。

两件事：
1. 走当前研究链路（v0.4.1：Planner → collection → Consumer → 内容 → 审核 → 事实核查）
   产出内部草稿（引用为库内 P-/E-/H- 编号）；
2. 用 `research_agent.study.acs_format` 把内部草稿转换为面向读者的 ACS 纯净文稿：
   正文无库内编号、顺序数字引用、文末 References、无系统术语与占位符。

用法::

    # 综述论文
    python examples/run_midpoint_acs.py --task review \
        --db data/ynamide_multiazabicycle_v020.db \
        --out-dir output/midpoint_acs

    # 新方法报告
    python examples/run_midpoint_acs.py --task methods \
        --db data/ynamide_multiazabicycle_v020.db \
        --out-dir output/midpoint_acs
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from research_agent.config import Settings  # noqa: E402
from research_agent.db import connect, get_paper  # noqa: E402
from research_agent.models import build_role_model, role_model_binding  # noqa: E402
from research_agent.study.acs_format import to_acs_document  # noqa: E402
from research_agent.study.graph import StudyServices, run_study  # noqa: E402

REVIEW_REQUEST = (
    "写一份题为《炔酰胺合成多元氮杂环化合物研究进展》的 ACS 格式综述论文。"
    "要求：以给定文献库为唯一事实来源，按 ACS 期刊格式组织（摘要、引言、"
    "结构特征与反应性、成环与环加成策略、催化体系与条件、区域与立体选择性、"
    "代表性骨架与应用、挑战与展望、结论），每条实质结论都要有文献依据，"
    "不得编造库内不存在的文献或数据，无法支撑的部分明确写成开放问题。"
)
METHODS_REQUEST = (
    "提出 4-6 个差异化、机制层面创新的炔酰胺合成多元氮杂环化合物新方法候选。"
    "每个候选必须给出：机制算子序列（算子链）、目标骨架、与其余候选的本质差异、"
    "创新来源、可行性理由、至少两条风险、最小验证实验方案，以及逐条回应"
    "目标硬约束（含至少两个环内氮原子、环拓扑明确、第二个氮原子的引入步骤明确）。"
    "所有依据必须来自给定文献库，按 ACS 格式给出引用。"
)

TASKS = {
    "review": {
        "request": REVIEW_REQUEST,
        "title": "炔酰胺合成多元氮杂环化合物研究进展",
        "basename": "ynamide_polyaza_review_acs",
        "include_candidates": False,
        "max_candidates": 6,
        "max_review_rounds": 1,
        "review_target_chars": 3000,
    },
    "methods": {
        "request": METHODS_REQUEST,
        "title": "炔酰胺合成多元氮杂环化合物的新方法",
        "basename": "ynamide_polyaza_new_methods_acs",
        "include_candidates": True,
        "max_candidates": 8,
        "max_review_rounds": 2,
    },
}


def collect_papers(db: Path, paper_keys: set[str]) -> dict[str, dict]:
    conn = connect(db)
    try:
        out = {}
        for key in paper_keys:
            rec = get_paper(conn, key)
            if rec:
                out[key] = rec
        return out
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="半程测试：ACS 纯净产物生成")
    ap.add_argument("--task", choices=sorted(TASKS), required=True)
    ap.add_argument("--db", default="data/ynamide_multiazabicycle_v020.db")
    ap.add_argument("--out-dir", default="output/midpoint_acs")
    ap.add_argument("--request", default="")
    ap.add_argument("--max-candidates", type=int, default=0)
    ap.add_argument("--max-review-rounds", type=int, default=0,
                    help="审核最大轮数；综述类任务建议 1，避免每轮 5-10 分钟的无效重生成")
    ap.add_argument("--review-target-chars", type=int, default=0,
                    help="综述目标中文字符数（默认取 TASKS 配置，通常 3000）")
    args = ap.parse_args(argv)

    spec = dict(TASKS[args.task])
    request = args.request or spec["request"]
    max_candidates = args.max_candidates or spec["max_candidates"]
    max_review_rounds = args.max_review_rounds or spec.get("max_review_rounds", 3)
    db = Path(args.db)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = Settings.from_env()
    settings.db_path = db
    models = {}
    for role in ("planner", "consumer", "content", "review", "fact_check"):
        models[role] = build_role_model(role)
    services = StudyServices(
        planner_model=models["planner"],
        consumer_model=models["consumer"],
        content_model=models["content"],
        review_model=models["review"],
        fact_check_model=models["fact_check"],
        settings=settings,
        collector=None,
    )
    print(f"[{args.task}] DB={db}", flush=True)
    for binding in services.binding_summary():
        print(f"  [{binding['label']}] {binding['provider']}/{binding['model']}",
              flush=True)

    started = datetime.now().isoformat(timespec="seconds")
    t0 = time.time()
    out = run_study(request, services=services,
                    budget_override={
                        "max_candidates": max_candidates,
                        "review_target_chars": (args.review_target_chars
                                                or spec.get("review_target_chars", 0)),
                    },
                    max_review_rounds=max_review_rounds,
                    persist=True)
    elapsed = round(time.time() - t0, 1)
    print(f"[{args.task}] run_study 完成 status={out.get('status')} "
          f"耗时={elapsed}s", flush=True)

    knowledge = out.get("knowledge") or {}
    draft = out.get("draft") or {}

    # 收集草稿里出现的所有库内编号，只取这些编号对应的论文元数据
    paper_keys: set[str] = set()
    for pattern in knowledge.get("patterns") or []:
        paper_keys.update(str(k) for k in pattern.get("paper_keys") or [] if k)
    for ev in knowledge.get("evidence") or []:
        if ev.get("paper_key"):
            paper_keys.add(str(ev["paper_key"]))
    for hyper in knowledge.get("hyperedges") or []:
        for ev in hyper.get("evidence") or []:
            if ev.get("paper_key"):
                paper_keys.add(str(ev["paper_key"]))
    papers = collect_papers(db, paper_keys)

    converted = to_acs_document(
        draft, knowledge, papers,
        title=spec["title"],
        subtitle=(f"半程测试产物 ｜ 语料库：`{db.name}`（v0.3.1 fresh 库 / "
                  f"v0.3.0 语义域修复 + 1012 篇入库；{len(papers)} 篇已用论文元数据）"),
        header_notes=[
            f"生成时间：{started} ｜ 研究链路：v0.4.1（Planner/Consumer/Content/Reviewer/FactCheck，"
            f"deepseek-v4-pro）",
            "引用体系：正文为 ACS 顺序数字上标，完整文献信息见文末 References；"
            "所有引用均可回溯到语料库中的真实论文。",
        ],
        keep_candidate_section=spec["include_candidates"],
    )

    base = out_dir / spec["basename"]
    base.with_suffix(".md").write_text(converted["markdown"], encoding="utf-8")
    (out_dir / f"{spec['basename']}.citation_map.json").write_text(
        json.dumps(converted["citation_map"], ensure_ascii=False, indent=2),
        encoding="utf-8")
    (out_dir / f"{spec['basename']}.checks.json").write_text(
        json.dumps(converted["checks"], ensure_ascii=False, indent=2),
        encoding="utf-8")
    (out_dir / f"{spec['basename']}.internal_draft.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    (out_dir / f"{spec['basename']}.consumer.json").write_text(
        json.dumps({
            "consumer_analysis": knowledge.get("consumer_analysis"),
            "design_context": knowledge.get("design_context"),
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / f"{spec['basename']}.run.json").write_text(
        json.dumps({
            "task": args.task,
            "request": request,
            "run_id": out.get("run_id"),
            "status": out.get("status"),
            "decision": (out.get("review") or {}).get("decision"),
            "fact_check": (out.get("fact_check") or {}).get("decision"),
            "elapsed_sec": elapsed,
            "stats": converted["stats"],
            "checks": converted["checks"],
            "candidate_pool": draft.get("candidate_pool"),
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "task": args.task,
        "status": out.get("status"),
        "elapsed_sec": elapsed,
        "stats": converted["stats"],
        "checks": converted["checks"],
    }, ensure_ascii=False, indent=2), flush=True)
    print(f"产物：{base.with_suffix('.md').resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
