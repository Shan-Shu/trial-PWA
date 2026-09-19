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

from research_agent.db import (  # noqa: E402
    connect, get_paper, upsert_paper, utcnow)
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

    def test_delete_project_cleans_dispatch_runs_orphans(self):
        """删除项目必须连派工单一起清掉。

        `writing_sections` / `section_runs` 有 ``ON DELETE CASCADE``，但
        **`dispatch_runs.project_id` 没有外键** —— 不显式清理就会留下孤儿，
        而 `list_dispatches` 正是按 project_id 过滤的，会捞到已删项目的单子。
        """
        project_id = writing.create_project(self.conn, "带派工单的项目")
        self.conn.execute(
            "INSERT INTO dispatch_runs(dispatch_id, project_id, status, ts) "
            "VALUES(?,?,?,?)", ("d-orphan", project_id, "done", utcnow()))
        self.conn.commit()
        removed = writing.delete_project(self.conn, project_id)
        self.assertEqual(removed["dispatches"], 1)
        left = self.conn.execute(
            "SELECT COUNT(*) AS c FROM dispatch_runs WHERE project_id=?",
            (project_id,)).fetchone()
        self.assertEqual(int(left["c"]), 0)

    def test_delete_project_archives_before_removing(self):
        """删除前自动留档：不改变操作手感，但把"不可恢复"变成"可恢复"。

        实测教训：正式库 16 个项目消失时既无日志也无备份，无从追溯也无法恢复。
        """
        import json

        project_id = writing.create_project(self.conn, "留档测试", "主题")
        writing.save_section(self.conn, project_id, "abstract", "摘要", "正文内容")
        removed = writing.delete_project(self.conn, project_id)
        out_dir = Path(writing.__file__).resolve().parents[3] / "data" / "deleted_projects"
        files = sorted(out_dir.glob(f"*-p{project_id}.json"))
        self.assertTrue(files, f"应留下留档文件，目录 {out_dir}")
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
        self.assertEqual(payload["project"]["title"], "留档测试")
        self.assertTrue(any("正文内容" in str(s.get("content"))
                            for s in payload["sections"]),
                        "留档里应含正文")
        self.assertGreaterEqual(removed["sections"], 1)

    def test_archive_failure_does_not_block_delete(self):
        """留档失败绝不能让删除失败（用户要删就得删）。"""
        from unittest import mock

        project_id = writing.create_project(self.conn, "留档失败")
        with mock.patch.object(writing, "_archive_project",
                               side_effect=OSError("disk full")):
            writing.delete_project(self.conn, project_id)
        self.assertIsNone(writing.get_project(self.conn, project_id))

    def test_delete_project_is_logged(self):
        """删除必须留痕。

        此前一声不响：实测发生过"正式库 16 个项目消失"，而统一事件日志里查不到
        任何痕迹，只能靠外部副本去猜谁删了什么。级别用 WARN（不可恢复）。
        """
        from unittest import mock

        project_id = writing.create_project(self.conn, "留痕测试")
        with mock.patch.object(writing, "log_event") as spy:
            writing.delete_project(self.conn, project_id)
        self.assertTrue(spy.called, "删除应写一条事件")
        args, kwargs = spy.call_args
        self.assertEqual(args[0], "project.deleted")
        self.assertEqual(kwargs.get("level"), "WARN")
        self.assertEqual(kwargs["data"]["project_id"], project_id)
        self.assertEqual(kwargs["data"]["title"], "留痕测试")

    def test_rename_project_is_logged(self):
        from unittest import mock

        project_id = writing.create_project(self.conn, "旧名")
        with mock.patch.object(writing, "log_event") as spy:
            writing.rename_project(self.conn, project_id, title="新名")
        self.assertTrue(spy.called, "改名应写一条事件")
        args, kwargs = spy.call_args
        self.assertEqual(args[0], "project.renamed")
        self.assertEqual(kwargs["data"]["title"], "新名")

    def test_delete_project_reports_removed_counts(self):
        """删除要如实回报清掉了什么（界面据此告诉用户删掉了几节）。"""
        project_id = writing.create_project(self.conn, "待删统计")
        removed = writing.delete_project(self.conn, project_id)
        self.assertGreaterEqual(removed["sections"], 1)
        self.assertIn("runs", removed)
        self.assertIn("dispatches", removed)

    def test_batch_delete_projects_skips_missing_ids(self):
        """批量删除：不存在的 id 不报错，如实回报；去重，且不误删别的项目。"""
        keep = writing.create_project(self.conn, "保留")
        first = writing.create_project(self.conn, "删A")
        second = writing.create_project(self.conn, "删B")
        result = writing.batch_delete_projects(
            self.conn, [first, second, 99999, first])
        self.assertEqual(sorted(result["deleted"]), sorted([first, second]))
        self.assertEqual(result["missing"], [99999])
        self.assertIsNotNone(writing.get_project(self.conn, keep))
        self.assertIsNone(writing.get_project(self.conn, first))

    def test_rename_project_updates_only_given_fields(self):
        project_id = writing.create_project(self.conn, "旧标题", "旧主题")
        updated = writing.rename_project(self.conn, project_id,
                                        title="新标题", topic="新主题")
        self.assertEqual(updated["title"], "新标题")
        self.assertEqual(updated["topic"], "新主题")
        partial = writing.rename_project(self.conn, project_id, title="只改标题")
        self.assertEqual(partial["title"], "只改标题")
        self.assertEqual(partial["topic"], "新主题", "没给的字段不该被动")

    def test_rename_project_rejects_empty_title(self):
        """标题不能改成空——否则管理页会留下一条无法辨认的记录。"""
        project_id = writing.create_project(self.conn, "有标题")
        with self.assertRaises(ValueError):
            writing.rename_project(self.conn, project_id, title="   ")
        self.assertEqual(
            writing.get_project(self.conn, project_id)["title"], "有标题")

    def test_list_projects_carries_overview_fields(self):
        """概况字段是"项目管理"的基础：已写/总节数 + 派工单数。"""
        project_id = writing.create_project(self.conn, "概况")
        self.conn.execute(
            "INSERT INTO dispatch_runs(dispatch_id, project_id, status, ts) "
            "VALUES(?,?,?,?)", ("d-ov", project_id, "done", utcnow()))
        self.conn.commit()
        row = [p for p in writing.list_projects(self.conn)
               if p["project_id"] == project_id][0]
        self.assertEqual(row["dispatch_count"], 1)
        self.assertIn("filled_sections", row)
        self.assertIn("total_sections", row)

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


