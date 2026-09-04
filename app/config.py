"""全局配置。所有磁盘路径由 BASE_DIR 自动探测派生（绝对路径、可移植、不依赖 cwd）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ── 工程根：运行时自动探测，随工程移动自动跟随（§5.4 硬约束） ──
BASE_DIR = Path(__file__).resolve().parent.parent


def _abs(*parts: str) -> str:
    """由 BASE_DIR 派生绝对路径；config 内不出现写死的盘符/路径。"""
    return str(Path(BASE_DIR, *parts))


@dataclass
class PathsConfig:
    db: str = field(default_factory=lambda: _abs("data", "app.db"))
    cookies: str = field(default_factory=lambda: _abs("data", "cookies.json"))
    # 下载目录：留空 = <base_dir>/data/downloads；用户填绝对路径则覆盖（覆盖值须为绝对路径）
    downloads_dir: str = ""
    tmp_dir: str = field(default_factory=lambda: _abs("data", ".tmp"))

    def __post_init__(self) -> None:
        if not self.downloads_dir:
            self.downloads_dir = _abs("data", "downloads")
        for d in (Path(self.tmp_dir), Path(self.downloads_dir)):
            d.mkdir(parents=True, exist_ok=True)
        Path(self.db).parent.mkdir(parents=True, exist_ok=True)


@dataclass
class CrawlerConfig:
    max_concurrency: int = 2
    request_interval: float = 1.0      # 秒
    retries: int = 3                   # 正常 HTTP 错误的有限重试次数
    retry_backoff: float = 1.5
    timeout: float = 20.0
    backup_sites: list[str] = field(default_factory=list)  # 用户显式配置才启用；默认空


@dataclass
class ProxyConfig:
    enabled: bool = True               # 目标站需要代理才能访问时设为 true（本机 127.0.0.1:7890）
    url: str = "http://127.0.0.1:7890"  # 支持 socks5:// 形式；仅用于网络可达性


@dataclass
class DownloaderConfig:
    max_concurrency: int = 2
    chunk_size: int = 1 * 1024 * 1024  # 1 MiB


@dataclass
class Config:
    base_dir: Path = BASE_DIR
    base_url: str = "https://hanime1.me/"
    ua: str = (
        "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36"
    )
    paths: PathsConfig = field(default_factory=PathsConfig)
    crawler: CrawlerConfig = field(default_factory=CrawlerConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    downloader: DownloaderConfig = field(default_factory=DownloaderConfig)


def load_config() -> Config:
    """允许环境变量覆盖：HANIME_PROXY_URL / HANIME_PROXY_ENABLED / HANIME_DOWNLOADS_DIR。"""
    cfg = Config()
    if os.environ.get("HANIME_PROXY_URL"):
        cfg.proxy.url = os.environ["HANIME_PROXY_URL"]
    if os.environ.get("HANIME_PROXY_ENABLED"):
        cfg.proxy.enabled = os.environ["HANIME_PROXY_ENABLED"].lower() in ("1", "true", "yes")
    if os.environ.get("HANIME_DOWNLOADS_DIR"):
        cfg.paths.downloads_dir = os.environ["HANIME_DOWNLOADS_DIR"]
        Path(cfg.paths.downloads_dir).mkdir(parents=True, exist_ok=True)
    return cfg
