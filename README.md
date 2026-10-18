# TGQQ Forwarder

TGQQ Forwarder 用于把指定 Telegram 消息自动转发到 QQ 官方机器人目标会话。Telegram 侧使用用户账号登录，QQ 侧使用 QQ 官方机器人 WebSocket 方式。

## 功能

- 使用 Telethon 登录一个或多个 Telegram 用户账号。
- 并行监听各账号可见的频道、群组、私聊、Bot 消息。
- 按规则筛选要转发的消息。
- 支持按 Telegram 账号、会话、发送者、Bot 标记、关键词、文本正则、媒体类型匹配。
- 自动提取 Telegram 普通 URL、文字超链接和 URL 按钮；可见 URL 不重复追加，隐藏链接会以“文字: URL”形式显示。
- 优先使用 QQ 原生 markdown 发送，若 QQ 官方接口拒绝 markdown，会自动降级为纯文本。
- 定时清理 `data/media/` 下的 Telegram 媒体下载缓存。
- 自动聚合 Telegram 相册/多图图文消息，避免同一组图文被拆成多次转发。
- 使用 QQ 官方机器人 WebSocket 模式连接 QQ。
- 支持转发到 QQ 群、C2C、频道、频道私信场景。
- 通过 Telegram 管理 Bot 查看状态、管理规则、查询日志。
- 提供 Telegram Mini App 管理台，可用可视化规则工坊管理关键词、目标、模板、日志和暂停状态。
- 内置 RSS / RSSHub 订阅：在管理台「订阅」入口统一管理，新条目自动发布到指定 Telegram 频道，再按既有规则转发到 QQ。可选把非中文条目自动翻译成中文（原文在上、译文在下）。
- 使用 Docker Compose 部署到 VPS。
- 推送 `v*` 标签时，GitHub Actions 自动构建 Docker 镜像并发布到 GitHub Container Registry。

## 重要限制

1. `TELEGRAM_SESSION_PATH` / 各账号 `session_path` 保存 Telegram 用户登录态，等同账号凭据，必须妥善保管。
2. QQ 群目标使用的是 QQ 官方机器人 API 中的 `group_openid`，不是普通 QQ 群号。
3. QQ 官方机器人不同场景的主动发送能力不同。部分群或频道发送可能需要先由 QQ 目标会话给机器人发送一条消息，以便程序缓存最近的 QQ `msg_id`。
4. 媒体转发已支持常见图片、视频、语音、文件场景，但某些 QQ 目标类型不支持直接上传时会退化为文本说明。
5. QQ 原生 markdown 能否显示取决于 QQ 官方机器人权限和消息场景；程序会优先尝试 markdown，失败后自动降级为纯文本。
6. Telegram 匿名管理员消息、频道签名消息、隐私受限的转发消息，可能无法拿到真实发送者 ID。
7. RSS 订阅为进程内单实例调度；Telegram 发送在“响应丢失 / 进程中断”后不能保证 exactly-once，重试可能造成重复发布。条目去重与“首次基线”策略可避免正常情况下的重复推送。
8. RSS 抓取默认禁止访问非公网地址（SSRF 防护）。自建 RSSHub 与转发器同机时，需把主机名加入 `RSS_ALLOWED_PRIVATE_HOSTS` 白名单。
9. RSS 自动翻译使用 OpenAI 兼容的 `/v1/chat/completions` 接口（可自选服务商与模型），译文质量与限流取决于所选服务；翻译失败会自动回退原文，不影响发布。

## 文件结构

```text
app/
  main.py                    # 主入口
  telegram_user/             # Telegram 用户账号登录与监听
  telegram_admin/            # Telegram 管理 Bot
  qq_official/               # QQ 官方机器人 WebSocket 发送模块
  rules/                     # 转发规则匹配与格式化
  rss/                       # RSS / RSSHub 订阅抓取、去重、发布与调度
  storage/                   # SQLite 数据库与 Repository
  worker/                    # 转发队列与 RSS worker
Dockerfile
docker-compose.yml              # VPS 默认使用 GHCR 远程镜像
docker-compose.build.yml        # 本地构建/调试使用
.env.example
.github/workflows/docker-image.yml
```

## VPS 部署

### 1. 安装 Docker 与 Docker Compose

以 Ubuntu/Debian 为例，建议按 Docker 官方文档安装 Docker Engine 和 Compose 插件。安装完成后确认：

```bash
docker version
docker compose version
```

### 2. 准备镜像

默认的 [docker-compose.yml](docker-compose.yml) 不在 VPS 上构建镜像，而是拉取你通过 GitHub Actions 发布到 GHCR 的真实镜像。

先在本地或 GitHub 上打一个版本标签并推送：

```bash
git tag v0.1.0
git push origin v0.1.0
```

GitHub Actions 构建完成后，镜像地址通常是：

```text
ghcr.io/<GitHub用户名或组织名>/<仓库名>:latest
ghcr.io/<GitHub用户名或组织名>/<仓库名>:v0.1.0
```

### 3. 上传部署文件到 VPS

VPS 上至少需要这些文件：

```text
docker-compose.yml
.env.example
```

如果你使用 `git clone` 部署，也可以上传完整项目目录：

```bash
git clone <你的仓库地址> tgqq-forwarder
cd tgqq-forwarder
```

### 4. 创建配置文件

