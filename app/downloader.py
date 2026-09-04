"""下载器（M4，§11.5 单文件多类）。

设计（用户意见 1-12 + PLAN 决策 4-10）：
- 只消费 VideoSource 契约：URL/分辨率/文件名，站点改版下载器不用改。
- 流式下载 chunk=1MiB，绝不一整块读入内存。
- Range 自动检测：已下载 N 字节 → 请求 Range: bytes=N-；206 续传，200 从头。
- 断点续传：始终写 .part，完成后 rename 到最终文件，绝不直接写最终路径。
- 队列：POST 立即返回 {id, status: queued}，Manager 用信号量控制并发（默认 2）。
- 暂停/恢复/取消：每个任务一个控制句柄（pause_event + cancel 标志）。
- URL 过期：403/404/410 或下载失败 → 通过 crawler 重抓详情页刷新 VideoSource。
- 合规边界（§9）：不绕过验证码/Cloudflare challenge/登录；challenge 只记录并提示人工。

内部类：
    DownloadError / DownloadPaused / DownloadCancelled / UrlExpired
    _Control                任务控制句柄（暂停事件 + 取消标志）
    DownloadWorker          单个下载任务（本文件核心逻辑）
    DownloadManager         队列 + 并发 + 生命周期 + 进度查询
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from .config import Config
from .database import Database
from .parser import sanitize_filename

log = logging.getLogger("hanime.downloader")

# 状态机：queued → downloading → completed / paused / failed / cancelled
_DOWNLOADING = ("downloading",)
_RESUMABLE = ("queued", "downloading", "paused")


def migrate_download_dirs(cfg: Config, db: Database) -> None:
    """旧版 {video_code} 下载目录 → {作品名} 目录：移动文件并更新 DB 路径（幂等）。

    仅处理 final_path 真实存在且目标目录不同的任务；同名同大小视为重复删除旧文件，
    同名不同内容保守跳过（保留旧路径）。标题缺失时保持 video_code 目录。
    """
    root = Path(cfg.paths.downloads_dir)
    moved = 0
    for t in db.list_download_tasks(limit=500):
        fp = (t.get("final_path") or "").strip()
        if not fp:
            continue
        old = Path(fp)
        if not old.exists():
            continue
        title = None
        v = db.get_video(str(t.get("video_code") or ""))
        if v and (v.get("title") or "").strip():
            title = v["title"].strip()
        name = sanitize_filename(title) if title else str(t.get("video_code") or "untitled")
        new_dir = root / name
        if new_dir.resolve() == old.parent.resolve():
            continue
        new_fp = new_dir / old.name
        if new_fp.exists():
            if new_fp.stat().st_size == old.stat().st_size:
                old.unlink(missing_ok=True)
                final = new_fp
            else:
                continue  # 同名不同内容：保守跳过
        else:
            new_dir.mkdir(parents=True, exist_ok=True)
            old.rename(new_fp)
            final = new_fp
            try:
                old.parent.rmdir()  # 旧目录已空则删除（非空时忽略）
            except OSError:
                pass
        db.update_download_task(t["id"], final_path=str(final))
        moved += 1
    if moved:
        log.info("migrated %d download dir(s) to title-based names", moved)


class DownloadError(Exception):
    pass


class DownloadPaused(Exception):
    pass


class DownloadCancelled(Exception):
    pass


class UrlExpired(Exception):
    """视频 CDN URL 失效（403/404/410），需重新解析详情页。"""


class _Control:
    """任务控制句柄：pause_event set=暂停；cancel=True=取消。"""

    def __init__(self) -> None:
        self.pause_event = asyncio.Event()
        self.cancel = False


class DownloadWorker:
    """单个下载任务：Range/流式/断点续传/暂停/取消/重试/URL 刷新。"""

    def __init__(self, db: Database, config: Config,
                 provider: Optional[Any] = None,
                 control: Optional[_Control] = None,
                 client: Optional[httpx.AsyncClient] = None) -> None:
        self.db = db
        self.cfg = config
        self.provider = provider          # 刷新 VideoSource 的异步对象（Crawler）
        self.ctrl = control or _Control()
        self._client = client
        self.chunk_size = int(config.downloader.chunk_size or (1024 * 1024))

    # ── 对外入口 ──
    async def run(self, task_id: int) -> None:
        task = self.db.get_download_task(task_id)
        if not task:
            return
        self.db.update_download_task(task_id, status="downloading")
        url = task.get("url")
        for attempt in range(int(self.cfg.downloader.retries or 3) + 1):
            try:
                if not url:
                    url = await self._refresh_url(task_id)
                    if not url:
                        self._fail(task_id, "无法获取视频源（请先抓取详情页）")
                        return
                    self.db.update_download_task(task_id, url=url)
                await self._download_once(task_id, url)
                return
            except DownloadPaused:
                self.db.update_download_task(task_id, status="paused")
                return
            except DownloadCancelled:
                self._cleanup_part(task_id)
                self.db.update_download_task(task_id, status="cancelled")
                return
            except UrlExpired:
                log.warning("download %s url expired, refreshing", task_id)
                self.db.update_download_task(task_id, error="URL 已过期，正在重新获取")
                url = await self._refresh_url(task_id)
                if not url:
                    self._fail(task_id, "视频源 URL 过期且重新解析失败")
                    return
                self.db.update_download_task(task_id, url=url)
                continue
            except (DownloadError, httpx.HTTPError, OSError) as e:
                retry = int(self.cfg.downloader.retries or 3)
                if attempt < retry:
                    wait = 1.5 * (attempt + 1)
                    log.warning("download %s attempt %d failed: %s, retry in %.1fs",
                                task_id, attempt + 1, e, wait)
                    self.db.update_download_task(
                        task_id, retry_count=attempt + 1, error=str(e))
                    await asyncio.sleep(wait)
                    continue
                self._fail(task_id, str(e))
                return
            except Exception as e:  # noqa: BLE001
                self._fail(task_id, str(e))
                return

    # ── 核心下载 ──
    async def _download_once(self, task_id: int, url: str) -> None:
        task = self.db.get_download_task(task_id)
        if not task:
            return
        downloaded = int(task.get("downloaded_bytes") or 0)
        headers = {"Range": f"bytes={downloaded}-"} if downloaded > 0 else {}
        async with self._http().stream("GET", url, headers=headers) as resp:
            if resp.status_code in (403, 404, 410):
                raise UrlExpired()
            if resp.status_code == 416:  # 已下载完整（Range 超出）
                total = int(task.get("total_bytes") or 0)
                if downloaded >= total > 0:
                    self._finalize(task_id, downloaded, total)
                    return
                resp.raise_for_status()
            resp.raise_for_status()

            if resp.status_code == 206:
                total = self._parse_content_range(resp.headers.get("content-range"))
                mode = "ab"
            else:  # 200：服务器不支持 Range，从头下载
                downloaded = 0
                total = self._parse_length(resp.headers.get("content-length"))
                mode = "wb"

            task = self.db.get_download_task(task_id)
            final_dir = self._final_dir(task)
            filename = self._filename(task, url)
            final_path = final_dir / filename
            temp_path = Path(self.cfg.paths.tmp_dir) / f"download_{task_id}.part"
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            self.db.update_download_task(
                task_id, total_bytes=total or task.get("total_bytes"),
                filename=filename, temp_path=str(temp_path), final_path=str(final_path))

            now = time.monotonic()
            last_time, last_bytes = now, downloaded
            speed = 0.0
            with open(temp_path, mode) as fh:
                async for chunk in resp.aiter_bytes(self.chunk_size):
                    if self.ctrl.cancel:
                        raise DownloadCancelled()
                    if self.ctrl.pause_event.is_set():
                        raise DownloadPaused()
                    fh.write(chunk)
                    downloaded += len(chunk)
                    t = time.monotonic()
                    dt = t - last_time
                    if dt >= 0.5:  # 每 0.5s 更新一次速度/进度
                        speed = (downloaded - last_bytes) / dt
                        last_time, last_bytes = t, downloaded
                        self.db.update_download_task(
                            task_id, downloaded_bytes=downloaded,
                            total_bytes=total or task.get("total_bytes"), speed=speed)

            self.db.update_download_task(
                task_id, downloaded_bytes=downloaded,
                total_bytes=total or task.get("total_bytes"), speed=0.0)
            if total and downloaded < total:
                raise DownloadError(
                    f"下载不完整 {downloaded}/{total} 字节，已保留 .part 可断点续传")
            self._finalize(task_id, downloaded, total)

    def _finalize(self, task_id: int, downloaded: int, total: Optional[int]) -> None:
        """完整性检查（有总长则比对）→ 原子 rename → completed。"""
        task = self.db.get_download_task(task_id)
        if not task:
            return
        temp = Path(task["temp_path"] or "")
        final = Path(task["final_path"] or "")
        if total and downloaded != total:
            raise DownloadError(f"大小校验失败 {downloaded}/{total}")
        if not temp.exists() or temp.stat().st_size == 0:
            raise DownloadError("临时文件缺失或为空")
        final.parent.mkdir(parents=True, exist_ok=True)
        temp.replace(final)  # 原子重命名，避免“看似完成”的坏文件
        self.db.update_download_task(
            task_id, status="completed", downloaded_bytes=downloaded,
            total_bytes=total, speed=0.0, error=None, final_path=str(final),
            temp_path=None)

    # ── 辅助 ──
    def _final_dir(self, task: dict[str, Any]) -> Path:
        """下载目录：作品名（sanitize 后）；标题缺失/为空时兜底 video_code。"""
        root = Path(self.cfg.paths.downloads_dir)
        title = None
        v = self.db.get_video(str(task["video_code"]))
        if v and (v.get("title") or "").strip():
            title = v["title"].strip()
        name = sanitize_filename(title) if title else str(task["video_code"])
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _fail(self, task_id: int, message: str) -> None:
        self.db.update_download_task(task_id, status="failed", error=message, speed=0.0)
        log.error("download %s failed: %s", task_id, message)

    async def _refresh_url(self, task_id: int) -> Optional[str]:
        """VideoSourceProvider：通过 crawler 重抓详情页，返回匹配分辨率的当前 URL。"""
        if not self.provider:
            return None
        task = self.db.get_download_task(task_id)
        if not task:
            return None
        try:
            detail = await self.provider.crawl_video(str(task["video_code"]))
            for s in detail.sources:
                if s.resolution == task["resolution"]:
                    return s.url
        except Exception as e:  # noqa: BLE001
            log.warning("refresh source for %s failed: %s", task_id, e)
        return None

    def _filename(self, task: dict[str, Any], url: str) -> str:
        name = f"{task['resolution']}.mp4"
        if url.lower().endswith((".m3u8", ".flv", ".webm", ".mkv")):
            name = f"{task['resolution']}{Path(url.split('?')[0]).suffix}"
        return sanitize_filename(name)

    def _cleanup_part(self, task_id: int) -> None:
        task = self.db.get_download_task(task_id)
        if task and task.get("temp_path"):
            Path(task["temp_path"]).unlink(missing_ok=True)

    @staticmethod
    def _parse_content_range(value: Optional[str]) -> Optional[int]:
        if not value:
            return None
        try:
            return int(value.rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _parse_length(value: Optional[str]) -> Optional[int]:
        try:
            return int(value) if value else None
        except ValueError:
            return None

    def _http(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        proxy = None
        if self.cfg.proxy.enabled and self.cfg.proxy.url:
            proxy = self.cfg.proxy.url
        return httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(30.0, read=30.0),
            proxy=proxy,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/149.0.0.0 Safari/537.36"})


class DownloadManager:
    """下载队列：并发限制 + 生命周期 + 暂停/恢复/取消调度。"""

    def __init__(self, db: Database, config: Config, provider: Optional[Any] = None,
                 client: Optional[httpx.AsyncClient] = None) -> None:
        self.db = db
        self.cfg = config
        self.provider = provider
        self._client = client
        self.sem = asyncio.Semaphore(int(config.downloader.max_concurrency or 2))
        self.controls: dict[int, _Control] = {}
        self.active: set[int] = set()

    async def start(self) -> None:
        """启动时：迁移旧下载目录（code→作品名），并恢复未完成任务。"""
        migrate_download_dirs(self.cfg, self.db)
        for t in self.db.list_download_tasks(limit=500):
            if t["status"] in _RESUMABLE:
                self.enqueue(t["id"])

    def enqueue(self, task_id: int) -> None:
        if task_id in self.active:
            return
        self.active.add(task_id)
        asyncio.create_task(self._run(task_id))

    async def _run(self, task_id: int) -> None:
        try:
            async with self.sem:
                ctrl = self.controls.setdefault(task_id, _Control())
                if ctrl.pause_event.is_set():
                    self.db.update_download_task(task_id, status="paused")
                    await ctrl.pause_event.wait()
                    if ctrl.cancel:
                        await self._finish_cancelled(task_id)
                        return
                worker = DownloadWorker(self.db, self.cfg, self.provider,
                                        control=ctrl, client=self._client)
                await worker.run(task_id)
        except asyncio.CancelledError:
            raise
        finally:
            self.active.discard(task_id)
            self.controls.pop(task_id, None)

    async def pause(self, task_id: int) -> bool:
        task = self.db.get_download_task(task_id)
        if not task:
            return False
        if task["status"] == "paused":
            return True
        ctrl = self.controls.setdefault(task_id, _Control())
        ctrl.pause_event.set()
        if task["status"] not in _RESUMABLE:
            return False
        self.db.update_download_task(task_id, status="paused")
        return True

    async def resume(self, task_id: int) -> bool:
        task = self.db.get_download_task(task_id)
        if not task:
            return False
        ctrl = self.controls.setdefault(task_id, _Control())
        ctrl.pause_event.clear()
        if task["status"] == "paused":
            self.enqueue(task_id)
            return True
        return False

    async def cancel(self, task_id: int) -> bool:
        task = self.db.get_download_task(task_id)
        if not task:
            return False
        ctrl = self.controls.setdefault(task_id, _Control())
        ctrl.cancel = True
        ctrl.pause_event.clear()  # 唤醒可能暂停中的 worker
        if task["status"] in _RESUMABLE:
            # worker 正在跑会自己收尾；若刚 enqueue 未起，直接收尾
            if task_id not in self.active:
                await self._finish_cancelled(task_id)
        return True

    async def delete(self, task_id: int) -> bool:
        await self.cancel(task_id)
        task = self.db.get_download_task(task_id)
        if task and task.get("temp_path"):
            Path(task["temp_path"]).unlink(missing_ok=True)
        if task and task.get("final_path") and Path(task["final_path"]).exists():
            Path(task["final_path"]).unlink(missing_ok=True)
        self.db.delete_download_task(task_id)
        return True

    async def _finish_cancelled(self, task_id: int) -> None:
        task = self.db.get_download_task(task_id)
        if task and task.get("temp_path"):
            Path(task["temp_path"]).unlink(missing_ok=True)
        self.db.update_download_task(task_id, status="cancelled")

    def close(self) -> None:
        if self._client is not None:
            asyncio.get_event_loop().run_until_complete(self._client.aclose())
