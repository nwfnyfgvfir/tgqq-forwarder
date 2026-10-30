from __future__ import annotations

import time
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any

import botpy
import pytest
from botpy.http import Route

from app.config import Settings
from app.qq_official.models import QQOutboundMessage
from app.qq_official.sender import QQOfficialSender
from app.storage.models import QQTargetType


async def test_send_media_sequence_sends_text_first_then_captioned_media() -> None:
    sender = QQOfficialSender(Settings())
    text_calls = []
    media_calls = []

    async def fake_send_text(self, item, target_type, text):
        text_calls.append((item, target_type, text))
        return {"kind": "text", "text": text}

    async def fake_send_media(self, item, target_type, text):
        media_calls.append((item, target_type, text))
        return {"kind": "media", "text": text}

    sender._send_text = MethodType(fake_send_text, sender)
    sender._send_media = MethodType(fake_send_media, sender)
    outbound = QQOutboundMessage(
        target_type="group",
        target_id="group-openid",
        text="完整正文",
        media_paths=[Path("one.jpg"), Path("two.jpg")],
        media_types=["photo", "photo"],
        media_caption="规则名称",
    )

    result = await sender._send_media_sequence(
        outbound,
        QQTargetType.GROUP,
        outbound.text,
        outbound.media_paths,
    )

    assert result == [
        {"kind": "text", "text": "完整正文"},
        {"kind": "media", "text": "规则名称"},
        {"kind": "media", "text": "规则名称"},
    ]
    assert [text for _item, _target_type, text in text_calls] == ["完整正文"]
    assert [text for _item, _target_type, text in media_calls] == ["规则名称", "规则名称"]
    assert [call[0].media_path for call in media_calls] == [Path("one.jpg"), Path("two.jpg")]


async def test_send_single_media_keeps_full_text_on_media() -> None:
    sender = QQOfficialSender(Settings())
    calls = []

    async def fake_send_media(self, item, target_type, text):
        calls.append((item, target_type, text))
        return {"text": text}

    sender._send_media = MethodType(fake_send_media, sender)
    outbound = QQOutboundMessage(
        target_type="group",
        target_id="group-openid",
        text="完整正文",
        media_paths=[Path("one.jpg")],
        media_types=["photo"],
        media_caption="规则名称",
    )

    result = await sender._send_media_sequence(
        outbound,
        QQTargetType.GROUP,
        outbound.text,
        outbound.media_paths,
    )

    assert result == [{"text": "完整正文"}]
    assert [text for _item, _target_type, text in calls] == ["完整正文"]
    assert calls[0][0].media_path == Path("one.jpg")


async def test_group_proactive_payload_omits_msg_id_even_when_cached() -> None:
    sender = QQOfficialSender(Settings(qq_use_markdown=False))
    sender.client.remember_session("group-openid", "group", "stale-msg-id")

    payload = sender._group_proactive_payload("hello")

    assert payload == {"content": "hello", "msg_type": 0, "msg_seq": payload["msg_seq"]}
    assert "msg_id" not in payload


async def test_post_group_message_retries_without_msg_id_on_resource_not_found() -> None:
    sender = QQOfficialSender(Settings())
    calls: list[dict[str, Any]] = []

    async def fake_post_group_message(*, group_openid: str, **payload: Any) -> dict[str, str]:
        calls.append({"group_openid": group_openid, **payload})
        if "msg_id" in payload:
            raise botpy.errors.ServerError("请求的资源不存在(用户/群已注销)")
        return {"id": "sent-1"}

    sender.client.api.post_group_message = fake_post_group_message  # type: ignore[method-assign]

    result = await sender._post_group_message(
        "group-openid",
        {"content": "hello", "msg_type": 0, "msg_seq": 1, "msg_id": "stale-msg-id"},
    )

    assert result == {"id": "sent-1"}
    assert len(calls) == 2
    assert "msg_id" in calls[0]
    assert "msg_id" not in calls[1]


async def test_group_media_payload_removes_markdown_and_uses_rich_media_type() -> None:
    sender = QQOfficialSender(Settings(qq_use_markdown=True))
    captured = {}

    async def fake_upload(
        self, media_path, file_type, *, openid=None, group_openid=None, file_name=None
    ):
        captured["upload"] = {
            "media_path": media_path,
            "file_type": file_type,
            "openid": openid,
            "group_openid": group_openid,
            "file_name": file_name,
        }
        return "MEDIA"

    async def fake_post_group(self, group_openid, payload):
        captured["post"] = {"group_openid": group_openid, "payload": payload}
        return payload

    sender._upload_group_or_c2c_media = MethodType(fake_upload, sender)
    sender._post_group_message = MethodType(fake_post_group, sender)
    outbound = QQOutboundMessage(
        target_type="group",
        target_id="group-openid",
        text="正文",
        media_path=Path("photo.jpg"),
        media_type="photo",
    )

    payload = await sender._send_media(outbound, QQTargetType.GROUP, outbound.text)

    assert payload["msg_type"] == 7
    assert payload["media"] == "MEDIA"
    assert payload["content"] == "正文"
    assert "markdown" not in payload
    assert captured["post"]["group_openid"] == "group-openid"
    assert captured["upload"]["group_openid"] == "group-openid"


