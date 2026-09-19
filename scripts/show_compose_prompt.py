"""看清"成段时模型到底拿到什么"——只读诊断，**不调模型、不花配额**。

为什么需要它：正文空泛的根因不在提示词的措辞，而在**喂进去的东西**。此前
模型收到的是"N 项条件"这类计数，且知识消费节点归纳的机制状态与算子链在成段
这一步被整体丢弃——于是只能写出"在优化条件下"这种话。

这个脚本用真实库把提示词**原样渲染出来**，让你直接判断素材够不够具体：
- 【可引用素材】里有没有真实数值（温度/当量/溶剂/收率…）；
- 【知识消费节点产出】里有没有机制状态与候选算子链（带 H-xxxx 溯源编号）。

实现上用一个"捕获型假模型"接住提示词后立刻结束，因此**绝不联网**。
库是复制出来的副本，正式库只读。

用法：
    uv run python scripts/show_compose_prompt.py [--project N] [--section KEY]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace",
                            line_buffering=True)
    except (AttributeError, ValueError):
        pass

os.environ.setdefault("RA_LOG_STDERR", "0")

SRC_DB = ROOT / "data" / "desk.db"
TMP_DB = ROOT / "data" / "compose_probe.db"


class _CaptureModel:
    """接住提示词就返回固定文本：验证"模型能拿到什么"而不真的调用模型。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, messages):  # noqa: ANN001
        self.prompts.append(str(getattr(messages[-1], "content", messages[-1])))
        return type("Msg", (), {"content": "（诊断用占位正文 [1]）"})()


def _slice(prompt: str, start_marker: str, end_marker: str) -> str:
    start = prompt.find(start_marker)
    if start < 0:
        return ""
    end = prompt.find(end_marker, start + len(start_marker))
    return prompt[start:end if end > 0 else len(prompt)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=int, default=0)
    ap.add_argument("--section", default="")
    args = ap.parse_args()

    if not SRC_DB.exists():
        print(f"跳过：没有真实库 {SRC_DB}")
        return 0

    from research_agent.config import Settings
    from research_agent.db import connect
    from research_agent.writing import section_service as sections
    from research_agent.writing.service import list_sections

    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(TMP_DB) + suffix)
        if stale.exists():
            stale.unlink()
    print(f"复制真实库 → {TMP_DB.name}（{SRC_DB.stat().st_size / 1048576:.1f} MB）")
    shutil.copyfile(SRC_DB, TMP_DB)

    conn = connect(TMP_DB)
    try:
        if args.project:
            project_id = int(args.project)
        else:
            row = conn.execute(
                "SELECT project_id FROM writing_sections "
                "GROUP BY project_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
            if not row:
                print("库里还没有写作项目")
                return 0
            project_id = int(row["project_id"])
        secs = list_sections(conn, project_id)
        if args.section:
            chosen = next((s for s in secs
                           if str(s.get("section_key")) == args.section), None)
        else:
            chosen = secs[0] if secs else None
        if not chosen:
            print(f"项目 {project_id} 没有可用大纲节点")
            return 0
        key = str(chosen.get("section_key"))
        heading = str(chosen.get("heading") or key)
        prow = conn.execute(
            "SELECT topic, title, genre FROM writing_projects WHERE project_id=?",
            (project_id,)).fetchone()
        print(f"项目 {project_id} · 部分 {key}（{heading}）")
        if prow:
            print(f"  主题：{prow['topic'] or '（空）'}｜标题：{prow['title'] or ''}"
                  f"｜体裁：{prow['genre'] or '（空）'}")
    finally:
        conn.close()

    model = _CaptureModel()
    settings = Settings(db_path=str(TMP_DB))
    # `skip_judgement=True` 正是**写作台的真实路径**：访谈闭环已经判过支撑，
    # 这一步只做"消费 + 成段"。同时也意味着**不会触发补检/外网检索**
    # （否则判定为不足时会真的去打 arXiv/Tavily，脚本会挂几分钟）。
    result = sections.run_section_workflow(
        project_id=project_id, section_key=key, instruction="",
        db_path=str(TMP_DB), settings=settings, compose_model=model,
        skip_judgement=True,
    )
    print(f"工作流状态：{result.get('status')}（判定 {result.get('decision')}）")
    verdict = result.get("sufficiency") or {}
    counts = verdict.get("counts") or {}
    if counts:
        print("判定计数：" + "，".join(f"{k}={v}" for k, v in counts.items()
                                     if not isinstance(v, list)))
    print(f"命中文献 {len(verdict.get('paper_keys') or [])} 篇，"
          f"证据编号 {len(verdict.get('evidence_ids') or [])} 条")
    if not (verdict.get("paper_keys") or []):
        print("  ⚠ 判定命中文献为 0：素材已由证据编号回溯归属文献兜底，"
              "但判定仍会显示「同主题文献不足」")
        print("    注意：本脚本**故意让规划器离线**（planner_model=None）。"
              "中文主题在英文库里，离线 Planner 切出的是中文检索词，"
              "LIKE 必然 0 命中；联网时 Planner 会产出英文检索词。")
    if not model.prompts:
        print("成段节点没有产出提示词（可能在更早的环节就停了）")
        return 0

    prompt = model.prompts[-1]
    print("\n" + "=" * 78)
    print("【可引用素材】——看有没有真实数值")
    print("=" * 78)
    print(_slice(prompt, "【可引用素材】", "【知识消费节点产出】").strip() or "（无）")

    print("\n" + "=" * 78)
    print("【知识消费节点产出】——看有没有机制状态与算子链")
    print("=" * 78)
    print(_slice(prompt, "【知识消费节点产出】", "【判定依据】").strip() or "（无）")

    # 粗粒度体检：数值与单位是否真的出现（比"看起来很长"有意义）
    import re
    units = re.findall(r"\d+(?:\.\d+)?\s*(?:°C|℃|equiv|mol%|hours?|h\b|%|mL|equiv)", prompt)
    print("\n" + "-" * 78)
    print(f"体检：提示词长度 {len(prompt)} 字符，"
          f"检测到 {len(units)} 处带单位的数值")
    if units:
        print("  例：" + "、".join(units[:12]))
    if "项条件" in prompt or "项测量" in prompt:
        print("  ⚠ 仍然出现「项条件/项测量」这类计数——应只给数值")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
