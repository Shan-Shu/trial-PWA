"""统一事件日志的单元测试。

覆盖四件事：**事件名受控**、**脱敏与截断**、**trace 关联**、**代理透明记账**。
这四条是"排查时能一眼看出断在哪一段"的前提，坏了就等于日志层失效。
"""
from __future__ import annotations

import json
import unittest

from research_agent.logging import (
    EVENTS,
    NULL_CONTEXT,
    bind_trace,
    configure,
    current_trace,
    log_event,
    log_llm,
    logged_invoke,
    new_trace,
    read_file,
    recent,
    recent_for_trace,
    redact,
    reset,
    truncate,
)
from research_agent.logging.proxy import LoggedModel, resolve_node

from _tmpdir import make_temp_dir


class FakeMessage:
    def __init__(self, content: str = "ok") -> None:
        self.content = content


class FakeModel:
    """最小可用的假模型：记录收到几条消息，返回固定内容。"""

    model_name = "fake-flash"

    def __init__(self, content: str = "ok") -> None:
        self.content = content
        self.calls: list[list] = []

    def invoke(self, messages, *args, **kwargs):
        self.calls.append(list(messages))
        return FakeMessage(self.content)


class BoomModel:
    model_name = "fake-pro"

    def invoke(self, messages, *args, **kwargs):
        raise RuntimeError("429 rate limited")


class LoggingTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.path = f"{self.tmp.name}/dsh.jsonl"
        reset()                      # 环形缓冲跨测试共享，先清干净
        configure(enabled=True, level="INFO", path=self.path,
                  mirror_to_db=False, to_stderr=False)

    def tearDown(self) -> None:
        configure(path=None, to_stderr=False)
        reset()
        self.tmp.cleanup()

    def lines(self) -> list[dict]:
        return read_file(500)


class EventSchemaTest(LoggingTestBase):
    def test_event_name_must_be_registered(self):
        """事件名是受控词表——写错名字会被测试逮住，避免日志里出现一次性名字。"""
        for evt in ("ui.click", "http.res", "interview.step", "dispatch.created",
                    "llm.call.error", "study.node.skipped"):
            self.assertIn(evt, EVENTS)
        # 词表里每个事件都要有中文说明（看板/文档直接用它）
        for evt, desc in EVENTS.items():
            self.assertTrue(desc.strip(), f"{evt} 缺少说明")

    def test_record_has_required_fields(self):
        rec = log_event("ui.click", node="interview", section="intro",
                        data={"btn": "advance"})
        for key in ("ts", "level", "evt", "trace", "node", "section", "job",
                    "dispatch"):
            self.assertIn(key, rec)
        self.assertEqual(rec["evt"], "ui.click")
        self.assertEqual(rec["level"], "INFO")

    def test_written_line_is_json(self):
        log_event("interview.step", node="interview", data={"action": "judge"})
        rows = self.lines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["data"]["action"], "judge")
        # 文件里必须是合法 JSON（一行一条，可被 jq / tail 消费）
        with open(self.path, "r", encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.readline())["evt"], "interview.step")

    def test_level_filter_keeps_ring_but_not_file(self):
        configure(level="WARN")
        log_event("ui.click")           # INFO：不该落文件
        log_event("dispatch.done", level="ERROR")
        rows = self.lines()
        self.assertEqual([r["evt"] for r in rows], ["dispatch.done"])
        # 环形缓冲仍然留下（失败时能回看上下文）
        self.assertIn("ui.click", [r["evt"] for r in recent(20)])

    def test_disabled_writes_nothing(self):
        configure(enabled=False)
        rec = log_event("ui.click")
        self.assertEqual(rec["evt"], "ui.click")
        self.assertEqual(self.lines(), [])


