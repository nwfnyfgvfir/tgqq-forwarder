from __future__ import annotations

import botpy
import httpx
import pytest

from app.qq_official.client import ForwarderQQClient
from app.qq_official.media import (
    QQAttachmentDownloader,
    media_type_for,
    sanitize_path_segment,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def make_downloader(tmp_path, handler, **kwargs) -> QQAttachmentDownloader:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return QQAttachmentDownloader(
        tmp_path / "media",
        client=client,
        **kwargs,
    )


class Attachment:
    def __init__(
        self,
        url: str,
        content_type: str = "",
        filename: str | None = None,
        size: int | None = None,
    ) -> None:
        self.url = url
        self.content_type = content_type
        self.filename = filename
        self.size = size


async def test_downloads_image_and_reports_media_type(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/a.png"
        return httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})

    downloader = make_downloader(tmp_path, handler)

    saved = await downloader.download(
        [Attachment("https://cdn.example/a.png", "image/png")],
        chat_id="group-1",
        message_id="msg-1",
    )

    assert len(saved) == 1
    index, path, media_type, filename = saved[0]
    assert index == 0
    assert media_type == "photo"
    assert filename is None
    assert path.suffix == ".png"
    assert path.read_bytes() == PNG_BYTES
    # 落在 media/qq/<chat_id>/ 下，媒体清理任务能按 mtime 回收
    assert path.parent == tmp_path / "media" / "qq" / "group-1"
    await downloader.aclose()


async def test_filename_suffix_wins_for_documents(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"%PDF-1.4 fake",
            headers={"content-type": "application/octet-stream"},
        )

    downloader = make_downloader(tmp_path, handler)

    saved = await downloader.download(
        [Attachment("https://cdn.example/b", "application/octet-stream", "报表.pdf")],
        chat_id="group-1",
        message_id="msg-2",
    )

    assert saved[0][1].suffix == ".pdf"
    assert saved[0][2] == "document"
    # 原始文件名要保留下来，转发时用它还原，而不是本地缓存名
    assert saved[0][3] == "报表.pdf"
    await downloader.aclose()


async def test_oversize_attachment_is_skipped(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * 16,
            headers={"content-type": "video/mp4", "content-length": str(2 * 1024 * 1024)},
        )

    downloader = make_downloader(tmp_path, handler, max_media_mb=1)

    saved = await downloader.download(
        [Attachment("https://cdn.example/big.mp4", "video/mp4")],
        chat_id="group-1",
        message_id="msg-3",
    )

    assert saved == []
    await downloader.aclose()


async def test_http_error_is_skipped(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    downloader = make_downloader(tmp_path, handler)

    saved = await downloader.download(
        [Attachment("https://cdn.example/missing.png", "image/png")],
        chat_id="group-1",
        message_id="msg-4",
    )

    assert saved == []
    await downloader.aclose()


async def test_unsupported_scheme_is_rejected(tmp_path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=PNG_BYTES)

    downloader = make_downloader(tmp_path, handler)

    saved = await downloader.download(
        [Attachment("file:///etc/passwd", "image/png")],
        chat_id="group-1",
        message_id="msg-5",
    )

    assert saved == []
    assert calls == []
    await downloader.aclose()


async def test_disabled_downloader_does_nothing(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not download when disabled")

    downloader = make_downloader(tmp_path, handler, enabled=False)

    saved = await downloader.download(
        [Attachment("https://cdn.example/a.png", "image/png")],
        chat_id="group-1",
        message_id="msg-6",
    )

    assert saved == []
    await downloader.aclose()


@pytest.mark.parametrize(
    ("content_type", "suffix", "expected"),
    [
        ("image/jpeg", ".jpg", "photo"),
        ("video/mp4", ".mp4", "video"),
        ("audio/ogg", ".ogg", "voice"),
        ("audio/mpeg", ".mp3", "audio"),
        ("application/pdf", ".pdf", "document"),
        ("", ".jpg", "photo"),
        ("", ".mp4", "video"),
    ],
)
def test_media_type_mapping(content_type: str, suffix: str, expected: str) -> None:
    assert media_type_for(content_type, suffix) == expected


def test_sanitize_path_segment() -> None:
    assert sanitize_path_segment("group-openid") == "group-openid"
    assert sanitize_path_segment("../../etc") == "etc"
    assert sanitize_path_segment("") == "unknown"


def group_message(attachments: list[dict]) -> botpy.message.GroupMessage:
    return botpy.message.GroupMessage(
        None,
        "event-1",
        {
            "id": "msg-1",
            "content": "看图",
            "author": {"member_openid": "member-1"},
            "group_openid": "group-1",
            "mentions": [],
            "attachments": attachments,
        },
    )


async def test_client_uses_downloaded_files_instead_of_notes(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})

    downloader = make_downloader(tmp_path, handler)
    captured: list = []

    async def on_message(message) -> None:
        captured.append(message)

    client = ForwarderQQClient(
        intents=botpy.Intents(public_messages=True),
        bot_log=False,
        on_message=on_message,
        attachment_downloader=downloader,
    )

    await client.on_group_message_create(
        group_message(
            [
                {
                    "content_type": "image/png",
                    "url": "https://cdn.example/a.png",
                    "filename": "a.png",
                }
            ]
        )
    )

    message = captured[0]
    assert message.media_type == "photo"
    assert len(message.media_paths) == 1
    assert message.media_paths[0].exists()
    assert message.media_filenames == ["a.png"]
    # 下载成功就不该再留 `[图片] url` 文本
    assert "https://cdn.example/a.png" not in message.text
    assert message.text == "看图"
    await downloader.aclose()


async def test_client_keeps_note_for_failed_attachment(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("ok.png"):
            return httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})
        return httpx.Response(410)

    downloader = make_downloader(tmp_path, handler)
    captured: list = []

    async def on_message(message) -> None:
        captured.append(message)

    client = ForwarderQQClient(
        intents=botpy.Intents(public_messages=True),
        bot_log=False,
        on_message=on_message,
        attachment_downloader=downloader,
    )

    await client.on_group_message_create(
        group_message(
            [
                {"content_type": "image/png", "url": "https://cdn.example/ok.png"},
                {"content_type": "image/png", "url": "https://cdn.example/gone.png"},
            ]
        )
    )

    message = captured[0]
    assert len(message.media_paths) == 1
    assert "https://cdn.example/gone.png" in message.text
    assert "https://cdn.example/ok.png" not in message.text
    await downloader.aclose()
