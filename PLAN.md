# Hanime 爬虫工具 — 项目计划（PLAN v3）

> 项目名：**hanime-crawler**（已于 2026-09-04 由原工作目录 `hame/` 重命名完成，现位于 `C:\Users\k1350\Desktop\hanime-crawler`）
>
> 定位：基于从 `Han1meViewer`（Kotlin Android 客户端）逆向提取的 hanime 站点 API 情报，用 **Python** 编写一个爬虫工具，前端先用**纯 HTML + JS** 实现。
>
> 用途与合规边界（硬约束）：
> - 仅用于个人技术学习与研究；仅抓取站点**公开页面**上展示的信息（HTML DOM 解析，不访问后端数据库）。
> - 不绕过登录、付费墙、DRM、验证码、Cloudflare challenge 或任何访问控制；不做大规模抓取、不批量再分发版权内容。
> - 目标站内容为 R18 成人内容，使用者须年满 18 周岁，并自行遵守所在地法律法规与目标站点条款。
> - 视频/图文版权归原站或原制作方所有，本工具不存储、不修改、不声称拥有原始内容。

---

## 0. 架构总览

核心原则：**Crawler 负责"找到东西"，Parser 负责"看懂东西"，DB 负责"记住东西"，Downloader 负责"拿到东西"。**

M1–M3 主链路：

```
HTTP（client.py）
   ↓
Parser（纯 HTML/JSON → Pydantic Model，不碰数据库）
   ↓
Pydantic Models（VideoCard / VideoDetail / Artist / Comment / Preview / VideoSource）
   ↓
Repository（database.py，upsert / 查询）
   ↓
SQLite（规范化表：videos / artists / tags / video_tags / comments / crawl_tasks / pages_meta / search_history / video_sources）
   ↑
FastAPI（api.py，缓存优先 + 后台任务 + 前端轮询）
   ↑
HTML + JS（static/，vanilla）
```

下载接口**预留但不实现**（M1–M3 不开发下载器）：

```
VideoSource（parser 产出、入库）
   ↓（接口契约，M4 可选实现）
Downloader（app/downloader.py，单文件多类）
```

设计决策（对应评审意见）：

| # | 决策 | 位置 |
|---|---|---|
| 1 | 不把"视频流直链长期有效"作为验收/数据核心假设；`video_sources` 表独立记录，含 `expires_at`，失效只重抓详情页 | §1.5、§4.3 |
| 2 | client 区分"故障切换"与"绕过访问控制"：普通错误有限重试 + 用户配置的备用站点；检测到 Cloudflare challenge 则停止自动重试、记录并提示人工处理，**不做自动过盾** | §5.1、§9 |
| 3 | SQLite 规范化：tags / video_tags 关系表替代 JSON 字段 | §4.3 |
| 4 | views / likes 解析为 `int` + 保留 `_raw` 原始文本 | §4.1 |
| 5 | Parser 严格 HTML → Model，不绑定数据库（Repository 负责持久化） | §4.2、§5.2 |
| 6 | API 缓存优先：有缓存直接返回，无缓存创建后台抓取任务 + 前端轮询，请求不阻塞 | §5.5 |
| 7 | `pages_meta` 增加 `content_hash / elapsed_ms / error`，支持页面结构变化检测 | §4.4 |
| 8 | 下载器从近期里程碑移除（M4 可选）；M1–M3 专注爬虫框架 | §8 |
| 9 | 离线测试 fixtures 纳入 M1；pytest 不联网可跑 | §8、§10 |
| 10 | 扁平化文件结构，Python 核心约 9 个文件，不强制拆分 | §3 |
| 11 | 支持 HTTP/SOCKS5 代理（config 显式配置，httpx 原生），仅用于网络可达性，不改变访问控制边界 | §5.1、§7 |
| 12 | **所有磁盘路径一律使用绝对路径**（`BASE_DIR` 运行时自动探测，不写死、不依赖 cwd），**下载目录用户可自定义**；工程可整体移动到任意位置直接运行 | §5.4、§7 |
| 13 | **Git 工作流：每完成一步即提交一次**（先跑该步测试再 `git commit`）；`Han1meViewer/` 参考仓库**不进入 git**（.gitignore 排除，只留本地参考） | §3.2、§10.4 |
| 14 | 测试分三层：离线单测 + **整体鲁棒性** + **用户可用性**，每步提交前必跑相关用例 | §10 |

---

## 1. 从 Han1meViewer 提取的 API 情报

来源：`Han1meViewer/app/src/main/java/com/yenaly/han1meviewer/logic/network/service/*.kt`、`logic/Parser.kt`、`HanimeResolution.kt`、`assets/search_options/*.json`（参考仓库保留在项目内，只读对照）。

### 1.1 站点与基础请求

