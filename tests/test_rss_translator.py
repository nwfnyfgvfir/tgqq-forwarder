from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from telegram.error import NetworkError

from app.config import DEFAULT_RSS_TRANSLATE_API_URL, Settings
from app.rss.errors import RssError
from app.rss.fetcher import FeedEntry, FeedResult
from app.rss.formatting import build_translation_block, render_entry
from app.rss.publisher import PublishResult
from app.rss.schemas import DEFAULT_MESSAGE_TEMPLATE, TEMPLATE_FIELDS, SubscriptionInput
from app.rss.service import RssService
from app.rss.translator import (
    AITranslator,
    TranslationOutcome,
    _is_retryable,
    _retry_delay,
    _translation_gate,
    detect_is_chinese,
    resolve_endpoint_url,
)
from app.rss.worker import RssWorker
from app.storage.db import Database
from app.storage.models import RssEntry, RssSubscription, utc_now

DEFAULT_URL = DEFAULT_RSS_TRANSLATE_API_URL
TEST_URL = "https://api.openai.com/v1/chat/completions"

# --------------------------------------------------------------------------- #
# 假 Chat Completions 端点与工具
# --------------------------------------------------------------------------- #


def ok(text: str) -> httpx.Response:
    """标准 Chat Completions 成功响应：choices[0].message.content。"""
    return httpx.Response(
        200,
        json={"choices": [{"index": 0, "message": {"role": "assistant", "content": text}}]},
    )


class AIEndpoint:
    """记录请求的假端点：按脚本顺序返回响应，用尽后返回成功空串。"""

    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.calls = 0
        self.urls: list[str] = []
        self.bodies: list[dict] = []
        self.headers: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.urls.append(str(request.url))
        self.bodies.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return ok("")

    def translator(self, **settings_kwargs) -> AITranslator:
        settings_kwargs.setdefault("rss_translate_api_url", TEST_URL)
        # 默认不重试，保证失败类用例的调用次数确定；重试单独用下面的用例验证。
        settings_kwargs.setdefault("rss_translate_max_attempts", 1)
        return AITranslator(
            Settings(_env_file=None, **settings_kwargs),
            transport=httpx.MockTransport(self.handler),
        )


class ConcurrencyTransport(httpx.AsyncBaseTransport):
    """记录同时在建请求数的异步传输，用于验证全局并发闸门。"""

    def __init__(self, delay: float = 0.03) -> None:
        self.delay = delay
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            body = json.loads(request.content)
            text = body["messages"][1]["content"]
            return ok(f"译:{text}")
        finally:
            self.in_flight -= 1


# --------------------------------------------------------------------------- #
# AITranslator 单元测试
# --------------------------------------------------------------------------- #


async def test_chat_completions_contract_returns_translation() -> None:
    endpoint = AIEndpoint([ok("你好")])
    translator = endpoint.translator(
        rss_translate_api_key="sk-test", rss_translate_model="gpt-4o-mini"
    )
    try:
        outcomes = await translator.translate(["hello"])
    finally:
        await translator.close()
    assert outcomes[0].text == "你好"
    assert outcomes[0].language == "auto"
    assert outcomes[0].skipped is False and outcomes[0].error is None
    assert endpoint.urls == [TEST_URL]
    body = endpoint.bodies[0]
    assert body["model"] == "gpt-4o-mini"
    assert body["temperature"] == 0 and body["stream"] is False
    assert body["messages"][0]["role"] == "system"
    assert "简体中文" in body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "hello"}
    assert endpoint.headers[0]["authorization"] == "Bearer sk-test"


async def test_missing_api_key_sends_no_authorization_header() -> None:
    endpoint = AIEndpoint([ok("你好")])
    translator = endpoint.translator()
    try:
        await translator.translate(["hello"])
    finally:
        await translator.close()
    assert "authorization" not in endpoint.headers[0]


