"""文献库侧车层：标签 / 文件夹 / 收藏 + 服务端筛选与分页。

来源：``paper_writing_assistant`` 的 ``library/store.py``（4 张侧车表、12 个方法），
合并时做了三处改造（见合并方案 v2 §4/§6.1）：

1. 主键由 ``paper_id INTEGER`` 改为 ``paper_key TEXT``；
2. 补齐外键级联（``db.SCHEMA`` 内已带 ``ON DELETE CASCADE``）；
3. 筛选/排序/分页**下推 SQL**，不再像来源版本那样"先取 5000 行再在内存里过滤"；
   并显式支持「年份未知」与「低信号入库」两个档位。

大字段（``pdf_blob`` / ``clean_text``）永不进入列表查询结果集。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from research_agent.db import utcnow

#: 列表查询返回的列（刻意排除 pdf_blob / clean_text）
_LIST_COLUMNS = (
    "p.paper_key, p.source, p.title, p.venue, p.pub_year, p.doi, p.pmcid, "
    "p.fulltext_source, p.source_type, p.citation_count, p.avg_h_index, "
    "p.publication_status, p.status, p.created_at, p.updated_at, "
    "p.volume, p.issue, p.pages, p.language, "
    "(SELECT COUNT(*) FROM paper_tags t WHERE t.paper_key = p.paper_key) AS tag_count, "
    "EXISTS(SELECT 1 FROM paper_favorites f WHERE f.paper_key = p.paper_key) "
    "  AS favorite, "
    "q.quality AS quality, q.authority AS authority, q.timeliness AS timeliness, "
    "q.venue_factor AS venue_factor, q.h_factor AS h_factor, "
    "q.citation_factor AS citation_factor, q.decision AS decision, "
    "q.needs_review AS needs_review, q.meta_missing AS meta_missing, "
    "q.rationale AS rationale, "
    "LENGTH(p.authors_meta) AS authors_len"
)

#: 允许的排序字段 -> SQL 片段
_SORT_COLUMNS = {
    "quality": "q.quality",
    "year": "p.pub_year",
    "citations": "p.citation_count",
    "title": "p.title",
    "created": "p.created_at",
    "updated": "p.updated_at",
    "source": "p.source",
}

#: 允许的年份未知档位取值
YEAR_UNKNOWN = "unknown"


class LibraryStore:
    """文献库侧车表的读写入口。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------- 标签
    def add_tag(self, paper_key: str, tag: str) -> bool:
        """添加标签；空标签或已存在返回 False（幂等）。"""
        name = str(tag or "").strip()
        if not name or not paper_key:
            return False
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO paper_tags(paper_key, tag, created_at) VALUES(?,?,?)",
            (paper_key, name, utcnow()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def remove_tag(self, paper_key: str, tag: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM paper_tags WHERE paper_key=? AND tag=?",
            (paper_key, str(tag or "").strip()),
        )
        self.conn.commit()
        return cur.rowcount

    def get_tags(self, paper_key: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT tag FROM paper_tags WHERE paper_key=? ORDER BY tag", (paper_key,)
        ).fetchall()
        return [r["tag"] for r in rows]

    def tags_for(self, paper_keys: Iterable[str]) -> dict[str, list[str]]:
        """批量取标签，避免 N+1。"""
        keys = [k for k in paper_keys if k]
        if not keys:
            return {}
        out: dict[str, list[str]] = {k: [] for k in keys}
        chunk = 400
        for start in range(0, len(keys), chunk):
            batch = keys[start:start + chunk]
            placeholders = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"SELECT paper_key, tag FROM paper_tags "
                f"WHERE paper_key IN ({placeholders}) ORDER BY tag",
                batch,
            ).fetchall()
            for row in rows:
                out.setdefault(row["paper_key"], []).append(row["tag"])
        return out

    def all_tags(self) -> list[dict[str, Any]]:
        """全部标签及计数（供筛选面板）。"""
        rows = self.conn.execute(
            "SELECT tag, COUNT(*) AS count FROM paper_tags "
            "GROUP BY tag ORDER BY count DESC, tag"
        ).fetchall()
        return [{"tag": r["tag"], "count": int(r["count"])} for r in rows]

    def batch_tag(self, paper_keys: Iterable[str], tag: str) -> int:
        """批量添加标签，返回实际新增条数。"""
        name = str(tag or "").strip()
        if not name:
            return 0
        added = 0
        for key in paper_keys:
            if self.add_tag(key, name):
                added += 1
        return added

    # ------------------------------------------------------------- 文件夹
    def create_folder(self, name: str) -> int:
        clean = str(name or "").strip()
        if not clean:
            raise ValueError("文件夹名不能为空")
        self.conn.execute(
            "INSERT OR IGNORE INTO paper_folders(name, created_at) VALUES(?,?)",
            (clean, utcnow()),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT folder_id FROM paper_folders WHERE name=?", (clean,)
        ).fetchone()
        return int(row["folder_id"])

    def rename_folder(self, folder_id: int, name: str) -> None:
        clean = str(name or "").strip()
        if not clean:
            raise ValueError("文件夹名不能为空")
        self.conn.execute(
            "UPDATE paper_folders SET name=? WHERE folder_id=?", (clean, int(folder_id))
        )
        self.conn.commit()

    def delete_folder(self, folder_id: int) -> None:
        """删除文件夹；成员关系由 ON DELETE CASCADE 清理。"""
        self.conn.execute(
            "DELETE FROM paper_folders WHERE folder_id=?", (int(folder_id),)
        )
        self.conn.commit()

    def list_folders(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT f.folder_id, f.name, f.created_at, COUNT(m.paper_key) AS count "
            "FROM paper_folders f "
            "LEFT JOIN paper_folder_map m ON m.folder_id = f.folder_id "
            "GROUP BY f.folder_id ORDER BY f.name"
        ).fetchall()
        return [
            {
                "folder_id": int(r["folder_id"]),
                "name": r["name"],
                "created_at": r["created_at"],
                "count": int(r["count"] or 0),
            }
            for r in rows
        ]

    def add_to_folder(self, paper_key: str, folder_id: int) -> bool:
        if not paper_key:
            return False
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO paper_folder_map(folder_id, paper_key, created_at) "
            "VALUES(?,?,?)",
            (int(folder_id), paper_key, utcnow()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def remove_from_folder(self, paper_key: str, folder_id: int) -> int:
        cur = self.conn.execute(
            "DELETE FROM paper_folder_map WHERE folder_id=? AND paper_key=?",
            (int(folder_id), paper_key),
        )
        self.conn.commit()
        return cur.rowcount

    def folder_ids_for(self, paper_key: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT folder_id FROM paper_folder_map WHERE paper_key=? ORDER BY folder_id",
            (paper_key,),
        ).fetchall()
        return [int(r["folder_id"]) for r in rows]

    def batch_add_to_folder(self, paper_keys: Iterable[str], folder_id: int) -> int:
        changed = 0
        for key in paper_keys:
            if self.add_to_folder(key, folder_id):
                changed += 1
        return changed

    # ------------------------------------------------------------- 收藏
    def set_favorite(self, paper_key: str, value: bool = True) -> None:
        if not paper_key:
            return
        if value:
            self.conn.execute(
                "INSERT OR IGNORE INTO paper_favorites(paper_key, created_at) VALUES(?,?)",
                (paper_key, utcnow()),
            )
        else:
            self.conn.execute(
                "DELETE FROM paper_favorites WHERE paper_key=?", (paper_key,)
            )
        self.conn.commit()

    def is_favorite(self, paper_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM paper_favorites WHERE paper_key=?", (paper_key,)
        ).fetchone()
        return bool(row)

    def favorite_paper_keys(self) -> set[str]:
        rows = self.conn.execute("SELECT paper_key FROM paper_favorites").fetchall()
        return {r["paper_key"] for r in rows}

    def batch_favorite(self, paper_keys: Iterable[str], value: bool = True) -> int:
        changed = 0
        for key in paper_keys:
            before = self.is_favorite(key)
            self.set_favorite(key, value)
            if before != bool(value):
                changed += 1
        return changed

    # ------------------------------------------------------------- 级联删除
    def delete_paper_records(self, paper_key: str) -> None:
        """删除侧车记录（papers 主表的删除由调用方负责；外键会再兜一次）。"""
        for sql in (
            "DELETE FROM paper_tags WHERE paper_key=?",
            "DELETE FROM paper_folder_map WHERE paper_key=?",
            "DELETE FROM paper_favorites WHERE paper_key=?",
        ):
            self.conn.execute(sql, (paper_key,))
        self.conn.commit()

    # ------------------------------------------------------------- 列表查询
    def query_papers(
        self,
        *,
        q: str | None = None,
        status: str | None = None,
        source: str | None = None,
        source_type: str | None = None,
        decision: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        year_mode: str | None = None,
        tag: str | None = None,
        folder_id: int | None = None,
        favorite_only: bool = False,
        low_signal: str | None = None,
        has_knowledge: bool | None = None,
        sort_by: str = "quality",
        sort_desc: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """服务端筛选 + 排序 + 分页，并返回 facets 与总数。

        ``year_mode="unknown"`` 只返回年份缺失的记录；此时 year_from/year_to 被忽略。
        ``low_signal`` 取 ``"yes"`` / ``"no"``，按检索门控标注过滤。
        """
        where: list[str] = []
        params: list[Any] = []

        if q:
            like = f"%{q.strip()}%"
            where.append(
                "(p.title LIKE ? OR p.abstract LIKE ? OR p.venue LIKE ? "
                "OR p.doi LIKE ? OR p.paper_key LIKE ?)"
            )
            params.extend([like] * 5)
        if status:
            where.append("p.status = ?")
            params.append(status)
        if source:
            where.append("p.source = ?")
            params.append(source)
        if source_type:
            where.append("p.source_type = ?")
            params.append(source_type)
        if decision:
            where.append("q.decision = ?")
            params.append(decision)
        if year_mode == YEAR_UNKNOWN:
            where.append("p.pub_year IS NULL")
        else:
            if year_from is not None:
                where.append("p.pub_year >= ?")
                params.append(int(year_from))
            if year_to is not None:
                where.append("p.pub_year <= ?")
                params.append(int(year_to))
        if tag:
            where.append(
                "EXISTS(SELECT 1 FROM paper_tags t WHERE t.paper_key = p.paper_key "
                "AND t.tag = ?)"
            )
            params.append(tag)
        if folder_id is not None:
            where.append(
                "EXISTS(SELECT 1 FROM paper_folder_map m WHERE m.paper_key = p.paper_key "
                "AND m.folder_id = ?)"
            )
            params.append(int(folder_id))
        if favorite_only:
            where.append(
                "EXISTS(SELECT 1 FROM paper_favorites f WHERE f.paper_key = p.paper_key)"
            )
        if low_signal == "yes":
            where.append("l.details LIKE '%low_signal%'")
        elif low_signal == "no":
            where.append("(l.details IS NULL OR l.details NOT LIKE '%low_signal%')")
        if has_knowledge is not None:
            # 抽取完成以知识节点的 processing_log 事件为准（paper_key 全库唯一）
            exists = (
                "EXISTS(SELECT 1 FROM processing_log k WHERE k.paper_key = p.paper_key "
                "AND k.event = 'extracted')"
            )
            where.append(exists if has_knowledge else f"NOT {exists}")

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        # 低信号标注来自检索门控事件（v0.4.3 起的 low_signal 放行记录）
        from_sql = (
            "FROM papers p "
            "LEFT JOIN quality_results q ON q.paper_key = p.paper_key "
            "LEFT JOIN processing_log l ON l.paper_key = p.paper_key "
            "  AND l.event = 'relevance-gate-low-signal'"
        )

        total_row = self.conn.execute(
            f"SELECT COUNT(DISTINCT p.paper_key) AS n {from_sql}{where_sql}", params
        ).fetchone()
        total = int(total_row["n"] or 0)

        sort_col = _SORT_COLUMNS.get(str(sort_by or "").strip(), "q.quality")
        direction = "DESC" if sort_desc else "ASC"
        limit = max(1, min(int(limit or 50), 500))
        offset = max(0, int(offset or 0))
        # NULL 统一排在最后：用 CASE 而非 NULLS LAST，避免依赖 SQLite 3.30+
        null_last = f"CASE WHEN {sort_col} IS NULL THEN 1 ELSE 0 END, {sort_col} {direction}"
        sql = (
            f"SELECT {_LIST_COLUMNS} {from_sql}{where_sql} "
            f"GROUP BY p.paper_key "
            f"ORDER BY {null_last}, p.pub_year DESC, p.paper_key "
            f"LIMIT ? OFFSET ?"
        )
        rows = self.conn.execute(sql, params + [limit, offset]).fetchall()
        items = [self._row_to_item(r) for r in rows]
        tags_map = self.tags_for([item["paper_key"] for item in items])
        folders = {f["folder_id"]: f["name"] for f in self.list_folders()}
        for item in items:
            item["tags"] = tags_map.get(item["paper_key"], [])
            item["folder_ids"] = self.folder_ids_for(item["paper_key"])
            item["folder_names"] = [
                folders[fid] for fid in item["folder_ids"] if fid in folders
            ]
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "facets": self.facets(),
        }

    def facets(self) -> dict[str, Any]:
        """筛选面板所需的分面统计。"""
        sources = self.conn.execute(
            "SELECT source, COUNT(*) AS count FROM papers "
            "WHERE source IS NOT NULL GROUP BY source ORDER BY count DESC"
        ).fetchall()
        statuses = self.conn.execute(
            "SELECT status, COUNT(*) AS count FROM papers "
            "WHERE status IS NOT NULL GROUP BY status ORDER BY count DESC"
        ).fetchall()
        decisions = self.conn.execute(
            "SELECT decision, COUNT(*) AS count FROM quality_results "
            "WHERE decision IS NOT NULL GROUP BY decision ORDER BY count DESC"
        ).fetchall()
        source_types = self.conn.execute(
            "SELECT source_type, COUNT(*) AS count FROM papers "
            "WHERE source_type IS NOT NULL GROUP BY source_type ORDER BY count DESC"
        ).fetchall()
        years = self.conn.execute(
            "SELECT MIN(pub_year) AS y_min, MAX(pub_year) AS y_max FROM papers "
            "WHERE pub_year IS NOT NULL"
        ).fetchone()
        unknown_year = self.conn.execute(
            "SELECT COUNT(*) AS count FROM papers WHERE pub_year IS NULL"
        ).fetchone()
        return {
            "tags": self.all_tags(),
            "folders": self.list_folders(),
            "sources": [{"value": r["source"], "count": int(r["count"])} for r in sources],
            "statuses": [{"value": r["status"], "count": int(r["count"])} for r in statuses],
            "decisions": [{"value": r["decision"], "count": int(r["count"])} for r in decisions],
            "source_types": [
                {"value": r["source_type"], "count": int(r["count"])} for r in source_types
            ],
            "year_min": years["y_min"] if years else None,
            "year_max": years["y_max"] if years else None,
            "year_unknown": int(unknown_year["count"] or 0) if unknown_year else 0,
        }

    def stats(self) -> dict[str, Any]:
        """库级统计：文献数、抽取覆盖、低信号、未命中分区等。"""

        def scalar(sql: str, params: tuple = ()) -> int:
            row = self.conn.execute(sql, params).fetchone()
            return int((row[0] if row else 0) or 0)

        return {
            "papers": scalar("SELECT COUNT(*) FROM papers"),
            "favorites": scalar("SELECT COUNT(*) FROM paper_favorites"),
            "tags": scalar("SELECT COUNT(DISTINCT tag) FROM paper_tags"),
            "folders": scalar("SELECT COUNT(*) FROM paper_folders"),
            "with_fulltext": scalar(
                "SELECT COUNT(*) FROM papers WHERE fulltext_source IN ('pdf','xml')"
            ),
            "abstract_only": scalar(
                "SELECT COUNT(*) FROM papers WHERE fulltext_source = 'abstract'"
            ),
            "assessed": scalar("SELECT COUNT(*) FROM quality_results"),
            "needs_review": scalar(
                "SELECT COUNT(*) FROM quality_results WHERE needs_review = 1"
            ),
            "extracted": scalar(
                "SELECT COUNT(DISTINCT paper_key) FROM processing_log "
                "WHERE event = 'extracted'"
            ),
            "low_signal": scalar(
                "SELECT COUNT(*) FROM processing_log "
                "WHERE event = 'relevance-gate-low-signal'"
            ),
            "unknown_year": scalar("SELECT COUNT(*) FROM papers WHERE pub_year IS NULL"),
        }

    # ------------------------------------------------------------- 内部
    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item.pop("authors_len", None)
        try:
            item["meta_missing"] = json.loads(item.get("meta_missing") or "[]")
        except (TypeError, json.JSONDecodeError):
            item["meta_missing"] = []
        item["favorite"] = bool(item.get("favorite"))
        item["needs_review"] = bool(item.get("needs_review"))
        item["tag_count"] = int(item.get("tag_count") or 0)
        item["folder_ids"] = []
        return item