| 项 | 值 |
|---|---|
| 主域名 | `https://hanime1.me/` |
| 备用域名（**用户显式配置后才启用**） | `https://hanime1.com/`、`https://hanimeone.me/`、`https://javchu.com/` |
| 请求头 User-Agent | 移动端 Chrome：`Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36`（Han1meViewer 使用） |
| Cookie | 需维护 CookieJar；登录态通过 cookie 维持 |
| CSRF | 写操作需 `X-CSRF-TOKEN` header + `_token` 表单字段，token 从页面 `input[name=_token]` 提取（v1 不实现写操作） |

### 1.2 端点清单（GET，公开数据）

| 端点 | 参数 | 返回 | 对应 Service 方法 |
|---|---|---|---|
| `/` | — | 首页 HTML：banner + 各分类模块 | `getHomePage` |
| `/search` | `page`、`query`、`genre`、`sort`、`broad`、`date`、`duration`、`tags[]`、`brands[]` | 搜索结果页 HTML（分页） | `getHanimeSearchResult` |
| `/watch` | `v`（视频 code，纯数字） | 视频详情页 HTML（含视频流地址） | `getHanimeVideo` |
| `/previews/{date}` | `date`（如 `202606`） | 月度预览页 HTML | `getHanimePreview` |
| `/loadComment` | `type`（`video`/`preview`）、`id` | JSON：`{"comments": "<HTML片段>"}` | `getComments` |
| `/loadReplies` | `id`（评论 id） | JSON：`{"replies": "<HTML片段>"}` | `getCommentReply` |
| `/subscriptions` | `page` | 订阅页 HTML（需登录） | `getMySubscriptions` |
| `/user/{userId}/{type}` | `page` | 用户收藏/稍后看列表页 | `getMyListItems` |
| `/user/{userId}/histories` | `sort`、`page` | 在线观看历史 | `getOnlineWatchHistories` |
| `/user/{userId}/uploaded`、`/uploading` | `sort`、`page` | 创作者上传/审核中视频 | `getUploadedVideos` 等 |
| `/playlist` | `list`（list code）、`page` | 播放列表内容页 | `getMyPlayListItems` |
| `/user/{userId}/playlists` | `page` | 用户播放列表 | `getPlaylists` |
| `/login` | — | 登录页（提取 CSRF token） | `getLoginPage` |

> 登录态写操作端点（`POST /login`、`/like`、`/save`、`/subscribe`、`/createComment` 等）**不实现**，仅记录备用。

### 1.3 页面解析要点（从 Parser.kt 提取）

**首页 `/`**：`#home-rows-wrapper > div` 按顺序对应模块：0 最新上市、1 最新上传、2 里番、3 泡面番、5 Motion Anime、6 3DCG、7 2.5D、8 2D、10 AI生成、11 MMD、12 Cosplay、13 他们在看；banner 在 `#home-banner-wrapper`（图、标题、`watch?v=` 正则取视频 code）。

**视频卡片（首页/搜索/订阅通用）**：`div[class^=horizontal-card]`
- 标题：`div.title, h4.video-title`
- 封面：`img` 第一个 `absUrl(src)`
- 视频 code：`a[href]` → 正则 `watch\?v=(\d+)`
- 时长：`.thumb-container div[class^=duration]`
- 观看数：`.thumb-container div[class^=stat-item]` 第 2 个 → 数值转换（见 §4.1）
- 作者/上传时间：`.subtitle` 或 `.video-meta-data`，用 `•` 分隔

**视频详情 `/watch?v={code}`**：
- 标题：`#shareBtn-title`
- 中文标题/简介：`div.video-details-wrapper` 内 `div[class^=video-caption-text]`（前兄弟节点为中文标题）
- 观看次数+上传日期：正则 `(觀看次數|观看次数)：(.+次) *(\d{4}-\d{2}-\d{2})`
- 标签：`.single-video-tag`（文本去掉 `#` 前缀和 ` (数量)` 后缀）
- 艺术家：`#video-artist-name`（名称）+ 下一兄弟节点（类型）+ 圆形头像 img
- 视频流（核心，产出 `VideoSource`）：
  - 首选：`video#player > source` 子标签，`size` 属性为分辨率（`1080P/720P/480P/240P`），`src` 为直链，`type` 为 MIME
  - 兜底：`#player-div-wrapper` 内 `<script>` 正则 `const source = '(.+)'` 提取
- 相关推荐：`#related-tabcontent`、系列影片：`#playlist-scroll`

**评论 `/loadComment`**：JSON → `comments` 字段（HTML 片段）二次解析：`#comment-start` 子元素每 4 个一组 → 头像/用户名/日期/内容/点赞数/回复数。

**月度预览 `/previews/{date}`**：`div.content-padding > div` 每 2 个一组 → title、cover、brand、releaseDate、tags、相关图。