async def test_target_override_in_system_prompt() -> None:
    endpoint = AIEndpoint([ok("Hello")])
    translator = endpoint.translator()
    try:
        await translator.translate(["Bonjour"], target="EN")
    finally:
        await translator.close()
    assert "English" in endpoint.bodies[0]["messages"][0]["content"]


async def test_unknown_target_tag_is_used_verbatim() -> None:
    endpoint = AIEndpoint([ok("translated")])
    translator = endpoint.translator()
    try:
        await translator.translate(["hello"], target="sw")
    finally:
        await translator.close()
    assert "sw" in endpoint.bodies[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": [{"type": "text", "text": "世界"}]}}
                ]
            },
        ),
        httpx.Response(200, json={"choices": [{"message": {"content": "世界"}}]}),
    ],
)
async def test_alternative_payloads_are_parsed(response) -> None:
    endpoint = AIEndpoint([response])
    translator = endpoint.translator()
    try:
        outcomes = await translator.translate(["hello"])
    finally:
        await translator.close()
    assert outcomes[0].error is None
    assert outcomes[0].text == "世界"
    assert outcomes[0].language == "auto"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": 123}}]}),
        httpx.Response(200, json={"choices": [{"message": {}}]}),
        httpx.Response(200, json={"error": {"message": "rate limited"}}),
        httpx.Response(200, text="not json"),
        httpx.Response(500, text="boom"),
        httpx.Response(429, text="too many requests"),
        httpx.Response(401, text="unauthorized"),
        httpx.ConnectTimeout("timed out"),
    ],
)
async def test_failures_return_error_without_raising(response) -> None:
    endpoint = AIEndpoint([response])
    translator = endpoint.translator()
    try:
        outcomes = await translator.translate(["hello"])
    finally:
        await translator.close()
    assert outcomes[0].error == "翻译服务暂时不可用"
    assert outcomes[0].text is None and outcomes[0].skipped is False
    # 错误文案不得回显端点 URL（含 key）或响应内容。
    assert "http" not in outcomes[0].error


async def test_chinese_text_skipped_without_any_request() -> None:
    endpoint = AIEndpoint()
    translator = endpoint.translator()
    try:
        outcomes = await translator.translate(["你好，世界！", "hello"])
    finally:
        await translator.close()
    assert outcomes[0].skipped is True
    assert outcomes[0].language == "zh"
    assert outcomes[0].text is None and outcomes[0].error is None
    # 中文不发请求；仅非中文的 "hello" 触发一次请求。
    assert endpoint.calls == 1
    assert [body["messages"][1]["content"] for body in endpoint.bodies] == ["hello"]


@pytest.mark.parametrize("text", ["こんにちは", "テストです", "안녕하세요"])
async def test_kana_and_other_scripts_are_not_chinese(text) -> None:
    assert detect_is_chinese(text) is False
    endpoint = AIEndpoint([ok("译文")])
    translator = endpoint.translator()
    try:
        outcomes = await translator.translate([text])
    finally:
        await translator.close()
    assert endpoint.calls == 1
    assert outcomes[0].text == "译文" and outcomes[0].language == "auto"


def test_detect_is_chinese_boundaries() -> None:
    assert detect_is_chinese("你好，世界！") is True
    assert detect_is_chinese("中") is True
    assert detect_is_chinese("hello 世界") is False  # 汉字占比低于一半
    assert detect_is_chinese("こんにちは") is False  # 假名不算中文
    assert detect_is_chinese("   ") is False
    assert detect_is_chinese("") is False


async def test_identical_translation_is_skipped() -> None:
    endpoint = AIEndpoint([ok("hello")])
    translator = endpoint.translator()
    try:
        outcomes = await translator.translate(["hello"])
    finally:
        await translator.close()
    assert endpoint.calls == 1
    assert outcomes[0].skipped is True
    assert outcomes[0].text is None
    assert outcomes[0].language == "auto" and outcomes[0].error is None


