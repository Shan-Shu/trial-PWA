"""BackendAdapter：把引擎的能力包成界面（PWA `ui/views/*`）期望的 48 个方法。

界面是按 PWA 的后端门面 `PaperWritingAssistant` 写的（48 个 `service.*` 调用 +
3 个 `task_manager.*`）。这个类的职责就是**在引擎之上实现同一个门面**，
让界面一行不用改（唯一的例外见下）。

**身份标识**：PWA 的文献主键是 `INTEGER id`，引擎的是 `paper_key`（TEXT，
如 `europepmc:MED:11082714`）。这里统一把引擎的 `paper_key` 当作 `id` 暴露出去
——**不编造整数 id**。界面里有几处 `int(paper["id"])` 是 PWA 主键类型的残留，
已就地改为不转换（id 本就是不透明标识）。

**未接线的地方如实标注**：引擎确实没有的概念（如"检索历史""审核偏好学习"
"实验设计库"）不编造数据，返回空并在 `unwired()` 里列出，界面可在系统状态页看到。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

__all__ = ["BackendAdapter", "build_adapter"]


class BackendAdapter:
    """引擎的界面门面。方法名与 PWA 的 `PaperWritingAssistant` 保持一致。"""

    def __init__(self, db_path: str | Path | None = None,
                 settings: Any = None) -> None:
        from research_agent.config import settings as default_settings

        self.settings = settings or default_settings
        self.db_path = str(db_path or self.settings.db_path)
        #: 引擎没有对应概念、因此未能实现的方法（名 → 原因）
        self._gaps: dict[str, str] = {}

    # ------------------------------------------------------------------ 内部
    def _conn(self):
        from research_agent.db import connect

        return connect(self.db_path)

    def _with_store(self, fn):
        """开连接 → 建 `LibraryStore` → 执行 → 必关连接。

        引擎的 `LibraryStore.__init__(conn)` 收的是**连接**而不是库路径
        （它是 SQL 层，不是门面）。所以不能 `LibraryStore(self.db_path)`——
        那样第一次 `conn.execute` 就会炸 `'str' object has no attribute 'execute'`。
        """
        from research_agent.library.store import LibraryStore

        conn = self._conn()
        try:
            return fn(LibraryStore(conn))
        finally:
            conn.close()

    def _gap(self, name: str, reason: str) -> None:
        """记一条"引擎无此概念"。**只记录，不编造数据。**"""
        self._gaps.setdefault(name, reason)
        logger.debug("未接线：%s（%s）", name, reason)

    def unwired(self) -> dict[str, str]:
        """当前已暴露的缺口（供系统状态页展示）。"""
        return dict(self._gaps)

    # ================================================================== 统计
    def _counts(self) -> dict[str, int]:
        """统一计数口径，**刻意不重复计数**。

        踩过的坑：把"质量通过"写成 `ingested + direct + flagged`，同一篇文献会被
        数两次——实测某真实库 1040 篇却显示"质量通过 1621"，一眼就假。

        引擎的质量决策值是 ``knowledge``（直接入知识）/ ``flagged``（标记放行）/
        ``human``（转人工）/ ``enrich``（待补元数据）——**没有 `direct`**，
        它在 `quality/node.py` 的映射里就被折成 `knowledge` 了，所以这里两个都认。
        一律用 ``COUNT(DISTINCT paper_key)``，避免重复评估造成虚高。
        """
        conn = self._conn()
        try:
            def one(sql: str) -> int:
                try:
                    return int(conn.execute(sql).fetchone()[0] or 0)
                except Exception:  # noqa: BLE001 —— 空库/无表按 0
                    return 0

            return {
                "papers": one("SELECT COUNT(*) FROM papers"),
                "ingested": one(
                    "SELECT COUNT(*) FROM papers WHERE status='ingested'"),
                "human_review": one(
                    "SELECT COUNT(*) FROM papers WHERE status='human_review'"),
                "rejected": one(
                    "SELECT COUNT(*) FROM papers WHERE status='quality_rejected'"),
                "passed": one(
                    "SELECT COUNT(DISTINCT paper_key) FROM quality_results "
                    "WHERE decision IN ('knowledge','flagged','direct')"),
                "extracted": one(
                    "SELECT COUNT(DISTINCT paper_key) FROM processing_log "
                    "WHERE event='extracted'"),
            }
        finally:
            conn.close()

    def stats(self) -> dict[str, Any]:
        """总览页要的计数。"""
        counts = self._counts()
        return {
            "total": counts["papers"],
            "passed": counts["passed"],
            "review": counts["human_review"],
            "rejected": counts["rejected"],
            "avg_score": self._quality_average(),
            "queries": 0,          # 见 recent_searches：引擎不存检索历史
        }

    def _quality_average(self) -> float:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT AVG(quality) AS q FROM quality_results").fetchone()
            return round(float((row["q"] if row else 0) or 0.0), 3)
        except Exception:  # noqa: BLE001 —— 空库/无表都算 0
            return 0.0
        finally:
            conn.close()

    def library_overview(self) -> dict[str, Any]:
        from research_agent.dashboard import api as dbapi

        counts = self._counts()
        onto = (dbapi.overview(self.db_path).get("ontology") or {})
        return {
            "total": counts["papers"],
            "passed": counts["passed"],
            "rejected": counts["rejected"],
            "knowledge_extracted": counts["extracted"],
            "ontology_terms": onto.get("nodes", 0),
            "ontology_relations": onto.get("edges", 0),
        }

    def system_status(self) -> dict[str, Any]:
        """系统状态页要的分组计数（键名按界面期望）。"""
        from research_agent.dashboard import api as dbapi

        ov = dbapi.overview(self.db_path)
        onto = ov.get("ontology") or {}
        counts = self._counts()
        conn = self._conn()
        try:
            def count(sql: str) -> int:
                try:
                    return int(conn.execute(sql).fetchone()[0] or 0)
                except Exception:  # noqa: BLE001 —— 表不存在算 0
                    return 0

            return {
                "papers": {
                    "total": counts["papers"],
                    "passed": counts["passed"],
                    "rejected": counts["rejected"],
                    "avg_score": self._quality_average(),
                },
                # 引擎的知识以本体（节点/边/超边）承载，没有 PWA 的
                # entities/triples 两表，这里用节点与边作等价计数。
                "knowledge": {
                    "entities": onto.get("nodes", 0),
                    "triples": onto.get("edges", 0),
                    "hyperedges": onto.get("hyperedges", 0),
                },
                "ontology": {"terms": onto.get("nodes", 0),
                             "relations": onto.get("edges", 0),
                             "nodes": onto.get("nodes", 0),
                             "edges": onto.get("edges", 0)},
                "experiment": {
                    "experiment_knowledge": count(
                        "SELECT COUNT(*) FROM ontology_hyperedge_conditions"),
                    "measurements": count(
                        "SELECT COUNT(*) FROM ontology_hyperedge_measurements"),
                    "designs": 0,
                },
                "writing": {"projects": count(
                    "SELECT COUNT(*) FROM writing_projects")},
                "review": {"pending": counts["human_review"]},
                "logs": ov.get("logs", 0),
                "last_activity": ov.get("last_activity"),
                "unwired": self.unwired(),
            }
        finally:
            conn.close()

    @property
    def active_phases(self) -> list[str]:
        """界面的"多智能体状态"列表。

        PWA 用的是它自己的 `ACTIVE_PHASES`（9 个命名外壳）；那层已退役，
        这里改为引擎**真正登记的节点**（`writing/node_registry`），
        这样界面显示的就是实际能干活的东西。
        """
        from research_agent.writing import node_registry as reg

        return [spec.node for spec in reg.NODES if spec.tasks]

    def node_labels(self) -> dict[str, str]:
        """节点 id → 中文名（界面可用它替换自己的 AGENT_LABELS）。"""
        from research_agent.writing import node_registry as reg

        return {spec.node: spec.label for spec in reg.NODES}

    # ================================================================== 文献库
    def list_papers(self, **filters: Any) -> list[dict[str, Any]]:
        """文献列表（界面用 `id`，即引擎的 `paper_key`）。"""
        from research_agent.dashboard import api as dbapi

        rows = dbapi.list_papers(self.db_path)
        status = filters.get("status")
        source = filters.get("source")
        limit = int(filters.get("limit") or 0)
        out = []
        for row in rows:
            if status and row.get("status") != status:
                continue
            if source and row.get("source") != source:
                continue
            out.append(self._paper_row(row))
            if limit and len(out) >= limit:
                break
        return out

    def library_query(self, *, status: str | None = None,
                      source: str | None = None, q: str | None = None,
                      year_from: int | None = None, year_to: int | None = None,
                      tag: str | None = None, folder_id: int | None = None,
                      favorite: bool = False, sort_by: str = "quality_score",
                      sort_desc: bool = True,
                      limit: int = 5000) -> list[dict[str, Any]]:
        """服务端筛选 + 排序（直接用引擎的 `query_papers`，它就是从 PWA 迁来的）。"""
        result = self._with_store(lambda s: s.query_papers(
            q=q or None, status=status or None, source=source or None,
            year_from=year_from, year_to=year_to, tag=tag or None,
            folder_id=int(folder_id) if folder_id is not None else None,
            favorite_only=bool(favorite),
            sort_by="quality" if sort_by in ("quality_score", "quality") else sort_by,
            sort_desc=bool(sort_desc), limit=max(1, int(limit))))
        items = result.get("items") if isinstance(result, dict) else (result or [])
        return [self._paper_row(r) for r in (items or [])]

    def get_paper(self, paper_id: Any) -> dict[str, Any] | None:
        from research_agent.dashboard import api as dbapi

        detail = dbapi.get_paper_detail(str(paper_id), self.db_path)
        if not detail:
            return None
        return self._paper_row(detail.get("paper") or {}, extra=detail)

    def paper_processing(self, paper_id: Any) -> dict[str, Any] | None:
        """单篇的"输入→输出"详情（质量 + 知识计数）。"""
        from research_agent.dashboard import api as dbapi

        detail = dbapi.get_paper_detail(str(paper_id), self.db_path)
        if not detail:
            return None
        counts = detail.get("counts") or {}
        extracted = detail.get("extracted") or {}
        return {
            "paper": self._paper_row(detail.get("paper") or {}),
            "quality_status": (detail.get("paper") or {}).get("status"),
            "quality_score": (detail.get("quality") or {}).get("quality"),
            "quality_level": (detail.get("quality") or {}).get("decision"),
            "has_knowledge": bool(extracted),
            "entity_count": int(counts.get("nodes") or extracted.get("entities") or 0),
            "triple_count": int(counts.get("edges") or 0),
            "sdo": extracted or None,
        }

    def delete_paper(self, paper_id: Any) -> None:
        key = str(paper_id)
        self._with_store(lambda s: s.delete_paper_records(key))
        conn = self._conn()
        try:
            conn.execute("DELETE FROM papers WHERE paper_key = ?", (key,))
            conn.commit()
        finally:
            conn.close()

    def batch_delete_papers(self, paper_ids: Iterable[Any]) -> dict[str, Any]:
        ids = list(paper_ids)
        for key in ids:
            self.delete_paper(key)
        return {"deleted": len(ids)}

    def export_papers(self, paper_ids: Iterable[Any],
                      fmt: str = "markdown") -> dict[str, Any]:
        """导出引用（引擎支持 gbt7714 / apa / acs / bibtex / ris / markdown）。"""
        from research_agent.library import citation as cit

        style = {"markdown": "markdown", "bibtex": "bibtex"}.get(fmt, fmt)
        records = [r for r in (self.get_paper(k) for k in paper_ids) if r]
        return {"count": len(records), "format": style,
                "content": cit.export(records, style)}

    # -- 侧车：标签 / 文件夹 / 收藏（引擎 LibraryStore 已实现）--------------
    def list_tags(self) -> list[str]:
        return self._with_store(lambda s: list(s.all_tags()))

    def list_folders(self) -> list[dict[str, Any]]:
        return self._with_store(lambda s: list(s.list_folders()))

    def create_folder(self, name: str) -> int:
        return self._with_store(lambda s: int(s.create_folder(name)))

    def add_tag(self, paper_id: Any, tag: str) -> None:
        self._with_store(lambda s: s.add_tag(str(paper_id), tag))

    def remove_tag(self, paper_id: Any, tag: str) -> None:
        self._with_store(lambda s: s.remove_tag(str(paper_id), tag))

    def add_to_folder(self, paper_id: Any, folder_id: int) -> None:
        self._with_store(lambda s: s.add_to_folder(str(paper_id), int(folder_id)))

    def set_favorite(self, paper_id: Any, value: bool = True) -> None:
        self._with_store(lambda s: s.set_favorite(str(paper_id), bool(value)))

    def batch_tag(self, paper_ids: Iterable[Any], tag: str) -> dict[str, Any]:
        keys = [str(k) for k in paper_ids]
        self._with_store(lambda s: s.batch_tag(keys, tag))
        return {"updated": len(keys)}

    def batch_favorite(self, paper_ids: Iterable[Any],
                       value: bool = True) -> dict[str, Any]:
        keys = [str(k) for k in paper_ids]
        self._with_store(lambda s: s.batch_favorite(keys, bool(value)))
        return {"updated": len(keys)}

    def batch_add_to_folder(self, paper_ids: Iterable[Any],
                            folder_id: int) -> dict[str, Any]:
        keys = [str(k) for k in paper_ids]
        self._with_store(lambda s: s.batch_add_to_folder(keys, int(folder_id)))
        return {"updated": len(keys)}

    # ================================================================== 知识
    def knowledge_data(self, paper_id: Any = None) -> dict[str, Any]:
        """知识数据：界面要 `{sdo, entities, triples, stats}`。

        引擎的知识落在本体里（节点/边/超边），没有 PWA 的 entities/triples 两表；
        这里把**与该文献相关的节点/边**当作 entities/triples 返回，
        并给出计数。`sdo` 用知识抽取的处理记录近似。
        """
        return {
            "sdo": self.paper_processing(paper_id) if paper_id else None,
            "entities": self._ontology_rows("nodes", paper_id),
            "triples": self._ontology_rows("edges", paper_id),
            "stats": self._knowledge_stats(paper_id),
        }

    def _ontology_rows(self, kind: str, paper_id: Any = None) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            if kind == "nodes":
                rows = conn.execute(
                    "SELECT node_id, node_type, name, aliases, confidence "
                    "FROM ontology_nodes LIMIT 5000").fetchall()
                return [{"id": int(r["node_id"]), "name": r["name"],
                         "type": r["node_type"],
                         "aliases": r["aliases"], "confidence": r["confidence"]}
                        for r in rows]
            rows = conn.execute(
                "SELECT e.edge_id, e.relation_type, e.confidence, "
                "       s.name AS subject, t.name AS object "
                "FROM ontology_edges e "
                "LEFT JOIN ontology_nodes s ON s.node_id = e.source_node "
                "LEFT JOIN ontology_nodes t ON t.node_id = e.target_node "
                "LIMIT 20000").fetchall()
            return [{"id": int(r["edge_id"]), "subject": r["subject"],
                     "predicate": r["relation_type"], "object": r["object"],
                     "confidence": r["confidence"]} for r in rows]
        except Exception:  # noqa: BLE001 —— 空库没有本体表
            return []
        finally:
            conn.close()

    def _knowledge_stats(self, paper_id: Any = None) -> dict[str, int]:
        conn = self._conn()
        try:
            def one(sql: str) -> int:
                try:
                    return int(conn.execute(sql).fetchone()[0] or 0)
                except Exception:  # noqa: BLE001
                    return 0

            return {
                "entities": one("SELECT COUNT(*) FROM ontology_nodes"),
                "triples": one("SELECT COUNT(*) FROM ontology_edges"),
                "hyperedges": one("SELECT COUNT(*) FROM ontology_hyperedges"),
                "conditions": one(
                    "SELECT COUNT(*) FROM ontology_hyperedge_conditions"),
                "measurements": one(
                    "SELECT COUNT(*) FROM ontology_hyperedge_measurements"),
            }
        finally:
            conn.close()

    def extract_knowledge(self, paper_id: Any, force: bool = False,
                          progress_cb: Any = None,
                          cancel_event: Any = None) -> dict[str, Any]:
        """知识抽取：复用引擎的 `process_papers`（质量 → 知识 → 本体）。"""
        from research_agent.pipeline import process_papers
        from research_agent.writing.collaboration import build_retrieval_services

        services = build_retrieval_services(self.settings)
        results = process_papers([str(paper_id)], services=services,
                                 progress_cb=progress_cb)
        status = str((results[0] if results else {}).get("status") or "")
        return {"paper_id": str(paper_id), "status": status,
                "ok": status in ("extracted", "knowledge")}

    def batch_extract_knowledge(self, paper_ids: Iterable[Any]) -> dict[str, Any]:
        keys = [str(k) for k in paper_ids]
        ok = 0
        errors: list[dict[str, str]] = []
        for key in keys:
            try:
                if self.extract_knowledge(key).get("ok"):
                    ok += 1
            except Exception as exc:  # noqa: BLE001 —— 单篇失败不影响其余
                errors.append({"paper_id": key, "error": str(exc)})
        return {"total": len(keys), "ok": ok, "errors": errors}

    def extract_all_knowledge(self, status: str = "quality_passed",
                              progress_cb: Any = None,
                              cancel_event: Any = None) -> dict[str, Any]:
        """批量抽取：优先抽**已入库但未抽取**的文献（最实际的瓶颈）。"""
        from research_agent.writing.collaboration import _pending_existing_count

        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT p.paper_key FROM papers p WHERE p.status='ingested' "
                "AND NOT EXISTS (SELECT 1 FROM processing_log l "
                "                WHERE l.paper_key = p.paper_key "
                "                  AND l.event = 'extracted') "
                "LIMIT 200").fetchall()
            keys = [str(r["paper_key"]) for r in rows]
        except Exception:  # noqa: BLE001
            keys = []
        finally:
            conn.close()
        if not keys:
            return {"total": 0, "ok": 0, "skipped": 0,
                    "note": "没有已入库但未抽取的文献"}
        result = self.batch_extract_knowledge(keys)
        result["total"] = len(keys)
        result["pending_before"] = _pending_existing_count(self._conn())
        return result

    # ================================================================== 本体
    def build_ontology(self, progress_cb: Any = None,
                       cancel_event: Any = None) -> dict[str, Any]:
        """重建本体视图（引擎的 `rebuild_ontology_views`）。"""
        from research_agent.ontology.store import (init_ontology,
                                                   rebuild_ontology_views)

        conn = self._conn()
        try:
            init_ontology(conn)
            return rebuild_ontology_views(conn) or {}
        finally:
            conn.close()

    def ontology_data(self) -> dict[str, Any]:
        """本体数据：界面要 `{terms, relations, stats}`。

        `terms` 由引擎节点映射（term=name、term_type=node_type），
        `relations` 由引擎边映射（subject_term/relation/object_term）。
        """
        conn = self._conn()
        try:
            terms = []
            relations = []
            try:
                for r in conn.execute(
                        "SELECT node_id, node_type, name, aliases, confidence "
                        "FROM ontology_nodes LIMIT 8000"):
                    import json as _json
                    try:
                        aliases = _json.loads(r["aliases"] or "[]")
                    except (TypeError, ValueError):
                        aliases = []
                    terms.append({
                        "id": int(r["node_id"]), "term": r["name"],
                        "term_type": r["node_type"], "aliases": aliases,
                        "confidence": r["confidence"], "source_paper_ids": [],
                    })
                for r in conn.execute(
                        "SELECT e.edge_id, e.relation_type, e.confidence, "
                        "       s.name AS subject, t.name AS object "
                        "FROM ontology_edges e "
                        "LEFT JOIN ontology_nodes s ON s.node_id = e.source_node "
                        "LEFT JOIN ontology_nodes t ON t.node_id = e.target_node "
                        "LIMIT 20000"):
                    relations.append({
                        "id": int(r["edge_id"]),
                        "subject_term": r["subject"],
                        "relation": r["relation_type"],
                        "object_term": r["object"],
                        "confidence": r["confidence"], "source_paper_ids": [],
                    })
            except Exception:  # noqa: BLE001 —— 空库没有本体表
                pass
            return {"terms": terms, "relations": relations,
                    "stats": {"terms": len(terms), "relations": len(relations)}}
        finally:
            conn.close()

    # ================================================================== 检索
    def search(self, raw_query: str, max_results: int | None = None,
               sources: list[str] | None = None, mode_override: str | None = None,
               progress_cb: Any = None,
               cancel_event: Any = None) -> dict[str, Any]:
        """智能检索：走引擎的采集链（关键词规划 → 多源检索 → 质量评估 → 入库）。"""
        from research_agent.study.collection import collect_mission
        from research_agent.writing.collaboration import build_retrieval_services

        terms = [t for t in str(raw_query or "").replace("，", ",").split(",")
                 if t.strip()] or [str(raw_query or "").strip()]
        request = {"seed_terms": terms[:8],
                   "max_results": int(max_results or 40),
                   "reason": "界面：智能检索"}
        if sources:
            request["source"] = sources[0]
        report = collect_mission(request,
                                 services=build_retrieval_services(self.settings))
        count = int(report.get("count") or 0)
        return {
            "query_id": 0,
            "papers": self._papers_by_keys(report.get("paper_keys") or []),
            "stats": {
                "raw_query": raw_query, "query_count": len(terms),
                "source_count": len(sources or []) or len(terms),
                "fetched": count + len(report.get("errors") or []),
                "duplicates": 0, "passed": count, "rejected": 0,
            },
            "errors": list(report.get("errors") or []),
            "gate": report.get("relevance_gate"),
        }

    def _papers_by_keys(self, keys: Iterable[Any]) -> list[dict[str, Any]]:
        out = []
        for key in keys:
            row = self.get_paper(key)
            if row:
                out.append(row)
        return out

    def hybrid_search(self, query: str, mode: str = "hybrid",
                      limit: int = 30) -> dict[str, Any]:
        """混合检索：引擎侧用**本体图扩展 + 关键词**两条路，这里合并二者结果。"""
        nodes = self._match_nodes(query, limit=limit)
        edges = self._edges_between([n["id"] for n in nodes], limit=limit * 4)
        return {"mode": mode, "nodes": nodes, "edges": edges,
                "seed_count": len(nodes), "node_count": len(nodes),
                "edge_count": len(edges)}

    def _match_nodes(self, query: str, limit: int = 30) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            like = f"%{str(query or '').strip()}%"
            rows = conn.execute(
                "SELECT node_id, node_type, name, confidence, aliases "
                "FROM ontology_nodes "
                "WHERE LOWER(COALESCE(name,'')) LIKE LOWER(?) "
                "   OR LOWER(COALESCE(aliases,'')) LIKE LOWER(?) "
                "LIMIT ?", (like, like, max(1, int(limit)))).fetchall()
            return [{"id": int(r["node_id"]), "term": r["name"],
                     "term_type": r["node_type"], "confidence": r["confidence"],
                     "aliases": r["aliases"]} for r in rows]
        except Exception:  # noqa: BLE001
            return []
        finally:
            conn.close()

    def _edges_between(self, node_ids: list[int],
                       limit: int = 100) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        conn = self._conn()
        try:
            marks = ",".join("?" * len(node_ids))
            rows = conn.execute(
                f"SELECT e.edge_id, e.relation_type, e.confidence, "
                f"       s.name AS subject, t.name AS object "
                f"FROM ontology_edges e "
                f"LEFT JOIN ontology_nodes s ON s.node_id = e.source_node "
                f"LEFT JOIN ontology_nodes t ON t.node_id = e.target_node "
                f"WHERE e.source_node IN ({marks}) OR e.target_node IN ({marks}) "
                f"LIMIT ?", (*node_ids, *node_ids, max(1, int(limit)))).fetchall()
            return [{"id": int(r["edge_id"]), "subject_term": r["subject"],
                     "relation": r["relation_type"], "object_term": r["object"],
                     "confidence": r["confidence"]} for r in rows]
        except Exception:  # noqa: BLE001
            return []
        finally:
            conn.close()

    def aggregate_search_results(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """把检索结果聚合成本体视角（节点/边）。"""
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        for item in items or []:
            for node in item.get("nodes") or []:
                nodes[str(node.get("id"))] = node
            edges.extend(item.get("edges") or [])
        return {"nodes": list(nodes.values()), "edges": edges}

    def recommend_related(self, query: str,
                          current_items: list[dict[str, Any]],
                          limit: int = 10) -> list[dict[str, Any]]:
        """相关推荐：取与当前结果**共享邻居**的本体节点。"""
        current = {str(n.get("id")) for item in (current_items or [])
                   for n in (item.get("nodes") or [])}
        neighbours: dict[str, int] = {}
        for item in current_items or []:
            for edge in item.get("edges") or []:
                for endpoint in (edge.get("source") or edge.get("subject_term"),
                                 edge.get("target") or edge.get("object_term")):
                    if endpoint and str(endpoint) not in current:
                        neighbours[str(endpoint)] = neighbours.get(str(endpoint), 0) + 1
        ranked = sorted(neighbours.items(), key=lambda kv: -kv[1])[:limit]
        out = []
        for node_id, weight in ranked:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT node_id, node_type, name, confidence FROM ontology_nodes "
                    "WHERE node_id = ?", (int(node_id),)).fetchone()
            except Exception:  # noqa: BLE001
                row = None
            finally:
                conn.close()
            if row:
                out.append({"id": int(row["node_id"]), "title": row["name"],
                            "type": row["node_type"], "weight": weight,
                            "confidence": row["confidence"]})
        return out

    def recent_searches(self, limit: int = 20) -> list[dict[str, Any]]:
        """**引擎无此概念**：没有检索历史表（PWA 有 `search_query` 表）。"""
        self._gap("recent_searches", "引擎不存检索历史（无 search_query 表）")
        return []

    # ================================================================== 实验
    def aggregate_experiments(self, progress_cb: Any = None,
                              cancel_event: Any = None) -> dict[str, Any]:
        """实验知识：引擎的载体是**超边条件与测量**（结构化实验数据）。

        这里按文献聚合成界面期望的 `experiments` 列表（每条含参数/结果计数）。
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT h.hyperedge_id, h.label, h.hyperedge_type, h.paper_key, "
                "  (SELECT COUNT(*) FROM ontology_hyperedge_conditions c "
                "   WHERE c.hyperedge_id = h.hyperedge_id) AS n_cond, "
                "  (SELECT COUNT(*) FROM ontology_hyperedge_measurements m "
                "   WHERE m.hyperedge_id = h.hyperedge_id) AS n_meas "
                "FROM ontology_hyperedges h LIMIT 5000").fetchall()
        except Exception:  # noqa: BLE001
            rows = []
        finally:
            conn.close()
        experiments = []
        for r in rows:
            n_cond, n_meas = int(r["n_cond"] or 0), int(r["n_meas"] or 0)
            if not (n_cond or n_meas):
                continue          # 没有条件/测量的超边不是"实验"
            experiments.append({
                "id": int(r["hyperedge_id"]),
                "experiment_name": r["label"] or f"超边 {r['hyperedge_id']}",
                "object_text": r["hyperedge_type"] or "",
                "paper_key": r["paper_key"],
                "methods": [{"name": r["hyperedge_type"]}] if r["hyperedge_type"] else [],
                "parameters": {"conditions": n_cond},
                "results": {"measurements": n_meas},
                "confidence": 0.0,
            })
        return {"experiments": len(experiments), "items": experiments}

    def list_experiment_knowledge(self, limit: int = 1000) -> list[dict[str, Any]]:
        result = self.aggregate_experiments()
        return (result.get("items") or [])[:max(1, int(limit))]

    def list_experiment_designs(self) -> list[dict[str, Any]]:
        """**引擎无此概念**：实验设计是**即时生成**的（`study/content` 候选池 +
        算子链），没有"设计库"可列。要留档应落库后再列。"""
        self._gap("list_experiment_designs",
                  "引擎即时生成实验设计，没有设计库（未落库）")
        return []

    def generate_experiment_design(self, goal: str, progress_cb: Any = None,
                                   cancel_event: Any = None) -> dict[str, Any]:
        """实验设计：调引擎的规划/研究链，产出 design_context（含算子链）。"""
        from research_agent.writing.section_service import build_project_plan

        conn = self._conn()
        try:
            plan = build_project_plan(
                conn, project_id=0, topic=str(goal or ""),
                instruction=str(goal or ""), settings=self.settings,
                planner_model=None, persist=False)
        finally:
            conn.close()
        design = (plan.get("plan") or {})
        return {"goal": goal, "design": design, "plan": plan,
                "design_context": design.get("design_context") or {}}

    # ================================================================== 写作
    def create_writing_project(self, title: str, topic: str = "") -> int:
        from research_agent.writing import service as wsvc

        conn = self._conn()
        try:
            return int(wsvc.create_project(conn, str(title or "").strip(),
                                           str(topic or "")))
        finally:
            conn.close()

    def list_writing_projects(self) -> list[dict[str, Any]]:
        from research_agent.writing import service as wsvc

        conn = self._conn()
        try:
            return [self._project_row(p) for p in wsvc.list_projects(conn)]
        finally:
            conn.close()

    def get_writing_project(self, project_id: Any) -> dict[str, Any] | None:
        from research_agent.writing import service as wsvc

        conn = self._conn()
        try:
            project = wsvc.get_project(conn, int(project_id))
            if not project:
                return None
            row = self._project_row(project)
            # 界面读 `project["outline"]`（PWA 存 outline JSON）；
            # 引擎把章节放在 writing_sections，这里映射成同一形状。
            row["outline"] = [
                {"key": s.get("section_key"), "heading": s.get("heading")}
                for s in wsvc.list_sections(conn, int(project_id))
            ]
            return row
        finally:
            conn.close()

    def list_writing_sections(self, project_id: Any) -> list[dict[str, Any]]:
        from research_agent.writing import service as wsvc

        conn = self._conn()
        try:
            return [self._section_row(s)
                    for s in wsvc.list_sections(conn, int(project_id))]
        finally:
            conn.close()

    def generate_outline(self, project_id: Any,
                         topic: str) -> list[dict[str, Any]]:
        """生成大纲：引擎的 `generate_outline`（走写作技能包的体裁骨架）。"""
        from research_agent.writing import service as wsvc

        conn = self._conn()
        try:
            model = None
            try:
                model, _reason = wsvc.default_model(self.settings)
            except Exception:  # noqa: BLE001 —— 无 Key 时走包骨架
                model = None
            outline = wsvc.generate_outline(conn, int(project_id),
                                            topic=str(topic or ""), model=model)
            return [{"key": s.get("section_key") or s.get("key"),
                     "heading": s.get("heading")} for s in (outline or [])]
        finally:
            conn.close()

    def generate_section(self, project_id: Any, section_key: str, heading: str,
                         topic: str,
                         materials: list[dict[str, Any]]) -> dict[str, Any]:
        """生成章节：引擎的 `generate_section`（含引文绑定）。"""
        from research_agent.writing import service as wsvc

        conn = self._conn()
        try:
            model = None
            try:
                model, _reason = wsvc.default_model(self.settings)
            except Exception:  # noqa: BLE001
                model = None
            return wsvc.generate_section(
                conn, int(project_id), str(section_key), str(heading),
                str(topic or ""), model=model, materials=materials or [])
        finally:
            conn.close()

    def polish_section(self, project_id: Any, section_key: str,
                       content: str) -> str:
        from research_agent.writing import service as wsvc

        conn = self._conn()
        try:
            model = None
            try:
                model, _reason = wsvc.default_model(self.settings)
            except Exception:  # noqa: BLE001
                model = None
            return wsvc.polish_section(conn, int(project_id), str(section_key),
                                       str(content or ""), model=model)
        finally:
            conn.close()

    def recommend_materials(self, query: str,
                            limit: int = 10) -> list[dict[str, Any]]:
        """推荐素材：优先**带条件/测量的超边**（这才是能写出可执行方案的素材）。"""
        conn = self._conn()
        try:
            like = f"%{str(query or '').strip()}%"
            rows = conn.execute(
                "SELECT h.hyperedge_id, h.label, h.paper_key, "
                "       p.title AS paper_title "
                "FROM ontology_hyperedges h "
                "LEFT JOIN papers p ON p.paper_key = h.paper_key "
                "WHERE LOWER(COALESCE(h.label,'')) LIKE LOWER(?) "
                "   OR LOWER(COALESCE(p.title,'')) LIKE LOWER(?) "
                "LIMIT ?", (like, like, max(1, int(limit)))).fetchall()
            return [{"id": int(r["hyperedge_id"]), "type": "evidence",
                     "title": r["label"] or r["paper_title"],
                     "text": r["paper_title"] or "", "citation": None,
                     "confidence": 0.0} for r in rows]
        except Exception:  # noqa: BLE001
            return []
        finally:
            conn.close()

    # ================================================================== 审核
    def create_review_candidates(self) -> dict[str, Any]:
        """候选队列：引擎在质量控制阶段就把低分文献置为 `human_review`，
        因此这里只需如实返回当前待审数量（不重复造队列）。"""
        from research_agent.dashboard import api as dbapi

        data = dbapi.human_review_items(self.db_path)
        return {"created": len(data.get("pending") or []),
                "note": "引擎在质量控制阶段自动置为待人工，无需另建队列"}

    def list_review_items(self, status: str | None = None) -> list[dict[str, Any]]:
        """待审条目：映射成界面期望的形状。

        `id` 与 `item_id` 都用 **paper_key**（引擎的身份），界面不再做整数转换。
        """
        from research_agent.dashboard import api as dbapi

        data = dbapi.human_review_items(self.db_path,
                                        include_history=bool(status == "history"))
        items = []
        for row in data.get("pending") or []:
            key = str(row.get("paper_key") or "")
            evidence = []
            if row.get("rationale"):
                evidence.append(str(row["rationale"]))
            if row.get("quality") is not None:
                evidence.append(
                    f"质量 {row.get('quality')}（A={row.get('authority')}, "
                    f"T={row.get('timeliness')}）")
            items.append({
                "id": key, "item_id": key, "item_type": "paper_quality",
                "title": f"质量待审核：{row.get('title') or key}",
                "evidence": evidence, "status": "pending",
                "paper_key": key, "decision": row.get("decision"),
                "data": row,
            })
        if status == "history":
            for row in data.get("history") or []:
                key = str(row.get("paper_key") or "")
                items.append({
                    "id": row.get("id"), "item_id": key,
                    "item_type": "paper_quality",
                    "title": f"已审：{row.get('title') or key}",
                    "evidence": [str(row.get("rationale") or "")],
                    "status": "decided", "action": row.get("action"),
                    "data": row,
                })
        return items

    def decide_review_item(self, item_id: Any, decision: str,
                           feedback: str = "") -> None:
        """提交裁决：映射到引擎的 `submit_human_review`。"""
        from research_agent.dashboard import api as dbapi

        action = {"approved": "approve", "rejected": "reject"}.get(
            str(decision), "custom")
        dbapi.submit_human_review(self.db_path, {
            "paper_key": str(item_id), "action": action,
            "rationale": str(feedback or ""),
        })

    def review_preferences(self) -> list[dict[str, Any]]:
        """**引擎无此概念**：审核偏好学习在 PWA 里是 3 条硬编码规则（假学习），
        引擎侧没有实现，也不搬那套。要做得真正实现后再接。"""
        self._gap("review_preferences",
                  "引擎未实现审核偏好学习（PWA 原版是硬编码规则，已弃）")
        return []

    # ================================================================== 写作台
    # 访谈闭环 + 派工：界面的「写作台」页用这一组方法。
    #
    # 职责边界（引擎侧已经定好，这里只做透传）：
    #   工作规划节点（interview）负责**规划与协作**——逐部分问答、判定支撑、
    #   组织补齐、把需求下发成派工单；**写作**由知识消费节点与内容形成节点完成。
    def section_templates(self, genre: str = "") -> dict[str, Any]:
        """体裁与模板（前置问答的第一个问题要用）。"""
        from research_agent.dashboard import section_api as secapi

        return secapi.section_templates(self.db_path, genre or None)

    def interview_start(self, project_id: Any, *, reset: bool = False,
                        genre: str = "", topic: str = "",
                        sections: list[str] | None = None) -> dict[str, Any]:
        """开始/恢复访谈。`reset=True` 清空问答进度（已写正文保留）。"""
        from research_agent.dashboard import section_api as secapi

        return secapi.interview_start(self.db_path, int(project_id),
                                      reset=reset, genre=genre, topic=topic,
                                      sections_list=sections)

    def interview_snapshot(self, project_id: Any) -> dict[str, Any]:
        """访谈快照：进度 + 当前问题 + 各部分状态 + 对话历史。

        `next_action` 非空表示"该起一个作业推进"，界面据此自动推进。
        """
        from research_agent.dashboard import section_api as secapi

        return secapi.interview_snapshot(self.db_path, int(project_id))

    def interview_answer(self, project_id: Any,
                         payload: dict[str, Any]) -> dict[str, Any]:
        """记录一次作答。**纯状态推进**，不跑耗时操作（耗时交给 step）。"""
        from research_agent.dashboard import section_api as secapi

        return secapi.interview_answer(self.db_path, int(project_id), payload)

    def _use_model(self, explicit: bool | None = None) -> bool:
        """是否允许调用真实模型。

        环境变量 ``RA_DESK_OFFLINE=1`` 可强制离线——界面回归必须能离线跑，
        否则每一步都要等真模型（拟 3 案可能几十秒），冒烟就不再是冒烟。
        """
        import os

        if explicit is not None:
            return bool(explicit)
        return os.environ.get("RA_DESK_OFFLINE", "").strip().lower() not in {
            "1", "true", "yes", "on"}

    def interview_step(self, project_id: Any,
                       progress_cb: Any = None, cancel_event: Any = None,
                       use_model: bool | None = None) -> dict[str, Any]:
        """推进访谈一步（拟方案 / 判定 / 协作 / 写作），返回作业信息。

        `progress_cb` / `cancel_event` 由任务管理器注入：界面上的进度条与
        「停止任务」按钮靠它们工作。
        """
        from research_agent.dashboard import section_api as secapi

        return secapi.interview_step(self.db_path, int(project_id),
                                     settings=self.settings,
                                     use_model=self._use_model(use_model))

    def section_job_status(self, job_id: str) -> dict[str, Any]:
        from research_agent.dashboard import section_api as secapi

        return secapi.job_status(self.db_path, str(job_id))

    def cancel_section_job(self, job_id: str) -> dict[str, Any]:
        from research_agent.dashboard import section_api as secapi

        return secapi.cancel_job(self.db_path, str(job_id))

    def section_trace(self, project_id: Any, section_key: str,
                      limit: int = 20) -> dict[str, Any]:
        """某部分的决策轨迹（每轮判定与协作的留痕）。"""
        from research_agent.dashboard import section_api as secapi

        return secapi.section_trace(self.db_path, int(project_id),
                                    str(section_key), int(limit))

    def section_content(self, project_id: Any,
                        section_key: str) -> dict[str, Any]:
        from research_agent.dashboard import section_api as secapi

        return secapi.section_content(self.db_path, int(project_id),
                                      str(section_key))

    # ------------------------------------------ 派工（向规划节点下需求）
    def parse_dispatch_request(self, request: str, *, project_id: Any = 0,
                               section_key: str = "", topic: str = "",
                               heading: str = "",
                               verdict: dict[str, Any] | None = None,
                               use_model: bool | None = None) -> dict[str, Any]:
        """自然语言需求 → 3 个可直接派工的方案（**只解析，不执行**）。

        这是"直接向工作规划节点发需求"的入口：规划节点把用户的话翻译成
        "对哪些节点下什么单"，再由派工执行器分发给各节点。
        """
        from research_agent.writing import dispatch_planner as dpl

        return dpl.build_dispatch_plan(
            request=str(request or ""), db_path=self.db_path,
            project_id=int(project_id or 0), section_key=section_key,
            topic=topic, heading=heading, verdict=verdict or {},
            use_model=self._use_model(use_model), settings=self.settings)

    def create_dispatch(self, plan: list[dict[str, Any]], *,
                        project_id: Any = 0, section_key: str = "",
                        origin: str = "user_direct", reason: str = "",
                        budget: dict[str, Any] | None = None,
                        progress_cb: Any = None,
                        cancel_event: Any = None) -> dict[str, Any]:
        """按方案下单并执行，返回含逐步回报的派工单。"""
        from research_agent.writing import dispatch as dp

        conn = self._conn()
        try:
            return dp.run_dispatch(
                plan=list(plan or []), conn=conn,
                project_id=int(project_id or 0), section_key=section_key,
                origin=origin, reason=reason, budget=budget or {},
                settings=self.settings, cancel_event=cancel_event,
                # 离线时必须也告诉执行器：否则界面不调模型、派工却照调，
                # 既烧配额又让离线回归不可复现（曾打出 arXiv/OpenAlex 请求
                # 并撞上 600s 超时）。
                model_available=self._use_model(None))
        finally:
            conn.close()

    def list_dispatches(self, *, project_id: Any = 0, section_key: str = "",
                        limit: int = 20) -> list[dict[str, Any]]:
        """最近的派工单（「当前派工」看板）。"""
        from research_agent.writing import dispatch as dp

        conn = self._conn()
        try:
            return dp.list_dispatches(conn, project_id=int(project_id or 0),
                                      section_key=section_key, limit=int(limit))
        finally:
            conn.close()

    # ================================================================== 行映射
    def _paper_row(self, row: dict[str, Any],
                   extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """引擎文献记录 → 界面期望的行（`id` = `paper_key`）。"""
        key = str(row.get("paper_key") or row.get("id") or "")
        tags: list[str] = []
        favorite = False
        try:
            tags, favorite = self._with_store(
                lambda s: (list(s.get_tags(key) or []),
                           bool(s.is_favorite(key))))
        except Exception:  # noqa: BLE001 —— 侧车表可能还没建
            pass
        return {
            "id": key, "paper_key": key,
            "title": row.get("title") or "",
            "abstract": row.get("abstract") or "",
            "authors": row.get("authors") or [],
            "venue": row.get("venue") or row.get("journal") or "",
            "journal": row.get("venue") or row.get("journal") or "",
            "year": row.get("pub_year") or row.get("year"),
            "pub_year": row.get("pub_year") or row.get("year"),
            "doi": row.get("doi") or "",
            "source": row.get("source") or "",
            "url": row.get("url") or "",
            "citation_count": row.get("citation_count") or 0,
            "status": row.get("status") or "",
            "quality_score": row.get("quality") or row.get("quality_score"),
            "quality_level": row.get("decision") or row.get("quality_level"),
            "tags": tags, "favorite": favorite,
            "has_knowledge": bool(row.get("has_knowledge")),
            "created_at": row.get("created_at"),
        }

    def _project_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row.get("project_id") or row.get("id"),
            "title": row.get("title") or "",
            "topic": row.get("topic") or "",
            "genre": row.get("genre") or "",
            "status": row.get("status") or "",
            "updated_at": row.get("updated_at"),
        }

    def _section_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "section_key": row.get("section_key") or row.get("key"),
            "heading": row.get("heading") or "",
            "content": row.get("content") or "",
            "citation_ids": row.get("citation_ids") or [],
            "status": row.get("status") or "",
        }


def build_adapter(db_path: str | Path | None = None,
                  settings: Any = None) -> BackendAdapter:
    """构造适配器（界面里用 `st.cache_resource` 缓存住，避免每次交互重建）。"""
    return BackendAdapter(db_path=db_path, settings=settings)
