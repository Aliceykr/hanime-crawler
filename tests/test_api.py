"""API 层测试：缓存优先 / 202 任务 / 轮询状态 / 错误映射。

抓取层用 mock（离线）；真实抓取在验收阶段手动验证。
"""

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.api import build_video_payload, create_app
from app.crawler import _shift_month


def test_shift_month():
    """预览月份回溯：跨年/进位/负偏移。"""
    assert _shift_month("202609", 0) == "202609"
    assert _shift_month("202609", -1) == "202608"
    assert _shift_month("202601", -1) == "202512"
    assert _shift_month("202512", -1) == "202511"
    assert _shift_month("202401", -12) == "202301"
    assert _shift_month("202512", 1) == "202601"


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

        async def close(self):
            pass  # lifespan 关闭会调用

    import app.api as api_mod
    monkeypatch.setattr(api_mod, "Crawler", FakeCrawler)
    monkeypatch.setattr(api_mod, "Database", lambda *a, **k: db)

    class FakeDownloadManager:
        def __init__(self, db, config, provider=None):
            self.db = db

        async def start(self):
            pass

        def enqueue(self, task_id):
            self.db.update_download_task(task_id, status="queued")

        async def pause(self, task_id):
            t = self.db.get_download_task(task_id)
            if not t:
                return False
            self.db.update_download_task(task_id, status="paused")
            return True

        async def resume(self, task_id):
            t = self.db.get_download_task(task_id)
            if not t:
                return False
            self.db.update_download_task(task_id, status="queued")
            return True

        async def cancel(self, task_id):
            t = self.db.get_download_task(task_id)
            if not t:
                return False
            self.db.update_download_task(task_id, status="cancelled")
            return True

        async def delete(self, task_id):
            t = self.db.get_download_task(task_id)
            if not t:
                return False
            self.db.delete_download_task(task_id)
            return True

        async def close(self):
            pass  # lifespan 关闭会调用

    monkeypatch.setattr(api_mod, "DownloadManager", FakeDownloadManager)

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


def test_cover_route_validation(client):
    """封面代理：非法协议 400；合法图片请求走 mock 返回图片字节。"""
    app, _ = client

    import app.api as api_mod

    class _FakeResp:
        status_code = 200
        content = b"\xff\xd8\xff\xe0fakejpeg"
        headers = {"content-type": "image/jpeg"}

    async def _fake_get(url):
        return _FakeResp()

    async def _fake_fail(url):
        raise RuntimeError("boom")

    monkeypatch_backup = api_mod._get_cover_client

    def _patch(getter):
        class _FakeClient:
            def __init__(self, url):
                self.url = url

            async def get(self, url):
                return await getter(url)

        def _factory(config):
            return _FakeClient(None)

        api_mod._get_cover_client = _factory

    try:
        # 非法协议
        r = app.get("/api/cover", params={"url": "javascript:alert(1)"})
        assert r.status_code == 400
        r = app.get("/api/cover", params={"url": "file:///etc/passwd"})
        assert r.status_code == 400
        # 成功取图
        _patch(_fake_get)
        r = app.get("/api/cover", params={"url": "https://cdn/x/1.jpg"})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.content == _FakeResp.content
        assert "cache-control" in r.headers
        # 上游失败
        _patch(_fake_fail)
        r = app.get("/api/cover", params={"url": "https://cdn/x/2.jpg"})
        assert r.status_code == 502
    finally:
        api_mod._get_cover_client = monkeypatch_backup