def _install_token(sender: QQOfficialSender, *, expires_in: float = 7200.0):
    """装一个假 access_token，并记录刷新次数。"""
    state = SimpleNamespace(refreshes=0)
    token = SimpleNamespace(
        access_token="old-token",
        expires_in=time.time() + expires_in,
    )

    async def update_access_token() -> None:
        state.refreshes += 1
        token.access_token = f"new-token-{state.refreshes}"
        token.expires_in = time.time() + 7200.0

    token.update_access_token = update_access_token
    sender._http._token = token
    return token, state


async def test_auth_error_refreshes_token_and_retries_once() -> None:
    sender = QQOfficialSender(Settings())
    _, state = _install_token(sender)
    calls: list[Any] = []

    async def fake_request(route, *args: Any, **kwargs: Any):
        calls.append(route)
        if len(calls) == 1:
            raise botpy.errors.AuthenticationFailedError("AccessToken无效或过期")
        return {"id": "sent"}

    sender._http_request = fake_request

    result = await sender._http.request(
        Route("POST", "/v2/groups/{group_openid}/messages", group_openid="g")
    )

    assert result == {"id": "sent"}
    assert len(calls) == 2
    assert state.refreshes == 1


async def test_auth_error_retries_only_once() -> None:
    sender = QQOfficialSender(Settings())
    _, state = _install_token(sender)
    calls: list[Any] = []

    async def fake_request(route, *args: Any, **kwargs: Any):
        calls.append(route)
        raise botpy.errors.AuthenticationFailedError("AccessToken无效或过期")

    sender._http_request = fake_request

    with pytest.raises(botpy.errors.AuthenticationFailedError):
        await sender._http.request(
            Route("POST", "/v2/groups/{group_openid}/messages", group_openid="g")
        )

    assert len(calls) == 2
    assert state.refreshes == 1


async def test_expiring_token_is_refreshed_before_request() -> None:
    sender = QQOfficialSender(Settings())
    _, state = _install_token(sender, expires_in=5.0)
    calls: list[Any] = []

    async def fake_request(route, *args: Any, **kwargs: Any):
        calls.append(route)
        return {"ok": True}

    sender._http_request = fake_request

    await sender._http.request(Route("GET", "/users/@me"))

    assert state.refreshes == 1
    assert len(calls) == 1


async def test_valid_token_is_not_refreshed() -> None:
    sender = QQOfficialSender(Settings())
    _, state = _install_token(sender, expires_in=3600.0)

    async def fake_request(route, *args: Any, **kwargs: Any):
        return {"ok": True}

    sender._http_request = fake_request

    await sender._http.request(Route("GET", "/users/@me"))

    assert state.refreshes == 0


async def test_group_media_upload_failure_falls_back_to_text(tmp_path) -> None:
    sender = QQOfficialSender(Settings())
    media = tmp_path / "cache_173_bin"
    media.write_bytes(b"pdf")
    sent: list[str] = []

    async def fake_upload(*_args: Any, **_kwargs: Any):
        raise botpy.errors.ServerError("文件类型暂不开放")

    async def fake_send_text(self, outbound, target_type, text):
        sent.append(text)
        return {"kind": "text", "text": text}

    sender._upload_group_or_c2c_media = fake_upload  # type: ignore[method-assign]
    sender._send_text = MethodType(fake_send_text, sender)
    outbound = QQOutboundMessage(
        target_type="group",
        target_id="group-openid",
        text="正文",
        media_path=media,
        media_type="document",
        media_filenames=["季度报表.pdf"],
    )

    result = await sender._send_media(outbound, QQTargetType.GROUP, "正文")

    assert result == {"kind": "text", "text": "正文\n[文件] 季度报表.pdf"}
    assert sent == ["正文\n[文件] 季度报表.pdf"]


def test_qq_media_type_keeps_audio_as_file(tmp_path) -> None:
    from app.qq_official.sender import FILE_FILE_TYPE, VOICE_FILE_TYPE

    mp3 = tmp_path / "song.mp3"
    silk = tmp_path / "voice.silk"
    mp3.write_bytes(b"")
    silk.write_bytes(b"")

    # QQ 语音只认 silk/amr，mp3 按文件发，避免变成无法播放的语音气泡。
    assert QQOfficialSender._qq_media_type("audio", mp3) == FILE_FILE_TYPE
    assert QQOfficialSender._qq_media_type("voice", silk) == VOICE_FILE_TYPE


