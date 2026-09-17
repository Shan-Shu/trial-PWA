"""动态本体图存储（SQLite）。

设计要点：
1. **动态 schema**：ontology_type_registry 同时存放“节点类型”与“关系类型”，
   知识提取发现新类型时自动注册（origin='dynamic'），本体版本号随之递增——
   这是“动态本体”演化的核心。
2. **合并更新**：节点以 (node_type, normalized_name) 唯一；重复出现时
   合并别名/属性/来源证据，置信度取 max（新证据不降低旧结论，只增补）。
3. **溯源**：每个节点/边记录 provenance（来源论文 + 证据句子），可审计。
4. **领域词表外置**：种子类型、关系同义归一与强断言集合全部来自
   `packs/skills/relation-lexicon`（可用 `RA_PACKS_DIR` 挂载自定义包），
   本模块不再内联任何学科词表。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from research_agent.db import meta_bump, meta_get, meta_set, utcnow
from research_agent.packs import (
    canonical_relation_type as _pack_canonical_relation,
    relation_lexicon,
    seed_node_types,
    seed_relation_types,
    strong_relations,
)


def canonical_relation_type(relation_type: str) -> str:
    """把同义/动词化变体归一到受控词表词；无法归一则保留原词。

    词表来自 `packs/skills/relation-lexicon`（不再内联在代码里）。
    """
    key = re.sub(r"\s+", " ", str(relation_type or "").strip().lower())
    synonyms = relation_lexicon()["synonyms"]
    return synonyms.get(key, _pack_canonical_relation(relation_type))


def seeded_node_types() -> list[tuple[str, str]]:
    """种子节点类型（技能包 + 各领域包的追加项）。"""
    return [(str(t), str(l)) for t, l in seed_node_types()]


def seeded_relation_types() -> list[tuple[str, str]]:
    """种子关系类型（技能包 + 各领域包的追加项）。"""
    return [(str(t), str(l)) for t, l in seed_relation_types()]


def strong_relation_types() -> set[str]:
    """需要置信门控的强断言关系集合。"""
    return strong_relations()


ONTOLOGY_SCHEMA = """
CREATE TABLE IF NOT EXISTS ontology_type_registry (
    type_key    TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- 'node' | 'relation'
    label       TEXT,
    origin      TEXT DEFAULT 'seed',    -- 'seed' | 'dynamic'
    created_at  TEXT,
    description TEXT
);

CREATE TABLE IF NOT EXISTS ontology_nodes (
    node_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    node_type      TEXT NOT NULL,
    name           TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    aliases        TEXT DEFAULT '[]',
    attributes     TEXT DEFAULT '{}',
    confidence     REAL DEFAULT 0.5,
    first_seen_at  TEXT,
    last_seen_at   TEXT,
    provenance     TEXT DEFAULT '[]',
    UNIQUE(node_type, normalized_name)
);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON ontology_nodes(node_type);

CREATE TABLE IF NOT EXISTS ontology_edges (
    edge_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_type TEXT NOT NULL,
    source_node   INTEGER NOT NULL,
    target_node   INTEGER NOT NULL,
    attributes    TEXT DEFAULT '{}',
    confidence    REAL DEFAULT 0.5,
    created_at    TEXT,
    provenance    TEXT DEFAULT '[]',
    UNIQUE(relation_type, source_node, target_node)
);
CREATE INDEX IF NOT EXISTS idx_edges_type ON ontology_edges(relation_type);

CREATE TABLE IF NOT EXISTS ontology_runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_key        TEXT,
    counts           TEXT,
    new_types        TEXT,
    ontology_version INTEGER,
    ran_at           TEXT
);

CREATE TABLE IF NOT EXISTS event_assertions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_key       TEXT,
    event_type      TEXT,
    trigger         TEXT,
    participants    TEXT DEFAULT '[]',
    entity_refs     TEXT DEFAULT '[]',
    time_text       TEXT,
    attributes      TEXT DEFAULT '{}',
    confidence      REAL DEFAULT 0.5,
    provenance      TEXT DEFAULT '[]',
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_paper ON event_assertions(paper_key);

CREATE TABLE IF NOT EXISTS material_registry (
    local_id        TEXT PRIMARY KEY,
    preferred_name  TEXT,
    synonyms        TEXT DEFAULT '[]',
    composition     TEXT DEFAULT '{}',
    term_status     TEXT DEFAULT 'local_uncurated',
    curated_by      TEXT,
    created_at      TEXT,
    updated_at      TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS merge_journal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_ids        TEXT DEFAULT '[]',
    to_id           INTEGER,
    rule_level      TEXT,
    reason          TEXT,
    operator        TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS merge_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_ids        TEXT DEFAULT '[]',
    reason          TEXT,
    status          TEXT DEFAULT 'open',
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS direction_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id         INTEGER,
    relation_type   TEXT,
    source_node     INTEGER,
    target_node     INTEGER,
    suggestion      TEXT,
    status          TEXT DEFAULT 'open',
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS ontology_hyperedges (
    hyperedge_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    hyperedge_type  TEXT NOT NULL,
    fingerprint     TEXT UNIQUE,
    label           TEXT,
    attributes      TEXT DEFAULT '{}',
    confidence      REAL DEFAULT 0.5,
    evidence_tier   TEXT DEFAULT 'unclassified',
    paper_key       TEXT,
    created_at      TEXT,
    provenance      TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_hyperedges_type
    ON ontology_hyperedges(hyperedge_type);
CREATE INDEX IF NOT EXISTS idx_hyperedges_paper
    ON ontology_hyperedges(paper_key);
CREATE INDEX IF NOT EXISTS idx_hyperedges_created
    ON ontology_hyperedges(created_at);

CREATE TABLE IF NOT EXISTS ontology_hyperedge_members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hyperedge_id    INTEGER NOT NULL,
    node_id         INTEGER NOT NULL,
    role            TEXT,
    position        INTEGER DEFAULT 0,
    qualifiers      TEXT DEFAULT '{}',
    UNIQUE(hyperedge_id, node_id, role)
);
CREATE INDEX IF NOT EXISTS idx_hyperedge_members_edge
    ON ontology_hyperedge_members(hyperedge_id);
CREATE INDEX IF NOT EXISTS idx_hyperedge_members_node
    ON ontology_hyperedge_members(node_id);

CREATE TABLE IF NOT EXISTS ontology_hyperedge_conditions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hyperedge_id    INTEGER NOT NULL,
    condition_key   TEXT,
    operator        TEXT,
    value_text      TEXT,
    value_num       REAL,
    unit            TEXT,
    qualifier       TEXT,
    UNIQUE(hyperedge_id, condition_key, value_text, unit)
);
CREATE INDEX IF NOT EXISTS idx_hyperedge_conditions_edge
    ON ontology_hyperedge_conditions(hyperedge_id);

CREATE TABLE IF NOT EXISTS ontology_hyperedge_measurements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hyperedge_id    INTEGER NOT NULL,
    metric          TEXT,
    value_text      TEXT,
    value_num       REAL,
    unit            TEXT,
    qualifier       TEXT,
    subject_node    INTEGER,
    UNIQUE(hyperedge_id, metric, value_text, unit, subject_node)
);
CREATE INDEX IF NOT EXISTS idx_hyperedge_measurements_edge
    ON ontology_hyperedge_measurements(hyperedge_id);

CREATE TABLE IF NOT EXISTS ontology_hyperedge_evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hyperedge_id    INTEGER NOT NULL,
    paper_key       TEXT,
    section         TEXT,
    span_text       TEXT,
    char_start      INTEGER,
    char_end        INTEGER,
    UNIQUE(hyperedge_id, paper_key, span_text, char_start, char_end)
);
CREATE INDEX IF NOT EXISTS idx_hyperedge_evidence_edge
    ON ontology_hyperedge_evidence(hyperedge_id);

