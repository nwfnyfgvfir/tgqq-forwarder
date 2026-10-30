from __future__ import annotations

import asyncio

import pytest

from app.channels.router import DeliveryError, DeliveryRouter
from app.config import Settings
from app.rules.models import TelegramForwardMessage
from app.storage.models import ForwardRule


def make_settings(**overrides) -> Settings:
    data = {"_env_file": None, "qq_send_timeout_seconds": 1.0, "weixin_send_timeout_seconds": 1.0}
    data.update(overrides)
    return Settings(**data)


def make_message() -> TelegramForwardMessage:
    return TelegramForwardMessage(
        message_id=1,
        chat_id=-100,
        chat_title="news",
        chat_type="channel",
        sender_id=42,
        sender_username="s",
        sender_display_name="S",
        sender_is_bot=False,
        text="hello",
        media_type=None,
        media_path=None,
        date=None,
        account_id="main",
    )


class FakeQQSender:
    def __init__(self) -> None:
        self.calls: list = []
        self.hang = False

    async def send(self, outbound) -> dict:
        self.calls.append(outbound)
        if self.hang:
            await asyncio.Event().wait()
        return {"ok": True}


class FakeWeixinService:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list = []
        self.error: Exception | None = None

    async def send(self, outbound) -> None:
        self.calls.append(outbound)
        if self.error is not None:
            raise self.error


def make_rule(**overrides) -> ForwardRule:
    data = {
        "name": "rule",
        "qq_target_type": "group",
        "qq_target_id": "group-openid",
        "message_template": "{text}",
    }
    data.update(overrides)
    return ForwardRule(**data)


async def test_qq_target_uses_qq_sender() -> None:
    qq = FakeQQSender()
    weixin = FakeWeixinService()
    router = DeliveryRouter(make_settings(), qq, weixin)  # type: ignore[arg-type]

    await router.deliver(make_rule(), make_message(), "hello")

    assert len(qq.calls) == 1
    assert qq.calls[0].target_id == "group-openid"
    assert qq.calls[0].text == "hello"
    assert weixin.calls == []


async def test_weixin_target_uses_weixin_service() -> None:
    qq = FakeQQSender()
    weixin = FakeWeixinService()
    router = DeliveryRouter(make_settings(), qq, weixin)  # type: ignore[arg-type]
    rule = make_rule(target_platform="weixin", qq_target_type="friend", qq_target_id="wxid_1")

    await router.deliver(rule, make_message(), "hello")

    assert qq.calls == []
    assert len(weixin.calls) == 1
    assert weixin.calls[0].target_user_id == "wxid_1"
    assert weixin.calls[0].text == "hello"


async def test_weixin_target_requires_enabled_channel() -> None:
    router = DeliveryRouter(
        make_settings(),
        FakeQQSender(),  # type: ignore[arg-type]
        FakeWeixinService(enabled=False),  # type: ignore[arg-type]
    )
    rule = make_rule(target_platform="weixin", qq_target_id="wxid_1")

    with pytest.raises(DeliveryError):
        await router.deliver(rule, make_message(), "hello")


async def test_unknown_target_platform_is_rejected() -> None:
    router = DeliveryRouter(
        make_settings(),
        FakeQQSender(),  # type: ignore[arg-type]
        FakeWeixinService(),  # type: ignore[arg-type]
    )
    rule = make_rule(target_platform="discord")

    with pytest.raises(DeliveryError):
        await router.deliver(rule, make_message(), "hello")


async def test_timeout_is_reported_as_delivery_error() -> None:
    qq = FakeQQSender()
    qq.hang = True
    router = DeliveryRouter(
        make_settings(qq_send_timeout_seconds=0.01),
        qq,  # type: ignore[arg-type]
        FakeWeixinService(),  # type: ignore[arg-type]
    )

    with pytest.raises(DeliveryError) as excinfo:
        await router.deliver(make_rule(), make_message(), "hello")
    assert "timed out" in str(excinfo.value)


