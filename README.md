# TGQQ Forwarder

TGQQ Forwarder 用于把指定 Telegram 消息自动转发到 QQ 官方机器人目标会话。Telegram 侧使用用户账号登录，QQ 侧使用 QQ 官方机器人 WebSocket 方式。

## 功能

- 使用 Telethon 登录 Telegram 用户账号。
- 监听该账号可见的频道、群组、私聊、Bot 消息。
- 按规则筛选要转发的消息。
- 支持按 Telegram 会话、发送者、Bot 标记、关键词、文本正则、媒体类型匹配。
- 自动提取 Telegram 普通 URL、文字超链接和 URL 按钮；可见 URL 不重复追加，隐藏链接会以“文字: URL”形式显示。
- 优先使用 QQ 原生 markdown 发送，若 QQ 官方接口拒绝 markdown，会自动降级为纯文本。
- 定时清理 `data/media/` 下的 Telegram 媒体下载缓存。
- 自动聚合 Telegram 相册/多图图文消息，避免同一组图文被拆成多次转发。
- 使用 QQ 官方机器人 WebSocket 模式连接 QQ。
- 支持转发到 QQ 群、C2C、频道、频道私信场景。
- 通过 Telegram 管理 Bot 查看状态、管理规则、查询日志。
- 提供 Telegram Mini App 管理台，可用可视化规则工坊管理关键词、目标、模板、日志和暂停状态。
- 使用 Docker Compose 部署到 VPS。
- 推送 `v*` 标签时，GitHub Actions 自动构建 Docker 镜像并发布到 GitHub Container Registry。

## 重要限制

1. `TELEGRAM_SESSION_PATH` 保存 Telegram 用户账号登录态，等同账号凭据，必须妥善保管。
2. QQ 群目标使用的是 QQ 官方机器人 API 中的 `group_openid`，不是普通 QQ 群号。
3. QQ 官方机器人不同场景的主动发送能力不同。部分群或频道发送可能需要先由 QQ 目标会话给机器人发送一条消息，以便程序缓存最近的 QQ `msg_id`。
4. 媒体转发已支持常见图片、视频、语音、文件场景，但某些 QQ 目标类型不支持直接上传时会退化为文本说明。
5. QQ 原生 markdown 能否显示取决于 QQ 官方机器人权限和消息场景；程序会优先尝试 markdown，失败后自动降级为纯文本。
6. Telegram 匿名管理员消息、频道签名消息、隐私受限的转发消息，可能无法拿到真实发送者 ID。

## 文件结构

```text
app/
  main.py                    # 主入口
  telegram_user/             # Telegram 用户账号登录与监听
  telegram_admin/            # Telegram 管理 Bot
  qq_official/               # QQ 官方机器人 WebSocket 发送模块
  rules/                     # 转发规则匹配与格式化
  storage/                   # SQLite 数据库与 Repository
  worker/                    # 转发队列
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

首次运行前，需要授权 Telegram 用户账号：

```bash
docker compose run --rm tgqq-forwarder python -m app.telegram_user.login
```

按提示输入验证码。如开启两步验证，还需要输入 Telegram 二步验证密码。

登录成功后，session 文件会保存到：

```text
data/sessions/user.session
```

不要泄露该文件。

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
TELEGRAM_DOWNLOAD_MEDIA=true
TELEGRAM_FORWARD_LINK_PREVIEW_MEDIA=false
TELEGRAM_MAX_MEDIA_MB=20
TELEGRAM_ALBUM_BUFFER_SECONDS=2.0
MEDIA_CLEANUP_INTERVAL_SECONDS=3600
MEDIA_RETENTION_SECONDS=86400

TG_ADMIN_BOT_TOKEN=123456:请替换为你的管理BotToken
ADMIN_TELEGRAM_USER_IDS=11111111,22222222
TG_ADMIN_BOT_CONNECT_TIMEOUT=15
TG_ADMIN_BOT_REQUEST_TIMEOUT=30
TG_ADMIN_BOT_POOL_TIMEOUT=15
TG_ADMIN_BOT_POLL_TIMEOUT=30
TG_ADMIN_BOT_POLL_READ_TIMEOUT=45

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

QQ 官方机器人通常不能在一条消息里发送多张图片。程序聚合后会按顺序发送多张图：第一张带完整文字和链接，后续图片带“继续发送媒体 x/y”的说明，保证不会重复发送整段文字。

### Telegram 超链接转发

程序会提取三类 Telegram 链接：

- 普通 URL，例如 `https://example.com`。这类 URL 已在正文可见时不会重复追加。
- 文字超链接，例如 Telegram 中显示为“点击查看”，实际链接是 `https://example.com`。
- URL 按钮，例如消息下方的“查看回复”“打开网页”等按钮。

