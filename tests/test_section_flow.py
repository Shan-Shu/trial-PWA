"""节点写作链测试：充分性判定、决策轨迹、成段与引文绑定。

覆盖"在大纲节点输入指令"的完整语义：

- **判定**：够写 / 不够写 / 预算用尽，且**加权平均不得掩盖短板**（硬闸门）；
- **链路**：不足时先补检再复审，不硬写；补检无效果时给出缺口清单；
- **痕迹**：每轮判定落 ``section_runs``，正文落 ``writing_sections`` 并带
  ``grounded_on``（引文能回溯到 ``paper_key`` / ``hyperedge_id``）；
- **诚实**：无模型时是 ``skeleton_fallback``，越界引文必须被显式报告。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from research_agent.config import Settings
from research_agent.db import connect, utcnow
from research_agent.ontology.store import init_ontology
from research_agent.writing import section_service as sections
from research_agent.writing.section_compose import (
    bind_citations, build_materials, compose_section, parse_citation_indices,
    render_material_digest)
from research_agent.writing.section_graph import (
    build_section_request, normalize_fallback_plan)
from research_agent.writing.sufficiency import (
    cited_count_hint, evaluate_sufficiency, tokenize)
from research_agent.writing.service import create_project, get_project


class _FakeModel:
    """最小可用的假模型：返回固定文本，用于验证"模型路径"而不联网。"""

    def __init__(self, text: str):
        self.text = text
        self.prompts: list[str] = []

    def invoke(self, messages):  # noqa: ANN001
        self.prompts.append(str(getattr(messages[-1], "content", messages[-1])))
        return type("Msg", (), {"content": self.text})()


class SectionBase(unittest.TestCase):
    """临时库 + 两篇金催化文献 + 一条带条件/测量的超边。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="section-test-"))
        self.db = self.tmp / "t.db"
        conn = connect(self.db)
        init_ontology(conn)  # 超边/条件/测量来自本体层 schema，不在核心 schema 里
        papers = [
            ("p:gold-1", "Gold-Catalysed Intermolecular Annulation of Ynamides",
             "A gold(I) complex catalyses ynamide annulation with high "
             "regioselectivity.", "gold catalysis; ynamide; regioselectivity"),
            ("p:gold-2", "Regioselectivity Control in Gold Catalysed Annulation",
             "Ligand effects on the regioselectivity of gold catalysed "
             "annulation of ynamides.", "gold catalysis; annulation; ligand"),
        ]
        for key, title, abstract, keywords in papers:
            conn.execute(
                "INSERT INTO papers(paper_key, title, abstract, keywords, venue, "
                "pub_year, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (key, title, abstract, keywords, "J. Test", 2024, "ingested",
                 utcnow()))
            conn.execute(
                "INSERT INTO quality_results(paper_key, decision, quality, "
                "assessed_at) VALUES(?,?,?,?)", (key, "direct", 0.9, utcnow()))
            conn.execute(
                "INSERT INTO processing_log(paper_key, node, event, details, ts) "
                "VALUES(?,?,?,?,?)", (key, "knowledge", "extracted", "{}",
                                      utcnow()))
        conn.execute(
            "INSERT INTO ontology_hyperedges(hyperedge_type, paper_key, label, "
            "created_at) VALUES(?,?,?,?)",
            ("reaction", "p:gold-1", "gold catalysed annulation", utcnow()))
        conn.execute(
            "INSERT INTO ontology_hyperedge_conditions(hyperedge_id, condition_key, "
            "value_text) VALUES(1, 'temperature', '60 C')")
        conn.execute(
            "INSERT INTO ontology_hyperedge_measurements(hyperedge_id, metric, "
            "value_text) VALUES(1, 'yield', '92%')")
        conn.execute(
            "INSERT INTO ontology_hyperedges(hyperedge_type, paper_key, label, "
            "created_at) VALUES(?,?,?,?)",
            ("reaction", "p:gold-2", "ligand controlled regioselectivity",
             utcnow()))
        conn.execute(
            "INSERT INTO ontology_hyperedge_conditions(hyperedge_id, condition_key, "
            "value_text) VALUES(2, 'ligand', 'P(tBu)3')")
        conn.commit()
        conn.close()
        self.settings = Settings(db_path=self.db)
        self.settings.section_sufficiency_min_papers = 2
        self.settings.section_sufficiency_min_evidence = 1

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _conn(self):
        return connect(self.db)

    def _plan(self):
        return {
            "goal": "gold catalysis ynamide annulation",
            "domain": "gold catalysis",
            "planner_mode": "offline_fallback",
            "retrieval_plan": {
                "query_variants": ["gold catalysis", "ynamide annulation"],
                "must_cover": ["regioselectivity"],
            },
            "mission": {"seed_terms": ["gold catalysis", "ynamide"]},
        }


