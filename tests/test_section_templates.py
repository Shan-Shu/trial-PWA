"""部分模板与工作规划测试：两套模板（文献综述 / 实验设计）的关键行为。

本文件守护这次需求变更的三条核心语义：

1. **系统自拟**：用户只给主题、不给任何指令与字段时，系统也能排出"每个部分写什么"，
   且各部分的必考维度**互不相同**（方法/参数重点考量化条件，引言/背景重点考可引文献）；
2. **判定按模板**：同一个库，因部分的 required_dimensions 不同而给出不同结论——
   缺量化条件时"参数与条件"判不足，而"研究目标"仍可判充足；
3. **双模并行**：用户填的字段优先于模板预设与规划，且来源被如实标注。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from research_agent.config import Settings
from research_agent.db import connect, utcnow
from research_agent.ontology.store import init_ontology
from research_agent.writing import section_plan as splan
from research_agent.writing import section_template as stpl
from research_agent.writing.section_compose import (
    compose_section, render_gap_notice, with_gap_notice)
from research_agent.writing.section_service import (
    build_project_plan, run_section_workflow)
from research_agent.writing.section_graph import build_section_graph
from research_agent.writing.service import create_project, list_sections
from tests.test_section_flow import _FakeModel, _services


class TemplateBase(unittest.TestCase):
    """临时库：4 篇同主题文献（有摘要、无量化超边）+ 1 条无条件的超边。

    刻意**不**提供量化条件与可比证据——用来验证"参数与条件"这类部分会判不足。
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tpl-test-"))
        self.db = self.tmp / "t.db"
        conn = connect(self.db)
        init_ontology(conn)
        papers = [
            ("p:g1", "Gold-Catalysed Annulation of Ynamides",
             "gold catalysis ynamide annulation with high regioselectivity",
             "gold catalysis; ynamide"),
            ("p:g2", "Ligand Effects in Gold Catalysed Annulation",
             "ligand control of gold catalysed ynamide annulation",
             "gold catalysis; ligand"),
            ("p:g3", "Mechanistic Study of Ynamide Annulation",
             "keteniminium intermediate in gold catalysed annulation",
             "gold catalysis; mechanism"),
            ("p:g4", "Scope of Gold Catalysed Annulation",
             "substrate scope of gold catalysed ynamide annulation",
             "gold catalysis; scope"),
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
        # 一条**没有条件/测量**的超边：量化条件维度必然为 0
        conn.execute(
            "INSERT INTO ontology_hyperedges(hyperedge_type, paper_key, label, "
            "created_at) VALUES(?,?,?,?)",
            ("reaction", "p:g1", "gold catalysed annulation", utcnow()))
        conn.commit()
        conn.close()
        self.settings = Settings(db_path=self.db)
        self.settings.section_sufficiency_min_papers = 2
        self.settings.section_sufficiency_min_evidence = 1
        self.settings.section_sufficiency_min_comparison = 1

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _conn(self):
        return connect(self.db)

    def _project(self, genre: str) -> int:
        conn = self._conn()
        try:
            return create_project(conn, f"{genre} 项目",
                                  "gold catalysis ynamide annulation",
                                  genre=genre)
        finally:
            conn.close()

    def _sections(self, project_id: int) -> list[dict]:
        conn = self._conn()
        try:
            return list_sections(conn, project_id)
        finally:
            conn.close()


class TestTemplateData(TemplateBase):
    """模板数据本身：两套模板齐全、字段声明成对。"""

    def test_two_genres_have_templates(self):
        for genre, expected in (("frontier_review", "review_"),
                                ("experiment_protocol", "proto_")):
            templates = stpl.list_templates(genre)
            self.assertTrue(templates, f"{genre} 没有模板")
            for item in templates:
                self.assertTrue(str(item["template"]).startswith(expected),
                                f"{genre} 的模板命名不符: {item['template']}")

    def test_every_generating_section_has_a_template(self):
        """除了"参考文献""摘要"这类不产出正文的部分，其余都应挂模板。"""
        # 不产出正文的部分（模板显式为 None，与前端的 generates_body 一致）
        non_body = {"references", "abstract"}
        for genre in ("frontier_review", "experiment_protocol"):
            key, spec = stpl.genre_definition(genre)
            for section in spec.get("sections") or []:
                section_key = str(section["key"])
                name, _tpl = stpl.template_for_section(genre, section_key)
                if section_key in non_body:
                    self.assertFalse(name, f"{section_key} 不应挂模板")
                else:
                    self.assertTrue(name, f"{genre}.{section_key} 未挂模板")

    def test_template_declares_role_focus_and_dimensions(self):
        for genre in ("frontier_review", "experiment_protocol"):
            for item in stpl.list_templates(genre):
                name = item["template"]
                self.assertTrue(str(item.get("role") or "").strip(),
                                f"{name} 缺 role")
                self.assertTrue(item.get("focus"), f"{name} 缺 focus")
                self.assertTrue(item.get("required_dimensions"),
                                f"{name} 缺 required_dimensions")
                self.assertTrue(item.get("evidence_types"),
                                f"{name} 缺 evidence_types")

    def test_dimension_weights_zero_out_unrequired(self):
        """未被 required 的维度权重必须为 0——否则无关闸门会挡住这一部分。"""
        spec = stpl.dimension_spec("experiment_protocol", "parameters")
        self.assertIn("conditions", spec.required)
        for key, weight in spec.weights.items():
            if key not in spec.required:
                self.assertEqual(weight, 0.0, f"{key} 不该有权重")
        self.assertAlmostEqual(sum(spec.weights.values()), 1.0, places=5)

    def test_protocol_parameters_require_conditions(self):
        """实验设计的参数节重点考量化条件（这是"按部分特征判定"的体现）。"""
        self.assertIn("conditions",
                      stpl.dimension_spec("experiment_protocol",
                                          "parameters").required)
        self.assertNotIn("conditions",
                         stpl.dimension_spec("experiment_protocol",
                                             "objective").required)

    def test_review_scope_requires_papers_and_comparison(self):
        required = stpl.dimension_spec("frontier_review", "scope").required
        self.assertIn("papers", required)
        self.assertIn("comparison", required)
        self.assertNotIn("conditions", required)


class TestWorkPlan(TemplateBase):
    """工作规划：只凭主题自拟，各部分要求互不相同。"""

    def test_auto_mode_generates_plan_without_any_instruction(self):
        project_id = self._project("experiment_protocol")
        conn = self._conn()
        try:
            plan = build_project_plan(
                conn, project_id=project_id,
                topic="gold catalysis ynamide annulation",
                instruction="", planner_model=None, persist=True)
        finally:
            conn.close()
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["mode"], "auto")
        self.assertGreaterEqual(plan["section_count"], 7)
        self.assertTrue(any(s["generates_body"] for s in plan["sections"]))

    def test_user_instruction_switches_mode(self):
        project_id = self._project("frontier_review")
        conn = self._conn()
        try:
            plan = build_project_plan(
                conn, project_id=project_id, topic="gold catalysis",
                instruction="只关注区域选择性", planner_model=None)
        finally:
            conn.close()
        self.assertEqual(plan["mode"], "user")
        self.assertIn("只关注区域选择性", plan["instruction"])

    def test_sections_get_different_required_dimensions(self):
        """**验收标准 1**：各部分的必考维度必须不同，而不是全篇一套。"""
        project_id = self._project("experiment_protocol")
        conn = self._conn()
        try:
            plan = build_project_plan(
                conn, project_id=project_id,
                topic="gold catalysis ynamide annulation",
                planner_model=None, persist=False)
        finally:
            conn.close()
        by_key = {s["section_key"]: s for s in plan["sections"]}
        # 参数节要量化条件；目标节不要
        self.assertIn("conditions", by_key["parameters"]["required_dimensions"])
        self.assertNotIn("conditions", by_key["objective"]["required_dimensions"])
        # 分组节要可比证据
        self.assertIn("comparison", by_key["groups"]["required_dimensions"])
        # 至少存在三种不同的维度组合
        combos = {tuple(sorted(s["required_dimensions"]))
                  for s in plan["sections"]}
        self.assertGreaterEqual(len(combos), 3,
                                f"维度组合过于单一: {combos}")

    def test_plan_carries_fields_with_presets(self):
        project_id = self._project("experiment_protocol")
        conn = self._conn()
        try:
            plan = build_project_plan(
                conn, project_id=project_id,
                topic="gold catalysis ynamide annulation",
                planner_model=None, persist=False)
        finally:
            conn.close()
        by_key = {s["section_key"]: s for s in plan["sections"]}
        params = [f["key"] for f in by_key["parameters"]["fields"]]
        self.assertIn("params", params)
        # 预设值来自模板：重复次数默认 3
        design_fields = {f["key"]: f for f in by_key["design"]["fields"]}
        self.assertEqual(design_fields["replicates"]["preset"], 3)

    def test_plan_marks_focus_source(self):
        project_id = self._project("experiment_protocol")
        conn = self._conn()
        try:
            plan = build_project_plan(
                conn, project_id=project_id,
                topic="gold catalysis ynamide annulation",
                planner_model=None, persist=False)
        finally:
            conn.close()
        for section in plan["sections"]:
            self.assertIn(section["focus_source"], ("plan", "template", "user"))


