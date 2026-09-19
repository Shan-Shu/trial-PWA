"""要求覆盖闸门必须能"如实说不知道"。

来源是一个真实案例：某中文主题的实验设计节，写作要点是中文短语
（"自变量按溶剂""因变量为产率"…），而库内知识全是英文（节点名 'Ynamides'）。
`LIKE '%自变量按溶剂%'` 必然 0 命中——那不是"库里缺这个"，而是**没法核对**。
把它算作缺失，就造出一个**永远不可能达标**的硬闸门（阈值 0.5，实测恒为 0.0），
这一节无论检索多少轮、抽取多少篇都写不出来。
"""
from __future__ import annotations

import unittest

from research_agent.config import Settings
from research_agent.db import connect
from research_agent.logging import configure, reset
from research_agent.ontology.store import init_ontology
from research_agent.writing import sufficiency as suf

from _tmpdir import make_temp_dir


class LibraryLanguageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.db = connect(f"{self.tmp.name}/lang.db")
        init_ontology(self.db)      # 本体表是懒建的，不建就没有 ontology_nodes
        reset()
        configure(path=f"{self.tmp.name}/dsh.jsonl", to_stderr=False,
                  mirror_to_db=False)

    def tearDown(self) -> None:
        self.db.close()
        configure(path=None, to_stderr=False)
        self.tmp.cleanup()

    def _seed(self, names: list[str]) -> None:
        for index, name in enumerate(names):
            self.db.execute(
                "INSERT INTO ontology_nodes(node_id, node_type, name,"
                " normalized_name, first_seen_at, last_seen_at)"
                " VALUES(?,?,?,?,?,?)",
                (index + 1, "concept", name, str(name).lower(), "t", "t"))
        self.db.commit()

    def test_has_cjk(self):
        self.assertTrue(suf._has_cjk("自变量按溶剂"))
        self.assertFalse(suf._has_cjk("solvent effect"))

    def test_english_library_detected(self):
        self._seed(["Ynamides", "Gold catalysis", "Alkaloids", "Terpenoids"])
        self.assertTrue(suf._library_looks_ascii(self.db))

    def test_chinese_library_not_flagged(self):
        self._seed(["炔酰胺", "金催化", "生物碱", "萜类"])
        self.assertFalse(suf._library_looks_ascii(self.db))

    def test_empty_library_is_not_assumed_english(self):
        self.assertFalse(suf._library_looks_ascii(self.db))

    def test_chinese_token_against_english_library_is_unverifiable(self):
        self._seed(["Ynamides", "Gold catalysis"])
        self.assertEqual(
            suf._requirement_status(self.db, "自变量按溶剂", True),
            "unverifiable")

    def test_chinese_token_against_chinese_library_is_missing(self):
        """库本来就是中文的 ⇒ 查不到就是真的缺，不能当"没法核对"。"""
        self._seed(["炔酰胺", "金催化"])
        self.assertEqual(
            suf._requirement_status(self.db, "自变量按溶剂", False),
            "missing")

    def test_english_token_against_english_library_stays_missing(self):
        self._seed(["Ynamides", "Gold catalysis"])
        self.assertEqual(
            suf._requirement_status(self.db, "solvent effect", True),
            "missing")

    def test_covered_token_is_covered(self):
        self._seed(["Ynamides", "Gold catalysis"])
        self.assertEqual(
            suf._requirement_status(self.db, "ynamides", True), "covered")


class RequirementGateTest(unittest.TestCase):
    """闸门层面：无法核对的词元不进分母，但必须如实上报。"""

    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.db = connect(f"{self.tmp.name}/gate.db")
        init_ontology(self.db)
        reset()
        configure(path=f"{self.tmp.name}/dsh.jsonl", to_stderr=False,
                  mirror_to_db=False)
        # 英文库：既有能被覆盖的词，也有中文要求
        for index, name in enumerate(["Ynamides", "Gold catalysis",
                                      "Regioselectivity", "Alkaloids"]):
            self.db.execute(
                "INSERT INTO ontology_nodes(node_id, node_type, name,"
                " normalized_name, first_seen_at, last_seen_at)"
                " VALUES(?,?,?,?,?,?)",
                (index + 1, "concept", name, str(name).lower(), "t", "t"))
        self.db.commit()
        self.settings = Settings(db_path=str(self.db))

    def tearDown(self) -> None:
        self.db.close()
        configure(path=None, to_stderr=False)
        self.tmp.cleanup()

    def _evaluate(self, instruction: str, **overrides):
        opts = {"section_requirement_unverifiable_ok": True}
        opts.update(overrides)
        settings = Settings(db_path=str(self.db), **opts)
        return suf.evaluate_sufficiency(
            self.db, plan={}, instruction=instruction, heading="实验设计",
            settings=settings, section_key="design",
            genre="experiment_protocol")

    def test_chinese_requirements_do_not_fake_a_missing_gap(self):
        result = self._evaluate("自变量按溶剂、因变量为产率、采用阳性对照")
        counts = result["counts"]
        self.assertEqual(counts["requirements_checkable"], 0)
        self.assertEqual(counts["requirements_missing"], [])
        self.assertTrue(counts["requirements_unverifiable"])
        self.assertTrue(counts["library_ascii"])
        self.assertEqual(result["dimensions"]["requirements"], 1.0)
        self.assertTrue(any("无法在库内核对" in r for r in result["reasons"]))

    def test_flag_off_restores_the_old_permanently_failing_gate(self):
        """保留回退开关：关掉后回到旧行为（中文要求一律算缺失）。"""
        result = self._evaluate("自变量按溶剂、因变量为产率、采用阳性对照",
                                section_requirement_unverifiable_ok=False)
        counts = result["counts"]
        self.assertEqual(counts["requirements_unverifiable"], [])
        self.assertTrue(counts["requirements_missing"])
        self.assertEqual(result["dimensions"]["requirements"], 0.0)
        gate = next(g for g in result["gates"] if g["gate"] == "requirements")
        self.assertFalse(gate["passed"], "关掉开关后该闸门应判失败")

    def test_english_requirements_are_still_gated_honestly(self):
        """英文要求 + 英文库：查不到就是真缺失，闸门照常拦。"""
        result = self._evaluate("comparison of solvent effect on selectivity")
        counts = result["counts"]
        self.assertEqual(counts["requirements_unverifiable"], [])
        self.assertTrue(counts["requirements_missing"])
        self.assertLess(result["dimensions"]["requirements"], 0.5)

    def test_covered_and_unverifiable_mix(self):
        """能核对的全覆盖 ⇒ 该维度满分；无法核对的不参与打分。"""
        result = self._evaluate("ynamides and gold catalysis 自变量按溶剂")
        counts = result["counts"]
        self.assertTrue(counts["requirements_covered"])
        self.assertTrue(counts["requirements_unverifiable"])
        self.assertEqual(counts["requirements_missing"], [])
        self.assertEqual(result["dimensions"]["requirements"], 1.0)


if __name__ == "__main__":
    unittest.main()
