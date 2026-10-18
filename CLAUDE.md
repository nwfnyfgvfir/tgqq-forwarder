# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

TGQQ Forwarder forwards selected messages between Telegram, QQ Official Bot sessions and personal WeChat. Sources are Telegram user accounts (Telethon), personal WeChat (Tencent iLink / `openclaw-weixin` channel: QR login + long polling) and messages received by the QQ Official Bot. Targets are QQ Official Bot sessions (botpy WebSocket/API), personal WeChat friends (`ilink/bot/sendmessage`) and Telegram chats (Telethon user account or Bot API).

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

# Personal WeChat QR login (writes data/weixin/<account>/credentials.json)
conda run -n tgqq-forwarder python -m app.weixin.login --account default
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

# First personal WeChat login in Docker (prints QR image path / link)
docker compose run --rm tgqq-forwarder python -m app.weixin.login

# Local image build instead of GHCR image
docker compose -f docker-compose.build.yml up -d --build
```

Package/build references:

- Runtime package metadata and dev dependencies are in `pyproject.toml`.
- Docker image entrypoint is `python -m app.main` in `Dockerfile`.
- `docker-compose.yml` expects `TGQQ_IMAGE` from `.env`; `docker-compose.build.yml` builds the local image.

## Configuration and runtime data

- `app/config.py` defines `Settings` with `pydantic-settings`; environment variables map directly to field names, e.g. `telegram_api_id` → `TELEGRAM_API_ID`.
- `Settings.validate_runtime_requirements()` requires Telegram API credentials only when `TELEGRAM_ENABLED` (default true), and QQ bot credentials only when `QQ_ENABLED` (default true). `WEIXIN_ENABLED` needs no static credentials.
- `get_settings()` creates runtime directories for data, logs, media, Telegram sessions, and WeChat state.
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

Telegram outbound settings (QQ / 微信 → TG):

- `TELEGRAM_SEND_BOT_TOKEN` is only needed for `target_type="bot"` targets; empty falls back to `RSS_BOT_TOKEN`, then `TG_ADMIN_BOT_TOKEN`. User-account targets reuse the existing Telethon sessions.
- `TELEGRAM_SEND_TIMEOUT_SECONDS` bounds one Telegram send (default 60s).

## High-level architecture

### Application runtime

`app/main.py` owns process wiring through `ApplicationRuntime`:

1. Load settings and configure logging.
2. Initialize SQLite/SQLModel database.
3. Start QQ sender (skipped when `QQ_ENABLED=false`), forwarding queue, media cleanup worker, Telegram account manager (N user listeners, skipped when `TELEGRAM_ENABLED=false`), WeChat service, and optional Telegram admin bot.
4. Stop components in reverse order on shutdown.

### Telegram ingestion path

`app/telegram_user/accounts.py` owns one `TelegramUserListener` per configured account. Each listener in `app/telegram_user/client.py` creates a Telethon `TelegramClient` and registers `events.NewMessage()`.

For each incoming Telegram message:

1. `TelegramMediaDownloader.download()` downloads real media when enabled, respects max size, namespaces files under `media/{account_id}/{chat_id}/`, and skips webpage preview media unless explicitly configured.
2. `event_parser.parse_event()` builds `TelegramForwardMessage` with chat/sender metadata, raw text/caption, extracted URL entities, URL buttons, media type, media paths, and `grouped_id`.
3. The listener stamps `account_id` / account user identity onto the message.
4. `TelegramAlbumBuffer` merges messages sharing `(account_id, chat_id, grouped_id)` so albums/multi-image posts forward once with ordered media paths.
5. Parsed messages enter the shared `ForwardQueue.enqueue()`.

Messages this process itself sent to Telegram are skipped before parsing: `app/telegram_outbound/registry.py::SentMessageRegistry` records `(chat_id, message_id)` after each send, and `TelegramUserListener` checks it (otherwise a TG → TG rule would re-ingest its own output and loop).

URL handling is centralized around `TelegramLink` in `app/rules/models.py`:

- `visible_url`: URL already visible in Telegram text.
- `text_url`: Telegram hidden text hyperlink.
- `button_url`: Telegram URL button.

`MessageFormatter` later decides which hidden/button links must be appended without duplicating already visible URLs.

### Rules, formatting, and queueing

Rules and logs are SQLModel tables in `app/storage/models.py`, accessed through repositories in `app/storage/repositories.py`.

`app/rules/service.py` combines repositories, `RuleMatcher`, and `MessageFormatter`:

- `RuleMatcher` filters by enabled state, source platform, Telegram/WeChat account id, chat ID/type, sender ID/bot flag, media type, include regex, and exclude regex.
- Keyword rules are encoded/decoded in `app/rules/keywords.py` as regex with metadata.
- `MessageFormatter` renders rule templates, appends missing hidden/button links, and avoids duplicate visible URL notes.

`app/worker/forward_queue.py` consumes parsed messages, skips work when forwarding is paused, finds matching rules, formats text **once per rule**, then delivers to every enabled target of that rule through `app/channels/router.py` (`DeliveryRouter`). Each target is sent and logged independently (`ForwardLog` per target, one failure never blocks the others). A target that points back at the source Telegram chat is logged as `skipped` instead of delivered.

Rule platform semantics:

- `ForwardRule.source_platform` is `telegram` / `weixin` / `qq` / `null` (null = any platform, the legacy behaviour).
- Targets live in the `forward_target` table (`ForwardTarget`): one row per `(rule, platform, target_type, target_id)` with `enabled` and `position`. A rule can fan out to any mix of QQ groups/channels, WeChat contacts and Telegram chats.
- `ForwardRule.target_platform` + `qq_target_*` remain as **compat columns for the first target** so old code, old API clients and the Telegram `/add_rule` command keep working. A rule with no `forward_target` rows is read as a single legacy target (`app/storage/models.py::legacy_target`).
- For WeChat targets `target_type` is fixed to `friend` and `target_id` holds the WeChat `user_id`/wxid; guild/channel fields are dropped.
- For Telegram targets `target_type` is `chat` (send with the Telethon user account) or `bot` (Bot API) and `target_id` is a numeric chat id, `@username`, or `me` (Saved Messages, user account only). `ForwardTarget.account_id` (added via `_ensure_column`) picks which Telethon account sends; it is cleared for `bot` targets.
- `source_chat_id` / `source_sender_id` / log ID columns use `FlexibleId` (TEXT affinity, numeric values still read back as `int`) so Telegram integer IDs and WeChat/QQ string IDs share the same columns.

### QQ integration

`app/qq_official/client.py` subclasses `botpy.Client`. Incoming QQ events do two things: they cache QQ target IDs / latest message IDs needed by QQ Official API sending contexts, and (when an `on_message` callback is supplied) they build a `TelegramForwardMessage` with `source_platform="qq"` and enqueue it, so QQ → Telegram / 微信 rules work. `chat_id` is `group_openid` (group), `channel_id` (channel), `guild_id` (DMS) or `user_openid` (C2C); `<@!id>` placeholders are stripped and attachments become `[图片] https://...` text notes (files are not downloaded). Duplicate event ids are ignored because QQ may fire both the @-mention and the plain group event for one message.

