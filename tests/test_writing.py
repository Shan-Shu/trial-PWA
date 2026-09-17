"""写作台与系统状态回归测试（合并新增）。

覆盖：体裁来自 pack、项目/章节落库与 UNIQUE 幂等、无模型时的显式降级标记、
引用编号入库、Markdown 导出、以及 app 路由装配。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from research_agent.db import connect, get_paper, upsert_paper  # noqa: E402
from research_agent.writing import service as writing  # noqa: E402
from tests._tmpdir import make_temp_dir  # noqa: E402


def _paper(key: str, **over) -> dict:
    rec = {
        "paper_key": key,
        "source": "europepmc",
        "title": "Gold-catalysed annulation of ynamides",
        "abstract": "A vinyl cation intermediate is captured.",
        "venue": "Nature Chemistry",
        "source_type": "journal",
        "pub_year": 2024,
        "status": "ingested",
    }
    rec.update(over)
    return rec


class WritingServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db_path = str(Path(self.tmp.name) / "writing.db")
        self.conn = connect(self.db_path)
        upsert_paper(self.conn, _paper("w:1"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_genres_come_from_pack(self):
        genres = writing.list_genres()
        keys = {g["key"] for g in genres}
        self.assertIn("research_article", keys)
        self.assertIn("frontier_review", keys)
        self.assertIn("grant_proposal", keys)
        default = [g for g in genres if g["is_default"]]
        self.assertEqual(len(default), 1)
        self.assertTrue(default[0]["sections"])

    def test_headings_come_from_pack_not_code(self):
        """换一个 pack 内容，章节标题必须随之改变 —— 这是"不硬编码"的证明。"""
        import json
        from research_agent import packs

        tmp_pack = Path(self.tmp.name) / "custom_packs"
        target = tmp_pack / "skills" / "writing"
        (target / "content").mkdir(parents=True, exist_ok=True)
        (target / "content" / "data.json").write_text(json.dumps({
            "default_genre": "probe",
            "genres": {"probe": {
                "label": "探针体裁",
                "sections": [{"key": "probe_section", "heading": "来源包定义的章节"}],
            }},
            "prompts": {"section_user": "{heading} / {materials}"},
        }, ensure_ascii=False), encoding="utf-8")

        saved = os.environ.get("RA_PACKS_DIR")
        os.environ["RA_PACKS_DIR"] = str(tmp_pack)
        os.environ["RA_PACKS_FALLBACK"] = ""
        packs.reset_cache()
        try:
            genres = writing.list_genres()
            self.assertEqual([g["key"] for g in genres], ["probe"])
            project_id = writing.create_project(self.conn, "探针项目")
            sections = writing.list_sections(self.conn, project_id)
            self.assertEqual([s["heading"] for s in sections], ["来源包定义的章节"])
        finally:
            if saved is None:
                os.environ.pop("RA_PACKS_DIR", None)
            else:
                os.environ["RA_PACKS_DIR"] = saved
            os.environ.pop("RA_PACKS_FALLBACK", None)
            packs.reset_cache()

    def test_code_has_no_default_outline_constant(self):
        """代码里不得再出现整段骨架常量（只允许注释里提到历史名称）。"""
        source = Path(writing.__file__).read_text(encoding="utf-8")
        self.assertNotIn("DEFAULT_OUTLINE =", source)
        self.assertNotIn('"abstract", "heading": "摘要"', source)

    def test_create_project_seeds_sections_from_genre(self):
        project_id = writing.create_project(self.conn, "测试项目", "炔酰胺环化",
                                            "research_article")
        sections = writing.list_sections(self.conn, project_id)
        self.assertTrue(sections)
        headings = [s["heading"] for s in sections]
        self.assertIn("摘要", headings)
        project = writing.get_project(self.conn, project_id)
        self.assertEqual(project["genre"], "research_article")
        self.assertEqual(len(project["outline"]), len(sections))

    def test_create_project_requires_title(self):
        with self.assertRaises(ValueError):
            writing.create_project(self.conn, "   ")

    def test_create_project_unknown_genre_falls_back(self):
        project_id = writing.create_project(self.conn, "回退", "", "no-such-genre")
        project = writing.get_project(self.conn, project_id)
        self.assertIn(project["genre"], {g["key"] for g in writing.list_genres()})

    def test_outline_without_model_is_marked(self):
        project_id = writing.create_project(self.conn, "P", "topic")
        result = writing.generate_outline(self.conn, project_id, topic="topic",
                                          model=None)
        self.assertEqual(result["generated_by"], "pack_skeleton")
        self.assertIsNone(result["model_error"])
        self.assertTrue(result["sections"])

    def test_section_without_model_is_marked_and_cites_materials(self):
        project_id = writing.create_project(self.conn, "P", "topic")
        result = writing.generate_section(
            self.conn, project_id, "abstract", "摘要", topic="topic",
            model=None, db_path=self.db_path,
        )
        self.assertEqual(result["generated_by"], "skeleton_fallback")
        self.assertIn("骨架草稿", result["content"])
        self.assertGreaterEqual(result["material_count"], 1)
        # 素材编号必须进入 citation_ids
        section = [s for s in writing.list_sections(self.conn, project_id)
                   if s["section_key"] == "abstract"][0]
        self.assertTrue(section["citation_ids"])
        self.assertEqual(section["citation_ids"], list(range(
            1, len(section["citation_ids"]) + 1)))

    def test_save_section_is_upsert(self):
        project_id = writing.create_project(self.conn, "P")
        first = writing.save_section(self.conn, project_id, "abstract", "摘要", "a")
        second = writing.save_section(self.conn, project_id, "abstract", "摘要", "b")
        self.assertEqual(first, second)
        sections = [s for s in writing.list_sections(self.conn, project_id)
                    if s["section_key"] == "abstract"]
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["content"], "b")

    def test_polish_without_model_returns_original(self):
        project_id = writing.create_project(self.conn, "P")
        writing.save_section(self.conn, project_id, "abstract", "摘要", "原文")
        result = writing.polish_section(self.conn, project_id, "abstract", "原文",
                                        model=None)
        self.assertFalse(result["polished"])
        self.assertEqual(result["content"], "原文")
        self.assertIn("未提供模型", result["reason"])

    def test_export_markdown_includes_all_sections(self):
        project_id = writing.create_project(self.conn, "导出测试", "topic")
        writing.save_section(self.conn, project_id, "abstract", "摘要", "正文内容")
        data = writing.export_project_markdown(self.conn, project_id)
        self.assertTrue(data["content"].startswith("# 导出测试"))
        self.assertIn("摘要", data["content"])
        self.assertIn("正文内容", data["content"])
        self.assertTrue(data["filename"].endswith(".md"))
        self.assertEqual(data["section_count"],
                         len(writing.list_sections(self.conn, project_id)))

    def test_delete_project_cascades_sections(self):
        project_id = writing.create_project(self.conn, "待删")
        writing.delete_project(self.conn, project_id)
        self.assertIsNone(writing.get_project(self.conn, project_id))
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM writing_sections WHERE project_id=?",
            (project_id,),
        ).fetchone()
        self.assertEqual(int(row["c"]), 0)

    def test_list_projects_counts_filled_sections(self):
        project_id = writing.create_project(self.conn, "统计")
        writing.save_section(self.conn, project_id, "abstract", "摘要", "x")
        projects = writing.list_projects(self.conn)
        target = [p for p in projects if p["project_id"] == project_id][0]
        self.assertGreaterEqual(target["filled_sections"], 1)
        self.assertGreaterEqual(target["total_sections"], 1)

    def test_prompt_templates_come_from_pack(self):
        prompts = writing.writing_pack().get("prompts") or {}
        for key in ("outline_user", "section_user", "polish_user"):
            self.assertIn(key, prompts)
        self.assertIn("{materials}", prompts["section_user"])
        self.assertIn("{heading}", prompts["section_user"])


class WritingApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db_path = str(Path(self.tmp.name) / "wapi.db")
        conn = connect(self.db_path)
        upsert_paper(conn, _paper("a:1"))
        conn.close()
        from research_agent.dashboard import writing_api as wrtapi
        self.api = wrtapi

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_project_flow_without_model(self):
        created = self.api.create_project(self.db_path, "流程项目", "topic")
        self.assertTrue(created["ok"])
        project_id = created["project_id"]

        outline = self.api.generate_outline(self.db_path, project_id, use_model=False)
        self.assertTrue(outline["ok"])
        self.assertEqual(outline["generated_by"], "pack_skeleton")

        detail = self.api.project_detail(self.db_path, project_id)
        self.assertTrue(detail["ok"])
        self.assertTrue(detail["sections"])
        first = detail["sections"][0]

        section = self.api.generate_section(
            self.db_path, project_id, first["section_key"], first["heading"],
            use_model=False,
        )
        self.assertTrue(section["ok"])
        self.assertIn("content", section)

        saved = self.api.save_section(
            self.db_path, project_id, first["section_key"], first["heading"],
            "手动改写的内容",
        )
        self.assertTrue(saved["ok"])
        self.assertTrue(any(s["content"] == "手动改写的内容"
                            for s in saved["sections"]))

        polished = self.api.polish_section(
            self.db_path, project_id, first["section_key"], "手动改写的内容",
            use_model=False,
        )
        self.assertFalse(polished["polished"])

        exported = self.api.export_project(self.db_path, project_id)
        self.assertTrue(exported["ok"])
        self.assertIn("手动改写的内容", exported["content"])

        deleted = self.api.delete_project(self.db_path, project_id)
        self.assertTrue(deleted["ok"])
        self.assertFalse(self.api.project_detail(self.db_path, project_id)["ok"])

    def test_create_project_without_title_returns_error(self):
        out = self.api.create_project(self.db_path, "  ")
        self.assertFalse(out["ok"])
        self.assertIn("标题", out["error"])

    def test_genres_endpoint(self):
        genres = self.api.genres()
        self.assertTrue(genres)
        self.assertTrue(all("sections" in g for g in genres))

    def test_missing_project_returns_error(self):
        for func in (self.api.project_detail, self.api.export_project):
            out = func(self.db_path, 99999)
            self.assertFalse(out["ok"])


class RouteWiringTest(unittest.TestCase):
    """确认新增路由已装配（不依赖 httpx/TestClient）。"""

    def test_app_declares_merged_routes(self):
        from research_agent.dashboard.app import create_app
        app = create_app(None, inject_llms=False)
        paths = {getattr(r, "path", "") for r in app.routes}
        for expected in (
            "/api/library/papers", "/api/library/facets", "/api/library/stats",
            "/api/library/paper/{key}", "/api/library/tags",
            "/api/library/folders", "/api/library/folders/{folder_id}/papers",
            "/api/library/favorites", "/api/library/batch", "/api/library/jobs",
            "/api/library/jobs/{job_id}", "/api/library/jobs/{job_id}/cancel",
            "/api/library/citation/{key}", "/api/library/export",
            "/api/system/packs",
            "/api/writing/genres", "/api/writing/projects",
            "/api/writing/projects/{project_id}",
            "/api/writing/projects/{project_id}/outline",
            "/api/writing/projects/{project_id}/sections",
            "/api/writing/projects/{project_id}/polish",
            "/api/writing/projects/{project_id}/export",
        ):
            self.assertIn(expected, paths, f"缺少路由 {expected}")

    def test_static_files_are_present(self):
        from research_agent.dashboard.app import STATIC_DIR
        for rel in ("index.html", "css/theme.css", "css/shell.css",
                    "js/app.js", "js/common/dom.js", "js/common/api.js",
                    "js/common/ui.js", "js/ui/design.js", "js/pages/shell.js",
                    "js/pages/registry.js", "js/pages/writing.js",
                    "js/pages/library.js", "js/pages/system.js",
                    "vendor/vis-network.min.js"):
            self.assertTrue((STATIC_DIR / rel).is_file(), f"缺少静态文件 {rel}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
