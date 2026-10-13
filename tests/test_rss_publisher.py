from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.rss.errors import RssError
from app.rss.formatting import utf16_units
from app.rss.publisher import RssPublisher


class FakeBot:
    def __init__(self) -> None:
        self.initializations = 0
        self.shutdowns = 0
        self.calls = []
        self.times = []

    async def initialize(self):
        self.initializations += 1

    async def shutdown(self):
        self.shutdowns += 1

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        self.times.append(asyncio.get_running_loop().time())
        return SimpleNamespace(message_id=42, chat=SimpleNamespace(id=-100987))


async def test_lazy_bot_plain_text_no_preview_and_actual_chat() -> None:
    settings = Settings(_env_file=None, rss_send_interval_seconds=0.01)
    bot = FakeBot()
    publisher = RssPublisher(settings, sender=bot)
    assert bot.initializations == 0 and publisher.configured
    await publisher.stop()
    assert bot.shutdowns == 0
    result = await publisher.send(
        "@channel_name", "<b>plain</b> *not markdown*", "https://example.com/a"
    )
    assert result.message_id == 42 and result.chat_id == -100987
    sent = bot.calls[0]
    assert sent["parse_mode"] is None
    assert sent["link_preview_options"].is_disabled
    assert sent["chat_id"] == "@channel_name"
    assert sent["text"].startswith("<b>plain</b> *not markdown*")
    assert sent["text"].endswith("https://example.com/a")
    await publisher.send("-100123", "text")
    assert bot.calls[-1]["chat_id"] == -100123
    assert bot.initializations == 1
    await publisher.stop()
    await publisher.stop()
    assert bot.shutdowns == 1


async def test_shared_bot_rate_limit_and_defer() -> None:
    settings = Settings(_env_file=None, rss_bot_token="123:offline", rss_send_interval_seconds=0.02)
    bot = FakeBot()
    one = RssPublisher(settings, sender=bot)
    two = RssPublisher(settings, sender=bot)
    await asyncio.gather(one.send("-100", "one"), two.send("-100", "two"))
    assert bot.times[1] - bot.times[0] >= 0.018
    one.defer(0.03)
    before = asyncio.get_running_loop().time()
    await two.send("-100", "three")
    assert bot.times[-1] - before >= 0.028
    await one.stop()
    await two.stop()


async def test_sender_message_utf16_limit_keeps_complete_link() -> None:
    bot = FakeBot()
    publisher = RssPublisher(Settings(_env_file=None), sender=bot)
    link = "https://example.com/a"
    await publisher.send("-100123", chr(0x1F680) * 5000, link)
    assert utf16_units(bot.calls[0]["text"]) <= 4096
    assert bot.calls[0]["text"].endswith(link)
    await publisher.stop()


async def test_bot_proxy_applied_only_when_configured(monkeypatch) -> None:
    captured: dict = {}

    class FakeRequest:
        def __init__(self, **kwargs) -> None:
            captured.clear()
            captured.update(kwargs)

    class FakeClient:
        def __init__(self, token=None, request=None) -> None:
            self.token = token
            self.request = request

        async def initialize(self):
            return None

        async def shutdown(self):
            return None

        async def send_message(self, **kwargs):
            return SimpleNamespace(message_id=1, chat=SimpleNamespace(id=-100))

    monkeypatch.setattr("app.rss.publisher.HTTPXRequest", FakeRequest)
    monkeypatch.setattr("app.rss.publisher.Bot", FakeClient)

    proxied = RssPublisher(
        Settings(
            _env_file=None,
            rss_bot_token="1:x",
            telegram_bot_proxy="socks5://127.0.0.1:1080",
        )
    )
    await proxied.send("-100", "text")
    assert captured["proxy"] == "socks5://127.0.0.1:1080"
    assert "httpx_kwargs" not in captured

    await proxied.stop()

    plain = RssPublisher(Settings(_env_file=None, rss_bot_token="1:x"))
    await plain.send("-100", "text")
    assert "proxy" not in captured
    assert "httpx_kwargs" not in captured
    await plain.stop()


async def test_missing_token_and_admin_fallback() -> None:
    publisher = RssPublisher(Settings(_env_file=None, tg_admin_bot_token=None, rss_bot_token=None))
    assert not publisher.configured
    with pytest.raises(RssError) as error:
        await publisher.send("-100123", "text")
    assert error.value.code == "rss_publisher_missing"
    assert RssPublisher(Settings(_env_file=None, tg_admin_bot_token="admin"))._token == "admin"
    assert (
        RssPublisher(
            Settings(_env_file=None, tg_admin_bot_token="admin", rss_bot_token="rss")
        )._token
        == "rss"
    )