`app/qq_official/sender.py` sends outbound messages:

- Text-only messages use the target-specific QQ API.
- Multiple media files are sent sequentially; the first media carries full text and later media use continuation text.
- Group/C2C media uses QQ media upload; channel/DMS image sending uses `file_image`; unsupported media can degrade to a text note.
- QQ markdown is attempted when enabled and falls back to plain text if QQ rejects native markdown.

### WeChat integration

`app/weixin/` implements the personal-WeChat channel used by AstrBot's `weixin_oc` adapter:

- `client.py`: `WeixinIlinkClient` over httpx. Endpoints: `ilink/bot/get_bot_qrcode`, `ilink/bot/get_qrcode_status`, `ilink/bot/getupdates` (long poll, cursor = `get_updates_buf`), `ilink/bot/sendmessage`, `ilink/bot/getuploadurl`. Required headers: `AuthorizationType: ilink_bot_token`, random `X-WECHAT-UIN`, `Authorization: Bearer <bot_token>`; QR status polling adds `iLink-App-ClientVersion: 1`. `errcode/ret == -14` means the login session expired. Both `getupdates` and `get_qrcode_status` are long polls (server returns after ~30s); a client-side timeout is normal and returns `{}` instead of raising.
- **QR payload gotcha**: `get_bot_qrcode` returns `{"qrcode": "<token>", "qrcode_img_content": "https://liteapp.weixin.qq.com/q/...?qrcode=<token>&bot_type=3", "ret": 0}` — `qrcode_img_content` is a **scan URL, not an image**. `app/weixin/qrcode.py` renders it locally with `segno` (PNG for the Mini App, ASCII for the CLI) and only accepts base64 payloads that pass an image-magic check.
- `crypto.py`: media uses AES-128-ECB + PKCS#7; `aes_key` may be base64(raw 16 bytes) or base64(32 hex chars).
- `media.py`: inbound media is downloaded from the CDN and decrypted; outbound media is encrypted, uploaded, and referenced by the `x-encrypted-param` response header.
- `credentials.py`: `data/weixin/<account>/credentials.json` (0600) stores `bot_token`, the long-poll cursor, per-contact `context_token`, and known contacts.
- `service.py`: `WeixinService` owns the login state machine, the long-poll loop, reconnect/backoff, inbound parsing into `TelegramForwardMessage` (`source_platform="weixin"`, `chat_id` = wxid), and outbound sending.
- **Protocol constraint**: `sendmessage` requires the recipient's `context_token`, which only arrives with an inbound message from that contact. Forwarding to WeChat therefore only works for contacts who have messaged the account; tokens are cached and persisted.
- `app/web/app.py` exposes `/api/weixin/status`, `/api/weixin/contacts`, `POST/GET /api/weixin/login`, `POST /api/weixin/logout`; the Mini App system page renders the QR code from `login.image_data_url`.
- RSS subscriptions can target WeChat directly (`app/rss/delivery.py`); `RssWorker` takes `weixin_service` and the publish gate (`ApplicationRuntime._can_publish_rss`) is satisfied by either a connected Telegram listener or a connected WeChat channel.

