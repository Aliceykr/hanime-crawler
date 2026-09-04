"""PyInstaller 打包入口（替代 python -m app.main 的字符串导入，保证静态收集 app 包）。

用法：
    python -m app.main            # 源码运行
    hanime-crawler.exe            # 打包后直接运行，行为一致
"""

from __future__ import annotations

import uvicorn

from .api import app  # noqa: F401  启动时完成数据库初始化、下载队列与路由注册

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False, log_level="info")
