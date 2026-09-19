# -*- coding: utf-8 -*-
"""第四批 issue 修复回归测试（P2-1/2-3/2-4/2-6/2-7）。

来源：`docs/ISSUE_TRIAGE_2026-09-11.md` 第 4 批。
- P2-1 中文分句/长段落切分/截断统计；
- P2-3 证据字符区间写入 provenance；
- P2-4 审核轮次快照节点（逐轮落库钩子）；
- P2-6 版本号与 VERSIONS/CHANGELOG 同步；
- P2-7 超边条件 NULL 安全去重、索引补全、list_hyperedges 去 N+1。
"""
from __future__ import annotations

import json
import re
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from langchain_core.messages import AIMessage

from research_agent import __version__
from research_agent.config import Settings
from research_agent.db import connect, get_study_runs, upsert_paper
from research_agent.knowledge.node import make_knowledge_node
from research_agent.knowledge.preprocess import chunk_paragraphs, split_sentences
from research_agent.ontology import store as ont
from research_agent.study.graph import StudyServices, build_study_graph, run_study
from tests._tmpdir import make_temp_dir

PAPER_KEY = "chem:p2"
ROOT = Path(__file__).resolve().parents[1]

EVIDENCE_SENTENCE = "The reaction in toluene at 80 C for 12 h gave 87% yield."
PAPER_TEXT = ("Intro paragraph about ynamides. " + EVIDENCE_SENTENCE +
              " N-sulfonyl ynamide 1a was used.")

EXTRACTION = {
    "entities": [
        {"type": "Chemical", "name": "N-sulfonyl ynamide",
         "aliases": [], "attributes": {}, "confidence": 0.9,
         "evidence": "N-sulfonyl ynamide 1a was used."},
        {"type": "Chemical", "name": "dihydropyridine",
         "aliases": [], "attributes": {}, "confidence": 0.85,
         "evidence": EVIDENCE_SENTENCE},
    ],
    "relations": [],
    "events": [],
    "hyperedges": [
        {
            "type": "procedure",
            "label": "cyclization of N-sulfonyl ynamide to 1,4-dihydropyridine",
            "members": [
                {"name": "N-sulfonyl ynamide", "role": "substrate"},
                {"name": "dihydropyridine", "role": "product"},
            ],
            "conditions": [
                {"key": "temperature", "operator": "=", "value": "80", "unit": "C"},
                # unit 为空 → 旧 UNIQUE 约束在 SQLite 下不生效（P2-7）
                {"key": "solvent", "operator": "described_as",
                 "value": "toluene", "unit": None},
            ],
            "measurements": [
                {"metric": "yield", "value": "87", "unit": "%"},
                # subject_node 为空 → 同上
                {"metric": "selectivity", "value": "99", "unit": None},
            ],
            "confidence": 0.88,
            "evidence": EVIDENCE_SENTENCE,
        }
    ],
}


class _ExtractionModel:
    def invoke(self, messages, **kwargs) -> AIMessage:
        return AIMessage(content=json.dumps(EXTRACTION, ensure_ascii=False))


def _seed_paper(db: Path, paper_key: str = PAPER_KEY) -> None:
    conn = connect(db)
    ont.init_ontology(conn)
    upsert_paper(conn, {
        "paper_key": paper_key,
        "source": "chem",
        "title": "Ynamide cyclization study",
        "abstract": "A cyclization of ynamides.",
        "clean_text": PAPER_TEXT,
        "pub_year": 2024,
        "authors": [{"name": "Ada Lovelace", "affiliations": []}],
        "status": "ingested",
    })
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- P2-1


class PreprocessTest(unittest.TestCase):
    def test_cjk_sentences_split_without_whitespace(self):
        text = "炔酰胺参与环化。该反应给出吡啶！收率较高？"
        parts = split_sentences(text)
        self.assertEqual(len(parts), 3, parts)
        self.assertTrue(parts[0].endswith("。"))
        self.assertTrue(parts[-1].endswith("？"))

    def test_english_abbreviation_not_split(self):
        parts = split_sentences("See Smith et al. 2020 for details. Then more.")
        self.assertEqual(len(parts), 2, parts)

    def test_long_paragraph_is_split_under_limit(self):
        para = "".join(f"第{i}句测试内容。" for i in range(400))
        chunks = chunk_paragraphs([para], max_chars=300)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(sum(len(p) for p in chunk), 300)

    def test_dropped_chunks_are_reported(self):
        paras = [f"paragraph {i} " + "x" * 400 for i in range(6)]
        stats: dict = {}
        chunks = chunk_paragraphs(paras, max_chars=500, max_chunks=2, stats=stats)
        self.assertEqual(len(chunks), 2)
        self.assertGreater(stats["dropped_chunks"], 0)
        self.assertGreater(stats["dropped_chars"], 0)
        self.assertEqual(stats["total_chunks_before_limit"],
                         len(chunks) + stats["dropped_chunks"])


