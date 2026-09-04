"""FastAPI 路由（§5.3 API 设计）。

- 缓存优先：GET /api/video/{code} 有缓存→200 直返；无→202 {task_id}+后台抓取+前端轮询。
- 首页/搜索为同步抓取（单请求量级），video 详情走任务。
- 全局共享 Crawler 实例（启动时初始化），并发与限速在 crawler/client 内。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .client import ChallengeError, FetchError
from .config import BASE_DIR, load_config
from .crawler import Crawler
from .database import Database

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

    app = FastAPI(title="hanime-crawler", version="0.1.0")

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await crawler.close()

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
            rows = await crawler.crawl_home()
            return {"rows": rows}
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

    # ── 静态前端 ──
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(_STATIC_DIR / "index.html"))

    return app


app = create_app()
