"""API 层测试：缓存优先 / 202 任务 / 轮询状态 / 错误映射。

抓取层用 mock（离线）；真实抓取在验收阶段手动验证。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.api import build_video_payload, create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.config import Config, PathsConfig
    from app.database import Database
    from app.models import VideoDetail

    cfg = Config(paths=PathsConfig(db=str(tmp_path / "app.db"),
                                   tmp_dir=str(tmp_path / ".tmp"),
                                   downloads_dir=str(tmp_path / "dl")))
    db = Database(cfg)
    db.init_db()

    class FakeCrawler:
        def __init__(self, config, db=db):
            self.db = db

        async def crawl_home(self):
            return {"rows": {}, "structure_warning": None}

        async def crawl_search(self, query=None, page=1, filters=None):
            return {"cards": [], "total_pages": 1, "query": query}

        async def crawl_video(self, code):
            from app.models import VideoDetail, VideoSource
            d = VideoDetail(
                code=code, title="Fake Title", views=100,
                sources=[VideoSource(video_code=code, resolution="1080P",
                                     url="https://x/1.mp4")],
            )
            self.db.upsert_video(d)
            return d

        async def crawl_preview(self, date):
            from app.models import Preview
            items = [Preview(date=date, code="990001", title="P1")]
            return {"date": date, "items": [i.model_dump() for i in items]}

        async def run_crawl_task(self, task_id, kind, target, **kw):
            self.db.update_crawl_task(task_id, "running")
            try:
                if kind == "preview":
                    await self.crawl_preview(target)
                else:
                    await self.crawl_video(target)
                self.db.update_crawl_task(task_id, "completed")
            except Exception as e:
                self.db.update_crawl_task(task_id, "failed", str(e))

    import app.api as api_mod
    monkeypatch.setattr(api_mod, "Crawler", FakeCrawler)
    monkeypatch.setattr(api_mod, "Database", lambda *a, **k: db)

    app = create_app()
    return TestClient(app), db


def test_home_route(client):
    app, _ = client
    r = app.get("/api/home")
    assert r.status_code == 200
    assert r.json()["rows"] == {}


def test_search_route(client):
    app, _ = client
    r = app.get("/api/search?query=test")
    assert r.status_code == 200
    assert r.json()["query"] == "test"


def test_video_uncached_returns_202_then_cached(client):
    app, db = client
    r = app.get("/api/video/999001")
    assert r.status_code == 202
    body = r.json()
    assert body["cached"] is False
    assert body["task_id"] > 0
    t = app.get(f"/api/task/{body['task_id']}").json()
    assert t["status"] == "completed"
    r2 = app.get("/api/video/999001")
    assert r2.status_code == 200
    assert r2.json()["cached"] is True
    assert r2.json()["video"]["title"] == "Fake Title"


def test_video_reuses_running_task(client):
    app, db = client
    tid = db.create_crawl_task("video", "999002")
    r = app.get("/api/video/999002")
    assert r.status_code == 202
    assert r.json()["task_id"] == tid


def test_task_404(client):
    app, _ = client
    assert app.get("/api/task/999999").status_code == 404


def test_search_history_route(client):
    app, _ = client
    r = app.get("/api/search_history")
    assert r.status_code == 200
    assert "items" in r.json()


def test_video_payload_assembles(client):
    app, db = client
    from app.models import VideoCard, VideoDetail, VideoSource
    db.upsert_video(VideoDetail(
        code="777001", title="T", tags=["2D"], artists=[{"name": "A"}],
        sources=[VideoSource(video_code="777001", resolution="1080P", url="https://x/1.mp4")],
        related=[VideoCard(code="777002", title="R")],
    ))
    p = build_video_payload(db, "777001")
    assert p["tags"] == ["2D"]
    assert p["artist"] == "A"
    assert p["sources"][0]["resolution"] == "1080P"
    assert p["related"][0]["code"] == "777002"
    assert "related_json" not in p


def test_static_index_served(client):
    app, _ = client
    r = app.get("/")
    assert r.status_code == 200
    assert "Hanime" in r.text
    r2 = app.get("/static/style.css")
    assert r2.status_code == 200


def test_previews_route(client):
    app, _ = client
    r = app.get("/api/previews/202604")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "202604"
    assert body["items"][0]["code"] == "990001"


def test_tasks_route(client):
    app, db = client
    app.get("/api/video/999003")  # 触发一个任务
    r = app.get("/api/tasks")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and items[0]["kind"] == "video"


def test_pages_meta_route(client):
    app, _ = client
    r = app.get("/api/pages_meta")
    assert r.status_code == 200
    assert "items" in r.json()