CREATE TABLE IF NOT EXISTS ontology_hyperedge_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_hyperedge  INTEGER NOT NULL,
    to_hyperedge    INTEGER NOT NULL,
    relation_type   TEXT,
    confidence      REAL DEFAULT 0.5,
    provenance      TEXT DEFAULT '[]',
    UNIQUE(from_hyperedge, to_hyperedge, relation_type)
);

CREATE TABLE IF NOT EXISTS ontology_hyperedge_clusters (
    cluster_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_key     TEXT UNIQUE,
    hyperedge_type  TEXT,
    relation_family TEXT,
    role_signature  TEXT DEFAULT '{}',
    member_signature TEXT DEFAULT '{}',
    support_count   INTEGER DEFAULT 0,
    confidence      REAL DEFAULT 0.5,
    summary         TEXT
);

CREATE TABLE IF NOT EXISTS ontology_hyperedge_cluster_members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id      INTEGER NOT NULL,
    hyperedge_id    INTEGER NOT NULL,
    confidence      REAL DEFAULT 0.5,
    UNIQUE(cluster_id, hyperedge_id)
);

CREATE TABLE IF NOT EXISTS ontology_domains (
    domain_key      TEXT PRIMARY KEY,
    label           TEXT,
    domain_type     TEXT,
    description     TEXT,
    attributes      TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS ontology_domain_members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_key      TEXT NOT NULL,
    node_id         INTEGER NOT NULL,
    weight          REAL DEFAULT 1.0,
    confidence      REAL DEFAULT 1.0,
    UNIQUE(domain_key, node_id)
);
CREATE INDEX IF NOT EXISTS idx_domain_members_node
    ON ontology_domain_members(node_id);

CREATE TABLE IF NOT EXISTS ontology_relation_channels (
    channel_key     TEXT PRIMARY KEY,
    relation_family TEXT,
    role_profile    TEXT DEFAULT '{}',
    source_domains  TEXT DEFAULT '[]',
    target_domains  TEXT DEFAULT '[]',
    support_count   INTEGER DEFAULT 0,
    paper_count     INTEGER DEFAULT 0,
    confidence      REAL DEFAULT 0.5,
    summary         TEXT
);

CREATE TABLE IF NOT EXISTS ontology_channel_hyperedges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_key     TEXT NOT NULL,
    hyperedge_id    INTEGER NOT NULL,
    weight          REAL DEFAULT 1.0,
    UNIQUE(channel_key, hyperedge_id)
);
CREATE INDEX IF NOT EXISTS idx_channel_hyperedges_edge
    ON ontology_channel_hyperedges(hyperedge_id);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _norm(name: str) -> str:
    """规范化名称：小写、去空白与常见分隔符，用于合并判定。"""
    name = re.sub(r"[\s_\-/\\.,;:'\"()\[\]{}]+", " ", str(name)).strip().lower()
    return re.sub(r"\s+", " ", name)


def _find_node_by_alias(conn: sqlite3.Connection, node_type: str,
                        names: list[str]) -> sqlite3.Row | None:
    """在同类型节点中按 name/normalized_name/别名查找匹配（用于去重合并）。"""
    norms = {_norm(n) for n in names if n and _norm(n)}
    if not norms:
        return None
    ph = ",".join("?" * len(norms))
    row = conn.execute(
        f"SELECT node_id, node_type, name, normalized_name, confidence, aliases, "
        f"attributes, provenance FROM ontology_nodes "
        f"WHERE node_type=? AND normalized_name IN ({ph}) LIMIT 1",
        [node_type, *norms],
    ).fetchone()
    if row:
        return row
    # 再按既有节点的 aliases 内容匹配
    rows = conn.execute(
        "SELECT node_id, node_type, name, normalized_name, confidence, aliases, "
        "attributes, provenance FROM ontology_nodes WHERE node_type=?",
        (node_type,),
    ).fetchall()
    for r in rows:
        try:
            alias_list = json.loads(r["aliases"] or "[]")
        except json.JSONDecodeError:
            alias_list = []
        alias_norms = {_norm(x) for x in alias_list}
        if norms & alias_norms:
            return r
    return None