async def test_long_text_truncated_before_sending() -> None:
    endpoint = AIEndpoint([ok("译文")])
    translator = endpoint.translator(rss_translate_max_chars=100)
    try:
        outcomes = await translator.translate(["a" * 500])
    finally:
        await translator.close()
    assert len(endpoint.bodies[0]["messages"][1]["content"]) == 100
    assert outcomes[0].text == "译文"


async def test_blank_text_skipped_without_any_request() -> None:
    endpoint = AIEndpoint()
    translator = endpoint.translator()
    try:
        outcomes = await translator.translate(["   ", "", "\n"])
    finally:
        await translator.close()
    assert all(outcome.skipped for outcome in outcomes)
    assert endpoint.calls == 0


async def test_empty_batch_returns_empty_list() -> None:
    endpoint = AIEndpoint()
    translator = endpoint.translator()
    try:
        assert await translator.translate([]) == []
    finally:
        await translator.close()
    assert endpoint.calls == 0


async def test_requests_are_sequential_in_order() -> None:
    endpoint = AIEndpoint([ok("一"), ok("二"), ok("三")])
    translator = endpoint.translator()
    try:
        outcomes = await translator.translate(["one", "two", "three"])
    finally:
        await translator.close()
    assert [body["messages"][1]["content"] for body in endpoint.bodies] == [
        "one",
        "two",
        "three",
    ]
    assert [outcome.text for outcome in outcomes] == ["一", "二", "三"]


async def test_client_reused_across_calls() -> None:
    endpoint = AIEndpoint([ok("一"), ok("二")])
    translator = endpoint.translator()
    try:
        await translator.translate(["one"])
        client = translator._client
        await translator.translate(["two"])
        assert translator._client is client
    finally:
        await translator.close()
    assert endpoint.calls == 2


async def test_base_url_is_completed_to_chat_completions_endpoint() -> None:
    endpoint = AIEndpoint([ok("译文")])
    translator = endpoint.translator(rss_translate_api_url="https://api.deepseek.com/v1")
    try:
        await translator.translate(["hello"])
    finally:
        await translator.close()
    assert endpoint.urls == ["https://api.deepseek.com/v1/chat/completions"]


def test_resolve_endpoint_url_variants() -> None:
    full = "https://api.openai.com/v1/chat/completions"
    assert resolve_endpoint_url(full) == full
    assert (
        resolve_endpoint_url("https://api.deepseek.com/v1")
        == "https://api.deepseek.com/v1/chat/completions"
    )
    assert (
        resolve_endpoint_url("https://api.deepseek.com")
        == "https://api.deepseek.com/chat/completions"
    )
    assert resolve_endpoint_url("https://gateway/v1/") == "https://gateway/v1/chat/completions"
    with_query = "https://gateway/v1/chat/completions?api-version=2024-01-01"
    assert resolve_endpoint_url(with_query) == with_query


# --------------------------------------------------------------------------- #
# 并发闸门与重试
# --------------------------------------------------------------------------- #


async def test_translation_concurrency_gate_is_shared_across_instances() -> None:
    transport = ConcurrencyTransport()
    settings = Settings(_env_file=None, rss_translate_api_url=TEST_URL)
    first = AITranslator(settings, transport=transport)
    second = AITranslator(settings, transport=transport)
    try:
        await asyncio.gather(
            first.translate(["one", "two", "three"]),
            second.translate(["four", "five", "six"]),
        )
    finally:
        await first.close()
        await second.close()
    # 两个实例（模拟 service 预览与 worker 发送）共享闸门，任一时刻最多一个在途请求。
    assert transport.calls == 6
    assert transport.max_in_flight == 1


async def test_translation_gate_is_shared_per_loop_and_limit() -> None:
    assert _translation_gate(1) is _translation_gate(1)
    assert _translation_gate(2) is not _translation_gate(1)


def test_is_retryable_classification() -> None:
    request = httpx.Request("POST", TEST_URL)
    assert _is_retryable(
        httpx.HTTPStatusError("429", request=request, response=httpx.Response(429))
    )
    assert _is_retryable(
        httpx.HTTPStatusError("503", request=request, response=httpx.Response(503))
    )
    assert _is_retryable(httpx.ConnectTimeout("timed out"))
    assert not _is_retryable(
        httpx.HTTPStatusError("401", request=request, response=httpx.Response(401))
    )
    assert not _is_retryable(ValueError("unexpected chat completions payload"))


