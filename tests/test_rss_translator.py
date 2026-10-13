from __future__ import annotations

import base64
import json
import time
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from telegram.error import NetworkError

from app.config import Settings
from app.rss.fetcher import FeedEntry, FeedResult
from app.rss.formatting import build_translation_block, render_entry
from app.rss.publisher import PublishResult
from app.rss.schemas import DEFAULT_MESSAGE_TEMPLATE, TEMPLATE_FIELDS, SubscriptionInput
from app.rss.service import RssService
from app.rss.translator import AUTH_URL, BingTranslator, TranslationOutcome
from app.rss.worker import RssWorker
from app.storage.db import Database
from app.storage.models import RssEntry, RssSubscription, utc_now

# --------------------------------------------------------------------------- #
# 假端点与工具
# --------------------------------------------------------------------------- #


def make_jwt(*, exp_offset: int = 3600) -> str:
    def segment(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = segment({"alg": "HS256", "typ": "JWT"})
    body = segment({"exp": int(time.time()) + exp_offset})
    return f"{header}.{body}.signature"


def translated(language: str, text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json=[
            {
                "detectedLanguage": {"language": language, "score": 1.0},
                "translations": [{"text": text, "to": "zh-Hans"}],
            }
        ],
    )


def detected_only(language: str) -> httpx.Response:
    return httpx.Response(
        200, json=[{"detectedLanguage": {"language": language, "score": 1.0}}]
    )


class TranslationEndpoint:
    """记录请求的假端点：auth 返回 JWT，translate 按脚本顺序返回。"""

    def __init__(self, *, jwt: str | None = None, translate_responses=None) -> None:
        self.jwt = jwt if jwt is not None else make_jwt()
        self.translate_responses = list(translate_responses or [])
        self.auth_calls = 0
        self.translate_calls = 0
        self.bodies: list[list[dict]] = []
        self.urls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        if str(request.url).startswith(AUTH_URL):
            self.auth_calls += 1
            return httpx.Response(200, text=self.jwt)
        self.translate_calls += 1
        self.bodies.append(json.loads(request.content))
        if self.translate_responses:
            response = self.translate_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return httpx.Response(200, json=[])

    def translator(self, **settings_kwargs) -> BingTranslator:
        return BingTranslator(
            Settings(_env_file=None, **settings_kwargs),
            transport=httpx.MockTransport(self.handler),
        )


# --------------------------------------------------------------------------- #
# BingTranslator 单元测试
# --------------------------------------------------------------------------- #


async def test_token_fetched_once_and_cached_across_calls() -> None:
    endpoint = TranslationEndpoint(
        translate_responses=[translated("en", "你好"), translated("en", "世界")]
    )
    translator = endpoint.translator()
    try:
        first = await translator.translate(["hello"])
        second = await translator.translate(["world"])
    finally:
        await translator.close()
    assert endpoint.auth_calls == 1
    assert endpoint.translate_calls == 2
    assert first[0].text == "你好" and second[0].text == "世界"
    assert first[0].language == "en" and first[0].skipped is False and first[0].error is None


async def test_near_expiry_token_is_refreshed() -> None:
    endpoint = TranslationEndpoint(
        jwt=make_jwt(exp_offset=10),
        translate_responses=[translated("en", "a"), translated("en", "b")],
    )
    translator = endpoint.translator()
    try:
        await translator.translate(["one"])
        await translator.translate(["two"])
    finally:
        await translator.close()
    assert endpoint.auth_calls == 2


async def test_401_refreshes_token_once_and_retries_once() -> None:
    endpoint = TranslationEndpoint(
        translate_responses=[httpx.Response(401, text="unauthorized"), translated("en", "你好")]
    )
    translator = endpoint.translator()
    try:
        outcomes = await translator.translate(["hello"])
    finally:
        await translator.close()
    assert endpoint.auth_calls == 2  # 首次取 token + 刷新一次
    assert endpoint.translate_calls == 2
    assert outcomes[0].text == "你好"


async def test_persistent_401_returns_error_without_raising() -> None:
    endpoint = TranslationEndpoint(
        translate_responses=[
            httpx.Response(401, text="unauthorized"),
            httpx.Response(403, text="forbidden"),
        ]
    )
    translator = endpoint.translator()
    try:
        outcomes = await translator.translate(["hello"])
    finally:
        await translator.close()
    assert endpoint.translate_calls == 2
    assert outcomes[0].text is None and outcomes[0].error == "翻译服务暂时不可用"


@pytest.mark.parametrize("language", ["zh", "zh-Hans", "zh-Hant"])
async def test_detected_chinese_is_skipped_without_translation(language) -> None:
    endpoint = TranslationEndpoint(translate_responses=[detected_only(language)])
    translator = endpoint.translator()
    try:
        outcomes = await translator.translate(["你好"])
    finally:
        await translator.close()
    assert outcomes[0].skipped is True
    assert outcomes[0].text is None
    assert outcomes[0].language == language
    assert outcomes[0].error is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, json=[{"detectedLanguage": {"language": "en"}}]),
        httpx.Response(500, text="boom"),
        httpx.ConnectTimeout("timed out"),
    ],
)
async def test_failures_return_error_without_raising(response) -> None:
    endpoint = TranslationEndpoint(translate_responses=[response])
    translator = endpoint.translator()
    try:
        outcomes = await translator.translate(["hello"])
    finally:
        await translator.close()
    assert outcomes[0].error == "翻译服务暂时不可用"
    assert outcomes[0].text is None and outcomes[0].skipped is False
    # 错误文案不得回显端点 URL 或 token。
    assert "http" not in outcomes[0].error


