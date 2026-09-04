"""数据库测试：建表、幂等 upsert、规范化关系、任务与下载表。全部离线，使用 tmp 目录。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config, PathsConfig
from app.database import Database
from app.models import Comment, CrawlTask, PageMeta, VideoDetail, VideoSource


def make_db(tmp_path: Path) -> Database:
    cfg = Config(paths=PathsConfig(db=str(tmp_path / "app.db"),
                                   tmp_dir=str(tmp_path / ".tmp"),
                                   downloads_dir=str(tmp_path / "downloads")))
    db = Database(cfg)
    db.init_db()
    return db


def sample_detail(code: str = "123456") -> VideoDetail:
    return VideoDetail(
        code=code,
        title="測試影片",
        title_cn="测试影片中文",
        description="desc",
        views=12000, views_raw="1.2萬次",
        likes=300, likes_raw="300",
        published_at="2026-01-01",
        duration="10:00",
        tags=["2D", "精翻"],
        artists=[{"name": "某社团"}],
        sources=[VideoSource(video_code=code, resolution="1080P", url="https://cdn/x.mp4", mime_type="video/mp4")],
    )


def test_init_db_creates_all_tables(tmp_path):
    db = make_db(tmp_path)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        names = {r["name"] for r in rows}
    expected = {"videos", "artists", "tags", "video_tags", "video_sources",
                "comments", "crawl_tasks", "pages_meta", "search_history", "downloads"}
    assert expected <= names


def test_upsert_video_is_idempotent(tmp_path):
    db = make_db(tmp_path)
    db.upsert_video(sample_detail())
    db.upsert_video(sample_detail())
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM videos").fetchone()["c"]
        nt = conn.execute("SELECT COUNT(*) c FROM video_tags").fetchone()["c"]
        ns = conn.execute("SELECT COUNT(*) c FROM video_sources").fetchone()["c"]
    assert n == 1
    assert nt == 2
    assert ns == 1


def test_video_relations_written(tmp_path):
    db = make_db(tmp_path)
    db.upsert_video(sample_detail())
    assert db.get_video("123456")["views"] == 12000
    assert db.get_video_tags("123456") == ["2D", "精翻"]
    assert db.get_video_artist("123456") == "某社团"
    sources = db.get_video_sources("123456")
    assert sources[0]["resolution"] == "1080P"
    assert sources[0]["url"].startswith("https://cdn")


def test_sources_updated_on_refetch(tmp_path):
    db = make_db(tmp_path)
    db.upsert_video(sample_detail())
    d2 = sample_detail()
    d2.sources[0].url = "https://cdn/new.mp4"
    db.upsert_video(d2)
    sources = db.get_video_sources("123456")
    assert len(sources) == 1
    assert sources[0]["url"] == "https://cdn/new.mp4"


def test_page_meta_upsert(tmp_path):
    db = make_db(tmp_path)
    m1 = PageMeta(url="https://hanime1.me/search?query=a", http_code=200, content_hash="abc", elapsed_ms=100)
    db.save_page_meta(m1)
    m2 = PageMeta(url="https://hanime1.me/search?query=a", http_code=200, content_hash="def", elapsed_ms=200)
    db.save_page_meta(m2)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM pages_meta WHERE url=?", (m1.url,)).fetchone()
    assert row["content_hash"] == "def"
    assert row["elapsed_ms"] == 200


def test_crawl_task_lifecycle(tmp_path):
    db = make_db(tmp_path)
    tid = db.create_crawl_task("search", "test")
    db.update_crawl_task(tid, "completed")
    t = db.get_crawl_task(tid)
    assert t["status"] == "completed"
    assert t["target"] == "test"


def test_search_history(tmp_path):
    db = make_db(tmp_path)
    db.add_search_history("test", {"sort": "new"})
    db.add_search_history("hello")
    hist = db.list_search_history()
    assert len(hist) == 2
    assert hist[0]["query"] == "hello"
    assert hist[1]["filters_json"].find("new") > -1


def test_comments_save_and_list(tmp_path):
    db = make_db(tmp_path)
    db.upsert_video(sample_detail())
    db.save_comments("123456", [Comment(id="c1", video_code="123456", author="A", content="hi", likes=1)])
    comments = db.list_comments("123456")
    assert len(comments) == 1
    assert comments[0]["author"] == "A"


def test_download_task_table(tmp_path):
    db = make_db(tmp_path)
    tid = db.create_download_task("123456", "1080P", "https://cdn/x.mp4")
    db.update_download_task(tid, status="downloading", downloaded_bytes=100)
    t = db.get_download_task(tid)
    assert t["status"] == "downloading"
    assert t["downloaded_bytes"] == 100
    assert len(db.list_download_tasks()) == 1
    db.delete_download_task(tid)
    assert db.get_download_task(tid) is None