class RedactTest(LoggingTestBase):
    def test_secret_values_are_masked(self):
        out = redact({"api_key": "sk-live-abcdefghijklmnop",
                      "Authorization": "Bearer x", "note": "ok"})
        self.assertEqual(out["api_key"], "***")
        self.assertEqual(out["Authorization"], "***")
        self.assertEqual(out["note"], "ok")

    def test_key_shaped_text_is_masked(self):
        text = redact("token=ghp_" + "a" * 30)
        self.assertNotIn("ghp_", text)
        self.assertIn("***", text)

    def test_long_text_is_truncated_with_length_marker(self):
        out = truncate("字" * 900)
        self.assertLess(len(out), 900)
        self.assertIn("900字符", out)

    def test_nested_and_list_values(self):
        out = redact({"outer": {"token": "x"}, "items": [{"password": "y"}]})
        self.assertEqual(out["outer"]["token"], "***")
        self.assertEqual(out["items"][0]["password"], "***")

    def test_no_prompt_text_in_llm_events(self):
        """模型事件只记字符数——提示词全文绝不落盘。"""
        secret = "机密提示词" * 50
        log_llm("llm.call.end", node="content_builder", role="content",
                model="fake", ms=1.0, prompt_chars=len(secret),
                output_chars=10)
        log_event("llm.call.end", node="content_builder",
                  data={"prompt_chars": len(secret)})
        with open(self.path, "r", encoding="utf-8") as fh:
            on_disk = fh.read()
        self.assertNotIn(secret, on_disk)
        self.assertNotIn(secret[:20], on_disk)


class TraceTest(LoggingTestBase):
    def test_trace_is_attached_inside_context(self):
        trace = new_trace()
        with bind_trace(trace) as bound:
            self.assertEqual(bound, trace)
            log_event("ui.click")
            log_event("http.res")
        rows = self.lines()
        self.assertEqual({r["trace"] for r in rows}, {trace})
        self.assertEqual(current_trace(), "")

    def test_recent_for_trace_returns_only_that_trace(self):
        other = new_trace()
        with bind_trace(new_trace()):
            log_event("ui.click")
        with bind_trace(other):
            log_event("interview.step", data={"action": "judge"})
        rows = recent_for_trace(other)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["data"]["action"], "judge")

    def test_explicit_trace_overrides_context(self):
        with bind_trace("t-context"):
            log_event("ui.click", trace="t-explicit")
        self.assertEqual(self.lines()[0]["trace"], "t-explicit")

    def test_null_context_is_usable(self):
        with NULL_CONTEXT:
            log_event("ui.click")
        self.assertEqual(self.lines()[0]["trace"], "")