class TestTokenizer(SectionBase):
    def test_instruction_markers_stripped_from_requirements(self):
        """'只写金催化部分' 必须切成要求词，而不是被整条丢弃。"""
        tokens = tokenize("只写金催化部分，引用不少于3条，强调区域选择性")
        self.assertIn("金催化部分", tokens)
        self.assertIn("区域选择性", tokens)
        self.assertFalse(any("只写" in t for t in tokens))

    def test_boilerplate_not_treated_as_requirement(self):
        """脚手架措辞（'为学术论文的'）不得进入要求集。"""
        tokens = tokenize("为学术论文的「摘要」一节做检索规划：金催化炔酰胺环化")
        self.assertNotIn("为学术论文的", tokens)
        self.assertNotIn("一节做检索规划", tokens)
        self.assertIn("金催化炔酰胺环化", tokens)

    def test_english_stopwords_dropped(self):
        tokens = tokenize("summarise the gold catalysed annulation with "
                          "regioselectivity")
        self.assertEqual(tokens, ["gold", "catalysed", "annulation",
                                  "regioselectivity"])

    def test_cited_count_hint_variants(self):
        self.assertEqual(cited_count_hint("引用不少于3条"), 3)
        self.assertEqual(cited_count_hint("至少 5 篇"), 5)
        self.assertEqual(cited_count_hint("cite at least 2 sources"), 2)
        self.assertEqual(cited_count_hint("写一段综述"), 0)