async def test_long_text_truncated_before_sending() -> None:
    endpoint = TranslationEndpoint(translate_responses=[translated("en", "译文")])
    translator = endpoint.translator(rss_translate_max_chars=100)
    try:
        outcomes = await translator.translate(["a" * 500])
    finally:
        await translator.close()
    assert endpoint.bodies[0][0]["Text"] == "a" * 100
    assert outcomes[0].text == "译文"


async def test_blank_text_skipped_without_any_request() -> None:
    endpoint = TranslationEndpoint()
    translator = endpoint.translator()
    try:
        outcomes = await translator.translate(["   ", "", "\n"])
    finally:
        await translator.close()
    assert all(outcome.skipped for outcome in outcomes)
    assert endpoint.auth_calls == 0 and endpoint.translate_calls == 0


async def test_empty_batch_returns_empty_list() -> None:
    endpoint = TranslationEndpoint()
    translator = endpoint.translator()
    try:
        assert await translator.translate([]) == []
    finally:
        await translator.close()
    assert endpoint.auth_calls == 0


# --------------------------------------------------------------------------- #
# worker 翻译行为
# --------------------------------------------------------------------------- #


class FakeFetcher:
    def __init__(self, result: FeedResult | None = None) -> None:
        self.result = result or FeedResult()
        self.calls: list = []

    async def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.result

    async def close(self) -> None:
        return None


class FakePublisher:
    configured = True

    def __init__(self) -> None:
        self.calls: list = []
        self.error: Exception | None = None

    async def send(self, chat_id, text, link=""):
        self.calls.append((chat_id, text, link))
        if self.error is not None:
            raise self.error
        return PublishResult(len(self.calls), -100987)

    async def stop(self) -> None:
        return None


class FakeTranslator:
    def __init__(self) -> None:
        self.calls: list = []
        self.error: Exception | None = None
        self.skip_language: str | None = None
        self.closed = False

    async def translate(self, texts, *, target: str = "zh-Hans"):
        self.calls.append((list(texts), target))
        if self.error is not None:
            raise self.error
        if self.skip_language is not None:
            return [TranslationOutcome(language=self.skip_language, skipped=True) for _ in texts]
        return [TranslationOutcome(language="en", text=f"ZH:{text}") for text in texts]

    async def close(self) -> None:
        self.closed = True


def english_entry(number: int = 1) -> FeedEntry:
    return FeedEntry(
        entry_key=f"guid-{number}",
        title=f"English Title {number}",
        link=f"https://example.com/{number}",
        summary=f"English summary {number}",
        published_at=utc_now(),
    )


def payload(**kwargs) -> SubscriptionInput:
    return SubscriptionInput(
        **{
            "name": "RSS",
            "feed_url": "https://example.com/rss",
            "tg_chat_id": "-100123",
            **kwargs,
        }
    )


@pytest.fixture
async def rss_context(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'translate.db'}")
    await db.init()
    settings = Settings(_env_file=None, rss_send_interval_seconds=0.01)
    fetcher = FakeFetcher(FeedResult("Feed", [english_entry()]))
    publisher = FakePublisher()
    translator = FakeTranslator()
    service = RssService(db, settings, fetcher, translator)
    worker = RssWorker(settings, service, publisher, translator=translator)
    try:
        yield db, settings, fetcher, publisher, translator, service, worker
    finally:
        await worker.stop()
        await db.dispose()


