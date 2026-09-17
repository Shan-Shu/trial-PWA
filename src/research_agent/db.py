"""本地数据库：论文元数据、PDF BLOB、清洗文本、质量评估、处理日志。

采用 Python 标准库 sqlite3，零额外依赖。动态本体图存储见 ontology.store。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from research_agent.config import Settings, settings as default_settings


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS papers (
    paper_key        TEXT PRIMARY KEY,
    source           TEXT,
    title            TEXT,
    abstract         TEXT,
    doi              TEXT,
    venue            TEXT,
    venue_issn       TEXT,
    pmcid            TEXT,              -- PubMed Central ID（PubMed 源全文）
    fulltext_source  TEXT,              -- pdf / xml(EuropePMC) / abstract
    source_type      TEXT,            -- journal / repository / proceedings ...
    pub_year         INTEGER,
    pub_date         TEXT,
    publication_status TEXT,          -- 发表情况：如 "Published"/"Preprint"
    citation_count   INTEGER,
    avg_h_index      REAL,
    authors_meta     TEXT DEFAULT '[]',
    volume           TEXT,
    issue            TEXT,
    pages            TEXT,
    keywords         TEXT,
    publisher        TEXT,
    language         TEXT,
    pdf_sha256       TEXT,
    pdf_size         INTEGER,
    pdf_blob         BLOB,
    clean_text       TEXT,
    clean_text_sha256 TEXT,
    status           TEXT DEFAULT 'raw',
    created_at       TEXT,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(pub_year);

CREATE TABLE IF NOT EXISTS quality_results (
    paper_key        TEXT PRIMARY KEY REFERENCES papers(paper_key) ON DELETE CASCADE,
    venue_factor     REAL,
    h_factor         REAL,
    citation_factor  REAL,
    authority        REAL,
    timeliness       REAL,
    quality          REAL,
    decision         TEXT,
    needs_review     INTEGER DEFAULT 0,
    meta_missing     TEXT DEFAULT '[]',
    rationale        TEXT,
    assessed_at      TEXT
);

CREATE TABLE IF NOT EXISTS human_reviews (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_key      TEXT,
    action         TEXT,
    decision       TEXT,
    rationale      TEXT,
    custom_result  TEXT,
    reviewed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_human_reviews_paper
    ON human_reviews(paper_key);

CREATE TABLE IF NOT EXISTS processing_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_key  TEXT,
    node       TEXT,
    event      TEXT,
    details    TEXT,
    ts         TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_paper ON processing_log(paper_key);

CREATE TABLE IF NOT EXISTS study_runs (
    run_id      TEXT,
    round       INTEGER,
    request     TEXT,
    status      TEXT,
    decision    TEXT,
    plan        TEXT,
    consumer    TEXT,
    draft       TEXT,
    review      TEXT,
    fact_check  TEXT,
    ts          TEXT,
    PRIMARY KEY (run_id, round)
);
CREATE INDEX IF NOT EXISTS idx_study_runs_ts ON study_runs(ts);

-- ---------------------------------------------------------------- 文献库侧车表
-- 来源：paper_writing_assistant 的 library/store.py，合并时补齐了
-- FOREIGN KEY + ON DELETE CASCADE（来源版本无外键，绕过主流程即留孤儿行），
-- 并把主键从 INTEGER id 改为 papers.paper_key。
CREATE TABLE IF NOT EXISTS paper_folders (
    folder_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_folder_map (
    folder_id  INTEGER NOT NULL REFERENCES paper_folders(folder_id) ON DELETE CASCADE,
    paper_key  TEXT    NOT NULL REFERENCES papers(paper_key)        ON DELETE CASCADE,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (folder_id, paper_key)
);

CREATE TABLE IF NOT EXISTS paper_tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_key  TEXT NOT NULL REFERENCES papers(paper_key) ON DELETE CASCADE,
    tag        TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (paper_key, tag)
);

CREATE TABLE IF NOT EXISTS paper_favorites (
    paper_key  TEXT PRIMARY KEY REFERENCES papers(paper_key) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tags_tag         ON paper_tags(tag);
CREATE INDEX IF NOT EXISTS idx_tags_paper       ON paper_tags(paper_key);
CREATE INDEX IF NOT EXISTS idx_folder_map_paper ON paper_folder_map(paper_key);

-- ---------------------------------------------------------------- 写作台
-- 来源：paper_writing_assistant 的 writing/store.py；章节骨架不再硬编码，
-- 改由 packs/skills/writing 提供（合并方案 v2 决策 D8）。
CREATE TABLE IF NOT EXISTS writing_projects (
    project_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    topic        TEXT DEFAULT '',
    genre        TEXT DEFAULT 'research_article',
    language     TEXT DEFAULT 'zh',
    outline_json TEXT DEFAULT '[]',
    plan_json    TEXT DEFAULT '{}',   -- 工作规划（唯一规划节点的产物）
    status       TEXT DEFAULT 'draft',
    created_at   TEXT NOT NULL,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS writing_sections (
    section_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES writing_projects(project_id)
                    ON DELETE CASCADE,
    section_key     TEXT NOT NULL,
    heading         TEXT NOT NULL,
    content         TEXT DEFAULT '',
    citation_ids    TEXT DEFAULT '[]',
    status          TEXT DEFAULT 'draft',
    created_at      TEXT NOT NULL,
    updated_at      TEXT,
    UNIQUE (project_id, section_key)
);

CREATE INDEX IF NOT EXISTS idx_writing_sections_project
    ON writing_sections(project_id);

-- 章节级工作流运行记录：供审计"这一段为什么这么写"（与 study_runs 同构）
-- 每一轮（规划 → 充分性判定 → 补检 → 消费 → 生成）都写一行。
CREATE TABLE IF NOT EXISTS section_runs (
    run_id            TEXT,
    round             INTEGER,
    project_id        INTEGER NOT NULL REFERENCES writing_projects(project_id)
                      ON DELETE CASCADE,
    section_key       TEXT NOT NULL,
    instruction       TEXT,
    stage             TEXT,          -- planning / sufficiency / collecting / consuming / composing / done / failed
    decision          TEXT,          -- sufficient / insufficient / exhausted
    template_key      TEXT,          -- 该部分用的模板（部分 = 固定模板）
    field_source_json TEXT,          -- 每个字段的来源（user / plan / template）
    unmet_json        TEXT,          -- 未满足的必考维度（允许带缺口写作时用于标注）
    plan_json         TEXT,
    sufficiency_json  TEXT,
    collection_json   TEXT,
    content_chars     INTEGER DEFAULT 0,
    generated_by      TEXT,
    error             TEXT,
    ts                TEXT NOT NULL,
    PRIMARY KEY (run_id, round)
);
CREATE INDEX IF NOT EXISTS idx_section_runs_lookup
    ON section_runs(project_id, section_key);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """打开数据库连接并确保 schema 存在。"""
    path = Path(db_path) if db_path else default_settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=20000")
    conn.executescript(SCHEMA)
    # 老库迁移：补充新增列（若缺）
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)")}
    for col, decl in (
        ("pmcid", "TEXT"),
        ("fulltext_source", "TEXT"),
        ("volume", "TEXT"),
        ("issue", "TEXT"),
        ("pages", "TEXT"),
        ("keywords", "TEXT"),
        ("publisher", "TEXT"),
        ("language", "TEXT"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {col} {decl}")
    # writing_sections：章节级工作流需要记录"本段依据什么生成的"
    if _table_exists(conn, "writing_sections"):
        section_cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(writing_sections)")}
        for col, decl in (
            ("grounded_on", "TEXT DEFAULT '{}'"),
            ("last_run_id", "TEXT"),
        ):
            if col not in section_cols:
                conn.execute(f"ALTER TABLE writing_sections ADD COLUMN {col} {decl}")
    # writing_projects：工作规划（唯一规划节点的产物）
    if _table_exists(conn, "writing_projects"):
        project_cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(writing_projects)")}
        if "plan_json" not in project_cols:
            conn.execute(
                "ALTER TABLE writing_projects ADD COLUMN plan_json TEXT DEFAULT '{}'")
    # section_runs：模板化后要记录用了哪个模板、字段来源、未满足的必考维度
    if _table_exists(conn, "section_runs"):
        run_cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(section_runs)")}
        for col, decl in (
            ("template_key", "TEXT"),
            ("field_source_json", "TEXT"),
            ("unmet_json", "TEXT"),
        ):
            if col not in run_cols:
                conn.execute(f"ALTER TABLE section_runs ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row)


def meta_get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return json.loads(row["v"]) if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def meta_bump(conn: sqlite3.Connection, key: str, delta: int = 1) -> int:
    cur = int(meta_get(conn, key, 0)) + delta
    meta_set(conn, key, cur)
    return cur


def log_event(conn: sqlite3.Connection, node: str, event: str,
              paper_key: str | None = None, details: Any = None) -> None:
    conn.execute(
        "INSERT INTO processing_log(paper_key, node, event, details, ts) VALUES(?,?,?,?,?)",
        (paper_key, node, event,
         json.dumps(details, ensure_ascii=False, default=str) if details is not None else None,
         utcnow()),
    )
    conn.commit()


def _json_dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _draft_for_storage(draft: dict[str, Any] | None) -> dict[str, Any] | None:
    """落库时只保留草稿的结构化关键字段，避免 markdown 重复占用空间。"""
    if not draft:
        return draft
    keep = ("title", "summary", "strategies", "candidate_pool", "selection",
            "design_contract", "revision_responses", "revision_consumed",
            "model_error", "_knowledge_stats")
    if not draft.get("strategies"):
        keep = ("title", "summary", "sections", "strategies", "design_contract",
                "revision_responses", "model_error", "_knowledge_stats")
    return {k: v for k, v in draft.items() if k in keep}


def save_study_run(conn: sqlite3.Connection, run_id: str, out: dict[str, Any],
                   *, round_index: int = 0, request: str = "") -> None:
    """保存一次研究任务的关键中间态（计划/消费/草稿/审核/事实核查）。

    没有这张表时，四节点产物只存在于内存，无法审计"改了什么、为什么改"。
    """
    knowledge = out.get("knowledge") or {}
    consumer = {
        "consumer_analysis": knowledge.get("consumer_analysis"),
        "design_context": knowledge.get("design_context"),
        "coverage_score": knowledge.get("coverage_score"),
        "patterns": len(knowledge.get("patterns") or []),
        "evidence": len(knowledge.get("evidence") or []),
        "hyperedges": len(knowledge.get("hyperedges") or []),
    }
    conn.execute(
        """
        INSERT INTO study_runs(run_id, round, request, status, decision, plan,
                               consumer, draft, review, fact_check, ts)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id, round) DO UPDATE SET
            status=excluded.status, decision=excluded.decision, plan=excluded.plan,
            consumer=excluded.consumer, draft=excluded.draft, review=excluded.review,
            fact_check=excluded.fact_check, ts=excluded.ts
        """,
        (
            run_id, int(round_index), request or out.get("request") or "",
            out.get("status"), out.get("decision"),
            _json_dump(out.get("plan")),
            _json_dump(consumer),
            _json_dump(_draft_for_storage(out.get("draft"))),
            _json_dump(out.get("review")),
            _json_dump(out.get("fact_check")),
            utcnow(),
        ),
    )
    conn.commit()


def get_study_runs(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """读取一次研究任务的全部修订历史（按轮次）。"""
    try:
        rows = conn.execute(
            "SELECT * FROM study_runs WHERE run_id=? ORDER BY round", (run_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for row in rows:
        item = dict(row)
        for key in ("plan", "consumer", "draft", "review", "fact_check"):
            if item.get(key):
                try:
                    item[key] = json.loads(item[key])
                except (TypeError, json.JSONDecodeError):
                    pass
        out.append(item)
    return out


def upsert_paper(conn: sqlite3.Connection, rec: dict[str, Any]) -> str:
    """写入/更新论文主记录，返回 paper_key。rec 中 authors 序列化进 authors_meta。"""
    key = rec["paper_key"]
    now = utcnow()
    authors = rec.get("authors") or []
    existing = conn.execute("SELECT created_at FROM papers WHERE paper_key=?", (key,)).fetchone()
    created = existing["created_at"] if existing else now
    conn.execute(
        """
        INSERT INTO papers(
            paper_key, source, title, abstract, doi, venue, venue_issn, source_type,
            pmcid, fulltext_source, pub_year, pub_date, publication_status,
            citation_count, avg_h_index, authors_meta,
            volume, issue, pages, keywords, publisher, language,
            pdf_sha256, pdf_size,
            pdf_blob, clean_text, clean_text_sha256, status, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(paper_key) DO UPDATE SET
            source=excluded.source, title=excluded.title, abstract=excluded.abstract,
            doi=excluded.doi, venue=excluded.venue, venue_issn=excluded.venue_issn,
            pmcid=excluded.pmcid, fulltext_source=excluded.fulltext_source,
            source_type=excluded.source_type, pub_year=excluded.pub_year,
            pub_date=excluded.pub_date, publication_status=excluded.publication_status,
            citation_count=excluded.citation_count, avg_h_index=excluded.avg_h_index,
            authors_meta=excluded.authors_meta,
            -- 大字段用 COALESCE：调用方没传（None）时保留库中已有值，
            -- 避免"部分更新"把已下载的 PDF/精校文本静默清空（P0-2）
            pdf_sha256=COALESCE(excluded.pdf_sha256, papers.pdf_sha256),
            pdf_size=COALESCE(excluded.pdf_size, papers.pdf_size),
            pdf_blob=COALESCE(excluded.pdf_blob, papers.pdf_blob),
            clean_text=COALESCE(excluded.clean_text, papers.clean_text),
            clean_text_sha256=COALESCE(excluded.clean_text_sha256,
                                       papers.clean_text_sha256),
            volume=excluded.volume, issue=excluded.issue, pages=excluded.pages,
            keywords=excluded.keywords, publisher=excluded.publisher,
            language=excluded.language,
            status=excluded.status, updated_at=excluded.updated_at
        """,
        (
            key, rec.get("source"), rec.get("title"), rec.get("abstract"),
            rec.get("doi"), rec.get("venue"), rec.get("venue_issn"),
            rec.get("source_type"), rec.get("pmcid"), rec.get("fulltext_source"),
            rec.get("pub_year"), rec.get("pub_date"),
            rec.get("publication_status"), rec.get("citation_count"),
            rec.get("avg_h_index"), json.dumps(authors, ensure_ascii=False),
            rec.get("volume"), rec.get("issue"), rec.get("pages"),
            rec.get("keywords"), rec.get("publisher"), rec.get("language"),
            rec.get("pdf_sha256"), rec.get("pdf_size"), rec.get("pdf_blob"),
            rec.get("clean_text"), rec.get("clean_text_sha256"),
            rec.get("status", "ingested"), created, now,
        ),
    )
    conn.commit()
    return key


def get_paper(conn: sqlite3.Connection, paper_key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM papers WHERE paper_key=?", (paper_key,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["authors"] = json.loads(d.pop("authors_meta") or "[]")
    except json.JSONDecodeError:
        d["authors"] = []
    return d


def iter_papers(conn: sqlite3.Connection, status: str | None = None) -> Iterable[dict]:
    if status:
        rows = conn.execute("SELECT paper_key FROM papers WHERE status=?", (status,))
    else:
        rows = conn.execute("SELECT paper_key FROM papers")
    for r in rows:
        rec = get_paper(conn, r["paper_key"])
        if rec:
            yield rec


def save_quality_result(conn: sqlite3.Connection, result: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO quality_results(
            paper_key, venue_factor, h_factor, citation_factor, authority,
            timeliness, quality, decision, needs_review, meta_missing, rationale, assessed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(paper_key) DO UPDATE SET
            venue_factor=excluded.venue_factor, h_factor=excluded.h_factor,
            citation_factor=excluded.citation_factor, authority=excluded.authority,
            timeliness=excluded.timeliness, quality=excluded.quality,
            decision=excluded.decision, needs_review=excluded.needs_review,
            meta_missing=excluded.meta_missing, rationale=excluded.rationale,
            assessed_at=excluded.assessed_at
        """,
        (
            result["paper_key"], result.get("venue_factor"), result.get("h_factor"),
            result.get("citation_factor"), result.get("authority"),
            result.get("timeliness"), result.get("quality"), result.get("decision"),
            int(bool(result.get("needs_review"))),
            json.dumps(result.get("meta_missing", []), ensure_ascii=False),
            result.get("rationale"), utcnow(),
        ),
    )
    conn.commit()


def get_quality_result(conn: sqlite3.Connection, paper_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM quality_results WHERE paper_key=?", (paper_key,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["meta_missing"] = json.loads(d.get("meta_missing") or "[]")
    except json.JSONDecodeError:
        d["meta_missing"] = []
    return d