def test_retry_delay_honors_retry_after_and_backoff() -> None:
    request = httpx.Request("POST", TEST_URL)
    limited = httpx.HTTPStatusError(
        "429", request=request, response=httpx.Response(429, headers={"retry-after": "7"})
    )
    assert _retry_delay(limited, 0, 1.0) == 7.0
    server = httpx.HTTPStatusError("503", request=request, response=httpx.Response(503))
    assert _retry_delay(server, 0, 1.0) == 1.0
    assert _retry_delay(server, 1, 1.0) == 2.0
    assert _retry_delay(server, 2, 1.0) == 4.0


async def test_retries_on_429_then_succeeds() -> None:
    endpoint = AIEndpoint([httpx.Response(429, headers={"retry-after": "0"}), ok("你好")])
    translator = endpoint.translator(
        rss_translate_max_attempts=3, rss_translate_retry_backoff_seconds=0.01
    )
    try:
        outcomes = await translator.translate(["hello"])
    finally:
        await translator.close()
    assert outcomes[0].text == "你好" and outcomes[0].error is None
    assert endpoint.calls == 2


async def test_retries_on_server_error_then_succeeds() -> None:
    endpoint = AIEndpoint([httpx.Response(503, text="unavailable"), ok("世界")])
    translator = endpoint.translator(
        rss_translate_max_attempts=3, rss_translate_retry_backoff_seconds=0.01
    )
    try:
        outcomes = await translator.translate(["hello"])
    finally:
        await translator.close()
    assert outcomes[0].text == "世界"
    assert endpoint.calls == 2


async def test_does_not_retry_on_client_error() -> None:
    endpoint = AIEndpoint([httpx.Response(401, text="unauthorized")])
    translator = endpoint.translator(
        rss_translate_max_attempts=3, rss_translate_retry_backoff_seconds=0.01
    )
    try:
        outcomes = await translator.translate(["hello"])
    finally:
        await translator.close()
    assert outcomes[0].error == "翻译服务暂时不可用"
    assert endpoint.calls == 1


async def test_retry_exhausted_returns_error() -> None:
    endpoint = AIEndpoint([httpx.Response(503), httpx.Response(503), httpx.Response(503)])
    translator = endpoint.translator(
        rss_translate_max_attempts=3, rss_translate_retry_backoff_seconds=0.01
    )
    try:
        outcomes = await translator.translate(["hello"])
    finally:
        await translator.close()
    assert outcomes[0].error == "翻译服务暂时不可用"
    assert endpoint.calls == 3


async def test_malformed_payload_is_not_retried() -> None:
    endpoint = AIEndpoint([httpx.Response(200, json={"choices": []})])
    translator = endpoint.translator(
        rss_translate_max_attempts=3, rss_translate_retry_backoff_seconds=0.01
    )
    try:
        outcomes = await translator.translate(["hello"])
    finally:
        await translator.close()
    assert outcomes[0].error == "翻译服务暂时不可用"
    assert endpoint.calls == 1


# --------------------------------------------------------------------------- #
# worker 翻译行为
# --------------------------------------------------------------------------- #


