from __future__ import annotations

from dataclasses import dataclass

from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

from app.telegram_user.event_parser import _extract_links, _media_type
from app.telegram_user.media_downloader import MessageMediaWebPage


@dataclass
class FakeEvent:
    message: object


class FakeMessage:
    media = None
    photo = None
    video = None
    voice = None
    audio = None
    document = None
    buttons = None
    reply_markup = None

    def __init__(self, entities_text=None, *, buttons=None, reply_markup=None, media=None) -> None:
        self._entities_text = entities_text or []
        self.buttons = buttons
        self.reply_markup = reply_markup
        self.media = media

    def get_entities_text(self):
        return self._entities_text


@dataclass
class FakeButton:
    text: str
    url: str | None = None


@dataclass
class FakeButtonRow:
    buttons: list[FakeButton]


@dataclass
class FakeReplyMarkup:
    rows: list[FakeButtonRow]


class FakePhotoMessage(FakeMessage):
    photo = object()


def test_extract_links_marks_visible_url_entity() -> None:
    message = FakeMessage([(MessageEntityUrl(offset=0, length=11), "example.com")])

    links = _extract_links(FakeEvent(message))

    assert len(links) == 1
    assert links[0].text == "example.com"
    assert links[0].url == "https://example.com"
    assert links[0].source == "visible_url"
    assert links[0].text_start == 0
    assert links[0].text_end == 11


def test_extract_links_marks_text_url_entity() -> None:
    message = FakeMessage(
        [(MessageEntityTextUrl(offset=0, length=4, url="https://example.com"), "Docs")]
    )

    links = _extract_links(FakeEvent(message))

    assert len(links) == 1
    assert links[0].text == "Docs"
    assert links[0].url == "https://example.com"
    assert links[0].source == "text_url"
    assert links[0].text_start == 0
    assert links[0].text_end == 4


def test_extract_links_reads_buttons_in_row_major_order() -> None:
    message = FakeMessage(
        buttons=[
            [FakeButton("A", "https://example.com/a"), FakeButton("B", "example.com/b")],
            [FakeButton("C", "https://example.com/c")],
        ]
    )

    links = _extract_links(FakeEvent(message))

    assert [(link.text, link.url, link.source) for link in links] == [
        ("A", "https://example.com/a", "button_url"),
        ("B", "https://example.com/b", "button_url"),
        ("C", "https://example.com/c", "button_url"),
    ]
    assert all(link.text_start is None and link.text_end is None for link in links)


def test_extract_links_dedupes_repeated_button_urls() -> None:
    message = FakeMessage(
        buttons=[
            [FakeButton("First", "https://example.com")],
            [FakeButton("Second", "example.com")],
        ]
    )

    links = _extract_links(FakeEvent(message))

    assert [(link.text, link.url) for link in links] == [("First", "https://example.com")]


def test_extract_links_falls_back_to_reply_markup_buttons() -> None:
    message = FakeMessage(
        reply_markup=FakeReplyMarkup(
            rows=[FakeButtonRow([FakeButton("Open", "https://example.com/open")])]
        )
    )

    links = _extract_links(FakeEvent(message))

    assert [(link.text, link.url, link.source) for link in links] == [
        ("Open", "https://example.com/open", "button_url")
    ]


def test_media_type_skips_webpage_media_by_default() -> None:
    message = FakeMessage(media=MessageMediaWebPage(webpage=None))

    assert _media_type(FakeEvent(message)) is None


def test_media_type_can_include_webpage_media() -> None:
    message = FakeMessage(media=MessageMediaWebPage(webpage=None))

    assert _media_type(FakeEvent(message), include_link_preview_media=True) == "link_preview"


def test_media_type_real_photo_still_wins() -> None:
    message = FakePhotoMessage(media=object())

    assert _media_type(FakeEvent(message)) == "photo"
