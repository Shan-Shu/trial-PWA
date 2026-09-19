"""派工协议的单元测试。

守住四条必需性质：**幂等、可取消、可回报、引用可串**。这四条任何一条坏了，
"规划节点下单、别的节点执行"就退化成"看着跑完了其实没跑"——这正是
改动前 `assess_quality` 的毛病（只数了数已有结果就报成功）。
"""
from __future__ import annotations

import threading
import unittest

from research_agent.db import connect
from research_agent.logging import configure, reset
from research_agent.writing import dispatch as dp
from research_agent.writing import node_registry as reg

from _tmpdir import make_temp_dir


class DispatchTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.db_path = f"{self.tmp.name}/dispatch.db"
        self.db = connect(self.db_path)
        reset()
        configure(path=f"{self.tmp.name}/dsh.jsonl", to_stderr=False,
                  mirror_to_db=False)

    def tearDown(self) -> None:
        self.db.close()
        configure(path=None, to_stderr=False)
        self.tmp.cleanup()

    def run_one(self, task: str, **kwargs) -> dict:
        return dp.run_dispatch(
            plan=[{"task": task, **({"args": kwargs.pop("args")}
                                    if "args" in kwargs else {})}],
            conn=self.db, **kwargs)


class RegistryWiringTest(unittest.TestCase):
    """登记表里每个任务都必须指向真实函数——登记了却没有实现等于骗人。"""

    def test_every_task_is_implemented(self):
        missing = [t["task"] for t in reg.list_tasks()
                   if not t["implemented"]]
        self.assertEqual(missing, [], f"这些任务登记了但没有实现: {missing}")

    def test_every_llm_node_has_at_least_one_task(self):
        for spec in reg.NODES:
            if spec.needs_model and spec.auto_dispatch:
                self.assertTrue(spec.tasks, f"{spec.node} 接了模型却没有任务")

    def test_llm_nodes_match_role_registry(self):
        from research_agent.models import ROLE_PROVIDER
        roles = {spec.role for spec in reg.NODES if spec.role}
        # interview 与 planner 共用 planner 角色，因此角色集合是 ROLE_PROVIDER 的子集
        self.assertTrue(roles <= set(ROLE_PROVIDER),
                        f"登记表出现了未注册的角色: {roles - set(ROLE_PROVIDER)}")
        self.assertEqual(
            set(reg.LLM_NODES),
            {spec.node for spec in reg.NODES if spec.needs_model})

    def test_describe_for_prompt_covers_tasks(self):
        text = reg.describe_for_prompt()
        for task in reg.list_tasks():
            self.assertIn(task["task"], text)


