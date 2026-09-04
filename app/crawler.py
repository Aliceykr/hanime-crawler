"""爬虫编排：首页/搜索/视频详情抓取，后台 asyncio 任务执行，结果入库。

职责链：client(fetch) → parser(HTML→Model) → database(upsert)。
Crawler 不直接拼接选择器，也不做 HTTP 细节；页面结构变化由 parser 检测。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional
from urllib.parse import urlencode

from .client import ChallengeError, FetchError, HanimeClient
from .config import Config, load_config
from .database import Database
from .models import VideoCard, VideoDetail
from .parser import parse_home, parse_search, parse_video_detail


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
    async def crawl_home(self) -> dict[str, list[dict[str, Any]]]:
        async def _do():
            html = await self.client.fetch("", kind="home")
            rows = parse_home(html)
            for cards in rows.values():
                for card in cards:
                    self.db.upsert_video(card_to_detail(card))
            return {title: [c.model_dump() for c in cards] for title, cards in rows.items()}
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
            cards, total_pages = parse_search(html)
            for card in cards:
                self.db.upsert_video(card_to_detail(card))
            self.db.add_search_history(query or "", filters)
            return {
                "cards": [c.model_dump() for c in cards],
                "total_pages": total_pages,
                "query": query,
            }
        return await self._run(_do())

    async def crawl_video(self, code: str) -> VideoDetail:
        async def _do():
            html = await self.client.fetch(f"watch?v={code}", kind="video")
            detail = parse_video_detail(html, code)
            self.db.upsert_video(detail)
            return detail
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
            else:
                raise ValueError(f"unknown task kind: {kind}")
            self.db.update_crawl_task(task_id, "completed")
        except (FetchError, ChallengeError, ValueError) as e:
            self.db.update_crawl_task(task_id, "failed", str(e))
            raise
        except Exception as e:  # 未知异常也记录，任务不悬挂
            self.db.update_crawl_task(task_id, "failed", f"{type(e).__name__}: {e}")
            raise
