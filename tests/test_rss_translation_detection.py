from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.rss.schemas import SubscriptionInput
from app.rss.translation_detection import detect_is_chinese, parse_detection_response
from app.rss.translator import (
    DETECTION_FALLBACK_WARNING,
    AITranslator,
    _translation_gate,
)


def completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def decisions(*rows) -> httpx.Response:
    return completion(json.dumps({
        "decisions": [
            {"id": index, "needs_translation": needed, "language": language}
            for index, needed, language in rows
        ]
    }))


def translator(handler, **kwargs) -> AITranslator:
    return AITranslator(
        Settings(
            _env_file=None,
            rss_translate_api_url="https://translation.example.com/v1",
            rss_translate_max_attempts=1,
            **kwargs,
        ),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("text", [
    "東京大学で新型人工知能研究を発表しました",
    "中文的正文很长，汉字占比很高，这里还有一段英文说明。\nThis update improves image quality.",
])
def test_local_rule_does_not_skip_japanese_or_foreign_paragraph(text):
    assert detect_is_chinese(text) is False


@pytest.mark.parametrize("text", [
    "2026-11-12 🎉 12345",
    "https://example.com/long/english/article?name=alpha",
    "contact@example.com @user_name #release",
    "```python\nprint('hello world')\n```",
    "`docker compose up -d`",
])
@pytest.mark.parametrize("mode", ["local", "ai"])
async def test_non_language_content_needs_no_ai_call(text, mode):
    def unexpected(request):
        pytest.fail("non-language content must not trigger AI requests")

    backend = translator(unexpected)
    try:
        result = (await backend.translate([text], detection_mode=mode))[0]
    finally:
        await backend.close()
    assert result.skipped and result.language == "und"
    assert result.decision.needs_translation is False


async def test_chinese_is_not_skipped_when_target_is_english():
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return completion("Hello, world!")

    backend = translator(handle)
    try:
        result = (await backend.translate(["你好，世界！"], target="EN"))[0]
    finally:
        await backend.close()
    assert result.text == "Hello, world!"
    assert len(calls) == 1


async def test_ai_decides_each_field_then_translates_only_needed_fields():
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        if "response_format" in body:
            return decisions((2, False, "zh"), (0, False, "en"), (1, True, "mixed"))
        return completion("这次更新增加了图片识别功能。")

    texts = [
        "OpenAI ChatGPT",
        "这是一段较长的中文说明，介绍今天发布的新功能和使用方式。"
        "This release adds image understanding.",
        "这是中文摘要",
    ]
    backend = translator(
        handle, rss_translate_api_key="test-secret", rss_translate_model="test-model"
    )
    try:
        results = await backend.translate(texts, detection_mode="ai")
    finally:
        await backend.close()
    assert [result.skipped for result in results] == [True, False, True]
    assert results[1].text == "这次更新增加了图片识别功能。"
    assert results[1].language == "mixed"
    assert all(result.decision.source == "ai" for result in results)
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["model"] == "test-model"
    assert json.loads(calls[0]["messages"][1]["content"])["texts"] == [
        {"id": index, "text": text} for index, text in enumerate(texts)
    ]
    assert calls[1]["messages"][1]["content"] == texts[1]


@pytest.mark.parametrize("failed", [
    httpx.Response(500, text="private upstream error"),
    httpx.ReadTimeout("private request URL"),
    completion('{"decisions":[{"id":0,"needs_translation":"false","language":"en"}]}'),
    completion("not JSON"),
])
async def test_failed_ai_detection_falls_back_and_can_still_translate(failed, caplog):
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        if "response_format" in body:
            if isinstance(failed, Exception):
                raise failed
            return failed
        return completion("新的产品发布了")

    backend = translator(handle)
    try:
        result = (await backend.translate(["A new product is released."], detection_mode="ai"))[0]
    finally:
        await backend.close()
    assert result.text == "新的产品发布了"
    assert result.error is None
    assert result.warning == DETECTION_FALLBACK_WARNING
    assert result.decision.source == "fallback"
    assert len(calls) == 2
    assert "private" not in caplog.text
    assert "translation.example.com" not in caplog.text


async def test_detection_deadline_cancels_request_then_uses_local_rules():
    async def handle(request):
        body = json.loads(request.content)
        if "response_format" in body:
            await asyncio.sleep(1)
        return completion("新功能已经发布")

    backend = translator(handle, rss_translate_detection_timeout_seconds=0.02)
    try:
        results = await asyncio.wait_for(
            backend.translate(["A new feature is available."], detection_mode="ai"), timeout=0.5
        )
    finally:
        await backend.close()
    assert results[0].text == "新功能已经发布"
    assert results[0].decision.source == "fallback"


async def test_detection_waiting_for_gate_is_also_bounded():
    def unexpected(request):
        pytest.fail("a busy gate should fall back before making a request")

    backend = translator(unexpected, rss_translate_detection_timeout_seconds=0.02)
    try:
        async with _translation_gate(1):
            results = await asyncio.wait_for(
                backend.translate(["这是中文正文"], detection_mode="ai"), timeout=0.5
            )
    finally:
        await backend.close()
    assert results[0].skipped
    assert results[0].decision.source == "fallback"


async def test_successful_decisions_are_cached_but_failures_are_not():
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(500) if count == 1 else decisions((0, False, "zh"))

    backend = translator(handle)
    try:
        first = await backend.translate(["这是中文正文"], detection_mode="ai")
        second = await backend.translate(["这是中文正文"], detection_mode="ai")
        third = await backend.translate(["这是中文正文"], detection_mode="ai")
    finally:
        await backend.close()
    assert count == 2
    assert first[0].decision.source == "fallback"
    assert second[0].decision.source == third[0].decision.source == "ai"


async def test_detection_cache_is_scoped_to_target_language():
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return decisions((0, False, "en"))

    backend = translator(handle)
    try:
        await backend.translate(["OpenAI"], target="ZH", detection_mode="ai")
        await backend.translate(["OpenAI"], target="EN", detection_mode="ai")
    finally:
        await backend.close()
    assert count == 2


async def test_detection_batches_have_bounded_size_and_preserve_field_order():
    batch_sizes = []

    def handle(request):
        rows = json.loads(json.loads(request.content)["messages"][1]["content"])["texts"]
        batch_sizes.append(len(rows))
        return decisions(*[(row["id"], False, "zh") for row in reversed(rows)])

    backend = translator(handle)
    try:
        results = await backend.translate([f"这是第{index}个中文标题" for index in range(23)],
                                          detection_mode="ai")
    finally:
        await backend.close()
    assert batch_sizes == [10, 10, 3]
    assert len(results) == 23 and all(result.skipped for result in results)


async def test_cancelled_detection_propagates_without_fallback():
    def handle(request):
        raise asyncio.CancelledError()

    backend = translator(handle)
    try:
        with pytest.raises(asyncio.CancelledError):
            await backend.translate(["Some article text"], detection_mode="ai")
    finally:
        await backend.close()


@pytest.mark.parametrize("content", [
    '{"decisions":[]}',
    '{"decisions":[{"id":true,"needs_translation":false,"language":"zh"}]}',
    '{"decisions":[{"id":0,"needs_translation":0,"language":"zh"}]}',
    '{"decisions":[{"id":0,"needs_translation":false,"language":"private words"}]}',
    '{"decisions":[{"id":99,"needs_translation":false,"language":"zh"}]}',
])
def test_invalid_ai_decisions_are_rejected(content):
    with pytest.raises(ValueError):
        parse_detection_response(content, {0})


def test_duplicate_ids_cannot_silently_skip_another_field():
    content = json.dumps({"decisions": [
        {"id": 0, "needs_translation": False, "language": "zh"},
        {"id": 0, "needs_translation": False, "language": "zh"},
    ]})
    with pytest.raises(ValueError):
        parse_detection_response(content, {0, 1})


def test_detection_configuration_has_validated_bounds():
    with pytest.raises(ValidationError):
        SubscriptionInput(name="test", feed_url="https://example.com/feed", tg_chat_id="-100",
                          translation_detection="unknown")
    assert Settings(_env_file=None).rss_translate_detection_timeout_seconds == 8
    for invalid in (0, -1, 31, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, rss_translate_detection_timeout_seconds=invalid)