# --------------------------------------------------------------------------- P2-3


class EvidencePositionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "pos.db"
        _seed_paper(self.db)
        self.conn = connect(self.db)
        self.settings = Settings(db_path=self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_evidence_char_span_recorded(self):
        node = make_knowledge_node(model=_ExtractionModel(), conn=self.conn,
                                   settings=self.settings)
        out = node({"current_key": PAPER_KEY, "quality": 0.9})
        self.assertEqual(out.get("status"), "extracted")
        rows = ont.list_hyperedges(self.conn)
        target = max(rows, key=lambda h: len(h.get("evidence") or []))
        spans = [e for e in target["evidence"] if e.get("char_start") is not None]
        self.assertTrue(spans, target["evidence"])
        expected = PAPER_TEXT.find(EVIDENCE_SENTENCE)
        self.assertEqual(spans[0]["char_start"], expected)
        self.assertEqual(spans[0]["char_end"], expected + len(EVIDENCE_SENTENCE))

    def test_unknown_span_leaves_positions_empty(self):
        payload = json.loads(json.dumps(EXTRACTION))
        payload["hyperedges"][0]["evidence"] = "这句话不在正文里。"
        payload["hyperedges"][0]["label"] = "ghost procedure"

        class _GhostModel:
            def invoke(self, messages, **kwargs):
                return AIMessage(content=json.dumps(payload, ensure_ascii=False))

        node = make_knowledge_node(model=_GhostModel(), conn=self.conn,
                                   settings=self.settings)
        node({"current_key": PAPER_KEY, "quality": 0.9})
        rows = ont.list_hyperedges(self.conn)
        ghost = [h for h in rows if h["label"] == "ghost procedure"]
        self.assertTrue(ghost, [h["label"] for h in rows])
        self.assertIsNone(ghost[0]["evidence"][0]["char_start"],
                          "查不到的句子不能猜位置")


# --------------------------------------------------------------------------- P2-4


class RoundSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "rounds.db"
        _seed_paper(self.db)
        self.settings = Settings(db_path=self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_callback_receives_review_state(self):
        seen: list[dict] = []
        graph = build_study_graph(StudyServices(settings=self.settings),
                                  on_round=lambda state: seen.append(dict(state)))
        out = graph.invoke({"request": "调研 ynamide 环化", "review_rounds": 0})
        self.assertIn(out.get("status"), ("reviewed", "manual_review"))
        self.assertTrue(seen, "审核轮次结束后必须触发轮次快照钩子")
        self.assertIsNotNone(seen[0].get("review"))
        self.assertIn("status", seen[0])

    def test_snapshot_failure_does_not_break_graph(self):
        def boom(_state):
            raise RuntimeError("落库炸了")

        graph = build_study_graph(StudyServices(settings=self.settings),
                                  on_round=boom)
        out = graph.invoke({"request": "调研 ynamide 环化", "review_rounds": 0})
        self.assertIn(out.get("status"), ("reviewed", "manual_review"))

    def test_run_study_persists_round_before_final_save(self):
        calls: list[int] = []
        real = __import__("research_agent.db", fromlist=["save_study_run"]).save_study_run

        def spy(conn, run_id, payload, *, round_index=0, request=""):
            calls.append(int(round_index))
            return real(conn, run_id, payload, round_index=round_index,
                        request=request)

        with mock.patch("research_agent.study.graph.save_study_run", spy):
            out = run_study("调研 ynamide 环化生成氮杂环的新方法",
                            StudyServices(settings=self.settings))
        self.assertGreaterEqual(len(calls), 2,
                                "轮次快照 + 结束落库至少各写一次")
        conn = connect(self.db)
        try:
            rows = get_study_runs(conn, out["run_id"])
        finally:
            conn.close()
        self.assertTrue(rows)


# --------------------------------------------------------------------------- P2-6


class VersionSyncTest(unittest.TestCase):
    def test_package_version_matches_pyproject(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), __version__,
                         "pyproject 版本与 __version__ 必须一致")

    def test_versions_md_lists_current_version(self):
        text = (ROOT / "VERSIONS.md").read_text(encoding="utf-8")
        self.assertIn(f"| v{__version__} ", text,
                      "VERSIONS.md 必须登记当前版本")


# --------------------------------------------------------------------------- P2-7


class HyperedgeDedupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "dedup.db"
        self.conn = connect(self.db)
        ont.init_ontology(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _insert(self):
        return ont.upsert_hyperedge(
            self.conn, hyperedge_type="procedure", label="cyclization",
            members=[], conditions=[
                {"key": "solvent", "operator": "described_as",
                 "value": "toluene", "unit": None},
                {"key": "temperature", "operator": "=", "value": "80",
                 "unit": "C"},
            ], measurements=[
                {"metric": "selectivity", "value": "99", "unit": None},
                {"metric": "yield", "value": "87", "unit": "%"},
            ], confidence=0.8, evidence_tier="primary", paper_key=PAPER_KEY,
            provenance=[{"paper": PAPER_KEY, "evidence": "toluene at 80 C"}])

    def test_null_unit_conditions_are_deduplicated(self):
        hid, first = self._insert()
        self.conn.commit()
        self.assertTrue(first)
        self._insert()
        self.conn.commit()
        conditions = self.conn.execute(
            "SELECT COUNT(*) FROM ontology_hyperedge_conditions "
            "WHERE hyperedge_id=?", (hid,)).fetchone()[0]
        measurements = self.conn.execute(
            "SELECT COUNT(*) FROM ontology_hyperedge_measurements "
            "WHERE hyperedge_id=?", (hid,)).fetchone()[0]
        self.assertEqual(conditions, 2, "unit 为 NULL 的条件不应重复入库")
        self.assertEqual(measurements, 2, "subject_node 为 NULL 的测量不应重复入库")

    def test_nullsafe_indexes_and_created_at_index_exist(self):
        names = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("uq_hyperedge_conditions_nullsafe", names)
        self.assertIn("uq_hyperedge_measurements_nullsafe", names)
        self.assertIn("idx_hyperedges_created", names)

    def test_migration_dedups_legacy_rows(self):
        conn = self.conn
        conn.execute("DELETE FROM ontology_hyperedge_conditions")
        conn.execute("DROP INDEX uq_hyperedge_conditions_nullsafe")
        for _ in range(3):
            conn.execute(
                "INSERT INTO ontology_hyperedge_conditions("
                "hyperedge_id, condition_key, operator, value_text, unit) "
                "VALUES(1, 'solvent', 'described_as', 'toluene', NULL)")
        conn.commit()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM ontology_hyperedge_conditions").fetchone()[0], 3)
        stats = ont._migrate_hyperedge_null_safe_uniqueness(conn)
        conn.commit()
        self.assertEqual(stats["dropped_conditions"], 2)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM ontology_hyperedge_conditions").fetchone()[0], 1)

    def test_list_hyperedges_query_count_is_constant(self):
        for i in range(6):
            ont.upsert_hyperedge(
                self.conn, hyperedge_type="procedure", label=f"proc-{i}",
                members=[], conditions=[{"key": "k", "value": str(i), "unit": None}],
                measurements=[], confidence=0.5, evidence_tier="primary",
                paper_key=PAPER_KEY, provenance=[])
        self.conn.commit()

        counts = []
        for limit in (2, 6):
            log: list[str] = []
            self.conn.set_trace_callback(log.append)
            try:
                rows = ont.list_hyperedges(self.conn, limit=limit)
            finally:
                self.conn.set_trace_callback(None)
            self.assertEqual(len(rows), limit)
            counts.append(sum(1 for s in log if s.strip().upper().startswith("SELECT")))
        self.assertLessEqual(counts[1], 8,
                             f"批量加载不应产生 N+1（实际 {counts}）")
        self.assertEqual(counts[0], counts[1],
                         "查询次数不应随超边条数增长（N+1 回归）")

    def test_list_hyperedges_filters_by_node(self):
        node_id, _ = ont.upsert_node(self.conn, node_type="Chemical",
                                     name="toluene", confidence=0.9)
        keep, _ = ont.upsert_hyperedge(
            self.conn, hyperedge_type="procedure", label="with-member",
            members=[{"node_id": node_id, "role": "solvent"}],
            conditions=[], measurements=[], confidence=0.9,
            evidence_tier="primary", paper_key=PAPER_KEY, provenance=[])
        ont.upsert_hyperedge(
            self.conn, hyperedge_type="procedure", label="without-member",
            members=[], conditions=[], measurements=[], confidence=0.5,
            evidence_tier="primary", paper_key=PAPER_KEY, provenance=[])
        self.conn.commit()
        rows = ont.list_hyperedges(self.conn, node_ids={node_id})
        self.assertEqual([r["hyperedge_id"] for r in rows], [keep])


if __name__ == "__main__":
    unittest.main(verbosity=2)