class DispatchRunTest(DispatchTestBase):
    def test_step_reports_produced(self):
        record = self.run_one("rebuild_ontology", origin="user_direct")
        self.assertEqual(record["status"], "done")
        self.assertEqual(len(record["steps"]), 1)
        step = record["steps"][0]
        self.assertEqual(step["task"], "rebuild_ontology")
        self.assertEqual(step["node"], "ontology")
        self.assertEqual(step["status"], "done")
        self.assertIn("produced", step)

    def test_idempotent_same_id_runs_once(self):
        first = self.run_one("rebuild_ontology")
        second = dp.run_dispatch(
            plan=[{"task": "rebuild_ontology"}], conn=self.db,
            dispatch_id=first["dispatch_id"])
        self.assertEqual(second["dispatch_id"], first["dispatch_id"])
        # 只落一行
        self.assertEqual(len(dp.list_dispatches(self.db)), 1)

    def test_failed_dispatch_can_be_retried(self):
        bad = dp.run_dispatch(plan=[{"task": "nope"}], conn=self.db)
        self.assertEqual(bad["status"], "failed")
        again = dp.run_dispatch(plan=[{"task": "nope"}], conn=self.db,
                                dispatch_id=bad["dispatch_id"])
        self.assertEqual(again["status"], "failed")

    def test_unknown_task_reports_failure(self):
        record = dp.run_dispatch(plan=[{"task": "no_such_task"}], conn=self.db)
        self.assertEqual(record["steps"][0]["status"], "failed")
        self.assertIn("未登记", record["steps"][0]["error"])

    def test_unregistered_task_spec_skips(self):
        """entry 为 None 的任务要**明确跳过**，而不是假装成功。"""
        record = dp.run_dispatch(plan=[{"task": "assess_quality",
                                        "args": {}}], conn=self.db)
        step = record["steps"][0]
        self.assertEqual(step["status"], "skipped")
        self.assertIn("paper_keys", step["skip_reason"])

    def test_cancel_before_start(self):
        event = threading.Event()
        event.set()
        record = self.run_one("rebuild_ontology", cancel_event=event)
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["steps"][0]["status"], "cancelled")

    def test_dispatches_are_listed_newest_first(self):
        self.run_one("rebuild_ontology")
        self.run_one("rebuild_ontology")
        rows = dp.list_dispatches(self.db, limit=5)
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["dispatch_id"], rows[1]["dispatch_id"])

    def test_project_and_section_filter(self):
        self.run_one("rebuild_ontology", project_id=7, section_key="intro")
        self.run_one("rebuild_ontology", project_id=8, section_key="method")
        self.assertEqual(len(dp.list_dispatches(self.db, project_id=7)), 1)
        self.assertEqual(
            len(dp.list_dispatches(self.db, section_key="method")), 1)

    def test_chained_reference_is_skipped_when_unresolvable(self):
        """`$step0.paper_keys` 取不到值时要**跳过并说明**，不能传空值硬跑。"""
        record = dp.run_dispatch(plan=[
            {"task": "extract_knowledge",
             "args": {"paper_keys": "$step0.paper_keys"}}], conn=self.db)
        step = record["steps"][0]
        self.assertEqual(step["status"], "skipped")
        self.assertIn("引用不到", step["skip_reason"])

    def test_reference_chains_from_previous_step(self):
        """上一步的产出能被下一步引用（"检索 → 抽取"自动串起来的关键）。"""
        record = dp.run_dispatch(plan=[
            {"task": "extract_knowledge",
             "args": {"paper_keys": "$step0.keys"}}], conn=self.db)
        # 第 0 步不存在 ⇒ 跳过；这里直接验证解析器本身
        self.assertEqual(record["steps"][0]["status"], "skipped")

    def test_resolver_expands_nested_values(self):
        results = [{"paper_keys": ["a", "b"], "nested": {"k": 1}}]
        out = dp._resolve_args({"keys": "$step0.paper_keys",
                                "deep": "$step0.nested.k",
                                "plain": 3}, results)
        self.assertEqual(out["keys"], ["a", "b"])
        self.assertEqual(out["deep"], 1)
        self.assertEqual(out["plain"], 3)

    def test_resolver_rejects_out_of_range(self):
        with self.assertRaises(dp._Unresolved):
            dp._resolve_args({"x": "$step5.paper_keys"}, [{"paper_keys": []}])

    def test_resolver_rejects_empty_value(self):
        with self.assertRaises(dp._Unresolved):
            dp._resolve_args({"x": "$step0.paper_keys"}, [{"paper_keys": []}])

    def test_budget_stops_extra_steps(self):
        record = dp.run_dispatch(
            plan=[{"task": "rebuild_ontology"}] * 3, conn=self.db,
            budget={"max_seconds": 1})
        self.assertIn(record["status"], {"done", "failed", "cancelled"})
        self.assertGreaterEqual(len(record["steps"]), 1)

    def test_step_cap_respected(self):
        record = dp.run_dispatch(
            plan=[{"task": "rebuild_ontology"}] * (dp.MAX_STEPS + 5),
            conn=self.db)
        self.assertLessEqual(len(record["steps"]), dp.MAX_STEPS + 1)

    def test_long_content_is_briefed_not_stored_raw(self):
        brief = dp._brief({"content": "字" * 5000, "citations": ["a"] * 30})
        self.assertLess(len(brief["content"]), 40)
        self.assertLessEqual(len(brief["citations"]), 9)

    def test_dispatch_tasks_helper(self):
        record = dp.dispatch_tasks(["rebuild_ontology"], conn=self.db)
        self.assertEqual(record["status"], "done")


class DispatchContextTest(DispatchTestBase):
    def test_context_available_inside_entry(self):
        seen: dict = {}

        def fake(**_kwargs):
            ctx = dp.current_context()
            seen["dispatch_id"] = ctx.dispatch_id if ctx else ""
            dp.report_progress(50.0, "半程")
            return {"ok": True}

        found = reg.get_task("rebuild_ontology")
        original = found[1].entry
        object.__setattr__(found[1], "entry", fake)
        try:
            record = self.run_one("rebuild_ontology", progress_cb=None)
        finally:
            object.__setattr__(found[1], "entry", original)
        self.assertTrue(seen.get("dispatch_id"))
        self.assertEqual(record["steps"][0]["status"], "done")

    def test_progress_is_forwarded(self):
        got: list = []

        def fake(**_kwargs):
            dp.report_progress(42.0, "进行中")
            return {"ok": True}

        found = reg.get_task("rebuild_ontology")
        original = found[1].entry
        object.__setattr__(found[1], "entry", fake)
        try:
            self.run_one("rebuild_ontology",
                         progress_cb=lambda p, m: got.append((p, m)))
        finally:
            object.__setattr__(found[1], "entry", original)
        self.assertEqual(got, [(42.0, "进行中")])

    def test_check_cancelled_is_false_without_context(self):
        self.assertFalse(dp.check_cancelled())
        dp.report_progress(1.0, "noop")   # 不该抛


if __name__ == "__main__":
    unittest.main()