class TestFieldResolution(TemplateBase):
    """双模并行（纯并行）：用户 > 规划 > 模板预设。"""

    def test_user_value_wins_over_template_preset(self):
        fields = stpl.resolve_fields(
            "experiment_protocol", "design",
            user_values={"replicates": 5})
        self.assertEqual(fields["replicates"]["value"], 5)
        self.assertEqual(fields["replicates"]["source"], stpl.SOURCE_USER)

    def test_template_preset_used_when_nothing_else(self):
        fields = stpl.resolve_fields("experiment_protocol", "design")
        self.assertEqual(fields["replicates"]["value"], 3)
        self.assertEqual(fields["replicates"]["source"], stpl.SOURCE_TEMPLATE)

    def test_plan_value_used_when_user_silent(self):
        fields = stpl.resolve_fields(
            "experiment_protocol", "design",
            plan_values={"replicates": 7})
        self.assertEqual(fields["replicates"]["value"], 7)
        self.assertEqual(fields["replicates"]["source"], stpl.SOURCE_PLAN)

    def test_user_beats_plan(self):
        """纯并行：用户填过的字段，规划不得覆盖。"""
        fields = stpl.resolve_fields(
            "experiment_protocol", "design",
            user_values={"replicates": 4}, plan_values={"replicates": 9})
        self.assertEqual(fields["replicates"]["value"], 4)
        self.assertEqual(fields["replicates"]["source"], stpl.SOURCE_USER)

    def test_extra_user_field_is_preserved(self):
        """用户填了模板没声明的字段，不能被静默丢弃。"""
        fields = stpl.resolve_fields(
            "frontier_review", "scope",
            user_values={"custom_note": "只看 2020 后的工作"})
        self.assertIn("custom_note", fields)
        self.assertEqual(fields["custom_note"]["source"], stpl.SOURCE_USER)

    def test_field_block_marks_sources(self):
        fields = stpl.resolve_fields("experiment_protocol", "parameters",
                                     user_values={"params": "无水无氧"})
        block = stpl.field_block("experiment_protocol", "parameters", fields)
        self.assertIn("用户指定", block)
        self.assertIn("无水无氧", block)

    def test_summary_groups_by_source(self):
        fields = stpl.resolve_fields("experiment_protocol", "design",
                                     user_values={"replicates": 5})
        summary = stpl.field_source_summary(fields)
        self.assertIn("replicates", summary[stpl.SOURCE_USER])
        self.assertIn("variables", summary[stpl.SOURCE_TEMPLATE])


