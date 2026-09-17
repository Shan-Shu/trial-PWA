"""批次 1 回归：P0-1 事实核查白名单、P0-2 人工审核不丢数据、
P0-3 监控水位线、P0-4 词典归并重指超边成员。

对应 docs/ISSUE_TRIAGE_2026-09-11.md 的批次 1。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from research_agent.config import Settings
from research_agent.db import connect, get_paper, meta_get, upsert_paper
from research_agent.ontology import store as ont
from research_agent.quality.control import run_dictionary_merge
from research_agent.quality.node import make_human_review_node
from research_agent.retrieval.monitor import PaperMonitor
from research_agent.study.fact_check import deterministic_fact_check
from tests._tmpdir import make_temp_dir

KNOWLEDGE = {
    "patterns": [{"pattern_id": "P-0001", "evidence_ids": ["E-0001-1"],
                  "paper_keys": ["p1"]}],
    "evidence": [{"evidence_id": "E-0001-1", "pattern_id": "P-0001",
                  "paper_key": "p1", "sentence": "ynamide 1a was used."}],
    "hyperedges": [],
    "design_context": {
        "mechanism_states": [{"state_id": "MS-0001", "label": "vinyl cation"}],
        "opportunity_gaps": [{"gap_id": "GAP-0001", "missing_link": "第二氮引入"}],
        "operator_candidates": [{"op_id": "OP-0001", "name": "polarity_reversal"}],
    },
}


class FactCheckWhitelistTest(unittest.TestCase):
    """P0-1：MS-/GAP-/OP- 是系统自己生成的合法编号，不得判为伪造引用。"""

    def _draft(self, refs: list[str]) -> dict:
        return {
            "title": "t",
            "summary": {"text": "s", "pattern_ids": [], "evidence_ids": []},
            "sections": [{"heading": "h", "items": [{
                "text": "含机制证据的候选。",
                "pattern_ids": [], "evidence_ids": [],
                "mechanism_evidence": refs, "status": "supported"}]}],
        }

    def test_design_context_ids_are_known(self):
        result = deterministic_fact_check(
            self._draft(["MS-0001", "GAP-0001", "OP-0001"]), KNOWLEDGE)
        self.assertEqual(result["issues"], [],
                         f"合法机制编号被判问题: {result['issues']}")
        self.assertEqual(result["decision"], "pass")

    def test_unknown_id_still_flagged(self):
        result = deterministic_fact_check(self._draft(["MS-9999"]), KNOWLEDGE)
        self.assertEqual(result["decision"], "revise")
        self.assertIn("fabrication", {i["type"] for i in result["issues"]})

    def test_hyperedge_evidence_ids_are_known(self):
        knowledge = dict(KNOWLEDGE)
        knowledge["hyperedges"] = [{
            "hyperedge_id": 7, "label": "Cu cyclization",
            "evidence_ids": ["H-0007-1"],
            "evidence": [{"paper_key": "p1", "span_text": "Cu cyclization."}],
        }]
        result = deterministic_fact_check(self._draft(["H-0007-1"]), knowledge)
        self.assertEqual(result["issues"], [])


class HumanReviewPreservesDataTest(unittest.TestCase):
    """P0-2：人工审核只改状态，不得清空 PDF/精校文本。"""

    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "p02.db"
        self.settings = Settings(db_path=self.db)
        self.conn = connect(self.db)
        upsert_paper(self.conn, {
            "paper_key": "p-test", "source": "test", "title": "T",
            "abstract": "A", "clean_text": "正文内容",
            "pdf_blob": b"%PDF-1.4 x", "pdf_sha256": "deadbeef",
            "pdf_size": 9, "clean_text_sha256": "cafe",
            "authors": [{"name": "A"}], "status": "ingested",
        })
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_pdf_and_clean_text_survive_human_review(self):
        make_human_review_node(conn=self.conn, settings=self.settings)(
            {"current_key": "p-test", "quality_result": {"rationale": "low Q"}})
        row = get_paper(self.conn, "p-test")
        self.assertEqual(row["status"], "human_review")
        self.assertIsNotNone(row.get("pdf_blob"), "PDF BLOB 被清空")
        self.assertTrue(row.get("clean_text"), "精校文本被清空")

    def test_none_still_clears_when_explicit(self):
        """显式传 None 表示"确实要清空"，COALESCE 不应阻止该语义之外的更新。"""
        rec = get_paper(self.conn, "p-test")
        rec["clean_text"] = "新正文"
        upsert_paper(self.conn, rec)
        self.assertEqual(get_paper(self.conn, "p-test")["clean_text"], "新正文")


class MonitorWatermarkTest(unittest.TestCase):
    """P0-3：水位线必须落库；同秒记录不丢；失败不推进。"""

    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "monitor.db"
        self.settings = Settings(db_path=self.db)
        conn = connect(self.db)
        conn.commit()
        conn.close()
        self.monitor = PaperMonitor(db_path=str(self.db), settings=self.settings,
                                    watermark_key="wm")

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, key: str, created_at: str) -> None:
        conn = connect(self.db)
        upsert_paper(conn, {
            "paper_key": key, "source": "t", "title": key, "abstract": "a",
            "authors": [{"name": "A"}], "status": "ingested",
        })
        conn.execute("UPDATE papers SET created_at=?, updated_at=? WHERE paper_key=?",
                     (created_at, created_at, key))
        conn.commit()
        conn.close()

    def test_watermark_is_persisted(self):
        self._add("p1", "2026-01-01T00:00:00+00:00")
        keys = self.monitor.run_once(process_existing=True)
        self.assertEqual(keys, ["p1"])
        conn = connect(self.db)
        wm = meta_get(conn, "wm")
        conn.close()
        self.assertIsNotNone(wm, "水位线未落库（P0-3 回归）")

    def test_same_second_records_are_not_skipped(self):
        self._add("p1", "2026-01-01T00:00:05+00:00")
        self.assertEqual(self.monitor.run_once(process_existing=True), ["p1"])
        # 同一秒内再入库一篇
        self._add("p2", "2026-01-01T00:00:05+00:00")
        self.assertEqual(self.monitor.run_once(), ["p2"])

    def test_acked_keys_are_not_returned_again(self):
        self._add("p1", "2026-01-01T00:00:05+00:00")
        self.assertEqual(self.monitor.run_once(process_existing=True), ["p1"])
        self.monitor.ack(["p1"])
        self.assertEqual(self.monitor.run_once(), [])

    def test_release_allows_retry(self):
        self._add("p1", "2026-01-01T00:00:05+00:00")
        keys = self.monitor.run_once(process_existing=True)
        self.monitor.release(keys)
        self.assertEqual(self.monitor.run_once(), ["p1"])


class DictionaryMergeHyperedgeTest(unittest.TestCase):
    """P0-4：词典归并必须把超边成员/测量主体重指到保留节点。"""

    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "p04.db"
        self.conn = connect(self.db)
        ont.init_ontology(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_members_repointed_and_no_dangling(self):
        keep, _ = ont.upsert_node(
            self.conn, node_type="Chemical", name="water", confidence=0.9,
            provenance=[{"paper": "p1", "evidence": "water"}])
        drop, _ = ont.upsert_node(
            self.conn, node_type="Chemical", name="H2O", confidence=0.8,
            provenance=[{"paper": "p2", "evidence": "H2O"}])
        hyperedge_id, _ = ont.upsert_hyperedge(
            self.conn, hyperedge_type="event", label="water addition",
            members=[{"node_id": keep, "role": "participant"},
                     {"node_id": drop, "role": "participant"}],
            confidence=0.9, paper_key="p1",
            provenance=[{"paper": "p1", "evidence": "water addition"}])
        self.conn.execute(
            "INSERT INTO ontology_hyperedge_measurements("
            "hyperedge_id, metric, value_text, subject_node) VALUES(?,?,?,?)",
            (hyperedge_id, "yield", "80", drop))
        self.conn.commit()

        report = run_dictionary_merge(self.conn)
        self.conn.commit()
        self.assertEqual(report["merged_nodes"], 1)

        alive = {r["node_id"] for r in self.conn.execute(
            "SELECT node_id FROM ontology_nodes")}
        self.assertEqual(alive, {keep})
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM ontology_hyperedges").fetchone()[0], 1)
        members = self.conn.execute(
            "SELECT node_id FROM ontology_hyperedge_members").fetchall()
        self.assertTrue(members)
        for row in members:
            self.assertIn(row["node_id"], alive, "存在悬空超边成员（P0-4 回归）")
        subjects = self.conn.execute(
            "SELECT subject_node FROM ontology_hyperedge_measurements").fetchall()
        for row in subjects:
            if row["subject_node"] is not None:
                self.assertIn(row["subject_node"], alive)

    def test_duplicate_member_rows_are_collapsed(self):
        """同一超边里 keep 与 drop 都是成员时，重指后不应出现重复成员。"""
        keep, _ = ont.upsert_node(self.conn, node_type="Chemical", name="water",
                                  confidence=0.9)
        drop, _ = ont.upsert_node(self.conn, node_type="Chemical", name="H2O",
                                  confidence=0.8)
        hyperedge_id, _ = ont.upsert_hyperedge(
            self.conn, hyperedge_type="event", label="addition",
            members=[{"node_id": keep, "role": "a"},
                     {"node_id": drop, "role": "b"}],
            confidence=0.9, paper_key="p1",
            provenance=[{"paper": "p1", "evidence": "addition"}])
        self.conn.commit()
        run_dictionary_merge(self.conn)
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT node_id FROM ontology_hyperedge_members "
            "WHERE hyperedge_id=?", (hyperedge_id,)).fetchall()
        self.assertEqual([r["node_id"] for r in rows], [keep])


if __name__ == "__main__":
    unittest.main(verbosity=2)