async def make_due(db, subscription_id: int) -> None:
    async with db.session() as session:
        sub = await session.get(RssSubscription, subscription_id)
        sub.next_check_at = utc_now() - timedelta(seconds=1)
        rows = (
            await session.execute(
                select(RssEntry).where(
                    RssEntry.subscription_id == subscription_id,
                    RssEntry.status == "pending",
                )
            )
        ).scalars()
        for row in rows:
            row.next_retry_at = utc_now() - timedelta(seconds=1)


async def test_worker_publishes_original_above_translation(rss_context) -> None:
    _, _, _, publisher, translator, service, worker = rss_context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    await worker.process_subscription(sub["id"])
    assert len(publisher.calls) == 1
    text = publisher.calls[0][1]
    assert translator.calls == [(["English Title 1", "English summary 1"], "zh-Hans")]
    assert text.index("English Title 1") < text.index("ZH:English Title 1")
    assert text.index("English summary 1") < text.index("ZH:English summary 1")
    entry = (await service.list_entries(sub["id"]))[0]
    assert entry["status"] == "sent"
    assert entry["detected_language"] == "en"
    assert entry["translated_title"] == "ZH:English Title 1"
    assert entry["translated_summary"] == "ZH:English summary 1"


async def test_worker_translation_failure_still_publishes_original(rss_context) -> None:
    _, _, _, publisher, translator, service, worker = rss_context
    translator.error = RuntimeError("boom")
    sub = await service.create_subscription(payload(initial_policy="latest"))
    await worker.process_subscription(sub["id"])
    assert len(publisher.calls) == 1
    text = publisher.calls[0][1]
    assert "English Title 1" in text and "ZH:" not in text
    entry = (await service.list_entries(sub["id"]))[0]
    assert entry["status"] == "sent"
    assert entry["translated_title"] is None and entry["detected_language"] is None


async def test_worker_does_not_retranslate_cached_entry(rss_context) -> None:
    db, _, _, publisher, translator, service, worker = rss_context
    publisher.error = NetworkError("boom")
    sub = await service.create_subscription(payload(initial_policy="latest"))
    await worker.process_subscription(sub["id"])
    pending = (await service.list_entries(sub["id"]))[0]
    assert pending["status"] == "pending"
    assert pending["translated_title"] == "ZH:English Title 1"
    assert len(translator.calls) == 1
    publisher.error = None
    await make_due(db, sub["id"])
    await worker.process_subscription(sub["id"])
    assert len(translator.calls) == 1  # 已缓存译文不再请求
    assert len(publisher.calls) == 2
    assert (await service.list_entries(sub["id"]))[0]["status"] == "sent"


async def test_worker_skips_translation_when_subscription_disabled(rss_context) -> None:
    _, _, _, publisher, translator, service, worker = rss_context
    sub = await service.create_subscription(payload(initial_policy="latest", translate=False))
    await worker.process_subscription(sub["id"])
    assert translator.calls == []
    assert "ZH:" not in publisher.calls[0][1]
    assert (await service.list_entries(sub["id"]))[0]["detected_language"] is None


async def test_worker_skips_translation_when_globally_disabled(rss_context) -> None:
    _, settings, _, publisher, translator, service, worker = rss_context
    settings.rss_translate_enabled = False
    sub = await service.create_subscription(payload(initial_policy="latest"))
    await worker.process_subscription(sub["id"])
    assert translator.calls == []
    assert "ZH:" not in publisher.calls[0][1]


async def test_worker_chinese_source_is_not_translated(rss_context) -> None:
    _, _, fetcher, publisher, translator, service, worker = rss_context
    fetcher.result = FeedResult(
        "Feed",
        [
            FeedEntry(
                entry_key="zh-1",
                title="中文标题",
                link="https://example.com/zh",
                summary="中文摘要",
                published_at=utc_now(),
            )
        ],
    )
    translator.skip_language = "zh-Hans"
    sub = await service.create_subscription(payload(initial_policy="latest"))
    await worker.process_subscription(sub["id"])
    assert len(translator.calls) == 1
    assert "中文标题" in publisher.calls[0][1]
    entry = (await service.list_entries(sub["id"]))[0]
    assert entry["detected_language"] == "zh-Hans"
    assert entry["translated_title"] is None


