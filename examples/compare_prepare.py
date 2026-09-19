"""为提示词对比实验准备隔离的新数据库 data/ontology_v02.db。

做法（与旧库完全隔离）：
1. 从旧库复制同主题 PubMed 文献（仅元数据+精校文本，不含 PDF BLOB）；
2. 用「新版检索提示词」（RetrievalLLM.plan_queries）生成 4-6 条子领域检索式，
   经 PubMed 补齐，直到新库 ≥300 篇；
3. 全程只写 data/ontology_v02.db，不触碰现有 data/research_agent.db。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from research_agent.config import Settings
from research_agent.db import connect, get_paper, upsert_paper
from research_agent.models import build_chat_model
from research_agent.ontology.store import init_ontology
from research_agent.retrieval.api_clients import ApiHub
from research_agent.retrieval.llm import RetrievalLLM
from research_agent.retrieval.node import ingest_search_results

OLD_DB = Path(__file__).resolve().parents[1] / "data" / "research_agent.db"
NEW_DB = Path(__file__).resolve().parents[1] / "data" / "ontology_v02.db"
TOPIC = ("新型骨修复生物材料 / novel biomaterials for bone repair "
         "(bone regeneration, bone defect repair, osteogenesis)")
TARGET = 300
COPY_OLD = 250


def main() -> int:
    old_conn = connect(OLD_DB)
    new_settings = Settings(db_path=NEW_DB)
    new_conn = connect(new_settings.db_path)
    init_ontology(new_conn)

    # 1) 复制旧库同主题 PubMed 文献（250 篇）
    keys = [
        r["paper_key"] for r in old_conn.execute(
            "SELECT paper_key FROM papers WHERE source='pubmed' "
            "ORDER BY created_at ASC LIMIT ?", (COPY_OLD,)
        )
    ]
    for k in keys:
        rec = get_paper(old_conn, k)
        if not rec:
            continue
        for f in ("pdf_blob", "pdf_sha256", "pdf_size"):
            rec[f] = None
        upsert_paper(new_conn, rec)
    print(f"已从旧库复制 {len(keys)} 篇 -> 新库", flush=True)
    old_conn.close()

    # 2) 用新版检索提示词规划检索式并补齐
    model = build_chat_model(provider="deepseek", model_name="deepseek-v4-flash")
    planner = RetrievalLLM(model, max_queries=6)
    queries = planner.plan_queries(TOPIC)
    print(f"新提示词规划的检索式({len(queries)}):", flush=True)
    for q in queries:
        print("  -", q, flush=True)
    with open(Path(__file__).resolve().parents[1] / "data" / "v02_queries.json",
              "w", encoding="utf-8") as f:
        json.dump({"topic": TOPIC, "queries": queries}, f, ensure_ascii=False, indent=2)

    api = ApiHub(source="pubmed")
    qi = 0
    while new_conn.execute("SELECT COUNT(*) FROM papers WHERE source='pubmed'"
                           ).fetchone()[0] < TARGET and qi < 12:
        q = queries[qi % len(queries)]
        qi += 1
        res = ingest_search_results(q, 20, api=api, model=None,
                                    settings=new_settings)
        n = new_conn.execute("SELECT COUNT(*) FROM papers WHERE source='pubmed'"
                             ).fetchone()[0]
        print(f"[补齐 {qi}] 查询: {q[:70]} | 新增 {len(res['paper_keys'])} | "
              f"新库累计 {n}", flush=True)
    new_conn.close()
    total = connect(new_settings.db_path).execute(
        "SELECT COUNT(*) FROM papers WHERE source='pubmed'").fetchone()[0]
    print(f"=== 新库就绪: {total} 篇 PubMed（目标 ≥{TARGET}）===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
