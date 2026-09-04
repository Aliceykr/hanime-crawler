"""入口：uvicorn 启动 hanime-crawler Web 服务。

用法：
    python -m app.main            # 127.0.0.1:8000
    uvicorn app.main:app
"""

from __future__ import annotations

import uvicorn

from .api import app  # noqa: F401  启动时完成数据库初始化与路由注册

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
