# hanime-crawler

基于 [Han1meViewer](https://github.com/misaka10032w/Han1meViewer)（Kotlin Android 客户端，只读参考仓库）逆向提取的 hanime1.me API 情报，用 Python 实现的本地爬虫 + 视频下载工具。前端为 HTML + CSS + 原生 JS（无框架）。

## 功能（里程碑）

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | Client（代理/限速/重试/Cloudflare challenge 停止/备用域名）＋ Parser（首页/搜索/详情/评论/Preview）＋ SQLite ＋ FastAPI（缓存优先 202 轮询）＋ HTML 前端 | ✅ |
| M2 | 评论抓取、Preview、任务系统、pages_meta 结构检测（改版提示） | ✅ |
| M3 | CSV/JSON 导出、统计、前端完善 | ✅ |
| M4 | 下载器（队列/并发/Range/断点续传/暂停恢复取消/URL 失效重解析）＋ 下载目录用户可配置 ＋ 定时增量 | ✅ |

## 快速开始

```powershell
# 1) 安装依赖（Python 3.11+）
pip install -r requirements.txt

# 2) 启动（默认 http://127.0.0.1:8000）
python -m app.main
# 或：uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3) 浏览器打开
http://127.0.0.1:8000
```

## 网络与代理

目标站需要代理才能访问，默认代理 `http://127.0.0.1:7890`（在 `app/config.py` 或环境变量修改）：

```powershell
$env:HANIME_PROXY_URL = "http://127.0.0.1:7890"   # 支持 socks5://
$env:HANIME_PROXY_ENABLED = "true"
```

合规边界（PLAN §9）：检测到 Cloudflare challenge 时**停止自动重试**并提示人工处理，不自动过盾、不绕过验证码/登录；备用域名仅在用户显式配置后用于故障切换。

## 下载与断点续传

- 详情页点击“下载”创建任务，立即返回任务 ID（不阻塞）；下载管理页可暂停/恢复/取消/删除。
- 支持 `Range` 自动检测（服务器不支持则从头下载）、流式 1 MiB 分块（不整块进内存）、`.part` 临时文件 → 完成后原子重命名（崩溃/关机/断网不产生“看似完成”的坏文件）。
- 视频 CDN URL 会过期：下载失败会自动重新抓详情页刷新 URL 后继续。
- 下载目录默认 `data/downloads/{video_code}/{resolution}.mp4`，可在 UI“下載”页或 `PUT /api/settings` 修改为任意绝对路径；也可用环境变量 `HANIME_DOWNLOADS_DIR`。

## 路径与可移植性

所有路径由 `BASE_DIR` 自动探测派生（绝对路径、不依赖 cwd、不写死盘符）：
- 源码运行：`app/config.py` 的上上级 → 整个工程目录可移动到任意位置后照常运行（`data/` 自动创建）。
- exe 运行（PyInstaller 冻结）：`BASE_DIR = exe 所在目录` → `data/`、下载文件都落在 exe 旁，exe 可整体拷贝到任何机器/目录。

## 打包为 exe（保留前端设计不变）

依赖 PyInstaller：

```powershell
pip install pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --console --name hanime-crawler `
  --add-data "app/static;app/static" `
  --hidden-import uvicorn.logging `
  --hidden-import uvicorn.loops --hidden-import uvicorn.loops.auto `
  --hidden-import uvicorn.protocols.http.auto `
  --hidden-import uvicorn.protocols.websockets.auto `
  --hidden-import uvicorn.lifespan.on `
  --hidden-import anyio._backends._asyncio `
  launcher.py
```

产物：`dist/hanime-crawler.exe`（约 22 MB，单文件）。双击运行后打开 http://127.0.0.1:8000；首次启动需等待解压数秒。exe 旁的 `data/`（数据库/下载）自动创建，exe 移到别处即带着数据走。

> 打包入口是顶层 `launcher.py`（绝对导入），源码开发仍用 `python -m app.main`。PyInstaller 产物（dist/build/*.spec）不入库。

## 测试

```powershell
python -m pytest tests -q
```

- 离线单元/集成测试（不联网）：`tests/test_parser.py`（用 `tests/fixtures/` 真实页面样本）、`test_database.py`、`test_config.py`、`test_api.py`（mock 抓取层）。
- 下载器测试 `tests/test_download.py`：本地 HTTP 服务器模拟视频源，覆盖完整下载 / Range 断点续传 / 暂停-恢复 / 取消 / URL 失效刷新 / 404 失败。
- 验收：先跑测试 → 再起服务器做真实端到端冒烟（首页/搜索/详情轮询/真实下载控制流）→ 整体复制目录验证可移植性。

## 数据

- SQLite：`data/app.db`（videos / artists / tags / video_tags / video_sources / comments / crawl_tasks / pages_meta / search_history / downloads）。
- 导出：`GET /api/export?fmt=csv|json` 或 UI 顶栏 CSV/JSON 按钮。
- 统计：`GET /api/stats` 或 UI“統計”。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/home` | 首页分类行（含 `structure_warning`） |
| GET | `/api/search` | 搜索（query/page/genre/sort/date/duration/tags/brands） |
| GET | `/api/video/{code}` | 缓存命中 200；未命中 202 + task_id，前端轮询 `/api/task/{id}` |
| GET | `/api/previews/{date}` | 新番预告（YYYYMM，仅近几个月有数据） |
| GET | `/api/tasks` `/api/pages_meta` `/api/search_history` `/api/stats` | 任务/页面元数据/搜索历史/统计 |
| GET | `/api/export?fmt=csv|json` | 导出 |
| GET/PUT | `/api/settings` | 下载目录、定时刷新间隔 |
| POST | `/api/download` | 创建下载任务 `{video_code, resolution}` → `{id, status: queued}` |
| GET | `/api/downloads` `/api/download/{id}` | 下载列表 / 单任务 |
| POST | `/api/download/{id}/pause|resume|cancel` | 暂停/恢复/取消 |
| DELETE | `/api/download/{id}` | 删除任务及文件 |

## 目录结构

```
hanime-crawler/
├── PLAN.md                 # 定稿计划（API 情报 + 决策表 + 里程碑）
├── requirements.txt
├── README.md
├── app/
│   ├── main.py             # 启动入口
│   ├── config.py           # BASE_DIR 自动探测 + paths/crawler/proxy/downloader + settings 持久化
│   ├── models.py           # Pydantic 模型（含 VideoSource 契约）
│   ├── client.py           # HTTP/代理/限速/重试/challenge 检测/pages_meta
│   ├── parser.py           # HTML→Model 纯函数 + 结构变化检测 + sanitize_filename
│   ├── crawler.py          # 抓取编排 + 后台任务 + 并发信号量
│   ├── database.py         # SQLite 建表/upsert/CRUD（10 张表）
│   ├── downloader.py       # 下载器（队列/Range/断点续传/暂停恢复取消/URL 刷新）
│   ├── api.py              # FastAPI 全部路由
│   └── static/             # index.html + style.css + app.js
├── tests/                  # 离线测试 + fixtures 样本 + 下载器测试
└── data/                   # 运行时生成（gitignore）
```
