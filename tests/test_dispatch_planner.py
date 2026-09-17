"""自然语言派工的单元测试。

守两件事：**只认登记表里的任务**（模型编出来的任务名必须被丢掉，不能假装能执行），
以及**兜底要诚实**（模型不可用时仍能给出三个真正不同的方案，并说明是兜底）。
"""
from __future__ import annotations

import unittest

from research_agent.db import connect
from research_agent.logging import configure, reset
from research_agent.writing import dispatch_planner as dpl
from research_agent.writing import node_registry as reg

from _tmpdir import make_temp_dir


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class ScriptedModel:
    """按脚本返回 JSON 的假模型。"""

    model_name = "fake-planner"

    def __init__(self, content: str) -> None:
        self.content = content

    def invoke(self, messages, *args, **kwargs):
        return FakeMessage(self.content)


class BrokenModel:
    model_name = "broken"

    def invoke(self, messages, *args, **kwargs):
        raise RuntimeError("503 model overloaded")


class DetectIntentTest(unittest.TestCase):
    def test_chinese_keywords(self):
        self.assertIn("extract", dpl.detect_intents("把所有 ynamide 文献抽成知识"))
        self.assertIn("retrieve", dpl.detect_intents("帮我补检一下这个主题"))
        self.assertIn("review", dpl.detect_intents("审核这一段"))

    def test_english_keywords(self):
        self.assertIn("retrieve", dpl.detect_intents("please search more papers"))
        self.assertIn("fact_check", dpl.detect_intents("fact check the draft"))

    def test_unknown_text_yields_nothing(self):
        self.assertEqual(dpl.detect_intents("嗯"), [])

    def test_matched_intents_have_registered_tasks(self):
        for intent, task in dpl.INTENT_TASKS.items():
            self.assertIsNotNone(reg.get_task(task),
                                 f"意图 {intent} 指向未登记的任务 {task}")


class PlanTemplateTest(unittest.TestCase):
    def test_retrieval_request_gives_three_variants(self):
        plans = dpl.plan_templates(["retrieve"])
        self.assertEqual([p["id"] for p in plans], ["A", "B", "C"])
        self.assertEqual([s["task"] for s in plans[0]["steps"]], ["retrieve"])
        self.assertEqual(plans[1]["steps"][1]["scope"], "new")
        self.assertEqual(plans[2]["steps"][1]["scope"], "existing")

    def test_extraction_only_request_does_not_offer_retrieve_only(self):
        """用户没说要检索时，"只检索"的方案是答非所问，不能摆上去。"""
        plans = dpl.plan_templates(["extract"])
        for plan in plans:
            tasks = [s["task"] for s in plan["steps"]]
            self.assertIn("extract_knowledge", tasks,
                          f"方案 {plan['id']} 没有执行用户要求的抽取")
        # 方案 B 是"只抽既有"——不花检索配额
        self.assertEqual([s["task"] for s in plans[1]["steps"]],
                         ["extract_knowledge"])

    def test_extra_intents_are_kept_in_every_plan(self):
        """用户提到的每一项都要出现在每个方案里——三选一不能吞掉要求。"""
        plans = dpl.plan_templates(["review", "fact_check"])
        for plan in plans:
            tasks = [s["task"] for s in plan["steps"]]
            self.assertIn("review", tasks)
            self.assertIn("fact_check", tasks)

    def test_standalone_task_is_not_forced_into_retrieve_extract(self):
        """"重建本体"这类请求不能被塞进"检索 + 抽取"的模子。"""
        plans = dpl.plan_templates(["ontology"])
        self.assertEqual([s["task"] for s in plans[0]["steps"]],
                         ["rebuild_ontology"])
        for plan in plans:
            for step in plan["steps"]:
                self.assertNotEqual(step["task"], "retrieve")


class ParseDispatchRequestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.db = connect(f"{self.tmp.name}/d.db")
        reset()
        configure(path=f"{self.tmp.name}/dsh.jsonl", to_stderr=False,
                  mirror_to_db=False)

    def tearDown(self) -> None:
        self.db.close()
        configure(path=None, to_stderr=False)
        self.tmp.cleanup()

    def test_fallback_gives_three_executable_plans(self):
        out = dpl.parse_dispatch_request(request="把所有 ynamide 文献抽成知识",
                                         topic="gold catalysis ynamide")
        self.assertEqual(out["parsed_by"], "fallback")
        self.assertIn("extract", out["intents"])
        # 空库下"只抽既有文献"的方案没有目标，会被去掉——留下的每个都必须能跑
        self.assertGreaterEqual(len(out["plans"]), 1)
        self.assertEqual([p["id"] for p in out["plans"]],
                         ["ABC"[i] for i in range(len(out["plans"]))])
        for plan in out["plans"]:
            self.assertTrue(plan["plan"], f"方案 {plan['id']} 没有任何任务")
            for step in plan["plan"]:
                self.assertIsNotNone(reg.get_task(step["task"]),
                                     f"方案里出现了未登记任务 {step['task']}")

    def test_every_step_has_a_registered_node(self):
        out = dpl.parse_dispatch_request(request="检索并抽取知识",
                                         topic="perovskite solar cell")
        for plan in out["plans"]:
            for step in plan["plan"]:
                node = reg.get_node(step["node"])
                self.assertIsNotNone(node, f"未登记节点 {step['node']}")
                self.assertIn(step["task"],
                              [t.task for t in node.tasks])

    def test_retrieval_step_carries_query_terms(self):
        out = dpl.parse_dispatch_request(request="补充一些文献",
                                         topic="gold catalysis ynamide")
        retrieve = out["plans"][0]["plan"][0]
        self.assertEqual(retrieve["task"], "retrieve")
        self.assertTrue(retrieve["args"]["seed_terms"])
        self.assertLessEqual(len(retrieve["args"]["seed_terms"]), 6)

    def test_extract_chains_from_retrieval(self):
        out = dpl.parse_dispatch_request(request="检索并抽知识",
                                         topic="gold catalysis")
        plan_b = next(p for p in out["plans"] if p["id"] == "B")
        extract = next(s for s in plan_b["plan"]
                       if s["task"] == "extract_knowledge")
        self.assertEqual(extract["args"]["paper_keys"], "$step0.paper_keys")

    def test_unknown_intent_from_model_is_dropped(self):
        """模型编出来的任务名必须被丢掉——宁可少做，不可假装能执行。"""
        model = ScriptedModel('{"intents": ["retrieve", "summon_dragon"],'
                              ' "query_terms": ["gold"], "year_from": 0}')
        out = dpl.parse_dispatch_request(request="随便", topic="gold",
                                         model=model)
        self.assertEqual(out["dropped"], ["summon_dragon"])
        self.assertNotIn("summon_dragon", out["intents"])
        for plan in out["plans"]:
            for step in plan["plan"]:
                self.assertIsNotNone(reg.get_task(step["task"]))

    def test_model_year_and_queries_are_used(self):
        model = ScriptedModel('{"intents": ["retrieve"],'
                              ' "query_terms": ["ynamide", "gold catalysis"],'
                              ' "year_from": 2019, "note": "补近五年"}')
        out = dpl.parse_dispatch_request(request="补文献", topic="x",
                                         model=model)
        self.assertEqual(out["parsed_by"], "llm")
        self.assertEqual(out["query_terms"], ["ynamide", "gold catalysis"])
        retrieve = out["plans"][0]["plan"][0]
        self.assertEqual(retrieve["args"]["year_from"], 2019)
        self.assertEqual(retrieve["args"]["seed_terms"],
                         ["ynamide", "gold catalysis"])

    def test_model_failure_falls_back_honestly(self):
        out = dpl.parse_dispatch_request(request="补充文献", topic="gold",
                                         model=BrokenModel())
        self.assertEqual(out["parsed_by"], "fallback")
        self.assertIn("失败", out["note"])
        self.assertTrue(out["plans"][0]["plan"])

    def test_model_garbage_still_yields_plans(self):
        out = dpl.parse_dispatch_request(request="抽知识", topic="gold",
                                         model=ScriptedModel("not json at all"))
        self.assertTrue(out["plans"][0]["plan"])
        self.assertIn("extract", out["intents"])

    def test_unrecognised_request_defaults_to_retrieval(self):
        out = dpl.parse_dispatch_request(request="嗯", topic="gold catalysis")
        self.assertEqual(out["intents"], ["retrieve"])
        self.assertIn("未识别", out["note"])

    def test_existing_scope_without_targets_is_marked_for_skip(self):
        """抽"库内既有文献"但查不到目标 ⇒ 保留这一步并**注明跳过原因**。

        保留（而不是删掉）是因为这一步可能是用户点名要的：让他看到"计划里有，
        但当前没得抽"，比方案里悄悄少一步更诚实。
        """
        out = dpl.parse_dispatch_request(request="抽库内既有文献的知识",
                                         topic="gold catalysis",
                                         conn=self.db)
        self.assertIn("extract", out["intents"])
        seen = [s for plan in out["plans"] for s in plan["plan"]
                if s["task"] == "extract_knowledge"
                and s["args"].get("scope") == "existing"]
        self.assertTrue(seen, "用户点名的抽取步骤不该被抹掉")
        for step in seen:
            self.assertIn("库内没有", step.get("skip_reason", ""))

    def test_auto_added_step_is_dropped_when_impossible(self):
        """自动追加的辅助步骤条件不满足时删掉，免得方案里挂一堆"注定跳过"。"""
        out = dpl.parse_dispatch_request(request="补检一下", topic="gold",
                                         conn=self.db)
        for plan in out["plans"]:
            for step in plan["plan"]:
                if step["task"] == "assess_quality":
                    self.fail("检索方案里不该出现注定跳过的质量评估")

    def test_plans_can_be_handed_to_the_executor(self):
        """解析结果必须能直接喂给派工执行器（这是这一层的意义）。

        刻意用**不碰网络**的任务（重建本体）验证形状，否则这个单测会去真检索——
        回归测试绝不能依赖外网。
        """
        from research_agent.writing import dispatch as dp

        out = dpl.parse_dispatch_request(request="重建本体视图", topic="gold",
                                         conn=self.db)
        plan = out["plans"][0]["plan"]
        self.assertIn("rebuild_ontology", [s["task"] for s in plan])
        record = dp.run_dispatch(plan=plan, conn=self.db,
                                 origin="user_direct")
        self.assertEqual(record["status"], "done")
        self.assertTrue(record["steps"])


