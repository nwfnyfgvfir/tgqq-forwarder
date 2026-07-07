from __future__ import annotations

from pathlib import Path
from types import MethodType

from app.config import Settings
from app.qq_official.models import QQOutboundMessage
from app.qq_official.sender import QQOfficialSender
from app.storage.models import QQTargetType


async def test_send_media_sequence_keeps_current_split_send_behavior() -> None:
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
        media_paths=[Path("one.jpg"), Path("two.jpg")],
        media_types=["photo", "photo"],
    )

    result = await sender._send_media_sequence(
        outbound,
        QQTargetType.GROUP,
        outbound.text,
        outbound.media_paths,
    )

    assert result == [{"text": "完整正文"}, {"text": "[继续发送媒体 2/2]"}]
    assert [text for _item, _target_type, text in calls] == ["完整正文", "[继续发送媒体 2/2]"]
    assert [call[0].media_path for call in calls] == [Path("one.jpg"), Path("two.jpg")]


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
