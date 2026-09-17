"""看板 REST API 测试（离线，使用临时库）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from research_agent.db import connect, log_event, save_quality_result, upsert_paper
from research_agent.ontology import store as ont
from tests._tmpdir import make_temp_dir


def seed_db(path: Path) -> None:
    conn = connect(path)
    ont.init_ontology(conn)
    rec = {
        "paper_key": "arxiv:9999.00001",
        "source": "arxiv", "title": "A Graph Neural Network Benchmark",
        "abstract": "We benchmark GNNs.", "doi": "10.48550/arXiv.9999.00001",
        "venue": "Nature", "venue_issn": "0028-0836", "source_type": "journal",
        "pub_year": 2024, "pub_date": "2024-01-01",
        "publication_status": "Published", "citation_count": 300,
        "avg_h_index": 30.0,
        "authors": [{"name": "Ada Lovelace", "affiliations": ["Meta AI"]}],
        "pdf_blob": b"%PDF-1.4 fake", "pdf_size": 12,
        "clean_text": "We benchmark graph neural networks on Open Graph Benchmark.",
        "status": "ingested",
    }
    upsert_paper(conn, rec)
    save_quality_result(conn, {
        "paper_key": rec["paper_key"], "authority": 0.9, "timeliness": 0.9,
        "quality": 0.9, "decision": "knowledge", "needs_review": False,
        "rationale": "high quality", "meta_missing": [],
        "venue_factor": 0.95, "h_factor": 0.7, "citation_factor": 0.9,
    })
    n1, _ = ont.upsert_node(conn, node_type="Method", name="GNN", confidence=0.95,
                            attributes={"kind": "model"},
                            provenance=[{"paper": rec["paper_key"], "evidence": "bench"}] )
    n2, _ = ont.upsert_node(conn, node_type="Dataset", name="OGB", confidence=0.8)
    ont.upsert_edge(conn, relation_type="evaluates", src_id=n1, tgt_id=n2,
                    confidence=0.9)
    ont.upsert_hyperedge(
        conn, hyperedge_type="evaluation", label="GNN evaluation",
        members=[{"node_id": n1, "role": "method"},
                 {"node_id": n2, "role": "dataset"}],
        confidence=0.9, paper_key=rec["paper_key"],
        provenance=[{"paper": rec["paper_key"], "evidence": "GNN evaluated on OGB"}])
    ont.record_ontology_run(conn, rec["paper_key"],
                            {"entities": 2, "relations": 1}, ["Experiment"])
    log_event(conn, "retrieval", "paper-ingested", rec["paper_key"], {"ok": 1})
    log_event(conn, "quality", "assessed", rec["paper_key"], {"Q": 0.9})
    log_event(conn, "knowledge", "extracted", rec["paper_key"], {"n": 2})
    conn.close()


class DashboardApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.db = Path(self.tmp.name) / "dash.db"
        seed_db(self.db)
        from research_agent.dashboard.app import create_app

        self.client = TestClient(create_app(self.db, inject_llms=False))

    def tearDown(self):
        self.tmp.cleanup()

    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_overview(self):
        r = self.client.get("/api/overview")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["papers"], 1)
        self.assertEqual(data["quality_decisions"]["knowledge"], 1)
        self.assertEqual(data["ontology"]["nodes"], 2)
        self.assertEqual(data["ontology"]["edges"], 1)

    def test_papers_and_detail(self):
        papers = self.client.get("/api/papers").json()
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["extracted"]["entities"], 2)
        detail = self.client.get("/api/papers/arxiv:9999.00001").json()
        self.assertEqual(detail["quality"]["decision"], "knowledge")
        self.assertIn("graph neural network", detail["paper"]["clean_preview"].lower())
        self.assertEqual(len(detail["logs"]), 3)

    def test_agents(self):
        data = self.client.get("/api/agents").json()
        agents = {a["id"]: a for a in data["agents"]}
        self.assertGreaterEqual(agents["retrieval"]["count"], 1)
        self.assertGreaterEqual(agents["quality"]["count"], 1)
        self.assertGreaterEqual(agents["knowledge"]["count"], 1)

    def test_study_status_idle(self):
        data = self.client.get("/api/study/status").json()
        self.assertEqual(data["status"], "idle")
        self.assertIn("nodes", data)
        self.assertIn("summary", data)

    def test_logs(self):
        logs = self.client.get("/api/logs?limit=10").json()
        self.assertEqual(len(logs), 3)

    def test_ontology_graph_and_node(self):
        g = self.client.get("/api/ontology").json()
        self.assertEqual(g["total"], 2)
        self.assertEqual(len(g["edges"]), 1)
        self.assertIn("hyperedges", g)
        self.assertIn("domains", g)
        self.assertIn("channels", g)
        self.assertGreaterEqual(len(g["hyperedges"]), 1)
        node_id = g["nodes"][0]["id"]
        nd = self.client.get(f"/api/ontology/nodes/{node_id}").json()
        self.assertIn("node", nd)
        self.assertGreaterEqual(len(nd["neighbors"]), 0)

    def test_index_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("research-agent", r.text)

    def test_status_nodes_and_filtered_logs(self):
        data = self.client.get("/api/status/nodes").json()
        ids = {n["id"] for n in data["nodes"]}
        self.assertIn("retrieval", ids)
        self.assertIn("quality", ids)
        self.assertIn("knowledge", ids)
        self.assertIn("planner", ids)
        logs = self.client.get("/api/logs?node=quality").json()
        self.assertEqual(len(logs), 1)

    def test_planner_interaction(self):
        r = self.client.post(
            "/api/planner/run",
            json={"request": "提出一个新的数据分析框架"},
        )
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["plan"]["goal"], "提出一个新的数据分析框架")
        logs = self.client.get("/api/logs?node=study&event=planner").json()
        self.assertGreaterEqual(len(logs), 1)

    def test_database_switch_endpoint(self):
        r = self.client.post("/api/db/select", json={"path": str(self.db)})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        health = self.client.get("/api/health").json()
        self.assertEqual(health["db"], str(self.db))

    def test_human_review_pending_and_submit(self):
        conn = connect(self.db)
        conn.execute(
            "UPDATE papers SET status='human_review' "
            "WHERE paper_key='arxiv:9999.00001'")
        conn.execute(
            "UPDATE quality_results SET decision='human', needs_review=1 "
            "WHERE paper_key='arxiv:9999.00001'")
        conn.commit()
        conn.close()
        pending = self.client.get("/api/reviews").json()["pending"]
        self.assertEqual(len(pending), 1)
        r = self.client.post("/api/reviews/submit", json={
            "paper_key": "arxiv:9999.00001",
            "action": "reject",
            "rationale": "数据与方法不适用于目标领域",
            "custom_result": {"owner": "reviewer-a"},
        })
        self.assertTrue(r.json()["ok"])
        history = self.client.get("/api/reviews?history=true").json()["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["decision"], "rejected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