class ProjectDeleteGuardTest(unittest.TestCase):
    """写作台删除保护：正在跑作业的项目不许删。

    这是 desk 界面里的纯逻辑（`_blocked_by_running_job`），放在这里是因为它的
    失败后果落在引擎的表上：作业落库时会往 `writing_sections` 写一行指向已删
    项目的记录，撞外键约束 → 用户看到"写作失败"而不是"你删早了"。
    """

    def _guard(self):
        from desk.ui.views.writing import _blocked_by_running_job
        return _blocked_by_running_job

    def test_no_running_job_deletes_everything(self):
        keep, blocked = self._guard()([1, 2, 3], None)
        self.assertEqual(keep, [1, 2, 3])
        self.assertIsNone(blocked)

    def test_running_project_is_removed_from_targets(self):
        keep, blocked = self._guard()([1, 2, 3], 2)
        self.assertEqual(keep, [1, 3])
        self.assertEqual(blocked, 2)

    def test_running_project_not_in_targets_does_not_block(self):
        keep, blocked = self._guard()([1, 3], 2)
        self.assertEqual(keep, [1, 3])
        self.assertIsNone(blocked)

    def test_only_running_project_leaves_nothing_to_delete(self):
        keep, blocked = self._guard()([5], 5)
        self.assertEqual(keep, [])
        self.assertEqual(blocked, 5)


