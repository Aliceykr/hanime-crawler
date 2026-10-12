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

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .client import ChallengeError, FetchError
from .config import BASE_DIR, Config, load_config, load_user_settings, save_user_settings
from .crawler import Crawler
from .database import Database
from .downloader import DownloadManager

log = logging.getLogger("hanime.api")

# 静态前端目录：exe 打包时随 --add-data 解压到 _MEIPASS/app/static
if getattr(sys, "frozen", False):
    _STATIC_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "app" / "static"
else:
    _STATIC_DIR = Path(BASE_DIR) / "app" / "static"

# ── 封面图片代理客户端：WebView/浏览器直连封面 CDN 会因 Referer 防盗链 403，
#    由后端带站点 Referer（走代理）取图后转给前端（§7.4 修复记录）。 ──
_cover_client: Optional[httpx.AsyncClient] = None
# ── 视频流代理客户端（試看弹窗）：同样带站点 Referer 绕过 CDN 防盗链，支持 Range（播放器 seek）──
_stream_client: Optional[httpx.AsyncClient] = None


def _get_stream_client(config: Config) -> httpx.AsyncClient:
    global _stream_client
    if _stream_client is None:
        proxy = config.proxy.url if config.proxy.enabled else None
        _stream_client = httpx.AsyncClient(
            headers={"User-Agent": config.ua, "Referer": config.base_url},
            proxy=proxy, timeout=httpx.Timeout(30, connect=15),
        )
    return _stream_client


def _get_cover_client(config: Config) -> httpx.AsyncClient:
    global _cover_client
    if _cover_client is None:
        proxy = config.proxy.url if config.proxy.enabled else None
        _cover_client = httpx.AsyncClient(
            headers={"User-Agent": config.ua, "Referer": config.base_url},
            proxy=proxy,
            timeout=config.crawler.timeout,
            follow_redirects=True,
        )
    return _cover_client


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

    # ── 封面图片代理（直连被 CDN Referer 防盗链 403，走后端取图） ──
    @app.get("/api/cover")
    async def api_cover(url: str) -> Response:
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="invalid url")
        try:
            resp = await _get_cover_client(config).get(url)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502,
                                detail=f"cover fetch failed: {type(e).__name__}: {e}")
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"cover upstream {resp.status_code}")
        ctype = resp.headers.get("content-type", "image/jpeg")
        if not ctype.lower().startswith("image/"):
            raise HTTPException(status_code=502, detail=f"not an image: {ctype}")
        return Response(content=resp.content, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=3600"})

    # ── 試看弹窗：视频流代理（带站点 Referer，支持 Range/206 供播放器 seek） ──
    @app.get("/api/stream")
    async def api_stream(url: str, request: Request) -> Response:
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="invalid url")
        headers: dict[str, str] = {}
        range_h = request.headers.get("range")
        if range_h:
            headers["Range"] = range_h
        try:
            # 必须流式：client.get() 会把整个视频缓冲进内存（657MB）才返回，播放器永远转圈
            req = _get_stream_client(config).build_request("GET", url, headers=headers)
            resp = await _get_stream_client(config).send(req, stream=True)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502,
                                detail=f"stream fetch failed: {type(e).__name__}: {e}")
        if resp.status_code not in (200, 206):
            raise HTTPException(status_code=502, detail=f"stream upstream {resp.status_code}")
        ctype = resp.headers.get("content-type", "video/mp4")
        if not ctype.lower().startswith(("video/", "application/octet-stream")):
            raise HTTPException(status_code=502, detail=f"not a video: {ctype}")
        pass_headers = {}
        for k in ("content-type", "content-length", "content-range", "accept-ranges", "cache-control"):
            if resp.headers.get(k):
                pass_headers[k] = resp.headers[k]

        async def _iter():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        return StreamingResponse(_iter(), status_code=resp.status_code, headers=pass_headers)

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

    # ── 视频详情：完整缓存直返；有卡片轻量数据先渲染+后台补详情；否则 202 轮询 ──
    @app.get("/api/video/{code}")
    async def api_video(code: str):
        cached = build_video_payload(db, code)
        if cached is not None and cached.get("sources"):
            return {"cached": True, "partial": False, "video": cached}
        if cached is not None:
            # 已有卡片轻量数据（标题/封面/时长等）：立即返回渲染，后台补抓详情
            if db.find_running_task("video", code) is None:
                task_id = db.create_crawl_task("video", code)
                asyncio.create_task(crawler.run_crawl_task(task_id, "video", code))
            return {"cached": True, "partial": True, "video": cached}
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

    @app.post("/api/download/{task_id}/open_folder")
    async def api_download_open_folder(task_id: int) -> dict[str, Any]:
        """打开已完成文件的所在文件夹（Windows explorer /select 定位文件）。"""
        t = db.get_download_task(task_id)
        if t is None:
            raise HTTPException(404, "download task not found")
        fp = (t.get("final_path") or "").strip()
        if not fp:
            raise HTTPException(400, "任务无最终文件")
        final = Path(fp).resolve()
        dl_root = Path(config.paths.downloads_dir).resolve()
        try:
            final.relative_to(dl_root)
        except ValueError:
            raise HTTPException(403, "路径超出下载目录")
        if not final.exists():
            raise HTTPException(404, "文件不存在")
        import subprocess
        subprocess.Popen(["explorer", "/select,", str(final)])
        return {"ok": True}

    # ── 静态前端 ──
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(_STATIC_DIR / "index.html"))

    return app


app = create_app()
