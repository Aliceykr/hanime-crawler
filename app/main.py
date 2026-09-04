"""入口：uvicorn 启动 hanime-crawler Web 服务，就绪后自动打开浏览器前端。

用法：
    python -m app.main            # 127.0.0.1:8000 + 自动开浏览器
    hanime-crawler.exe            # 打包后行为一致
    HANIME_NO_BROWSER=1 ...       # 跳过自动打开浏览器
"""

from __future__ import annotations

import os
import socket
import threading
import time
import webbrowser

import uvicorn

from .api import app  # noqa: F401  启动时完成数据库初始化与路由注册

BROWSER_URL = "http://127.0.0.1:8000"


def auto_open_browser() -> None:
    """服务器就绪后自动打开前端：后台线程轮询端口，不阻塞启动。"""
    def _wait() -> None:
        for _ in range(120):  # 最多等 60s（onefile 首次解压较慢）
            try:
                with socket.create_connection(("127.0.0.1", 8000), timeout=0.5):
                    webbrowser.open(BROWSER_URL)
                    print(f"[hanime-crawler] 已在默认浏览器打开 {BROWSER_URL}")
                    return
            except OSError:
                time.sleep(0.5)
    threading.Thread(target=_wait, daemon=True).start()


if __name__ == "__main__":
    if not os.environ.get("HANIME_NO_BROWSER"):
        auto_open_browser()
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False, log_level="info")
