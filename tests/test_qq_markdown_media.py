from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import botpy.errors
import httpx
import pytest
from PIL import Image

from app.config import Settings
from app.qq_official.markdown_media import (
    MAX_IMAGE_BYTES,
    MarkdownImageUploader,
    MarkdownMediaUnavailable,
)
from app.qq_official.models import QQOutboundMessage
from app.qq_official.sender import QQOfficialSender
from app.storage.models import QQTargetType


def _image(path: Path, *, image_format: str = "PNG", size=(80, 40)) -> Path:
    with Image.new("RGB", size, "blue") as image:
        image.save(path, format=image_format)
    return path


def _mock_api(sender: QQOfficialSender) -> SimpleNamespace:
    state = SimpleNamespace(
        calls=[],
        sent=[],
        attempts=[],
        puts=[],
        deleted=[],
        prepares=0,
        prepare_error=None,
        deny_markdown=False,
        audit_images=False,
        fail_second=False,
        pause_second=False,
        second_started=asyncio.Event(),
    )

    def send(payload):
        state.attempts.append(payload)
        if payload.get("msg_type") == 2:
            if state.deny_markdown:
                raise botpy.errors.ServerError("不允许发送原生 markdown")
            if state.audit_images and "qqbot-media://" in payload["markdown"]["content"]:
                raise botpy.errors.ServerError("消息内容违规")
        state.sent.append(payload)
        return {"id": f"sent-{len(state.sent)}"}

    async def request(route, **kwargs):
        state.calls.append((route, kwargs.get("json")))
        if route.path.startswith("/v2/media-images"):
            assert route.url.startswith("https://api.bot.qq.com/")
        if route.path.endswith("/prepare"):
            state.prepares += 1
            if state.prepare_error:
                raise state.prepare_error
            if state.prepares == 2:
                state.second_started.set()
                if state.fail_second:
                    raise botpy.errors.ServerError("图片内容违规")
                if state.pause_second:
                    await asyncio.Event().wait()
            return {
                "upload_id": f"upload-{state.prepares}",
                "presigned_url": f"https://upload.example.com/image-{state.prepares}",
            }
        if route.path.endswith("/complete"):
            suffix = kwargs["json"]["upload_id"].removeprefix("upload-")
            return {
                "image_id": f"image-{suffix}",
                "url": f"qqbot-media://image/v1/token-{suffix}",
            }
        if route.method == "DELETE":
            state.deleted.append(route.path)
            return {}
        if route.path.endswith("/files"):
            return {"file_uuid": "uuid", "file_info": "info", "ttl": 300}
        if route.path in {
            "/v2/users/{openid}/messages", "/v2/groups/{group_openid}/messages"
        }:
            if "qqbot-media://" in str(kwargs["json"]):
                assert route.url.startswith("https://api.bot.qq.com/")
            return send(kwargs["json"])
        raise AssertionError(f"Unexpected API call: {route.method} {route.path}")

    async def post_group(*, group_openid, **payload):
        assert group_openid == "target"
        return send(payload)

    def put(request: httpx.Request) -> httpx.Response:
        state.puts.append(request)
        assert request.method == "PUT"
        assert "authorization" not in request.headers
        return httpx.Response(
            200, headers={"ETag": f'"{hashlib.md5(request.content).hexdigest()}"'}
        )

    sender._http_request = request
    sender.client.api.post_group_message = post_group
    sender.markdown_images._client = httpx.AsyncClient(transport=httpx.MockTransport(put))
    return state


@pytest.fixture
async def sender_api():
    sender = QQOfficialSender(Settings(qq_use_markdown=True))
    state = _mock_api(sender)
    try:
        yield sender, state
    finally:
        await sender.stop()


@pytest.mark.parametrize("target_type", [QQTargetType.GROUP, QQTargetType.C2C])
@pytest.mark.parametrize("count", [1, 2])
async def test_images_and_markdown_are_sent_in_one_message(
    sender_api, tmp_path, target_type, count
):
    sender, state = sender_api
    paths = [_image(tmp_path / f"image-{index}.png") for index in range(count)]
    body = (
        "# PK个人频道\n\n**账号：`xuer`**\n\n🎁 **会员活动**\n"
        "[活动入口](https://example.com/plans?code=123#subscribe)"
    )

    result = await sender.send(
        QQOutboundMessage(
            target_type=target_type, target_id="target", text=body, media_paths=paths
        )
    )

    assert len(result) == len(state.sent) == 1
    payload = state.sent[0]
    assert payload["msg_type"] == 2
    assert set(payload) == {"msg_type", "msg_seq", "markdown"}
    markdown = payload["markdown"]["content"]
    expected_images = [
        f"![图片 #80px #40px](qqbot-media://image/v1/token-{index + 1})"
        for index in range(count)
    ]
    assert markdown == "\n\n".join([*expected_images, body])
    assert [request.content for request in state.puts] == [path.read_bytes() for path in paths]
    assert len(state.deleted) == count
    assert all("/files" not in route.path for route, _ in state.calls)


@pytest.mark.parametrize("target_type", [QQTargetType.GROUP, QQTargetType.C2C])
async def test_unavailable_material_api_keeps_media_and_markdown(sender_api, tmp_path, target_type):
    sender, state = sender_api
    state.prepare_error = botpy.errors.ServerError("素材接口暂不可用")
    body = "# 标题\n\n[查看](https://example.com/details)"

    await sender.send(
        QQOutboundMessage(
            target_type=target_type,
            target_id="target",
            text=body,
            media_path=_image(tmp_path / "image.png"),
            media_caption="规则名称",
        )
    )

    assert [payload["msg_type"] for payload in state.sent] == [7, 2]
    assert state.sent[0]["content"] == "规则名称"
    assert state.sent[1]["markdown"]["content"] == body
    assert state.deleted == []