**分页判断**：`ul.pagination a.page-link[href]` 中 `?page=(\d+)` 最大值，或当前页卡片为空 → 无更多数据。

### 1.4 搜索参数取值（来自 assets/search_options/*.json）

| 参数 | 可选值（search_key） |
|---|---|
| `sort` | `最新上市`、`最新上傳`、`本日排行`、`本週排行`、`本月排行`、`觀看次數`、`讚好比例`、`他們在看`、`時長最長` |
| `duration` | `1 分鐘 +`、`5 分鐘 +`、`10 分鐘 +`、`20 分鐘 +`、`30 分鐘 +`、`60 分鐘 +`、`0 - 10 分鐘`、`0 - 20 分鐘` |
| `date` | `過去 24 小時`、`過去 2 天`、`過去 1 週`、`過去 1 個月`、`過去 3 個月`、`過去 1 年` |
| `genre` / `tags[]` / `brands[]` | 多值，见 `Han1meViewer/app/src/main/assets/search_options/{genre,tags,brands}.json` |

### 1.5 视频流地址（HanimeResolution.kt）

- 分辨率枚举：`1080P`、`720P`、`480P`、`240P`、`Unknown`，从 `<source size>` 提取并按此顺序整理；MIME subtype 映射文件后缀（`mp4/mpeg/avi/3gp/3g2/ogv/ts/webm`）。
- **关键认知**：直链是动态 CDN 地址，可能带时效签名，**不能假设长期有效**，站点也可能随时改变生成方式。
- 因此：解析 `<source>`、提取分辨率、保存"当前时刻的 URL"只是**解析能力**；"URL 长期有效"不是数据库的核心假设。`video_sources` 表记录 `fetched_at` 与（如果能判断的）`expires_at`，URL 失效时**重新抓取详情页**即可（§5.3）。

---

## 2. 技术选型（Python）

| 层 | 选型 | 理由 |
|---|---|---|
| HTTP | `httpx`（async，`httpx[socks]`） | 连接池、超时/重试、流式下载、**HTTP/SOCKS5 代理原生支持** |
| 解析 | `parsel` | 比 BeautifulSoup 快，CSS 语法与 JSoup 接近，便于对照 Parser.kt |
| 后端服务 | `FastAPI + uvicorn` | 同时提供 REST API 与静态 HTML 托管，轻量 |
| 存储 | SQLite（标准库 `sqlite3`） | 单机够用，零配置，**不引入 ORM** |
| 异步任务 | `asyncio` 原生 | **不引入 Celery / Redis 等重量级任务系统** |
| 前端 | 纯 `HTML + CSS + vanilla JS` | 不引前端框架 |

依赖清单（`requirements.txt`）：`httpx[socks]`、`parsel`、`fastapi`、`uvicorn`、`pydantic`（`[socks]` 用于 SOCKS5 代理；只用 HTTP 代理可去掉）。

---

## 3. 项目结构（扁平化，文件数约束）

### 3.1 总体原则

- **功能完整、结构清晰、尽可能少的文件**；禁止为追求过度工程化做不必要的模块拆分。
- 功能相关且代码量较小的模块合并到同一文件；**不为一个类、一个函数、一个数据模型单独建文件**。
- 功能上的解耦通过**类、函数、接口**完成，而不是通过增加文件数量。
- 项目以"一个人可以快速理解、修改、运行、维护"为架构目标，不以企业级架构为目标：宁可一个 600 行的 `parser.py`，也不要五个 100 行的 parser 子文件。

### 3.2 推荐结构（M1 完成后核心代码 ≤ 15 个源码/配置文件）

```
hanime-crawler/
├── PLAN.md                     # 本计划
├── README.md
├── requirements.txt
├── .gitignore                  # 排除 Han1meViewer/、data/、__pycache__ 等（见 §10.4）
├── app/
│   ├── main.py                 # FastAPI 入口：挂路由 + 静态文件 + 启动初始化
│   ├── config.py               # 域名/UA/Cookie/限流/并发/代理/路径 配置；BASE_DIR 自动探测（工程可整体移动），下载目录可自定义
│   ├── models.py               # Pydantic 模型：VideoCard/VideoDetail/Artist/Comment/Preview/VideoSource
│   ├── client.py               # HTTP：请求、Cookie、重试、限速、故障切换（不含自动过盾）
│   ├── parser.py               # 全部解析：首页/卡片/详情/评论/Preview → Model（不碰 DB）
│   ├── crawler.py              # 任务编排：首页/搜索/视频/评论抓取（后台 asyncio 任务）
│   ├── downloader.py           # 下载器（M4 可选，接口预留：VideoSource → Downloader）
│   ├── database.py             # SQLite 初始化/建表/Repository（upsert/查询/抓取任务/下载任务）
│   ├── api.py                  # 全部 FastAPI 路由（缓存优先 + 后台任务 + 轮询）
│   └── static/
│       ├── index.html
│       ├── style.css
│       └── app.js
├── tests/
│   ├── test_parser.py          # 离线 pytest（fixtures 驱动，不联网）
│   └── fixtures/               # home.html / search.html / video.html / comments.json / preview.html
├── Han1meViewer/               # 只读参考仓库（.gitignore 排除，**不提交进 git**；遇问题对照其源码）
└── data/                       # 运行时生成，gitignore；路径由自动探测的 BASE_DIR 派生（随工程移动），可被配置覆盖
```