class TestTemplateJudgement(TemplateBase):
    """判定按模板走：同一个库，不同部分给出不同结论。"""

    def _verdict(self, project_id: int, section_key: str) -> dict:
        from research_agent.writing.sufficiency import evaluate_sufficiency
        conn = self._conn()
        try:
            project = conn.execute(
                "SELECT genre FROM writing_projects WHERE project_id=?",
                (project_id,)).fetchone()
            genre = str(project["genre"])
            name, tpl = stpl.template_for_section(genre, section_key)
            return evaluate_sufficiency(
                conn,
                plan={"retrieval_plan": {"query_variants": ["gold catalysis"]},
                      "mission": {"seed_terms": ["gold catalysis"]}},
                instruction="", heading=section_key,
                settings=self.settings, section_key=section_key, genre=genre,
                focus_terms=list(tpl.get("focus") or []),
            )
        finally:
            conn.close()

    def test_parameters_insufficient_without_quantitative_conditions(self):
        """**验收标准 2**：缺量化条件 → 参数节判不足，且指出缺的是 conditions。"""
        project_id = self._project("experiment_protocol")
        verdict = self._verdict(project_id, "parameters")
        self.assertIn(verdict["decision"], ("insufficient", "exhausted"))
        self.assertIn("conditions", verdict["unmet_dimensions"])
        self.assertIn("conditions", verdict["required_dimensions"])

    def test_objective_sufficient_with_only_citable_papers(self):
        """同一库下，目标节只要求可引文献与一般机制 → 可判充足。"""
        project_id = self._project("experiment_protocol")
        verdict = self._verdict(project_id, "objective")
        self.assertNotIn("conditions", verdict.get("unmet_dimensions") or [])
        self.assertEqual(verdict["decision"], "sufficient",
                         f"gates={verdict.get('gates')}")

    def test_scope_and_parameters_differ_on_same_library(self):
        """同一个库、同一时刻：两部分结论不同，证明判定确实按部分特征走。"""
        protocol = self._project("experiment_protocol")
        self.assertEqual(self._verdict(protocol, "objective")["decision"],
                         "sufficient")
        self.assertIn(self._verdict(protocol, "parameters")["decision"],
                      ("insufficient", "exhausted"))

    def test_unrequired_dimension_has_no_gate_effect(self):
        """没被 required 的维度即使不达标，也不该被记为必考未达标、不参与封顶。

        注意：该维度的闸门**可以** passed=False（如实记录库的现状），
        关键在 required=False 且不出现在 unmet_dimensions 里。
        """
        project_id = self._project("experiment_protocol")
        verdict = self._verdict(project_id, "objective")
        required = set(verdict["required_dimensions"])
        for gate in verdict["gates"]:
            if gate["gate"] in required:
                self.assertTrue(gate.get("required", True))
            else:
                self.assertFalse(gate.get("required", True),
                                 f"{gate['gate']} 不该是必考")
        for name in verdict["unmet_dimensions"]:
            self.assertIn(name, required,
                          f"{name} 不是必考维度，不该计入未达标")
        self.assertEqual(verdict["decision"], "sufficient")

    def test_conditions_not_required_does_not_block_objective(self):
        """实验设计的"研究目标"不考量量化条件：库里没有量化条件也应判充足。"""
        project_id = self._project("experiment_protocol")
        verdict = self._verdict(project_id, "objective")
        self.assertNotIn("conditions", verdict["required_dimensions"])
        self.assertNotIn("conditions", verdict["unmet_dimensions"])
        self.assertEqual(verdict["decision"], "sufficient",
                         f"failed={verdict.get('failed_gates')}")

    def test_legacy_gates_still_apply_when_no_template(self):
        """未配模板的部分沿用旧闸门，避免"换体裁就悄悄放宽判定"。"""
        project_id = self._project("research_article")
        verdict = self._verdict(project_id, "methods")
        self.assertFalse(verdict.get("allow_gaps"))
        # 未配模板时，既有闸门仍标记为必考
        legacy = [g for g in verdict["gates"]
                  if g["gate"] in ("papers", "evidence", "conditions")]
        self.assertTrue(legacy)
        self.assertTrue(all(g.get("required", True) for g in legacy))

    def test_allow_gaps_true_for_templated_sections(self):
        project_id = self._project("experiment_protocol")
        self.assertTrue(self._verdict(project_id, "parameters")["allow_gaps"])

    def test_untemplated_section_disallows_gaps(self):
        """没挂模板的部分（如 research_article）退回旧约定：不足就不写。"""
        project_id = self._project("research_article")
        verdict = self._verdict(project_id, "methods")
        self.assertFalse(verdict["allow_gaps"])


