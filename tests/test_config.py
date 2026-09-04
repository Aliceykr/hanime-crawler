"""配置与路径测试：绝对路径、可移植（BASE_DIR 自动探测）、下载目录可配置。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import BASE_DIR, Config, PathsConfig, load_config


def test_base_dir_is_absolute_and_portable():
    assert BASE_DIR.is_absolute()
    # 自动探测 = config.py 所在目录的上一级（工程根）
    assert BASE_DIR == Path(__file__).resolve().parent.parent


def test_paths_derived_from_base_dir():
    p = PathsConfig()
    assert Path(p.db).is_absolute()
    assert Path(p.tmp_dir).is_absolute()
    assert Path(p.downloads_dir).is_absolute()
    assert Path(p.downloads_dir).name == "downloads"
    # 派生目录被自动创建
    assert Path(p.tmp_dir).exists()
    assert Path(p.downloads_dir).exists()


def test_downloads_dir_user_override():
    target = str(Path(__file__).resolve().parent / "my_downloads")
    p = PathsConfig(downloads_dir=target)
    assert p.downloads_dir == target
    assert Path(target).exists()


def test_load_config_defaults():
    cfg = load_config()
    assert cfg.proxy.url == "http://127.0.0.1:7890"
    assert cfg.crawler.max_concurrency == 2
    assert cfg.downloader.chunk_size == 1 * 1024 * 1024
    assert cfg.base_url == "https://hanime1.me/"


def test_config_env_override(monkeypatch):
    monkeypatch.setenv("HANIME_PROXY_URL", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("HANIME_PROXY_ENABLED", "true")
    cfg = load_config()
    assert cfg.proxy.url == "socks5://127.0.0.1:1080"
    assert cfg.proxy.enabled is True