Python 核心文件 **约 9 个**，前端 **3 个**，测试按需增加。

### 3.3 模块合并规则

| 文件 | 职责 |
|---|---|
| `client.py` | HTTP 请求、Cookie、重试、限速、故障切换 |
| `parser.py` | 首页、卡片、视频详情、评论、Preview 等**所有** HTML/JSON 解析 |
| `crawler.py` | 首页抓取、搜索抓取、视频抓取、评论抓取、任务编排 |
| `downloader.py` | 下载队列、Worker、Range、断点续传、暂停/恢复/取消、下载状态（**单文件多类**） |
| `database.py` | SQLite 初始化、建表、查询、upsert、下载任务、抓取任务 |
| `api.py` | 所有 FastAPI 路由 |

不再强制拆 `parser/{home,card,video,comment,preview}.py`、`crawler/{client,endpoints,tasks}.py`、`downloader/{manager,worker,http,resume,storage,models}.py`；除非后期代码量明显增长。

### 3.4 文件拆分阈值

仅当满足以下**任一**条件才拆分：
1. 单个 Python 文件超过约 800～1000 行；
2. 一个文件出现明显互不相关的功能；
3. 单元测试难以独立组织；
4. 某个模块需要被多个项目复用；
5. 修改一个功能经常需要处理大量无关代码。

### 3.5 Downloader 特别约束（M4）

- 保持为**一个主要模块** `app/downloader.py`，内部使用多个类：`DownloadManager`、`DownloadWorker`、`DownloadTask`、`DownloadState`，不拆文件。
- 功能仍完整支持：下载队列、并发限制、流式下载、Range、断点续传、暂停/恢复/取消、重试、进度/速度统计、`.part` 临时文件、完成后原子重命名、URL 失效后经 `VideoSourceProvider` 重新获取。
- **路径规则（可移植）**：下载最终目录与 `.tmp` 临时目录均为 **config.paths 中的绝对路径**；`BASE_DIR` 在运行时由 `config.py` 自身位置自动探测（`Path(__file__).resolve().parent.parent`，即工程根），**工程整体移动/拷贝后自动跟随，无需改配置**；`downloads_dir` 用户可自定义，默认 `BASE_DIR/data/downloads`；代码内不写死任何具体盘符/路径。

---

## 4. 数据模型

### 4.1 领域模型（Pydantic，Parser 的输出）

Parser **只负责 HTML/JSON → Model**，不直接写数据库（§5.2）。

```python
class VideoSource(BaseModel):
    video_code: str
    resolution: str          # 1080P / 720P / 480P / 240P / Unknown
    url: str
    mime_type: str | None
    fetched_at: datetime
    expires_at: datetime | None   # 如果能从响应/URL 判断签名时效则填，否则 None

class Artist(BaseModel):
    name: str
    genre: str | None
    avatar_url: str | None

class VideoCard(BaseModel):       # 首页/搜索卡片
    video_code: str
    title: str
    cover_url: str
    duration: str | None
    views: int | None             # 数值化
    views_raw: str | None         # 原始文本，如 "1.2萬次"
    artist: str | None
    upload_time: str | None

class VideoDetail(BaseModel):     # 详情页
    video_code: str
    title: str
    chinese_title: str | None
    intro: str | None
    cover_url: str
    views: int | None
    views_raw: str | None
    upload_time: str | None
    likes: int | None             # 数值化
    likes_raw: str | None
    tags: list[str]
    artist: Artist | None
    sources: list[VideoSource]    # 每个分辨率一个
    related: list[VideoCard]
    playlist: list[VideoCard]

class Comment(BaseModel): ...     # id/username/content/date/likes/parent_id/reply_count
class Preview(BaseModel): ...     # title/cover/brand/release_date/tags/related_pics
```

**数值解析规范（对应"1.2萬 / 12.3K / 1,234 / 1234"多格式）**：

```python
def parse_count(raw: str | None) -> tuple[int | None, str | None]:
    # 返回 (数值, 原始文本)
    # "1.2萬次" → (12000, "1.2萬次")；"12.3K" → (12300, "12.3K")
    # "1,234" → (1234, "1,234")；"1234" → (1234, "1234")；无法解析 → (None, raw)
```

同时保留原始文本，站点格式变化时容易排查。