class TestSufficiency(SectionBase):
    def test_sufficient_when_all_gates_pass(self):
        conn = self._conn()
        try:
            v = evaluate_sufficiency(
                conn, plan=self._plan(),
                instruction="summarise gold catalysed annulation of ynamides",
                heading="引言", settings=self.settings)
        finally:
            conn.close()
        self.assertEqual(v["decision"], "sufficient")
        self.assertEqual(v["failed_gates"], [])
        self.assertGreaterEqual(v["counts"]["matched_papers"], 2)

    def test_shortfall_cannot_be_hidden_by_weighted_average(self):
        """实测回归：命中 1 篇（阈值 6）曾因其余维度满分被判 sufficient。

        注意：阈值会被"库内已入库总数"自适应下调，因此这里用**较小的库**
        （2 篇）配一个仍高于它的下限（3 篇），保证确实触发 papers 硬闸门。
        """
        conn = self._conn()
        try:
            strict = Settings(db_path=self.db)
            strict.section_sufficiency_min_papers = 3
            strict.section_adaptive_paper_floor = False
            v = evaluate_sufficiency(
                conn, plan=self._plan(),
                instruction="summarise gold catalysed annulation",
                heading="引言", settings=strict)
        finally:
            conn.close()
        self.assertIn(v["decision"], ("insufficient", "exhausted"))
        self.assertIn("papers", v["failed_gates"])
        self.assertTrue(any("硬指标未达标" in r for r in v["reasons"]))
        self.assertLess(v["confidence"], v["threshold"],
                        "硬闸门未达标时置信度必须被压到阈值以下")

    def test_exhausted_when_budget_used_up(self):
        conn = self._conn()
        try:
            strict = Settings(db_path=self.db)
            strict.section_sufficiency_min_papers = 50
            strict.section_adaptive_paper_floor = False
            v = evaluate_sufficiency(
                conn, plan=self._plan(), instruction="gold catalysis",
                heading="引言", settings=strict, rounds_done=2)
        finally:
            conn.close()
        self.assertEqual(v["decision"], "exhausted")
        self.assertTrue(any("预算" in r for r in v["reasons"]))

    def test_suggested_queries_never_contain_full_sentence(self):
        conn = self._conn()
        try:
            strict = Settings(db_path=self.db)
            strict.section_sufficiency_min_papers = 50
            strict.section_adaptive_paper_floor = False
            v = evaluate_sufficiency(
                conn, plan=self._plan(),
                instruction="只写金催化部分，引用不少于3条，强调区域选择性",
                heading="引言", settings=strict)
        finally:
            conn.close()
        for q in v["suggested_queries"]:
            self.assertLess(len(q), 40, f"补检词过长（疑似整句）: {q}")

    def test_generic_heading_excluded_from_requirements(self):
        """'方法''引言'这类标题不应成为永远无法覆盖的要求。"""
        conn = self._conn()
        try:
            v = evaluate_sufficiency(
                conn, plan={"retrieval_plan": {}, "mission": {}},
                instruction="gold catalysis", heading="方法",
                settings=self.settings)
        finally:
            conn.close()
        self.assertNotIn("方法", (v["counts"]["requirements_missing"] or []))

    def test_cite_hint_lowers_paper_floor(self):
        """用户只要 1 条引用时，不该仍按 6 篇判不足。"""
        conn = self._conn()
        try:
            strict = Settings(db_path=self.db)
            strict.section_sufficiency_min_papers = 6
            v = evaluate_sufficiency(
                conn, plan=self._plan(),
                instruction="summarise gold catalysis, cite at least 1 source",
                heading="引言", settings=strict)
        finally:
            conn.close()
        self.assertLessEqual(v["counts"]["matched_papers"], 6)
        self.assertNotIn("papers", v["failed_gates"])

    def test_llm_gap_downgrades_sufficient(self):
        conn = self._conn()
        try:
            v = evaluate_sufficiency(
                conn, plan=self._plan(),
                instruction="summarise gold catalysed annulation of ynamides",
                heading="引言", settings=self.settings,
                llm_analysis={"retrieval_requests": [{"query_terms": ["x"]}]})
        finally:
            conn.close()
        self.assertIn(v["decision"], ("insufficient", "exhausted"))
        self.assertTrue(v["llm_verdict"])


