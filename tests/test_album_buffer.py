from __future__ import annotations

from pathlib import Path

from app.rules.models import TelegramForwardMessage
from app.telegram_user.album_buffer import TelegramAlbumBuffer


def make_message(**kwargs) -> TelegramForwardMessage:
    data = {
        "message_id": 1,
        "chat_id": -100123,
        "chat_title": "news",
        "chat_type": "channel",
        "sender_id": 42,
        "sender_username": "sender",
        "sender_display_name": "Sender",
        "sender_is_bot": False,
        "text": "",
        "media_type": None,
        "media_path": None,
        "date": None,
        "grouped_id": 99,
    }
    data.update(kwargs)
    return TelegramForwardMessage(**data)


def test_album_merge_preserves_first_webpage_preview_metadata() -> None:
    messages = [
        make_message(
            message_id=1,
            text="标题",
            media_path=Path("one.jpg"),
            media_type="photo",
            media_paths=[Path("one.jpg")],
            media_types=["photo"],
            webpage_title="预览标题",
            webpage_description="预览摘要",
            webpage_url="https://example.com/one",
        ),
        make_message(
            message_id=2,
            text="正文",
            media_path=Path("two.jpg"),
            media_type="photo",
            media_paths=[Path("two.jpg")],
            media_types=["photo"],
            webpage_title="第二个预览标题",
            webpage_description="第二个预览摘要",
            webpage_url="https://example.com/two",
        ),
    ]

    merged = TelegramAlbumBuffer._merge(messages)

    assert merged.text == "标题\n正文"
    assert merged.media_path == Path("one.jpg")
    assert merged.media_type == "photo"
    assert merged.media_paths == [Path("one.jpg"), Path("two.jpg")]
    assert merged.media_types == ["photo", "photo"]
    assert merged.webpage_title == "预览标题"
    assert merged.webpage_description == "预览摘要"
    assert merged.webpage_url == "https://example.com/one"
    assert merged.raw_url == "https://example.com/one"
