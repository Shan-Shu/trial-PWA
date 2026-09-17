"""超边条件/测量抽取与消费回归测试（v0.4.1）。

覆盖 `docs/STATUS_AND_NEXT_STEPS_v0.4.0.md` 第 3.4 节：条件与测量此前在
"抽取 → 入库 → 消费"链路上被整体丢弃（3851 条超边只有 34 条条件、0 条测量）。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage

from research_agent.config import Settings
from research_agent.db import connect, upsert_paper
from research_agent.knowledge.extractor import flag_issues
from research_agent.knowledge.node import make_knowledge_node
from research_agent.ontology import store as ont
from research_agent.study.consumer import (
    _compact_consumer_knowledge,
    mine_ontology_evidence,
)
from tests._tmpdir import make_temp_dir

PAPER_KEY = "chem:1"

EXTRACTION = {
    "entities": [
        {"type": "Chemical", "name": "N-sulfonyl ynamide",
         "aliases": [], "attributes": {}, "confidence": 0.9,
         "evidence": "N-sulfonyl ynamide 1a was used."},
        {"type": "Chemical", "name": "dihydropyridine",
         "aliases": [], "attributes": {}, "confidence": 0.85,
         "evidence": "The product was 1,4-dihydropyridine."},
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
                {"key": "temperature", "operator": "=", "value": "80", "unit": "°C"},
                {"key": "duration", "operator": "=", "value": "12", "unit": "h"},
                {"key": "solvent", "operator": "described_as",
                 "value": "toluene", "unit": None},
                {"key": "catalyst", "operator": "=", "value": "5", "unit": "mol%"},
            ],
            "measurements": [
                {"metric": "yield", "value": "87", "unit": "%",
                 "qualifier": "isolated"},
                {"metric": "ee", "value": "94", "unit": "%", "qualifier": "HPLC"},
            ],
            "confidence": 0.88,
            "evidence": "The reaction in toluene at 80 °C for 12 h gave 87% yield.",
        }
    ],
}


class _ExtractionModel:
    def invoke(self, messages, **kwargs) -> AIMessage:
        return AIMessage(content=json.dumps(EXTRACTION, ensure_ascii=False))


class HyperedgeConditionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "cond.db"
        conn = connect(self.db)
        ont.init_ontology(conn)
        upsert_paper(conn, {
            "paper_key": PAPER_KEY,
            "source": "chem",
            "title": "Ynamide cyclization study",
            "abstract": "A cyclization of ynamides.",
            "clean_text": (
                "The reaction in toluene at 80 °C for 12 h gave 87% yield. "
                "N-sulfonyl ynamide 1a was used."),
            "pub_year": 2024,
            "authors": [{"name": "Ada Lovelace", "affiliations": []}],
            "status": "ingested",
        })
        conn.commit()
        conn.close()
        self.conn = connect(self.db)
        self.settings = Settings(db_path=self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _run_extraction(self):
        node = make_knowledge_node(model=_ExtractionModel(), conn=self.conn,
                                   settings=self.settings)
        return node({"current_key": PAPER_KEY, "quality": 0.9})

    def test_conditions_and_measurements_persist(self):
        out = self._run_extraction()
        self.assertEqual(out.get("status"), "extracted")
        rows = ont.list_hyperedges(self.conn)
        self.assertTrue(rows)
        target = max(rows, key=lambda h: len(h.get("conditions") or []))
        keys = {c["condition_key"] for c in target["conditions"]}
        self.assertIn("temperature", keys)
        self.assertIn("solvent", keys)
        self.assertIn("catalyst", keys)
        metrics = {m["metric"]: m for m in target["measurements"]}
        self.assertIn("yield", metrics)
        self.assertEqual(metrics["yield"]["value_text"], "87")
        self.assertIn("ee", metrics)

    def test_consumer_receives_conditions_in_knowledge_pack(self):
        self._run_extraction()
        bundle = mine_ontology_evidence(
            self.conn,
            {"seed_terms": ["ynamide cyclization"], "min_confidence": 0.5})
        self.assertTrue(bundle["hyperedges"])
        compact = _compact_consumer_knowledge(bundle)
        cards = compact["hyperedges"]
        self.assertTrue(any(card["conditions"] for card in cards),
                        "消费节点必须收到结构化条件")
        self.assertTrue(any(card["measurements"] for card in cards),
                        "消费节点必须收到结构化测量")
        self.assertTrue(all(card["evidence"] for card in cards
                            if card["conditions"] or card["measurements"]))

    def test_flag_issues_points_out_missing_quantities(self):
        data = {
            "hyperedges": [{
                "type": "procedure",
                "label": "cyclization in toluene at 80 °C",
                "members": [],
                "conditions": [],
                "measurements": [],
                "evidence": "87% yield was obtained.",
            }]
        }
        issues = flag_issues(data)
        self.assertTrue(any("conditions" in i for i in issues))
        self.assertTrue(any("SCHEMA #13" in i for i in issues))

    def test_flag_issues_silent_when_no_quantity_hint(self):
        data = {
            "hyperedges": [{
                "type": "claim",
                "label": "ynamides are versatile building blocks",
                "members": [],
                "conditions": [],
                "measurements": [],
                "evidence": "Ynamides are versatile.",
            }]
        }
        issues = flag_issues(data)
        self.assertFalse(any("SCHEMA #13" in i for i in issues))


if __name__ == "__main__":
    unittest.main(verbosity=2)
