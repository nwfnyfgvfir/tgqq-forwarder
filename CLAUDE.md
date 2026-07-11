# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

TGQQ Forwarder forwards selected Telegram user-account messages to QQ Official Bot target sessions. Telegram is handled through a logged-in user session with Telethon; QQ delivery uses QQ Official Bot WebSocket/API via `botpy`.

User-facing documentation in this repository is written in Chinese.

## Common commands

This project is deployed to VPS with Docker Compose. For Claude Code/local verification, use the Conda environment `tgqq-forwarder`.

```bash
# Create local development environment if missing
conda create -n tgqq-forwarder python=3.12 -y
conda run -n tgqq-forwarder python -m pip install -e '.[dev]'

# Run all tests
conda run -n tgqq-forwarder python -m pytest

# Run a single test file or test case
conda run -n tgqq-forwarder python -m pytest tests/test_rules.py
conda run -n tgqq-forwarder python -m pytest tests/test_rules.py::test_formatter_uses_template_values

# Lint
conda run -n tgqq-forwarder python -m ruff check .

# Local Telegram login and app run
# Single-account (legacy TELEGRAM_SESSION_PATH) or multi-account via TELEGRAM_ACCOUNTS_JSON
conda run -n tgqq-forwarder python -m app.telegram_user.login --account all
conda run -n tgqq-forwarder python -m app.telegram_user.login --account main
conda run -n tgqq-forwarder python -m app.main
```

Docker deployment commands:

```bash
cp .env.example .env
# edit .env, especially TGQQ_IMAGE, Telegram credentials, QQ credentials, admin bot settings

docker compose pull
docker compose up -d
docker compose logs -f
docker compose down

# First Telegram user login in Docker (all accounts, or --account <id>)
docker compose run --rm tgqq-forwarder python -m app.telegram_user.login --account all
docker compose run --rm tgqq-forwarder python -m app.telegram_user.login --account main

# Local image build instead of GHCR image
docker compose -f docker-compose.build.yml up -d --build
```

Package/build references:

- Runtime package metadata and dev dependencies are in `pyproject.toml`.
- Docker image entrypoint is `python -m app.main` in `Dockerfile`.
- `docker-compose.yml` expects `TGQQ_IMAGE` from `.env`; `docker-compose.build.yml` builds the local image.

## Configuration and runtime data

- `app/config.py` defines `Settings` with `pydantic-settings`; environment variables map directly to field names, e.g. `telegram_api_id` → `TELEGRAM_API_ID`.
- `Settings.validate_runtime_requirements()` requires Telegram API credentials and QQ bot credentials before startup.
- `get_settings()` creates runtime directories for data, logs, media, and Telegram sessions.
- Runtime data defaults under `data/`: SQLite DB, logs, Telegram media cache, and Telegram user session.
- `TELEGRAM_SESSION_PATH` is the legacy single-account session path (maps to account id `default`) and should be treated as account credentials.
- Multi-account config uses `TELEGRAM_ACCOUNTS_JSON` (`[{id, session_path?, phone?, enabled?, api_id?, api_hash?}, ...]`).
- Default multi-account layout is `TELEGRAM_SESSIONS_DIR/<account_id>/account.session` (and Telethon may also create `account.session-journal` beside it). Explicit `session_path` still wins for legacy flat files.
- Messages carry `account_id`; rules may set `source_account_id` (null = any account). Optional `TELEGRAM_DEDUPE_CROSS_ACCOUNT` dedupes the same `(chat_id, message_id)` across accounts.
- `TelegramAccountManager` starts one listener per enabled account and shares one `ForwardQueue`.
- QQ group targets use QQ Official Bot `group_openid`, not normal QQ group numbers.

Important media/link settings:

- `TELEGRAM_DOWNLOAD_MEDIA` is the master switch for Telegram media downloads.
- `TELEGRAM_FORWARD_LINK_PREVIEW_MEDIA` defaults to false, so Telegram webpage preview media is skipped while real attached media still forwards.
- `TELEGRAM_ALBUM_BUFFER_SECONDS` controls Telegram grouped album aggregation.
- Media cache cleanup is controlled by `MEDIA_CLEANUP_INTERVAL_SECONDS` and `MEDIA_RETENTION_SECONDS`.

