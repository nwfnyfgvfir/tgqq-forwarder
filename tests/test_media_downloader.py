from __future__ import annotations

from pathlib import Path

from telethon.tl.types import MessageMediaWebPage

from app.telegram_user.media_downloader import TelegramMediaDownloader, is_link_preview_media


class FakeFile:
    size = 1


class FakeMessage:
    file = FakeFile()
    photo = None
    video = None
    voice = None
    audio = None
    document = None
    web_preview = None

    def __init__(self, *, media: object, downloaded: str | None = None) -> None:
        self.media = media
        self.downloaded = downloaded or "/tmp/downloaded.jpg"
        self.download_called = False

    async def download_media(self, *, file: str) -> str:
        self.download_called = True
        return str(Path(file) / Path(self.downloaded).name)


class FakePhotoMessage(FakeMessage):
    photo = object()


class FakeEvent:
    chat_id = -100123

    def __init__(self, message: FakeMessage) -> None:
        self.message = message


def webpage_media() -> MessageMediaWebPage:
    return MessageMediaWebPage(webpage=None)


async def test_link_preview_media_is_skipped_by_default(tmp_path: Path) -> None:
    message = FakeMessage(media=webpage_media())
    downloader = TelegramMediaDownloader(tmp_path)

    downloaded = await downloader.download(FakeEvent(message))

    assert downloaded is None
    assert not message.download_called


async def test_link_preview_media_can_be_downloaded(tmp_path: Path) -> None:
    message = FakeMessage(media=webpage_media())
    downloader = TelegramMediaDownloader(tmp_path, download_link_preview_media=True)

    downloaded = await downloader.download(FakeEvent(message))

    assert downloaded == tmp_path / str(FakeEvent.chat_id) / "downloaded.jpg"
    assert message.download_called


async def test_real_photo_media_downloads_by_default(tmp_path: Path) -> None:
    message = FakePhotoMessage(media=object())
    downloader = TelegramMediaDownloader(tmp_path)

    downloaded = await downloader.download(FakeEvent(message))

    assert downloaded == tmp_path / str(FakeEvent.chat_id) / "downloaded.jpg"
    assert message.download_called


async def test_media_download_uses_account_bucket(tmp_path: Path) -> None:
    message = FakePhotoMessage(media=object())
    downloader = TelegramMediaDownloader(tmp_path, account_id="main")

    downloaded = await downloader.download(FakeEvent(message))

    assert downloaded == tmp_path / "main" / str(FakeEvent.chat_id) / "downloaded.jpg"
    assert message.download_called


def test_is_link_preview_media_ignores_real_media_with_web_preview() -> None:
    message = FakePhotoMessage(media=object())
    message.web_preview = object()

    assert not is_link_preview_media(message)
