"""HTTP 客户端：代理、UA、限速、有限重试、Cloudflare challenge 检测、备用域名故障切换、pages_meta 记录。

边界（§9 合规）：
- 正常 HTTP 错误（5xx/超时/连接失败）→ 有限次数重试 → 仍失败记录原因；
- 检测到 Cloudflare challenge / 验证码 → **停止自动重试**，记录并提示人工，绝不自动过盾；
- 备用域名仅在 config.crawler.backup_sites 显式配置后用于故障切换，不用于规避访问控制；
- 代理仅用于网络可达性，代理不可用时报明确错误，不静默直连。
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

from .config import Config, load_config
from .database import Database
from .models import PageMeta
from .parser import sha1_text

_CHALLENGE_MARKERS = (
    "cf-browser-verification", "challenge-platform", "cf_chl_",
    "Just a moment", "Checking your browser", "cf-error-code",
    "__cf_chl", "cf-chl-widget",
)


class ChallengeError(Exception):
    """Cloudflare 质询：按合规边界停止自动重试。"""


class FetchError(Exception):
    """普通抓取失败（重试用尽）。"""


class HanimeClient:
    def __init__(self, config: Optional[Config] = None, db: Optional[Database] = None):
        self.config = config or load_config()
        self.db = db or Database(self.config)
        self.db.init_db()
        proxy = self.config.proxy.url if self.config.proxy.enabled else None
        self._client = httpx.AsyncClient(
            base_url=self.config.base_url,
            headers={"User-Agent": self.config.ua},
            proxy=proxy,
            timeout=self.config.crawler.timeout,
            follow_redirects=True,
        )
        self._rate_lock = asyncio.Lock()
        self._last_request = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def is_challenge(resp: httpx.Response, text: str) -> bool:
        if resp.status_code == 403 or resp.status_code == 503:
            lower = text.lower()
            if any(m in lower for m in _CHALLENGE_MARKERS):
                return True
            if "cf-ray" in resp.headers and len(text) < 2000:
                return True
        return False

    async def _rate_limit(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            wait = self.config.crawler.request_interval - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    def _save_meta(self, url: str, http_code: Optional[int], elapsed_ms: int,
                   error: Optional[str] = None, text: Optional[str] = None) -> None:
        self.db.save_page_meta(PageMeta(
            url=url, http_code=http_code,
            content_hash=sha1_text(text) if text else None,
            elapsed_ms=elapsed_ms, error=error,
        ))

    async def fetch(self, path: str, kind: Optional[str] = None) -> str:
        """GET 页面并返回 HTML/JSON 文本；失败抛 FetchError / ChallengeError。

        kind 用于 pages_meta 结构检测，预留。
        """
        path = path.lstrip("/") or ""
        primary = f"{self.config.base_url}{path}"
        backups = []
        for site in self.config.crawler.backup_sites:  # 仅显式配置才尝试
            backups.append(f"{site.rstrip('/')}/{path}")
        candidates = [primary, *backups]
        retries = max(1, self.config.crawler.retries)
        last_err: Optional[str] = None

        for attempt in range(retries):
            for url in candidates:
                await self._rate_limit()
                t0 = time.monotonic()
                try:
                    resp = await self._client.get(url)
                    text = resp.text
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    if self.is_challenge(resp, text):
                        self._save_meta(url, resp.status_code, elapsed_ms,
                                        error="cloudflare_challenge", text=text)
                        raise ChallengeError(f"Cloudflare challenge at {url}（请人工处理：更换合法可访问入口或更新访问方式）")
                    if resp.status_code >= 400:
                        self._save_meta(url, resp.status_code, elapsed_ms,
                                        error=f"HTTP {resp.status_code}", text=text)
                        last_err = f"HTTP {resp.status_code} @ {url}"
                        continue  # 换候选地址或重试
                    self._save_meta(url, resp.status_code, elapsed_ms, text=text)
                    return text
                except (httpx.TimeoutException, httpx.ConnectError,
                        httpx.ProxyError, httpx.RemoteProtocolError) as e:
                    self._save_meta(url, None, int((time.monotonic() - t0) * 1000),
                                    error=f"{type(e).__name__}: {e}")
                    last_err = f"{type(e).__name__} @ {url}: {e}"
                    continue
                except ChallengeError:
                    raise
            # 本候选集一轮失败 → 退避后重试
            if attempt < retries - 1:
                await asyncio.sleep(self.config.crawler.retry_backoff)
        raise FetchError(last_err or f"failed to fetch {primary}")
