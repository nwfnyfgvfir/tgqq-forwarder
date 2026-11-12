from __future__ import annotations

import asyncio
import json
import shutil
import wave
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.channels.router import DeliveryRouter
from app.config import Settings
from app.qq_official.moderation import QQ_VIOLATION_NOTICE, AIContentModerator
from app.rules.models import TelegramForwardMessage
from app.storage.models import ForwardRule

SAFE_TECH_MESSAGES = [
    "OmniGet 开源下载器，支持 YouTube、X、TikTok、视频、图片、音频、Whisper 字幕，"
    "可处理 Udemy、Coursera 课程资源，支持 MCP Server 和 AI Agent。白嫖，开源。",
    "Upcoder Spark 免费容器有 SSH、HTTPS、自动备份。条款禁止代理、VPN、隧道和中继，"
    "有人称安装 Tailscale 后账号被封，安装前请注意服务条款和 CPU 配额。",
    "Mozilla Firefox 免费 VPN 取消每月 50GB 流量上限，地区从 6 个增加到 33 个，"
    "活动持续到 2027 年 1 月 31 日。",
]


def settings(**options) -> Settings:
    return Settings(
        _env_file=None, qq_ai_moderation_enabled=True,
        qq_ai_moderation_api_url="https://review.example/v1",
        qq_ai_moderation_api_key="test-key", **options,
    )


def verdict(category: str | None = None) -> httpx.Response:
    content = {"blocked": bool(category), "categories": [category] if category else []}
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(content)
    }}]})


class QQSender:
    def __init__(self):
        self.calls = []

    async def send(self, message):
        self.calls.append(message)
        return {"id": "sent"}


def message(text: str, image: Path | None = None) -> TelegramForwardMessage:
    return TelegramForwardMessage(
        message_id=1, chat_id=-100, chat_title="news", chat_type="channel",
        sender_id=42, sender_username="source", sender_display_name="Source", sender_is_bot=False,
        text=text, date=None, media_path=image, media_type="photo" if image else None,
    )


def rule(**options) -> ForwardRule:
    return ForwardRule(name="rule", qq_target_type="group", qq_target_id="target", **options)


@pytest.mark.parametrize("text", SAFE_TECH_MESSAGES)
@pytest.mark.parametrize("initial_category", ["other", "political_sensitive"])
async def test_false_positive_is_rechecked_before_original_is_forwarded(text, initial_category):
    qq = QQSender()
    requests = []

    def handle(request):
        assert qq.calls == [], "no QQ message may be sent before confirmation finishes"
        requests.append(json.loads(request.content))
        return verdict(initial_category) if len(requests) == 1 else verdict()

    router = DeliveryRouter(settings(), qq)
    moderator = AIContentModerator(router.settings, transport=httpx.MockTransport(handle))
    router.qq_moderator = moderator
    try:
        result = await router.deliver(rule(), message(text), text)
        cached = await moderator.check(requests[0]["messages"][1]["content"])
    finally:
        await moderator.close()

    assert not result.blocked and not cached.blocked
    assert len(requests) == 2
    assert requests[0]["messages"][1] == requests[1]["messages"][1]
    assert requests[0]["messages"][0] != requests[1]["messages"][0]
    assert [item.text for item in qq.calls] == [text]
    assert "复核未确认违规" in result.note
    assert text not in result.note


@pytest.mark.parametrize("category", ["sexual", "political_sensitive", "other"])
async def test_benign_technical_words_do_not_bypass_a_confirmed_violation(category):
    qq = QQSender()
    calls = []
    body = "VPN and GitHub tool news followed by actual prohibited content."

    def handle(request):
        assert qq.calls == []
        calls.append(request)
        return verdict(category)

    router = DeliveryRouter(settings(), qq)
    moderator = AIContentModerator(router.settings, transport=httpx.MockTransport(handle))
    router.qq_moderator = moderator
    try:
        result = await router.deliver(rule(), message(body), body)
    finally:
        await moderator.close()
    assert result.blocked and "已复核" in result.note
    assert len(calls) == 2
    assert [item.text for item in qq.calls] == [QQ_VIOLATION_NOTICE]
    assert qq.calls[0].media_paths == []