async def test_weixin_error_is_wrapped() -> None:
    from app.weixin.models import WeixinError

    weixin = FakeWeixinService()
    weixin.error = WeixinError("缺少 context_token")
    router = DeliveryRouter(
        make_settings(),
        FakeQQSender(),  # type: ignore[arg-type]
        weixin,  # type: ignore[arg-type]
    )
    rule = make_rule(target_platform="weixin", qq_target_id="wxid_1")

    with pytest.raises(DeliveryError) as excinfo:
        await router.deliver(rule, make_message(), "hello")
    assert "context_token" in str(excinfo.value)


def test_target_platforms_lists_weixin_only_when_enabled() -> None:
    qq = FakeQQSender()
    assert DeliveryRouter(make_settings(), qq, None).target_platforms == ["qq"]  # type: ignore[arg-type]
    assert DeliveryRouter(
        make_settings(),
        qq,  # type: ignore[arg-type]
        FakeWeixinService(),  # type: ignore[arg-type]
    ).target_platforms == ["qq", "weixin"]


class AuditFailingQQSender(FakeQQSender):
    """前 fail_times 次抛审核错误，之后正常。"""

    def __init__(self, error: Exception, *, fail_times: int = 1) -> None:
        super().__init__()
        self.error = error
        self.fail_times = fail_times

    async def send(self, outbound) -> dict:
        self.calls.append(outbound)
        if len(self.calls) <= self.fail_times:
            raise self.error
        return {"ok": True}


async def test_qq_audit_error_falls_back_to_source_link() -> None:
    import botpy.errors

    qq = AuditFailingQQSender(botpy.errors.ServerError("消息内容违规"))
    router = DeliveryRouter(make_settings(), qq, FakeWeixinService())  # type: ignore[arg-type]
    message = make_message()
    message.webpage_url = "https://x.com/NFT_Chen/status/2116054898093764785"

    result = await router.deliver(make_rule(), message, "含敏感词的正文")

    # 第一次发原文被拦，第二次只发链接；正文不会再出现。
    assert [call.text for call in qq.calls] == [
        "含敏感词的正文",
        "https://x.com/NFT_Chen/status/2116054898093764785",
    ]
    assert result.sent_text == "https://x.com/NFT_Chen/status/2116054898093764785"
    assert result.note and "降级" in result.note


async def test_qq_audit_fallback_uses_first_body_link_without_webpage() -> None:
    import botpy.errors

    from app.rules.models import TelegramLink

    qq = AuditFailingQQSender(botpy.errors.ServerError("内容审核不通过"))
    router = DeliveryRouter(make_settings(), qq, FakeWeixinService())  # type: ignore[arg-type]
    message = make_message()
    message.links = [TelegramLink(text="原文", url="https://example.com/a")]

    result = await router.deliver(make_rule(), message, "正文")

    assert result.sent_text == "https://example.com/a"


async def test_qq_audit_fallback_can_be_disabled() -> None:
    import botpy.errors

    qq = AuditFailingQQSender(botpy.errors.ServerError("消息内容违规"))
    router = DeliveryRouter(
        make_settings(qq_audit_link_fallback=False), qq, FakeWeixinService()
    )  # type: ignore[arg-type]
    message = make_message()
    message.webpage_url = "https://example.com/a"

    with pytest.raises(botpy.errors.ServerError):
        await router.deliver(make_rule(), message, "正文")
    assert len(qq.calls) == 1


async def test_qq_audit_error_without_any_link_still_fails() -> None:
    import botpy.errors

    qq = AuditFailingQQSender(botpy.errors.ServerError("消息内容违规"))
    router = DeliveryRouter(make_settings(), qq, FakeWeixinService())  # type: ignore[arg-type]

    with pytest.raises(botpy.errors.ServerError):
        await router.deliver(make_rule(), make_message(), "正文")
    assert len(qq.calls) == 1
