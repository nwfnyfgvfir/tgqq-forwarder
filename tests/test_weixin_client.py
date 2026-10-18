from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.weixin.client import WeixinIlinkClient, build_text_item
from app.weixin.crypto import decrypt_media, encrypt_media
from app.weixin.models import WeixinApiError, WeixinSessionExpired


def make_client(handler, *, token: str | None = "bot-token") -> WeixinIlinkClient:
    transport = httpx.MockTransport(handler)
    return WeixinIlinkClient(
        base_url="https://ilink.example",
        cdn_base_url="https://cdn.example/c2c",
        token=token,
        http_client=httpx.AsyncClient(transport=transport),
    )


async def test_get_bot_qrcode_sends_required_headers_and_params() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        captured["authorization_type"] = request.headers.get("AuthorizationType")
        captured["uin"] = request.headers.get("X-WECHAT-UIN")
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"qrcode": "qr-token"})

    client = make_client(handler)
    data = await client.get_bot_qrcode()

    assert data["qrcode"] == "qr-token"
    assert captured["path"] == "/ilink/bot/get_bot_qrcode"
    assert captured["params"] == {"bot_type": "3"}
    assert captured["authorization_type"] == "ilink_bot_token"
    assert captured["authorization"] is None
    assert isinstance(captured["uin"], str)
    # X-WECHAT-UIN 是随机 uint32 的 base64
    assert base64.b64decode(str(captured["uin"])).decode().isdigit()


async def test_get_qrcode_status_unwraps_envelope_and_sets_client_version_header() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["version"] = request.headers.get("iLink-App-ClientVersion")
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "status": "confirmed",
                    "bot_token": "tok",
                    "ilink_bot_id": "bot",
                    "ilink_user_id": "me",
                },
                "baseurl": "https://ilink.example",
            },
        )

    client = make_client(handler)
    data = await client.get_qrcode_status("qr-token")

    assert data["status"] == "confirmed"
    assert data["bot_token"] == "tok"
    assert data["baseurl"] == "https://ilink.example"
    assert captured["version"] == "1"
    assert captured["params"] == {"qrcode": "qr-token"}


async def test_get_updates_sends_cursor_and_returns_messages() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={"ret": 0, "msgs": [{"from_user_id": "wxid"}], "get_updates_buf": "next"},
        )

    client = make_client(handler)
    data = await client.get_updates("cursor-1")

    assert data["get_updates_buf"] == "next"
    assert data["msgs"] == [{"from_user_id": "wxid"}]
    assert captured["path"] == "/ilink/bot/getupdates"
    assert captured["authorization"] == "Bearer bot-token"
    body = captured["body"]
    assert body["get_updates_buf"] == "cursor-1"
    assert body["base_info"]["channel_version"]


async def test_get_updates_raises_session_expired_on_errcode_minus_14() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": -14, "errmsg": "session timeout"})

    client = make_client(handler)
    with pytest.raises(WeixinSessionExpired):
        await client.get_updates("cursor")


async def test_api_error_is_raised_for_nonzero_ret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ret": -1, "errmsg": "boom"})

    client = make_client(handler)
    with pytest.raises(WeixinApiError) as excinfo:
        await client.send_message(
            to_user_id="wxid",
            item_list=[build_text_item("hi")],
            context_token="ctx",
        )
    assert excinfo.value.errcode == -1
    assert "boom" in str(excinfo.value)


async def test_http_error_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    client = make_client(handler)
    with pytest.raises(WeixinApiError):
        await client.get_bot_qrcode()


async def test_send_message_payload_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ret": 0})

    client = make_client(handler)
    await client.send_message(
        to_user_id="wxid-1",
        item_list=[build_text_item("hello")],
        context_token="ctx-1",
    )

    assert captured["path"] == "/ilink/bot/sendmessage"
    msg = captured["body"]["msg"]
    assert msg["to_user_id"] == "wxid-1"
    assert msg["context_token"] == "ctx-1"
    assert msg["message_type"] == 2
    assert msg["message_state"] == 2
    assert msg["item_list"] == [{"type": 1, "text_item": {"text": "hello"}}]
    assert msg["client_id"]


async def test_media_upload_encrypts_and_download_decrypts(tmp_path) -> None:
    raw = b"binary-image-bytes" * 5
    media_path = tmp_path / "image.jpg"
    media_path.write_bytes(raw)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ilink/bot/getuploadurl":
            body = json.loads(request.content)
            captured["upload_body"] = body
            return httpx.Response(
                200,
                json={"upload_param": "up-param"},
            )
        if request.url.path == "/c2c/upload":
            captured["upload_query"] = parse_qs(request.url.query.decode())
            captured["cipher"] = request.content
            return httpx.Response(200, headers={"x-encrypted-param": "dl-param"})
        if request.url.path == "/c2c/download":
            aes_key_hex = str(captured["upload_body"]["aeskey"])
            return httpx.Response(200, content=encrypt_media(bytes.fromhex(aes_key_hex), raw))
        raise AssertionError(f"unexpected path {request.url.path}")

    client = make_client(handler)
    media, padded_size = await client.upload_and_build_media_payload(
        to_user_id="wxid-1",
        media_path=media_path,
        upload_media_type=1,
    )

    upload_body = captured["upload_body"]
    assert upload_body["rawsize"] == len(raw)
    assert upload_body["filesize"] == padded_size
    assert upload_body["no_need_thumb"] is True
    assert upload_body["media_type"] == 1
    assert captured["upload_query"]["filekey"] == [upload_body["filekey"]]

    aes_key_hex = str(upload_body["aeskey"])
    assert decrypt_media(bytes.fromhex(aes_key_hex), captured["cipher"]) == raw

    assert media["encrypt_query_param"] == "dl-param"
    assert media["encrypt_type"] == 1

    downloaded = await client.download_media(
        encrypted_query_param="dl-param",
        aes_key=str(media["aes_key"]),
    )
    assert downloaded == raw


async def test_upload_requires_upload_param(tmp_path) -> None:
    media_path = tmp_path / "x.bin"
    media_path.write_bytes(b"data")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = make_client(handler)
    with pytest.raises(WeixinApiError):
        await client.upload_and_build_media_payload(
            to_user_id="wxid",
            media_path=media_path,
            upload_media_type=1,
        )