```bash
cp .env.example .env
nano .env
```

必须填写：

```env
TGQQ_IMAGE=ghcr.io/你的GitHub用户名/你的仓库名:latest
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=请替换为你的_api_hash
TELEGRAM_PHONE=+8613800000000
TG_ADMIN_BOT_TOKEN=123456:请替换为你的管理BotToken
ADMIN_TELEGRAM_USER_IDS=11111111,22222222
TG_ADMIN_BOT_CONNECT_TIMEOUT=15
TG_ADMIN_BOT_REQUEST_TIMEOUT=30
TG_ADMIN_BOT_POOL_TIMEOUT=15
TG_ADMIN_BOT_POLL_TIMEOUT=30
TG_ADMIN_BOT_POLL_READ_TIMEOUT=45
# 可选：Bot API（api.telegram.org）代理。留空则沿用容器 HTTP_PROXY / HTTPS_PROXY。
# Telethon 走 MTProto，不受此项影响；RSS 发布与管理 Bot 共用该设置。
# TELEGRAM_BOT_PROXY=http://host.docker.internal:7890
# TELEGRAM_BOT_PROXY=socks5://host.docker.internal:1080
QQ_BOT_APPID=请替换为你的QQ机器人AppID
QQ_BOT_SECRET=请替换为你的QQ机器人Secret
```

说明：

- `TGQQ_IMAGE` 必须填写 GitHub Actions 已发布到 GHCR 的真实镜像地址，例如 `ghcr.io/alice/tgqq-forwarder:latest`。
- `TELEGRAM_API_ID` 和 `TELEGRAM_API_HASH` 从 <https://my.telegram.org/apps> 获取。
- `TG_ADMIN_BOT_TOKEN` 由 Telegram 的 BotFather 创建。
- `ADMIN_TELEGRAM_USER_IDS` 是允许管理本程序的 Telegram 用户 ID，多个 ID 用英文逗号分隔。
- `QQ_BOT_APPID` 与 `QQ_BOT_SECRET` 来自 QQ 官方机器人后台。

### 5. 首次登录 Telegram 用户账号

首次运行前，需要授权 Telegram 用户账号。

**单账号（兼容旧配置）**：

```bash
docker compose run --rm tgqq-forwarder python -m app.telegram_user.login
```

登录成功后，session 文件会保存到：

```text
data/sessions/user.session
```

（Telethon 运行时还可能生成同目录下的 `user.session-journal`，属正常现象。）

**多账号**：在 `.env` 配置 `TELEGRAM_ACCOUNTS_JSON` 后，分别或一次登录全部账号。  
默认每个账号使用独立子目录，避免 `.session` / `.session-journal` 混在同一层：

```text
data/sessions/main/account.session
data/sessions/news/account.session
```

配置示例：

```env
TELEGRAM_SESSIONS_DIR=/app/data/sessions
TELEGRAM_ACCOUNTS_JSON=[{"id":"main","phone":"+8613800000000"},{"id":"news","phone":"+8613900000000"}]
```

```bash
docker compose run --rm tgqq-forwarder python -m app.telegram_user.login --account main
docker compose run --rm tgqq-forwarder python -m app.telegram_user.login --account news
# 或
docker compose run --rm tgqq-forwarder python -m app.telegram_user.login --account all
```

按提示输入验证码。如开启两步验证，还需要输入 Telegram 二步验证密码。  
不要泄露任何 `*.session` 文件。

### 6. 启动服务

默认从 `TGQQ_IMAGE` 指定的 GHCR 镜像拉取并启动：

```bash
docker compose pull
docker compose up -d
```

查看日志：

```bash
docker compose logs -f
```

如果启用了 Mini App，Compose 会把容器内 `${MINI_APP_PORT:-8000}` 端口映射到主机同名端口，请用 HTTPS 反向代理暴露给 Telegram。

停止服务：

```bash
docker compose down
```

更新到最新远程镜像：

```bash
docker compose pull
docker compose up -d
```

如果你明确要在 VPS 上本地构建，而不是使用 GHCR 镜像，请使用本地构建专用文件：

```bash
docker compose -f docker-compose.build.yml up -d --build
```

## 配置项说明

`.env.example` 中的主要配置：

