"""PyInstaller 打包入口（顶层脚本，绝对导入，避免相对导入问题）。

源码运行仍用：python -m app.main
打包运行：hanime-crawler.exe（行为一致，data/ 建在 exe 旁）
"""

import uvicorn

from app.api import app  # noqa: F401  启动时完成数据库初始化、下载队列与路由注册

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False, log_level="info")