### 4.2 解析与存储严格分离

```
HTML/JSON
   ↓
parser.py（纯函数：HTML → Pydantic Model）
   ↓
crawler.py（调用 parser，把 Model 交给 Repository）
   ↓
database.py（Repository：upsert_video / upsert_artist / upsert_tag ...）
   ↓
SQLite
```

收益：可以拿 `Han1meViewer` 的 Parser.kt 当"黄金样本"离线对照测试 parser.py（输入同一份 HTML，比对解析结果字段）。

### 4.3 SQLite 表（规范化，M1 建表）

```
videos ──┬── artist_id ──→ artists
         └── video_tags ──→ tags          （多对多关系表，替代 JSON 数组）
video_sources（每分辨率一行，含 expires_at）
comments
crawl_tasks
pages_meta
search_history
downloads（M4 预留，M1-M3 不建表不实现）
```

| 表 | 关键字段 |
|---|---|
| `videos` | `code`(PK)、`title`、`chinese_title`、`intro`、`cover_url`、`duration`、`views`、`views_raw`、`likes`、`likes_raw`、`upload_time`、`artist_id`(FK)、`created_at`、`updated_at` |
| `artists` | `id`(PK)、`name`、`genre`、`avatar_url` |
| `tags` | `id`(PK)、`name`(UNIQUE) |
| `video_tags` | `video_code`(FK)、`tag_id`(FK)，联合主键 |
| `video_sources` | `id`(PK)、`video_code`(FK)、`resolution`、`url`、`mime_type`、`fetched_at`、`expires_at`、`status`(valid/expired) |
| `comments` | `id`、`video_code`、`username`、`content`、`date`、`likes`、`parent_id`、`reply_count` |
| `crawl_tasks` | `id`、`type`(home/search/video/comment)、`params`(JSON)、`status`、`total`、`done`、`error`、`created_at`、`updated_at` |
| `pages_meta` | 见 §4.4 |
| `search_history` | `id`、`query`、`params`(JSON)、`created_at` |

> 规范化后，后续"按 genre / tag / artist 筛选"用关系表直接 JOIN，比 JSON 字段舒服得多。
> `downloads` 表字段设计（M4 启用时建）：`id`、`video_code`、`resolution`、`url`、`filename`、`temp_path`、`final_path`、`total_bytes`、`downloaded_bytes`、`status`(queued/downloading/paused/completed/failed/cancelled)、`speed`、`error`、`retry_count`、`created_at`、`updated_at`。

### 4.4 pages_meta（页面抓取记录，支持结构变化检测）

| 字段 | 说明 |
|---|---|
| `url` | 抓取的 URL（唯一） |
| `fetched_at` | 抓取时间 |
| `http_code` | HTTP 状态码 |
| `content_hash` | 页面内容哈希（如 sha1） |
| `elapsed_ms` | 请求耗时 |
| `error` | 错误信息（超时/网络/解析异常），成功为 NULL |

**结构变化检测逻辑**（crawler 或启动检查时）：

```
HTTP 200 且 content_hash 与上次不同
   ↓
重新解析，卡片数/字段命中率骤降（如 cards = 0）
   ↓
提示："可能发生页面结构变化"，不再静默吞掉
```

这解决爬虫项目最常见的坑："页面 200，但解析突然全空"。

---

## 5. 核心模块设计

### 5.1 client.py — HTTP 会话（故障切换 ≠ 绕过访问控制）

```
请求
├─ 正常 HTTP 错误（5xx / 网络超时）
│    ↓ 有限次数重试（3 次指数退避）
│    ↓ 仍失败
│    ↓ 记录失败原因（pages_meta.error）
│    ↓ 仅当用户在 config 显式配置了备用站点 → 才切换重试
└─ 检测到 Cloudflare challenge（质询页特征）
     ↓ 停止自动重试（不做自动 cf_clearance / 指纹模拟 / JS challenge 破解）
     ↓ 记录 challenge 事件
     ↓ 提示用户人工处理或更换合法可访问入口
```

- `AsyncClient` 单例：默认 UA（§1.1）、`follow_redirects=True`、超时 15s。
- CookieJar 持久化 `config.paths.cookies`（绝对路径，默认 `BASE_DIR/data/cookies.json`）。
- 限速：信号量 + 每请求间隔（默认 1 req/s），**避免对站点造成压力**。
- **代理支持（网络可达性）**：`config.proxy` 非空时传给 `AsyncClient(proxy=...)`，支持 `http://` 与 `socks5://`（需 `httpx[socks]`）。**当前配置：`http://127.0.0.1:7890`（本机代理，用户提供）**。目标站需要代理才能访问时（如本地网络无法直连），这是唯一依赖项；代理不可用 → 报错记录，**不自动切换、不静默降级**。

### 5.2 parser.py — 纯解析

