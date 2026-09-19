"""领域包/技能包加载层回归测试（v0.4.2）。

覆盖审计报告 `docs/HARDCODED_DOMAIN_CONTENT_AUDIT.md` 中"领域内容已外置"的验收：
- 领域判定、画像、种子类型、关系同义归一、机制词表、期刊分区全部来自 packs；
- 缺失包时**显式告警并返回空/缺省**，而不是回落到内联学科模板；
- 可通过 RA_PACKS_DIR / RA_JOURNAL_QUARTILES 覆盖。
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from research_agent import packs
from research_agent.domains import (
    EMPTY_PROFILE,
    infer_domain_kind,
    normalize_domain_profile,
)
from research_agent.ontology import store as ont
from research_agent.ontology.term_dictionaries import lookup_identity
from research_agent.quality.scoring import venue_factor
from research_agent.study.mechanism_lexicon import mechanism_lexicon
from research_agent.study.planner import infer_content_type, infer_task_kind
from tests._tmpdir import make_temp_dir


class PackLayerTest(unittest.TestCase):
    def test_builtin_packs_present(self):
        self.assertIn("chemistry", packs.available_domains())
        self.assertIn("general", packs.available_domains())
        for skill in ("journal-quartiles", "relation-lexicon",
                      "mechanism-keywords", "task-kind-hints"):
            self.assertIn(skill, packs.available_skills())
            self.assertTrue(packs.skill_data(skill), f"{skill} 数据为空")

    def test_domain_inference_from_packs(self):
        self.assertEqual(
            infer_domain_kind("", "提出一种炔酰胺合成多元氮杂环的新方法"),
            "chemistry")
        self.assertEqual(
            infer_domain_kind("", "战争胜利的伟力深藏在人民群众之中"),
            "humanities_social_science")
        # 未命中任何领域关键词时回落到声明 fallback 的领域
        self.assertEqual(infer_domain_kind("", "ZZZ 无关键词"), "general")

    def test_domain_profile_has_no_inline_template(self):
        profile = normalize_domain_profile(None, "", "炔酰胺环化")
        self.assertEqual(profile["domain_kind"], "chemistry")
        self.assertTrue(profile["dimensions"])
        self.assertIn("Reaction", profile["candidate_entity_types"])

    def test_seed_types_merge_core_and_domain(self):
        nodes = dict((k, v) for k, v in ont.seeded_node_types())
        relations = dict((k, v) for k, v in ont.seeded_relation_types())
        self.assertIn("Method", nodes)              # 核心种子
        self.assertIn("Catalyst", nodes)            # 化学领域包追加
        self.assertIn("uses", relations)            # 核心关系
        self.assertIn("affords", relations)         # 化学领域包追加

    def test_relation_synonyms_come_from_pack(self):
        self.assertEqual(ont.canonical_relation_type("utilizes"), "uses")
        self.assertEqual(ont.canonical_relation_type("novel_link"), "novel_link")

    def test_strong_relations_from_pack(self):
        strong = ont.strong_relation_set()
        self.assertIn("promotes", strong)
        self.assertNotIn("uses", strong)

    def test_mechanism_lexicon_merges_domain_vocab(self):
        chem = mechanism_lexicon("chemistry")
        hss = mechanism_lexicon("humanities_social_science")
        self.assertTrue(chem["mechanism_keywords"])
        self.assertTrue(chem["activation_rules"], "化学领域应有活化规则")
        # 人文学科没有化学机制增补：规则为空但不应崩溃，且已告警
        self.assertEqual(hss["activation_rules"], [])

    def test_journal_quartiles_and_scores(self):
        quartiles = packs.journal_quartiles()
        self.assertTrue(quartiles)
        scores = packs.quartile_scores()
        self.assertIn("Q1", scores)
        self.assertGreater(scores["Q1"], scores["Q2"])

    def test_unknown_journal_is_neutral_not_penalized(self):
        # 用确定不会被任何分区表命中的名字（避免 .env 里的全量 JCR 表干扰）
        factor, quartile, _ = venue_factor(
            {"venue": "Zzz Nonexistent Journal Of Nothing",
             "source_type": "journal"})
        self.assertIsNone(quartile)
        self.assertGreaterEqual(factor, 0.5)

    def test_jcr_seeded_chemistry_journals_resolve(self):
        """P0-6 验收：内置 JCR 化学子集能让本领域期刊拿到分区。"""
        for venue, expected in (
                ("Journal of the American Chemical Society", "Q1"),
                ("Angewandte Chemie (International ed. in English)", "Q1"),
                ("Organic Letters", "Q1"),
                ("Chemical Science", "Q1"),
                ("ACS Catalysis", "Q1")):
            factor, quartile, _ = venue_factor(
                {"venue": venue, "source_type": "journal"})
            self.assertEqual(quartile, expected,
                             f"{venue} 未命中分区表（P0-6 回归）")
            self.assertGreaterEqual(factor, 0.8)

    def test_planner_hints_from_pack(self):
        self.assertEqual(infer_task_kind("写一份 RAG 综述"), "summary")
        self.assertEqual(infer_task_kind("提出一种新方法"), "generative")
        self.assertEqual(infer_content_type("写一份前沿综述"), "frontier_review")
        self.assertEqual(infer_content_type("设计一个实验方案"),
                         "experiment_protocol")

    def test_domain_dictionary_from_pack(self):
        hit = lookup_identity("H2O", node_type="Chemical")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["external_id"], "CHEBI:15377")
        self.assertIsNone(lookup_identity("完全不存在的术语XYZ"))


class RelevanceGatePolicyTest(unittest.TestCase):
    """P1-6 策略 C：词元不可判定 / 中文主题 + 英文语料时不整批误杀。"""

    def setUp(self):
        from research_agent.retrieval.node import apply_topic_relevance_gate

        self.gate = apply_topic_relevance_gate
        self.records = [
            {"paper_key": "a", "title": "Ynamide annulation to azacycles",
             "abstract": "Copper-catalyzed annulation of ynamides."},
            {"paper_key": "b", "title": "Gold catalysis of ynamides",
             "abstract": "Gold carbene intermediates."},
        ]

    def test_abbreviation_topic_is_tokenized(self):
        from research_agent.retrieval.node import _topic_tokens

        self.assertEqual(_topic_tokens(["RAG"]), {"rag"})
        self.assertEqual(_topic_tokens(["DNA repair"]), {"dna", "repair"})

    def test_abbreviation_topic_gate_binds(self):
        """缩写主题不再因"切不出 token"而整批放行。"""
        kept, dropped = self.gate(
            [{"paper_key": "x", "title": "RAG for question answering",
              "abstract": "Retrieval augmented generation improves QA."},
             {"paper_key": "y", "title": "Liposomal doxorubicin in mice",
              "abstract": "Stealth liposomes avoid the reticuloendothelial system."}],
            ["RAG"], on_low_signal="warn")
        self.assertEqual([r["paper_key"] for r in kept], ["x"])
        self.assertEqual([r["paper_key"] for r in dropped], ["y"])

    def test_cjk_topic_with_english_corpus_warns_not_drops(self):
        kept, dropped = self.gate(self.records, ["炔酰胺"], on_low_signal="warn")
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])
        self.assertTrue(all(r.get("_gate_low_signal") ==
                            "cjk_topic_english_corpus" for r in kept))

    def test_cjk_topic_drop_policy_keeps_old_behaviour(self):
        kept, dropped = self.gate(self.records, ["炔酰胺"], on_low_signal="drop")
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 2)

    def test_domain_language_records_still_filtered(self):
        """中文主题 + 中文语料时按正常词元判定（不应被低信号豁免）。"""
        records = [
            {"paper_key": "cn1", "title": "炔酰胺合成多元氮杂环的新方法",
             "abstract": "本文报道炔酰胺参与的环化反应与选择性控制。"},
            {"paper_key": "cn2", "title": "脂质体药物递送系统研究",
             "abstract": "本文研究脂质体的体内分布与药代动力学行为。"},
        ]
        kept, dropped = self.gate(records, ["炔酰胺"], on_low_signal="warn")
        self.assertEqual([r["paper_key"] for r in kept], ["cn1"])
        self.assertEqual([r["paper_key"] for r in dropped], ["cn2"])


class PackOverrideTest(unittest.TestCase):
    """缺包 / 自定义包的行为：显式降级，不静默回落到代码内模板。"""

    def setUp(self):
        self.tmp = make_temp_dir()
        self._saved = {k: os.environ.get(k)
                       for k in ("RA_PACKS_DIR", "RA_JOURNAL_QUARTILES")}
        packs.reset_cache()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        packs.reset_cache()
        self.tmp.cleanup()

    def _write_pack(self, root: Path, domain: str, payload: dict) -> None:
        target = root / "domains" / domain
        target.mkdir(parents=True, exist_ok=True)
        (target / "domain.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_custom_domain_pack_is_used(self):
        root = Path(self.tmp.name) / "packs"
        self._write_pack(root, "quantum", {
            "kind": "quantum",
            "keywords": ["qubit", "量子比特"],
            "profile": {"label": "量子信息", "dimensions": ["相干性"],
                        "candidate_entity_types": ["Qubit"],
                        "candidate_relation_types": ["couples_to"]},
        })
        os.environ["RA_PACKS_DIR"] = str(root)
        packs.reset_cache()
        self.assertIn("quantum", packs.available_domains())
        self.assertEqual(infer_domain_kind("", "研究 qubit 的相干时间"), "quantum")
        profile = normalize_domain_profile(None, "", "qubit 相干性")
        self.assertEqual(profile["label"], "量子信息")
        self.assertEqual(profile["candidate_relation_types"], ["couples_to"])

    def test_empty_pack_dir_degrades_explicitly(self):
        os.environ["RA_PACKS_DIR"] = str(Path(self.tmp.name) / "empty")
        os.environ["RA_PACKS_FALLBACK"] = ""
        packs.reset_cache()
        self.assertEqual(packs.available_domains(), [])
        self.assertIsNone(packs.domain_dir("chemistry"))
        profile = normalize_domain_profile(None, "", "任意请求")
        self.assertEqual(profile["domain_kind"], "general")
        # 空包下不应崩溃，且画像退化为空结构而不是某个学科模板
        self.assertIsInstance(profile["dimensions"], list)
        self.assertTrue(set(profile["candidate_entity_types"]) <=
                        set(EMPTY_PROFILE["candidate_entity_types"] or [])
                        or profile["candidate_entity_types"] == [])

    def test_journal_override_file(self):
        # 先屏蔽 .env 里可能存在的全量 JCR 表，确保验证的是"覆盖文件"生效
        os.environ.pop("RA_JOURNAL_QUARTILES", None)
        path = Path(self.tmp.name) / "jq.json"
        path.write_text(json.dumps({
            "journal of the american chemical society": "Q1"}), encoding="utf-8")
        os.environ["RA_JOURNAL_QUARTILES"] = str(path)
        packs.reset_cache()
        self.assertEqual(
            packs.journal_quartile("Journal of the American Chemical Society"),
            "Q1")
        factor, quartile, _ = venue_factor({
            "venue": "Journal of the American Chemical Society",
            "source_type": "journal"})
        self.assertEqual(quartile, "Q1")
        self.assertAlmostEqual(factor, 0.95, places=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
