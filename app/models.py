"""数据模型（Pydantic）。Parser 只负责 HTML → Model，Repository 负责持久化（§4.1）。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── 视频源契约：Downloader 只消费 VideoSource（§0 决策 4/5） ──
class VideoSource(BaseModel):
    video_code: str
    resolution: str                    # 1080P / 720P / 480P / 240P
    url: str
    mime_type: Optional[str] = None
    fetched_at: datetime = Field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None   # 能判断则填；URL 过期只重抓 /watch 详情页


class VideoCard(BaseModel):
    code: str
    title: str
    title_cn: Optional[str] = None
    cover_url: Optional[str] = None
    duration: Optional[str] = None
    views: Optional[int] = None
    views_raw: Optional[str] = None
    uploader: Optional[str] = None
    published_at: Optional[str] = None      # .subtitle 中 • 分隔的日期段原文


class Artist(BaseModel):
    name: str
    slug: Optional[str] = None


class VideoDetail(BaseModel):
    code: str
    title: str
    title_cn: Optional[str] = None
    description: Optional[str] = None
    views: Optional[int] = None
    views_raw: Optional[str] = None
    likes: Optional[int] = None
    likes_raw: Optional[str] = None
    published_at: Optional[str] = None
    duration: Optional[str] = None
    cover_url: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    artists: list[Artist] = Field(default_factory=list)
    related: list[VideoCard] = Field(default_factory=list)
    series_title: Optional[str] = None
    sources: list[VideoSource] = Field(default_factory=list)
    crawled_at: datetime = Field(default_factory=datetime.now)


class Comment(BaseModel):
    id: str
    video_code: Optional[str] = None
    parent_id: Optional[str] = None
    author: str
    content: str
    likes: Optional[int] = None
    created_at: Optional[str] = None


class Preview(BaseModel):
    date: str                            # 202606
    code: str
    title: Optional[str] = None
    cover_url: Optional[str] = None


class CrawlTask(BaseModel):
    id: int = 0
    kind: str                            # home / search / video / comment / preview
    target: str                          # 如视频 code / 搜索词 / 日期
    status: str = "queued"               # queued / running / completed / failed
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class PageMeta(BaseModel):
    url: str
    fetched_at: datetime = Field(default_factory=datetime.now)
    http_code: Optional[int] = None
    content_hash: Optional[str] = None    # sha1
    elapsed_ms: Optional[int] = None
    error: Optional[str] = None


class SearchHistory(BaseModel):
    id: int = 0
    query: str
    filters_json: str = "{}"
    created_at: datetime = Field(default_factory=datetime.now)
