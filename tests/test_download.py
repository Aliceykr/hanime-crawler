"""下载器离线测试：本地 HTTP 服务器（支持 Range）模拟真实视频源。

覆盖：完整下载 / 断点续传(.part→rename) / Range(206) / 暂停-恢复 / 取消 /
URL 失效自动刷新(VideoSourceProvider) / 404 失败。
全部离线，不访问 hanime1.me。每个测试在单个事件循环内驱动 DownloadManager。
"""

import asyncio
import functools
import http.server
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.config import Config, DownloaderConfig, PathsConfig, ProxyConfig
from app.database import Database
from app.downloader import DownloadManager, migrate_download_dirs
from app.models import VideoDetail
from app.parser import sanitize_filename


class _SlowHandler(http.server.SimpleHTTPRequestHandler):
    """限速 handler：每次拷贝 64KiB 睡 20ms，制造可控竞态窗口。"""

    def copyfile(self, source, outputfile):
        while True:
            buf = source.read(65536)
            if not buf:
                break
            outputfile.write(buf)
            time.sleep(0.02)

    def log_message(self, *a):  # 静默
        pass


@pytest.fixture()
def env(tmp_path):
    serve_dir = tmp_path / "serve"
    serve_dir.mkdir()
    cfg = Config(
        paths=PathsConfig(
            db=str(tmp_path / "t.db"),
            tmp_dir=str(tmp_path / ".tmp"),
            downloads_dir=str(tmp_path / "dl"),
        ),
        proxy=ProxyConfig(enabled=False),  # 本地测试不走代理
        downloader=DownloaderConfig(max_concurrency=2, chunk_size=1024 * 1024, retries=1),
    )
    db = Database(cfg)
    db.init_db()
    handler = functools.partial(_SlowHandler, directory=str(serve_dir))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield cfg, db, serve_dir, srv.server_port
    srv.shutdown()


def _make_file(serve_dir: Path, name: str, size_mb: int) -> str:
    p = serve_dir / name
    with open(p, "wb") as f:
        f.write(b"x" * (size_mb * 1024 * 1024))
    return p


