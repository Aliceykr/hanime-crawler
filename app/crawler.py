"""爬虫编排：首页/搜索/视频详情/评论/Preview 抓取，后台 asyncio 任务执行，结果入库。

职责链：client(fetch) → parser(HTML→Model) → database(upsert)。
Crawler 不直接拼接选择器，也不做 HTTP 细节；页面结构变化由 parser 检测（structure_warning）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlencode

from .client import ChallengeError, FetchError, HanimeClient
from .config import Config, load_config
from .database import Database
from .models import Preview, VideoCard, VideoDetail
from .parser import (
    detect_structure_change, parse_comments, parse_home, parse_preview,
    parse_search, parse_video_detail,
)

log = logging.getLogger("hanime.crawler")


def card_to_detail(card: VideoCard) -> VideoDetail:
    """卡片 → 轻量 VideoDetail（缺失字段为 None），供入库；详情抓取后幂等补齐。"""
    return VideoDetail(
        code=card.code, title=card.title, cover_url=card.cover_url,
        duration=card.duration, views=card.views, views_raw=card.views_raw,
        published_at=card.published_at,
    )


class Crawler:
    def __init__(self, config: Optional[Config] = None,
                 client: Optional[HanimeClient] = None,
                 db: Optional[Database] = None):
        self.config = config or load_config()
        self.db = db or Database(self.config)
        self.client = client or HanimeClient(self.config, self.db)
        # 爬虫并发独立于（将来的）下载并发：爬虫并发上限 + 请求限速（§7）
        self._sem = asyncio.Semaphore(self.config.crawler.max_concurrency)

    async def close(self) -> None:
        await self.client.close()

    async def _run(self, coro):
        async with self._sem:
            return await coro

    # ── 具体抓取动作 ──
    async def crawl_home(self) -> dict[str, Any]:
        async def _do():
            html = await self.client.fetch("", kind="home")
            warn = detect_structure_change(html, "home")
            if warn:
                log.warning("home structure change: %s", warn)
            rows = parse_home(html)
            for cards in rows.values():
                for card in cards:
                    self.db.upsert_video(card_to_detail(card))
            return {
                "rows": {t: [c.model_dump() for c in cards] for t, cards in rows.items()},
                "structure_warning": warn,
            }
        return await self._run(_do())

    async def crawl_search(self, query: Optional[str] = None, page: int = 1,
                           filters: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        filters = filters or {}
        params: dict[str, Any] = {"page": page}
        if query:
            params["query"] = query
        for key in ("genre", "sort", "broad", "date", "duration"):
            if filters.get(key):
                params[key] = filters[key]
        for key in ("tags", "brands"):
            vals = filters.get(key) or []
            for v in vals:
                params.setdefault(f"{key}[]", []).append(v)
        url = "search?" + urlencode(params, doseq=True)

        async def _do():
            html = await self.client.fetch(url, kind="search")
            warn = detect_structure_change(html, "search")
            if warn:
                log.warning("search structure change: %s", warn)
            cards, total_pages = parse_search(html)
            for card in cards:
                self.db.upsert_video(card_to_detail(card))
            self.db.add_search_history(query or "", filters)
            return {
                "cards": [c.model_dump() for c in cards],
                "total_pages": total_pages,
                "query": query,
                "structure_warning": warn,
            }
        return await self._run(_do())

    async def crawl_video(self, code: str) -> VideoDetail:
        async def _do():
            html = await self.client.fetch(f"watch?v={code}", kind="video")
            warn = detect_structure_change(html, "video")
            if warn:
                log.warning("video structure change: %s", warn)
            detail = parse_video_detail(html, code)
            self.db.upsert_video(detail)
            if warn is None:
                await self.crawl_comments(code)  # 详情结构正常才尝试评论（评论失败不致命）
            return detail
        return await self._run(_do())

    async def crawl_comments(self, code: str) -> list[dict[str, Any]]:
        """抓取视频评论（loadComment），失败仅记录日志，不影响详情任务。"""
        try:
            text = await self.client.fetch(f"loadComment?type=video&id={code}", kind="comment")
            comments = parse_comments(text)
            self.db.save_comments(code, comments)
            return [c.model_dump() for c in comments]
        except (FetchError, ChallengeError) as e:
            log.warning("comments fetch failed for %s: %s", code, e)
            return []

    async def crawl_preview(self, date: str) -> dict[str, Any]:
        """抓取新番预告页（previews/{YYYYMM}），卡片入库并返回。"""
        async def _do():
            html = await self.client.fetch(f"previews/{date}", kind="preview")
            warn = detect_structure_change(html, "preview")
            if warn:
                log.warning("preview structure change: %s", warn)
            items = parse_preview(html, date)
            for p in items:
                self.db.upsert_video(VideoDetail(
                    code=p.code, title=p.title or f"preview-{p.code}",
                    cover_url=p.cover_url,
                ))
            return {"date": date, "items": [p.model_dump() for p in items],
                    "structure_warning": warn}
        return await self._run(_do())

    # ── 后台任务执行（任务表驱动） ──
    async def run_crawl_task(self, task_id: int, kind: str, target: str,
                             **kwargs: Any) -> None:
        self.db.update_crawl_task(task_id, "running")
        try:
            if kind == "home":
                await self.crawl_home()
            elif kind == "search":
                await self.crawl_search(
                    query=target or None,
                    page=int(kwargs.get("page", 1)),
                    filters=kwargs.get("filters"),
                )
            elif kind == "video":
                await self.crawl_video(target)
            elif kind == "preview":
                await self.crawl_preview(target)
            else:
                raise ValueError(f"unknown task kind: {kind}")
            self.db.update_crawl_task(task_id, "completed")
        except (FetchError, ChallengeError, ValueError) as e:
            self.db.update_crawl_task(task_id, "failed", str(e))
            raise
        except Exception as e:  # 未知异常也记录，任务不悬挂
            self.db.update_crawl_task(task_id, "failed", f"{type(e).__name__}: {e}")
            raise