class FakeFetcher:
    def __init__(self, result: FeedResult | None = None) -> None:
        self.result = result or FeedResult()
        self.calls: list = []
        self.error: Exception | None = None

    async def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
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

    async def translate(self, texts, *, target: str = "ZH"):
        self.calls.append((list(texts), target))
        if self.error is not None:
            raise self.error
        if self.skip_language is not None:
            return [TranslationOutcome(language=self.skip_language, skipped=True) for _ in texts]
        return [TranslationOutcome(language="auto", text=f"ZH:{text}") for text in texts]

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
    assert translator.calls == [(["English Title 1", "English summary 1"], "ZH")]
    assert text.index("English Title 1") < text.index("ZH:English Title 1")
    assert text.index("English summary 1") < text.index("ZH:English summary 1")
    entry = (await service.list_entries(sub["id"]))[0]
    assert entry["status"] == "sent"
    assert entry["detected_language"] == "auto"
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
    translator.skip_language = "zh"
    sub = await service.create_subscription(payload(initial_policy="latest"))
    await worker.process_subscription(sub["id"])
    assert len(translator.calls) == 1
    assert "中文标题" in publisher.calls[0][1]
    entry = (await service.list_entries(sub["id"]))[0]
    # detected_language="zh" 作为「已判定为中文」的缓存标记，重试不会重复请求。
    assert entry["detected_language"] == "zh"
    assert entry["translated_title"] is None


async def test_worker_skips_title_translation_when_template_omits_title(rss_context) -> None:
    _, _, _, publisher, translator, service, worker = rss_context
    template = "{summary}\n\n{translation}\n\n{link}"
    sub = await service.create_subscription(
        payload(initial_policy="latest", message_template=template)
    )
    await worker.process_subscription(sub["id"])
    # 只翻译模板引用的摘要，标题不请求、不写缓存、不出现在译文里。
    assert translator.calls == [(["English summary 1"], "ZH")]
    text = publisher.calls[0][1]
    assert "ZH:English summary 1" in text
    assert "ZH:English Title 1" not in text
    entry = (await service.list_entries(sub["id"]))[0]
    assert entry["status"] == "sent"
    assert entry["translated_title"] is None
    assert entry["translated_summary"] == "ZH:English summary 1"


async def test_preview_skips_title_translation_when_template_omits_title(rss_context) -> None:
    _, _, fetcher, _, translator, service, _ = rss_context
    fetcher.result = FeedResult("Feed", [english_entry(1)])
    result = await service.preview(payload(message_template="{summary}\n\n{translation}\n\n{link}"))
    assert translator.calls == [(["English summary 1"], "ZH")]
    row = result["entries"][0]
    assert row["translated_title"] is None
    assert row["translated_summary"] == "ZH:English summary 1"
    assert "ZH:English Title 1" not in row["rendered_text"]


async def test_test_send_latest_skips_title_translation_when_template_omits_title(
    rss_context,
) -> None:
    _, _, fetcher, publisher, translator, service, worker = rss_context
    fetcher.result = FeedResult("Feed", [english_entry(1)])
    sub = await service.create_subscription(
        payload(message_template="{summary}\n\n{translation}\n\n{link}")
    )
    result = await worker.test_send_latest(sub["id"])
    assert translator.calls == [(["English summary 1"], "ZH")]
    assert result["sent"] is True
    assert "ZH:English Title 1" not in publisher.calls[0][1]


async def test_worker_translates_both_fields_when_template_uses_only_translation(
    rss_context,
) -> None:
    _, _, _, publisher, translator, service, worker = rss_context
    sub = await service.create_subscription(
        payload(initial_policy="latest", message_template="{translation}\n\n{link}")
    )
    await worker.process_subscription(sub["id"])
    assert translator.calls == [(["English Title 1", "English summary 1"], "ZH")]
    text = publisher.calls[0][1]
    assert "ZH:English Title 1" in text and "ZH:English summary 1" in text


async def test_preview_includes_translation_fields_without_persisting(rss_context) -> None:
    _, _, fetcher, publisher, translator, service, _ = rss_context
    fetcher.result = FeedResult("Feed", [english_entry(n) for n in range(1, 4)])
    result = await service.preview(payload())
    assert len(result["entries"]) == 3
    assert len(translator.calls) == 1 and len(translator.calls[0][0]) == 6
    for row in result["entries"]:
        assert row["detected_language"] == "auto"
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
# 手动试发最新一条
# --------------------------------------------------------------------------- #