class ProxyTest(LoggingTestBase):
    def test_invoke_is_logged_with_sizes(self):
        model = LoggedModel(FakeModel("hello"), role="content")
        model.invoke("p" * 40)
        evts = [(r["evt"], r.get("node")) for r in recent(10, evt="llm")]
        self.assertEqual(evts, [("llm.call.start", "content_builder"),
                                 ("llm.call.end", "content_builder")])
        end = recent(1, evt="llm.call.end")[0]
        self.assertEqual(end["data"]["prompt_chars"], 40)
        self.assertEqual(end["data"]["output_chars"], 5)
        self.assertEqual(end["data"]["model"], "fake-flash")

    def test_failure_logs_error_and_reraises(self):
        model = LoggedModel(BoomModel(), role="retriever")
        with self.assertRaises(RuntimeError):
            model.invoke("q")
        err = recent(1, evt="llm.call.error")[0]
        self.assertEqual(err["level"], "ERROR")
        self.assertEqual(err["node"], "retrieval")
        self.assertIn("429", err["data"]["error"])

    def test_node_resolution_order(self):
        self.assertEqual(resolve_node("content", "explicit"), "explicit")
        self.assertEqual(resolve_node("content"), "content_builder")
        self.assertEqual(resolve_node("fact_check"), "fact_checker")
        self.assertEqual(resolve_node(""), "")

    def test_other_attributes_pass_through(self):
        model = LoggedModel(FakeModel(), role="content")
        self.assertEqual(model.model_name, "fake-flash")

    def test_logged_invoke_does_not_double_count_proxy(self):
        model = LoggedModel(FakeModel(), role="content")
        logged_invoke(model, "x", node="content_builder")
        starts = recent(20, evt="llm.call.start")
        self.assertEqual(len(starts), 1)

    def test_nested_proxy_does_not_double_count(self):
        """重复包装一个已代理的模型，一次调用仍只能记一条。

        实测出现过三重记账：同一请求落三条 ``llm.call.start``，耗时看着
        像三次调用，排查时会被彻底带偏。
        """
        inner = LoggedModel(FakeModel(), role="planner")
        outer = LoggedModel(inner, role="content", node="content_builder")
        outer.invoke("x" * 5)
        self.assertEqual(len(recent(20, evt="llm.call.start")), 1)
        self.assertEqual(len(recent(20, evt="llm.call.end")), 1)
        end = recent(1, evt="llm.call.end")[0]
        # 显式给出的 role/node 覆盖内层，模型名仍取自真实的那个
        self.assertEqual(end["data"]["role"], "content")
        self.assertEqual(end["node"], "content_builder")
        self.assertEqual(end["data"]["model"], "fake-flash")

    def test_logged_invoke_wraps_bare_model(self):
        logged_invoke(FakeModel(), "x" * 7, node="content_builder",
                      role="content")
        end = recent(1, evt="llm.call.end")[0]
        self.assertEqual(end["data"]["prompt_chars"], 7)


class RingBufferTest(LoggingTestBase):
    def test_recent_filters(self):
        log_event("ui.click", node="interview")
        log_event("http.res", node="app", level="WARN")
        self.assertEqual(len(recent(10, evt="ui")), 1)
        self.assertEqual(len(recent(10, level="WARN")), 1)
        self.assertEqual(len(recent(10, node="app")), 1)

    def test_prefix_filter_matches_family(self):
        log_event("llm.call.start", node="content_builder")
        log_event("llm.call.end", node="content_builder")
        log_event("ui.click")
        self.assertEqual(len(recent(10, evt="llm")), 2)


class FrontendLogEndpointTest(LoggingTestBase):
    """`/api/log` 与 trace 透传：前端点击必须能与后端处理串成一条链。"""

    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient
        from research_agent.config import Settings
        from research_agent.dashboard.app import create_app

        self.client = TestClient(create_app(
            f"{self.tmp.name}/dash.db", inject_llms=False,
            settings=Settings(db_path=f"{self.tmp.name}/dash.db")))

    def test_ui_event_is_recorded(self):
        r = self.client.post("/api/log", json={
            "evt": "ui.click", "data": {"target": "button#wrTopicOk"},
            "trace": "t-1234abcd"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        rows = recent(10, node="ui")
        self.assertEqual(rows[0]["evt"], "ui.click")
        self.assertEqual(rows[0]["trace"], "t-1234abcd")
        self.assertEqual(rows[0]["data"]["target"], "button#wrTopicOk")

    def test_unlisted_event_is_rejected(self):
        r = self.client.post("/api/log", json={"evt": "evil.event", "data": {}})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])
        self.assertEqual(recent(10, node="ui"), [])

    def test_trace_header_is_honoured_and_echoed(self):
        r = self.client.get("/api/health", headers={"X-Trace-Id": "t-deadbeef"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("X-Trace-Id"), "t-deadbeef")
        res = [e for e in recent(20, evt="http.res")
               if e["data"]["path"] == "/api/health"]
        self.assertEqual(res[-1]["trace"], "t-deadbeef")

    def test_bad_trace_header_is_replaced(self):
        r = self.client.get("/api/health", headers={"X-Trace-Id": "../etc"})
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.headers.get("X-Trace-Id"), "../etc")
        self.assertTrue(str(r.headers.get("X-Trace-Id")).startswith("t-"))


if __name__ == "__main__":
    unittest.main()