- 所有函数为纯函数：`parse_home(html) -> HomePage`、`parse_cards(html) -> list[VideoCard]`、`parse_video_detail(html) -> VideoDetail`、`parse_comments(json_str) -> list[Comment]`、`parse_preview(html) -> Preview`。
- 字段缺失不抛错 → 返回 `None` 并记录；用 fixtures 离线测试。
- **不 import database**，不写任何持久化逻辑。

### 5.3 crawler.py — 后台任务编排

- `crawl_home()`、`crawl_search(params, max_pages)`、`crawl_video(code)`、`crawl_comments(code)`。
- `crawl_video(code)`：抓 `/watch?v=` → parse → `upsert_video + upsert_artist + upsert_tags + upsert_sources`；**URL 失效时重抓详情页**即可恢复 `video_sources`。
- 任务状态实时写入 `crawl_tasks`；由 `api.py` 创建为 **asyncio 后台任务**（§5.5）。
- 抓取成功/失败均写 `pages_meta`（含 content_hash / elapsed_ms）。

### 5.4 database.py — SQLite + Repository

- 启动建表（§4.3 全部表，`downloads` 除外）；`sqlite3` + `threading`/`asyncio` 封装（单写连接或每任务连接）。
- **路径规范（硬约束）**：所有磁盘路径——DB 文件、cookies、下载目录、`.tmp`——一律使用由 `BASE_DIR` 派生的**绝对路径**；`BASE_DIR` 运行时自动探测（`Path(__file__).resolve().parent.parent`，即工程根），**不写死具体磁盘位置、不依赖 `Path.cwd()`**；因此工程整体移动/拷贝到任意位置后无需改任何配置即可运行；`downloads_dir` 用户可自定义（§7 paths）。
- Repository 方法：`upsert_video`、`upsert_artist`、`upsert_tags(video_code, tags)`、`upsert_sources`、`get_video(code)`、`get_sources(code)`、`mark_source_expired(id)`、`create_crawl_task`、`update_crawl_task`、`get_page_meta`、`log_page_meta`。

### 5.5 api.py — 缓存优先 + 后台任务 + 轮询（请求不阻塞）

以 `GET /api/video/{code}` 为例：

```
GET /api/video/123456
   ↓
SQLite 有该视频缓存？
   ├─ 有 → 直接返回（200，含元数据 + video_sources）
   └─ 没有
        ↓ 创建 crawl_tasks 记录（status=queued）
        ↓ 启动 asyncio 后台任务抓取（不 await 完成）
        ↓ 返回 202 {"task_id": 7, "status": "queued", "cached": false}
        ↓ 前端轮询 GET /api/task/{id} → 完成后 GET /api/video/{code}
```

路由清单：
- `GET /api/home` → 读库（空则触发后台抓首页）
- `GET /api/search?query=&genre=&sort=&page=` → 同步触发搜索抓取并返回当页结果（搜索量级可控，可等待；也可走后台任务，按实测决定）
- `GET /api/video/{code}` → 缓存优先（上述流程）
- `GET /api/video/{code}/comments` → 同上模式
- `GET /api/task/{id}`、`POST /api/task`、`POST /api/task/{id}/stop`
- `GET /api/pages-meta?changed=1` → 结构变化告警列表（可选）
- `GET /` → 托管 `static/index.html`

---

## 6. 前端（HTML）设计

单页 `index.html`（vanilla JS，移动端优先）：

- **顶部导航**：首页 / 搜索 / 任务
- **首页 Tab**：分类模块网格（封面 + 标题 + 时长 + 观看数），点击进详情
- **搜索面板**：关键词、genre、sort、duration、date、分页；结果网格复用卡片组件；搜索历史本地存储
- **详情页**：封面、标题/中文标题、作者、标签、观看数/上传时间、简介、分辨率列表（展示 VideoSource + 复制直链）、相关推荐、系列列表；未缓存时显示"抓取中"并轮询任务状态
- **任务面板**：发起抓取（类型 + 参数）、任务进度条、停止按钮、结构变化告警提示

交互全部 `fetch('/api/*')`；抓取中的详情页显示进度而不是空白转圈。

---

## 7. 并发与限流（独立配置）

```python
# config.py
paths:
    base_dir = auto                       # 自动探测：Path(__file__).resolve().parent.parent（工程根），随工程移动
    db = "<base_dir>\\data\\app.db"       # 由 BASE_DIR 派生，运行时为绝对路径
    cookies = "<base_dir>\\data\\cookies.json"
    downloads_dir = ""                    # ★ 下载目录：留空 = <base_dir>\data\downloads；填绝对路径则覆盖
    tmp_dir = "<base_dir>\\data\\.tmp"    # 下载临时目录（.part）
    # 规则：config 内不出现任何写死的盘符/路径；全部由 BASE_DIR 派生，或由用户显式覆盖（覆盖值仍须为绝对路径）

crawler:
    max_concurrency = 2
    request_interval = 1.0
    backup_sites = []          # 用户显式配置才启用；默认空

proxy:
    enabled = true             # 目标站需要代理才能访问时设为 true
    url = "http://127.0.0.1:7890"   # 本机代理（用户提供）；支持 socks5:// 形式

downloader:                    # M4 预留
    max_concurrency = 2
    chunk_size = 1 * 1024 * 1024
```

