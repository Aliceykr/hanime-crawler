"""解析器：HTML/JSON → Pydantic Model。

纯函数、不接触数据库（§4.1/§5.2）：Parser 只负责理解页面结构，
持久化由 database.py（Repository）负责。选择器以 Han1meViewer Parser.kt 为
黄金样本，并按 2026-09 实测页面结构校正（首页行容器 home-rows-videos-wrapper、
source size 数字、skip-page 分页、related 容器 div#related-tabcontent 等）。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Optional

from parsel import Selector

from .models import Artist, Comment, Preview, VideoCard, VideoDetail, VideoSource

_WHITESPACE = re.compile(r"\s+")


def clean(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    t = _WHITESPACE.sub(" ", text).strip()
    return t or None


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


# ── 数值解析：1.2萬→12000、12.3K→12300、1,234→1234、1234→1234 ──
_COUNT_RE = re.compile(r"([\d.,]+)\s*(萬|万|K|k|M|m)?")


def parse_count(text: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    if not text:
        return None, None
    raw = clean(text) or ""
    m = _COUNT_RE.search(raw)
    if not m:
        return None, raw
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None, raw
    unit = m.group(2)
    if unit in ("萬", "万"):
        num *= 10000
    elif unit and unit.lower() == "k":
        num *= 1000
    elif unit and unit.lower() == "m":
        num *= 1000000
    return int(num), raw


# ── 文件名清洗（Windows 非法字符 + 保留名） ──
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, fallback: str = "untitled") -> str:
    name = _ILLEGAL_CHARS.sub("_", name).strip().strip(".")
    if not name:
        return fallback
    base = name.rsplit(".", 1)[0].upper() if "." in name else name.upper()
    if base in _RESERVED_NAMES:
        name = "_" + name
    # 控制字符与尾部空格再清一遍
    return name[:200] or fallback


# ── 卡片（首页/搜索/相关视频共用结构） ──
def parse_card(card: Selector) -> Optional[VideoCard]:
    href = (card.css("a.video-link::attr(href)").get()
            or card.css('a[href*="watch?v="]::attr(href)').get())
    m = re.search(r"watch\?v=(\d+)", href or "")
    if not m:
        return None  # 广告卡（erolabs 等）或无关链接
    code = m.group(1)
    title = clean(card.css("div.title::text, h4.video-title::text").get())
    if not title:
        return None
    cover = card.css("img.main-thumb::attr(src)").get() or card.css("img::attr(src)").get()
    duration = clean(card.css(".thumb-container .duration::text").get()
                     or card.css(".duration::text").get())
    stats = [clean(s) for s in card.css(".stats-container .stat-item::text").getall()]
    views_raw = stats[1] if len(stats) > 1 else None  # 第 1 个为赞比例，第 2 个为观看数
    views, _ = parse_count(views_raw)
    subtitle = clean(card.css(".subtitle").xpath("string()").get() or "")
    uploader, published_at = None, None
    if subtitle:
        parts = [p.strip() for p in subtitle.split("•") if p.strip()]
        if parts:
            uploader = parts[0]
        if len(parts) > 1:
            published_at = parts[-1]
    return VideoCard(
        code=code, title=title, cover_url=cover, duration=duration,
        views=views, views_raw=views_raw, uploader=uploader, published_at=published_at,
    )


def parse_cards(html: str) -> list[VideoCard]:
    sel = Selector(text=html)
    return [c for c in (parse_card(x) for x in sel.css(".horizontal-card")) if c]


# ── 首页：分类行（标题 → 卡片） ──
def parse_home(html: str) -> dict[str, list[VideoCard]]:
    sel = Selector(text=html)
    rows: dict[str, list[VideoCard]] = {}
    for sec in sel.css(".home-rows-section-margin-top"):
        title = clean(sec.css("a.horizontal-row-title h3::text").get())
        cards = [c for c in (parse_card(x) for x in sec.css(".horizontal-card")) if c]
        if title and cards:
            rows.setdefault(title, []).extend(cards)
    return rows


# ── 搜索：卡片 + 总页数（skip-page 控件：value=当前页，/ N=总页数） ──
def parse_search(html: str) -> tuple[list[VideoCard], int]:
    cards = parse_cards(html)
    total_pages = 1
    m = re.search(r'id="skip-page-input"[^>]*value="(\d+)"[^>]*>.{0,300}?/\s*(\d+)', html, re.S)
    if m:
        try:
            total_pages = max(1, int(m.group(2)))
        except ValueError:
            total_pages = 1
    return cards, total_pages


# ── 视频详情 ──
_VIEWS_DATE_RE = re.compile(r"(觀看次數|观看次数)：(.+?)次(?:\s|&nbsp;)*(\d{4}-\d{2}-\d{2})")


def parse_video_detail(html: str, code: str) -> VideoDetail:
    sel = Selector(text=html)
    title = clean(sel.css("#shareBtn-title::text").get()) or f"video-{code}"

    caption = sel.css(".video-caption-text")
    description = clean(caption.xpath("string()").get()) if caption else None
    title_cn = None
    if caption:
        for sib in caption.xpath("preceding-sibling::div"):
            t = clean(sib.xpath("string()").get())
            if t and "觀看次數" not in t and "观看次数" not in t and t != title:
                title_cn = t
                break

    views_raw = None
    published_at = None
    vm = _VIEWS_DATE_RE.search(html)
    if vm:
        views_raw = f"{vm.group(2)}次"
        published_at = vm.group(3)
    views, _ = parse_count(views_raw)

    likes_raw = None
    likes = None
    like_el = sel.css("#video-like-btn")
    if like_el:
        likes_raw = clean(like_el.xpath("string()").get())
        lm = re.search(r"\((\d+)\)", likes_raw or "")
        if lm:
            likes = int(lm.group(1))

    tags = [t for t in (clean(x) for x in sel.css(".single-video-tag a::text").getall()) if t]
    artist_name = clean(sel.css("#video-artist-name::text").get())
    artists = [Artist(name=artist_name)] if artist_name else []

    related = [c for c in (parse_card(x) for x in sel.css("#related-tabcontent .horizontal-card")) if c]
    series_title = clean(sel.css("#playlist-scroll h4::text, "
                                 "#playlist-scroll .video-playlist-title::text").get())

    sources: list[VideoSource] = []
    for s in sel.css("video source[src][type]"):
        url = s.css("::attr(src)").get()
        mime = s.css("::attr(type)").get()
        res = s.css("::attr(size)").get()
        if res and res.isdigit():
            res = f"{res}P"
        if url and res:
            sources.append(VideoSource(video_code=code, resolution=res, url=url, mime_type=mime))
    if not sources:
        sm = re.search(r"const source = '([^']+)'", html)
        if sm:
            sources.append(VideoSource(video_code=code, resolution="auto", url=sm.group(1)))

    return VideoDetail(
        code=code, title=title, title_cn=title_cn, description=description,
        views=views, views_raw=views_raw, likes=likes, likes_raw=likes_raw,
        published_at=published_at, tags=tags, artists=artists,
        related=related, series_title=series_title, sources=sources,
    )


# ── 评论（loadComment 返回 {"comments": "HTML"}） ──
def parse_comments(json_text: str) -> list[Comment]:
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return []
    html = data.get("comments") or ""
    sel = Selector(text=html)
    comments: list[Comment] = []
    for wrap in sel.css("#comment-start .report-btn-wrapper"):
        author = None
        created_at = None
        author_el = wrap.css(".comment-index-text").get()
        if author_el is not None:
            # 作者行：<a>名字 <span>时间</span></a>
            a_sel = wrap.css(".comment-index-text").xpath(".")[0] if False else None
        name_parts = wrap.css(".comment-index-text").xpath(
            "a//text()").getall()
        author = clean("".join(name_parts)) or None
        time_parts = wrap.css(".comment-index-text span::text").getall()
        created_at = clean(" ".join(time_parts)) or None
        content = clean(wrap.css(".comment-index-text").xpath("string()").get()) or None
        cid = wrap.css(".report-btn::attr(data-reportable-id)").get()
        if not cid:
            continue
        likes = None
        like_wrap = wrap.xpath(
            'following-sibling::div[contains(@class,"comment-like-form-wrapper")][1]')
        if like_wrap:
            lm = re.search(r"\d+", like_wrap.xpath("string()").get() or "")
            if lm:
                likes = int(lm.group(0))
        comments.append(Comment(
            id=cid, parent_id=None, author=author or "", content=content or "",
            likes=likes, created_at=created_at,
        ))
    return comments


# ── Preview 新番预告 ──
def parse_preview(html: str, date: str) -> list[Preview]:
    sel = Selector(text=html)
    items: list[Preview] = []
    for node in sel.css(".preview-video-width"):
        href = node.css('a[href*="watch?v="]::attr(href)').get()
        m = re.search(r"watch\?v=(\d+)", href or "")
        if not m:
            continue
        title = clean(node.css(".home-rows-videos-title::text").get())
        cover = node.css("img::attr(src)").get()
        items.append(Preview(date=date, code=m.group(1), title=title, cover_url=cover))
    return items


# ── 页面结构变化检测（§5.3）：HTTP 200 但关键节点缺失 → 提示改版 ──
def detect_structure_change(html: str, kind: str) -> Optional[str]:
    """返回 None=正常；返回描述字符串=可能改版。"""
    sel = Selector(text=html)
    if kind in ("home",):
        if not sel.css(".home-rows-section-margin-top"):
            return "首页缺少 .home-rows-section-margin-top 容器，可能改版"
    elif kind in ("search",):
        if not sel.css(".horizontal-card"):
            return "搜索页无 .horizontal-card，可能改版"
    elif kind in ("video",):
        if not sel.css("#shareBtn-title"):
            return "详情页缺少 #shareBtn-title，可能改版"
    return None