async def test_test_send_latest_publishes_newest_entry(rss_context) -> None:
    _, _, fetcher, publisher, translator, service, worker = rss_context
    fetcher.result = FeedResult("Feed", [english_entry(n) for n in range(1, 4)])
    # initial_policy=skip：正式流程首次只建基线不发送，试发必须绕过该限制。
    sub = await service.create_subscription(payload(initial_policy="skip"))
    result = await worker.test_send_latest(sub["id"])
    assert result["sent"] is True
    assert result["matched"] is True
    assert result["translated"] is True
    assert result["entry"]["title"] == "English Title 3"
    assert result["message_id"] == 1
    assert len(publisher.calls) == 1
    text = publisher.calls[0][1]
    assert "English Title 3" in text and "ZH:English Title 3" in text
    # 试发不写条目历史，避免污染去重基线。
    assert await service.list_entries(sub["id"]) == []


async def test_test_send_latest_reports_keyword_mismatch_but_sends(rss_context) -> None:
    _, _, fetcher, publisher, translator, service, worker = rss_context
    fetcher.result = FeedResult("Feed", [english_entry(1)])
    sub = await service.create_subscription(payload(include_keywords=["不会命中"]))
    result = await worker.test_send_latest(sub["id"])
    assert result["sent"] is True
    assert result["matched"] is False
    assert any("未命中" in warning for warning in result["warnings"])
    assert len(publisher.calls) == 1


async def test_test_send_latest_respects_translate_toggle(rss_context) -> None:
    _, _, fetcher, publisher, translator, service, worker = rss_context
    fetcher.result = FeedResult("Feed", [english_entry(1)])
    sub = await service.create_subscription(payload(translate=False))
    result = await worker.test_send_latest(sub["id"])
    assert result["sent"] is True
    assert result["translated"] is False
    assert translator.calls == []
    assert "ZH:" not in publisher.calls[0][1]


async def test_test_send_latest_translation_failure_sends_original(rss_context) -> None:
    _, _, fetcher, publisher, translator, service, worker = rss_context
    fetcher.result = FeedResult("Feed", [english_entry(1)])
    translator.error = RuntimeError("boom")
    sub = await service.create_subscription(payload())
    result = await worker.test_send_latest(sub["id"])
    assert result["sent"] is True
    assert result["translated"] is False
    assert any("翻译失败" in warning for warning in result["warnings"])
    assert "ZH:" not in publisher.calls[0][1]


async def test_test_send_latest_reports_send_failure(rss_context) -> None:
    _, _, fetcher, publisher, translator, service, worker = rss_context
    fetcher.result = FeedResult("Feed", [english_entry(1)])
    publisher.error = NetworkError("boom")
    sub = await service.create_subscription(payload())
    result = await worker.test_send_latest(sub["id"])
    assert result["sent"] is False
    assert "网络" in result["error"]
    assert result["entry"]["title"] == "English Title 1"


async def test_test_send_latest_skips_entries_without_link(rss_context) -> None:
    _, _, fetcher, _, _, service, worker = rss_context
    fetcher.result = FeedResult(
        "Feed",
        [
            FeedEntry("a", "无链接", "", "摘要", utc_now()),
            FeedEntry("b", "有链接", "https://example.com/b", "摘要", utc_now()),
        ],
    )
    sub = await service.create_subscription(payload())
    result = await worker.test_send_latest(sub["id"])
    assert result["entry"]["title"] == "有链接"


async def test_test_send_latest_requires_publisher_configured(rss_context) -> None:
    _, _, _, publisher, _, service, worker = rss_context
    publisher.configured = False
    sub = await service.create_subscription(payload())
    with pytest.raises(RssError) as excinfo:
        await worker.test_send_latest(sub["id"])
    assert excinfo.value.code == "rss_publisher_missing"


async def test_test_send_latest_no_entries_raises(rss_context) -> None:
    _, _, fetcher, _, _, service, worker = rss_context
    fetcher.result = FeedResult("Feed", [])
    sub = await service.create_subscription(payload())
    with pytest.raises(RssError) as excinfo:
        await worker.test_send_latest(sub["id"])
    assert excinfo.value.code == "rss_no_entries"