async def test_preview_includes_translation_fields_without_persisting(rss_context) -> None:
    _, _, fetcher, publisher, translator, service, _ = rss_context
    fetcher.result = FeedResult("Feed", [english_entry(n) for n in range(1, 4)])
    result = await service.preview(payload())
    assert len(result["entries"]) == 3
    assert len(translator.calls) == 1 and len(translator.calls[0][0]) == 6
    for row in result["entries"]:
        assert row["detected_language"] == "en"
        assert row["translated_title"].startswith("ZH:")
        assert row["translated_title"] in row["rendered_text"]
    assert await service.list_subscriptions() == []
    assert publisher.calls == []


async def test_preview_translation_failure_warns_and_shows_original(rss_context) -> None:
    _, _, fetcher, publisher, translator, service, _ = rss_context
    fetcher.result = FeedResult("Feed", [english_entry(1)])
    translator.error = RuntimeError("boom")
    result = await service.preview(payload())
    assert "部分条目翻译失败，预览按原文展示" in result["warnings"]
    assert result["entries"][0]["translated_title"] is None
    assert "English Title 1" in result["entries"][0]["rendered_text"]
    assert publisher.calls == []


async def test_preview_skips_translation_when_disabled(rss_context) -> None:
    _, _, fetcher, _, translator, service, _ = rss_context
    fetcher.result = FeedResult("Feed", [english_entry(1)])
    result = await service.preview(payload(translate=False))
    assert translator.calls == []
    assert result["entries"][0]["translated_title"] is None


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #


def test_build_translation_block_variants() -> None:
    both = SimpleNamespace(translated_title="译文标题", translated_summary="译文摘要")
    title_only = SimpleNamespace(translated_title="译文标题", translated_summary=None)
    summary_only = SimpleNamespace(translated_title=None, translated_summary="译文摘要")
    assert build_translation_block(both) == "译文标题\n\n译文摘要"
    assert build_translation_block(title_only) == "译文标题"
    assert build_translation_block(summary_only) == "译文摘要"
    assert build_translation_block(SimpleNamespace()) == ""
    blank = SimpleNamespace(translated_title="  ", translated_summary="")
    assert build_translation_block(blank) == ""


def test_default_template_orders_original_above_translation() -> None:
    config = payload()
    entry = SimpleNamespace(
        title="English Title",
        summary="English summary",
        link="https://example.com/1",
        feed_title="feed",
        published_at=None,
        translated_title="中文标题",
        translated_summary="中文摘要",
    )
    text = render_entry(config, entry)
    assert text.index("English Title") < text.index("中文标题")
    assert text.index("English summary") < text.index("中文摘要")
    assert "\n\n\n" not in text
    assert text.endswith("https://example.com/1")

    without = SimpleNamespace(
        **{**entry.__dict__, "translated_title": None, "translated_summary": None}
    )
    assert "\n\n\n" not in render_entry(config, without)


# --------------------------------------------------------------------------- #
# schemas / settings
# --------------------------------------------------------------------------- #


def test_translation_defaults_and_template_fields() -> None:
    config = payload()
    assert config.translate is True
    assert payload(translate=False).translate is False
    assert "translation" in TEMPLATE_FIELDS
    assert "{translation}" in DEFAULT_MESSAGE_TEMPLATE
    assert config.message_template == DEFAULT_MESSAGE_TEMPLATE


def test_valid_translation_settings() -> None:
    settings = Settings(_env_file=None)
    assert settings.rss_translate_enabled is True
    assert settings.rss_translate_target == "zh-Hans"
    assert settings.rss_translate_timeout_seconds == 10
    assert settings.rss_translate_max_chars == 1200
    assert Settings(_env_file=None, rss_translate_target="zh-Hant").rss_translate_target == (
        "zh-Hant"
    )
    assert Settings(_env_file=None, rss_translate_target=" en ").rss_translate_target == "en"


@pytest.mark.parametrize(
    "overrides",
    [
        {"rss_translate_target": "zh_Hans"},
        {"rss_translate_target": ""},
        {"rss_translate_target": "zh-Hans-CN"},
        {"rss_translate_timeout_seconds": 0},
        {"rss_translate_timeout_seconds": 121},
        {"rss_translate_max_chars": 99},
        {"rss_translate_max_chars": 5001},
    ],
)
def test_invalid_translation_settings(overrides) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)