class TestCompose(SectionBase):
    def test_material_numbering_is_stable_and_bound(self):
        conn = self._conn()
        try:
            mats = build_materials(conn, ["p:gold-2", "p:gold-1"],
                                   ["H-0001", "H-0002"], limit=10)
        finally:
            conn.close()
        self.assertEqual([m["paper_key"] for m in mats], ["p:gold-2", "p:gold-1"])
        digest = render_material_digest(mats)
        self.assertIn("[1] Regioselectivity Control", digest)

    def test_evidence_bound_to_owning_paper(self):
        """H-0001 属于 p:gold-1，不能挂到 p:gold-2 名下。"""
        conn = self._conn()
        try:
            mats = build_materials(conn, ["p:gold-1", "p:gold-2"],
                                   ["H-0001", "H-0002"], limit=10)
        finally:
            conn.close()
        first, second = mats[0], mats[1]
        self.assertEqual([e["hyperedge_id"] for e in first["evidence"]], [1])
        self.assertEqual([e["hyperedge_id"] for e in second["evidence"]], [2])

    def test_parse_citation_ranges(self):
        self.assertEqual(parse_citation_indices("见 [1] 与 [2,3] 及 [5-7]"),
                         [1, 2, 3, 5, 6, 7])

    def test_out_of_range_citation_reported(self):
        result = bind_citations("正文 [1] 与编造的 [9]", [{"paper_key": "p:gold-1",
                                                     "title": "X",
                                                     "evidence": []}])
        self.assertEqual(result["invalid_indices"], [9])
        self.assertEqual(result["citation_ids"], ["p:gold-1"])

    def test_no_model_yields_labeled_skeleton(self):
        result = compose_section(
            heading="引言", instruction="gold catalysis",
            materials=[{"paper_key": "p:gold-1", "title": "T", "evidence": []}],
            model=None, model_reason="缺少 API Key")
        self.assertEqual(result["generated_by"], "skeleton_fallback")
        self.assertIn("缺少 API Key", result["content"])
        self.assertIn("骨架草稿", result["content"])

    def test_model_output_is_used_and_cited(self):
        model = _FakeModel("金催化炔酰胺环化具有区域选择性 [1]。")
        result = compose_section(
            heading="引言", instruction="gold catalysis",
            materials=[{"paper_key": "p:gold-1", "title": "T", "evidence": []}],
            model=model)
        self.assertEqual(result["generated_by"], "llm")
        self.assertEqual(result["citations"], ["p:gold-1"])
        self.assertEqual(result["invalid_indices"], [])

    def test_prompt_carries_numbered_materials_and_instruction(self):
        """提示词必须把"带编号素材"和"节点指令"都交给模型，否则引用无从谈起。"""
        model = _FakeModel("正文 [1]。")
        compose_section(
            heading="引言", instruction="只写金催化部分",
            materials=[{"paper_key": "p:gold-1",
                        "title": "Gold Catalysed Annulation", "evidence": []}],
            model=model)
        prompt = model.prompts[0]
        self.assertIn("[1] Gold Catalysed Annulation", prompt)
        self.assertIn("只写金催化部分", prompt)
        self.assertIn("引言", prompt)
        # 编号纪律必须写进提示词（不能只在代码里校验越界）
        self.assertTrue(
            "只能引用下列编号" in prompt or "编号必须来自" in prompt
            or "不得引用清单外" in prompt or "严禁引用清单外" in prompt,
            prompt[:800])

    def test_model_with_invented_citation_is_flagged(self):
        model = _FakeModel("伪造引用 [7]。")
        result = compose_section(
            heading="引言", instruction="gold catalysis",
            materials=[{"paper_key": "p:gold-1", "title": "T", "evidence": []}],
            model=model)
        self.assertEqual(result["invalid_indices"], [7])


class TestPlanningHelpers(SectionBase):
    def test_section_request_is_compact(self):
        req = build_section_request({"topic": "gold catalysis", "title": ""},
                                    "引言", "highlight regioselectivity")
        self.assertLess(len(req), 60)
        self.assertIn("gold catalysis", req)

    def test_fallback_plan_terms_are_split(self):
        """离线兜底曾把整句当检索词 → 永远 0 命中。"""
        plan = {
            "planner_mode": "offline_fallback",
            "goal": "gold catalysis ynamide annulation：引言小节",
            "mission": {"seed_terms": ["gold catalysis ynamide annulation：引言小节"]},
            "retrieval_plan": {"query_variants": [
                "gold catalysis ynamide annulation：引言小节"]},
        }
        out = normalize_fallback_plan(
            plan, "gold catalysis ynamide annulation：引言小节")
        variants = (out.get("retrieval_plan") or {}).get("query_variants") or []
        self.assertGreater(len(variants), 1)
        self.assertTrue(all(len(v) < 40 for v in variants))

    def test_llm_plan_is_left_untouched(self):
        plan = {"planner_mode": "llm",
                "retrieval_plan": {"query_variants": ["gold catalysis"]}}
        out = normalize_fallback_plan(plan, "whatever")
        self.assertEqual(out, plan)