### Telegram outbound (QQ / 微信 → TG)

`app/telegram_outbound/` owns delivery into Telegram:

- `registry.py`: `SentMessageRegistry` remembers `(chat_id, message_id)` of messages this process sent (TTL + LRU bound). `TelegramUserListener` consults it to drop self-sent messages.
- `delivery.py`: `TelegramDelivery` dispatches on `ForwardTarget.target_type` — `chat` → `TelegramUserSender` (Telethon `send_message` / `send_file`, `me` maps to Saved Messages, `account_id` selects the listener), `bot` → `TelegramBotSender` (python-telegram-bot `Bot`, token from `TELEGRAM_SEND_BOT_TOKEN` → `RSS_BOT_TOKEN` → `TG_ADMIN_BOT_TOKEN`, can only reach chats the bot already joined).
- `DeliveryRouter` takes it as an optional 4th dependency; `ForwardQueue` also skips any Telegram target whose chat equals the source chat (`_is_echo_target`), logging `skipped` instead of looping.
- `ApplicationRuntime` creates one registry and shares it between `TelegramAccountManager` and `TelegramDelivery`; `TelegramDelivery.stop()` shuts the Bot session down.

### Telegram admin bot

`app/telegram_admin/bot.py` starts only when both `TG_ADMIN_BOT_TOKEN` and `ADMIN_TELEGRAM_USER_IDS` are set.

`app/telegram_admin/commands.py` implements admin commands for status, dialog lookup, rule management, QQ target lookup, logs/errors, and pause/resume. `/add_rule` accepts rule names with spaces and can merge duplicate keyword rules through `ForwardRuleService.create_or_merge_rule()`.