async def test_test_send_latest_fetch_failure_raises(rss_context) -> None:
    _, _, fetcher, _, _, service, worker = rss_context
    fetcher.error = RssError("rss_fetch_failed", 502, "订阅抓取失败或超时")
    sub = await service.create_subscription(payload())
    with pytest.raises(RssError) as excinfo:
        await worker.test_send_latest(sub["id"])
    assert excinfo.value.code == "rss_fetch_failed"


async def test_test_send_latest_missing_subscription_raises(rss_context) -> None:
    _, _, _, _, _, _, worker = rss_context
    with pytest.raises(RssError) as excinfo:
        await worker.test_send_latest(999)
    assert excinfo.value.code == "rss_not_found"


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


def test_build_translation_block_follows_template_placeholders() -> None:
    both = SimpleNamespace(translated_title="译文标题", translated_summary="译文摘要")
    # 模板没写 {title}：译文里不出现标题。
    assert build_translation_block(both, "{summary}\n{translation}") == "译文摘要"
    # 模板没写 {summary}：译文里不出现摘要。
    assert build_translation_block(both, "{title}\n{translation}") == "译文标题"
    # 只用 {translation}：原文字段全由译文承担，两者都保留。
    assert build_translation_block(both, "{translation}") == "译文标题\n\n译文摘要"
    # {{title}} 是转义字面量，不算引用。
    assert build_translation_block(both, "{{title}}\n{summary}\n{translation}") == "译文摘要"


def test_render_entry_without_title_placeholder_omits_translated_title() -> None:
    config = payload(message_template="{summary}\n\n{translation}\n\n{link}")
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
    assert "中文摘要" in text
    assert "中文标题" not in text
    assert "English Title" not in text
    assert "\n\n\n" not in text


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
    assert settings.rss_translate_target == "ZH"
    assert settings.rss_translate_api_url == DEFAULT_URL
    assert settings.rss_translate_api_key is None
    assert settings.rss_translate_model == "gpt-4o-mini"
    assert settings.rss_translate_timeout_seconds == 30
    assert settings.rss_translate_max_chars == 1200
    assert settings.rss_translate_max_concurrency == 1
    assert settings.rss_translate_max_attempts == 3
    assert settings.rss_translate_retry_backoff_seconds == 1.0
    assert Settings(_env_file=None, rss_translate_target="zh-Hant").rss_translate_target == (
        "zh-Hant"
    )
    assert Settings(_env_file=None, rss_translate_target=" en ").rss_translate_target == "en"


def test_api_key_and_model_normalization() -> None:
    assert Settings(_env_file=None, rss_translate_api_key="  ").rss_translate_api_key is None
    assert (
        Settings(_env_file=None, rss_translate_api_key=" sk-1 ").rss_translate_api_key == "sk-1"
    )
    assert Settings(_env_file=None, rss_translate_model="   ").rss_translate_model == "gpt-4o-mini"
    assert (
        Settings(_env_file=None, rss_translate_model=" deepseek-chat ").rss_translate_model
        == "deepseek-chat"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"rss_translate_target": "zh_Hans"},
        {"rss_translate_target": ""},
        {"rss_translate_target": "zh-Hans-CN"},
        {"rss_translate_target": "1"},
        {"rss_translate_timeout_seconds": 0},
        {"rss_translate_timeout_seconds": 121},
        {"rss_translate_max_chars": 99},
        {"rss_translate_max_chars": 5001},
        {"rss_translate_model": "m" * 201},
        {"rss_translate_api_key": "k" * 4097},
        {"rss_translate_max_concurrency": 0},
        {"rss_translate_max_concurrency": 17},
        {"rss_translate_max_attempts": 0},
        {"rss_translate_max_attempts": 11},
        {"rss_translate_retry_backoff_seconds": -0.1},
        {"rss_translate_retry_backoff_seconds": 61},
    ],
)
def test_invalid_translation_settings(overrides) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)
