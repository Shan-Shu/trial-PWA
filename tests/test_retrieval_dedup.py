"""补检去重：库内已有的候选**不该再走一遍**下载/清洗/评估/抽取。

来源是一次真实体检：一次补检抓回 21 篇，其中 **19 篇库里本来就有** ——
重复文献仍会走「LLM 清洗元数据 → 下载 PDF → 解析 → 入库」，随后还要再吃
质量评估与知识抽取的模型调用，配额基本白烧。

这里守住三件事：
1. 三种匹配键（paper_key / DOI / 归一化标题）都要能命中，尤其**跨来源**的
   DOI 与**带 HTML 实体/标签**的标题；
2. 真正的新文献不能被误判成重复（宁多抓一篇，不少抓一篇）；
3. 命中后必须**在 download_pdf 之前**就返回（这是省开销的关键，用假 ApiHub
   记录调用次数来证明），并留一条事件日志。
"""
from __future__ import annotations

import json
import unittest

from research_agent.config import Settings
from research_agent.db import connect, upsert_paper
from research_agent.logging import configure, reset
from research_agent.retrieval.node import (
    _normalize_title,
    ingest_search_results,
    split_library_duplicates,
)

from tests._tmpdir import make_temp_dir


def _record(key: str, **over) -> dict:
    rec = {
        "paper_key": key,
        "source": "europepmc",
        "title": "Gold catalysis annulation of ynamides",
        "abstract": "A gold complex catalyses ynamide annulation with high "
                    "regioselectivity and broad substrate scope.",
        "venue": "J. Test",
        "source_type": "journal",
        "pub_year": 2024,
        "status": "ingested",
    }
    rec.update(over)
    return rec


class _FakeApi:
    """只提供检索与下载；**记录 download_pdf 调用**以证明跳过了真实开销。"""

    def __init__(self, records: list[dict]) -> None:
        self.records = records
        self.downloads: list[str] = []

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        return [dict(r) for r in self.records[:max_results]]

    def download_pdf(self, rec: dict):  # noqa: ANN001
        self.downloads.append(str(rec.get("paper_key")))
        return None


class TitleNormalizationTest(unittest.TestCase):
    def test_html_entity_and_tag_forms_collapse(self):
        """同一标题的 HTML 实体版与标签版必须归一到同一串。

        实测里两种写法都由真实来源产出过（``&lt;i&gt;N&lt;/i&gt;-Mesyl`` 与
        ``<i>N</i>-Mesyl``），不抹平就会把同一篇当成两篇。
        """
        a = _normalize_title("&lt;i&gt;N&lt;/i&gt;-Mesyl-Enabled Cu Catalysis")
        b = _normalize_title("<i>N</i>-Mesyl-Enabled Cu Catalysis")
        self.assertEqual(a, b)
        # 连字符按词分隔处理（换成空格），所以是 mesyl enabled 而不是 mesylenabled
        self.assertEqual(a, "n mesyl enabled cu catalysis")

    def test_punctuation_and_case_ignored(self):
        self.assertEqual(
            _normalize_title("Pd-catalyzed, Regioselective Synthesis."),
            _normalize_title("Pd catalyzed regioselective synthesis"))


class SplitDuplicatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.db = connect(f"{self.tmp.name}/dedup.db")
        upsert_paper(self.db, _record("seed:1", doi="10.1000/abc",
                                      title="Gold catalysis annulation"))
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _split(self, records):
        return split_library_duplicates(self.db, records)

    def test_matches_by_paper_key(self):
        fresh, dupes = self._split([_record("seed:1")])
        self.assertEqual(fresh, [])
        self.assertEqual(dupes[0]["_duplicate_by"], "paper_key")

    def test_matches_by_doi_across_sources(self):
        """跨来源判重必须靠 DOI：paper_key 带来源前缀，同一篇可能不同 key。"""
        fresh, dupes = self._split([
            _record("openalex:W999", doi="https://doi.org/10.1000/ABC",
                    title="完全不同的标题")])
        self.assertEqual(fresh, [])
        self.assertEqual(dupes[0]["_duplicate_by"], "doi")

    def test_matches_by_title_with_html_entities(self):
        fresh, dupes = self._split([
            _record("openalex:W888", doi="",
                    title="&lt;i&gt;Gold&lt;/i&gt; Catalysis Annulation")])
        self.assertEqual(fresh, [])
        self.assertEqual(dupes[0]["_duplicate_by"], "title")

    def test_new_paper_is_kept(self):
        fresh, dupes = self._split([
            _record("openalex:W777", doi="10.1000/xyz",
                    title="A completely different paper about ligands")])
        self.assertEqual(len(fresh), 1)
        self.assertEqual(dupes, [])

    def test_title_prefix_is_not_a_match(self):
        """标题只做完全相等比对：前缀/模糊匹配会把同主题的另一篇误判成重复。"""
        fresh, dupes = self._split([
            _record("openalex:W666", doi="",
                    title="Gold catalysis annulation of ynamides revisited")])
        self.assertEqual(len(fresh), 1, "带后缀的另一篇不该被判为重复")
        self.assertEqual(dupes, [])


class IngestSkipsExistingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.db_path = f"{self.tmp.name}/ingest.db"
        self.db = connect(self.db_path)
        self.log_path = f"{self.tmp.name}/dsh.jsonl"
        reset()
        configure(path=self.log_path, to_stderr=False, mirror_to_db=False)
        upsert_paper(self.db, _record("seed:1", doi="10.1000/abc",
                                      title="Gold catalysis annulation"))
        self.db.commit()
        self.settings = Settings(db_path=self.db_path)

    def tearDown(self) -> None:
        self.db.close()
        configure(path=None, to_stderr=False)
        self.tmp.cleanup()

    def _run(self, records):
        api = _FakeApi(records)
        out = ingest_search_results(
            "gold catalysis", max_results=5, api=api, model=None,
            conn=self.db, settings=self.settings,
            topic_terms=["gold catalysis"])
        return out, api

    def test_existing_paper_is_skipped_before_download(self):
        """库里已有 → 不下载、不入库，并在报告里如实回报。"""
        out, api = self._run([_record("seed:1", doi="10.1000/abc",
                                      title="Gold catalysis annulation")])
        self.assertEqual(out["paper_keys"], [])
        self.assertEqual(out["duplicates"]["skipped"], 1)
        self.assertEqual(out["duplicates"]["by"], {"paper_key": 1})
        self.assertEqual(api.downloads, [],
                         "重复文献不该再触发 PDF 下载（这是省开销的关键）")

    def test_new_paper_is_still_processed(self):
        out, api = self._run([_record("new:1", doi="10.1000/new",
                                      title="Gold catalysis with new ligands")])
        self.assertEqual(out["paper_keys"], ["new:1"])
        self.assertEqual(api.downloads, ["new:1"])
        self.assertNotIn("duplicates", out)

    def test_dedup_can_be_disabled(self):
        """开关关掉后，重复文献按老行为照常处理（给"我要强制重跑"留出口）。"""
        self.settings.retrieval_skip_existing = False
        out, api = self._run([_record("seed:1", doi="10.1000/abc",
                                      title="Gold catalysis annulation")])
        self.assertEqual(api.downloads, ["seed:1"])
        self.assertNotIn("duplicates", out)

    def test_skipping_is_logged(self):
        """跳过必须留痕：否则"新增 0"在日志里看不出是"库里都有"还是"没抓到"。

        注意落点是 **`processing_log`**（`retrieval/node.py` 用的是 `db.log_event`），
        与它的邻居事件 `relevance-gate-dropped` / `paper-ingested` 一致，
        而不是统一 jsonl 事件日志——我第一版断言读错了文件。
        """
        self._run([_record("seed:1", doi="10.1000/abc",
                           title="Gold catalysis annulation")])
        row = self.db.execute(
            "SELECT details FROM processing_log WHERE node='retrieval' "
            "AND event='duplicates-skipped'").fetchone()
        self.assertIsNotNone(row, "应写一条 duplicates-skipped 记录")
        self.assertEqual(json.loads(row["details"])["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
