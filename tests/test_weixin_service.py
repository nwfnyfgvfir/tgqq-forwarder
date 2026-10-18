from __future__ import annotations

import base64
import json

import pytest

from app.config import Settings
from app.weixin.models import (
    WeixinCredentials,
    WeixinError,
    WeixinOutboundMessage,
    WeixinSessionExpired,
)
from app.weixin.service import WeixinService, split_text

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-image"


def make_settings(tmp_path, **overrides) -> Settings:
    data = {
        "_env_file": None,
        "data_dir": tmp_path,
        "log_dir": tmp_path / "logs",
        "media_dir": tmp_path / "media",
        "weixin_state_dir": tmp_path / "weixin",
        "weixin_enabled": True,
    }
    data.update(overrides)
    return Settings(**data)


class FakeClient:
    def __init__(self) -> None:
        self.token: str | None = None
        self.base_url = "https://ilink.example"
        self.qrcode_payload: dict = {
            "qrcode": "qr-1",
            "qrcode_img_content": base64.b64encode(PNG_BYTES).decode(),
            "qrcode_url": "https://qr.example/1",
        }
        self.qrcode_status: dict | Exception = {"status": "wait"}
        self.updates: dict = {"ret": 0, "msgs": [], "get_updates_buf": "buf-1"}
        self.updates_error: Exception | None = None
        self.sent: list[dict] = []
        self.downloads: list[dict] = []
        self.closed = False

    async def get_bot_qrcode(self, bot_type: str = "3") -> dict:
        return dict(self.qrcode_payload)

    async def get_qrcode_status(self, qrcode: str, *, timeout_seconds: float = 35.0) -> dict:
        if isinstance(self.qrcode_status, Exception):
            raise self.qrcode_status
        return dict(self.qrcode_status)

    async def get_updates(self, buf: str, *, timeout_seconds: float = 35.0) -> dict:
        # 真实长轮询一定会 await I/O；这里加一个极小 sleep，避免忙轮询饿死事件循环。
        import asyncio

        await asyncio.sleep(0.005)
        if self.updates_error is not None:
            raise self.updates_error
        return dict(self.updates)

    async def send_message(
        self,
        *,
        to_user_id: str,
        item_list: list[dict],
        context_token: str,
        client_id: str | None = None,
    ) -> dict:
        self.sent.append(
            {
                "to_user_id": to_user_id,
                "item_list": item_list,
                "context_token": context_token,
            }
        )
        return {"ret": 0}

    async def download_media(self, *, encrypted_query_param: str, aes_key: str) -> bytes:
        self.downloads.append(
            {"encrypted_query_param": encrypted_query_param, "aes_key": aes_key}
        )
        return PNG_BYTES

    async def upload_and_build_media_payload(
        self, *, to_user_id: str, media_path, upload_media_type: int
    ):
        return (
            {"encrypt_query_param": "dl-param", "aes_key": "a2V5", "encrypt_type": 1},
            32,
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def service(tmp_path, fake_client) -> WeixinService:
    collected: list = []
    svc = WeixinService(
        make_settings(tmp_path),
        collected.append,  # type: ignore[arg-type]
        client_factory=lambda credentials: fake_client,
    )
    svc.client = fake_client
    svc.collected = collected  # type: ignore[attr-defined]
    svc.state.credentials = WeixinCredentials(token="bot-token", account_id="bot", user_id="me")
    fake_client.token = "bot-token"
    return svc


async def test_inbound_text_message_maps_to_forward_message(service, fake_client) -> None:
    raw = {
        "message_id": "msg-1",
        "from_user_id": "wxid_friend",
        "message_type": 1,
        "context_token": "ctx-1",
        "create_time": 1_700_000_000,
        "item_list": [{"type": 1, "text_item": {"text": "你好 世界"}}],
    }

    await service._handle_inbound(raw)

    assert len(service.collected) == 1
    message = service.collected[0]
    assert message.source_platform == "weixin"
    assert message.chat_id == "wxid_friend"
    assert message.sender_id == "wxid_friend"
    assert message.chat_type == "private"
    assert message.text == "你好 世界"
    assert message.message_id == "msg-1"
    assert message.account_id == "default"
    assert message.date is not None
    assert service.state.context_tokens["wxid_friend"] == "ctx-1"
    assert service.state.contacts["wxid_friend"].last_seen > 0


async def test_inbound_skips_bot_messages(service) -> None:
    await service._handle_inbound(
        {
            "message_id": "msg-2",
            "from_user_id": "wxid_friend",
            "message_type": 2,
            "item_list": [{"type": 1, "text_item": {"text": "bot"}}],
        }
    )
    assert service.collected == []


async def test_inbound_deduplicates_by_message_id(service) -> None:
    raw = {
        "message_id": "msg-3",
        "from_user_id": "wxid_friend",
        "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
    }
    await service._handle_inbound(dict(raw))
    await service._handle_inbound(dict(raw))
    assert len(service.collected) == 1


async def test_inbound_media_is_downloaded(service, fake_client) -> None:
    raw = {
        "message_id": "msg-4",
        "from_user_id": "wxid_friend",
        "item_list": [
            {
                "type": 2,
                "image_item": {
                    "media": {"encrypt_query_param": "eqp", "aes_key": "a2V5"},
                },
            }
        ],
    }

    await service._handle_inbound(raw)

    message = service.collected[0]
    assert message.text == "[图片]"
    assert message.media_type == "photo"
    assert len(message.media_paths) == 1
    assert message.media_paths[0].exists()
    assert message.media_paths[0].suffix == ".png"
    assert fake_client.downloads == [{"encrypted_query_param": "eqp", "aes_key": "a2V5"}]


async def test_send_requires_context_token(service) -> None:
    with pytest.raises(WeixinError):
        await service.send(WeixinOutboundMessage(target_user_id="wxid_unknown", text="hi"))


async def test_send_splits_long_text_and_sends_media(service, fake_client, tmp_path) -> None:
    service.settings.weixin_max_text_chars = 50
    service.state.context_tokens["wxid_friend"] = "ctx-1"
    media = tmp_path / "a.jpg"
    media.write_bytes(b"jpeg")

    await service.send(
        WeixinOutboundMessage(
            target_user_id="wxid_friend",
            text="a" * 120,
            media_paths=[media],
            media_types=["photo"],
            media_caption="rule",
        )
    )

    text_items = [call for call in fake_client.sent if call["item_list"][0]["type"] == 1]
    assert len(text_items) == 3
    assert "".join(call["item_list"][0]["text_item"]["text"] for call in text_items) == "a" * 120
    assert all(call["context_token"] == "ctx-1" for call in fake_client.sent)
    image_calls = [call for call in fake_client.sent if call["item_list"][0]["type"] == 2]
    assert len(image_calls) == 1
    assert service.status().outbound_total == 1


async def test_begin_login_saves_qrcode_image(service, tmp_path) -> None:
    login = await service.begin_login()

    assert login.qrcode == "qr-1"
    assert login.image_path is not None
    assert login.image_path.read_bytes() == PNG_BYTES
    assert login.image_url == "https://qr.example/1"
    assert login.pending is True


async def test_login_confirmation_persists_credentials(service, fake_client, tmp_path) -> None:
    await service.begin_login()
    fake_client.qrcode_status = {
        "status": "confirmed",
        "bot_token": "new-token",
        "ilink_bot_id": "bot-1",
        "ilink_user_id": "user-1",
        "baseurl": "https://ilink.example",
    }

    login = await service.refresh_login_status()

    assert login is not None and login.confirmed is True
    assert service.logged_in is True
    assert service.status().connected is True
    assert service.state.credentials is not None
    assert service.state.credentials.account_id == "bot-1"

    stored = json.loads(service.store.path.read_text(encoding="utf-8"))
    assert stored["token"] == "new-token"
    assert stored["account_id"] == "bot-1"


async def test_expired_qrcode_is_refreshed(service, fake_client) -> None:
    await service.begin_login()
    fake_client.qrcode_status = {"status": "expired"}

    await service.refresh_login_status()

    assert service.login is not None
    assert service.login.qrcode == "qr-1"
    assert service._qr_expired_count == 1


async def test_session_expiry_clears_credentials(service, fake_client) -> None:
    await service._handle_session_expired(WeixinSessionExpired("expired", errcode=-14))

    assert service.logged_in is False
    assert service.status().logged_in is False
    assert service.store.path.exists() is False


async def test_status_reports_contacts(service) -> None:
    service.state.contacts.clear()
    await service._handle_inbound(
        {
            "message_id": "msg-9",
            "from_user_id": "wxid_a",
            "context_token": "ctx-a",
            "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
        }
    )

    status = service.status()
    assert status.contacts == 1
    assert status.inbound_total == 1
    contacts = service.list_contacts()
    assert contacts[0].user_id == "wxid_a"
    assert contacts[0].has_context_token is True


def test_split_text_prefers_newline_boundaries() -> None:
    text = "line1\nline2\nline3"
    assert split_text(text, 100) == [text]
    chunks = split_text(text, 12)
    assert chunks[0] == "line1\nline2"
    assert "".join(chunk.replace("\n", "") for chunk in chunks) == "line1line2line3"


def test_split_text_returns_empty_for_blank() -> None:
    assert split_text("   ", 10) == []


async def test_service_loop_logs_in_then_receives(tmp_path, fake_client) -> None:
    import asyncio

    collected: list = []
    settings = make_settings(
        tmp_path,
        weixin_qr_poll_interval_seconds=0.01,
        weixin_long_poll_timeout_seconds=0.01,
    )
    svc = WeixinService(
        settings,
        collected.append,  # type: ignore[arg-type]
        client_factory=lambda credentials: fake_client,
    )
    fake_client.qrcode_status = {
        "status": "confirmed",
        "bot_token": "loop-token",
        "ilink_bot_id": "bot-loop",
        "ilink_user_id": "user-loop",
    }
    fake_client.updates = {
        "ret": 0,
        "msgs": [
            {
                "message_id": "loop-msg-1",
                "from_user_id": "wxid_loop",
                "context_token": "ctx-loop",
                "item_list": [{"type": 1, "text_item": {"text": "来自微信"}}],
            }
        ],
        "get_updates_buf": "buf-loop",
    }

    await svc.start()
    try:
        for _ in range(200):
            if collected:
                break
            await asyncio.sleep(0.01)
        assert svc.logged_in is True
        assert len(collected) == 1
        assert collected[0].text == "来自微信"
        assert svc.state.context_tokens["wxid_loop"] == "ctx-loop"
    finally:
        await svc.stop()

    assert fake_client.closed is True
    stored = json.loads(svc.store.path.read_text(encoding="utf-8"))
    assert stored["token"] == "loop-token"
    assert stored["context_tokens"]["wxid_loop"] == "ctx-loop"
    assert stored["get_updates_buf"] == "buf-loop"