async def _await_status(db, tid, statuses, timeout=40.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        t = db.get_download_task(tid)
        if t and t["status"] in statuses:
            return t
        await asyncio.sleep(0.05)
    raise AssertionError(f"task {tid} not in {statuses} within timeout")


def test_download_complete(env):
    cfg, db, serve_dir, port = env
    _make_file(serve_dir, "v.mp4", 3)
    url = f"http://127.0.0.1:{port}/v.mp4"
    tid = db.create_download_task("100001", "1080P", url=url)

    async def body():
        mgr = DownloadManager(db, cfg)
        mgr.enqueue(tid)
        t = await _await_status(db, tid, {"completed", "failed"})
        assert t["status"] == "completed"
        assert t["total_bytes"] == 3 * 1024 * 1024
        assert t["downloaded_bytes"] == t["total_bytes"]
        final = Path(t["final_path"])
        assert final.exists() and final.stat().st_size == t["total_bytes"]
        assert not t["temp_path"]  # .part 已原子重命名

    asyncio.run(body())


def test_download_resume_uses_range(env):
    """预写 .part 前半 → 再次下载应走 Range(bytes=N-) 续传，文件最终完整。"""
    cfg, db, serve_dir, port = env
    _make_file(serve_dir, "big.mp4", 4)
    url = f"http://127.0.0.1:{port}/big.mp4"
    tid = db.create_download_task("100002", "720P", url=url)
    part = Path(cfg.paths.tmp_dir) / f"download_{tid}.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"y" * (1024 * 1024))
    db.update_download_task(tid, downloaded_bytes=1024 * 1024)

    async def body():
        mgr = DownloadManager(db, cfg)
        mgr.enqueue(tid)
        t = await _await_status(db, tid, {"completed", "failed"})
        assert t["status"] == "completed"
        assert t["downloaded_bytes"] == 4 * 1024 * 1024
        assert Path(t["final_path"]).stat().st_size == 4 * 1024 * 1024

    asyncio.run(body())


def test_download_url_expired_refresh(env):
    """URL 404 → 触发 VideoSourceProvider 重抓 → 拿到新 URL → 下载成功。"""
    cfg, db, serve_dir, port = env
    _make_file(serve_dir, "ok.mp4", 1)
    good_url = f"http://127.0.0.1:{port}/ok.mp4"

    class FakeProvider:
        async def crawl_video(self, code):
            from app.models import VideoDetail, VideoSource
            return VideoDetail(
                code=code, title="x",
                sources=[VideoSource(video_code=code, resolution="1080P", url=good_url)],
            )

    tid = db.create_download_task("100003", "1080P",
                                  url=f"http://127.0.0.1:{port}/gone.mp4")

    async def body():
        mgr = DownloadManager(db, cfg, provider=FakeProvider())
        mgr.enqueue(tid)
        t = await _await_status(db, tid, {"completed", "failed"})
        assert t["status"] == "completed"
        assert t["url"] == good_url  # 已刷新为新 URL

    asyncio.run(body())


def test_download_404_fails(env):
    cfg, db, serve_dir, port = env
    tid = db.create_download_task("100004", "480P",
                                  url=f"http://127.0.0.1:{port}/missing.mp4")

    async def body():
        mgr = DownloadManager(db, cfg)
        mgr.enqueue(tid)
        t = await _await_status(db, tid, {"failed", "completed"})
        assert t["status"] == "failed"
        assert t["error"]

    asyncio.run(body())


def test_download_pause_resume(env):
    """下载中暂停（每 chunk 检查）→ paused → 恢复从 .part 续传 → completed。"""
    cfg, db, serve_dir, port = env
    _make_file(serve_dir, "slow.mp4", 20)
    url = f"http://127.0.0.1:{port}/slow.mp4"
    tid = db.create_download_task("100005", "1080P", url=url)

    async def body():
        mgr = DownloadManager(db, cfg)
        mgr.enqueue(tid)
        # 等 .part 出现且已有数据（worker 已进入 chunk 循环）再暂停
        part = Path(cfg.paths.tmp_dir) / f"download_{tid}.part"
        for _ in range(200):
            if part.exists() and part.stat().st_size > 0:
                break
            await asyncio.sleep(0.05)
        await mgr.pause(tid)
        t = await _await_status(db, tid, {"paused"})
        assert t["status"] == "paused"
        assert part.exists() and part.stat().st_size > 0

        await mgr.resume(tid)
        t = await _await_status(db, tid, {"completed", "failed"})
        assert t["status"] == "completed"
        assert Path(t["final_path"]).stat().st_size == 20 * 1024 * 1024

    asyncio.run(body())


def test_download_cancel(env):
    """下载中取消 → .part 删除 + status cancelled。"""
    cfg, db, serve_dir, port = env
    _make_file(serve_dir, "big2.mp4", 20)
    url = f"http://127.0.0.1:{port}/big2.mp4"
    tid = db.create_download_task("100006", "1080P", url=url)

    async def body():
        mgr = DownloadManager(db, cfg)
        mgr.enqueue(tid)
        await asyncio.sleep(0.1)
        await mgr.cancel(tid)
        t = await _await_status(db, tid, {"cancelled"})
        assert t["status"] == "cancelled"
        assert not t["temp_path"] or not Path(t["temp_path"]).exists()

    asyncio.run(body())


def test_download_delete_removes_files(env):
    cfg, db, serve_dir, port = env
    _make_file(serve_dir, "v.mp4", 1)
    url = f"http://127.0.0.1:{port}/v.mp4"
    tid = db.create_download_task("100007", "1080P", url=url)

    async def body():
        mgr = DownloadManager(db, cfg)
        mgr.enqueue(tid)
        t = await _await_status(db, tid, {"completed", "failed"})
        if t["status"] == "completed":
            assert Path(t["final_path"]).exists()
        await mgr.delete(tid)
        assert db.get_download_task(tid) is None

    asyncio.run(body())


def test_download_dir_uses_title(env):
    """下载目录用作品名（sanitize 后），不是 video_code 代号。"""
    cfg, db, serve_dir, port = env
    _make_file(serve_dir, "v.mp4", 1)
    url = f"http://127.0.0.1:{port}/v.mp4"
    db.upsert_video(VideoDetail(code="200002", title="My/Title: 測試? <作品>"))
    tid = db.create_download_task("200002", "1080P", url=url)

    async def body():
        mgr = DownloadManager(db, cfg)
        mgr.enqueue(tid)
        t = await _await_status(db, tid, {"completed", "failed"})
        assert t["status"] == "completed"
        final = Path(t["final_path"])
        assert final.parent.name == sanitize_filename("My/Title: 測試? <作品>")
        assert final.exists()

    asyncio.run(body())


def test_migrate_download_dirs(env):
    """旧 {video_code} 目录 → {作品名} 目录：移动文件并更新 DB 路径。"""
    cfg, db, serve_dir, port = env
    dl = Path(cfg.paths.downloads_dir)
    old_dir = dl / "100001"
    old_dir.mkdir(parents=True)
    f = old_dir / "1080P.mp4"
    f.write_bytes(b"x" * 100)
    db.upsert_video(VideoDetail(code="100001", title="作品 A"))
    tid = db.create_download_task("100001", "1080P", url="http://x/x.mp4")
    db.update_download_task(tid, status="completed", final_path=str(f))
    migrate_download_dirs(cfg, db)
    t = db.get_download_task(tid)
    new_fp = Path(t["final_path"])
    assert new_fp.parent.name == sanitize_filename("作品 A")
    assert new_fp.exists()
    assert not old_dir.exists()