```env
APP_ENV=production
TZ=Asia/Shanghai
LOG_LEVEL=INFO
TGQQ_IMAGE=ghcr.io/你的GitHub用户名/你的仓库名:latest

DATA_DIR=/app/data
LOG_DIR=/app/data/logs
MEDIA_DIR=/app/data/media
DATABASE_URL=sqlite+aiosqlite:////app/data/app.db

TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=请替换为你的_api_hash
TELEGRAM_PHONE=+8613800000000
TELEGRAM_SESSION_PATH=/app/data/sessions/user.session
TELEGRAM_SESSIONS_DIR=/app/data/sessions
# 多账号示例（省略 session_path 时默认 sessions/<id>/account.session）：
# TELEGRAM_ACCOUNTS_JSON=[{"id":"main","phone":"+8613800000000"},{"id":"news","phone":"+8613900000000"}]
TELEGRAM_REQUIRE_ALL_ACCOUNTS=true
TELEGRAM_RECONNECT_ENABLED=true
TELEGRAM_RECONNECT_DELAY_SECONDS=5.0
TELEGRAM_DEDUPE_CROSS_ACCOUNT=false
TELEGRAM_DOWNLOAD_MEDIA=true
TELEGRAM_FORWARD_LINK_PREVIEW_MEDIA=false
TELEGRAM_MAX_MEDIA_MB=20
TELEGRAM_ALBUM_BUFFER_SECONDS=2.0
MEDIA_CLEANUP_INTERVAL_SECONDS=3600
MEDIA_RETENTION_SECONDS=86400

# RSS / RSSHub 订阅（在 Mini App「订阅」入口管理，默认复用管理 Bot 发布）
RSS_ENABLED=true
# 可选：独立发布 Bot 的 token，只用于 sendMessage，不会开启 getUpdates
# RSS_BOT_TOKEN=123456:请替换为发布BotToken
RSS_POLL_SECONDS=10
RSS_REQUEST_TIMEOUT_SECONDS=20
RSS_MAX_FEED_BYTES=2097152
RSS_MAX_ENTRIES_PER_FEED=500
RSS_SEND_INTERVAL_SECONDS=1.1
RSS_MAX_SEND_ATTEMPTS=5
# 非公网地址默认拒绝；自建 RSSHub（如 docker-compose 服务名 rsshub）在此显式登记
RSS_ALLOWED_PRIVATE_HOSTS=
# RSS 条目自动翻译：调用 OpenAI 兼容的 /v1/chat/completions 接口，源语言自动检测，
# 非中文才翻译，原文在上、译文在下。可对接 OpenAI、DeepSeek、通义千问、自建网关等。
# 接口地址可填完整端点（.../v1/chat/completions）或基础地址（.../v1，自动补全）。
# API Key 与接口地址均视为凭据不要外泄；翻译失败自动回退原文，可整体关闭。
RSS_TRANSLATE_ENABLED=true
RSS_TRANSLATE_TARGET=ZH
RSS_TRANSLATE_API_URL=https://api.openai.com/v1/chat/completions
RSS_TRANSLATE_API_KEY=
RSS_TRANSLATE_MODEL=gpt-4o-mini
RSS_TRANSLATE_TIMEOUT_SECONDS=30
RSS_TRANSLATE_MAX_CHARS=1200
RSS_TRANSLATE_MAX_CONCURRENCY=1
RSS_TRANSLATE_MAX_ATTEMPTS=3
RSS_TRANSLATE_RETRY_BACKOFF_SECONDS=1

TG_ADMIN_BOT_TOKEN=123456:请替换为你的管理BotToken
ADMIN_TELEGRAM_USER_IDS=11111111,22222222
TG_ADMIN_BOT_CONNECT_TIMEOUT=15
TG_ADMIN_BOT_REQUEST_TIMEOUT=30
TG_ADMIN_BOT_POOL_TIMEOUT=15
TG_ADMIN_BOT_POLL_TIMEOUT=30
TG_ADMIN_BOT_POLL_READ_TIMEOUT=45
# 可选：Bot API（api.telegram.org）代理。留空则沿用容器 HTTP_PROXY / HTTPS_PROXY。
# Telethon 走 MTProto，不受此项影响；RSS 发布与管理 Bot 共用该设置。
# TELEGRAM_BOT_PROXY=http://host.docker.internal:7890
# TELEGRAM_BOT_PROXY=socks5://host.docker.internal:1080

QQ_BOT_APPID=请替换为你的QQ机器人AppID
QQ_BOT_SECRET=请替换为你的QQ机器人Secret
QQ_ENABLE_GROUP_C2C=true
QQ_ENABLE_GUILD_DIRECT_MESSAGE=false
QQ_ALLOW_SEND_WITHOUT_CACHED_MSG_ID=true
QQ_USE_MARKDOWN=true

MINI_APP_ENABLED=true
MINI_APP_HOST=0.0.0.0
MINI_APP_PORT=8000
MINI_APP_PUBLIC_URL=https://你的域名.example.com
MINI_APP_AUTH_TTL_SECONDS=3600
MINI_APP_ALLOWED_ORIGINS=https://你的域名.example.com

# 单条 QQ 发送（含多图与重试）总超时；超时后记失败并继续消费队列
QQ_SEND_TIMEOUT_SECONDS=120
FORWARD_QUEUE_SIZE=1000
```

## Telegram Mini App 管理台

本项目内置一个 Telegram Mini App 管理台，用于替代复杂的 `/add_rule` 长命令。管理台提供：

- 运行状态总览：Telegram 连接、QQ WebSocket、转发暂停状态、队列深度。
- 规则工坊：选择 Telegram 会话、QQ 目标，编辑关键词、正则、媒体类型、模板并实时预览。
- 规则库：编辑、复制、启用/禁用、删除规则，关键词规则会自动解码显示。
- 日志页：查看最近转发结果和错误。
- 系统页：查看 Mini App 部署状态和排查提示。

### Mini App 配置

`.env` 中启用并配置：

```env
MINI_APP_ENABLED=true
MINI_APP_HOST=0.0.0.0
MINI_APP_PORT=8000
MINI_APP_PUBLIC_URL=https://你的域名.example.com
MINI_APP_AUTH_TTL_SECONDS=3600
MINI_APP_ALLOWED_ORIGINS=https://你的域名.example.com
```

安全规则：