def test_stream_route(client):
    """試看流代理:非法协议 400 / Range 透传 + 流式(send stream=True)/ 非视频 502 / 上游异常 502。

    关键回归:send 必须 stream=True(整读会把 657MB 视频缓冲进内存,播放器永远转圈)。
    """
    app, _ = client

    import app.api as api_mod

    class _FakeResp:
        def __init__(self, status=206, headers=None, chunks=(b"abc", b"def")):
            self.status_code = status
            self.headers = headers or {"content-type": "video/mp4",
                                       "content-range": "bytes 0-1023/657976421",
                                       "accept-ranges": "bytes"}
            self._chunks = chunks

        async def aiter_bytes(self):
            for c in self._chunks:
                yield c

        async def aclose(self):
            pass

    class _FakeClient:
        def __init__(self):
            self.last_headers: dict = {}
            self.last_stream = None
            self.resp = _FakeResp()

        def build_request(self, method, url, headers=None):
            self.last_headers = dict(headers or {})
            return (method, url, self.last_headers)

        async def send(self, req, stream=False):
            self.last_stream = stream
            return self.resp

    fake = _FakeClient()
    backup = api_mod._get_stream_client
    api_mod._get_stream_client = lambda config: fake
    try:
        # 非法协议拒绝(防 SSRF 到 file:// 等)
        assert app.get("/api/stream", params={"url": "file:///etc/passwd"}).status_code == 400
        assert app.get("/api/stream", params={"url": "javascript:alert(1)"}).status_code == 400
        # Range 透传 + 流式请求 + 206 与 content-range 原样返回
        r = app.get("/api/stream", params={"url": "https://cdn/x/1.mp4"},
                    headers={"Range": "bytes=0-1023"})
        assert r.status_code == 206
        assert fake.last_stream is True  # 根因回归:必须流式,不整读
        assert fake.last_headers.get("Range") == "bytes=0-1023"
        assert r.headers.get("content-range") == "bytes 0-1023/657976421"
        assert r.content == b"abcdef"
        # 非视频内容拒绝
        fake.resp = _FakeResp(status=200, headers={"content-type": "text/html"})
        assert app.get("/api/stream", params={"url": "https://cdn/x/2"}).status_code == 502
        # 上游异常 → 502 可读错误
        class _Boom:
            def build_request(self, *a, **k):
                return None

            async def send(self, *a, **k):
                raise RuntimeError("boom")

        api_mod._get_stream_client = lambda config: _Boom()
        assert app.get("/api/stream", params={"url": "https://cdn/x/3"}).status_code == 502
    finally:
        api_mod._get_stream_client = backup


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


def test_stats_route(client):
    app, db = client
    from app.models import VideoDetail
    db.upsert_video(VideoDetail(code="771001", title="S", views=1200))
    r = app.get("/api/stats")
    assert r.status_code == 200
    s = r.json()
    assert s["total_videos"] >= 1
    assert s["total_views"] >= 1200
    assert "total_downloads" in s


def test_export_csv_and_json(client):
    app, db = client
    from app.models import VideoDetail
    db.upsert_video(VideoDetail(code="771002", title="导出测试", views=10))
    r = app.get("/api/export?fmt=csv")
    assert r.status_code == 200
    assert r.text.startswith("\ufeff")  # Excel 兼容 BOM
    assert "771002" in r.text
    assert "导出测试" in r.text
    r2 = app.get("/api/export?fmt=json")
    assert r2.status_code == 200
    assert r2.json()[0]["code"] == "771002"


def test_download_api_flow(client):
    app, db = client
    # 创建（无 source 也允许排队）
    r = app.post("/api/download", json={"video_code": "888001", "resolution": "1080P"})
    assert r.status_code == 200
    tid = r.json()["id"]
    assert r.json()["status"] == "queued"
    # 查询 / 列表
    assert app.get(f"/api/download/{tid}").json()["video_code"] == "888001"
    assert app.get("/api/downloads").json()["items"][0]["id"] == tid
    # 暂停 / 恢复 / 取消
    assert app.post(f"/api/download/{tid}/pause").json()["status"] == "paused"
    assert app.post(f"/api/download/{tid}/resume").json()["status"] == "queued"
    assert app.post(f"/api/download/{tid}/cancel").json()["status"] == "cancelled"
    # 删除
    assert app.delete(f"/api/download/{tid}").json()["ok"] is True
    assert app.get(f"/api/download/{tid}").status_code == 404


def test_download_api_validation(client):
    app, _ = client
    assert app.post("/api/download", json={}).status_code == 400


def test_lifespan_startup_and_shutdown(client):
    """回归(step 25):on_event → lifespan 迁移后,进入/退出上下文仍正常。

    startup 拉起下载队列(migrate + 续传),shutdown 关闭 crawler / downloader /
    封面与视频流两个模块级代理客户端;任一环节缺失或抛异常此测试即失败。
    """
    app, _ = client
    with app as ctx:
        assert ctx.get("/api/home").status_code == 200
    assert app.get("/api/tasks").status_code == 200


def test_settings_route(client, monkeypatch, tmp_path):
    app, _ = client
    # 隔离 settings 文件，避免污染工程 data/settings.json
    store: dict[str, Any] = {}
    import app.api as api_mod
    monkeypatch.setattr(api_mod, "load_user_settings", lambda: dict(store))
    monkeypatch.setattr(api_mod, "save_user_settings",
                        lambda u: (store.update(u), store)[1])
    r = app.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["downloads_dir"]
    r2 = app.put("/api/settings", json={"downloads_dir": r.json()["downloads_dir"]})
    assert r2.status_code == 200
    assert r2.json()["ok"] is True
    # 相对路径必须拒绝
    assert app.put("/api/settings", json={"downloads_dir": "relative/path"}).status_code == 400