@pytest.mark.parametrize("target_type", [QQTargetType.GROUP, QQTargetType.C2C])
async def test_markdown_denial_falls_back_without_material_references(
    sender_api, tmp_path, target_type
):
    sender, state = sender_api
    state.deny_markdown = True
    body = "**正文** [查看](https://example.com/details)"

    await sender.send(
        QQOutboundMessage(
            target_type=target_type,
            target_id="target",
            text=body,
            media_path=_image(tmp_path / "image.png"),
        )
    )

    assert [payload["msg_type"] for payload in state.sent] == [7, 0]
    assert state.sent[1]["content"] == body
    assert "qqbot-media://" not in str(state.sent)
    assert state.deleted == ["/v2/media-images/image-1/delete"]


@pytest.mark.parametrize("target_type", [QQTargetType.GROUP, QQTargetType.C2C])
async def test_combined_message_audit_stops_without_resending(sender_api, tmp_path, target_type):
    sender, state = sender_api
    state.audit_images = True

    with pytest.raises(botpy.errors.ServerError, match="审核未通过"):
        await sender.send(
            QQOutboundMessage(
                target_type=target_type,
                target_id="target",
                text="original https://example.com/private",
                media_path=_image(tmp_path / "image.png"),
            )
        )

    assert len(state.attempts) == 1
    assert state.sent == []
    assert state.deleted == ["/v2/media-images/image-1/delete"]
    assert all("/files" not in route.path for route, _ in state.calls)


async def test_partial_album_audit_releases_materials_and_sends_nothing(sender_api, tmp_path):
    sender, state = sender_api
    state.fail_second = True

    with pytest.raises(botpy.errors.ServerError, match="审核未通过"):
        await sender.send(
            QQOutboundMessage(
                target_type="group",
                target_id="target",
                text="original",
                media_paths=[_image(tmp_path / f"{index}.png") for index in range(2)],
            )
        )

    assert state.sent == []
    assert state.deleted == ["/v2/media-images/image-1/delete"]


async def test_cancellation_releases_completed_materials(sender_api, tmp_path):
    sender, state = sender_api
    state.pause_second = True
    task = asyncio.create_task(
        sender.send(
            QQOutboundMessage(
                target_type="group",
                target_id="target",
                text="original",
                media_paths=[_image(tmp_path / f"{index}.png") for index in range(2)],
            )
        )
    )
    await asyncio.wait_for(state.second_started.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert state.sent == []
    assert state.deleted == ["/v2/media-images/image-1/delete"]


async def test_long_body_falls_back_without_truncating_image_reference(sender_api, tmp_path):
    sender, state = sender_api
    body = "a" * 3890

    await sender.send(
        QQOutboundMessage(
            target_type="group",
            target_id="target",
            text=body,
            media_path=_image(tmp_path / "image.png"),
        )
    )

    assert [payload["msg_type"] for payload in state.sent] == [7, 2]
    assert state.sent[1]["markdown"]["content"] == body
    assert state.deleted == ["/v2/media-images/image-1/delete"]


@pytest.mark.parametrize(
    "image_format,suffix,content_type",
    [
        ("PNG", ".png", "image/png"),
        ("JPEG", ".jpg", "image/jpeg"),
        ("GIF", ".gif", "image/gif"),
        ("WEBP", ".webp", "image/webp"),
    ],
)
async def test_material_upload_uses_actual_format_and_digests(
    sender_api, tmp_path, image_format, suffix, content_type
):
    sender, state = sender_api
    path = _image(tmp_path / "mislabeled.jpg", image_format=image_format)
    data = path.read_bytes()

    image = await sender.markdown_images.upload(path)

    assert (image.width, image.height) == (80, 40)
    prepared = state.calls[0][1]
    assert prepared == {
        "file_name": f"mislabeled{suffix}",
        "file_size": len(data),
        "content_type": content_type,
        "md5": hashlib.md5(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
    }
    assert state.puts[0].content == data
    assert state.puts[0].headers["Content-Type"] == content_type
    await sender.markdown_images.release(image.image_id)


@pytest.mark.parametrize("kind", ["oversized", "invalid"])
async def test_invalid_images_never_reach_material_api(tmp_path, kind):
    path = tmp_path / "image.png"
    if kind == "oversized":
        with path.open("wb") as output:
            output.seek(MAX_IMAGE_BYTES)
            output.write(b"x")
    else:
        path.write_bytes(b"not an image")

    async def unexpected_request(*args, **kwargs):
        pytest.fail("invalid images must not be uploaded")

    uploader = MarkdownImageUploader(unexpected_request, timeout=1)
    with pytest.raises((MarkdownMediaUnavailable, OSError)):
        await uploader.upload(path)


@pytest.mark.parametrize("status", [302, 403, 200])
async def test_failed_put_or_bad_checksum_never_completes_upload(sender_api, tmp_path, status):
    sender, state = sender_api
    await sender.markdown_images.aclose()

    def failed_put(request):
        return httpx.Response(
            status, headers={"ETag": "wrong-checksum", "Location": "https://other.example.com"}
        )

    sender.markdown_images._client = httpx.AsyncClient(transport=httpx.MockTransport(failed_put))

    with pytest.raises((MarkdownMediaUnavailable, httpx.HTTPStatusError)):
        await sender.markdown_images.upload(_image(tmp_path / "image.png"))

    assert [route.path for route, _ in state.calls] == ["/v2/media-images/prepare"]