class TestGapNotice(TemplateBase):
    """带缺口写作：可以写，但必须显式标注。"""

    def test_notice_lists_missing_dimensions_in_plain_words(self):
        notice = render_gap_notice(["conditions", "comparison"],
                                   ["quantitative", "comparative"])
        self.assertIn("本节证据缺口", notice)
        self.assertIn("缺少可量化条件", notice)
        self.assertIn("缺少可两两比较的同类证据", notice)
        self.assertIn("量化条件/数值", notice)

    def test_notice_inserted_at_top_and_idempotent(self):
        body = "## 参数与条件\n\n温度设定为 60 °C。"
        once = with_gap_notice(body, ["conditions"])
        self.assertTrue(once.startswith("> **本节证据缺口"))
        self.assertIn("温度设定为 60 °C", once)
        twice = with_gap_notice(once, ["conditions"])
        self.assertEqual(once, twice, "重复标注应幂等")

    def test_compose_adds_notice_when_gaps_present(self):
        result = compose_section(
            heading="参数与条件", fields_block="用户指定：无水无氧",
            materials=[{"paper_key": "p:g1", "title": "T", "evidence": []}],
            model=_FakeModel("温度 60 °C。[1]"),
            unmet_dimensions=["conditions"],
            evidence_types=["quantitative"])
        self.assertTrue(result["content"].startswith("> **本节证据缺口"))
        self.assertEqual(result["unmet_dimensions"], ["conditions"])
        self.assertIn("推断", result["gap_notice"])

    def test_compose_without_gaps_has_no_notice(self):
        result = compose_section(
            heading="研究目标", materials=[], model=_FakeModel("目标：验证配体效应。"))
        self.assertNotIn("本节证据缺口", result["content"])
        self.assertEqual(result["gap_notice"], "")


