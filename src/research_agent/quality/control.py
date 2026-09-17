"""质量控制节点：文献质量评分之外的全局一致性控制。

在原有 quality 任务基础上，当本体新增节点跨过阈值时，基于领域词典对
Chemical/Method/Concept/Property 等节点做外部身份归并。词典由 IUPAC Gold Book
和 ChEBI 的轻量本地层提供，避免无证据的泛称合并。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from research_agent.config import Settings, settings as default_settings
from research_agent.db import meta_get, meta_set
from research_agent.ontology import store as ont
from research_agent.ontology.term_dictionaries import lookup_identity

logger = logging.getLogger(__name__)


def _json_list(value: Any) -> list:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _dedup(value: list) -> list:
    out = []
    for item in value:
        sig = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if sig not in {json.dumps(x, ensure_ascii=False, sort_keys=True) for x in out}:
            out.append(item)
    return out


def _merge_attr(old: dict, new: dict) -> dict:
    merged = dict(old or {})
    for key, value in (new or {}).items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        if key not in merged:
            merged[key] = value
        elif merged[key] != value:
            if not isinstance(merged[key], list):
                merged[key] = [merged[key]]
            if value not in merged[key]:
                merged[key].append(value)
    return merged


def _evidence_score(row: sqlite3.Row) -> tuple[int, float, int]:
    prov = _json_list(row["provenance"])
    return len(prov), float(row["confidence"] or 0), int(row["node_id"])


def _merge_edges(conn: sqlite3.Connection, drop_id: int, keep_id: int) -> int:
    """把指向 drop 节点的边重定向到 keep，并合并同关系重复边。"""
    rows = conn.execute(
        "SELECT * FROM ontology_edges "
        "WHERE source_node=? OR target_node=?",
        (drop_id, drop_id),
    ).fetchall()
    merged_count = 0
    for row in rows:
        src = int(row["source_node"])
        tgt = int(row["target_node"])
        new_src = keep_id if src == drop_id else src
        new_tgt = keep_id if tgt == drop_id else tgt
        if new_src == new_tgt:
            continue
        existing = conn.execute(
            "SELECT * FROM ontology_edges "
            "WHERE relation_type=? AND source_node=? AND target_node=? "
            "AND edge_id<>?",
            (row["relation_type"], new_src, new_tgt, row["edge_id"]),
        ).fetchone()
        if existing:
            keep_prov = _dedup(
                _json_list(existing["provenance"])
                + _json_list(row["provenance"])
            )
            attrs = _merge_attr(
                json.loads(existing["attributes"] or "{}"),
                json.loads(row["attributes"] or "{}"),
            )
            conf = max(float(existing["confidence"] or 0),
                       float(row["confidence"] or 0))
            tier = str(existing["evidence_tier"] or "") or str(row["evidence_tier"] or "")
            conn.execute(
                "UPDATE ontology_edges SET confidence=?, attributes=?, "
                "provenance=?, evidence_tier=? WHERE edge_id=?",
                (
                    conf,
                    json.dumps(attrs, ensure_ascii=False),
                    json.dumps(keep_prov, ensure_ascii=False),
                    tier,
                    int(existing["edge_id"]),
                ),
            )
            conn.execute("DELETE FROM ontology_edges WHERE edge_id=?",
                         (int(row["edge_id"]),))
            merged_count += 1
        else:
            conn.execute(
                "UPDATE ontology_edges SET source_node=?, target_node=? "
                "WHERE edge_id=?",
                (new_src, new_tgt, int(row["edge_id"])),
            )
    return merged_count


def _repoint_hyperedge_refs(conn: sqlite3.Connection,
                            drop_id: int,
                            keep_id: int) -> int:
    """把超边成员与测量主体从被删节点重指到保留节点（P0-4）。

    不重指的后果：产生悬空成员行（本库所有 ontology_* 表都没有 FOREIGN KEY），
    随后知识节点的孤儿超边清理会因"成员全部失效"而删除整条超边，
    连带它的 conditions / measurements / evidence —— 一次词典归并即造成数据丢失。
    """
    count = 0
    # 成员表：同一超边里可能同时存在 keep 与 drop 成员，先删重复再加回
    dup = conn.execute(
        "SELECT m1.id FROM ontology_hyperedge_members m1 "
        "JOIN ontology_hyperedge_members m2 "
        "  ON m1.hyperedge_id = m2.hyperedge_id "
        " WHERE m1.node_id=? AND m2.node_id=?",
        (drop_id, keep_id),
    ).fetchall()
    if dup:
        conn.executemany("DELETE FROM ontology_hyperedge_members WHERE id=?",
                         [(int(r["id"]),) for r in dup])
    cur = conn.execute(
        "UPDATE ontology_hyperedge_members SET node_id=? WHERE node_id=?",
        (keep_id, drop_id),
    )
    count += cur.rowcount or 0
    cur = conn.execute(
        "UPDATE ontology_hyperedge_measurements SET subject_node=? "
        "WHERE subject_node=?",
        (keep_id, drop_id),
    )
    count += cur.rowcount or 0
    return count


def _repoint_event_refs(conn: sqlite3.Connection,
                        drop_id: int,
                        keep_id: int) -> int:
    rows = conn.execute(
        "SELECT id, entity_refs FROM event_assertions "
        "WHERE entity_refs LIKE ? OR entity_refs LIKE ?",
        (f"%{drop_id}%", f"%{drop_id},%"),
    ).fetchall()
    count = 0
    for row in rows:
        refs = [int(x) for x in _json_list(row["entity_refs"]) if x]
        changed = False
        out: list[int] = []
        for ref in refs:
            val = keep_id if ref == drop_id else ref
            if val not in out:
                out.append(val)
                changed = changed or ref == drop_id
        if changed:
            conn.execute(
                "UPDATE event_assertions SET entity_refs=? WHERE id=?",
                (json.dumps(out), int(row["id"])),
            )
            count += 1
    return count


def _merge_group(conn: sqlite3.Connection,
                 rows: list[sqlite3.Row],
                 identity: dict[str, Any]) -> int:
    """把同一外部词典身份的节点合并到证据最多/最早的一个 keeper。"""
    if len(rows) < 2:
        return 0
    ranked = sorted(rows, key=_evidence_score, reverse=True)
    keeper = ranked[0]
    keep_id = int(keeper["node_id"])
    merged = 0
    for drop in ranked[1:]:
        drop_id = int(drop["node_id"])
        keep_aliases = _json_list(keeper["aliases"])
        drop_aliases = _json_list(drop["aliases"])
        attrs = _merge_attr(
            json.loads(keeper["attributes"] or "{}"),
            json.loads(drop["attributes"] or "{}"),
        )
        prov = _dedup(_json_list(keeper["provenance"])
                      + _json_list(drop["provenance"]))
        aliases = list(dict.fromkeys(keep_aliases + drop_aliases))
        conf = max(float(keeper["confidence"] or 0),
                   float(drop["confidence"] or 0))
        conn.execute(
            "UPDATE ontology_nodes SET aliases=?, attributes=?, confidence=?, "
            "provenance=?, external_source=?, identity_key=?, term_status=? "
            "WHERE node_id=?",
            (
                json.dumps(aliases, ensure_ascii=False),
                json.dumps(attrs, ensure_ascii=False),
                conf,
                json.dumps(prov, ensure_ascii=False),
                identity["external_source"],
                identity["external_id"],
                "dictionary_normalized",
                keep_id,
            ),
        )
        _merge_edges(conn, drop_id, keep_id)
        _repoint_event_refs(conn, drop_id, keep_id)
        _repoint_hyperedge_refs(conn, drop_id, keep_id)
        ont.log_merge(
            conn, [drop_id], keep_id,
            rule_level="dictionary",
            reason=(
                f"{identity['external_source']}:{identity['external_id']} "
                f"→ {identity['canonical_name']}"
            ),
            operator="quality_control",
        )
        conn.execute("DELETE FROM ontology_nodes WHERE node_id=?", (drop_id,))
        merged += 1
    return merged


def run_dictionary_merge(conn: sqlite3.Connection) -> dict[str, Any]:
    """扫描节点并按领域词典外部身份归并；只处理同类型同词典身份。"""
    rows = conn.execute(
        "SELECT node_id, node_type, name, aliases, confidence, attributes, "
        "provenance FROM ontology_nodes ORDER BY node_id"
    ).fetchall()
    groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        name = str(row["name"] or "").strip()
        aliases = [str(x) for x in _json_list(row["aliases"]) if str(x).strip()]
        identity = lookup_identity(
            name, node_type=str(row["node_type"]), aliases=aliases)
        if not identity:
            continue
        key = (
            str(row["node_type"]),
            identity["external_source"],
            identity["external_id"],
        )
        groups.setdefault(key, []).append(row)
    stats = {
        "dictionary_groups": len(groups),
        "duplicate_groups": sum(1 for v in groups.values() if len(v) > 1),
        "merged_nodes": 0,
        "reasons": [],
    }
    for key, group_rows in groups.items():
        identity = {
            "external_source": key[1],
            "external_id": key[2],
            "canonical_name": key[2],
        }
        before = len(group_rows)
        merged = _merge_group(conn, group_rows, identity)
        if merged:
            stats["merged_nodes"] += merged
            stats["reasons"].append(f"{key[0]} {key[1]}:{key[2]} "
                                    f"({before}→{before - merged})")
    conn.commit()
    return stats


def maybe_global_merge(conn: sqlite3.Connection,
                       settings: Settings | None = None,
                       *,
                       force: bool = False) -> dict[str, Any]:
    """新增节点达到阈值时触发一次全局词典归并。"""
    settings = settings or default_settings
    if not getattr(settings, "global_merge_enabled", True):
        return {"triggered": False, "reason": "disabled"}
    try:
        last_count = int(meta_get(
            conn, "ontology_global_merge_last_nodes", 0) or 0)
    except (TypeError, ValueError):
        last_count = 0
    try:
        current = conn.execute(
            "SELECT COUNT(*) AS c FROM ontology_nodes"
        ).fetchone()["c"]
    except sqlite3.OperationalError:
        return {"triggered": False, "reason": "ontology-not-initialized"}
    interval = max(1, int(getattr(settings, "global_merge_interval_nodes", 300)))
    if not force and current < last_count + interval:
        return {
            "triggered": False,
            "current_nodes": current,
            "next_threshold": last_count + interval,
        }
    stats = run_dictionary_merge(conn)
    after = conn.execute(
        "SELECT COUNT(*) AS c FROM ontology_nodes"
    ).fetchone()["c"]
    meta_set(conn, "ontology_global_merge_last_nodes", after)
    meta_set(conn, "ontology_global_merge_last_run",
             json.dumps(stats, ensure_ascii=False))
    logger.info("质量控制全局归并: %s", stats)
    return {
        "triggered": True,
        "stats": stats,
        "nodes_before": current,
        "nodes_after": after,
    }