class BuildDispatchPlanTest(unittest.TestCase):
    """对外入口：带项目上下文、可离线，并且结果能直接下单。"""

    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.db_path = f"{self.tmp.name}/plan.db"
        self.db = connect(self.db_path)
        reset()
        configure(path=f"{self.tmp.name}/dsh.jsonl", to_stderr=False,
                  mirror_to_db=False)

    def tearDown(self) -> None:
        self.db.close()
        configure(path=None, to_stderr=False)
        self.tmp.cleanup()

    def test_offline_entry_does_not_touch_the_network(self):
        out = dpl.build_dispatch_plan(request="把所有 ynamide 文献抽成知识",
                                      conn=self.db, topic="gold catalysis",
                                      use_model=False)
        self.assertTrue(out["ok"])
        self.assertEqual(out["parsed_by"], "fallback")
        self.assertTrue(out["plans"])

    def test_project_context_is_used_for_query_terms(self):
        from research_agent.writing.service import create_project

        project_id = create_project(self.db, title="炔酰胺", genre="frontier_review",
                                    topic="gold catalysis ynamide")
        out = dpl.build_dispatch_plan(request="补检文献", conn=self.db,
                                      project_id=int(project_id),
                                      use_model=False)
        self.assertEqual(out["project_id"], int(project_id))
        retrieve = out["plans"][0]["plan"][0]
        self.assertIn("ynamide", " ".join(retrieve["args"]["seed_terms"]))

    def test_unknown_project_is_tolerated(self):
        out = dpl.build_dispatch_plan(request="补检文献", conn=self.db,
                                      project_id=99999, use_model=False)
        self.assertTrue(out["ok"])
        self.assertTrue(out["plans"])

    def test_result_can_be_dispatched(self):
        from research_agent.writing import dispatch as dp

        out = dpl.build_dispatch_plan(request="重建本体视图", conn=self.db,
                                      use_model=False)
        plan = out["plans"][0]["plan"]
        record = dp.run_dispatch(plan=plan, conn=self.db)
        self.assertEqual(record["status"], "done")

    def test_api_endpoint_offline(self):
        from fastapi.testclient import TestClient
        from research_agent.config import Settings
        from research_agent.dashboard.app import create_app

        client = TestClient(create_app(
            self.db_path, inject_llms=False,
            settings=Settings(db_path=self.db_path)))
        resp = client.post("/api/dispatches/parse",
                           json={"request": "把文献抽成知识", "topic": "gold",
                                 "use_model": False})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["plans"])
        empty = client.post("/api/dispatches/parse", json={"request": ""})
        self.assertFalse(empty.json()["ok"])


if __name__ == "__main__":
    unittest.main()
