"""入库规范化测试：关系词同义归一 + 别名去重合并。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research_agent.db import connect
from research_agent.ontology import store as ont
from tests._tmpdir import make_temp_dir


class OntologyNormalizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.conn = connect(Path(self.tmp.name) / "norm.db")
        ont.init_ontology(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_canonical_relation(self):
        self.assertEqual(ont.canonical_relation_type("utilizes"), "uses")
        self.assertEqual(ont.canonical_relation_type("assessed"), "evaluates")
        self.assertEqual(ont.canonical_relation_type("promotes"), "promotes")
        self.assertEqual(ont.canonical_relation_type("enhance"), "promotes")
        self.assertEqual(ont.canonical_relation_type("upregulates"), "regulates")
        self.assertEqual(ont.canonical_relation_type("induce"), "promotes")
        self.assertEqual(ont.canonical_relation_type("composed of"), "made_of")
        self.assertEqual(ont.canonical_relation_type("novel_link"), "novel_link")

    def test_v005_relations_and_identity(self):
        self.assertEqual(ont.canonical_relation_type("correlate"), "correlates_with")
        self.assertEqual(ont.canonical_relation_type("enable"), "enables")
        self.assertEqual(ont.canonical_relation_type("complicates"), "complicates")
        self.assertEqual(ont.canonical_relation_type("risk factor for"),
                         "risk_factor_for")
        self.assertEqual(ont.canonical_relation_type("results in"), "results_in")
        self.assertEqual(ont.canonical_relation_type("is a"), "is_a")
        # 身份/证据列存在
        cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(ontology_nodes)")}
        for c in ("identity_key", "external_source", "external_id",
                  "term_status", "scope_tag", "evidence_tier"):
            self.assertIn(c, cols)
        ecols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(ontology_edges)")}
        self.assertIn("evidence_tier", ecols)

    def test_material_registry_and_event_side(self):
        lid = ont.register_material(
            self.conn, "GelMA", synonyms=["gelatin methacryloyl"],
            composition={"base": "gelatin", "modification": "methacryloyl"})
        self.assertTrue(lid.startswith("lcmat:"))
        row = self.conn.execute(
            "SELECT preferred_name, term_status FROM material_registry "
            "WHERE local_id=?", (lid,)).fetchone()
        self.assertEqual(row["preferred_name"], "GelMA")
        self.assertEqual(row["term_status"], "local_uncurated")
        eid = ont.add_event_assertion(
            self.conn, paper_key="p1", event_type="Experiment",
            trigger="mechanical test", participants=["GelMA"],
            entity_refs=[1], confidence=0.8,
            provenance=[{"paper": "p1", "evidence": "s1"}])
        self.assertGreater(eid, 0)

    def test_edge_relation_normalized_in_db(self):
        n1 = ont.upsert_node(self.conn, node_type="Method", name="X", confidence=0.8)[0]
        n2 = ont.upsert_node(self.conn, node_type="Dataset", name="Y", confidence=0.8)[0]
        eid, is_new = ont.upsert_edge(self.conn, relation_type="utilizes",
                                      src_id=n1, tgt_id=n2, confidence=0.9)
        self.assertTrue(is_new)
        row = self.conn.execute(
            "SELECT relation_type FROM ontology_edges WHERE edge_id=?", (eid,)
        ).fetchone()
        self.assertEqual(row["relation_type"], "uses")
        reg = self.conn.execute(
            "SELECT 1 FROM ontology_type_registry WHERE type_key='uses'"
        ).fetchone()
        self.assertIsNotNone(reg)

    def test_alias_merge_across_papers(self):
        # 论文 A：以缩写建节点
        n1, new1 = ont.upsert_node(
            self.conn, node_type="Material", name="CPC",
            aliases=["calcium phosphate cement"], confidence=0.8,
            provenance=[{"paper": "pA", "evidence": "CPC (calcium phosphate cement)"}])
        self.assertTrue(new1)
        # 论文 B：用全称建节点（其缩写恰为论文 A 的规范名/别名）
        n2, new2 = ont.upsert_node(
            self.conn, node_type="Material", name="calcium phosphate cement",
            aliases=["CPC"], confidence=0.9,
            provenance=[{"paper": "pB", "evidence": "calcium phosphate cement (CPC)"}])
        self.assertEqual(n1, n2)
        self.assertFalse(new2)
        row = self.conn.execute(
            "SELECT name, aliases, provenance FROM ontology_nodes WHERE node_id=?", (n1,)
        ).fetchone()
        self.assertEqual(row["name"], "CPC")
        import json
        self.assertIn("calcium phosphate cement", json.loads(row["aliases"]))
        self.assertEqual(len(json.loads(row["provenance"])), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