1. Mini App API 会校验 Telegram 传入的 `initData` HMAC 签名，不能伪造前端用户 ID。
2. 只有 `ADMIN_TELEGRAM_USER_IDS` 中的 Telegram 用户能调用管理 API。
3. 前端不会展示 `TG_ADMIN_BOT_TOKEN`、Telegram session、QQ secret 等敏感信息。
4. Telegram Mini App 生产环境必须使用公网 HTTPS；普通 HTTP 或内网地址不能作为正式入口。

### 打开 Mini App

启动服务后，管理 Bot 会注册 `/app` 命令。如果配置了 `MINI_APP_PUBLIC_URL`，可以：

```text
/app
```

然后点击“打开 Mini App 管理台”。Bot 也会尝试把 Telegram 菜单按钮设置为 Mini App 入口；如果 Telegram API 拒绝菜单按钮设置，服务只记录 warning，不影响转发。

也可以在 BotFather 中手动配置 Menu Button/Web App URL 为 `MINI_APP_PUBLIC_URL`。

### 反向代理示例

Caddy：

```caddyfile
你的域名.example.com {
  reverse_proxy 127.0.0.1:8000
}
```

Nginx：

```nginx
server {
    listen 443 ssl http2;
    server_name 你的域名.example.com;

    ssl_certificate /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

排查顺序：

- 浏览器访问 `https://你的域名.example.com/healthz` 应返回 `{"ok":true}`。
- `MINI_APP_PUBLIC_URL` 必须和 BotFather/菜单按钮 URL 一致。
- 管理账号 ID 必须在 `ADMIN_TELEGRAM_USER_IDS` 中。
- 若提示 `init_data_expired`，关闭 Mini App 后从 Telegram 重新打开。
- 若 QQ 目标为空，先在目标 QQ 群/C2C/频道里给机器人发一条消息或 @ 机器人。

## 相册、多图图文、链接、Markdown 与媒体清理

### Telegram 相册/多图图文聚合

Telegram 的多图图文消息在底层可能按多条 `NewMessage` 到达，但它们共享同一个 `grouped_id`。程序会等待一个很短的窗口，把同一组消息合并后再转发，避免出现“第一张图单独发一次，剩余图片和文字又发一次”的情况。

默认配置：

```env
TELEGRAM_ALBUM_BUFFER_SECONDS=2.0
```

含义：同一相册组等待 2 秒聚合。网络较慢或仍偶发拆分时，可调大到 `3.0` 或 `5.0`。设置为 `0` 可禁用聚合。

QQ 官方机器人当前使用的 botpy/v2 富媒体接口通常不能在一条消息里发送多张图片；群/C2C 富媒体使用 `msg_type=7` 且只携带单个 `media`，Markdown 文本使用 `msg_type=2`。程序聚合后仍会按顺序发送多张图：

- 如果聚合后的文本非空，会先发送一条完整文本消息，内容由规则的消息模板渲染。
- 然后按 Telegram 原始顺序逐个发送媒体文件。
- 多媒体场景下，每个媒体消息随附的文字使用规则名；规则名为空时使用 `媒体`。这样可以避免每张图都重复发送完整正文。
- 模板不会暴露本地媒体缓存路径，也不会默认生成 `媒体：photo`、`媒体：photo、video` 这类媒体类型说明。

### 消息模板变量

规则的 `message_template` 使用 Python `str.format` 风格占位符，例如 `{text}`、`{sender_name}`。如果模板中使用了不存在的变量或格式化失败，程序会回退到默认模板。

默认模板：

```text
# {source_title_md}

账号：`{account_id}`

**发送者：{sender_name_md}**

{text}

{footer_note}
```

可用变量如下：

