"""ACS 纯净转换回归测试（v0.4.1 半程测试新增）。

覆盖：库内编号清理、顺序数字引用、References 生成、系统术语/占位符自检、
悬空引用识别、状态标记处理。
"""
from __future__ import annotations

import unittest

from research_agent.study.acs_format import (
    acs_reference_line,
    build_citation_index,
    clean_body,
    to_acs_document,
)


def knowledge() -> dict:
    return {
        "patterns": [
            {"pattern_id": "P-0001", "paper_keys": ["europepmc:MED:1"],
             "evidence_ids": ["E-0001-1"]},
            {"pattern_id": "P-0002", "paper_keys": ["pubmed:2"],
             "evidence_ids": ["E-0002-1"]},
        ],
        "evidence": [
            {"evidence_id": "E-0001-1", "pattern_id": "P-0001",
             "paper_key": "europepmc:MED:1", "sentence": "s1"},
            {"evidence_id": "E-0002-1", "pattern_id": "P-0002",
             "paper_key": "pubmed:2", "sentence": "s2"},
        ],
        "hyperedges": [{
            "hyperedge_id": 37, "label": "Cu-catalyzed cyclization",
            "professional": True,
            "evidence": [{"paper_key": "pubmed:3", "span_text": "s3"},
                         {"paper_key": "pubmed:3", "span_text": "s4"}],
            "evidence_ids": ["H-0037-1", "H-0037-2"],
        }],
    }


def papers() -> dict:
    return {
        "europepmc:MED:1": {
            "paper_key": "europepmc:MED:1",
            "title": "Ynamide annulation to azacycles.",
            "venue": "J. Org. Chem.", "pub_year": 2021, "volume": "86",
            "pages": "1-9", "doi": "10.1021/xyz",
            "authors": [{"name": "Ada Lovelace"}, {"name": "Alan Turing"}],
        },
        "pubmed:2": {
            "paper_key": "pubmed:2", "title": "Gold catalysis of ynamides.",
            "venue": "Angew. Chem. Int. Ed.", "pub_year": 2022,
            "doi": "https://doi.org/10.1002/abc", "authors": [],
        },
        "pubmed:3": {
            "paper_key": "pubmed:3",
            "title": "Copper-Catalyzed &lt;i&gt;trans&lt;/i&gt;-Selective Cyclization.",
            "venue": "Org. Lett.", "pub_year": 2023, "doi": "10.1021/def",
            "authors": [{"name": "Marie Curie"}],
        },
    }


def draft(**overrides) -> dict:
    data = {
        "title": "t",
        "sections": [{
            "heading": "成环策略",
            "items": [
                {"text": "铜催化环化可构建氮杂环 [P-0001, E-0001-1]。",
                 "pattern_ids": ["P-0001"], "evidence_ids": ["E-0001-1"],
                 "status": "supported"},
                {"text": "金催化路径尚待验证 [P-0002]。",
                 "pattern_ids": ["P-0002"], "evidence_ids": [],
                 "status": "hypothesis"},
                {"text": "铜催化乙烯基阳离子捕获 [H-0037-1]。",
                 "pattern_ids": [], "evidence_ids": ["H-0037-1"],
                 "status": "supported"},
            ],
        }],
    }
    data.update(overrides)
    return data