## High-level architecture

### Application runtime

`app/main.py` owns process wiring through `ApplicationRuntime`:

1. Load settings and configure logging.
2. Initialize SQLite/SQLModel database.
3. Start QQ sender, forwarding queue, media cleanup worker, Telegram account manager (N user listeners), and optional Telegram admin bot.
4. Stop components in reverse order on shutdown.

### Telegram ingestion path

`app/telegram_user/accounts.py` owns one `TelegramUserListener` per configured account. Each listener in `app/telegram_user/client.py` creates a Telethon `TelegramClient` and registers `events.NewMessage()`.

For each incoming Telegram message:

1. `TelegramMediaDownloader.download()` downloads real media when enabled, respects max size, namespaces files under `media/{account_id}/{chat_id}/`, and skips webpage preview media unless explicitly configured.
2. `event_parser.parse_event()` builds `TelegramForwardMessage` with chat/sender metadata, raw text/caption, extracted URL entities, URL buttons, media type, media paths, and `grouped_id`.
3. The listener stamps `account_id` / account user identity onto the message.
4. `TelegramAlbumBuffer` merges messages sharing `(account_id, chat_id, grouped_id)` so albums/multi-image posts forward once with ordered media paths.
5. Parsed messages enter the shared `ForwardQueue.enqueue()`.

URL handling is centralized around `TelegramLink` in `app/rules/models.py`:

- `visible_url`: URL already visible in Telegram text.
- `text_url`: Telegram hidden text hyperlink.
- `button_url`: Telegram URL button.

`MessageFormatter` later decides which hidden/button links must be appended without duplicating already visible URLs.

### Rules, formatting, and queueing

Rules and logs are SQLModel tables in `app/storage/models.py`, accessed through repositories in `app/storage/repositories.py`.

`app/rules/service.py` combines repositories, `RuleMatcher`, and `MessageFormatter`:

- `RuleMatcher` filters by enabled state, Telegram account id, chat ID/type, sender ID/bot flag, media type, include regex, and exclude regex.
- Keyword rules are encoded/decoded in `app/rules/keywords.py` as regex with metadata.
- `MessageFormatter` renders rule templates, appends missing hidden/button links, and avoids duplicate visible URL notes.

`app/worker/forward_queue.py` consumes parsed Telegram messages, skips work when forwarding is paused, finds matching rules, formats text, builds `QQOutboundMessage`, sends via QQ, and records `ForwardLog` success/failure entries.

### QQ integration

`app/qq_official/client.py` subclasses `botpy.Client`. Incoming QQ events are not business commands; they cache QQ target IDs and latest message IDs needed by QQ Official API sending contexts.

`app/qq_official/sender.py` sends outbound messages:

- Text-only messages use the target-specific QQ API.
- Multiple media files are sent sequentially; the first media carries full text and later media use continuation text.
- Group/C2C media uses QQ media upload; channel/DMS image sending uses `file_image`; unsupported media can degrade to a text note.
- QQ markdown is attempted when enabled and falls back to plain text if QQ rejects native markdown.

### Telegram admin bot

`app/telegram_admin/bot.py` starts only when both `TG_ADMIN_BOT_TOKEN` and `ADMIN_TELEGRAM_USER_IDS` are set.

`app/telegram_admin/commands.py` implements admin commands for status, dialog lookup, rule management, QQ target lookup, logs/errors, and pause/resume. `/add_rule` accepts rule names with spaces and can merge duplicate keyword rules through `ForwardRuleService.create_or_merge_rule()`.

## Tests

Existing tests are organized by subsystem:

- `tests/test_settings.py`: settings parsing.
- `tests/test_rules.py`: rule matching, keyword helpers, message formatting/link dedupe.
- `tests/test_event_parser.py`: Telegram entity/button/link-preview parsing.
- `tests/test_media_downloader.py`: media download and link preview skip behavior.
- `tests/test_admin_commands.py`: admin command argument parsing.
- `tests/test_db.py`: database/repository/service behavior.
