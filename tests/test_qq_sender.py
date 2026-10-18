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

    async def fake_upload(self, media_path, file_type, *, openid=None, group_openid=None):
        captured["upload"] = {
            "media_path": media_path,
            "file_type": file_type,
            "openid": openid,
            "group_openid": group_openid,
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
