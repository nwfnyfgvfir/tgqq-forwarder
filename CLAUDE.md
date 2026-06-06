# 项目说明

## 部署方式

本项目面向 VPS 使用 Docker Compose 部署，不使用 Conda。

## 本地开发

如需在本机开发，建议使用 Python 3.12 的虚拟环境：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

常用命令：

```bash
python -m app.telegram_user.login
python -m app.main
pytest
```

## Docker 运行

```bash
cp .env.example .env
# 在 .env 中设置 TGQQ_IMAGE=ghcr.io/<owner>/<repo>:latest
docker compose pull
docker compose up -d
```

如需本地构建镜像，使用：

```bash
docker compose -f docker-compose.build.yml up -d --build
```

首次登录 Telegram 用户账号：

```bash
docker compose run --rm tgqq-forwarder python -m app.telegram_user.login
```

## 文档语言

面向用户的说明文档使用中文。