爬虫请求与（将来的）下载量级不同，配置各自独立；代理只用于网络可达性，与故障切换、访问控制无关。**全部路径为绝对路径**：代码不推断、不拼接相对路径、不依赖启动目录；`BASE_DIR` 自动探测使**工程可整体移动到任意位置直接运行**；用户改 `downloads_dir` 即可改变下载落盘位置。

---

## 8. 功能优先级（Milestone v3）

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| **M1 爬虫框架** | client + parser + models + SQLite + API + 基础前端 + **离线 fixtures 测试** | pytest 不联网全绿；能搜索、翻页、打开详情，看到元数据与各分辨率 `VideoSource`（**不以"直链长期有效"为验收**，URL 只是当前快照）；请求不阻塞（缓存命中直接返回） |
| **M2 完善** | 评论 + Preview + 后台任务系统 + 缓存 + pages_meta 结构变化检测 | 评论/预览可抓；任务面板实时进度；页面改版能被检测提示 |
| **M3 数据功能** | 数据导出（CSV/JSON）、统计、更完善前端（筛选/排序/标签页） | 可导出、按 genre/tag/artist 筛选 |
| **M4 可选** | 下载器（VideoSource→Downloader）、登录态、收藏同步、定时增量抓取 | 按需启用 |

> M1–M3 **不实现下载器**，只预留 `VideoSource → Downloader` 接口契约，把爬虫框架本身做扎实。

---

## 9. 合规与风险控制

1. 仅公开页面数据；不碰登录后接口、不绕过 Cloudflare 防护/验证码/DRM。
2. **检测到 Cloudflare challenge 即停止自动重试**，记录并提示人工处理；不做自动 cf_clearance、指纹模拟、JS challenge 破解（代码行为与合规声明一致）。
3. 备用域名仅在用户显式配置后用于故障切换，不用于规避访问控制。
4. 限速 + 并发上限（§7），抓取量控制在个人研究尺度，禁止全站镜像。
5. 不对外提供服务、不发布抓取内容；抓取结果仅本地研究。
6. 目标站结构会变：解析器容错 + pages_meta 结构变化检测，不静默吞错。
7. README 与启动横幅明示用途边界与免责声明（对齐 Han1meViewer 的声明风格）。
8. **代理仅用于网络可达性**（本地网络无法直连目标站时的通道），不用于规避任何访问控制；走代理访问时仍遵守上述全部边界。

---

## 10. 测试与验收（每步提交前必跑）

### 10.1 离线单元测试（pytest，不联网）

- `tests/fixtures/` 保存 `home.html / search.html / video.html / comments.json / preview.html` 真实样本；pytest 完全离线运行，以 Han1meViewer Parser.kt 逻辑为"黄金样本"比对字段（标题/时长/views/code）。
- 数值转换测试：`1.2萬 / 12.3K / 1,234 / 1234` → `(int, raw)`。
- 文件工具测试：`sanitize_filename()`（非法字符 + Windows 保留名）、绝对路径派生。

### 10.2 整体鲁棒性测试（Robustness）

模拟故障场景，验证**不崩、有明确报错/降级**，覆盖：

| # | 场景 | 期望行为 |
|---|---|---|
| R1 | 代理不可用 / 目标站超时 / DNS 失败 | client 有限重试（3 次退避）→ 记录 `pages_meta.error` → 任务置 failed → API 返回可读错误，**进程不崩** |
| R2 | Cloudflare challenge / 验证码页 | **停止自动重试**，记录 challenge，提示人工处理；不无限循环 |
| R3 | 页面结构变化（HTTP 200 但选择器全空） | `content_hash` 变化 + `cards=0` → 告警"可能页面改版"，**不静默写入空数据** |
| R4 | 畸形 HTML / 截断页面 / JSON 内嵌非法 HTML | parser 字段缺失返回 None，不抛异常，单条失败不影响批量 |
| R5 | 数据库异常（文件锁/损坏/磁盘满） | 启动自检并明确报错或重建；写入失败不影响进程存活 |
| R6 | 连续翻页 50 页 | 限速生效，不触发 429 惩罚性响应；并发上限不超配 |
| R7 | 重复请求同一视频 | upsert 幂等，不产生重复行 |
| R8 | API 与前端契约 | fixtures 校验全部 JSON 字段与前端 JS 假设一致（缺字段即测试失败） |

