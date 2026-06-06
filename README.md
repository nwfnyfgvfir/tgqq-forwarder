# TGQQ Forwarder

TGQQ Forwarder 用于把指定 Telegram 消息自动转发到 QQ 官方机器人目标会话。Telegram 侧使用用户账号登录，QQ 侧使用 QQ 官方机器人 WebSocket 方式。

## 功能

- 使用 Telethon 登录 Telegram 用户账号。
- 监听该账号可见的频道、群组、私聊、Bot 消息。
- 按规则筛选要转发的消息。
- 支持按 Telegram 会话、发送者、Bot 标记、文本正则、媒体类型匹配。
- 使用 QQ 官方机器人 WebSocket 模式连接 QQ。
- 支持转发到 QQ 群、C2C、频道、频道私信场景。
- 通过 Telegram 管理 Bot 查看状态、管理规则、查询日志。
- 使用 Docker Compose 部署到 VPS。
- 推送 `v*` 标签时，GitHub Actions 自动构建 Docker 镜像并发布到 GitHub Container Registry。

## 重要限制

1. `TELEGRAM_SESSION_PATH` 保存 Telegram 用户账号登录态，等同账号凭据，必须妥善保管。
2. QQ 群目标使用的是 QQ 官方机器人 API 中的 `group_openid`，不是普通 QQ 群号。
3. QQ 官方机器人不同场景的主动发送能力不同。部分群或频道发送可能需要先由 QQ 目标会话给机器人发送一条消息，以便程序缓存最近的 QQ `msg_id`。
4. 媒体转发已支持常见图片、视频、语音、文件场景，但某些 QQ 目标类型不支持直接上传时会退化为文本说明。
5. Telegram 匿名管理员消息、频道签名消息、隐私受限的转发消息，可能无法拿到真实发送者 ID。

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
TELEGRAM_MAX_MEDIA_MB=20

TG_ADMIN_BOT_TOKEN=123456:请替换为你的管理BotToken
ADMIN_TELEGRAM_USER_IDS=11111111,22222222

QQ_BOT_APPID=请替换为你的QQ机器人AppID
QQ_BOT_SECRET=请替换为你的QQ机器人Secret
QQ_ENABLE_GROUP_C2C=true
QQ_ENABLE_GUILD_DIRECT_MESSAGE=false
QQ_ALLOW_SEND_WITHOUT_CACHED_MSG_ID=true
QQ_USE_MARKDOWN=false

FORWARD_QUEUE_SIZE=1000
```

## Telegram 管理 Bot 命令

服务启动后，管理 Bot 会自动注入中文命令和命令说明。只有 `ADMIN_TELEGRAM_USER_IDS` 中的用户可以操作。

| 命令 | 说明 |
|---|---|
| `/start` | 显示帮助信息 |
| `/status` | 查看运行状态 |
| `/dialogs [关键词]` | 查看或搜索 Telegram 会话 |
| `/rules` | 查看转发规则 |
| `/add_rule <名称> <TG会话ID|*> <TG发送者ID|*> <QQ目标类型> <QQ目标ID>` | 新增规则 |
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

### 添加规则示例

转发某个频道全部消息到 QQ 群：

```text
/add_rule channel_news -1001234567890 * group QQ_GROUP_OPENID
```

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
