"""为看板准备一份有内容的演示库（幂等，可反复运行）。

    uv run python scripts/seed_dashboard_db.py --db data/dashboard_demo.db

内容：跨来源论文若干（含中英文、有/无年份、被引高低、会议/预印本）、
质量结果（覆盖 direct/flagged/human 三档）、标签/文件夹/收藏、
动态本体（节点/边/超边，含条件与测量）、处理日志与一条研究运行记录。
目的：让合并后的 9 个 tab 都有东西可看，而不是空界面。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from research_agent.db import (  # noqa: E402
    connect,
    log_event,
    save_quality_result,
    save_study_run,
    upsert_paper,
    utcnow,
)
from research_agent.library.store import LibraryStore  # noqa: E402
from research_agent.ontology import store as ont  # noqa: E402

PAPERS = [
    {
        "paper_key": "europepmc:10.1002/anie.202312345",
        "source": "europepmc", "source_type": "journal",
        "title": "Gold-Catalysed Intermolecular Annulation of Ynamides with Nitriles",
        "abstract": ("A gold(I) complex bearing a bulky phosphine ligand catalyses the "
                     "intermolecular annulation of N-sulfonyl ynamides with nitriles, "
                     "giving pyrimidine derivatives in up to 92% yield. Mechanistic "
                     "studies support a keteniminium intermediate that is captured by "
                     "the nitrile nitrogen."),
        "doi": "10.1002/anie.202312345",
        "venue": "Angewandte Chemie (International ed. in English)",
        "venue_issn": "1521-3773", "pub_year": 2024, "pub_date": "2024-03-11",
        "publication_status": "Published", "citation_count": 41, "avg_h_index": 52.4,
        "authors": [{"name": "Zhang Wei"}, {"name": "Li Na"}, {"name": "Wang Lei"}],
        "volume": "63", "issue": "12", "pages": "e202312345",
        "keywords": "ynamide; annulation; gold catalysis; keteniminium",
        "publisher": "Wiley", "language": "en",
        "fulltext_source": "pdf", "status": "ingested",
    },
    {
        "paper_key": "europepmc:10.1039/D3SC04567A",
        "source": "europepmc", "source_type": "journal",
        "title": "Base-Mediated Dearomatisation for the Synthesis of N-Fused Heterocycles",
        "abstract": ("Potassium carbonate promotes dearomatisation of pyridinium salts, "
                     "enabling a cascade that delivers N-fused polycyclic heterocycles. "
                     "The method tolerates esters and aryl halides."),
        "doi": "10.1039/D3SC04567A",
        "venue": "Chemical Science",
        "pub_year": 2023, "citation_count": 18, "avg_h_index": 44.0,
        "authors": [{"name": "Chen Xi"}, {"name": "Liu Yang"}],
        "volume": "14", "issue": "33", "pages": "8821-8828",
        "language": "en", "fulltext_source": "xml", "status": "ingested",
    },
    {
        "paper_key": "arxiv:2402.01234",
        "source": "arxiv", "source_type": "preprint",
        "title": "Machine-Learned Transition-State Models for Nitrene Transfer",
        "abstract": ("We train a graph neural network on 12,000 DFT transition states "
                     "to predict barriers for nitrene transfer reactions with a mean "
                     "absolute error of 2.1 kcal/mol."),
        "doi": "", "venue": "arXiv",
        "pub_year": 2024, "citation_count": 3, "avg_h_index": 30.5,
        "authors": [{"name": "Novak Petr"}, {"name": "Okafor Adaeze"}],
        "language": "en", "fulltext_source": "pdf", "status": "ingested",
    },
    {
        "paper_key": "pubmed:39881234",
        "source": "pubmed", "source_type": "journal",
        "title": "Biodegradable Implant Materials for Bone Defect Repair: A Review",
        "abstract": ("We survey biodegradable scaffolds used in craniofacial bone "
                     "repair, comparing mechanical and resorption profiles across "
                     "eleven material classes."),
        "doi": "10.1016/j.biomaterials.2025.01.004",
        "venue": "Biomaterials",
        "pub_year": 2025, "citation_count": 7, "avg_h_index": 61.8,
        "authors": [{"name": "Haddad Rania"}, {"name": "Kim Soo-Jin"},
                    {"name": "Meyer Anke"}],
        "language": "en", "fulltext_source": "abstract", "status": "ingested",
    },
    {
        "paper_key": "ncpssd:W2600001",
        "source": "ncpssd", "source_type": "journal",
        "title": "战争与人民群众：基于历史文献的量化考察",
        "abstract": ("本文以地方志与档案汇编为基础，考察革命战争时期群众动员的组织形态与"
                     "地理分布，指出既有研究对基层动员成本的低估。"),
        "doi": "", "venue": "近代史研究",
        "pub_year": 2021, "citation_count": 2,
        "authors": [{"name": "周明"}, {"name": "李静"}],
        "volume": "3", "issue": "2", "pages": "45-67",
        "language": "zh", "fulltext_source": "abstract", "status": "ingested",
    },
    {
        "paper_key": "crossref:10.1021/acs.orglett.4c00123",
        "source": "crossref", "source_type": "journal",
        "title": "Transient Directing Groups in C–H Functionalisation of Heteroarenes",
        "abstract": ("Transient imine directing groups enable regioselective C–H "
                     "functionalisation without stoichiometric auxiliary removal."),
        "doi": "10.1021/acs.orglett.4c00123",
        "venue": "Organic Letters",
        "pub_year": None,                     # 刻意留空：验证「年份未知」档位
        "citation_count": 1,
        "authors": [{"name": "Rossi Giulia"}],
        "volume": "26", "issue": "4", "pages": "812-817",
        "language": "en", "fulltext_source": "abstract", "status": "ingested",
    },
    {
        "paper_key": "europepmc:10.1038/s41586-024-00001-x",
        "source": "europepmc", "source_type": "journal",
        "title": "A General Platform for Asymmetric Radical–Polar Crossover",
        "abstract": ("A dual catalytic platform couples photoredox initiation with "
                     "chiral Lewis acid trapping, delivering enantioenriched products "
                     "from simple olefins."),
        "doi": "10.1038/s41586-024-00001-x",
        "venue": "Nature",
        "pub_year": 2024, "citation_count": 96, "avg_h_index": 71.2,
        "authors": [{"name": "Okafor Adaeze"}, {"name": "Meyer Anke"},
                    {"name": "Zhang Wei"}, {"name": "Novak Petr"}],
        "volume": "628", "issue": "8009", "pages": "101-107",
        "language": "en", "fulltext_source": "pdf", "status": "ingested",
    },
    {
        "paper_key": "europepmc:10.1002/ejoc.202301999",
        "source": "europepmc", "source_type": "journal",
        "title": "Solvent-Free Mechanochemical Synthesis of Azoles",
        "abstract": ("Ball milling promotes cyclocondensation of hydrazides with "
                     "orthoesters without solvent, giving 1,2,4-triazoles in minutes."),
        "doi": "10.1002/ejoc.202301999",
        "venue": "European Journal of Organic Chemistry",
        "pub_year": 2023, "citation_count": 5,
        "authors": [{"name": "Tanaka Hiroshi"}],
        "volume": "26", "issue": "48", "pages": "e202301999",
        "language": "en", "fulltext_source": "abstract", "status": "human_review",
    },
    {
        # 第二篇金催化文献：写作台的"充分性判定"要求同一主题至少 2 篇可支撑，
        # 只有 1 篇时会被正确判为"缺数据"。加这篇是为了让演示库能走通
        # "判定充足 → 消费数据 → 成段"这条链路（否则演示项目只能看到不足分支）。
        "paper_key": "europepmc:10.1002/chem.202400777",
        "source": "europepmc", "source_type": "journal",
        "title": ("Ligand-Controlled Regioselectivity in Gold-Catalysed "
                  "Annulation of Ynamides"),
        "abstract": ("Bulky phosphine ligands switch the regioselectivity of "
                     "gold-catalysed ynamide annulation by favouring the "
                     "keteniminium pathway over direct carbene transfer; "
                     "annealed yields reach 88% with 5 mol% catalyst."),
        "doi": "10.1002/chem.202400777",
        "venue": "Chemistry – A European Journal",
        "pub_year": 2024, "citation_count": 42, "avg_h_index": 58.0,
        "authors": [{"name": "Ivanova Daria"}, {"name": "Chen Hao"}],
        "volume": "30", "issue": "12", "pages": "e202400777",
        "language": "en", "fulltext_source": "abstract", "status": "ingested",
    },
]

QUALITY = {
    "europepmc:10.1002/anie.202312345": (0.91, "direct", False,
                                         ["选题聚焦，机制证据充分"]),
    "europepmc:10.1039/D3SC04567A": (0.83, "direct", False, []),
    "arxiv:2402.01234": (0.62, "flagged", True, ["预印本，未经同行评审"]),
    "pubmed:39881234": (0.79, "flagged", True, ["综述型文献，非原始实验"]),
    "ncpssd:W2600001": (0.71, "flagged", True, ["中文社科语料，缺 DOI"]),
    "crossref:10.1021/acs.orglett.4c00123": (0.58, "flagged", True,
                                             ["年份缺失，元数据不完整"]),
    "europepmc:10.1038/s41586-024-00001-x": (0.95, "direct", False, []),
    "europepmc:10.1002/ejoc.202301999": (0.41, "human", True,
                                         ["质量分低于阈值，转人工审核"]),
    "europepmc:10.1002/chem.202400777": (0.88, "direct", False,
                                         ["配体效应证据完整"]),
}

#: (节点类型, 名称, 置信度, 别名)
NODES = [
    ("Reaction", "Intermolecular annulation", 0.92, []),
    ("Reaction", "Dearomatisation cascade", 0.86, ["去芳构化串联"]),
    ("Substrate", "N-sulfonyl ynamide", 0.90, ["ynamide", "炔酰胺"]),
    ("Substrate", "Pyridinium salt", 0.84, []),
    ("Catalyst", "Au(I) phosphine complex", 0.88, ["gold catalyst", "金催化剂"]),
    ("Catalyst", "K2CO3", 0.81, ["potassium carbonate"]),
    ("Ligand", "Bulky phosphine", 0.72, []),
    ("Product", "Pyrimidine", 0.89, ["嘧啶"]),
    ("Product", "N-fused polycycle", 0.80, []),
    ("Intermediate", "Keteniminium", 0.85, ["烯酮亚胺"]),
    ("Intermediate", "Vinyl cation", 0.83, ["乙烯基阳离子"]),
    ("Method", "Mechanochemistry", 0.75, ["ball milling", "机械化学"]),
    ("Method", "Photoredox catalysis", 0.87, []),
    ("Property", "Enantioselectivity", 0.78, ["ee", "对映选择性"]),
    ("Property", "Yield", 0.86, ["产率"]),
    ("Property", "Regioselectivity", 0.82, ["区域选择性"]),
    ("Solvent", "Toluene", 0.70, ["甲苯"]),
    ("Chemical", "water", 0.95, ["H2O"]),
]

#: (关系类型, 源, 目标, 置信度, 证据句)
EDGES = [
    ("catalyzed_by", "Intermolecular annulation", "Au(I) phosphine complex", 0.91,
     "The reaction is catalysed by a gold(I) phosphine complex."),
    ("uses", "Intermolecular annulation", "N-sulfonyl ynamide", 0.93,
     "N-sulfonyl ynamides were employed as the limiting partner."),
    ("affords", "Intermolecular annulation", "Pyrimidine", 0.90,
     "Pyrimidine derivatives were isolated in up to 92% yield."),
    ("proceeds_via", "Intermolecular annulation", "Keteniminium", 0.84,
     "Labelling and DFT support a keteniminium intermediate."),
    ("catalyzed_by", "Dearomatisation cascade", "K2CO3", 0.82,
     "K2CO3 was essential for the dearomatisation step."),
    ("uses", "Dearomatisation cascade", "Pyridinium salt", 0.85,
     "Pyridinium salts undergo dearomatisation under the stated conditions."),
    ("affords", "Dearomatisation cascade", "N-fused polycycle", 0.79,
     "The cascade delivers N-fused polycycles."),
    ("improves_upon", "Photoredox catalysis", "Mechanochemistry", 0.55,
     "Both approaches shorten reaction times relative to thermal conditions."),
    ("has_property", "Photoredox catalysis", "Enantioselectivity", 0.77,
     "Enantioselectivities up to 96% ee were obtained."),
    ("has_property", "Intermolecular annulation", "Yield", 0.88,
     "Yields ranged from 61% to 92%."),
    ("uses", "Mechanochemistry", "Solvent", 0.60, "Reactions were run neat."),
]

#: 超边：(类型, label, 成员[(name, role)], 条件, 测量, 置信度, 论文)
HYPEREDGES = [
    {
        "hyperedge_type": "procedure", "label": "Au-catalysed annulation protocol",
        "members": [("N-sulfonyl ynamide", "substrate"),
                    ("Au(I) phosphine complex", "catalyst"),
                    ("Pyrimidine", "product")],
        "conditions": [{"key": "temperature", "operator": "=", "value": "60",
                        "unit": "°C"},
                       {"key": "solvent", "operator": "=", "value": "DCE",
                        "unit": None},
                       {"key": "catalyst_loading", "operator": "=", "value": "5",
                        "unit": "mol%"}],
        "measurements": [{"metric": "yield", "value": "92", "unit": "%"},
                         {"metric": "ee", "value": "96", "unit": "%"}],
        "confidence": 0.89,
        "paper_key": "europepmc:10.1002/anie.202312345",
    },
    {
        "hyperedge_type": "claim", "label": "Keteniminium pathway",
        "members": [("Intermolecular annulation", "process"),
                    ("Keteniminium", "intermediate"),
                    ("N-sulfonyl ynamide", "evidence")],
        "conditions": [{"key": "temperature", "operator": ">",
                        "value": "40", "unit": "°C"}],
        "measurements": [{"metric": "barrier", "value": "18.4",
                          "unit": "kcal/mol"}],
        "confidence": 0.81,
        "paper_key": "europepmc:10.1002/anie.202312345",
    },
    {
        "hyperedge_type": "event", "label": "Dearomatisation observed",
        "members": [("Pyridinium salt", "substrate"),
                    ("N-fused polycycle", "product")],
        "conditions": [],
        "measurements": [{"metric": "yield", "value": "74", "unit": "%"}],
        "confidence": 0.76,
        "paper_key": "europepmc:10.1039/D3SC45A",
    },
    {
        "hyperedge_type": "claim", "label": "Ligand controls regioselectivity",
        "members": [("Bulky phosphine", "catalyst"),
                    ("Gold-catalysed annulation", "process"),
                    ("Regioselectivity", "outcome")],
        "conditions": [{"key": "catalyst_loading", "operator": "=", "value": "5",
                        "unit": "mol%"},
                       {"key": "ligand", "operator": "=", "value": "P(tBu)3",
                        "unit": None}],
        "measurements": [{"metric": "yield", "value": "88", "unit": "%"},
                         {"metric": "regioselectivity_ratio", "value": "12",
                          "unit": ":1"}],
        "confidence": 0.85,
        "paper_key": "europepmc:10.1002/chem.202400777",
    },
]

TAG_PLAN = {
    "europepmc:10.1002/anie.202312345": ["金催化", "机制证据强"],
    "europepmc:10.1038/s41586-024-00001-x": ["顶刊", "自由基"],
    "europepmc:10.1039/D3SC04567A": ["去芳构化"],
    "ncpssd:W2600001": ["人文社科"],
    "europepmc:10.1002/ejoc.202301999": ["待复核"],
}
FAVORITES = ["europepmc:10.1002/anie.202312345",
             "europepmc:10.1038/s41586-024-00001-x"]
FOLDERS = {
    "本体测试集": ["europepmc:10.1002/anie.202312345", "arxiv:2402.01234"],
    "写作素材": ["europepmc:10.1038/s41586-024-00001-x",
                 "europepmc:10.1039/D3SC04567A"],
}


def seed(db_path: Path) -> dict:
    conn = connect(db_path)
    stats = {"papers": 0, "quality": 0, "nodes": 0, "edges": 0, "hyperedges": 0,
             "tags": 0, "folders": 0, "favorites": 0}
    for rec in PAPERS:
        existing = conn.execute("SELECT 1 FROM papers WHERE paper_key=?",
                                (rec["paper_key"],)).fetchone()
        upsert_paper(conn, rec)
        stats["papers"] += 1
        if not existing:
            log_event(conn, "retrieval", "paper-ingested", rec["paper_key"],
                      {"source": rec["source"], "fulltext_source": rec["fulltext_source"]})

    for key, (score, decision, needs_review, reasons) in QUALITY.items():
        venue_factor = min(0.95, 0.35 + score * 0.6)
        save_quality_result(conn, {
            "paper_key": key,
            "venue_factor": round(venue_factor, 3),
            "h_factor": round(min(1.0, 0.4 + score * 0.5), 3),
            "citation_factor": round(min(1.0, 0.3 + score * 0.6), 3),
            "authority": round(score * 0.95, 3),
            "timeliness": round(min(1.0, score * 1.05), 3),
            "quality": score, "decision": decision,
            "needs_review": needs_review, "meta_missing": [],
            "rationale": "；".join(reasons) or "按 A/T/Q 公式评估",
        })
        stats["quality"] += 1
        log_event(conn, "quality", "assessed", key,
                  {"quality": score, "decision": decision})

    ont.init_ontology(conn)
    node_ids: dict[str, int] = {}
    for node_type, name, conf, aliases in NODES:
        node_id, is_new = ont.upsert_node(
            conn, node_type=node_type, name=name, confidence=conf,
            aliases=aliases,
            provenance=[{"paper": "europepmc:10.1002/anie.202312345",
                         "evidence": f"{name} 出现在机制讨论中"}])
        node_ids[name] = node_id
        stats["nodes"] += 1
    for rel, src, tgt, conf, evidence in EDGES:
        if src not in node_ids or tgt not in node_ids:
            continue
        ont.upsert_edge(
            conn, relation_type=rel, src_id=node_ids[src], tgt_id=node_ids[tgt],
            confidence=conf,
            provenance=[{"paper": "europepmc:10.1002/anie.202312345",
                         "evidence": evidence}])
        stats["edges"] += 1
    for spec in HYPEREDGES:
        members = []
        for name, role in spec["members"]:
            if name in node_ids:
                members.append({"node_id": node_ids[name], "role": role})
        if not members:
            continue
        ont.upsert_hyperedge(
            conn, hyperedge_type=spec["hyperedge_type"], label=spec["label"],
            members=members, conditions=spec["conditions"],
            measurements=spec["measurements"], confidence=spec["confidence"],
            paper_key=spec["paper_key"],
            provenance=[{"paper": spec["paper_key"],
                         "evidence": f"{spec['label']}（超边示例证据）"}])
        stats["hyperedges"] += 1

    # 处理日志：让「事件与日志」「研究流程」有内容
    for key in ("europepmc:10.1002/anie.202312345",
                "europepmc:10.1038/s41586-024-00001-x",
                "europepmc:10.1002/chem.202400777"):
        log_event(conn, "knowledge", "extracted", key,
                  {"entities": 6, "relations": 5, "new_nodes": 4})
    log_event(conn, "knowledge", "extract-failed", "europepmc:10.1002/ejoc.202301999",
              {"failed_chunks": 2, "errors": ["LLM 输出非 JSON"]})
    log_event(conn, "retrieval", "relevance-gate-low-signal", "pubmed:39881234",
              {"low_signal": "topic_token_overlap_zero",
               "note": "短摘要放行并标注"})
    log_event(conn, "human_review", "human-review", "europepmc:10.1002/ejoc.202301999",
              {"reason": "Q=0.41 低于阈值"})

    save_study_run(conn, "demo-run-0001", {
        "status": "reviewed", "decision": "pass",
        "plan": {"goal": "提出一种趋近 L3 的机制级设计",
                 "domain": "chemistry", "task_kind": "generative"},
        "consumer": {"mechanism_states": 4, "operator_candidates": 3},
        "draft": {"candidates": 3},
        "review": {"overall_score": 0.62},
        "fact_check": {"decision": "pass", "issues": 0},
    }, round_index=1, request="炔酰胺合成多元氮杂环的新方法")
    log_event(conn, "study", "session-start", None,
              {"run_id": "demo-run-0001", "request": "炔酰胺合成多元氮杂环的新方法"})

    store = LibraryStore(conn)
    for key, tags in TAG_PLAN.items():
        for tag in tags:
            if store.add_tag(key, tag):
                stats["tags"] += 1
    for name, keys in FOLDERS.items():
        folder_id = store.create_folder(name)
        stats["folders"] += 1
        for key in keys:
            store.add_to_folder(key, folder_id)
    for key in FAVORITES:
        if not store.is_favorite(key):
            store.set_favorite(key, True)
            stats["favorites"] += 1

    conn.commit()
    conn.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成看板演示库")
    ap.add_argument("--db", default=str(ROOT / "data" / "dashboard_demo.db"))
    args = ap.parse_args(argv)
    path = Path(args.db)
    path.parent.mkdir(parents=True, exist_ok=True)
    stats = seed(path)
    print(f"演示库就绪: {path}")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