### 10.3 用户可用性测试（Usability）

浏览器全流程走查（`http://127.0.0.1:8000`）：

1. 打开首页 → 分类网格渲染 → 点击卡片进详情。
2. 详情未缓存 → 显示"抓取中"进度 → 完成后自动刷新；**不白屏、不无限转圈、不裸堆栈**。
3. 搜索 → 翻页 → 搜索历史记录可用。
4. 任务面板 → 发起/停止任务 → 进度实时更新。
5. 错误场景 UI：代理失败等异常 → 页面显示可读提示与建议（如"请检查代理 127.0.0.1:7890"），不展示原始异常堆栈。
6. 移动端 375px 宽：布局不溢出、可点区域 ≥ 44px、长标题换行正常。
7. 冷启动：清空 `data/` 后启动 → 自动建目录/建表，页面立即可用。

### 10.4 Git 提交流程（每步必做）

1. 每完成一个功能步骤：先跑 **10.1 离线测试 + 相关 10.2/10.3 用例**，通过后才提交。
2. `.gitignore` 固定排除：`Han1meViewer/`（参考仓库，**永不入库**）、`data/`（运行时数据）、`__pycache__/`、`.venv/`、`.pytest_cache/` 等。
3. 提交命令：`git add <本次涉及文件>` → `git commit -m "step N: <做了什么>（测试：pytest 全绿 / 鲁棒性用例通过）"`。
4. 提交信息必须写明该步内容与测试结果，便于回溯。
5. 遇到问题优先对照 `Han1meViewer/` 源码排查（Parser 选择器、端点参数等）。

---

## 11. 下一步

1. 初始化 git：`git init` + 创建 `.gitignore`（排除 Han1meViewer/、data/ 等）+ 首次提交（PLAN.md + .gitignore）。
2. 初始化 `requirements.txt` + `config.py` + `models.py`（定义 VideoSource 等全部模型）+ `database.py` 建表。
3. 抓取 5 份真实页面样本存入 `tests/fixtures/`，实现 `parser.py` 并跑通离线 pytest。
4. 实现 `client.py` + `crawler.py` + `api.py` + 静态前端，跑通 搜索 → 详情 → 缓存 闭环，交付 M1。
5. 每步完成后按 §10.4 提交 git。

---

## 12. 实施记录（2026-09-04）

| 步骤 | 提交 | 内容 | 测试 |
|---|---|---|---|
| step 0 | 610c10a | git init + .gitignore（排除 Han1meViewer/data）+ PLAN 定稿 | gitignore 核验 |
| step 2 | c083c0c | requirements / config（BASE_DIR 自动探测+代理）/ models / database 建表 | pytest 14 |
| step 3 | 4a6e30c | 5 份真实样本 fixtures + parser（纯函数+广告过滤+结构检测） | pytest 26 |
| step 4 (M1) | 79b885c | client + crawler + api（缓存优先 202 轮询）+ 前端 | pytest 34 + 真实冒烟 |
| step 5 (M2) | e1df049 | 评论 / Preview / 任务列表 / pages_meta / 结构检测接入 | pytest 38 |
| step 6 (M3) | 7f3fde6 | CSV(utf-8-sig)/JSON 导出 + 统计 + 前端 | pytest 40 |
| step 7 (M4) | 4b754e4 | downloader.py（队列/Range/断点续传/暂停恢复取消/URL 刷新）+ 下载 API + settings + 定时增量 | pytest 50 |
| step 8 (验收) | — | 真实冒烟（首页12行/预览10部/完整下载39MB→completed/续传/取消清理）+ 浏览器 UI 全流程 + 整体复制可移植性验证（新目录 50 测试通过） | pytest 50 + 冒烟 + 可移植 |

### 验收结论（§10.2/10.3 对照）

- 鲁棒性 R1 代理不可用/403 → 前端错误提示，不静默直连（client 检测 challenge 停止重试）✅
- R2 URL 过期 → 自动重抓详情刷新 VideoSource 后继续（真实 404 场景 + 单元测试）✅
- R3 崩溃恢复 → .part 残留 + paused/queued 任务在服务重启后由 DownloadManager.start() 恢复 ✅
- R4 数据库并发 → WAL + 单连接 upsert 防锁 ✅
- R5 站点改版 → detect_structure_change 返回 structure_warning，前端提示 ✅
- R6 大文件内存 → 1 MiB 流式分块，不整读 ✅
- R7 并发隔离 → 爬虫 2/请求间隔 1s，下载 2 并发，独立信号量 ✅
- R8 绝对路径/可移植 → BASE_DIR 自动探测，整体复制后 pytest 50 全绿 ✅
- 可用性 7 项（一键启动/UI 可操作/搜索详情缓存/下载进度/导出/设置保存/移动可运行）全部实测 ✅