class AcsFormatTest(unittest.TestCase):
    def test_citation_index_covers_patterns_evidence_hyperedges(self):
        index = build_citation_index(knowledge())
        self.assertEqual(index["P-0001"], "europepmc:MED:1")
        self.assertEqual(index["E-0001-1"], "europepmc:MED:1")
        self.assertEqual(index["H-0037"], "pubmed:3")
        self.assertEqual(index["H-0037-2"], "pubmed:3")

    def test_body_has_no_internal_ids_and_has_superscripts(self):
        out = to_acs_document(draft(), knowledge(), papers(),
                              title="综述")
        body = out["markdown"]
        self.assertNotIn("P-0001", body)
        self.assertNotIn("E-0001-1", body)
        self.assertNotIn("H-0037-1", body)
        self.assertIn("<sup>1</sup>", body)
        self.assertEqual(out["checks"]["residual_internal_ids"], [])
        self.assertTrue(out["checks"]["clean"])

    def test_references_numbered_and_acs_style(self):
        out = to_acs_document(draft(), knowledge(), papers(), title="综述")
        self.assertEqual(out["checks"]["references"], 3)
        refs = out["references"]
        self.assertTrue(refs[0].startswith("(1) Lovelace, A.; Turing, A."))
        self.assertIn("*J. Org. Chem.*", refs[0])
        self.assertIn("https://doi.org/10.1021/xyz", refs[0])
        # 已有 https://doi.org/ 前缀不应重复
        self.assertIn("https://doi.org/10.1002/abc", refs[1])
        self.assertNotIn("https://doi.org/https", "\n".join(refs))

    def test_dangling_citation_reported(self):
        data = draft()
        data["sections"][0]["items"].append({
            "text": "无来源断言 [P-9999]。", "pattern_ids": ["P-9999"],
            "evidence_ids": [], "status": "supported"})
        out = to_acs_document(data, knowledge(), papers(), title="综述")
        self.assertIn("P-9999", out["checks"]["dangling_citations"])
        self.assertFalse(out["checks"]["clean"])

    def test_system_terms_and_placeholders_are_flagged(self):
        data = draft()
        data["sections"][0]["items"][0]["text"] += " 依据 design_context 中的模式卡。"
        out = to_acs_document(data, knowledge(), papers(), title="综述")
        self.assertIn("design_context", out["checks"]["system_terms"])
        self.assertIn("模式卡", out["checks"]["system_terms"])

    def test_status_markers_removed_and_hypothesis_annotated(self):
        out = to_acs_document(draft(), knowledge(), papers(), title="综述")
        self.assertNotIn("[hypothesis]", out["markdown"])
        self.assertIn("尚待实验验证", out["markdown"])

    def test_candidate_section_can_be_omitted(self):
        data = draft(strategies=[{
            "rank": 1, "title": "候选一", "innovation_level": "L3",
            "operator_chain": [{"operator": "polarity_reversal",
                                "input": "a", "output": "b"}],
            "differentiation": "d", "novelty_source": "n", "rationale": "r",
            "risks": ["风险一"], "validation_plan": "最小实验",
            "satisfies_constraints": [
                {"constraint": "氮原子数≥2", "satisfied": True, "reason": "含两个氮"}],
        }])
        with_candidates = to_acs_document(data, knowledge(), papers(),
                                          title="报告",
                                          keep_candidate_section=True)
        without = to_acs_document(data, knowledge(), papers(), title="综述",
                                  keep_candidate_section=False)
        self.assertIn("候选新方法", with_candidates["markdown"])
        self.assertIn("氮原子数≥2", with_candidates["markdown"])
        self.assertNotIn("候选新方法", without["markdown"])
        self.assertEqual(without["stats"]["candidates"], 1)

    def test_clean_body_strips_markers(self):
        text = "- [hypothesis] 某结论。\n\n\n\n下一段。MS-0001"
        cleaned = clean_body(text)
        self.assertNotIn("[hypothesis]", cleaned)
        self.assertNotIn("MS-0001", cleaned)
        self.assertIn("某结论。", cleaned)
        self.assertNotIn("\n\n\n", cleaned)

    def test_reference_line_without_authors(self):
        line = acs_reference_line({"title": "T.", "venue": "V.",
                                   "pub_year": 2020}, 7)
        self.assertTrue(line.startswith("(7) T."))
        self.assertIn("*V.* 2020", line)

    def test_html_entities_and_tags_are_cleaned(self):
        out = to_acs_document(draft(), knowledge(), papers(), title="综述")
        joined = "\n".join(out["references"])
        self.assertIn("Copper-Catalyzed trans-Selective Cyclization.",
                      joined)
        self.assertNotIn("&lt;", joined)
        self.assertNotIn("<i>", joined)

    def test_author_surname_first_form_is_parsed(self):
        line = acs_reference_line({
            "title": "T.", "venue": "V.", "pub_year": 2020,
            "authors": [{"name": "van Geel R"}, {"name": "Löwik DW"},
                        {"name": "Campbell, A. D. G."}],
        }, 1)
        self.assertIn("van Geel, R.", line)
        self.assertIn("Löwik, D. W.", line)
        self.assertIn("Campbell, A. D. G.", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