def init_ontology(conn: sqlite3.Connection) -> None:
    conn.executescript(ONTOLOGY_SCHEMA)
    _ensure_column(conn, "ontology_nodes", "identity_key", "TEXT")
    _ensure_column(conn, "ontology_nodes", "external_source", "TEXT")
    _ensure_column(conn, "ontology_nodes", "external_id", "TEXT")
    _ensure_column(conn, "ontology_nodes", "term_status",
                   "TEXT DEFAULT 'local_uncurated'")
    _ensure_column(conn, "ontology_nodes", "scope_tag", "TEXT")
    _ensure_column(conn, "ontology_nodes", "evidence_tier",
                   "TEXT DEFAULT 'unclassified'")
    _ensure_column(conn, "ontology_edges", "evidence_tier",
                   "TEXT DEFAULT 'unclassified'")
    for key, label in seeded_node_types():
        _ensure_type_row(conn, key, "node", label, "seed")
    for key, label in seeded_relation_types():
        _ensure_type_row(conn, key, "relation", label, "seed")
    # P2-7：SQLite 的 UNIQUE 把 NULL 视为互不相等，unit/subject_node 为空的
    # 条件/测量会被反复插入。这里做一次性迁移（去重 + 建表达式唯一索引）。
    _migrate_hyperedge_null_safe_uniqueness(conn)
    if meta_get(conn, "ontology_schema_version") is None:
        meta_set(conn, "ontology_schema_version", 1)
    conn.commit()


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _migrate_hyperedge_null_safe_uniqueness(conn: sqlite3.Connection) -> dict[str, int]:
    """给超边条件/测量补上"NULL 也参与比较"的唯一性（P2-7）。

    SQLite 的 UNIQUE(hyperedge_id, condition_key, value_text, unit) 在 unit 为
    NULL 时形同失效（NULL != NULL），同一条件会重复落库。这里：
      1) 按 ifnull 归一后的键删除历史重复行（保留最小 id）；
      2) 建立表达式唯一索引，使 INSERT OR IGNORE 对 NULL 也生效。
    索引已存在时直接跳过，因此不是每次 init 都扫表。
    """
    stats = {"dropped_conditions": 0, "dropped_measurements": 0,
             "indexes_created": 0}
    if _index_exists(conn, "uq_hyperedge_conditions_nullsafe"):
        return stats
    cur = conn.execute(
        """
        DELETE FROM ontology_hyperedge_conditions
        WHERE id NOT IN (
            SELECT MIN(id) FROM ontology_hyperedge_conditions
            GROUP BY hyperedge_id, ifnull(condition_key, ''),
                     ifnull(value_text, ''), ifnull(unit, '')
        )
        """
    )
    stats["dropped_conditions"] = max(0, int(cur.rowcount or 0))
    cur = conn.execute(
        """
        DELETE FROM ontology_hyperedge_measurements
        WHERE id NOT IN (
            SELECT MIN(id) FROM ontology_hyperedge_measurements
            GROUP BY hyperedge_id, ifnull(metric, ''), ifnull(value_text, ''),
                     ifnull(unit, ''), ifnull(subject_node, -1)
        )
        """
    )
    stats["dropped_measurements"] = max(0, int(cur.rowcount or 0))
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_hyperedge_conditions_nullsafe
        ON ontology_hyperedge_conditions(
            hyperedge_id, ifnull(condition_key, ''), ifnull(value_text, ''),
            ifnull(unit, ''))
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_hyperedge_measurements_nullsafe
        ON ontology_hyperedge_measurements(
            hyperedge_id, ifnull(metric, ''), ifnull(value_text, ''),
            ifnull(unit, ''), ifnull(subject_node, -1))
        """
    )
    stats["indexes_created"] = 2
    return stats


def _ensure_type_row(conn: sqlite3.Connection, type_key: str, kind: str,
                     label: str | None, origin: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ontology_type_registry WHERE type_key=?", (type_key,)
    ).fetchone()
    if row:
        return False
    conn.execute(
        "INSERT INTO ontology_type_registry(type_key, kind, label, origin, created_at) "
        "VALUES(?,?,?,?,?)",
        (type_key, kind, label, origin, utcnow()),
    )
    return True


def ensure_node_type(conn: sqlite3.Connection, type_key: str,
                     label: str | None = None) -> bool:
    """注册节点类型（若不存在）。返回是否为新类型（动态演化触发）。"""
    is_new = _ensure_type_row(conn, type_key, "node", label, "dynamic")
    if is_new:
        meta_bump(conn, "ontology_schema_version")
        conn.commit()
    return is_new


def ensure_relation_type(conn: sqlite3.Connection, type_key: str,
                         label: str | None = None) -> bool:
    """注册关系类型（若不存在）。返回是否为新类型。"""
    is_new = _ensure_type_row(conn, type_key, "relation", label, "dynamic")
    if is_new:
        meta_bump(conn, "ontology_schema_version")
        conn.commit()
    return is_new


def _merge_aliases(old: list, new: list) -> list:
    out = list(old)
    for a in new or []:
        if a and a not in out:
            out.append(a)
    return out


def _merge_attributes(old: dict, new: dict, provenance_key: str) -> dict:
    """属性合并：old/new 均为 {attr: value}；仅保留非空值。

    v0.0.5 起不再生成 {value, source} 包装：来源统一由 provenance 记录；
    同属性出现不同值时收敛为列表（纯值，无 source 内嵌）。
    """
    merged = dict(old or {})
    for k, v in (new or {}).items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        prev = merged.get(k)
        if k in merged and prev != v:
            if not isinstance(prev, list):
                merged[k] = [prev]
            if v not in merged[k]:
                merged[k].append(v)
        else:
            merged[k] = v
    return merged


def upsert_node(conn: sqlite3.Connection, *, node_type: str, name: str,
                confidence: float, aliases: list[str] | None = None,
                attributes: dict | None = None,
                provenance: list[dict] | None = None) -> tuple[int, bool]:
    """插入或合并节点，返回 (node_id, is_new)。"""
    norm = _norm(name)
    now = utcnow()
    ensure_node_type(conn, node_type)
    row = conn.execute(
        "SELECT node_id, name, normalized_name, confidence, aliases, attributes, "
        "provenance FROM ontology_nodes "
        "WHERE node_type=? AND normalized_name=?",
        (node_type, norm),
    ).fetchone()
    if not row:
        # 按别名去重：论文换了一种写法（或给出了既有规范名的别名）也并入同一节点
        row = _find_node_by_alias(conn, node_type, [name] + list(aliases or []))
    if not row:
        cur = conn.execute(
            "INSERT INTO ontology_nodes(node_type, name, normalized_name, aliases, "
            "attributes, confidence, first_seen_at, last_seen_at, provenance) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (node_type, name, norm,
             json.dumps(aliases or [], ensure_ascii=False),
             json.dumps(attributes or {}, ensure_ascii=False),
             max(0.0, min(1.0, confidence)), now, now,
             json.dumps(provenance or [], ensure_ascii=False)),
        )
        return int(cur.lastrowid), True

    new_conf = max(float(row["confidence"]), float(confidence))
    old_aliases = json.loads(row["aliases"] or "[]")
    old_attr = json.loads(row["attributes"] or "{}")
    old_prov = json.loads(row["provenance"] or "[]")
    extra_aliases = list(aliases or [])
    if _norm(row["name"]) != norm and name not in extra_aliases:
        extra_aliases.append(name)   # 该写法成为新别名，记录在既有规范节点下
    merged_attr = _merge_attributes(old_attr, attributes or {}, provenance_key=name)
    merged_prov = _dedup_provenance(old_prov + (provenance or []))
    conn.execute(
        "UPDATE ontology_nodes SET aliases=?, attributes=?, confidence=?, last_seen_at=?, "
        "provenance=? WHERE node_id=?",
        (
            json.dumps(_merge_aliases(old_aliases, extra_aliases), ensure_ascii=False),
            json.dumps(merged_attr, ensure_ascii=False),
            new_conf, now,
            json.dumps(merged_prov, ensure_ascii=False),
            row["node_id"],
        ),
    )
    return int(row["node_id"]), False


def _dedup_provenance(prov: list) -> list:
    seen: set = set()
    out = []
    for p in prov:
        sig = json.dumps(p, ensure_ascii=False, sort_keys=True)
        if sig not in seen:
            seen.add(sig)
            out.append(p)
    return out


def upsert_edge(conn: sqlite3.Connection, *, relation_type: str,
                src_id: int, tgt_id: int, confidence: float,
                attributes: dict | None = None,
                provenance: list[dict] | None = None,
                evidence_tier: str | None = None) -> tuple[int, bool]:
    """插入或合并关系边，返回 (edge_id, is_new)。"""
    relation_type = canonical_relation_type(relation_type)
    ensure_relation_type(conn, relation_type)
    now = utcnow()
    row = conn.execute(
        "SELECT edge_id, confidence, provenance, evidence_tier FROM ontology_edges "
        "WHERE relation_type=? AND source_node=? AND target_node=?",
        (relation_type, src_id, tgt_id),
    ).fetchone()
    if not row:
        cur = conn.execute(
            "INSERT INTO ontology_edges(relation_type, source_node, target_node, "
            "attributes, confidence, created_at, provenance, evidence_tier) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (relation_type, src_id, tgt_id,
             json.dumps(attributes or {}, ensure_ascii=False),
             max(0.0, min(1.0, confidence)), now,
             json.dumps(provenance or [], ensure_ascii=False),
             evidence_tier),
        )
        return int(cur.lastrowid), True
    new_conf = max(float(row["confidence"]), float(confidence))
    old_prov = json.loads(row["provenance"] or "[]")
    merged_prov = _dedup_provenance(old_prov + (provenance or []))
    tier = evidence_tier or row["evidence_tier"]
    conn.execute(
        "UPDATE ontology_edges SET confidence=?, provenance=?, evidence_tier=? "
        "WHERE edge_id=?",
        (new_conf, json.dumps(merged_prov, ensure_ascii=False), tier, row["edge_id"]),
    )
    return int(row["edge_id"]), False


def get_node_id(conn: sqlite3.Connection, node_type: str, name: str) -> int | None:
    row = conn.execute(
        "SELECT node_id FROM ontology_nodes WHERE node_type=? AND normalized_name=?",
        (node_type, _norm(name)),
    ).fetchone()
    return int(row["node_id"]) if row else None


def record_ontology_run(conn: sqlite3.Connection, paper_key: str,
                        counts: dict, new_types: list[str]) -> int:
    version = meta_bump(conn, "ontology_instance_version")
    cur = conn.execute(
        "INSERT INTO ontology_runs(paper_key, counts, new_types, ontology_version, ran_at) "
        "VALUES(?,?,?,?,?)",
        (
            paper_key,
            json.dumps(counts, ensure_ascii=False),
            json.dumps(new_types, ensure_ascii=False),
            version, utcnow(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def strong_relation_set() -> set[str]:
    """强断言关系集合（来自 packs/skills/relation-lexicon）。

    保留函数形式而非模块级常量：词表可被 `RA_PACKS_DIR` 覆盖，
    运行期不应缓存成不可变快照。
    """
    return strong_relation_types()


def add_event_assertion(conn: sqlite3.Connection, *, paper_key: str,
                        event_type: str, trigger: str,
                        participants: list[str], entity_refs: list[int],
                        time_text: str | None = None,
                        attributes: dict | None = None,
                        confidence: float = 0.5,
                        provenance: list[dict] | None = None) -> int:
    """把事件写入旁路表（不再生成事件节点/星型 involves 边）。"""
    cur = conn.execute(
        "INSERT INTO event_assertions(paper_key, event_type, trigger, participants, "
        "entity_refs, time_text, attributes, confidence, provenance, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            paper_key, event_type, trigger,
            json.dumps(participants, ensure_ascii=False),
            json.dumps(entity_refs, ensure_ascii=False),
            time_text,
            json.dumps(attributes or {}, ensure_ascii=False),
            max(0.0, min(1.0, confidence)),
            json.dumps(provenance or [], ensure_ascii=False),
            utcnow(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def register_material(conn: sqlite3.Connection, preferred_name: str, *,
                      synonyms: list[str] | None = None,
                      composition: dict | None = None,
                      term_status: str = "local_uncurated",
                      curated_by: str | None = None,
                      notes: str | None = None) -> str:
    """本地材料登记（lcmat 命名空间）。返回 local_id。"""
    slug = re.sub(r"[^a-z0-9]+", "-", preferred_name.lower()).strip("-")[:60]
    local_id = f"lcmat:{slug or 'unnamed'}"
    now = utcnow()
    conn.execute(
        "INSERT INTO material_registry(local_id, preferred_name, synonyms, composition, "
        "term_status, curated_by, created_at, updated_at, notes) "
        "VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(local_id) DO UPDATE SET preferred_name=excluded.preferred_name, "
        "synonyms=excluded.synonyms, composition=excluded.composition, "
        "term_status=excluded.term_status, updated_at=excluded.updated_at, "
        "notes=excluded.notes",
        (
            local_id, preferred_name,
            json.dumps(synonyms or [], ensure_ascii=False),
            json.dumps(composition or {}, ensure_ascii=False),
            term_status, curated_by, now, now, notes,
        ),
    )
    conn.commit()
    return local_id


def queue_merge_candidate(conn: sqlite3.Connection, node_ids: list[int],
                          reason: str) -> int:
    cur = conn.execute(
        "INSERT INTO merge_candidates(node_ids, reason, status, created_at) "
        "VALUES(?,?,?,?)",
        (json.dumps(node_ids), reason, "open", utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_merge(conn: sqlite3.Connection, from_ids: list[int], to_id: int, *,
              rule_level: str, reason: str, operator: str = "auto") -> int:
    cur = conn.execute(
        "INSERT INTO merge_journal(from_ids, to_id, rule_level, reason, operator, "
        "created_at) VALUES(?,?,?,?,?,?)",
        (json.dumps(from_ids), to_id, rule_level, reason, operator, utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def queue_direction_flag(conn: sqlite3.Connection, *, edge_id: int,
                         relation_type: str, source_node: int, target_node: int,
                         suggestion: str) -> int:
    cur = conn.execute(
        "INSERT INTO direction_queue(edge_id, relation_type, source_node, target_node, "
        "suggestion, status, created_at) VALUES(?,?,?,?,?,?,?)",
        (edge_id, relation_type, source_node, target_node, suggestion, "open", utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)



def _json_obj(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_arr(value: Any) -> list:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _condition_rows(items: Any) -> list[dict[str, Any]]:
    if isinstance(items, dict):
        return [{"key": k, "value": v} for k, v in items.items()]
    return [x for x in (items or []) if isinstance(x, dict)]


def _measure_rows(items: Any) -> list[dict[str, Any]]:
    return [x for x in (items or []) if isinstance(x, dict)]


def _resolve_hyperedge_member(conn: sqlite3.Connection,
                              member: dict[str, Any]) -> int | None:
    if member.get("node_id") is not None:
        try:
            node_id = int(member["node_id"])
        except (TypeError, ValueError):
            node_id = 0
        row = conn.execute(
            "SELECT node_id FROM ontology_nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        if row:
            return int(row["node_id"])
    name = str(member.get("name") or "").strip()
    node_type = str(member.get("type") or member.get("node_type") or "").strip()
    if not name:
        return None
    if node_type:
        row = conn.execute(
            "SELECT node_id FROM ontology_nodes WHERE node_type=? AND normalized_name=?",
            (node_type, _norm(name)),
        ).fetchone()
        if row:
            return int(row["node_id"])
    row = conn.execute(
        "SELECT node_id FROM ontology_nodes WHERE normalized_name=? LIMIT 1",
        (_norm(name),),
    ).fetchone()
    return int(row["node_id"]) if row else None


def _hyperedge_fingerprint(hyperedge_type: str, label: str | None,
                           members: list[dict[str, Any]],
                           conditions: list[dict[str, Any]],
                           measurements: list[dict[str, Any]],
                           paper_key: str | None,
                           evidence: list[dict[str, Any]]) -> str:
    payload = {
        "type": hyperedge_type,
        "label": label or "",
        "paper": paper_key or "",
        "members": sorted([
            [int(m["node_id"]), str(m.get("role") or "")]
            for m in members if m.get("node_id") is not None
        ]),
        "conditions": sorted(json.dumps(x, sort_keys=True, ensure_ascii=False)
                             for x in conditions),
        "measurements": sorted(json.dumps(x, sort_keys=True, ensure_ascii=False)
                               for x in measurements),
        "evidence": sorted(json.dumps(x, sort_keys=True, ensure_ascii=False)
                           for x in evidence),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def upsert_hyperedge(conn: sqlite3.Connection, *, hyperedge_type: str,
                     label: str | None = None,
                     members: list[dict[str, Any]] | None = None,
                     conditions: Any = None,
                     measurements: Any = None,
                     confidence: float = 0.5,
                     evidence_tier: str | None = None,
                     paper_key: str | None = None,
                     provenance: list[dict] | None = None,
                     attributes: dict | None = None) -> tuple[int, bool]:
    """写入科研超边；不创建 Reaction/Event 节点，所有角色都存在成员表中。"""
    hyperedge_type = str(hyperedge_type or "claim").strip()
    resolved_members = []
    for pos, member in enumerate(members or []):
        if not isinstance(member, dict):
            continue
        node_id = _resolve_hyperedge_member(conn, member)
        if node_id is None:
            continue
        resolved_members.append({
            "node_id": node_id,
            "role": str(member.get("role") or "participant"),
            "position": int(member.get("position") or pos),
            "qualifiers": _json_obj(member.get("qualifiers")),
        })
    condition_rows = _condition_rows(conditions)
    measurement_rows = _measure_rows(measurements)
    evidence_rows = []
    for item in provenance or []:
        if isinstance(item, dict):
            evidence_rows.append({
                "paper_key": item.get("paper") or paper_key,
                "section": item.get("section"),
                "span_text": item.get("evidence") or item.get("span_text") or "",
                "char_start": item.get("char_start"),
                "char_end": item.get("char_end"),
            })
    fingerprint = _hyperedge_fingerprint(
        hyperedge_type, label, resolved_members, condition_rows,
        measurement_rows, paper_key, evidence_rows)
    row = conn.execute(
        "SELECT hyperedge_id, confidence, provenance FROM ontology_hyperedges "
        "WHERE fingerprint=?", (fingerprint,)
    ).fetchone()
    now = utcnow()
    if row:
        hyperedge_id = int(row["hyperedge_id"])
        merged_prov = _dedup_provenance(
            _json_arr(row["provenance"]) + (provenance or []))
        conn.execute(
            "UPDATE ontology_hyperedges SET confidence=?, provenance=? "
            "WHERE hyperedge_id=?",
            (max(float(row["confidence"] or 0), float(confidence)),
             json.dumps(merged_prov, ensure_ascii=False), hyperedge_id),
        )
        is_new = False
    else:
        cur = conn.execute(
            "INSERT INTO ontology_hyperedges("
            "hyperedge_type, fingerprint, label, attributes, confidence, "
            "evidence_tier, paper_key, created_at, provenance"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                hyperedge_type, fingerprint, label,
                json.dumps(attributes or {}, ensure_ascii=False),
                max(0.0, min(1.0, float(confidence))),
                evidence_tier or "unclassified", paper_key, now,
                json.dumps(provenance or [], ensure_ascii=False),
            ),
        )
        hyperedge_id = int(cur.lastrowid)
        is_new = True
    for m in resolved_members:
        conn.execute(
            "INSERT OR IGNORE INTO ontology_hyperedge_members("
            "hyperedge_id, node_id, role, position, qualifiers"
            ") VALUES(?,?,?,?,?)",
            (hyperedge_id, m["node_id"], m["role"], m["position"],
             json.dumps(m["qualifiers"], ensure_ascii=False)),
        )
    for c in condition_rows:
        value = c.get("value", c.get("value_text"))
        value_num = c.get("value_num")
        if value_num is None and isinstance(value, (int, float)):
            value_num = float(value)
        conn.execute(
            "INSERT OR IGNORE INTO ontology_hyperedge_conditions("
            "hyperedge_id, condition_key, operator, value_text, value_num, unit, qualifier"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                hyperedge_id, str(c.get("key") or c.get("condition_key") or ""),
                c.get("operator"), None if value is None else str(value),
                value_num, c.get("unit"), c.get("qualifier"),
            ),
        )
    for m in measurement_rows:
        value = m.get("value", m.get("value_text"))
        num = m.get("value_num")
        if num is None and isinstance(value, (int, float)):
            num = float(value)
        conn.execute(
            "INSERT OR IGNORE INTO ontology_hyperedge_measurements("
            "hyperedge_id, metric, value_text, value_num, unit, qualifier, subject_node"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                hyperedge_id, str(m.get("metric") or m.get("name") or ""),
                None if value is None else str(value), num, m.get("unit"),
                m.get("qualifier"), m.get("subject_node"),
            ),
        )
    for e in evidence_rows:
        conn.execute(
            "INSERT OR IGNORE INTO ontology_hyperedge_evidence("
            "hyperedge_id, paper_key, section, span_text, char_start, char_end"
            ") VALUES(?,?,?,?,?,?)",
            (
                hyperedge_id, e.get("paper_key") or paper_key, e.get("section"),
                e.get("span_text") or "", e.get("char_start"), e.get("char_end"),
            ),
        )
    conn.commit()
    return hyperedge_id, is_new


def list_hyperedge_briefs(conn: sqlite3.Connection, *,
                          min_confidence: float = 0.0,
                          hyperedge_types: list[str] | None = None,
                          limit: int = 0) -> list[dict[str, Any]]:
    """轻量超边索引：只取打分所需字段，供“先筛选、后加载”使用。

    真实语料里超边可达数千条，逐条联表加载成员/条件/证据代价很高。
    本函数用两次查询取回全部候选的 (编号, 类型, 标签, 置信度) 与成员名，
    供调用方在内存里做相关性排序，再对入选子集调用 ``load_hyperedges()``。
    """
    sql = ("SELECT hyperedge_id, hyperedge_type, label, confidence, paper_key "
           "FROM ontology_hyperedges WHERE confidence>=?")
    params: list[Any] = [float(min_confidence)]
    if hyperedge_types:
        sql += " AND hyperedge_type IN (" + ",".join("?" * len(hyperedge_types)) + ")"
        params.extend(hyperedge_types)
    sql += " ORDER BY confidence DESC, hyperedge_id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))
    rows = conn.execute(sql, params).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        out[int(r["hyperedge_id"])] = {
            "hyperedge_id": int(r["hyperedge_id"]),
            "hyperedge_type": r["hyperedge_type"],
            "label": r["label"] or "",
            "confidence": round(float(r["confidence"] or 0), 3),
            "paper_key": r["paper_key"],
            "member_names": [],
        }
    if not out:
        return []
    ids = list(out)
    placeholders = ",".join("?" * len(ids))
    member_rows = conn.execute(
        "SELECT m.hyperedge_id, m.position, n.name FROM ontology_hyperedge_members m "
        "JOIN ontology_nodes n ON n.node_id=m.node_id "
        f"WHERE m.hyperedge_id IN ({placeholders}) "
        "ORDER BY m.hyperedge_id, m.position, m.id",
        ids,
    ).fetchall()
    for mr in member_rows:
        brief = out.get(int(mr["hyperedge_id"]))
        if brief is not None and mr["name"]:
            brief["member_names"].append(str(mr["name"]))
    return list(out.values())


def load_hyperedges(conn: sqlite3.Connection,
                    hyperedge_ids: list[int]) -> list[dict[str, Any]]:
    """按编号批量完整加载超边（成员/条件/测量/证据），固定 5 次查询。"""
    ids = [int(x) for x in hyperedge_ids]
    if not ids:
        return []

    def fetch_in(select_and_from: str, tail: str = "") -> list[sqlite3.Row]:
        """按 400 个一组执行 IN 查询，避免 SQLite 变量数上限。"""
        collected: list[sqlite3.Row] = []
        for i in range(0, len(ids), 400):
            part = ids[i:i + 400]
            sql = (select_and_from + " IN (" + ",".join("?" * len(part)) + ")" + tail)
            collected.extend(conn.execute(sql, part).fetchall())
        return collected

    base = {int(r["hyperedge_id"]): dict(r) for r in fetch_in(
        "SELECT hyperedge_id, hyperedge_type, label, attributes, confidence, "
        "evidence_tier, paper_key, created_at, provenance "
        "FROM ontology_hyperedges WHERE hyperedge_id")}
    members: dict[int, list[dict[str, Any]]] = {}
    for r in fetch_in(
            "SELECT m.hyperedge_id, m.node_id, m.role, m.position, m.qualifiers, "
            "n.name, n.node_type, n.attributes "
            "FROM ontology_hyperedge_members m "
            "JOIN ontology_nodes n ON n.node_id=m.node_id "
            "WHERE m.hyperedge_id", " ORDER BY m.hyperedge_id, m.position, m.id"):
        members.setdefault(int(r["hyperedge_id"]), []).append(dict(r))
    conditions: dict[int, list[dict[str, Any]]] = {}
    for r in fetch_in(
            "SELECT hyperedge_id, condition_key, operator, value_text, value_num, "
            "unit, qualifier FROM ontology_hyperedge_conditions "
            "WHERE hyperedge_id", " ORDER BY hyperedge_id, id"):
        conditions.setdefault(int(r["hyperedge_id"]), []).append(dict(r))
    measurements: dict[int, list[dict[str, Any]]] = {}
    for r in fetch_in(
            "SELECT hyperedge_id, metric, value_text, value_num, unit, qualifier, "
            "subject_node FROM ontology_hyperedge_measurements "
            "WHERE hyperedge_id", " ORDER BY hyperedge_id, id"):
        measurements.setdefault(int(r["hyperedge_id"]), []).append(dict(r))
    evidence: dict[int, list[dict[str, Any]]] = {}
    for r in fetch_in(
            "SELECT hyperedge_id, paper_key, section, span_text, char_start, char_end "
            "FROM ontology_hyperedge_evidence WHERE hyperedge_id",
            " ORDER BY hyperedge_id, id"):
        evidence.setdefault(int(r["hyperedge_id"]), []).append(dict(r))

    out = []
    for hid in ids:
        row = base.get(hid)
        if row is None:
            continue
        for key in ("attributes", "provenance"):
            try:
                row[key] = json.loads(row.get(key) or ("[]" if key == "provenance" else "{}"))
            except json.JSONDecodeError:
                row[key] = [] if key == "provenance" else {}
        row_members = members.get(hid, [])
        for m in row_members:
            for key in ("qualifiers", "attributes"):
                try:
                    m[key] = json.loads(m.get(key) or "{}")
                except json.JSONDecodeError:
                    m[key] = {}
        row.update({"members": row_members, "conditions": conditions.get(hid, []),
                    "measurements": measurements.get(hid, []),
                    "evidence": evidence.get(hid, [])})
        out.append(row)
    return out


def list_hyperedges(conn: sqlite3.Connection, *, limit: int = 500,
                    min_confidence: float = 0.0,
                    node_ids: set[int] | None = None,
                    hyperedge_types: list[str] | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT hyperedge_id, hyperedge_type, label, attributes, confidence, "
           "evidence_tier, paper_key, created_at, provenance "
           "FROM ontology_hyperedges WHERE confidence>=?")
    params: list[Any] = [float(min_confidence)]
    if hyperedge_types:
        sql += " AND hyperedge_type IN (" + ",".join("?" * len(hyperedge_types)) + ")"
        params.extend(hyperedge_types)
    sql += " ORDER BY confidence DESC, hyperedge_id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    rows = conn.execute(sql, params).fetchall()
    # P2-7：原先每个超边 4 次子表查询（N+1）。改为一次批量加载（固定 5 次查询），
    # 顺序与 confidence DESC 保持一致；node_ids 过滤语义与原先相同（先 LIMIT 后过滤）。
    loaded = {int(d["hyperedge_id"]): d
              for d in load_hyperedges(conn, [int(r["hyperedge_id"]) for r in rows])}
    out = []
    for r in rows:
        d = loaded.get(int(r["hyperedge_id"]))
        if d is None:
            continue
        if node_ids is not None and not any(
                int(m["node_id"]) in node_ids for m in d.get("members", [])):
            continue
        out.append(d)
    return out


_LOCAL_LABEL_RE = re.compile(
    r'^\s*(?:compound|compd|product|intermediate|entry|item|stage|step|substrate|analyte)'
    r'\s*[A-Za-z]{0,4}\d+[A-Za-z]{0,4}\s*$',
    re.IGNORECASE,
)
_PURE_CODE_RE = re.compile(
    r'^\s*\(?[A-Za-z]{0,4}\d+[A-Za-z]{0,4}\)?\s*$')

_SEMANTIC_DOMAIN_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("ynamides", ("ynamide", "ynamides", "ynamide-", "ynamide")),
    ("nitrogen heterocycles", (
        "benzimidazole", "imidazole", "indole", "indazole", "pyridine",
        "pyrimidine", "pyrazine", "pyridazine", "triazine", "triazole",
        "pyrazole", "pyrrole", "quinoline", "isoquinoline", "diazepine",
        "azepine", "piperazine", "oxazole", "thiazole", "azacycle",
    )),
    ("catalysis and catalysts", (
        "catalyst", "catalysis", "catalytic", "palladium", "copper",
        "gold", "rhodium", "nickel", "iridium", "cobalt", "scandium",
    )),
    ("disease and clinical outcomes", (
        "disease", "syndrome", "cancer", "carcinoma", "infection",
        "disorder", "clinical outcome",
    )),
    ("drugs and interventions", (
        "drug", "inhibitor", "therapy", "treatment", "medication",
        "intervention",
    )),
    ("proteins and genes", (
        "protein", "gene", "kinase", "receptor", "enzyme", "transcription factor",
    )),
    ("materials and composites", (
        "material", "polymer", "ceramic", "composite", "hydrogel",
        "scaffold", "nanoparticle", "nanomaterial", "coating",
    )),
    ("machine learning methods", (
        "neural network", "transformer", "algorithm", "classifier",
        "large language model", "graph neural network",
    )),
    ("datasets and benchmarks", (
        "dataset", "benchmark", "corpus", "evaluation suite",
    )),
    ("social institutions and policies", (
        "institution", "policy", "government", "organization",
        "population", "public administration",
    )),
]

_DOMAIN_PARENT_BLACKLIST = {
    "chemical", "chemicals", "compound", "compounds", "substance",
    "material", "materials", "method", "methods", "concept", "entity",
    "process", "thing", "item", "product", "products", "event",
}



_LOCAL_CODE_IN_TEXT_RE = re.compile(
    r'\b(compound|compd|product|intermediate|entry|item|stage|step|substrate|analyte)'
    r'\s*[A-Za-z]{0,4}\d+[A-Za-z]{0,4}\b',
    re.IGNORECASE,
)



_GENERIC_LOCAL_BASES = {
    "compound", "compd", "product", "intermediate", "entry", "item",
    "stage", "step", "substrate", "analyte", "derivative", "analog",
}


def strip_local_reference_codes(name: str) -> str:
    text = str(name or '').strip()
    text = _LOCAL_CODE_IN_TEXT_RE.sub(lambda m: m.group(1), text)
    return re.sub(r'\s+', ' ', text).strip()


def is_local_reference_label(name: str) -> bool:
    n = str(name or "").strip()
    return bool(n and (_LOCAL_LABEL_RE.match(n) or _PURE_CODE_RE.match(n)))


def descriptive_alias(aliases: list[str]) -> str | None:
    for alias in aliases or []:
        a = str(alias or "").strip()
        if len(a) < 5 or is_local_reference_label(a):
            continue
        stripped = re.sub(
            r'\s+[A-Za-z]{0,4}\d+[A-Za-z]{0,4}$', '', a).strip()
        if len(stripped) >= 5 and not is_local_reference_label(stripped):
            return stripped
    return None


def _merge_nodes_simple(conn: sqlite3.Connection, keep_id: int,
                        drop_id: int, reason: str) -> None:
    if keep_id == drop_id:
        return
    rows = conn.execute(
        "SELECT * FROM ontology_edges WHERE source_node=? OR target_node=?",
        (drop_id, drop_id),
    ).fetchall()
    for row in rows:
        src = keep_id if int(row["source_node"]) == drop_id else int(row["source_node"])
        tgt = keep_id if int(row["target_node"]) == drop_id else int(row["target_node"])
        if src == tgt:
            continue
        existing = conn.execute(
            "SELECT edge_id, provenance, confidence FROM ontology_edges "
            "WHERE relation_type=? AND source_node=? AND target_node=? AND edge_id<>?",
            (row["relation_type"], src, tgt, row["edge_id"]),
        ).fetchone()
        if existing:
            try:
                old = json.loads(existing["provenance"] or "[]")
                new = json.loads(row["provenance"] or "[]")
            except json.JSONDecodeError:
                old, new = [], []
            conn.execute(
                "UPDATE ontology_edges SET provenance=?, confidence=? WHERE edge_id=?",
                (json.dumps(_dedup_provenance(old + new), ensure_ascii=False),
                 max(float(existing["confidence"] or 0), float(row["confidence"] or 0)),
                 int(existing["edge_id"])),
            )
            conn.execute("DELETE FROM ontology_edges WHERE edge_id=?", (int(row["edge_id"]),))
        else:
            conn.execute(
                "UPDATE ontology_edges SET source_node=?, target_node=? WHERE edge_id=?",
                (src, tgt, int(row["edge_id"])),
            )
    conn.execute(
        "UPDATE OR IGNORE ontology_hyperedge_members SET node_id=? WHERE node_id=?",
        (keep_id, drop_id),
    )
    conn.execute("DELETE FROM ontology_hyperedge_members WHERE node_id=?", (drop_id,))
    for ev in conn.execute("SELECT id, entity_refs FROM event_assertions").fetchall():
        refs = []
        try:
            refs = json.loads(ev["entity_refs"] or "[]")
        except json.JSONDecodeError:
            refs = []
        out = []
        for ref in refs:
            val = keep_id if int(ref) == drop_id else int(ref)
            if val not in out:
                out.append(val)
        if out != refs:
            conn.execute("UPDATE event_assertions SET entity_refs=? WHERE id=?",
                         (json.dumps(out), int(ev["id"])))
    log_merge(conn, [drop_id], keep_id, rule_level="local_label",
              reason=reason, operator="cleanup")
    conn.execute("DELETE FROM ontology_nodes WHERE node_id=?", (drop_id,))


def cleanup_local_label_nodes(conn: sqlite3.Connection) -> dict[str, int]:
    """清理 Compound 32、5a、纯编号等论文内部临时标识节点。"""
    renamed = merged = dropped = 0
    rows = conn.execute(
        "SELECT node_id,node_type,name,aliases FROM ontology_nodes ORDER BY node_id"
    ).fetchall()
    for row in rows:
        name = str(row["name"] or "").strip()
        cleaned_name = strip_local_reference_codes(name)
        if (cleaned_name != name and len(cleaned_name) >= 5
                and cleaned_name.lower() not in _GENERIC_LOCAL_BASES
                and not is_local_reference_label(cleaned_name)):
            try:
                aliases = json.loads(row["aliases"] or "[]")
            except json.JSONDecodeError:
                aliases = []
            aliases = _merge_aliases(aliases, [name])
            norm_clean = _norm(cleaned_name)
            existing = conn.execute(
                "SELECT node_id FROM ontology_nodes WHERE node_type=? "
                "AND normalized_name=? AND node_id<>?",
                (row["node_type"], norm_clean, int(row["node_id"])),
            ).fetchone()
            if existing:
                _merge_nodes_simple(conn, int(existing["node_id"]),
                                    int(row["node_id"]), f"{name} -> {cleaned_name}")
                merged += 1
            else:
                conn.execute(
                    "UPDATE ontology_nodes SET name=?, normalized_name=?, aliases=? WHERE node_id=?",
                    (cleaned_name, norm_clean,
                     json.dumps(aliases, ensure_ascii=False), int(row["node_id"])),
                )
                renamed += 1
            continue
        if not is_local_reference_label(name):
            continue
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except json.JSONDecodeError:
            aliases = []
        preferred = descriptive_alias(aliases)
        node_id = int(row["node_id"])
        if not preferred:
            conn.execute("DELETE FROM ontology_edges WHERE source_node=? OR target_node=?", (node_id, node_id))
            conn.execute("DELETE FROM ontology_hyperedge_members WHERE node_id=?", (node_id,))
            conn.execute("DELETE FROM ontology_nodes WHERE node_id=?", (node_id,))
            dropped += 1
            continue
        norm = _norm(preferred)
        existing = conn.execute(
            "SELECT node_id FROM ontology_nodes WHERE node_type=? AND normalized_name=? AND node_id<>?",
            (row["node_type"], norm, node_id),
        ).fetchone()
        if existing:
            _merge_nodes_simple(conn, int(existing["node_id"]), node_id,
                                f"local label {name} -> {preferred}")
            merged += 1
        else:
            extra_aliases = _merge_aliases(aliases, [name])
            conn.execute(
                "UPDATE ontology_nodes SET name=?, normalized_name=?, aliases=? WHERE node_id=?",
                (preferred, norm, json.dumps(extra_aliases, ensure_ascii=False), node_id),
            )
            renamed += 1
    for node in conn.execute(
            "SELECT node_id,name,aliases FROM ontology_nodes").fetchall():
        try:
            aliases = json.loads(node["aliases"] or "[]")
        except json.JSONDecodeError:
            aliases = []
        cleaned_aliases = []
        for alias in aliases:
            a = str(alias or "").strip()
            if not a or is_local_reference_label(a):
                continue
            stripped = strip_local_reference_codes(a)
            if stripped.lower() in _GENERIC_LOCAL_BASES:
                continue
            a = stripped
            if a and a != node["name"] and a not in cleaned_aliases:
                cleaned_aliases.append(a)
        if cleaned_aliases != aliases:
            conn.execute(
                "UPDATE ontology_nodes SET aliases=? WHERE node_id=?",
                (json.dumps(cleaned_aliases, ensure_ascii=False),
                 int(node["node_id"])),
            )
    orphan_rows = conn.execute(
        "SELECT hyperedge_id FROM ontology_hyperedges h "
        "WHERE NOT EXISTS (SELECT 1 FROM ontology_hyperedge_members m "
        "WHERE m.hyperedge_id=h.hyperedge_id)"
    ).fetchall()
    orphan_ids = [int(r["hyperedge_id"]) for r in orphan_rows]
    for hyperedge_id in orphan_ids:
        for table in ("ontology_hyperedge_members", "ontology_hyperedge_conditions",
                      "ontology_hyperedge_measurements", "ontology_hyperedge_evidence",
                      "ontology_hyperedge_cluster_members", "ontology_channel_hyperedges"):
            conn.execute(f"DELETE FROM {table} WHERE hyperedge_id=?", (hyperedge_id,))
        conn.execute("DELETE FROM ontology_hyperedges WHERE hyperedge_id=?",
                     (hyperedge_id,))
    conn.commit()
    return {"renamed": renamed, "merged": merged, "dropped": dropped,
            "dropped_hyperedges": len(orphan_ids)}


def _hierarchy_parent_names(conn: sqlite3.Connection) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    rows = conn.execute(
        "SELECT e.source_node, n.name FROM ontology_edges e "
        "JOIN ontology_nodes n ON n.node_id=e.target_node "
        "WHERE e.relation_type IN ('is_a','part_of')"
    ).fetchall()
    for r in rows:
        name = str(r["name"] or "").strip()
        if not name or is_local_reference_label(name) or name.lower() in _DOMAIN_PARENT_BLACKLIST:
            continue
        out.setdefault(int(r["source_node"]), []).append(name)
    return out


def _semantic_labels_for_node(name: str, aliases: list[str],
                              parents: list[str]) -> set[str]:
    text = " ".join([name] + aliases).lower()
    labels = set(parents)
    for domain, keywords in _SEMANTIC_DOMAIN_RULES:
        if any(keyword in text for keyword in keywords):
            labels.add(domain)
    return labels


def rebuild_ontology_views(conn: sqlite3.Connection) -> dict[str, Any]:
    """重建语义节点域和关系通道；域是聚合视图，不是新节点。"""
    conn.execute("DELETE FROM ontology_domain_members WHERE domain_key LIKE 'auto-%'")
    conn.execute("DELETE FROM ontology_domains WHERE domain_key LIKE 'auto-%'")
    parents = _hierarchy_parent_names(conn)
    node_rows = conn.execute(
        "SELECT node_id,node_type,name,aliases,confidence FROM ontology_nodes"
    ).fetchall()
    domains: dict[str, dict[str, Any]] = {}
    for r in node_rows:
        try:
            aliases = json.loads(r["aliases"] or "[]")
        except json.JSONDecodeError:
            aliases = []
        labels = _semantic_labels_for_node(
            str(r["name"] or ""), aliases, parents.get(int(r["node_id"]), []))
        for label in labels:
            key = "auto-semantic:" + hashlib.sha1(label.encode("utf-8")).hexdigest()[:20]
            d = domains.setdefault(key, {"label": label, "members": []})
            d["members"].append((int(r["node_id"]), float(r["confidence"] or 0.5)))
    for key, d in domains.items():
        conn.execute(
            "INSERT INTO ontology_domains(domain_key,label,domain_type,description,attributes) "
            "VALUES(?,?,?,?,?)",
            (key, d["label"], "semantic_cluster",
             f"语义相近实体域：{d['label']}", "{}"),
        )
        for node_id, confidence in d["members"]:
            conn.execute(
                "INSERT OR IGNORE INTO ontology_domain_members(domain_key,node_id,weight,confidence) "
                "VALUES(?,?,?,?)",
                (key, node_id, 1.0, confidence),
            )
    edges = list_hyperedges(conn, limit=100000, min_confidence=0.0)
    conn.execute("DELETE FROM ontology_channel_hyperedges WHERE channel_key LIKE 'auto:%'")
    conn.execute("DELETE FROM ontology_relation_channels WHERE channel_key LIKE 'auto:%'")
    channels: dict[str, dict[str, Any]] = {}
    for edge in edges:
        role_sig = sorted([
            f"{m.get('role')}:{m.get('node_type')}" for m in edge["members"]
        ])
        key_raw = json.dumps([edge["hyperedge_type"], role_sig], ensure_ascii=False, sort_keys=True)
        key = "auto:" + hashlib.sha1(key_raw.encode("utf-8")).hexdigest()[:20]
        ch = channels.setdefault(key, {
            "relation_family": edge["hyperedge_type"],
            "role_profile": {},
            "papers": set(),
            "hyperedges": [],
            "confidence": [],
        })
        for role, node_type in [x.split(":", 1) for x in role_sig if ":" in x]:
            ch["role_profile"].setdefault(role, [])
            if node_type not in ch["role_profile"][role]:
                ch["role_profile"][role].append(node_type)
        if edge.get("paper_key"):
            ch["papers"].add(edge["paper_key"])
        ch["hyperedges"].append(edge["hyperedge_id"])
        ch["confidence"].append(float(edge.get("confidence") or 0.5))
    for key, ch in channels.items():
        conn.execute(
            "INSERT INTO ontology_relation_channels("
            "channel_key, relation_family, role_profile, source_domains, "
            "target_domains, support_count, paper_count, confidence, summary"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                key, ch["relation_family"],
                json.dumps(ch["role_profile"], ensure_ascii=False),
                json.dumps([], ensure_ascii=False),
                json.dumps([], ensure_ascii=False),
                len(ch["hyperedges"]), len(ch["papers"]),
                sum(ch["confidence"]) / max(1, len(ch["confidence"])),
                f"{ch['relation_family']} 角色通道",
            ),
        )
        for hyperedge_id in ch["hyperedges"]:
            conn.execute(
                "INSERT OR IGNORE INTO ontology_channel_hyperedges("
                "channel_key, hyperedge_id, weight) VALUES(?,?,?)",
                (key, hyperedge_id, 1.0),
            )
    conn.commit()
    return {
        "domains": len(domains),
        "domain_members": sum(len(d["members"]) for d in domains.values()),
        "channels": len(channels),
        "hyperedges": len(edges),
    }


def backfill_hyperedges_from_legacy(conn: sqlite3.Connection) -> dict[str, int]:
    """把旧 relation/event 数据投影为兼容超边，不创建额外节点。"""
    created = 0
    relations = conn.execute(
        "SELECT edge_id, relation_type, source_node, target_node, attributes, "
        "confidence, provenance, evidence_tier FROM ontology_edges"
    ).fetchall()
    for r in relations:
        try:
            prov = json.loads(r["provenance"] or "[]")
        except json.JSONDecodeError:
            prov = []
        evidence = []
        paper_keys = []
        for p in prov:
            if not isinstance(p, dict):
                continue
            if p.get("paper") and p["paper"] not in paper_keys:
                paper_keys.append(p["paper"])
            evidence.append({
                "paper": p.get("paper"),
                "section": p.get("section"),
                "evidence": p.get("evidence") or "",
            })
        _, is_new = upsert_hyperedge(
            conn,
            hyperedge_type="relation",
            label=r["relation_type"],
            members=[
                {"node_id": int(r["source_node"]), "role": "subject"},
                {"node_id": int(r["target_node"]), "role": "object"},
            ],
            conditions={},
            measurements=[],
            confidence=float(r["confidence"] or 0.5),
            evidence_tier=r["evidence_tier"],
            paper_key=paper_keys[0] if paper_keys else None,
            provenance=evidence,
            attributes={"legacy_edge_id": int(r["edge_id"])},
        )
        created += int(is_new)
    events = conn.execute(
        "SELECT id, paper_key, event_type, trigger, entity_refs, time_text, "
        "attributes, confidence, provenance FROM event_assertions"
    ).fetchall()
    for ev in events:
        try:
            refs = json.loads(ev["entity_refs"] or "[]")
        except json.JSONDecodeError:
            refs = []
        try:
            attrs = json.loads(ev["attributes"] or "{}")
        except json.JSONDecodeError:
            attrs = {}
        try:
            prov = json.loads(ev["provenance"] or "[]")
        except json.JSONDecodeError:
            prov = []
        _, is_new = upsert_hyperedge(
            conn,
            hyperedge_type="event",
            label=ev["trigger"] or ev["event_type"],
            members=[{"node_id": int(x), "role": "participant"} for x in refs],
            conditions={"time": ev["time_text"]} if ev["time_text"] else {},
            measurements=[],
            confidence=float(ev["confidence"] or 0.5),
            paper_key=ev["paper_key"],
            provenance=prov,
            attributes=attrs,
        )
        created += int(is_new)
    rebuild_ontology_views(conn)
    return {"created": created, "relations": len(relations), "events": len(events)}


def graph_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    n_nodes = conn.execute("SELECT COUNT(*) AS c FROM ontology_nodes").fetchone()["c"]
    n_edges = conn.execute("SELECT COUNT(*) AS c FROM ontology_edges").fetchone()["c"]
    n_types = conn.execute("SELECT COUNT(*) AS c FROM ontology_type_registry").fetchone()["c"]
    node_by_type = {
        r["node_type"]: r["c"]
        for r in conn.execute(
            "SELECT node_type, COUNT(*) AS c FROM ontology_nodes GROUP BY node_type"
        )
    }
    edge_by_type = {
        r["relation_type"]: r["c"]
        for r in conn.execute(
            "SELECT relation_type, COUNT(*) AS c FROM ontology_edges GROUP BY relation_type"
        )
    }
    try:
        n_hyperedges = conn.execute(
            "SELECT COUNT(*) AS c FROM ontology_hyperedges").fetchone()["c"]
        n_domains = conn.execute(
            "SELECT COUNT(*) AS c FROM ontology_domains").fetchone()["c"]
        n_channels = conn.execute(
            "SELECT COUNT(*) AS c FROM ontology_relation_channels").fetchone()["c"]
    except sqlite3.OperationalError:
        n_hyperedges = n_domains = n_channels = 0
    return {
        "nodes": n_nodes,
        "edges": n_edges,
        "hyperedges": n_hyperedges,
        "domains": n_domains,
        "channels": n_channels,
        "types": n_types,
        "node_by_type": node_by_type,
        "edge_by_type": edge_by_type,
        "schema_version": meta_get(conn, "ontology_schema_version", 1),
        "instance_version": meta_get(conn, "ontology_instance_version", 0),
    }