async def test_upload_payload_includes_file_name(tmp_path) -> None:
    sender = QQOfficialSender(Settings())
    media = tmp_path / "cache_173_bin"
    media.write_bytes(b"pdf-bytes")
    captured: dict[str, Any] = {}

    async def fake_request(route, **kwargs):
        captured["path"] = route.path
        captured["json"] = kwargs["json"]
        return {"file_uuid": "uuid-1", "file_info": "info-1", "ttl": 0}

    sender.client.api._http.request = fake_request  # type: ignore[method-assign]

    await sender._upload_group_or_c2c_media(
        media,
        4,
        group_openid="group-openid",
        file_name="季度报表.pdf",
    )

    assert captured["json"]["file_name"] == "季度报表.pdf"
    assert captured["json"]["file_type"] == 4
    assert captured["json"]["srv_send_msg"] is False


def test_upload_file_name_falls_back_to_local_name(tmp_path) -> None:
    media = tmp_path / "cache_173_bin"

    assert QQOfficialSender._upload_file_name(None, media) == "cache_173_bin"
    assert QQOfficialSender._upload_file_name("", media) == "cache_173_bin"
    # 没有扩展名时用本地后缀补齐，QQ 才能识别格式
    assert QQOfficialSender._upload_file_name("季度报表", media.with_suffix(".pdf")) == (
        "季度报表.pdf"
    )
    # 路径分隔符要被去掉
    assert QQOfficialSender._upload_file_name("../a/b.pdf", media) == "b.pdf"


def test_qq_media_type_maps_only_supported_formats(tmp_path) -> None:
    from app.qq_official.sender import (
        FILE_FILE_TYPE,
        IMAGE_FILE_TYPE,
        VIDEO_FILE_TYPE,
    )

    def kind(name: str, media_type: str | None = None) -> int:
        path = tmp_path / name
        path.write_bytes(b"")
        return QQOfficialSender._qq_media_type(media_type, path)

    # QQ 原生支持的三类
    assert kind("a.jpg") == IMAGE_FILE_TYPE
    assert kind("a.png") == IMAGE_FILE_TYPE
    assert kind("a.mp4") == VIDEO_FILE_TYPE
    assert kind("a.silk") == 3
    # webp / gif / mov 等 QQ 不认，按文件发（带 file_name 后客户端能正确显示）
    assert kind("a.webp") == FILE_FILE_TYPE
    assert kind("a.gif") == FILE_FILE_TYPE
    assert kind("a.mov") == FILE_FILE_TYPE
    assert kind("a.mp3") == FILE_FILE_TYPE
    # 无扩展名时退回来源声明的类型
    assert kind("noext", "photo") == IMAGE_FILE_TYPE
    assert kind("noext", "video") == VIDEO_FILE_TYPE
    assert kind("noext", "document") == FILE_FILE_TYPE


def test_retry_predicate_skips_audit_errors() -> None:
    from app.qq_official.sender import _is_retryable_qq_error

    # 内容违规是永久失败：重试只会让队列白等 2/4/8/16 秒。
    assert _is_retryable_qq_error(botpy.errors.ServerError("消息内容违规")) is False
    assert _is_retryable_qq_error(botpy.errors.ServerError("内容审核不通过")) is False
    assert _is_retryable_qq_error(botpy.errors.ServerError("服务繁忙，请稍后重试")) is True
    assert _is_retryable_qq_error(OSError("connection reset")) is True
    assert _is_retryable_qq_error(ValueError("unexpected")) is False


async def test_group_audit_error_is_not_retried_and_reports_reason() -> None:
    sender = QQOfficialSender(Settings())
    calls = []

    async def fake_once(self, group_openid, payload):
        calls.append(group_openid)
        raise botpy.errors.ServerError("消息内容违规")

    sender._post_group_message_once = MethodType(fake_once, sender)
    with pytest.raises(botpy.errors.ServerError):
        await sender._post_group_message("group-openid", {"content": "x", "msg_type": 0})
    assert calls == ["group-openid"]

    # 走完整 send() 时错误文案要能直接看懂。
    async def fake_send_text(self, item, target_type, text):
        raise botpy.errors.ServerError("消息内容违规")

    sender._send_text = MethodType(fake_send_text, sender)
    outbound = QQOutboundMessage(target_type="group", target_id="g", text="正文")
    with pytest.raises(botpy.errors.ServerError) as send_error:
        await sender.send(outbound)
    message = str(send_error.value)
    assert "审核未通过" in message and "停止重试" in message
