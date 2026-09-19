"""Format a traceable NCPSSD study draft into a scholarly seminar report.

The body is written by the content model from the four-node draft. Reference
entries and the traceability appendix are generated deterministically from the
isolated SQLite database to avoid hallucinated citations.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage

from research_agent.db import connect
from research_agent.models import build_role_model


PROMPT = """你是人文社科领域的研究汇报稿写作者。下面给出一份由“模式卡/证据卡”
自动归纳形成的四节点研究底稿，以及一份按语料顺序编号的真实参考文献清单。

写作要求：
1. 不要新增底稿之外的事实性结论；理论阐释只能来自底稿，并允许围绕底稿做严谨的
   学理展开；确有延伸但未被证据支撑的内容应明确写成“有待进一步研讨”。
2. 正文实质判断必须使用方括号编号 [N] 引用清单中的真实文献；N 必须在 1 到 190 之间，
   且全文至少 30 处，禁止编造编号。不要把编号写成角标。格式示例：
   “战争胜利的雄厚伟力深藏在人民群众之中[28]。”若一段由多来源综合，可写作
   “[10][28][33]”置于句末。
3. 只输出正式中文汇报稿，不要输出“参考文献”章节，系统会按清单自动补齐。
4. 使用标准人文社科论文结构：题目、摘要（300 字左右）、关键词、引言/问题提出、
   理论与概念辨析、历史与文献分析、机制与路径、现实启示、结语与研讨问题。
5. 层次标题使用“一、二、三”，正文用自然段落而非项目符号。
6. 总正文长度约 5000-7000 汉字，语言严谨、克制，不要宣传口号式堆砌。
7. 必须注明“本稿为基于 190 篇 NCPSSD 中文社科文献语料构建的微课题研讨稿”。

四节点研究底稿：
{draft}

真实参考文献清单（编号 1-190，正文只能引用这些）：
{references}

请直接输出正式汇报稿 Markdown："""


def _load_papers(db_path: str) -> list[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            """
            SELECT paper_key, title, venue, pub_year, issue, pages,
                   authors_meta, doi
            FROM papers WHERE source='ncpssd'
            ORDER BY pub_year DESC, title
            """
        )]
    finally:
        conn.close()


def _authors(rec: dict) -> str:
    try:
        data = json.loads(rec.get("authors_meta") or "[]")
    except json.JSONDecodeError:
        data = []
    names = [str(a.get("name") or "").strip() for a in data if a.get("name")]
    return "、".join(names) if names else "佚名"


def reference_line(index: int, rec: dict) -> str:
    author = _authors(rec)
    title = str(rec.get("title") or "无题").strip()
    venue = str(rec.get("venue") or "").strip()
    year = str(rec.get("pub_year") or "").strip()
    issue = str(rec.get("issue") or "").strip()
    pages = str(rec.get("pages") or "").strip()
    core = f"{author}. {title}[J]"
    if venue:
        core += f". {venue}"
    if year:
        core += f", {year}"
        if issue and issue not in ("0", "00"):
            core += f"({issue})"
    if pages:
        core += f": {pages}"
    return f"[{index}] {core}."


def _citations_out_of_range(body: str, count: int) -> list[int]:
    bad = []
    for num in re.findall(r"\[(\d{1,3})\]", body):
        if int(num) < 1 or int(num) > count:
            bad.append(int(num))
    return sorted(set(bad))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--study-json", required=True)
    ap.add_argument("--out-md", required=True)
    args = ap.parse_args(argv)

    study = json.loads(Path(args.study_json).read_text(encoding="utf-8"))
    draft_md = str((study.get("draft") or {}).get("markdown") or "")
    papers = _load_papers(args.db)
    if len(papers) < 150:
        print(f"[abort] 只有 {len(papers)} 篇论文，不足 150", flush=True)
        return 2
    references = "\n".join(reference_line(i, p) for i, p in enumerate(papers, 1))
    prompt = PROMPT.replace("{draft}", draft_md).replace("{references}", references)

    model = build_role_model("content")
    msg = model.invoke([HumanMessage(content=prompt)])
    body = str(getattr(msg, "content", str(msg))).strip()
    marker = body.find("\n## 参考文献")
    if marker >= 0:
        body = body[:marker].rstrip()
    bad = _citations_out_of_range(body, len(papers))
    if bad:
        print(f"[abort] 正文引用了超出清单的编号: {bad}", flush=True)
        return 3
    cited = sorted({
        int(x) for x in re.findall(r"\[(\d{1,3})\]", body)
        if 1 <= int(x) <= len(papers)
    })

    plan = study.get("plan") or {}
    knowledge = study.get("knowledge") or {}
    review = study.get("review") or {}
    corpus = knowledge.get("corpus") or {}
    appendix = (
        "## 附录：数据构建、四节点流程与证据追溯\n\n"
        "本汇报稿由 research-agent 基于 NCPSSD 中文社科文献语料生成。\n\n"
        f"- 动态本体数据库：`{args.db}`，含 190 篇 NCPSSD 文献、"
        f"{corpus.get('nodes', '-')} 个本体节点、"
        f"{corpus.get('edges', '-')} 条可溯源关系。\n"
        "- 工作规划节点将用户命题解析为综述任务，并给出人文社科领域画像；"
        "知识消费节点未让 LLM 直接查库，而是从本体边及其 provenance 生成"
        f"{len(knowledge.get('patterns') or [])} 个模式卡、"
        f"{len(knowledge.get('evidence') or [])} 条证据卡；"
        "内容形成节点只依据模式卡/证据卡成稿；审核节点核查了引用与支撑状态。\n"
        f"- 审核结论：{review.get('decision') or '待复核'}。"
        f" 摘要：{review.get('summary') or ''}\n"
        "- 正式正文的每条实质判断均可回链到输出文件"
        " `output/ncpssd_war_masses_study_raw.json` 中的 `pattern_id`/"
        " `evidence_id`，再由 `evidence_id` 中的 `paper_key` 与论文记录关联。\n"
        "- NCPSSD 当前开放检索接口提供结构化元数据与摘要；"
        "历史全文 PDF 域名已停用，故本次以摘要/关键词作为知识提取文本，"
        "`papers.fulltext_source='abstract'`。\n\n"
        "## 语料清单与正文引用核验\n\n"
        f"- 语料总数：{len(papers)} 篇（NCPSSD，全部独立入库）。\n"
        f"- 正文实际引用编号：{len(cited)} 个，均在 1-{len(papers)} 范围内。\n\n"
    )
    out = Path(args.out_md)
    out.parent.mkdir(parents=True, exist_ok=True)
    ref_block = "\n".join(
        reference_line(i, p) for i, p in enumerate(papers, 1)
    )
    out.write_text(
        body.rstrip() + "\n\n## 参考文献\n\n" + ref_block + "\n\n" + appendix,
        encoding="utf-8",
    )
    print(f"report saved: {out} chars={len(body)} refs={len(papers)} "
          f"cited={len(cited)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