class TestWorkflow(SectionBase):
    def _project(self) -> int:
        conn = self._conn()
        try:
            return create_project(conn, "测试项目", "gold catalysis ynamide",
                                  genre=None)
        finally:
            conn.close()

    def _project_section(self, project_id: int) -> str:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT section_key FROM writing_sections WHERE project_id=? "
                "ORDER BY section_id LIMIT 1", (project_id,)).fetchone()
            return str(row["section_key"])
        finally:
            conn.close()

    def test_insufficient_triggers_collection_then_writes(self):
        """补检前不足 → 调用 collector → 复审变够 → 成段落库（逐轮留痕）。"""
        project_id = self._project()
        key = self._project_section(project_id)
        calls: list[dict] = []

        def collector(request):
            calls.append(request)
            # 补检"没有新增文献"：命中数不变，用于验证链路仍会复审（不无限循环）
            return {"count": 0, "paper_keys": [], "errors": []}

        strict = Settings(db_path=self.db)
        strict.section_sufficiency_min_papers = 3
        strict.section_adaptive_paper_floor = False
        strict.section_max_collection_rounds = 2

        result = sections.run_section_workflow(
            project_id=project_id, section_key=key,
            instruction="summarise gold catalysed annulation",
            db_path=str(self.db), settings=strict,
            services=_services(strict, collector),
            compose_model=_FakeModel("金催化 [1] 区域选择性 [2]。"),
        )
        self.assertEqual(result["status"], "needs_data")
        self.assertGreaterEqual(len(calls), 1, "不足时应实际发起补检")
        self.assertEqual(result["decision"], "exhausted")

        conn = self._conn()
        try:
            trace = sections.get_section_trace(conn, project_id, key)
        finally:
            conn.close()
        stages = [r["stage"] for r in trace["rounds"]]
        self.assertIn("planning", stages)
        self.assertIn("sufficiency", stages)
        self.assertIn("needs_data", stages)
        self.assertGreaterEqual(trace["round_count"], 2)
        section = trace["section"] or {}
        self.assertEqual(section["status"], "needs_data")
        self.assertEqual((section.get("content") or "").strip(), "")

    def test_sufficient_writes_content_and_grounding(self):
        project_id = self._project()
        key = self._project_section(project_id)
        result = sections.run_section_workflow(
            project_id=project_id, section_key=key,
            instruction="summarise gold catalysed annulation of ynamides",
            db_path=str(self.db), settings=self.settings,
            compose_model=_FakeModel("金催化 [1]，区域选择性 [2]。"),
        )
        self.assertEqual(result["status"], "written")
        self.assertEqual(result["decision"], "sufficient")
        self.assertEqual(result["invalid_indices"], [])

        conn = self._conn()
        try:
            trace = sections.get_section_trace(conn, project_id, key)
            states = sections.latest_section_state(conn, project_id)
        finally:
            conn.close()
        section = trace["section"] or {}
        self.assertIn("金催化", section["content"])
        grounded = section["grounded_on"]
        self.assertEqual(grounded["status"], "written")
        self.assertEqual(len(grounded["paper_keys"]), 2)
        self.assertTrue(grounded["bindings"])
        self.assertEqual(states[key]["status"], "written")
        # 正文与轨迹用同一个 run_id 关联
        self.assertEqual(section["last_run_id"], result["run_id"])
        self.assertEqual(trace["rounds"][-1]["run_id"], result["run_id"])

    def test_empty_instruction_runs_in_auto_mode(self):
        """空指令**不再报错**：走"系统自拟"模式（工作规划 + 模板默认）。

        这是需求变更的核心：用户只该给主题，不该被迫逐部分声明要写什么。
        """
        project_id = self._project()
        key = self._project_section(project_id)
        result = sections.run_section_workflow(
            project_id=project_id, section_key=key, instruction="",
            db_path=str(self.db), settings=self.settings,
            compose_model=_FakeModel("金催化 [1]。"))
        self.assertNotEqual(result["status"], "failed")
        self.assertIn(result["work_plan_mode"], ("auto", "user"))

    def test_unknown_section_rejected(self):
        project_id = self._project()
        with self.assertRaises(ValueError):
            sections.run_section_workflow(
                project_id=project_id, section_key="nope",
                instruction="gold", db_path=str(self.db),
                settings=self.settings)

    def test_cancel_before_run_marks_cancelled(self):
        project_id = self._project()
        key = self._project_section(project_id)
        event = threading.Event()
        event.set()
        result = sections.run_section_workflow(
            project_id=project_id, section_key=key,
            instruction="summarise gold catalysis",
            db_path=str(self.db), settings=self.settings,
            cancel_event=event)
        self.assertEqual(result["status"], "cancelled")

    def test_rounds_persist_per_judgement(self):
        """每一轮充分性判定都必须单独留痕（可审计"为什么补检"）。"""
        project_id = self._project()
        key = self._project_section(project_id)

        def collector(request):
            conn = connect(self.db)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO papers(paper_key, title, abstract, "
                    "keywords, status, created_at) VALUES(?,?,?,?,?,?)",
                    ("p:extra", "Extra Gold Catalysis Paper",
                     "gold catalysis annulation ynamide",
                     "gold catalysis", "ingested", utcnow()))
                conn.execute(
                    "INSERT OR REPLACE INTO quality_results(paper_key, decision, "
                    "quality, assessed_at) VALUES(?,?,?,?)",
                    ("p:extra", "direct", 0.9, utcnow()))
                conn.execute(
                    "INSERT INTO processing_log(paper_key, node, event, details, "
                    "ts) VALUES(?,?,?,?,?)",
                    ("p:extra", "knowledge", "extracted", "{}", utcnow()))
                conn.commit()
            finally:
                conn.close()
            return {"count": 1, "paper_keys": ["p:extra"], "errors": []}

        strict = Settings(db_path=self.db)
        strict.section_sufficiency_min_papers = 3
        strict.section_adaptive_paper_floor = False
        # 严格模式下自适应不再兜底，因此证据下限要显式给到库能提供的量，
        # 否则测的就不是"复审通过"而是"另一个闸门"。
        strict.section_sufficiency_min_evidence = 1
        strict.section_max_collection_rounds = 1
        result = sections.run_section_workflow(
            project_id=project_id, section_key=key,
            instruction="summarise gold catalysed annulation of ynamides",
            db_path=str(self.db), settings=strict,
            services=_services(strict, collector),
            compose_model=_FakeModel("补检后 [1]。"))
        self.assertEqual(result["status"], "written",
                         "补检补上第 3 篇后应复审通过并成段")
        conn = self._conn()
        try:
            rounds = conn.execute(
                "SELECT round, stage, decision FROM section_runs "
                "WHERE project_id=? AND section_key=? ORDER BY round",
                (project_id, key)).fetchall()
        finally:
            conn.close()
        self.assertGreaterEqual(len(rounds), 3, "规划 + 初判 + 复审应各有痕迹")
        self.assertEqual([r["round"] for r in rounds],
                         sorted({r["round"] for r in rounds}),
                         "每轮判定必须有独立行（不能互相覆盖）")
        decisions = [r["decision"] for r in rounds]
        self.assertIn("insufficient", decisions, "初判应记录为不足")
        self.assertIn("sufficient", decisions, "复审应记录为充足")
        stages = [r["stage"] for r in rounds]
        self.assertGreaterEqual(stages.count("sufficiency"), 1)


def _services(settings: Settings, collector):
    from research_agent.study.graph import StudyServices
    return StudyServices(settings=settings, collector=collector)


if __name__ == "__main__":
    unittest.main()