| 变量 | 含义 | 为空或注意事项 |
|---|---|---|
| `{message_id}` | Telegram 消息 ID。 | 多图相册聚合后使用该组第一条消息的 ID。 |
| `{chat_id}` | Telegram 会话 ID。 | 可能是频道、群组、私聊 ID；拿不到时为空字符串。 |
| `{chat_title}` | Telegram 会话标题或用户名。 | 私聊可能显示对方名称；拿不到时为空字符串。 |
| `{chat_type}` | Telegram 会话类型。 | 常见值为 `private`、`group`、`channel`、`unknown`。 |
| `{source_title}` | 面向展示的来源标题。 | 优先为 `Telegram：<会话标题>`；没有标题但有会话 ID 时为 `Telegram：<chat_id>`；都没有时为 `Telegram：unknown`。 |
| `{source_title_md}` | 适合放进 Markdown 的来源标题。 | 基于 `{source_title}`，会转义 `\`、`[`、`]`、`*`、`_`、反引号等字符，避免标题破坏 Markdown。默认模板建议用这个变量。 |
| `{account_id}` | 监听该消息的 Telegram 账号配置 ID。 | 对应 `TELEGRAM_ACCOUNTS_JSON` 里的 `id`；单账号兼容模式通常为 `default`；拿不到时为空字符串。默认模板会显示这一行。 |
| `{account_user_id}` | 监听账号登录后的 Telegram 用户数字 ID。 | 账号尚未完成登录或未 stamp 时为空字符串。 |
| `{account_username}` | 监听账号的 Telegram 用户名。 | 不包含 `@`；没有用户名或未 stamp 时为空字符串。 |
| `{sender_id}` | Telegram 发送者 ID。 | 匿名管理员、频道签名、隐私受限转发等场景可能为空。 |
| `{sender_username}` | Telegram 发送者用户名。 | 不包含 `@`；没有用户名时为空字符串。 |
| `{sender_name}` | 面向展示的发送者名称。 | 优先使用显示名，其次用户名，再其次发送者 ID；都没有时为 `unknown`。 |
| `{sender_name_md}` | 适合放进 Markdown 的发送者名称。 | 基于 `{sender_name}` 转义 Markdown 特殊字符。默认模板建议用这个变量。 |
| `{sender_is_bot}` | 发送者是否是 Telegram Bot。 | 渲染为 Python 布尔值文本 `True` 或 `False`。 |
| `{text}` | 最常用的正文变量。 | 来源是 Telegram 原始文本或媒体 caption，并经过格式化处理：可定位的文字超链接会嵌入为 Markdown 链接；链接预览标题/摘要会在不重复时追加到正文后；关键词规则命中时会用 `***关键词***` 高亮。 |
| `{webpage_title}` | Telegram 链接预览标题。 | 只表示网页预览里的标题，不是 Telegram 会话标题；没有链接预览时为空字符串。 |
| `{webpage_description}` | Telegram 链接预览摘要。 | 没有链接预览摘要时为空字符串。 |
| `{webpage_url}` | Telegram 链接预览 URL。 | 经过规范化；没有链接预览 URL 时为空字符串。 |
| `{webpage_preview_text}` | 链接预览标题和摘要的组合文本。 | 按“标题换行摘要”拼接；缺失的部分会跳过。这个变量是原始预览组合，和 `{text}` 中去重追加后的效果不同。 |
| `{raw_url}` | Telegram 链接预览 URL 的兼容变量。 | 当前通常与 `{webpage_url}` 一致；没有链接预览 URL 时为空字符串。 |
| `{media_type}` | 当前消息的首个媒体类型。 | 常见值包括 `photo`、`video`、`voice`、`audio`、`document`、`media`、`link_preview`；没有媒体时为空字符串。多媒体相册只显示第一项，不建议用于展示完整媒体列表。 |
| `{media_path}` | 本地媒体缓存路径。 | 为避免泄露服务器路径，这个变量始终渲染为空字符串。媒体文件仍会通过 QQ 媒体接口单独发送。 |
| `{media_note}` | 媒体说明兼容变量。 | 当前始终为空字符串；不会生成 `媒体：photo` 或多媒体类型摘要。保留此变量是为了兼容旧模板。 |
| `{links_note}` | 需要额外补全的隐藏链接或按钮链接。 | Markdown 格式，多行形如 `- [按钮文字](https://example.com)`；已在正文中可见的 URL 不会重复追加。 |
| `{plain_links_note}` | 纯文本形式的隐藏链接或按钮链接。 | 多行形如 `- 按钮文字: https://example.com`；适合不想依赖 Markdown 链接渲染的模板。 |
| `{keywords_note}` | 关键词命中说明。 | 只有规则是关键词规则且本条消息命中时才有内容，格式为 `检测到关键词：AI、Python`。 |
| `{footer_note}` | 默认模板使用的尾注组合。 | 由 `{links_note}`、`{media_note}`、`{keywords_note}` 按顺序合并并去掉空项；由于 `{media_note}` 当前为空，实际通常只包含补全链接和关键词说明。 |

补充规则：

- 如果模板没有包含 `{links_note}`、`{plain_links_note}` 或 `{footer_note}`，程序仍会把缺失的隐藏链接/按钮链接追加到消息末尾，避免链接丢失。
- 如果模板没有包含 `{keywords_note}` 或 `{footer_note}`，程序仍会把关键词命中说明追加到消息末尾。
- 普通可见 URL 只要已经出现在正文或渲染结果中，就不会再出现在 `{links_note}` 里。
- 多媒体文件列表本身不是模板变量；它由发送层按顺序逐个上传到 QQ。

### Telegram 超链接转发

程序会提取三类 Telegram 链接：

- 普通 URL，例如 `https://example.com`。这类 URL 已在正文可见时不会重复追加。
- 文字超链接，例如 Telegram 中显示为“点击查看”，实际链接是 `https://example.com`。
- URL 按钮，例如消息下方的“查看回复”“打开网页”等按钮。

默认模板会优先把文字超链接渲染回正文，例如 `[点击查看](https://example.com)`；按钮链接没有 Telegram 正文位置，如果按钮文字没有在正文中唯一出现，则会在末尾以 Markdown 链接补全：

```text
- [查看回复](https://example.com/reply)
```

按钮链接只显示按钮文字本身作为链接，不额外标注“按钮”。即使 QQ 场景无法渲染 markdown，隐藏链接和按钮链接仍会以可见 URL 形式显示。

### Telegram 链接预览媒体

默认配置：

```env
TELEGRAM_DOWNLOAD_MEDIA=true
TELEGRAM_FORWARD_LINK_PREVIEW_MEDIA=false
```

`TELEGRAM_DOWNLOAD_MEDIA` 是媒体下载总开关。默认仍会转发 Telegram 消息真实附带的图片、视频、语音和文件，但不会转发 Telegram 对网页链接自动生成的预览图片。若确实需要转发链接预览媒体，可设置 `TELEGRAM_FORWARD_LINK_PREVIEW_MEDIA=true`。

### QQ Markdown

默认配置：

```env
QQ_USE_MARKDOWN=true
```

程序会优先使用 QQ 原生 markdown 消息。默认模板会把 Telegram 来源作为标题、发送者加粗展示，并在末尾追加隐藏链接、按钮链接和 `检测到关键词：...`。当前不会追加媒体类型摘要。若 QQ 官方接口返回“不允许发送原生 markdown”之类的错误，会自动使用纯文本重发。是否能真正显示 markdown 取决于 QQ 官方机器人权限和目标场景。

### media 目录清理

默认配置：

```env
MEDIA_CLEANUP_INTERVAL_SECONDS=3600
MEDIA_RETENTION_SECONDS=86400
```

含义：每 3600 秒检查一次 `data/media/`，删除修改时间超过 86400 秒的媒体文件。设置任一值为 `0` 可禁用清理任务。

## RSS / RSSHub 订阅推送

链路：**RSS 源 → 内置订阅 worker → Telegram 发布 Bot 发到专用频道 → 既有 Telethon 监听按规则转发到 QQ**。RSS 功能不需要单独的服务，随主程序一起运行。

### 1. 配置订阅

打开 Mini App 管理台，进入「订阅」→「新增订阅」：

- **Feed URL**：RSSHub 路由（如 `https://rsshub.app/github/issue/…`）或任意标准 RSS/Atom 地址。
- **Telegram 目标**：建议填专用频道的**数字 ID**（`-100…`），可从「从监听账号搜索频道」挑选；`@username` 无法作为 TG → QQ 规则的 `source_chat_id`。
- **检查间隔**：60–86400 秒。
- **首次处理策略**：默认「只建立基线」——首次检查只记录已有条目，不推送历史文章；选「发送最近 N 条」可把最新 N 条入队。
- **消息模板**：默认
  ```
  ━━━━━━━━━━━━━━
  📡 {subscription_name}
  🕒 {published_at}

  # {title}

  {summary}

  ┈┈┈┈┈┈┈┈┈┈┈┈┈┈
  🌐 译文
  {translation}

  🔗 {link}
  ```
  层次靠符号与空行做：顶部粗线是**卡片边**，`📡` 订阅名与 `🕒` 时间构成**元信息块**，`# {title}` 的**标题**独立成段，之后是**原文正文**；`┈┈` 细线 + `🌐 译文` 标签把**译文区**和原文明显隔开，`🔗` 收尾给出原文链接。
  标题用 `#` 是为了 **QQ 侧**：订阅消息发到 Telegram 频道时是纯文本（`parse_mode=None`，Telegram Bot API 也没有标题语法），`#` 会原样显示；再由规则转发到 QQ 时走 QQ 原生 markdown，`#` 会被渲染成**一号标题**（`##` 为二号）。如果只在 Telegram 里看，可以把 `# ` 去掉。
  `{published_at}` 渲染为**本地时间**（如 `2026-10-13 08:30`，按容器 `TZ` 换算）。其他可用变量：`feed_title`。原文链接总会附加在末尾，不会重复。
- **空字段自动收行**：模板里某一行引用的占位符**全部为空**时，这一行连同 `🕒`、`🔗` 等图标一起省略；某个空行分隔的**整段**占位符全空时，整段省略（`┈┈` 细线与 `🌐 译文` 标签会跟着译文一起消失）。所以可以放心给可选字段加装饰前缀，源里没时间、没有译文时不会留下孤零零的图标或空行。行内只有部分占位符为空时（如 `{title} · {published_at}`）整行保留，空值位置留空。
- **翻译成中文**：默认勾选。源语言自动检测，**仅当原文不是中文**时才翻译，消息里**原文在上、译文在下**。译文**只覆盖模板里出现的原文字段**：模板没写 `{title}` 就不翻译标题，`{translation}` 里也不会出现译文标题；没写 `{summary}` 同理。模板只写 `{translation}`（原文字段全由译文承担）时，标题与摘要都会翻译。译文会缓存到条目上，重试不会重复请求。
- 保存前可点「预览」真实抓取该源并渲染消息效果，**不保存、不建基线、不发送**；预览同样会展示译文。

> 翻译使用 **OpenAI 兼容的 `/v1/chat/completions` 接口**，可对接 OpenAI、DeepSeek、通义千问或任意自建网关：只需填接口地址、API Key 与模型名。接口地址可填完整端点（如 `https://api.openai.com/v1/chat/completions`），也可填基础地址（如 `https://api.deepseek.com/v1`），会**自动补全 `/chat/completions` 路径**。**API Key 与接口地址均视为凭据，不要外泄**；中文源由本地判定直接跳过，不消耗额度；**翻译失败会自动回退原文**，不影响抓取与发布。可在 `.env` 关闭或调整：
>
> ```env
> RSS_TRANSLATE_ENABLED=true
> RSS_TRANSLATE_TARGET=ZH
> RSS_TRANSLATE_API_URL=https://api.openai.com/v1/chat/completions
> RSS_TRANSLATE_API_KEY=sk-...
> RSS_TRANSLATE_MODEL=gpt-4o-mini
> RSS_TRANSLATE_TIMEOUT_SECONDS=30
> RSS_TRANSLATE_MAX_CHARS=1200
> ```
>
> 容器内请求默认沿用环境变量 `HTTP_PROXY` / `HTTPS_PROXY`。

### 2. 让 Telegram 一侧能收到

1. 用 BotFather 创建的 Bot（默认复用 `TG_ADMIN_BOT_TOKEN`，也可单独配 `RSS_BOT_TOKEN`）添加为目标频道管理员，允许发消息。
2. 项目已登录的 Telegram 用户账号加入该频道（用于 TG → QQ 监听）。

### 3. 让 QQ 一侧能收到

在「订阅」卡片点「配置 QQ 转发规则」，会自动带上频道 ID、会话类型 `channel`、匹配模式“全部消息”、模板 `{text}`；只需在规则工坊选择 QQ 目标并保存。注意：

- 按**频道数字 ID** 匹配，不要限制“发送者必须是 Bot”（频道消息在现有代码里不标记为 Bot）。
- 若使用 `@username` 作为发布目标，需要先改成数字频道 ID 才能配置规则。

### 4. 运行行为

- 「立即检查」与「重试失败」只提交异步任务，页面提示“已排队”，之后刷新查看结果。
- 「立即试发」用于**端到端自检**：同步抓取当前源、取最新一条，走完整链路（翻译 → 渲染 → 发布到 TG 频道）并**立即返回结果**。它**绕过首次基线与去重**，关键词未命中时也会强制发送（页面会提示）；不写入条目历史，因此不会污染去重基线。发布失败（Bot 权限、频道无效、限流、网络）会在卡片上给出原因。注意：试发只保证 TG 频道收到，TG → QQ 转发仍取决于监听账号是否就绪、转发是否暂停。
- 抓取失败按指数退避；Telegram 发送失败自动重试，触达 `RSS_MAX_SEND_ATTEMPTS` 后标记失败，可一键重试。
- 全局转发暂停或 Telegram 监听未就绪时，RSS 仍会抓取和入队，但**不会发布**，面板会显式提示。
- 修改 Feed URL 会重建基线并跳过旧源未发送条目；修改 TG 目标只会影响之后待发送条目，已发布条目不会重发。
- 翻译在发送前完成并缓存到条目上：已缓存的译文不会重复请求；翻译失败会记录 warning 并按原文发布，不会因此重试或标记失败。
- 翻译请求**全局限流**：所有翻译调用（预览 / 发送 / 试发）共享 `RSS_TRANSLATE_MAX_CONCURRENCY`（默认 `1`，即串行），避免并发过高触发上游限流；遇 `429` / 超时 / `5xx` 会按 `RSS_TRANSLATE_MAX_ATTEMPTS` 与 `RSS_TRANSLATE_RETRY_BACKOFF_SECONDS` **指数退避重试**（并遵循上游 `Retry-After`），`4xx`（鉴权/参数/模型不存在）与响应格式错误不重试。

## Telegram 管理 Bot 命令

服务启动后，管理 Bot 会自动注入中文命令和命令说明。只有 `ADMIN_TELEGRAM_USER_IDS` 中的用户可以操作。

| 命令 | 说明 |
|---|---|
| `/start` | 显示帮助信息 |
| `/status` | 查看运行状态 |
| `/accounts` | 查看各 Telegram 账号连接状态 |
| `/dialogs [账号ID] [关键词]` | 查看或搜索指定账号的 Telegram 会话 |
| `/rules` | 查看转发规则 |
| `/qq_targets` | 查看已缓存的 QQ 目标 ID |
| `/add_rule <名称> [TG账号ID|*] <TG会话ID|*> <TG发送者ID|*> <QQ目标类型> <QQ目标ID> [关键词...]` | 新增规则；名称可含空格；重复规则会合并关键词 |
| `/del_rule <ID>` | 删除规则 |
| `/enable_rule <ID>` | 启用规则 |
| `/disable_rule <ID>` | 禁用规则 |
| `/logs [数量]` | 查看最近转发日志 |
| `/errors [数量]` | 查看最近错误日志 |
| `/pause` | 暂停全部转发 |
| `/resume` | 恢复全部转发 |

### 查看 Telegram 会话 ID

```text
/accounts
/dialogs
/dialogs main
/dialogs main 频道关键词
```

返回示例：

```text
channel | -1001234567890 | 某频道
group | -1009876543210 | 某群组
private | 123456789 | 某用户
```

### 查询 QQ 目标 ID

QQ 官方机器人使用的目标 ID 不是普通 QQ 群号或 QQ 号：

| QQ目标类型 | 需要的目标 ID |
|---|---|
| `group` | QQ 官方机器人事件中的 `group_openid` |
| `c2c` | QQ 官方机器人事件中的用户 `openid` |
| `channel` | QQ 频道 ID |
| `dms` | QQ 频道私信对应的 guild ID |

查询方式：

1. 确保服务已经启动，QQ 官方机器人 WebSocket 已连接。
2. 在目标 QQ 群、C2C、频道里给机器人发一条消息，或在群/频道中 @ 机器人。
3. 回到 Telegram 管理 Bot，执行：

```text
/qq_targets
```

返回示例：

```text
已缓存的 QQ 目标 ID：
格式：类型 | 目标ID | 最近消息ID | 说明
group | GROUP_OPENID_xxx | msg_xxx | QQ群
c2c | USER_OPENID_xxx | msg_xxx | QQ用户
channel | 123456789 | msg_xxx | 频道 123456789
```

复制 `目标ID` 一列，用在 `/add_rule` 的最后一个参数。

注意：缓存只保存在当前进程内。程序重启后需要目标 QQ 会话再次给机器人发消息，才能重新出现在 `/qq_targets` 中。

### 添加规则示例

转发某个频道全部消息到 QQ 群：

```text
/add_rule channel_news -1001234567890 * group QQ_GROUP_OPENID
```

指定只监听某个 Telegram 账号收到的消息：

```text
/add_rule channel_news main -1001234567890 * group QQ_GROUP_OPENID
```

转发某个频道中包含特定关键词的消息到 QQ 群：

```text
/add_rule channel_ai -1001234567890 * group QQ_GROUP_OPENID AI,Python,机器人
```

规则名称可以包含空格，程序会从后往前识别可选的 `TG账号ID`、`TG会话ID`、`TG发送者ID`、`QQ目标类型`、`QQ目标ID`：

```text
/add_rule LINUX DO Channel -1002035446470 * c2c 3BAABA13021BB09F7298EC3EBC7 gpt,注册机,公益
```

也可以用空格分隔多个关键词，并指定账号：

```text
/add_rule channel_ai main -1001234567890 * group QQ_GROUP_OPENID AI Python 机器人
```

关键词为可选项；不设置关键词时，匹配该规则的全部消息都会转发。设置一个或多个关键词后，只要消息正文包含任一关键词就会转发。

如果新增规则与已有规则的名称、TG 账号、TG 会话、TG 发送者、QQ 目标、模板等条件相同，仅关键词不同，程序不会创建重复规则，而是合并关键词并去重。若历史数据中已经存在多条这类重复规则，再次添加同条件规则时会保留第一条并删除多余重复项。

转发某个群里某个人的消息到 QQ 群：

```text
/add_rule one_user -1009876543210 123456789 group QQ_GROUP_OPENID
```

转发某个 Telegram Bot 的消息到 QQ 群：

```text
/add_rule one_bot * 777000111 group QQ_GROUP_OPENID
```

`QQ目标类型` 可选：

| 类型 | 含义 |
|---|---|
| `group` | QQ 群，目标 ID 为 `group_openid` |
| `c2c` | QQ 用户，目标 ID 为用户 `openid` |
| `channel` | QQ 频道，目标 ID 为频道 ID |
| `dms` | QQ 频道私信，目标 ID 为 guild ID |

## GitHub Actions 自动发布镜像

工作流文件：

```text
.github/workflows/docker-image.yml
```

当推送符合 `v*` 的标签时，会自动构建并推送镜像到 GitHub Container Registry：

```text
ghcr.io/<owner>/<repo>:<tag>
ghcr.io/<owner>/<repo>:latest
```

示例：

```bash
git tag v0.1.0
git push origin v0.1.0
```

GitHub Actions 使用仓库内置的 `GITHUB_TOKEN`，并已声明 `packages: write` 权限。

## 常见故障排查

### `telegram.error.NetworkError: httpx.ConnectError`（管理 Bot 轮询 / RSS 发布失败）

现象：日志里管理 Bot 报 `Exception happened while polling for updates`，或者 RSS 订阅一直发布失败，但 Telegram 用户监听（Telethon）正常。

原因：Telethon 走 **MTProto**，管理 Bot 与 RSS 发布走 **Bot API（api.telegram.org，HTTPS）**，两者是不同的通道。大陆 VPS 上常见“MTProto 可用、api.telegram.org 被阻断或间歇失败”。

排查与处理：

```bash
# 1. 容器内直连 Bot API 是否可用（超时/报错即为不通）
docker compose exec tgqq-forwarder python -c "import httpx;print(httpx.get('https://api.telegram.org',timeout=10).status_code)"

# 2. 看是否只有某个地址族不通（IPv6 无路由是常见原因）
docker compose exec tgqq-forwarder python -c "import socket;print(socket.getaddrinfo('api.telegram.org',443,proto=socket.IPPROTO_TCP))"
```

- **偶发一次**：PTB 自带指数退避重试，通常自愈，观察是否持续刷屏即可。
- **持续失败**：给 Bot API 配代理，在 `.env` 设置 `TELEGRAM_BOT_PROXY`。容器要访问宿主机代理时，先在 `docker-compose.yml` 增加
  `extra_hosts: ["host.docker.internal:host-gateway"]`，再写 `TELEGRAM_BOT_PROXY=http://host.docker.internal:7890`。
- 该设置对管理 Bot 与 RSS 发布同时生效；`socks5://` 需要镜像内 `httpx[socks]`（本项目已声明该依赖）。

## 本地开发

本项目不要求 Conda。本地开发可使用 Python 3.12 虚拟环境：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

运行：

```bash
python -m app.telegram_user.login --account all
python -m app.main
```

测试：

```bash
pytest
```

## 数据目录

运行数据默认保存在 `data/`：

```text
data/app.db                    # SQLite 数据库
data/logs/                     # 日志
data/media/{account_id}/...    # Telegram 媒体下载缓存（按账号分目录）
data/sessions/user.session              # 单账号兼容 session（account=default）
data/sessions/main/account.session      # 多账号推荐布局
data/sessions/news/account.session
```

建议定期备份 `data/`，尤其是 `app.db` 和全部账号 session 目录（含 `.session` / `.session-journal`）。