默认模板会把正文中不可见的链接追加为：

```text
链接：
- 点击查看: https://example.com
- 查看回复: https://example.com/reply
```

这样即使 QQ 场景无法渲染 markdown，隐藏链接和按钮链接仍会以可见 URL 形式显示。

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

程序会优先使用 QQ 原生 markdown 消息。若 QQ 官方接口返回“不允许发送原生 markdown”之类的错误，会自动使用纯文本重发。是否能真正显示 markdown 取决于 QQ 官方机器人权限和目标场景。

### media 目录清理

默认配置：

```env
MEDIA_CLEANUP_INTERVAL_SECONDS=3600
MEDIA_RETENTION_SECONDS=86400
```

含义：每 3600 秒检查一次 `data/media/`，删除修改时间超过 86400 秒的媒体文件。设置任一值为 `0` 可禁用清理任务。

## Telegram 管理 Bot 命令

服务启动后，管理 Bot 会自动注入中文命令和命令说明。只有 `ADMIN_TELEGRAM_USER_IDS` 中的用户可以操作。

| 命令 | 说明 |
|---|---|
| `/start` | 显示帮助信息 |
| `/status` | 查看运行状态 |
| `/dialogs [关键词]` | 查看或搜索 Telegram 会话 |
| `/rules` | 查看转发规则 |
| `/qq_targets` | 查看已缓存的 QQ 目标 ID |
| `/add_rule <名称> <TG会话ID|*> <TG发送者ID|*> <QQ目标类型> <QQ目标ID> [关键词...]` | 新增规则；名称可含空格；重复规则会合并关键词 |
| `/del_rule <ID>` | 删除规则 |
| `/enable_rule <ID>` | 启用规则 |
| `/disable_rule <ID>` | 禁用规则 |
| `/logs [数量]` | 查看最近转发日志 |
| `/errors [数量]` | 查看最近错误日志 |
| `/pause` | 暂停全部转发 |
| `/resume` | 恢复全部转发 |

### 查看 Telegram 会话 ID

```text
/dialogs
/dialogs 频道关键词
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

转发某个频道中包含特定关键词的消息到 QQ 群：

```text
/add_rule channel_ai -1001234567890 * group QQ_GROUP_OPENID AI,Python,机器人
```

规则名称可以包含空格，程序会从后往前识别 `TG会话ID`、`TG发送者ID`、`QQ目标类型`、`QQ目标ID`：

```text
/add_rule LINUX DO Channel -1002035446470 * c2c 3BAABA13021BB09F7298EC3EBC7 gpt,注册机,公益
```

也可以用空格分隔多个关键词：

```text
/add_rule channel_ai -1001234567890 * group QQ_GROUP_OPENID AI Python 机器人
```

关键词为可选项；不设置关键词时，匹配该规则的全部消息都会转发。设置一个或多个关键词后，只要消息正文包含任一关键词就会转发。

如果新增规则与已有规则的名称、TG 会话、TG 发送者、QQ 目标、模板等条件相同，仅关键词不同，程序不会创建重复规则，而是合并关键词并去重。若历史数据中已经存在多条这类重复规则，再次添加同条件规则时会保留第一条并删除多余重复项。

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
python -m app.telegram_user.login
python -m app.main
```

测试：

```bash
pytest
```

## 数据目录

运行数据默认保存在 `data/`：

```text
data/app.db                 # SQLite 数据库
data/logs/                  # 日志
data/media/                 # Telegram 媒体下载缓存
data/sessions/user.session  # Telegram 用户账号登录态
```

建议定期备份 `data/`，尤其是 `app.db` 和 `sessions/user.session`。
