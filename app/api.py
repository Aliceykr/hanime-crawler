"""FastAPI 路由（§5.3 API 设计）。

- 缓存优先：GET /api/video/{code} 有缓存→200 直返；无→202 {task_id}+后台抓取+前端轮询。
- 首页/搜索为同步抓取（单请求量级），video 详情走任务。
- 全局共享 Crawler 实例（启动时初始化），并发与限速在 crawler/client 内。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .client import ChallengeError, FetchError
from .config import BASE_DIR, load_config, load_user_settings, save_user_settings
from .crawler import Crawler
from .database import Database
from .downloader import DownloadManager

log = logging.getLogger("hanime.api")

# 静态前端目录：exe 打包时随 --add-data 解压到 _MEIPASS/app/static
if getattr(sys, "frozen", False):
    _STATIC_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "app" / "static"
else:
    _STATIC_DIR = Path(BASE_DIR) / "app" / "static"


def build_video_payload(db: Database, code: str) -> Optional[dict[str, Any]]:
    import json as _json
    v = db.get_video(code)
    if not v:
        return None
    v["tags"] = db.get_video_tags(code)
    v["artist"] = db.get_video_artist(code)
    v["sources"] = db.get_video_sources(code)
    v["comments"] = db.list_comments(code)
    try:
        v["related"] = _json.loads(v.get("related_json") or "[]")
    except (TypeError, _json.JSONDecodeError):
        v["related"] = []
    v.pop("related_json", None)
    return v


def create_app() -> FastAPI:
    config = load_config()
    db = Database(config)
    db.init_db()
    crawler = Crawler(config, db=db)
    downloader = DownloadManager(db, config, provider=crawler)

    app = FastAPI(title="hanime-crawler", version="0.1.0")

    @app.on_event("startup")
    async def _startup() -> None:
        await downloader.start()
        if config.auto_interval_minutes > 0:
            asyncio.create_task(_auto_refresh_loop(crawler, config.auto_interval_minutes))

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await crawler.close()

    async def _auto_refresh_loop(c: Crawler, minutes: int) -> None:
        while True:
            await asyncio.sleep(minutes * 60)
            try:
                await c.crawl_home()
            except Exception as e:  # noqa: BLE001
                log.warning("auto refresh failed: %s", e)

    def _http_error(e: Exception) -> HTTPException:
        if isinstance(e, ChallengeError):
            return HTTPException(502, f"Cloudflare challenge：{e}")
        if isinstance(e, FetchError):
            return HTTPException(502, str(e))
        return HTTPException(500, f"{type(e).__name__}: {e}")

    # ── 首页 ──
    @app.get("/api/home")
    async def api_home() -> dict[str, Any]:
        try:
            return await crawler.crawl_home()
        except (ChallengeError, FetchError) as e:
            raise _http_error(e)

    # ── 搜索 ──
    @app.get("/api/search")
    async def api_search(query: Optional[str] = None, page: int = 1,
                         genre: Optional[str] = None, sort: Optional[str] = None,
                         date: Optional[str] = None, duration: Optional[str] = None,
                         tags: str = "", brands: str = "") -> dict[str, Any]:
        filters: dict[str, Any] = {}
        for k, v in (("genre", genre), ("sort", sort), ("date", date), ("duration", duration)):
            if v:
                filters[k] = v
        if tags:
            filters["tags"] = [t for t in tags.split(",") if t]
        if brands:
            filters["brands"] = [b for b in brands.split(",") if b]
        try:
            return await crawler.crawl_search(query=query, page=page, filters=filters)
        except (ChallengeError, FetchError) as e:
            raise _http_error(e)

    # ── 新番预告（M2） ──
    @app.get("/api/previews/{date}")
    async def api_previews(date: str) -> dict[str, Any]:
        try:
            return await crawler.crawl_preview(date)
        except (ChallengeError, FetchError) as e:
            raise _http_error(e)

    # ── 任务列表 / 页面元数据（M2） ──
    @app.get("/api/tasks")
    async def api_tasks(limit: int = 20) -> dict[str, Any]:
        return {"items": db.list_crawl_tasks(limit=min(limit, 200))}

    @app.get("/api/pages_meta")
    async def api_pages_meta(limit: int = 20) -> dict[str, Any]:
        return {"items": db.list_page_meta(limit=min(limit, 200))}

    # ── M3：导出与统计 ──
    @app.get("/api/stats")
    async def api_stats() -> dict[str, Any]:
        return db.get_stats()

    @app.get("/api/export")
    async def api_export(fmt: str = "csv"):
        items = db.export_videos()
        from fastapi.responses import Response, PlainTextResponse
        if fmt == "json":
            body = json.dumps(items, ensure_ascii=False, indent=2)
            return Response(content=body, media_type="application/json",
                            headers={"Content-Disposition":
                                     'attachment; filename="hanime_videos.json"'})
        # csv：兼容 Excel（utf-8-sig BOM）
        import io
        import csv as _csv
        buf = io.StringIO()
        if items:
            writer = _csv.DictWriter(buf, fieldnames=list(items[0].keys()))
            writer.writeheader()
            writer.writerows(items)
        return Response(content="\ufeff" + buf.getvalue(),
                        media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 'attachment; filename="hanime_videos.csv"'})

    # ── 视频详情：缓存优先，未命中创建后台任务 ──
    @app.get("/api/video/{code}")
    async def api_video(code: str):
        cached = build_video_payload(db, code)
        # 完整缓存 = 已抓详情且解析到视频源；仅有卡片轻量数据不算（避免返回无源详情）
        if cached is not None and cached.get("sources"):
            return {"cached": True, "video": cached}
        existing = db.find_running_task("video", code)
        if existing is not None:
            return JSONResponse(status_code=202, content={
                "cached": False, "task_id": existing, "status": "queued"})
        task_id = db.create_crawl_task("video", code)
        asyncio.create_task(crawler.run_crawl_task(task_id, "video", code))
        return JSONResponse(status_code=202, content={
            "cached": False, "task_id": task_id, "status": "queued"})

    # ── 任务状态 ──
    @app.get("/api/task/{task_id}")
    async def api_task(task_id: int) -> dict[str, Any]:
        t = db.get_crawl_task(task_id)
        if t is None:
            raise HTTPException(404, "task not found")
        return t

    # ── 搜索历史 ──
    @app.get("/api/search_history")
    async def api_search_history() -> dict[str, Any]:
        return {"items": db.list_search_history()}

    # ── M4：设置（下载目录用户可配置，§0 决策 12） ──
    @app.get("/api/settings")
    async def api_settings() -> dict[str, Any]:
        us = load_user_settings()
        return {
            "downloads_dir": str(Path(config.paths.downloads_dir).resolve()),
            "auto_interval_minutes": config.auto_interval_minutes,
            "proxy_enabled": config.proxy.enabled,
            "proxy_url": config.proxy.url,
            "user_settings": us,
        }

    @app.put("/api/settings")
    async def api_settings_update(payload: dict[str, Any]) -> dict[str, Any]:
        dl = (payload.get("downloads_dir") or "").strip()
        if dl:
            p = Path(dl)
            if not p.is_absolute():
                raise HTTPException(400, "downloads_dir 必须是绝对路径")
            p.mkdir(parents=True, exist_ok=True)
            config.paths.downloads_dir = str(p.resolve())
        auto = payload.get("auto_interval_minutes")
        if isinstance(auto, int) and auto >= 0:
            config.auto_interval_minutes = auto
        save_user_settings({
            "downloads_dir": str(Path(config.paths.downloads_dir).resolve()),
            "auto_interval_minutes": config.auto_interval_minutes,
        })
        return {"ok": True, "downloads_dir": config.paths.downloads_dir}

    # ── M4：下载任务（§0 决策 8/9） ──
    @app.post("/api/download")
    async def api_download_create(payload: dict[str, Any]) -> dict[str, Any]:
        code = str(payload.get("video_code") or "").strip()
        resolution = str(payload.get("resolution") or "").strip()
        if not code or not resolution:
            raise HTTPException(400, "video_code 和 resolution 必填")
        url = None
        for s in db.get_video_sources(code):
            if s["resolution"] == resolution:
                url = s["url"]
                break
        task_id = db.create_download_task(code, resolution, url=url)
        downloader.enqueue(task_id)  # 立即入队返回，不阻塞 HTTP
        return {"id": task_id, "status": "queued"}

    @app.get("/api/downloads")
    async def api_downloads(limit: int = 100) -> dict[str, Any]:
        return {"items": db.list_download_tasks(limit=min(limit, 500))}

    @app.get("/api/download/{task_id}")
    async def api_download_get(task_id: int) -> dict[str, Any]:
        t = db.get_download_task(task_id)
        if t is None:
            raise HTTPException(404, "download task not found")
        return t

    @app.post("/api/download/{task_id}/pause")
    async def api_download_pause(task_id: int) -> dict[str, Any]:
        if not await downloader.pause(task_id):
            raise HTTPException(404, "download task not found")
        return {"id": task_id, "status": db.get_download_task(task_id)["status"]}

    @app.post("/api/download/{task_id}/resume")
    async def api_download_resume(task_id: int) -> dict[str, Any]:
        if not await downloader.resume(task_id):
            raise HTTPException(404, "download task not found")
        return {"id": task_id, "status": db.get_download_task(task_id)["status"]}

    @app.post("/api/download/{task_id}/cancel")
    async def api_download_cancel(task_id: int) -> dict[str, Any]:
        if not await downloader.cancel(task_id):
            raise HTTPException(404, "download task not found")
        return {"id": task_id, "status": db.get_download_task(task_id)["status"]}

    @app.delete("/api/download/{task_id}")
    async def api_download_delete(task_id: int) -> dict[str, Any]:
        if not await downloader.delete(task_id):
            raise HTTPException(404, "download task not found")
        return {"ok": True}

    # ── 静态前端 ──
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(_STATIC_DIR / "index.html"))

    return app


app = create_app()
