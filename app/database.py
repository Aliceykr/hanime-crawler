"""SQLite 存储层（标准库 sqlite3，无 ORM）。所有磁盘路径来自 config.paths（绝对路径）。

表（§4.3 规范化）：videos / artists / tags / video_tags / video_sources /
comments / crawl_tasks / pages_meta / search_history；downloads 在 M4 启用时建。
Parser 不接触本模块：HTML → Model（parser.py）→ Repository（本文件）→ SQLite。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Optional

from .config import Config, load_config
from .models import Comment, CrawlTask, PageMeta, VideoDetail

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    slug TEXT
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS videos (
    code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    title_cn TEXT,
    description TEXT,
    views INTEGER,
    views_raw TEXT,
    likes INTEGER,
    likes_raw TEXT,
    published_at TEXT,
    duration TEXT,
    cover_url TEXT,
    artist_id INTEGER REFERENCES artists(id),
    series_title TEXT,
    related_json TEXT,
    crawled_at TEXT,
    content_hash TEXT
);
CREATE TABLE IF NOT EXISTS video_tags (
    video_code TEXT NOT NULL REFERENCES videos(code),
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (video_code, tag_id)
);
CREATE TABLE IF NOT EXISTS video_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_code TEXT NOT NULL REFERENCES videos(code),
    resolution TEXT NOT NULL,
    url TEXT NOT NULL,
    mime_type TEXT,
    fetched_at TEXT,
    expires_at TEXT,
    UNIQUE (video_code, resolution)
);
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    video_code TEXT REFERENCES videos(code),
    parent_id TEXT,
    author TEXT,
    content TEXT,
    likes INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS crawl_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS pages_meta (
    url TEXT PRIMARY KEY,
    fetched_at TEXT,
    http_code INTEGER,
    content_hash TEXT,
    elapsed_ms INTEGER,
    error TEXT
);
CREATE TABLE IF NOT EXISTS search_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    filters_json TEXT DEFAULT '{}',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_code TEXT NOT NULL,
    resolution TEXT NOT NULL,
    url TEXT,
    filename TEXT,
    temp_path TEXT,
    final_path TEXT,
    total_bytes INTEGER,
    downloaded_bytes INTEGER DEFAULT 0,
    status TEXT DEFAULT 'queued',
    speed REAL,
    error TEXT,
    retry_count INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_video_tags_code ON video_tags(video_code);
CREATE INDEX IF NOT EXISTS idx_sources_code ON video_sources(video_code);
CREATE INDEX IF NOT EXISTS idx_comments_video ON comments(video_code);
CREATE INDEX IF NOT EXISTS idx_tasks_kind ON crawl_tasks(kind);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, config: Optional[Config] = None):
        self.config = config or load_config()
        self.db_path = self.config.paths.db

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            # 兼容旧库：为已存在的 videos 表补列（幂等）
            try:
                conn.execute("ALTER TABLE videos ADD COLUMN related_json TEXT")
            except sqlite3.OperationalError:
                pass
        # 启动清扫:残留的 queued/running 任务在进程死时已终止,不清理会让
        # find_running_task 永远命中 → 该视频详情页永久 202 卡死(§12 step 24b)
        self.sweep_stale_tasks()

    def sweep_stale_tasks(self) -> int:
        """把残留的 queued/running 抓取任务标记为 failed,返回清扫行数。"""
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE crawl_tasks SET status='failed', error=?, updated_at=? "
                "WHERE status IN ('queued','running')",
                ("interrupted: process restart", _now()),
            )
            return cur.rowcount

    # ── 幂等写入 ──
    def upsert_artist(self, name: str, slug: Optional[str] = None,
                      conn: Optional[sqlite3.Connection] = None) -> int:
        if conn is None:
            with self.connect() as c:
                return self.upsert_artist(name, slug, c)
        conn.execute(
            "INSERT INTO artists(name, slug) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET slug=COALESCE(excluded.slug, artists.slug)",
            (name, slug),
        )
        row = conn.execute("SELECT id FROM artists WHERE name=?", (name,)).fetchone()
        return row["id"]

    def upsert_tag(self, name: str, conn: Optional[sqlite3.Connection] = None) -> int:
        if conn is None:
            with self.connect() as c:
                return self.upsert_tag(name, c)
        conn.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (name,))
        row = conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
        return row["id"]

    def upsert_video(self, detail: VideoDetail, content_hash: Optional[str] = None) -> None:
        with self.connect() as conn:
            artist_id = None
            if detail.artists:
                first = detail.artists[0]
                artist_id = self.upsert_artist(first.name, first.slug, conn)
            conn.execute(
                """INSERT INTO videos(code, title, title_cn, description, views, views_raw,
                       likes, likes_raw, published_at, duration, cover_url, artist_id,
                       series_title, related_json, crawled_at, content_hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(code) DO UPDATE SET
                       title=excluded.title,
                       title_cn=COALESCE(excluded.title_cn, videos.title_cn),
                       description=COALESCE(excluded.description, videos.description),
                       views=COALESCE(excluded.views, videos.views),
                       views_raw=COALESCE(excluded.views_raw, videos.views_raw),
                       likes=COALESCE(excluded.likes, videos.likes),
                       likes_raw=COALESCE(excluded.likes_raw, videos.likes_raw),
                       published_at=COALESCE(excluded.published_at, videos.published_at),
                       duration=COALESCE(excluded.duration, videos.duration),
                       cover_url=COALESCE(excluded.cover_url, videos.cover_url),
                       artist_id=COALESCE(excluded.artist_id, videos.artist_id),
                       series_title=COALESCE(excluded.series_title, videos.series_title),
                       related_json=COALESCE(excluded.related_json, videos.related_json),
                       crawled_at=excluded.crawled_at,
                       content_hash=excluded.content_hash""",
                (
                    detail.code, detail.title, detail.title_cn, detail.description,
                    detail.views, detail.views_raw, detail.likes, detail.likes_raw,
                    detail.published_at, detail.duration, detail.cover_url, artist_id,
                    detail.series_title,
                    json.dumps([r.model_dump() for r in detail.related], ensure_ascii=False)
                    if detail.related else None,
                    detail.crawled_at.isoformat(timespec="seconds"),
                    content_hash,
                ),
            )
            # tags
            for t in detail.tags:
                tag_id = self.upsert_tag(t, conn)
                conn.execute(
                    "INSERT OR IGNORE INTO video_tags(video_code, tag_id) VALUES(?, ?)",
                    (detail.code, tag_id),
                )
            # sources：按 (video_code, resolution) 幂等
            for s in detail.sources:
                conn.execute(
                    """INSERT INTO video_sources(video_code, resolution, url, mime_type, fetched_at, expires_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(video_code, resolution) DO UPDATE SET
                           url=excluded.url, mime_type=excluded.mime_type,
                           fetched_at=excluded.fetched_at, expires_at=excluded.expires_at""",
                    (
                        s.video_code, s.resolution, s.url, s.mime_type,
                        s.fetched_at.isoformat(timespec="seconds"),
                        s.expires_at.isoformat(timespec="seconds") if s.expires_at else None,
                    ),
                )

    def save_page_meta(self, meta: PageMeta) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO pages_meta(url, fetched_at, http_code, content_hash, elapsed_ms, error)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET
                       fetched_at=excluded.fetched_at, http_code=excluded.http_code,
                       content_hash=excluded.content_hash, elapsed_ms=excluded.elapsed_ms,
                       error=excluded.error""",
                (
                    meta.url, meta.fetched_at.isoformat(timespec="seconds"), meta.http_code,
                    meta.content_hash, meta.elapsed_ms, meta.error,
                ),
            )

    # ── 查询 ──
    def get_video(self, code: str) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM videos WHERE code=?", (code,)).fetchone()
            return dict(row) if row else None

    def get_video_sources(self, code: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT resolution, url, mime_type, fetched_at, expires_at "
                "FROM video_sources WHERE video_code=? ORDER BY resolution",
                (code,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_video_tags(self, code: str) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT t.name FROM tags t JOIN video_tags vt ON vt.tag_id=t.id "
                "WHERE vt.video_code=?",
                (code,),
            ).fetchall()
            return [r["name"] for r in rows]

    def get_video_artist(self, code: str) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT a.name FROM artists a JOIN videos v ON v.artist_id=a.id "
                "WHERE v.code=?",
                (code,),
            ).fetchone()
            return row["name"] if row else None

    def search_videos(self, query: str, limit: int = 40, offset: int = 0) -> list[dict[str, Any]]:
        with self.connect() as conn:
            like = f"%{query}%"
            rows = conn.execute(
                """SELECT v.*, a.name AS artist_name FROM videos v
                   LEFT JOIN artists a ON a.id = v.artist_id
                   WHERE v.title LIKE ? OR v.title_cn LIKE ? OR v.code LIKE ?
                   ORDER BY v.crawled_at DESC LIMIT ? OFFSET ?""",
                (like, like, like, limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_videos(self, limit: int = 50, offset: int = 0,
                    tag: Optional[str] = None, artist: Optional[str] = None) -> list[dict[str, Any]]:
        sql = """SELECT v.*, a.name AS artist_name FROM videos v
                 LEFT JOIN artists a ON a.id = v.artist_id"""
        conds, params = [], []
        if tag:
            conds.append("v.code IN (SELECT video_code FROM video_tags vt JOIN tags t ON t.id=vt.tag_id WHERE t.name=?)")
            params.append(tag)
        if artist:
            conds.append("a.name = ?")
            params.append(artist)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY v.crawled_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    # ── 搜索历史 ──
    def add_search_history(self, query: str, filters: Optional[dict] = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO search_history(query, filters_json, created_at) VALUES(?,?,?)",
                (query, json.dumps(filters or {}, ensure_ascii=False), _now()),
            )

    def list_search_history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM search_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── 抓取任务 ──
    def create_crawl_task(self, kind: str, target: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO crawl_tasks(kind, target, status, created_at, updated_at) "
                "VALUES(?,?, 'queued', ?, ?)",
                (kind, target, _now(), _now()),
            )
            return cur.lastrowid

    def update_crawl_task(self, task_id: int, status: str, error: Optional[str] = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE crawl_tasks SET status=?, error=?, updated_at=? WHERE id=?",
                (status, error, _now(), task_id),
            )

    def get_crawl_task(self, task_id: int) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM crawl_tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def find_running_task(self, kind: str, target: str) -> Optional[int]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM crawl_tasks WHERE kind=? AND target=? "
                "AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
                (kind, target),
            ).fetchone()
            return row["id"] if row else None

    def list_crawl_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM crawl_tasks ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def list_page_meta(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pages_meta ORDER BY fetched_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── M3：导出与统计 ──
    def export_videos(self) -> list[dict[str, Any]]:
        """导出视频（tags/artist 汇总为逗号分隔，供 CSV/JSON 导出）。"""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT v.code, v.title, v.title_cn, v.description, v.views, v.likes,
                          v.published_at, v.duration, v.cover_url, v.series_title,
                          v.crawled_at,
                          COALESCE(a.name, '') AS artist,
                          (SELECT GROUP_CONCAT(t.name, ', ')
                             FROM video_tags vt JOIN tags t ON t.id = vt.tag_id
                            WHERE vt.video_code = v.code) AS tags
                     FROM videos v
                     LEFT JOIN artists a ON a.id = v.artist_id
                     ORDER BY v.code DESC""",
            ).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            def one(q, *a):
                return conn.execute(q, a).fetchone()[0]
            total_videos = one("SELECT COUNT(*) FROM videos")
            total_artists = one("SELECT COUNT(*) FROM artists")
            total_tags = one("SELECT COUNT(*) FROM tags")
            total_comments = one("SELECT COUNT(*) FROM comments")
            total_views = one("SELECT COALESCE(SUM(views),0) FROM videos")
            total_downloads = one("SELECT COUNT(*) FROM downloads")
            tasks_by_status = conn.execute(
                "SELECT status, COUNT(*) c FROM crawl_tasks GROUP BY status").fetchall()
            source_count = one("SELECT COUNT(*) FROM video_sources")
            last_crawl = one("SELECT MAX(crawled_at) FROM videos")
            return {
                "total_videos": total_videos,
                "total_artists": total_artists,
                "total_tags": total_tags,
                "total_comments": total_comments,
                "total_views": total_views,
                "total_downloads": total_downloads,
                "source_count": source_count,
                "tasks_by_status": {r["status"]: r["c"] for r in tasks_by_status},
                "last_crawl": last_crawl,
            }

    # ── 评论（M2） ──
    def save_comments(self, video_code: str, comments: list[Comment]) -> None:
        if not comments:
            return
        with self.connect() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO comments(id, video_code, parent_id, author, content, likes, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                [
                    (
                        c.id, video_code, c.parent_id, c.author, c.content,
                        c.likes, c.created_at,
                    )
                    for c in comments
                ],
            )

    def list_comments(self, video_code: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM comments WHERE video_code=? ORDER BY created_at DESC LIMIT 200",
                (video_code,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── 下载任务（M4；表已预留） ──
    def create_download_task(self, video_code: str, resolution: str,
                             url: Optional[str] = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO downloads(video_code, resolution, url, status, created_at, updated_at)
                   VALUES(?,?,?, 'queued', ?, ?)""",
                (video_code, resolution, url, _now(), _now()),
            )
            return cur.lastrowid

    def update_download_task(self, task_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE downloads SET {cols} WHERE id=?", (*fields.values(), task_id))

    def get_download_task(self, task_id: int) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM downloads WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def list_download_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM downloads ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_download_task(self, task_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM downloads WHERE id=?", (task_id,))
