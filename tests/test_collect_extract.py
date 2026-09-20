"""补检必须带抽取，否则判定永远不会变。

来源是一个真实卡住的案例：判定缺 conditions，用户点了「执行检索补全」，
协作报告是 ``tasks=['retrieve']``、``added=116``、``extracted=0``——抓回 116 篇
但一篇都没抽。而判定的六个维度里，只有 papers 读检索产物，knowledge /
conditions / requirements / evidence / comparison 全部读抽取产物，所以复判结论
一个字都没变，用户反复点补检也永远卡在同一处。
"""
from __future__ import annotations

import unittest
from unittest import mock

from research_agent.config import Settings
from research_agent.db import connect
from research_agent.logging import configure, reset
from research_agent.writing import collaboration as col
from research_agent.writing import interview as iv

from _tmpdir import make_temp_dir


class FakeSection:
    """最小可用的 SectionPlanState 替身（只用到这几个字段）。"""

    def __init__(self, verdict=None, gap=iv.GAP_COLLECT, rounds=2,
                 custom_plans=None, custom_choice="", key="design"):
        self.key = key
        self.gap_decision = gap
        self.verdict = verdict or {"unmet_dimensions": ["conditions"]}
        self.collection_rounds = rounds
        self.custom_plans = custom_plans or []
        self.custom_choice = custom_choice
        self.collaboration = {}
        self.rounds_total = 0


class KnowledgeDimensionTest(unittest.TestCase):
    def test_knowledge_only_dimensions_are_exactly_the_extraction_ones(self):
        """这组常量必须与 sufficiency 的 SQL 来源一致：除 papers 外都读抽取产物。"""
        self.assertNotIn("papers", col.KNOWLEDGE_ONLY_DIMENSIONS)
        for name in ("knowledge", "conditions", "requirements", "evidence",
                     "comparison"):
            self.assertIn(name, col.KNOWLEDGE_ONLY_DIMENSIONS)


class PlainCollectIncludesExtractionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.db = connect(f"{self.tmp.name}/collab.db")
        reset()
        configure(path=f"{self.tmp.name}/dsh.jsonl", to_stderr=False,
                  mirror_to_db=False)

    def tearDown(self) -> None:
        self.db.close()
        configure(path=None, to_stderr=False)
        self.tmp.cleanup()

    def _run(self, section, *, retrieved=116, extracted=7, extract_note="",
             duplicates=0, exhausted=False):
        """用假执行体跑 run_collaboration，只看"决定做什么"。"""
        calls: dict = {}

        def fake_retrieve(conn, *, section_key, queries, rounds_cap,
                          settings, progress_cb):
            calls["retrieve"] = {"queries": list(queries),
                                 "rounds_cap": rounds_cap}
            step = {"round": 1, "task": "retrieve", "ok": True,
                    "count": retrieved,
                    "paper_keys": [f"p{i}" for i in range(retrieved)]}
            if duplicates:
                step["duplicates_skipped"] = duplicates
            return retrieved, 1, [step], exhausted

        def fake_extract(conn, *, scope, project, section_key, added_keys,
                         settings, progress_cb):
            calls["extract"] = {"scope": scope, "added": len(added_keys)}
            step = {"task": "extract_knowledge", "scope": scope, "ok": True,
                    "count": extracted, "attempted": len(added_keys)}
            if extract_note:
                step["note"] = extract_note
            return extracted, [step]

        with mock.patch.object(col, "_do_retrieve", fake_retrieve), \
                mock.patch.object(col, "_do_extract", fake_extract):
            report = col.run_collaboration(
                self.db, project_id=1, section_key="design",
                section_state=section, project={"topic": "gold catalysis"},
                settings=Settings(db_path=str(self.db)))
        return report, calls

    def test_plain_collect_now_extracts(self):
        section = FakeSection()
        report, calls = self._run(section)
        self.assertIn("extract", calls, "普通补检必须执行抽取")
        self.assertIn("extract_knowledge", report["tasks"])
        self.assertEqual(report["extracted"], 7)
        self.assertIn("抽取 7 篇", report["summary"])

    def test_zero_extraction_is_reported_not_hidden(self):
        """抽取为 0 时必须在摘要里说出来——否则"补检了但没抽"在界面上不可见。"""
        section = FakeSection()
        report, _ = self._run(section, extracted=0,
                              extract_note="抽取未成功：知识提炼模型不可用")
        self.assertIn("抽取 0 篇", report["summary"])
        self.assertIn("模型不可用", report["summary"])

    def test_keep_gap_still_does_nothing(self):
        section = FakeSection(gap=iv.GAP_KEEP)
        report, calls = self._run(section)
        self.assertEqual(report["kind"], "keep_gap")
        self.assertNotIn("extract", calls)
        self.assertNotIn("retrieve", calls)

    def test_custom_plan_keeps_its_own_tasks(self):
        """用户自定义了方案就按方案走，不被"默认加上抽取"覆盖。"""
        section = FakeSection(custom_plans=[
            {"id": "A", "tasks": ["retrieve"], "query_terms": ["gold"]}],
            custom_choice="A")
        report, calls = self._run(section)
        self.assertEqual(report["tasks"], ["retrieve"])
        self.assertNotIn("extract", calls)

    def test_extract_scope_round_trips_into_report(self):
        section = FakeSection()
        report, _ = self._run(section)
        self.assertIn(report["extract_scope"], ("new", "existing", "both"))

    def test_duplicates_are_reported_in_summary(self):
        """库里已有而跳过的候选必须说出来。

        实测一次补检抓回 21 篇、19 篇库里本来就有。若只报「新增 0 篇」，用户
        会以为检索失败；必须让他看到"抓到了，但都是已有的，没重复下载/评估"。
        """
        section = FakeSection()
        report, _ = self._run(section, retrieved=0, duplicates=19,
                              exhausted=True)
        self.assertEqual(report["duplicates_skipped"], 19)
        self.assertIn("跳过库内已有 19 篇", report["summary"])
        self.assertIn("候选均为库内已有", report["summary"])

    def test_zero_new_without_duplicates_says_not_found(self):
        """另一种"新增 0"（压根没抓到）不能被说成"库里都有"。"""
        section = FakeSection()
        report, _ = self._run(section, retrieved=0, duplicates=0, exhausted=True)
        self.assertEqual(report["duplicates_skipped"], 0)
        self.assertNotIn("跳过库内已有", report["summary"])
        self.assertIn("连续无新增", report["summary"])


class ExtractScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.db = connect(f"{self.tmp.name}/scope.db")
        self.db.execute(
            "INSERT INTO papers(paper_key, title, status, created_at, updated_at)"
            " VALUES('p1','A gold catalysis study','ingested','t','t')")
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_pending_existing_count_counts_unextracted(self):
        self.assertEqual(col._pending_existing_count(self.db), 1)
        self.db.execute(
            "INSERT INTO processing_log(paper_key, node, event, ts)"
            " VALUES('p1','knowledge','extracted','t')")
        self.db.commit()
        self.assertEqual(col._pending_existing_count(self.db), 0)

    def test_knowledge_gap_prefers_existing_when_available(self):
        """缺知识类维度、且库里有没抽的文献 ⇒ 优先抽既有（不花检索配额）。"""
        section = FakeSection({"unmet_dimensions": ["knowledge"]})
        self.assertEqual(col._preferred_extract_scope(self.db, section),
                         "both")

    def test_missing_papers_only_prefers_new(self):
        """只缺文献数 ⇒ 抽这次新抓回来的就够。"""
        section = FakeSection({"unmet_dimensions": ["papers"]})
        self.assertEqual(col._preferred_extract_scope(self.db, section), "new")

    def test_no_pending_existing_falls_back_to_new(self):
        self.db.execute(
            "INSERT INTO processing_log(paper_key, node, event, ts)"
            " VALUES('p1','knowledge','extracted','t')")
        self.db.commit()
        section = FakeSection({"unmet_dimensions": ["conditions"]})
        self.assertEqual(col._preferred_extract_scope(self.db, section), "new")

    def test_empty_verdict_is_treated_as_knowledge_gap(self):
        """还没判定过（verdict 为空）时按"知识缺口"处理，宁多抽不空跑。"""
        section = FakeSection({})
        self.assertEqual(col._preferred_extract_scope(self.db, section), "both")


if __name__ == "__main__":
    unittest.main()
