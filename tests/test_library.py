"""文献库层回归测试（合并新增）。

覆盖：侧车表 CRUD、外键级联、筛选下推（含年份未知与低信号档位）、分面、
批量作业（进度/取消/单篇失败隔离）、引用四种样式与期刊缩写、导出、系统状态端点。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from research_agent import packs  # noqa: E402
from research_agent.db import (  # noqa: E402
    connect,
    meta_get,
    save_quality_result,
    upsert_paper,
    utcnow,
)
from research_agent.library import citation as cite  # noqa: E402
from research_agent.library.jobs import LibraryJobManager, run_batch_extract  # noqa: E402
from research_agent.library.store import LibraryStore  # noqa: E402
from tests._tmpdir import make_temp_dir  # noqa: E402


def _paper(key: str, **over) -> dict:
    rec = {
        "paper_key": key,
        "source": "europepmc",
        "title": "Catalytic annulation of ynamides",
        "abstract": "A gold-catalysed annulation is reported.",
        "doi": "10.1000/xyz",
        "venue": "Angewandte Chemie (International ed. in English)",
        "source_type": "journal",
        "pub_year": 2024,
        "citation_count": 12,
        "authors_meta": json.dumps(
            [{"name": "Zhang Wei"}, {"name": "Li Na"}, {"name": "Wang Lei"},
             {"name": "Chen Xi"}],
            ensure_ascii=False,
        ),
        "volume": "63",
        "issue": "12",
        "pages": "e202312345",
        "fulltext_source": "abstract",
        "status": "ingested",
    }
    rec.update(over)
    return rec


class LibraryStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db_path = str(Path(self.tmp.name) / "lib.db")
        self.conn = connect(self.db_path)
        self.store = LibraryStore(self.conn)
        for key in ("a:1", "a:2", "a:3"):
            upsert_paper(self.conn, _paper(key))
        save_quality_result(self.conn, {
            "paper_key": "a:1", "authority": 0.9, "timeliness": 0.8,
            "quality": 0.86, "decision": "direct", "needs_review": False,
            "meta_missing": [], "rationale": "ok",
        })
        save_quality_result(self.conn, {
            "paper_key": "a:2", "authority": 0.4, "timeliness": 0.3,
            "quality": 0.36, "decision": "human", "needs_review": True,
            "meta_missing": ["doi"], "rationale": "low",
        })

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------- 标签
    def test_tag_crud_is_idempotent(self):
        self.assertTrue(self.store.add_tag("a:1", "综述"))
        self.assertFalse(self.store.add_tag("a:1", "综述"))
        self.assertEqual(self.store.get_tags("a:1"), ["综述"])
        self.assertEqual(self.store.remove_tag("a:1", "综述"), 1)
        self.assertEqual(self.store.get_tags("a:1"), [])

    def test_empty_tag_is_ignored(self):
        self.assertFalse(self.store.add_tag("a:1", "   "))
        self.assertEqual(self.store.get_tags("a:1"), [])

    def test_batch_tag_and_counts(self):
        added = self.store.batch_tag(["a:1", "a:2", "a:3"], "catalysis")
        self.assertEqual(added, 3)
        counts = {row["tag"]: row["count"] for row in self.store.all_tags()}
        self.assertEqual(counts["catalysis"], 3)

    def test_tags_for_is_batched(self):
        self.store.add_tag("a:1", "x")
        self.store.add_tag("a:2", "y")
        mapping = self.store.tags_for(["a:1", "a:2", "a:3"])
        self.assertEqual(mapping["a:1"], ["x"])
        self.assertEqual(mapping["a:2"], ["y"])
        self.assertEqual(mapping["a:3"], [])

    # ------------------------------------------------------------- 文件夹
    def test_folder_create_is_idempotent(self):
        first = self.store.create_folder("本体构建")
        second = self.store.create_folder("本体构建")
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.list_folders()), 1)

    def test_folder_requires_name(self):
        with self.assertRaises(ValueError):
            self.store.create_folder("  ")

    def test_folder_membership_and_count(self):
        fid = self.store.create_folder("组A")
        self.assertTrue(self.store.add_to_folder("a:1", fid))
        self.assertFalse(self.store.add_to_folder("a:1", fid))
        self.assertEqual(self.store.folder_ids_for("a:1"), [fid])
        folder = self.store.list_folders()[0]
        self.assertEqual(folder["count"], 1)
        self.assertEqual(self.store.remove_from_folder("a:1", fid), 1)

    def test_delete_folder_cascades_membership(self):
        fid = self.store.create_folder("临时")
        self.store.add_to_folder("a:1", fid)
        self.store.delete_folder(fid)
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM paper_folder_map WHERE folder_id=?", (fid,)
        ).fetchone()
        self.assertEqual(int(row["c"]), 0)

    # ------------------------------------------------------------- 收藏
    def test_favorite_toggle_and_batch(self):
        self.assertFalse(self.store.is_favorite("a:1"))
        self.store.set_favorite("a:1", True)
        self.assertTrue(self.store.is_favorite("a:1"))
        changed = self.store.batch_favorite(["a:2", "a:3"], True)
        self.assertEqual(changed, 2)
        self.assertEqual(self.store.favorite_paper_keys(), {"a:1", "a:2", "a:3"})
        self.assertEqual(self.store.batch_favorite(["a:1"], True), 0)
        self.store.set_favorite("a:1", False)
        self.assertFalse(self.store.is_favorite("a:1"))

    # ------------------------------------------------------------- 级联删除
    def test_delete_paper_cascades_sidecar_rows(self):
        self.store.add_tag("a:1", "t")
        fid = self.store.create_folder("f")
        self.store.add_to_folder("a:1", fid)
        self.store.set_favorite("a:1", True)
        self.conn.execute("DELETE FROM papers WHERE paper_key=?", ("a:1",))
        self.conn.commit()
        for table, column in (("paper_tags", "paper_key"),
                              ("paper_folder_map", "paper_key"),
                              ("paper_favorites", "paper_key")):
            row = self.conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE {column}=?", ("a:1",)
            ).fetchone()
            self.assertEqual(int(row["c"]), 0, f"{table} 应被级联清理")

    # ------------------------------------------------------------- 查询
    def test_query_sorts_by_quality_desc(self):
        out = self.store.query_papers(sort_by="quality", sort_desc=True)
        self.assertEqual(out["total"], 3)
        self.assertEqual(out["items"][0]["paper_key"], "a:1")

    def test_query_excludes_blobs(self):
        out = self.store.query_papers(limit=5)
        self.assertNotIn("pdf_blob", out["items"][0])
        self.assertNotIn("clean_text", out["items"][0])

    def test_filter_by_decision_and_status(self):
        human = self.store.query_papers(decision="human")
        self.assertEqual([i["paper_key"] for i in human["items"]], ["a:2"])
        direct = self.store.query_papers(decision="direct")
        self.assertEqual([i["paper_key"] for i in direct["items"]], ["a:1"])

    def test_year_unknown_bucket(self):
        upsert_paper(self.conn, _paper("a:4", pub_year=None))
        unknown = self.store.query_papers(year_mode="unknown")
        keys = [i["paper_key"] for i in unknown["items"]]
        self.assertIn("a:4", keys)
        self.assertNotIn("a:1", keys)
        # 默认（不传 year_mode）也应能看到年份缺失的记录，而不是被 1900/2100 过滤掉
        every = self.store.query_papers(limit=50)
        self.assertIn("a:4", [i["paper_key"] for i in every["items"]])

    def test_pagination_and_total(self):
        page1 = self.store.query_papers(limit=2, offset=0)
        page2 = self.store.query_papers(limit=2, offset=2)
        self.assertEqual(page1["total"], 3)
        self.assertEqual(len(page1["items"]), 2)
        self.assertEqual(len(page2["items"]), 1)

    def test_filter_by_tag_folder_favorite(self):
        self.store.add_tag("a:2", "待读")
        fid = self.store.create_folder("精读")
        self.store.add_to_folder("a:3", fid)
        self.store.set_favorite("a:1", True)
        by_tag = self.store.query_papers(tag="待读")
        self.assertEqual([i["paper_key"] for i in by_tag["items"]], ["a:2"])
        by_folder = self.store.query_papers(folder_id=fid)
        self.assertEqual([i["paper_key"] for i in by_folder["items"]], ["a:3"])
        fav = self.store.query_papers(favorite_only=True)
        self.assertEqual([i["paper_key"] for i in fav["items"]], ["a:1"])

    def test_low_signal_filter(self):
        self.conn.execute(
            "INSERT INTO processing_log(paper_key, node, event, details, ts) "
            "VALUES(?,?,?,?,?)",
            ("a:2", "retrieval", "relevance-gate-low-signal",
             json.dumps({"low_signal": "topic_token_overlap_zero"}),
             utcnow()),
        )
        self.conn.commit()
        low = self.store.query_papers(low_signal="yes")
        self.assertEqual([i["paper_key"] for i in low["items"]], ["a:2"])
        normal = self.store.query_papers(low_signal="no")
        self.assertNotIn("a:2", [i["paper_key"] for i in normal["items"]])

    def test_facets_shape(self):
        self.store.add_tag("a:1", "t")
        self.store.create_folder("f")
        data = self.store.facets()
        for key in ("tags", "folders", "sources", "statuses", "decisions",
                    "year_min", "year_max", "year_unknown"):
            self.assertIn(key, data)
        self.assertEqual(data["folders"][0]["name"], "f")
        self.assertEqual(data["year_min"], 2024)

    def test_stats(self):
        stats = self.store.stats()
        self.assertEqual(stats["papers"], 3)
        self.assertEqual(stats["assessed"], 2)
        self.assertEqual(stats["needs_review"], 1)


class CitationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db_path = str(Path(self.tmp.name) / "cite.db")
        self.conn = connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_styles_loaded_from_pack(self):
        styles = cite.available_styles()
        for expected in ("gb7714", "apa", "acs", "bibtex", "ris", "markdown"):
            self.assertIn(expected, styles)
        self.assertEqual(cite.default_style(), "gb7714")

    def test_unknown_style_raises(self):
        with self.assertRaises(ValueError):
            cite.format_citation(_paper("a:1"), "no-such-style")

    def test_venue_abbreviation_via_pack(self):
        rec = _paper("a:1")
        self.assertEqual(cite.venue_label(rec), "Angewandte Chemie International Edition")
        self.assertEqual(cite.venue_label(rec, abbreviated=True), "Angew. Chem. Int. Ed.")

    def test_gb7714_uses_doc_type_and_volume(self):
        text = cite.format_citation(_paper("a:1"), "gb7714")
        self.assertIn("[J]", text)
        self.assertIn("63", text)
        self.assertIn("2024", text)
        self.assertIn("DOI:10.1000/xyz", text)

    def test_bibtex_entry_type_follows_source_type(self):
        journal = cite.format_citation(_paper("a:1"), "bibtex")
        self.assertTrue(journal.startswith("@article{"), journal[:40])
        conf = cite.format_citation(
            _paper("a:2", source_type="proceedings", venue="Some Conference"),
            "bibtex",
        )
        self.assertTrue(conf.startswith("@inproceedings{"), conf[:40])
        self.assertIn("booktitle", conf)

    def test_bibtex_has_volume_number_pages(self):
        text = cite.format_citation(_paper("a:1"), "bibtex")
        for field in ("volume", "number", "pages", "doi"):
            self.assertIn(f"{field} = {{", text)

    def test_bibtex_key_is_ascii_for_cjk_authors(self):
        rec = _paper("a:3", authors_meta=json.dumps([{"name": "张三"}],
                                                    ensure_ascii=False))
        text = cite.format_citation(rec, "bibtex")
        key = text.split("{", 1)[1].split(",", 1)[0]
        self.assertTrue(key.isascii(), key)
        self.assertTrue(key.strip(), key)

    def test_bibtex_keys_are_deduplicated(self):
        recs = [_paper("a:1"), _paper("a:2"), _paper("a:3")]
        text = cite.render_many(recs, "bibtex")
        keys = [chunk.split("{", 1)[1].split(",", 1)[0]
                for chunk in text.split("@")[1:]]
        self.assertEqual(len(keys), len(set(keys)), keys)

    def test_ris_export(self):
        text = cite.format_citation(_paper("a:1"), "ris")
        self.assertIn("TY  - JOUR", text)
        self.assertIn("ER  - ", text)

    def test_markdown_export_lists_records(self):
        text = cite.export([_paper("a:1"), _paper("a:2")], "markdown")
        self.assertIn("# 文献导出", text)
        self.assertIn("## 1.", text)
        self.assertIn("## 2.", text)

    def test_plain_style_numbers_multiple_records(self):
        text = cite.render_many([_paper("a:1"), _paper("a:2")], "apa")
        self.assertTrue(text.startswith("[1] "), text[:20])
        self.assertIn("[2] ", text)

    def test_missing_fields_do_not_crash(self):
        rec = {"paper_key": "a:9", "title": None, "authors_meta": None,
               "venue": None, "pub_year": None, "doi": None}
        for style in ("gb7714", "apa", "acs", "bibtex", "ris"):
            text = cite.format_citation(rec, style)
            self.assertIsInstance(text, str)
            self.assertTrue(text.strip())

    def test_authors_meta_string_and_list(self):
        rec_str = _paper("a:1", authors_meta='[{"name": "Zhang Wei"}]')
        rec_list = _paper("a:2", authors_meta=[{"name": "Zhang Wei"}])
        self.assertEqual(cite.format_citation(rec_str, "apa"),
                         cite.format_citation(rec_list, "apa"))

    def test_gb7714_truncates_many_authors(self):
        text = cite.format_citation(_paper("a:1"), "gb7714")
        self.assertIn("等", text)


class LibraryJobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db_path = str(Path(self.tmp.name) / "jobs.db")
        conn = connect(self.db_path)
        for key in ("j:1", "j:2"):
            upsert_paper(conn, _paper(key))
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_job_runs_and_reports_terminal_state(self):
        manager = LibraryJobManager(self.db_path)

        def runner(*args, progress_cb=None, cancel_event=None):
            for i in range(3):
                if progress_cb:
                    progress_cb((i + 1) / 3 * 100, f"step {i + 1}")
            return {"done": 3}

        job_id = manager.start("unit", runner, total=3)
        for _ in range(100):
            snap = manager.status(job_id)
            if snap and snap["status"] in {"done", "error", "cancelled"}:
                break
            time.sleep(0.02)
        snap = manager.status(job_id)
        self.assertEqual(snap["status"], "done")
        self.assertEqual(snap["percent"], 100.0)
        self.assertEqual(snap["result"], {"done": 3})

    def test_cancel_marks_job_cancelled(self):
        manager = LibraryJobManager(self.db_path)
        started = threading.Event()

        def runner(*args, progress_cb=None, cancel_event=None):
            started.set()
            for _ in range(200):
                if cancel_event is not None and cancel_event.is_set():
                    return {"cancelled": True}
                time.sleep(0.01)
            return {"cancelled": False}

        job_id = manager.start("unit", runner, total=1)
        started.wait(timeout=2)
        self.assertTrue(manager.cancel(job_id))
        for _ in range(200):
            snap = manager.status(job_id)
            if snap and snap["status"] in {"done", "error", "cancelled"}:
                break
            time.sleep(0.02)
        self.assertEqual(manager.status(job_id)["status"], "cancelled")

    def test_error_is_captured(self):
        manager = LibraryJobManager(self.db_path)

        def runner(*args, progress_cb=None, cancel_event=None):
            raise RuntimeError("boom")

        job_id = manager.start("unit", runner, total=1)
        for _ in range(100):
            snap = manager.status(job_id)
            if snap and snap["status"] in {"done", "error", "cancelled"}:
                break
            time.sleep(0.02)
        snap = manager.status(job_id)
        self.assertEqual(snap["status"], "error")
        self.assertIn("boom", snap["error"])

    def test_list_jobs_does_not_deadlock(self):
        """来源项目 PWA 的 list_tasks() 在持锁时回调 status() 会自锁，此处必须不锁。"""
        manager = LibraryJobManager(self.db_path)

        def runner(*args, progress_cb=None, cancel_event=None):
            return {}

        manager.start("unit", runner, total=1)
        done = threading.Event()

        def _call():
            manager.list_jobs(limit=5)
            done.set()

        thread = threading.Thread(target=_call, daemon=True)
        thread.start()
        self.assertTrue(done.wait(timeout=3), "list_jobs 疑似自锁")

    def test_status_of_unknown_job_is_none(self):
        manager = LibraryJobManager(self.db_path)
        self.assertIsNone(manager.status("does-not-exist"))

    def test_batch_extract_accounting_is_total(self):
        """批量作业体的台账必须守恒；单篇失败不得中断批次。

        注意：这里只校验台账，不触发 pipeline 的联网元数据回补
        （因此不依赖网络，也不会因外部服务不可达而变脆）。
        """
        result = run_batch_extract([], db_path=self.db_path)
        self.assertEqual(result["total"], 0)
        for key in ("ok", "failed", "skipped", "errors"):
            self.assertIn(key, result)
        # 不存在的 key 也必须被记账，而不是抛错中断
        result = run_batch_extract(["missing:key"], db_path=self.db_path)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["ok"] + result["skipped"] + result["failed"], 1)
        self.assertEqual(result["ok"], 0)


class LibraryApiTest(unittest.TestCase):
    """直接调用数据层函数（不依赖 httpx/TestClient）。"""

    def setUp(self):
        self.tmp = make_temp_dir()
        self.db_path = str(Path(self.tmp.name) / "api.db")
        conn = connect(self.db_path)
        for key in ("p:1", "p:2"):
            upsert_paper(conn, _paper(key))
        conn.close()
        from research_agent.dashboard import library_api as libapi
        self.libapi = libapi

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_papers_returns_items_and_facets(self):
        out = self.libapi.list_papers(self.db_path, limit=10)
        self.assertEqual(out["total"], 2)
        self.assertIn("facets", out)
        self.assertEqual(out["limit"], 10)

    def test_sidecar_roundtrip(self):
        self.libapi.add_tag(self.db_path, "p:1", "重要")
        fid = self.libapi.create_folder(self.db_path, "第一批")
        self.libapi.add_to_folder(self.db_path, fid, ["p:1"])
        self.libapi.set_favorites(self.db_path, ["p:1"], True)
        data = self.libapi.paper_sidecar(self.db_path, "p:1")
        self.assertEqual(data["tags"], ["重要"])
        self.assertEqual(data["folder_names"], ["第一批"])
        self.assertTrue(data["favorite"])

    def test_citation_endpoint_returns_all_styles(self):
        data = self.libapi.citation_for(self.db_path, "p:1", "bibtex")
        self.assertTrue(data["ok"])
        self.assertIn("@article", data["text"])
        self.assertIn("gb7714", data["rendered"])

    def test_citation_unknown_style_returns_error(self):
        data = self.libapi.citation_for(self.db_path, "p:1", "bogus")
        self.assertFalse(data["ok"])
        self.assertIn("未知引用样式", data["error"])

    def test_citation_missing_paper(self):
        data = self.libapi.citation_for(self.db_path, "nope")
        self.assertFalse(data["ok"])

    def test_export_reports_missing_keys(self):
        data = self.libapi.export_records(self.db_path, ["p:1", "nope"], "bibtex")
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["missing"], ["nope"])
        self.assertTrue(data["filename"].endswith(".bib"))

    def test_export_unknown_style_is_error_not_silent(self):
        data = self.libapi.export_records(self.db_path, ["p:1"], "bogus")
        self.assertFalse(data["ok"])

    def test_start_batch_returns_job_and_completes(self):
        started = self.libapi.start_batch(
            self.db_path, "favorite", ["p:1", "p:2"], {"value": True})
        self.assertTrue(started["ok"])
        job_id = started["job_id"]
        for _ in range(200):
            snap = self.libapi.job_status(self.db_path, job_id)
            if snap and snap["status"] in {"done", "error", "cancelled"}:
                break
            time.sleep(0.02)
        self.assertEqual(self.libapi.job_status(self.db_path, job_id)["status"], "done")
        self.assertTrue(self.libapi.paper_sidecar(self.db_path, "p:2")["favorite"])

    def test_start_batch_rejects_unknown_action(self):
        out = self.libapi.start_batch(self.db_path, "explode", ["p:1"], {})
        self.assertFalse(out["ok"])

    def test_stats_endpoint(self):
        stats = self.libapi.stats(self.db_path)
        self.assertEqual(stats["papers"], 2)
        self.assertIn("low_signal", stats)

    def test_system_packs_reports_sources(self):
        data = self.libapi.system_packs(self.db_path)
        self.assertIn("packs", data)
        self.assertIn("journal", data)
        self.assertIn("citation", data)
        self.assertGreater(data["journal"]["entries"], 0)
        self.assertGreater(data["journal"]["default_subset"], 0)
        self.assertIn("gb7714", data["citation"]["styles"])
        self.assertEqual(data["citation"]["venue_overrides"], 26)

    def test_system_packs_counts_unknown_year_and_unmatched(self):
        conn = connect(self.db_path)
        upsert_paper(conn, _paper("p:3", pub_year=None,
                                  venue="Some Unlisted Journal"))
        conn.close()
        data = self.libapi.system_packs(self.db_path)
        self.assertEqual(data["journal"]["unknown_year_papers"], 1)
        self.assertIn("Some Unlisted Journal",
                      [row["venue"] for row in data["journal"]["unmatched_top"]])

    def test_default_subset_matches_pack_file(self):
        base = packs.skill_dir("journal-quartiles")
        payload = json.loads(
            (base / "content" / "quartiles_chemistry.json").read_text(encoding="utf-8"))
        inner = payload.get("quartiles") if isinstance(payload, dict) else None
        expected = len(inner if isinstance(inner, dict) else payload)
        self.assertEqual(self.libapi._default_subset_size(), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
