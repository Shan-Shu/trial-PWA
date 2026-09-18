"""补检预算必须真的会用尽（否则"补检 → 仍不足 → 再补检"会无限循环）。

这是实测踩到的坑：判定只看到"这一轮跑了多少轮"，而每轮协作都把
`rounds_done` 覆盖成当次值，于是预算永远显示没用完，用户一圈圈点下去，
每一圈都真花检索配额。
"""
from __future__ import annotations

import unittest

from research_agent.db import connect
from research_agent.logging import configure, reset
from research_agent.writing import interview as iv
from research_agent.writing.service import create_project

from _tmpdir import make_temp_dir


class RoundBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.db = connect(f"{self.tmp.name}/budget.db")
        reset()
        configure(path=f"{self.tmp.name}/dsh.jsonl", to_stderr=False,
                  mirror_to_db=False)
        self.project_id = int(create_project(
            self.db, "预算测试", genre="experiment_protocol",
            topic="gold catalysis"))
        iv.start_interview(self.db, self.project_id, genre="experiment_protocol",
                           topic="gold catalysis",
                           sections=["objective"])
        # 走完前置问答
        iv.answer(self.db, self.project_id,
                  {"kind": "intake", "step": "topic", "value": "gold catalysis"})
        iv.answer(self.db, self.project_id,
                  {"kind": "intake", "step": "sections",
                   "value": ["objective"]})

    def tearDown(self) -> None:
        self.db.close()
        configure(path=None, to_stderr=False)
        self.tmp.cleanup()

    def _state(self):
        return iv.load_state(self.db, self.project_id)

    def _to_gap(self, round_total: int = 0) -> None:
        """把这一节推进到"等缺口决定"，并预设已用轮数。"""
        state = self._state()
        sec = state.section("objective")
        sec.stage = iv.STAGE_AWAITING_GAP
        sec.verdict = {"decision": "exhausted",
                       "unmet_dimensions": ["conditions"]}
        sec.rounds_total = round_total
        iv.save_state(self.db, self.project_id, state)

    def test_rounds_accumulate_across_collect_decisions(self):
        """累计值单调增长，而不是被每次协作覆盖。"""
        state = self._state()
        sec = state.section("objective")
        sec.rounds_total = 2
        iv.save_state(self.db, self.project_id, state)
        self._to_gap(round_total=2)
        # 再决定一次补检：本次上限应被剩余额度限制
        iv.answer(self.db, self.project_id,
                  {"kind": "gap_decision", "section_key": "objective",
                   "decision": iv.GAP_COLLECT, "rounds": 2})
        sec = self._state().section("objective")
        self.assertEqual(sec.stage, iv.STAGE_COLLABORATING)
        self.assertLessEqual(sec.collection_rounds,
                             iv.MAX_COLLECTION_ROUNDS - 2,
                             "本次轮数应受剩余额度限制")

    def test_collect_is_refused_once_budget_is_spent(self):
        """累计到硬上限后，服务端直接拒绝补检——这才是循环的终点。"""
        self._to_gap(round_total=iv.MAX_COLLECTION_ROUNDS)
        with self.assertRaises(ValueError) as ctx:
            iv.answer(self.db, self.project_id,
                      {"kind": "gap_decision", "section_key": "objective",
                       "decision": iv.GAP_COLLECT, "rounds": 2})
        message = str(ctx.exception)
        self.assertIn("已累计补检", message)
        self.assertIn("保留缺口", message)

    def test_keep_gap_still_works_when_budget_spent(self):
        """预算用尽不代表走不下去：保留缺口照常撰写必须始终可用。"""
        self._to_gap(round_total=iv.MAX_COLLECTION_ROUNDS)
        iv.answer(self.db, self.project_id,
                  {"kind": "gap_decision", "section_key": "objective",
                   "decision": iv.GAP_KEEP})
        self.assertEqual(self._state().section("objective").stage,
                         iv.STAGE_WRITING)

    def test_custom_task_still_works_when_budget_spent(self):
        self._to_gap(round_total=iv.MAX_COLLECTION_ROUNDS)
        iv.answer(self.db, self.project_id,
                  {"kind": "gap_decision", "section_key": "objective",
                   "decision": iv.GAP_CUSTOM, "text": "只补 3 篇新文献"})
        self.assertEqual(self._state().section("objective").stage,
                         iv.STAGE_AWAITING_CUSTOM)

    def test_question_reports_budget_state(self):
        """问题里要如实带上"花了多少、还剩多少、能不能再补"。"""
        self._to_gap(round_total=2)
        q = iv.current_question(self.db, self.project_id)
        self.assertEqual(q["kind"], "gap_decision")
        self.assertEqual(q["rounds_spent"], 2)
        self.assertEqual(q["rounds_remaining"],
                         iv.MAX_COLLECTION_ROUNDS - 2)
        self.assertFalse(q["budget_spent"])
        collect = next(o for o in q["options"] if o["id"] == iv.GAP_COLLECT)
        self.assertFalse(collect.get("disabled", False))
        self.assertIn("已累计补检 2 轮", collect["hint"])

    def test_question_disables_collect_when_exhausted(self):
        self._to_gap(round_total=iv.MAX_COLLECTION_ROUNDS)
        q = iv.current_question(self.db, self.project_id)
        self.assertTrue(q["budget_spent"])
        self.assertEqual(q["rounds_remaining"], 0)
        collect = next(o for o in q["options"] if o["id"] == iv.GAP_COLLECT)
        self.assertTrue(collect["disabled"])
        self.assertIn("不会再产生可用证据", collect["hint"])
        # 另外两项仍可用，且被标为推荐
        for option in q["options"]:
            if option["id"] in (iv.GAP_KEEP, iv.GAP_CUSTOM):
                self.assertFalse(option.get("disabled", False))
                self.assertTrue(option.get("recommended"))

    def test_rounds_total_survives_reload(self):
        """累计值必须持久化，否则重开页面又"忘了"已经补过几轮。"""
        self._to_gap(round_total=3)
        state = iv.load_state(self.db, self.project_id)
        self.assertEqual(state.section("objective").rounds_total, 3)

    def test_rounds_total_defaults_to_zero(self):
        state = self._state()
        self.assertEqual(state.section("objective").rounds_total, 0)


if __name__ == "__main__":
    unittest.main()
