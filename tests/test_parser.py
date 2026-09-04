"""Parser 离线测试：完全使用 tests/fixtures/ 样本，不联网。

以 Han1meViewer Parser.kt 逻辑为黄金样本；样本为 2026-09-04 实测抓取
（首页/搜索/详情/评论/Preview），结构已按实际情况校正。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.parser import (
    detect_structure_change, parse_cards, parse_comments, parse_count, parse_home,
    parse_preview, parse_search, parse_video_detail, sanitize_filename,
)

FIX = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8", errors="ignore")


# ── 数值转换 ──
def test_parse_count_variants():
    assert parse_count("1.2萬次") == (12000, "1.2萬次")
    assert parse_count("12.3K") == (12300, "12.3K")
    assert parse_count("1,234") == (1234, "1,234")
    assert parse_count("1234") == (1234, "1234")
    assert parse_count("觀看次數：4768次")[0] == 4768
    assert parse_count(None) == (None, None)
    assert parse_count("") == (None, None)


def test_parse_count_big_unit():
    assert parse_count("1.5M") == (1500000, "1.5M")


# ── 文件名清洗 ──
def test_sanitize_filename():
    assert sanitize_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("COM1.mp4") == "_COM1.mp4"
    assert sanitize_filename("NUL") == "_NUL"
    assert sanitize_filename("") == "untitled"
    assert sanitize_filename("  正常标题  ") == "正常标题"


# ── 卡片 ──
def test_parse_cards_search_filters_ads():
    cards = parse_cards(load("search.html"))
    assert len(cards) >= 10
    # 全部 code 为数字、标题非空
    assert all(c.code.isdigit() for c in cards)
    assert all(c.title for c in cards)
    # 广告卡（erolabs 外链）已被过滤
    assert all(not c.cover_url or "erolabs" not in c.cover_url for c in cards)


def test_parse_home_rows():
    rows = parse_home(load("home.html"))
    assert "最新上市" in rows
    first = rows["最新上市"][0]
    assert first.code.isdigit()
    assert first.title
    assert first.views is not None or first.views_raw is not None


# ── 搜索 ──
def test_parse_search_pagination():
    cards, total = parse_search(load("search.html"))
    assert len(cards) >= 10
    assert total >= 1


# ── 视频详情 ──
def test_parse_video_detail():
    d = parse_video_detail(load("video.html"), "408051")
    assert d.code == "408051"
    assert d.title
    assert d.views is not None and d.views > 0
    assert d.published_at and len(d.published_at) == 10
    assert d.likes is not None and d.likes > 0
    assert d.tags
    assert d.artists and d.artists[0].name
    assert d.description
    # 视频源：3 个（1080P/720P/480P）
    resos = {s.resolution for s in d.sources}
    assert "1080P" in resos and "720P" in resos and "480P" in resos
    assert all(s.url.startswith("http") for s in d.sources)
    assert d.related or d.series_title is not None


def test_parse_video_detail_missing_page():
    # 详情缺失关键节点 → 不抛异常，标题兜底
    d = parse_video_detail("<html><body>error</body></html>", "999999")
    assert d.code == "999999"
    assert d.title
    assert d.views is None
    assert d.sources == []


# ── 评论 ──
def test_parse_comments():
    comments = parse_comments(load("comments.json"))
    assert len(comments) >= 1
    c = comments[0]
    assert c.id.isdigit()
    assert c.author
    assert c.content


def test_parse_comments_invalid_json():
    assert parse_comments("not json") == []
    assert parse_comments('{"comments": ""}') == []


# ── Preview ──
def test_parse_preview():
    items = parse_preview(load("preview.html"), "202604")
    assert len(items) >= 5
    p = items[0]
    assert p.date == "202604"
    assert p.code.isdigit()
    assert p.title
    assert p.cover_url


def test_parse_preview_error_page():
    items = parse_preview("<title>Server Error</title>", "202609")
    assert items == []


# ── 结构变化检测（§5.3）──
def test_detect_structure_change():
    assert detect_structure_change(load("home.html"), "home") is None
    assert detect_structure_change(load("search.html"), "search") is None
    assert detect_structure_change(load("video.html"), "video") is None
    assert detect_structure_change(load("preview.html"), "preview") is None
    # 改版页（HTTP 200 但结构缺失）应触发提示
    assert detect_structure_change("<html><body>empty</body></html>", "home")
    assert detect_structure_change("<html><body>empty</body></html>", "search")
    assert detect_structure_change("<html><body>empty</body></html>", "video")
    assert detect_structure_change("<html><body>empty</body></html>", "preview")