class TestTemplateWorkflow(TemplateBase):
    """端到端：带缺口写作决策 + 逐轮留痕里的模板信息。"""

    def test_writes_with_gaps_when_template_allows(self):
        project_id = self._project("experiment_protocol")
        conn = self._conn()
        try:
            key = next(s["section_key"] for s in list_sections(conn, project_id)
                       if s["section_key"] == "parameters")
        finally:
            conn.close()
        strict = Settings(db_path=self.db)
        strict.section_sufficiency_min_papers = 2
        strict.section_sufficiency_min_evidence = 1
        strict.section_max_collection_rounds = 1
        result = run_section_workflow(
            project_id=project_id, section_key=key, instruction="",
            user_fields={"params": "无水无氧，-20 °C"},
            db_path=str(self.db), settings=strict,
            services=_services(strict, lambda r: {"count": 0,
                                                  "paper_keys": [],
                                                  "errors": []}),
            compose_model=_FakeModel("温度 60 °C。[1]"))
        self.assertEqual(result["status"], "written_with_gaps",
                         f"decision={result.get('decision')} "
                         f"unmet={result.get('unmet_dimensions')}")
        self.assertIn("conditions", result["unmet_dimensions"])
        self.assertEqual(result["template_key"], "proto_parameters")
        self.assertEqual(
            result["field_sources"][stpl.SOURCE_USER], ["params"])

        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT content, grounded_on FROM writing_sections "
                "WHERE project_id=? AND section_key=?",
                (project_id, key)).fetchone()
        finally:
            conn.close()
        self.assertIn("本节证据缺口", row["content"])
        import json
        grounded = json.loads(row["grounded_on"])
        self.assertEqual(grounded["status"], "written_with_gaps")
        self.assertEqual(grounded["template_key"], "proto_parameters")
        self.assertIn("conditions", grounded["unmet_dimensions"])

    def test_trace_records_template_and_sources(self):
        project_id = self._project("frontier_review")
        conn = self._conn()
        try:
            key = "scope"
        finally:
            conn.close()
        strict = Settings(db_path=self.db)
        strict.section_sufficiency_min_papers = 2
        strict.section_sufficiency_min_evidence = 1
        strict.section_max_collection_rounds = 1
        result = run_section_workflow(
            project_id=project_id, section_key=key, instruction="",
            user_fields={"year_from": 2020},
            db_path=str(self.db), settings=strict,
            services=_services(strict, lambda r: {"count": 0,
                                                  "paper_keys": [],
                                                  "errors": []}),
            compose_model=_FakeModel("范围说明。[1]"))
        from research_agent.writing.section_service import get_section_trace
        conn = self._conn()
        try:
            trace = get_section_trace(conn, project_id, key)
        finally:
            conn.close()
        self.assertTrue(trace["rounds"])
        done = [r for r in trace["rounds"] if r["stage"] == "done"]
        self.assertTrue(done)
        self.assertEqual(done[-1]["template_key"], "review_scope")
        self.assertEqual(done[-1]["field_source_json"] is not None, True)
        self.assertEqual(result["template_key"], "review_scope")


if __name__ == "__main__":
    unittest.main()