async def test_initial_safe_verdict_needs_no_confirmation_request():
    calls = []

    def handle(request):
        calls.append(request)
        return verdict()

    moderator = AIContentModerator(settings(), transport=httpx.MockTransport(handle))
    try:
        assert not (await moderator.check(SAFE_TECH_MESSAGES[0])).blocked
        assert not (await moderator.check(SAFE_TECH_MESSAGES[0])).blocked
    finally:
        await moderator.close()
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["http", "invalid", "timeout"])
async def test_confirmation_failure_falls_back_without_caching_provisional_block(failure):
    calls = []

    async def handle(request):
        calls.append(request)
        if len(calls) % 2:
            return verdict("other")
        if failure == "http":
            return httpx.Response(500)
        if failure == "timeout":
            await asyncio.sleep(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": "invalid"}}]})

    moderator = AIContentModerator(
        settings(qq_ai_moderation_timeout_seconds=0.03), transport=httpx.MockTransport(handle)
    )
    try:
        for _ in range(2):
            decision = await moderator.check(SAFE_TECH_MESSAGES[1])
            assert not decision.blocked and decision.unavailable
            assert not moderator._cache
    finally:
        await moderator.close()
    assert len(calls) == 4


async def test_enabling_confirmation_cannot_reuse_an_unconfirmed_cached_block():
    calls = []

    def handle(request):
        calls.append(request)
        return verdict("other") if len(calls) < 3 else verdict()

    config = settings(qq_ai_moderation_confirm_blocked=False)
    moderator = AIContentModerator(config, transport=httpx.MockTransport(handle))
    try:
        assert (await moderator.check("VPN product news")).blocked
        config.qq_ai_moderation_confirm_blocked = True
        assert not (await moderator.check("VPN product news")).blocked
    finally:
        await moderator.close()
    assert len(calls) == 3


@pytest.mark.parametrize("image_is_blocked", [True, False])
async def test_revoking_text_block_still_reviews_actual_image_pixels(tmp_path, image_is_blocked):
    path = tmp_path / "picture.png"
    with Image.new("RGB", (40, 30), "red") as pixels:
        pixels.save(path)
    qq = QQSender()
    requests = []

    def handle(request):
        assert qq.calls == []
        payload = json.loads(request.content)
        requests.append(payload)
        content = payload["messages"][1]["content"]
        if isinstance(content, str):
            return verdict("other") if len(requests) == 1 else verdict()
        assert content[1]["image_url"]["url"].startswith("data:image/")
        return verdict("sexual") if image_is_blocked else verdict()

    router = DeliveryRouter(settings(), qq)
    moderator = AIContentModerator(router.settings, transport=httpx.MockTransport(handle))
    router.qq_moderator = moderator
    text = SAFE_TECH_MESSAGES[2]
    try:
        result = await router.deliver(rule(), message(text, path), text)
    finally:
        await moderator.close()

    assert result.blocked is image_is_blocked
    if image_is_blocked:
        assert len(requests) == 4
        assert requests[2]["messages"][1] == requests[3]["messages"][1]
        assert [item.text for item in qq.calls] == [QQ_VIOLATION_NOTICE]
    else:
        assert len(requests) == 3
        assert "复核未确认违规" in result.note
        assert [item.text for item in qq.calls] == [text]
        assert qq.calls[0].media_paths == [path]


async def test_confirmation_cancellation_is_not_swallowed():
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 2:
            raise asyncio.CancelledError()
        return verdict("other")

    moderator = AIContentModerator(settings(), transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(asyncio.CancelledError):
            await moderator.check("VPN product news")
        assert not moderator._cache
    finally:
        await moderator.close()


async def test_explicit_local_keyword_still_blocks_before_ai():
    def unexpected(request):
        pytest.fail("explicit local keywords must be checked before AI")

    qq = QQSender()
    router = DeliveryRouter(settings(), qq)
    moderator = AIContentModerator(router.settings, transport=httpx.MockTransport(unexpected))
    router.qq_moderator = moderator
    try:
        result = await router.deliver(rule(qq_skip_keywords=["VPN"]),
                                      message(SAFE_TECH_MESSAGES[2]), SAFE_TECH_MESSAGES[2])
    finally:
        await moderator.close()
    assert result.blocked and "命中 QQ 屏蔽词" in result.note
    assert [item.text for item in qq.calls] == [QQ_VIOLATION_NOTICE]


async def test_audio_confirmation_failure_keeps_transcript_for_keyword_fallback(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is required for audio review")
    path = tmp_path / "recording.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 16000)
    qq = QQSender()
    transcript_reviews = []

    def handle(request):
        assert qq.calls == []
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "spoken blocked-term in the recording"})
        content = json.loads(request.content)["messages"][1]["content"]
        if "Audio transcript:" in content:
            transcript_reviews.append(content)
            return verdict("other") if len(transcript_reviews) == 1 else httpx.Response(500)
        return verdict()

    router = DeliveryRouter(settings(), qq)
    moderator = AIContentModerator(router.settings, transport=httpx.MockTransport(handle))
    router.qq_moderator = moderator
    source = message("ordinary product news", path)
    source.media_type = "audio"
    source.media_types = ["audio"]
    try:
        result = await router.deliver(rule(qq_skip_keywords=["blocked-term"]), source, source.text)
        assert all(not cached.blocked for _, cached in moderator._cache.values())
    finally:
        await moderator.close()
    assert transcript_reviews[0] == transcript_reviews[1]
    assert result.blocked and "命中 QQ 屏蔽词" in result.note
    assert [item.text for item in qq.calls] == [QQ_VIOLATION_NOTICE]


def test_confirmation_is_enabled_by_default_and_can_be_disabled():
    assert Settings(_env_file=None).qq_ai_moderation_confirm_blocked is True
    disabled = Settings(_env_file=None, qq_ai_moderation_confirm_blocked="false")
    assert disabled.qq_ai_moderation_confirm_blocked is False