class AdapterDbPathTest(unittest.TestCase):
    """适配器只给 db_path 时，settings 必须跟着指向同一个库。

    这是实测事故的回归：启动器用 `RA_DESK_DB=data\\desk.db` 调
    `build_adapter(db_path=...)`，而 `settings` 默认是**全局单例**（指向
    `data/research_agent.db`）。适配器自己的查询走 `self.db_path` 所以看着正常，
    但转发给引擎的 settings 是另一个库——凡是按 `settings.db_path` 取连接的引擎
    路径（补检里的 `run_topic`）就把结果写进了错误的库。
    后果：抓到的文献与 PDF 全进 research_agent.db（实测把它从 1.1 MB 撑到 130 MB），
    而 desk.db 一篇都没加，用户点完"补检"等于白跑。

    **这是"对 A 库操作却写进 B 库"的同类缺陷**，必须在适配器入口就堵死。
    """

    def test_settings_follows_explicit_db_path(self):
        from desk.backend import build_adapter

        adapter = build_adapter(db_path=r"D:\tmp\somewhere\desk.db")
        self.assertEqual(str(adapter.db_path), r"D:\tmp\somewhere\desk.db")
        self.assertEqual(str(adapter.settings.db_path),
                         r"D:\tmp\somewhere\desk.db",
                         "settings.db_path 必须与适配器用同一个库")

    def test_global_default_settings_not_mutated(self):
        """修的是副本，不能就地改全局单例——那会污染同进程里其它调用方。"""
        from desk.backend import build_adapter
        from research_agent.config import settings as default_settings

        before = str(default_settings.db_path)
        build_adapter(db_path=r"D:\tmp\other\desk.db")
        self.assertEqual(str(default_settings.db_path), before)

    def test_explicit_settings_is_respected(self):
        """调用方明确传了 settings 时，以它为准（不强行改写）。"""
        from desk.backend import build_adapter
        from research_agent.config import Settings

        mine = Settings(db_path=Path(r"D:\tmp\mine.db"))
        adapter = build_adapter(db_path=r"D:\tmp\given.db", settings=mine)
        self.assertIs(adapter.settings, mine)

    def test_retrieval_pipeline_lands_in_same_db(self):
        """补检的检索流水线必须落在同一个库。

        真正写库的是 `run_topic`：它按 ``services.settings.db_path`` 取连接。
        所以这里直接断言"由适配器 settings 构建出来的检索服务栈"指向同一个库——
        这是把修复与真正出问题的那条路径连起来，而不是只测一个构造函数。
        """
        from desk.backend import build_adapter
        from research_agent.writing.collaboration import build_retrieval_services

        adapter = build_adapter(db_path=r"D:\tmp\collab\desk.db")
        services = build_retrieval_services(adapter.settings)
        self.assertEqual(str(services.settings.db_path),
                         str(adapter.db_path),
                         "补检会把文献写进 services.settings.db_path")


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

    def test_desk_ui_files_are_present(self):
        """界面改为 Streamlit 后，检查**真正在用的界面**是否齐备。

        原断言检查的是已退役的 JS 前端（`dashboard/static/**`），它刻意未随
        research-desk 搬入；继续断言它只会永远失败，还掩盖真正的回归
        ——比如新增页面忘了注册、views 目录被误删、或旧前端被误加回来。
        """
        import desk
        import desk.ui
        from desk.ui.views import PAGES

        ui_dir = Path(desk.ui.__file__).resolve().parent
        for rel in ("app.py", "components.py", "task_ui.py",
                    "views/__init__.py"):
            self.assertTrue((ui_dir / rel).is_file(), f"缺少界面文件 {rel}")
        # 10 个页面都要有对应的 view 模块（防止漏注册/误删）
        expected = {"dashboard", "experiment", "knowledge", "library",
                    "ontology", "retrieval", "review", "search", "system",
                    "writing"}
        missing = [n for n in expected
                   if not (ui_dir / "views" / f"{n}.py").is_file()]
        self.assertEqual(missing, [], f"缺少页面模块: {missing}")
        self.assertEqual(len(PAGES), 10, "页面注册数应为 10")

    def test_legacy_js_frontend_is_not_shipped(self):
        """旧 JS 前端已退役：它**不该**被搬进来（否则又是两套前端并行）。"""
        from research_agent.dashboard.app import STATIC_DIR
        self.assertFalse(STATIC_DIR.exists(),
                         "旧 JS 前端应已退役，不应随 research-desk 搬入")


if __name__ == "__main__":
    unittest.main(verbosity=2)