### RSS subscriptions

`app/rss/` owns RSS/RSSHub subscriptions end to end: `schemas.py` validates `SubscriptionInput` (feed URL, target channel, interval, first-run policy, keywords, template), `fetcher.py` fetches/parses feeds with SSRF guards (public hosts only unless listed in `RSS_ALLOWED_PRIVATE_HOSTS`, per-hop DNS pinning, ETag/Last-Modified, size caps, HTTP `identity` only), `service.py` does CRUD plus entry bookkeeping, `worker.py` runs the in-process scheduler with per-subscription job locks, persistent dedupe (`rss_entry` unique on `subscription_id + entry_key`), retry/backoff, and `publisher.py` publishes through the Telegram Bot API (`RSS_BOT_TOKEN`, falling back to the admin bot, plain text, link preview disabled).

RSS target semantics: subscriptions are **Telegram-only**. `RssSubscription` has just `tg_chat_id` (negative channel id or `@username`, validated in `SubscriptionInput.chat_valid`) and publishing never goes through `DeliveryRouter`, so WeChat/QQ delivery is only reachable as a two-hop setup: publish into a Telegram channel, then add a normal forwarding rule whose source is that channel and whose target is WeChat/QQ (the subscription page's "转规则" button pre-fills such a rule from `tg_chat_id`).

`ApplicationRuntime` starts one `RssWorker` (`app/main.py`) and injects both `RssService` and `RssWorker` into the Mini App; `app/web/rss_routes.py` exposes `/api/rss/*` under the same admin auth. The worker keeps fetching while forwarding is paused or Telegram is disconnected, but only publishes when `can_publish()` is true. `POST /api/rss/subscriptions/{id}/test-send` (`RssWorker.test_send_latest`) is a synchronous end-to-end self-check: it fetches now, force-publishes the newest entry (bypassing the first-run baseline, dedupe and keyword filters, without writing entry history), and returns the outcome directly; it does not depend on `can_publish()`.

Known limits: single-instance in-process scheduling, no exactly-once guarantee (a lost send response may duplicate on retry), media/photo entries publish as text links only, and TG → QQ still depends on the existing `ForwardQueue` (in-memory dedupe, drop when full).

## Tests

Existing tests are organized by subsystem:

- `tests/test_settings.py`: settings parsing.
- `tests/test_rules.py`: rule matching, keyword helpers, message formatting/link dedupe.
- `tests/test_event_parser.py`: Telegram entity/button/link-preview parsing.
- `tests/test_media_downloader.py`: media download and link preview skip behavior.
- `tests/test_admin_commands.py`: admin command argument parsing.
- `tests/test_db.py`: database/repository/service behavior.
- `tests/test_weixin_crypto.py`: PKCS#7 + AES-ECB roundtrip and `aes_key` parsing.
- `tests/test_weixin_client.py`: iLink request headers/payloads, envelope unwrapping, errcode handling, media upload/download (httpx `MockTransport`).
- `tests/test_weixin_service.py`: inbound parsing, dedupe, context_token caching, send splitting, QR login flow, session expiry.
- `tests/test_weixin_api.py`: Mini App WeChat endpoints and platform-aware rule creation.
- `tests/test_delivery_router.py`: target-platform dispatch, disabled channel, timeout wrapping.
- `tests/test_rule_platforms.py`: platform matching, string IDs, schema normalisation, DB roundtrip.
- `tests/test_telegram_delivery.py`: sent-message registry, user-account and Bot senders, router dispatch, echo-target skip in the queue.
- `tests/test_qq_inbound.py`: QQ inbound parsing into `TelegramForwardMessage` (group / channel / C2C, mention stripping, attachments, dedupe).
- `tests/test_rss_weixin.py`: RSS delivery dispatch, WeChat availability/context-token gating, transient vs permanent failures, worker integration and test-send.
