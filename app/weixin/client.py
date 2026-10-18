"""iLink（ClawBot）HTTP 客户端。

所有请求都需要三个固定头：

* ``AuthorizationType: ilink_bot_token``
* ``X-WECHAT-UIN``：随机 uint32 的 base64，每次请求重新生成
* ``Authorization: Bearer <bot_token>``（登录后才需要）

登录相关的两个接口免 token；``getupdates`` / ``sendmessage`` 必须带 token。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import random
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.weixin.crypto import aes_padded_size, decrypt_media, encrypt_media, parse_media_aes_key
from app.weixin.models import (
    DEFAULT_BASE_URL,
    DEFAULT_BOT_TYPE,
    DEFAULT_CDN_BASE_URL,
    SESSION_TIMEOUT_ERRCODE,
    WeixinApiError,
    WeixinSessionExpired,
)

logger = logging.getLogger(__name__)

CHANNEL_VERSION = "tgqq-forwarder"
DEFAULT_API_TIMEOUT_SECONDS = 60.0
DEFAULT_LONG_POLL_TIMEOUT_SECONDS = 35.0


def random_wechat_uin() -> str:
    """``X-WECHAT-UIN``：随机 uint32 的 base64。"""
    return base64.b64encode(str(random.getrandbits(32)).encode("utf-8")).decode("utf-8")


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class WeixinIlinkClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        cdn_base_url: str = DEFAULT_CDN_BASE_URL,
        token: str | None = None,
        api_timeout_seconds: float = DEFAULT_API_TIMEOUT_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.cdn_base_url = (cdn_base_url or DEFAULT_CDN_BASE_URL).rstrip("/")
        self.token = token or None
        self.api_timeout_seconds = api_timeout_seconds
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(api_timeout_seconds, connect=15.0),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    # ---------------------------------------------------------------- 底层请求

    def _build_headers(self, *, token_required: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": random_wechat_uin(),
        }
        if token_required and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _resolve_url(self, endpoint: str) -> str:
        return f"{self.base_url}/{endpoint.lstrip('/')}"

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        token_required: bool = False,
        timeout_seconds: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = self._build_headers(token_required=token_required)
        if extra_headers:
            headers.update(extra_headers)
        timeout = timeout_seconds if timeout_seconds is not None else self.api_timeout_seconds
        try:
            response = await self._http.request(
                method,
                self._resolve_url(endpoint),
                params=params,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise WeixinApiError(
                f"{method} {endpoint} 请求失败：{type(exc).__name__}: {exc}"
            ) from exc

        text = response.text
        if response.status_code >= 400:
            raise WeixinApiError(
                f"{method} {endpoint} 返回 HTTP {response.status_code}: {text[:512]}"
            )
        if not text:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise WeixinApiError(f"{method} {endpoint} 返回非 JSON 内容：{text[:256]}") from exc
        if not isinstance(data, dict):
            raise WeixinApiError(f"{method} {endpoint} 返回格式异常：{type(data).__name__}")
        return data

    @staticmethod
    def unwrap(payload: dict[str, Any]) -> dict[str, Any]:
        """兼容两种响应外壳：裸字段，或 ``{"success": true, "data": {...}}``。"""
        data = payload.get("data")
        if "success" in payload and isinstance(data, dict):
            merged = dict(data)
            for key in ("baseurl", "base_url"):
                if key in payload and key not in merged:
                    merged[key] = payload[key]
            return merged
        return payload

    @staticmethod
    def raise_for_error(payload: dict[str, Any], action: str) -> None:
        code = payload.get("errcode")
        if code is None:
            code = payload.get("ret")
        if code is None:
            return
        errcode = _as_int(code)
        if errcode == 0:
            return
        message = (
            payload.get("errmsg")
            or payload.get("err_msg")
            or payload.get("message")
            or payload.get("msg")
            or ""
        )
        detail = f"{action} 失败：errcode={code} {message}".strip()
        if errcode == SESSION_TIMEOUT_ERRCODE:
            raise WeixinSessionExpired(detail, errcode=errcode)
        raise WeixinApiError(detail, errcode=errcode)

    # ---------------------------------------------------------------- 登录

    async def get_bot_qrcode(self, bot_type: str = DEFAULT_BOT_TYPE) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            "ilink/bot/get_bot_qrcode",
            params={"bot_type": bot_type},
            timeout_seconds=15.0,
        )
        data = self.unwrap(payload)
        self.raise_for_error(data, "获取登录二维码")
        return data

    async def get_qrcode_status(
        self,
        qrcode: str,
        *,
        timeout_seconds: float = DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            "ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode},
            timeout_seconds=timeout_seconds,
            extra_headers={"iLink-App-ClientVersion": "1"},
        )
        data = self.unwrap(payload)
        self.raise_for_error(data, "轮询扫码状态")
        return data

    # ---------------------------------------------------------------- 收发

    async def get_updates(
        self,
        get_updates_buf: str,
        *,
        timeout_seconds: float = DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        try:
            payload = await self._request(
                "POST",
                "ilink/bot/getupdates",
                payload={
                    "base_info": {"channel_version": CHANNEL_VERSION},
                    "get_updates_buf": get_updates_buf,
                },
                token_required=True,
                # 服务端会 hold 住请求直到有新消息或自身超时，客户端留一点余量。
                timeout_seconds=timeout_seconds + 15.0,
            )
        except WeixinApiError as exc:
            # 长轮询超时属于正常现象，返回空响应让调用方进入下一轮。
            if isinstance(exc.__cause__, httpx.TimeoutException):
                logger.debug("weixin long poll timed out; retrying")
                return {}
            raise
        data = self.unwrap(payload)
        self.raise_for_error(data, "拉取微信消息")
        return data

    async def send_message(
        self,
        *,
        to_user_id: str,
        item_list: list[dict[str, Any]],
        context_token: str,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            "ilink/bot/sendmessage",
            payload={
                "base_info": {"channel_version": CHANNEL_VERSION},
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": client_id or uuid.uuid4().hex,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": item_list,
                },
            },
            token_required=True,
        )
        data = self.unwrap(payload)
        self.raise_for_error(data, "发送微信消息")
        return data

    # ---------------------------------------------------------------- 媒体

    def _build_cdn_upload_url(self, upload_param: str, file_key: str) -> str:
        return (
            f"{self.cdn_base_url}/upload?"
            f"encrypted_query_param={quote(upload_param)}&filekey={quote(file_key)}"
        )

    def _build_cdn_download_url(self, encrypted_query_param: str) -> str:
        return f"{self.cdn_base_url}/download?encrypted_query_param={quote(encrypted_query_param)}"

    async def get_upload_url(
        self,
        *,
        to_user_id: str,
        file_key: str,
        media_type: int,
        raw_size: int,
        raw_md5: str,
        aes_key_hex: str,
    ) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            "ilink/bot/getuploadurl",
            payload={
                "filekey": file_key,
                "media_type": media_type,
                "to_user_id": to_user_id,
                "rawsize": raw_size,
                "rawfilemd5": raw_md5,
                "filesize": aes_padded_size(raw_size),
                "no_need_thumb": True,
                "aeskey": aes_key_hex,
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
            token_required=True,
        )
        data = self.unwrap(payload)
        self.raise_for_error(data, "获取媒体上传地址")
        return data

    async def upload_media(self, *, upload_url: str, aes_key_hex: str, media_path: Path) -> str:
        """加密并上传媒体，返回下载用的 ``encrypted_query_param``。"""
        raw = media_path.read_bytes()
        encrypted = encrypt_media(bytes.fromhex(aes_key_hex), raw)
        try:
            response = await self._http.post(
                upload_url,
                content=encrypted,
                headers={"Content-Type": "application/octet-stream"},
                timeout=self.api_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise WeixinApiError(f"上传媒体失败：{type(exc).__name__}: {exc}") from exc

        if response.status_code != 200:
            raise WeixinApiError(
                f"上传媒体失败：HTTP {response.status_code} {response.text[:256]}"
            )
        download_param = response.headers.get("x-encrypted-param")
        if not download_param:
            raise WeixinApiError("上传媒体失败：响应缺少 x-encrypted-param")
        logger.debug(
            "weixin media uploaded path=%s raw=%s cipher=%s md5=%s",
            media_path.name,
            len(raw),
            len(encrypted),
            hashlib.md5(raw).hexdigest(),
        )
        return download_param

    async def download_media(self, *, encrypted_query_param: str, aes_key: str) -> bytes:
        url = self._build_cdn_download_url(encrypted_query_param)
        try:
            response = await self._http.get(url, timeout=self.api_timeout_seconds)
        except httpx.HTTPError as exc:
            raise WeixinApiError(f"下载媒体失败：{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise WeixinApiError(f"下载媒体失败：HTTP {response.status_code}")
        key = parse_media_aes_key(aes_key)
        return decrypt_media(key, response.content)

    async def upload_and_build_media_payload(
        self,
        *,
        to_user_id: str,
        media_path: Path,
        upload_media_type: int,
    ) -> tuple[dict[str, Any], int]:
        """上传单个文件，返回 ``(media 结构, 密文长度)``。"""
        raw = media_path.read_bytes()
        file_key = uuid.uuid4().hex
        aes_key_hex = uuid.uuid4().bytes.hex()
        upload_info = await self.get_upload_url(
            to_user_id=to_user_id,
            file_key=file_key,
            media_type=upload_media_type,
            raw_size=len(raw),
            raw_md5=hashlib.md5(raw).hexdigest(),
            aes_key_hex=aes_key_hex,
        )
        upload_param = str(upload_info.get("upload_param") or "").strip()
        upload_full_url = str(upload_info.get("upload_full_url") or "").strip()
        if upload_full_url:
            upload_url = upload_full_url
        elif upload_param:
            upload_url = self._build_cdn_upload_url(upload_param, file_key)
        else:
            raise WeixinApiError("获取媒体上传地址失败：缺少 upload_param / upload_full_url")

        download_param = await self.upload_media(
            upload_url=upload_url,
            aes_key_hex=aes_key_hex,
            media_path=media_path,
        )
        return (
            {
                "encrypt_query_param": download_param,
                "aes_key": base64.b64encode(aes_key_hex.encode("utf-8")).decode("utf-8"),
                "encrypt_type": 1,
            },
            aes_padded_size(len(raw)),
        )


def build_text_item(text: str) -> dict[str, Any]:
    return {"type": 1, "text_item": {"text": text}}


def build_image_item(media: dict[str, Any], *, size: int = 0) -> dict[str, Any]:
    return {"type": 2, "image_item": {"media": media, "mid_size": size}}


def build_video_item(media: dict[str, Any], *, size: int = 0) -> dict[str, Any]:
    return {"type": 5, "video_item": {"media": media, "video_size": size}}


def build_file_item(media: dict[str, Any], *, file_name: str, raw_size: int) -> dict[str, Any]:
    return {
        "type": 4,
        "file_item": {"media": media, "file_name": file_name, "len": str(raw_size)},
    }
