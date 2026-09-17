"""PWA -> research-agent 迁移脚本回归测试（合并新增）。

构造一个 minified 的 PWA 库（papers + 侧车四表），验证：
dry-run 不写库、apply 后主记录与侧车齐全、重复执行幂等、状态映射显式、质量分换算正确。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from research_agent.db import connect, get_paper, get_quality_result  # noqa: E402
from research_agent.library.store import LibraryStore  # noqa: E402
from tests._tmpdir import make_temp_dir  # noqa: E402

# 以文件路径直接加载脚本（scripts/ 不是包）
_spec = importlib.util.spec_from_file_location(
    "migrate_from_pwa", ROOT / "scripts" / "migrate_from_pwa.py")
migrate_mod = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(migrate_mod)

PWA_SCHEMA = """
CREATE TABLE papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, source_id TEXT, doi TEXT, title TEXT, title_norm TEXT,
    authors_json TEXT, abstract TEXT, year INTEGER, journal TEXT, url TEXT,
    citation_count INTEGER, metadata_json TEXT,
    quality_score REAL, quality_level TEXT, quality_reasons_json TEXT,
    status TEXT, duplicate_of INTEGER, created_at TEXT, updated_at TEXT
);
CREATE TABLE paper_folders (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, created_at TEXT);
CREATE TABLE paper_folder_map (folder_id INTEGER, paper_id INTEGER, PRIMARY KEY (folder_id, paper_id));
CREATE TABLE paper_tags (id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id INTEGER, tag TEXT, UNIQUE (paper_id, tag));
CREATE TABLE paper_favorites (paper_id INTEGER PRIMARY KEY);
"""


def build_pwa(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(PWA_SCHEMA)
    rows = [
        # id, doi, title, year, journal, score, status
        (1, "10.1000/a", "Gold catalysis of ynamides", 2023,
         "Angewandte Chemie (International ed. in English)", 88.0, "quality_passed"),
        (2, "10.1000/b", "Silver catalysis review", 2020, "Some Journal", 42.0,
         "quality_review"),
        (3, "", "No DOI record", 2019, "Another Journal", 30.0, "quality_rejected"),
    ]
    for pid, doi, title, year, journal, score, status in rows:
        conn.execute(
            "INSERT INTO papers(id, source, doi, title, title_norm, authors_json, "
            "abstract, year, journal, citation_count, quality_score, quality_level, "
            "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, "pubmed", doi, title, title.lower(), json.dumps(
                [{"name": "Zhang Wei"}, {"name": "Li Na"}]),
             "abstract text", year, journal, 7, score, "B", status, "2026-01-01",
             "2026-01-02"),
        )
    conn.execute("INSERT INTO paper_folders(id, name, created_at) VALUES(1,'精读','2026-01-01')")
    conn.execute("INSERT INTO paper_folder_map(folder_id, paper_id) VALUES(1,1)")
    conn.execute("INSERT INTO paper_tags(paper_id, tag) VALUES(1,'催化')")
    conn.execute("INSERT INTO paper_tags(paper_id, tag) VALUES(2,'待读')")
    conn.execute("INSERT INTO paper_favorites(paper_id) VALUES(1)")
    conn.commit()
    conn.close()


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.src = Path(self.tmp.name) / "paper_assistant.db"
        self.dst = Path(self.tmp.name) / "research_agent.db"
        build_pwa(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_does_not_write(self):
        report = migrate_mod.migrate(self.src, self.dst, apply=False,
                                     overwrite=False, limit=None)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["papers"]["total"], 3)
        self.assertEqual(report["papers"]["inserted"], 3)
        conn = connect(self.dst)
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM papers").fetchone()
            self.assertEqual(int(row["c"]), 0, "dry-run 不应写库")
        finally:
            conn.close()

    def test_apply_writes_papers_and_sidecar(self):
        report = migrate_mod.migrate(self.src, self.dst, apply=True,
                                     overwrite=False, limit=None)
        self.assertEqual(report["papers"]["inserted"], 3)
        self.assertEqual(report["sidecar"]["tags"], 2)
        self.assertEqual(report["sidecar"]["folders"], 1)
        self.assertEqual(report["sidecar"]["memberships"], 1)
        self.assertEqual(report["sidecar"]["favorites"], 1)
        self.assertEqual(report["quality_results"], 3)

        conn = connect(self.dst)
        try:
            store = LibraryStore(conn)
            keys = [r["paper_key"] for r in conn.execute(
                "SELECT paper_key FROM papers ORDER BY paper_key").fetchall()]
            self.assertEqual(len(keys), 3)
            doi_key = [k for k in keys if "10.1000/a" in k][0]
            paper = get_paper(conn, doi_key)
            self.assertEqual(paper["venue"],
                             "Angewandte Chemie (International ed. in English)")
            self.assertEqual(paper["pub_year"], 2023)
            self.assertEqual(paper["fulltext_source"], "abstract")
            self.assertEqual(paper["status"], "ingested")
            # authors_json -> authors_meta 转换（get_paper 会把 authors_meta 解析为 authors）
            self.assertEqual(paper["authors"][0]["name"], "Zhang Wei")
            # 侧车
            self.assertEqual(store.get_tags(doi_key), ["催化"])
            self.assertTrue(store.is_favorite(doi_key))
            self.assertEqual(len(store.folder_ids_for(doi_key)), 1)
            # 质量分 0-100 -> 0-1
            quality = get_quality_result(conn, doi_key)
            self.assertAlmostEqual(float(quality["quality"]), 0.88, places=3)
            self.assertIn("迁移自 PWA", quality["rationale"])
        finally:
            conn.close()

    def test_status_and_decision_mapping(self):
        migrate_mod.migrate(self.src, self.dst, apply=True, overwrite=False,
                            limit=None)
        conn = connect(self.dst)
        try:
            rows = conn.execute(
                "SELECT p.status, q.decision FROM papers p "
                "LEFT JOIN quality_results q ON q.paper_key = p.paper_key"
            ).fetchall()
            statuses = {r["status"] for r in rows}
            decisions = {r["decision"] for r in rows}
            self.assertIn("ingested", statuses)
            self.assertIn("human_review", statuses)
            self.assertIn("direct", decisions)
            self.assertIn("flagged", decisions)
            self.assertIn("human", decisions)
        finally:
            conn.close()

    def test_second_run_is_idempotent(self):
        first = migrate_mod.migrate(self.src, self.dst, apply=True,
                                    overwrite=False, limit=None)
        self.assertEqual(first["papers"]["inserted"], 3)
        second = migrate_mod.migrate(self.src, self.dst, apply=True,
                                     overwrite=False, limit=None)
        self.assertEqual(second["papers"]["inserted"], 0)
        self.assertEqual(second["papers"]["skipped_existing"], 3)
        conn = connect(self.dst)
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM papers").fetchone()
            self.assertEqual(int(row["c"]), 3, "重复迁移不应产生重复行")
            tags = conn.execute("SELECT COUNT(*) AS c FROM paper_tags").fetchone()
            self.assertEqual(int(tags["c"]), 2)
        finally:
            conn.close()

    def test_overwrite_updates_existing(self):
        migrate_mod.migrate(self.src, self.dst, apply=True, overwrite=False,
                            limit=None)
        conn = connect(self.dst)
        try:
            key = [r["paper_key"] for r in conn.execute(
                "SELECT paper_key FROM papers").fetchall() if "10.1000/a" in r["paper_key"]][0]
            conn.execute("UPDATE papers SET title='手工修改' WHERE paper_key=?", (key,))
            conn.commit()
        finally:
            conn.close()
        report = migrate_mod.migrate(self.src, self.dst, apply=True,
                                     overwrite=True, limit=None)
        self.assertEqual(report["papers"]["overwritten"], 3)
        conn = connect(self.dst)
        try:
            self.assertNotEqual(get_paper(conn, key)["title"], "手工修改")
        finally:
            conn.close()

    def test_key_generation_prefers_doi_then_title(self):
        self.assertEqual(
            migrate_mod.make_paper_key("pubmed", "10.1000/A", "t", 1),
            "pwa:pubmed:doi:10.1000/a")
        self.assertTrue(
            migrate_mod.make_paper_key("pubmed", "", "Some Title", 2)
            .startswith("pwa:pubmed:title:"))
        self.assertEqual(migrate_mod.make_paper_key("", "", "", 9), "pwa:pwa:id:9")

    def test_limit_option(self):
        report = migrate_mod.migrate(self.src, self.dst, apply=False,
                                     overwrite=False, limit=2)
        self.assertEqual(report["papers"]["total"], 2)

    def test_missing_source_raises(self):
        with self.assertRaises(SystemExit):
            migrate_mod.migrate(Path(self.tmp.name) / "nope.db", self.dst,
                                apply=False, overwrite=False, limit=None)

    def test_meta_records_migration_provenance(self):
        migrate_mod.migrate(self.src, self.dst, apply=True, overwrite=False,
                            limit=None)
        conn = connect(self.dst)
        try:
            row = conn.execute("SELECT v FROM meta WHERE k='pwa_migration'").fetchone()
            self.assertIsNotNone(row)
            payload = json.loads(row["v"])
            self.assertEqual(payload["papers_total"], 3)
            self.assertEqual(payload["sidecar"]["favorites"], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
