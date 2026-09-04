"""PyInstaller 打包入口：双击 exe 直接弹出应用窗口（WebView2 内嵌前端）。

- 动态空闲端口启动本地 uvicorn（避免与占用 8000 的进程冲突）
- pywebview 窗口加载本地前端：界面与浏览器版完全一致（HTML/CSS/JS 零改动）
- 关闭窗口即退出进程
- HANIME_NO_WEBVIEW=1 时仅启动服务不弹窗（自动化验证用）
"""

from __future__ import annotations

import os
import socket
import threading
import time


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float = 60.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def main() -> None:
    import uvicorn

    from app.api import app  # noqa: F401  启动时完成数据库初始化、下载队列与路由注册

    # 无窗口模式（自动化验证/手动测试）固定 8000；窗口模式用动态空闲端口避免冲突
    if os.environ.get("HANIME_NO_WEBVIEW"):
        port = 8000
    else:
        port = find_free_port()

    threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=port,
                                   reload=False, log_level="info"),
        daemon=True,
    ).start()
    if not _wait_port(port):
        print("[hanime-crawler] 本地服务启动失败")
        return
    url = f"http://127.0.0.1:{port}"

    if os.environ.get("HANIME_NO_WEBVIEW"):
        print(f"[hanime-crawler] 服务已就绪（无窗口模式）：{url}")
        while True:  # 服务模式常驻，Ctrl+C 退出
            time.sleep(3600)
        return

    import webview

    webview.create_window(
        "Hanime Crawler",
        url,
        width=1280, height=860,
        min_size=(960, 640),
        confirm_close=False,
    )
    webview.start()
    # 窗口关闭 → webview.start() 返回 → 进程退出（uvicorn 为 daemon 线程）
    print("[hanime-crawler] 窗口已关闭，进程退出")


if __name__ == "__main__":
    main()
