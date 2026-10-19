"""个人微信通道服务：扫码登录 + 长轮询收消息 + 发送消息。

生命周期与 Telegram 监听器一致，由 ``ApplicationRuntime`` 启停：

* 有可用凭证 → 直接进入长轮询；
* 没有凭证 → 自动申请二维码，等待管理员扫码（终端日志 / Web 管理台）；
* 登录态失效（errcode=-14）→ 清空凭证并重新进入扫码流程。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.rules.models import TelegramForwardMessage
from app.weixin.client import (
    DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
    WeixinIlinkClient,
    build_text_item,
)
from app.weixin.credentials import WeixinState, WeixinStateStore
from app.weixin.media import WeixinMediaStore
from app.weixin.models import (
    DEFAULT_BASE_URL,
    DEFAULT_BOT_TYPE,
    PLATFORM_WEIXIN,
    PREPARE_FAILED_ERRCODE,
    PendingWeixinMessage,
    WeixinApiError,
    WeixinContact,
    WeixinCredentials,
    WeixinError,
    WeixinItemType,
    WeixinLoginError,
    WeixinLoginState,
    WeixinMessageType,
    WeixinMissingToken,
    WeixinOutboundMessage,
    WeixinPending,
    WeixinSessionExpired,
    WeixinStatus,
)
from app.weixin.qrcode import (
    decode_image_base64,
    looks_like_url,
    render_qr_png,
)

logger = logging.getLogger(__name__)

MessageCallback = Callable[[TelegramForwardMessage], Awaitable[None]]

QRCODE_TTL_SECONDS = 300.0
QRCODE_IMAGE_NAME = "login-qrcode.png"
RECENT_MESSAGE_LIMIT = 500
MAX_MEDIA_ITEMS = 4
ITEM_PLACEHOLDERS = {
    WeixinItemType.IMAGE: "[图片]",
    WeixinItemType.VOICE: "[语音]",
    WeixinItemType.FILE: "[文件]",
    WeixinItemType.VIDEO: "[视频]",
}


class WeixinService:
    def __init__(
        self,
        settings: Settings,
        on_message: MessageCallback,
        *,
        account_id: str | None = None,
        client_factory: Callable[[WeixinCredentials | None], WeixinIlinkClient] | None = None,
    ) -> None:
        self.settings = settings
        self.on_message = on_message
        self.account_id = account_id or settings.weixin_account_id
        self.store = WeixinStateStore(settings.weixin_state_dir, self.account_id)
        self.media = WeixinMediaStore(
            settings.media_dir,
            self.account_id,
            enabled=settings.weixin_download_media,
            max_media_mb=settings.weixin_max_media_mb,
        )
        self._client_factory = client_factory or self._default_client_factory
        self.client: WeixinIlinkClient | None = None
        self.state: WeixinState = WeixinState()
        self.login: WeixinLoginState | None = None
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._connected = False
        self._last_error: str | None = None
        self._inbound_total = 0
        self._outbound_total = 0
        self._error_total = 0
        self._recent_ids: OrderedDict[str, float] = OrderedDict()
        self._qr_expired_count = 0
        # 出站节流：ilink 对连续 sendmessage 会返回 errcode=-2（限流）。
        self._last_send_at = 0.0
        self._send_lock = asyncio.Lock()
        # token 失效时挂起的消息（按联系人），等对方发消息刷新 token 后补发。
        self._pending: dict[str, list[PendingWeixinMessage]] = {}
        self._pending_task: asyncio.Task | None = None
        self._flush_tasks: set[asyncio.Task] = set()
        self._flush_lock = asyncio.Lock()

    # ---------------------------------------------------------------- 生命周期

    @property
    def enabled(self) -> bool:
        return bool(self.settings.weixin_enabled)

    @property
    def logged_in(self) -> bool:
        return bool(self.state.credentials and self.state.credentials.token)

    @property
    def is_connected(self) -> bool:
        return self._connected and self.logged_in

    @property
    def account_user_id(self) -> str | None:
        credentials = self.state.credentials
        return credentials.user_id if credentials else None

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Weixin channel disabled; skip starting")
            return
        if self._task is not None and not self._task.done():
            return
        self.state = self.store.load()
        self._ensure_client()
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="weixin-channel")
        if self.settings.weixin_pending_send_enabled:
            self._pending_task = asyncio.create_task(
                self._pending_loop(), name="weixin-pending-send"
            )
        await asyncio.sleep(0)
        logger.info(
            "Weixin channel started account=%s logged_in=%s",
            self.account_id,
            self.logged_in,
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._pending_task is not None:
            self._pending_task.cancel()
            try:
                await self._pending_task
            except asyncio.CancelledError:
                pass
            self._pending_task = None
        for task in list(self._flush_tasks):
            task.cancel()
        self._flush_tasks.clear()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        self._connected = False
        logger.info("Weixin channel stopped account=%s", self.account_id)

    # ---------------------------------------------------------------- 状态

    def status(self) -> WeixinStatus:
        return WeixinStatus(
            enabled=self.enabled,
            logged_in=self.logged_in,
            connected=self.is_connected,
            account_id=self.account_id,
            account_user_id=self.account_user_id,
            base_url=(
                self.state.credentials.base_url if self.state.credentials else DEFAULT_BASE_URL
            ),
            contacts=len(self.state.contacts),
            inbound_total=self._inbound_total,
            outbound_total=self._outbound_total,
            error_total=self._error_total,
            pending_outbound=self.pending_count(),
            last_error=self._last_error,
            login=self.login,
        )

    def pending_count(self) -> int:
        return sum(len(items) for items in self._pending.values())

    def list_contacts(self) -> list[WeixinContact]:
        contacts = list(self.state.contacts.values())
        for contact in contacts:
            contact.has_context_token = contact.user_id in self.state.context_tokens
        contacts.sort(key=lambda item: item.last_seen, reverse=True)
        return contacts

    def has_context_token(self, user_id: str) -> bool:
        return bool(self.state.context_tokens.get(user_id))

    # ---------------------------------------------------------------- 登录

    async def begin_login(self, *, force: bool = False) -> WeixinLoginState:
        if not self.enabled:
            raise WeixinError("微信通道未启用，请先设置 WEIXIN_ENABLED=true")
        if self.client is None:
            self._ensure_client()
        if self.login is not None and self.login.pending and not force:
            return self.login
        await self._start_login()
        assert self.login is not None
        return self.login

    def login_status(self) -> WeixinLoginState | None:
        """当前扫码会话（只读快照）。

        轮询由服务内部的长轮询循环负责，HTTP 侧不再触发额外请求，
        避免和循环同时 hold 在同一个二维码上。
        """
        return self.login

    async def logout(self) -> None:
        self.store.clear()
        self.state = WeixinState()
        self.login = None
        self._connected = False
        if self.client is not None:
            self.client.token = None

    async def _start_login(self) -> None:
        assert self.client is not None
        self._qr_expired_count = 0
        await self._create_login_session()

    async def _create_login_session(self) -> None:
        assert self.client is not None
        data = await self.client.get_bot_qrcode(self.settings.weixin_bot_type or DEFAULT_BOT_TYPE)
        qrcode = str(
            data.get("qrcode")
            or data.get("qrcode_token")
            or data.get("token")
            or data.get("qrcode_str")
            or ""
        ).strip()
        if not qrcode:
            raise WeixinLoginError(f"获取二维码失败：响应缺少 qrcode 字段（{sorted(data)}）")

        image_path, image_url, scan_url = self._prepare_qrcode(data)
        self.login = WeixinLoginState(
            qrcode=qrcode,
            image_path=image_path,
            image_url=image_url,
            scan_url=scan_url,
            expires_at=time.time() + QRCODE_TTL_SECONDS,
            refresh_count=self._qr_expired_count,
        )
        logger.warning(
            "Weixin 需要扫码登录：请用微信扫描二维码。图片=%s 扫码内容=%s",
            image_path or "(无)",
            scan_url or image_url or "(无)",
        )

    def _qrcode_path(self) -> Path:
        return Path(self.settings.weixin_state_dir) / self.account_id / QRCODE_IMAGE_NAME

    def _prepare_qrcode(
        self,
        data: dict[str, Any],
    ) -> tuple[Path | None, str | None, str | None]:
        """解析二维码响应，返回 ``(本地图片, 外部图片链接, 扫码内容)``。

        真实接口返回的 ``qrcode_img_content`` 是扫码 URL（``https://liteapp.weixin.qq.com/...``），
        不是图片，必须本地渲染；同时兼容它直接返回 base64 图片的形态。
        """
        content = str(
            data.get("qrcode_img_content")
            or data.get("qrcode_image")
            or data.get("image")
            or ""
        ).strip()
        image_url = (
            str(
                data.get("qrcode_url")
                or data.get("qrcode_img_url")
                or data.get("qrcode_image_url")
                or ""
            ).strip()
            or None
        )

        image_path: Path | None = None
        scan_url: str | None = None

        if looks_like_url(content):
            scan_url = content
            target = self._qrcode_path()
            if render_qr_png(content, target):
                image_path = target
        elif content:
            binary = decode_image_base64(content)
            if binary is None:
                logger.warning("微信二维码内容既不是链接也不是图片，已忽略")
            else:
                target = self._qrcode_path()
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(binary)
                    image_path = target
                except OSError:
                    logger.exception("Failed to write Weixin qrcode image to %s", target)

        return image_path, image_url, scan_url

    async def _poll_login_once(self) -> None:
        login = self.login
        if login is None or not login.pending:
            return
        assert self.client is not None
        try:
            data = await self.client.get_qrcode_status(
                login.qrcode,
                timeout_seconds=self.settings.weixin_long_poll_timeout_seconds
                or DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
            )
        except WeixinSessionExpired:
            login.status = "expired"
            return

        status = str(data.get("status") or data.get("state") or "").strip().lower()
        if not status:
            # 本轮长轮询超时，没有任何状态变化；保持现有状态，只检查本地有效期。
            if time.time() > login.expires_at:
                await self._on_login_expired()
            return
        login.status = status
        if status in {"scaned", "scanned", "scan"}:
            login.status = "scaned"
            logger.info("Weixin 二维码已被扫描，等待手机确认")
            return
        if status in {"confirmed", "confirm", "ok", "success"}:
            await self._on_login_confirmed(data)
            return
        if status in {"expired", "timeout"}:
            await self._on_login_expired()
            return
        if time.time() > login.expires_at:
            await self._on_login_expired()

    async def _on_login_expired(self) -> None:
        login = self.login
        if login is None:
            return
        login.status = "expired"
        self._qr_expired_count += 1
        if self._qr_expired_count > self.settings.weixin_qr_max_refresh:
            login.error = "二维码多次过期，已停止自动刷新"
            logger.error("Weixin 二维码多次过期，停止自动刷新")
            return
        logger.info("Weixin 二维码过期，自动刷新第 %s 次", self._qr_expired_count)
        try:
            await self._create_login_session()
        except Exception as exc:
            login.error = str(exc)
            logger.exception("Failed to refresh Weixin qrcode")

    async def _on_login_confirmed(self, data: dict[str, Any]) -> None:
        login = self.login
        token = str(data.get("bot_token") or data.get("token") or "").strip()
        if not token:
            if login is not None:
                login.status = "error"
                login.error = "扫码确认但未返回 bot_token"
            raise WeixinLoginError("扫码确认但未返回 bot_token")

        base_url = str(data.get("baseurl") or data.get("base_url") or DEFAULT_BASE_URL).strip()
        credentials = WeixinCredentials(
            token=token,
            account_id=str(data.get("ilink_bot_id") or data.get("bot_id") or ""),
            user_id=str(data.get("ilink_user_id") or data.get("user_id") or ""),
            base_url=base_url or DEFAULT_BASE_URL,
            cdn_base_url=self.settings.weixin_cdn_base_url,
        )
        self.state.credentials = credentials
        if self.client is not None:
            self.client.token = token
            self.client.base_url = credentials.base_url.rstrip("/")
        if login is not None:
            login.status = "confirmed"
            login.confirmed = True
            login.error = None
        self._persist()
        self._connected = True
        self._last_error = None
        logger.info(
            "Weixin 登录成功 account=%s ilink_bot_id=%s user_id=%s",
            self.account_id,
            credentials.account_id,
            credentials.user_id,
        )

    # ---------------------------------------------------------------- 主循环

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                if not self.logged_in:
                    await self._login_tick()
                    continue
                await self._poll_inbound()
            except asyncio.CancelledError:
                raise
            except WeixinSessionExpired as exc:
                await self._handle_session_expired(exc)
            except Exception as exc:
                self._error_total += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._connected = False
                logger.exception("Weixin channel loop error account=%s", self.account_id)
                await self._sleep(5.0)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _login_tick(self) -> None:
        if self.login is None or not self.login.pending:
            if self.login is not None and self.login.error:
                # 刷新次数用尽：退避后再试，避免空转打爆接口。
                await self._sleep(60.0)
                self.login = None
            try:
                await self._start_login()
            except Exception as exc:
                self._error_total += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Failed to start Weixin login")
                await self._sleep(10.0)
                return
        await self._poll_login_once()
        if self.logged_in:
            return
        await self._sleep(self.settings.weixin_qr_poll_interval_seconds)

    async def _poll_inbound(self) -> None:
        assert self.client is not None
        data = await self.client.get_updates(
            self.state.get_updates_buf,
            timeout_seconds=self.settings.weixin_long_poll_timeout_seconds
            or DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
        )
        buffer = data.get("get_updates_buf") or data.get("get_updates_buff") or ""
        if isinstance(buffer, str) and buffer:
            self.state.get_updates_buf = buffer

        messages = data.get("msgs") or data.get("messages") or []
        if not isinstance(messages, list):
            messages = []
        self._connected = True
        self._last_error = None
        for raw in messages:
            if not isinstance(raw, dict):
                continue
            try:
                await self._handle_inbound(raw)
            except Exception:
                self._error_total += 1
                logger.exception("Failed to handle Weixin inbound message")
        # 游标与 context_token 一起落盘：一次轮询最多写一次文件。
        if messages or buffer:
            self._persist()
        else:
            # 服务端立即返回空响应时避免忙轮询占满事件循环。
            await asyncio.sleep(0.2)

    async def _handle_session_expired(self, exc: Exception) -> None:
        logger.warning("Weixin 登录态失效，需要重新扫码：%s", exc)
        self._connected = False
        self._last_error = str(exc)
        # logout() 会删除凭证文件，这里不再回写，避免留下空 token 的残留状态。
        await self.logout()

    # ---------------------------------------------------------------- 入站

    async def _handle_inbound(self, raw: dict[str, Any]) -> None:
        message_type = raw.get("message_type")
        if message_type is not None:
            parsed_type = _as_int(message_type)
            if parsed_type is not None and parsed_type != int(WeixinMessageType.USER):
                return

        from_user_id = str(raw.get("from_user_id") or raw.get("from_user") or "").strip()
        if not from_user_id:
            return

        message_id = str(
            raw.get("message_id") or raw.get("msg_id") or raw.get("new_msg_id") or ""
        ).strip()
        if message_id and self._is_duplicate(message_id):
            logger.debug("Skip duplicated Weixin message id=%s", message_id)
            return

        context_token = str(raw.get("context_token") or "").strip()
        if context_token:
            self.state.context_tokens[from_user_id] = context_token

        contact = self.state.contacts.get(from_user_id) or WeixinContact(user_id=from_user_id)
        contact.last_seen = time.time()
        if context_token:
            # 对方又发消息了 = token 已刷新，之前判定的失效标记要清掉。
            contact.token_updated_at = contact.last_seen
            contact.token_stale = False
        contact.has_context_token = from_user_id in self.state.context_tokens
        self.state.contacts[from_user_id] = contact
        if context_token:
            # 新 token 到手，立刻尝试补发之前挂起的消息。
            self._schedule_flush(from_user_id)

        message = await self._build_forward_message(
            raw,
            from_user_id=from_user_id,
            message_id=message_id,
        )
        self._inbound_total += 1
        try:
            await self.on_message(message)
        except Exception:
            logger.exception("Weixin on_message callback failed user=%s", from_user_id)

    async def _build_forward_message(
        self,
        raw: dict[str, Any],
        *,
        from_user_id: str,
        message_id: str,
    ) -> TelegramForwardMessage:
        items = raw.get("item_list") or raw.get("items") or []
        if not isinstance(items, list):
            items = []
        text = self._text_from_items(items)

        media_paths: list[Path] = []
        media_types: list[str] = []
        media_filenames: list[str] = []
        if self.client is not None:
            for item in items:
                if not isinstance(item, dict):
                    continue
                if len(media_paths) >= MAX_MEDIA_ITEMS:
                    break
                saved = await self.media.download_inbound(
                    self.client,
                    item,
                    from_user_id=from_user_id,
                )
                if saved is None:
                    continue
                path, media_type, file_name = saved
                media_paths.append(path)
                media_types.append(media_type)
                media_filenames.append(file_name or "")

        contact = self.state.contacts.get(from_user_id)
        return TelegramForwardMessage(
            message_id=_message_id_value(message_id),
            chat_id=from_user_id,
            chat_title=contact.name if contact else None,
            chat_type="private",
            sender_id=from_user_id,
            sender_username=None,
            sender_display_name=contact.name if contact else from_user_id,
            sender_is_bot=False,
            text=text,
            media_type=media_types[0] if media_types else None,
            media_path=media_paths[0] if media_paths else None,
            date=_parse_timestamp(raw),
            grouped_id=None,
            media_paths=media_paths,
            media_types=media_types,
            media_filenames=media_filenames,
            account_id=self.account_id,
            account_user_id=None,
            account_username=self.account_user_id,
            source_platform=PLATFORM_WEIXIN,
        )

    @staticmethod
    def _text_from_items(items: list[Any]) -> str:
        parts: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = _as_int(item.get("type"))
            if item_type == int(WeixinItemType.TEXT):
                text = str((item.get("text_item") or {}).get("text") or "").strip()
                if text:
                    parts.append(text)
                continue
            if item_type == int(WeixinItemType.VOICE):
                voice_text = str((item.get("voice_item") or {}).get("text") or "").strip()
                parts.append(voice_text or ITEM_PLACEHOLDERS[WeixinItemType.VOICE])
                continue
            try:
                placeholder = ITEM_PLACEHOLDERS.get(WeixinItemType(item_type or 0))
            except ValueError:
                placeholder = None
            if placeholder:
                parts.append(placeholder)
        return "\n".join(parts).strip()

    def _is_duplicate(self, message_id: str) -> bool:
        now = time.monotonic()
        while self._recent_ids:
            oldest_id, seen_at = next(iter(self._recent_ids.items()))
            if now - seen_at <= 300.0 and len(self._recent_ids) < RECENT_MESSAGE_LIMIT:
                break
            self._recent_ids.pop(oldest_id, None)
        if message_id in self._recent_ids:
            return True
        self._recent_ids[message_id] = now
        return False

    # ---------------------------------------------------------------- 出站

    async def send(self, outbound: WeixinOutboundMessage) -> None:
        """发送消息；token 失效且开启了挂起补发时，消息会排队等 token 刷新。"""
        try:
            await self._send_now(outbound)
        except (WeixinMissingToken, WeixinApiError) as exc:
            if self._can_park(exc) and self._queue_pending(outbound):
                raise WeixinPending(
                    f"微信 token 暂不可用，消息已挂起，等对方发消息后自动补发：{exc}"
                ) from exc
            raise

    def _can_park(self, exc: WeixinError) -> bool:
        if not self.settings.weixin_pending_send_enabled:
            return False
        if isinstance(exc, WeixinMissingToken):
            return True
        return isinstance(exc, WeixinApiError) and exc.errcode == PREPARE_FAILED_ERRCODE

    async def _send_now(self, outbound: WeixinOutboundMessage) -> None:
        if not self.logged_in or self.client is None:
            raise WeixinError("微信未登录，无法发送消息")
        target_user_id = (outbound.target_user_id or "").strip()
        if not target_user_id:
            raise WeixinError("微信目标用户为空")
        context_token = self.state.context_tokens.get(target_user_id)
        if not context_token:
            raise WeixinMissingToken(
                "缺少该联系人的 context_token：微信协议要求对方先给本微信号发一条消息，"
                "才能回复他（收到消息后会自动缓存）"
            )

        text = (outbound.text or "").strip()
        max_chars = max(50, int(self.settings.weixin_max_text_chars))
        delivered = False
        for chunk in split_text(text, max_chars):
            await self._send_items(
                to_user_id=target_user_id,
                item_list=[build_text_item(chunk)],
                context_token=context_token,
                label="文本",
                # 前面已经有内容发出去了，说明 token 没问题，失败更可能是限流/内容问题。
                mark_stale=not delivered,
            )
            delivered = True

        media_paths = [path for path in outbound.media_paths if path and Path(path).exists()]
        if media_paths and self.settings.weixin_send_media:
            media_types: list[str | None] = [
                outbound.media_type_at(index) for index in range(len(media_paths))
            ]
            items = await self.media.build_outbound_items(
                self.client,
                target_user_id=target_user_id,
                media_paths=media_paths,
                media_types=media_types,
                file_names=[
                    outbound.media_filename_at(index) for index in range(len(media_paths))
                ],
                caption=outbound.media_caption,
            )
            if media_paths and not items:
                raise WeixinError("媒体上传全部失败，本次没有发出任何媒体")
            failures: list[str] = []
            for index, item in enumerate(items):
                try:
                    await self._send_items(
                        to_user_id=target_user_id,
                        item_list=[item],
                        context_token=context_token,
                        label=f"媒体 {index + 1}/{len(items)}",
                        mark_stale=not delivered,
                    )
                    delivered = True
                except WeixinSessionExpired:
                    raise
                except WeixinError as exc:
                    # 单个媒体失败不影响后续媒体，最后统一汇报。
                    failures.append(f"媒体 {index + 1}/{len(items)}: {exc}")
                    logger.warning(
                        "Weixin media send failed target=%s index=%s",
                        target_user_id,
                        index + 1,
                        exc_info=True,
                    )
            if failures:
                raise WeixinError(
                    "部分内容发送失败（其余内容已发送）：" + "；".join(failures)
                )
        self._outbound_total += 1

    async def _send_items(
        self,
        *,
        to_user_id: str,
        item_list: list[dict[str, Any]],
        context_token: str,
        label: str,
        mark_stale: bool = True,
    ) -> None:
        """单次 sendmessage：节流 + 对 errcode=-2 做退避重试。

        ilink 对连续发送会返回 ``errcode=-2 prepare failed``，它既可能是限流，
        也可能是 ``context_token`` 过期，光看错误码分不出来，所以先重试几次，
        仍然失败时在错误信息里给出可操作提示。

        每次重试都会重新读取 ``context_token``：退避期间对方如果刚好发来消息，
        新 token 就已经落库，可以直接用新的再试一次。
        """
        client = self.client
        if client is None:
            raise WeixinError("微信未登录，无法发送消息")
        delays = [max(0.0, float(item)) for item in self.settings.weixin_send_retry_delays_seconds]
        last_error: WeixinApiError | None = None
        for attempt in range(len(delays) + 1):
            await self._wait_send_slot()
            token = self.state.context_tokens.get(to_user_id) or context_token
            try:
                data = await client.send_message(
                    to_user_id=to_user_id,
                    item_list=item_list,
                    context_token=token,
                )
                self._remember_context_token(to_user_id, data)
                return
            except WeixinSessionExpired:
                raise
            except WeixinApiError as exc:
                if exc.errcode != PREPARE_FAILED_ERRCODE:
                    raise
                last_error = exc
                if attempt >= len(delays):
                    break
                delay = delays[attempt]
                logger.warning(
                    "Weixin sendmessage returned -2, retrying in %.1fs label=%s attempt=%s",
                    delay,
                    label,
                    attempt + 1,
                )
                await asyncio.sleep(delay)
        if mark_stale:
            self._mark_token_stale(to_user_id)
        raise WeixinApiError(
            f"{last_error}（{label}；-2 通常是限流或 context_token 失效，已重试 {len(delays)} 次。"
            "持续失败时请让该联系人在微信里给本号发一条消息，刷新 token 后再试）",
            errcode=PREPARE_FAILED_ERRCODE,
        )

    def _remember_context_token(self, user_id: str, payload: dict[str, Any] | None) -> None:
        """sendmessage 的响应如果带了新的 context_token，就顺手更新，减少后续失效。"""
        if not isinstance(payload, dict):
            return
        candidates: list[Any] = [payload.get("context_token")]
        for container_key in ("msg", "data"):
            container = payload.get(container_key)
            if isinstance(container, dict):
                candidates.append(container.get("context_token"))
                nested = container.get("msg")
                if isinstance(nested, dict):
                    candidates.append(nested.get("context_token"))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                token = candidate.strip()
                if self.state.context_tokens.get(user_id) != token:
                    self.state.context_tokens[user_id] = token
                    self._touch_token(user_id)
                return

    def _touch_token(self, user_id: str) -> None:
        contact = self.state.contacts.get(user_id) or WeixinContact(user_id=user_id)
        contact.has_context_token = True
        contact.token_updated_at = time.time()
        contact.token_stale = False
        self.state.contacts[user_id] = contact
        self._persist()

    def _mark_token_stale(self, user_id: str) -> None:
        contact = self.state.contacts.get(user_id)
        if contact is None:
            return
        contact.token_stale = True
        self._persist()

    # ---------------------------------------------------------------- 挂起补发

    def _queue_pending(self, outbound: WeixinOutboundMessage) -> bool:
        """token 失效时把消息挂起，等对方下次发消息（token 刷新）后补发。"""
        if not self.settings.weixin_pending_send_enabled:
            return False
        user_id = (outbound.target_user_id or "").strip()
        if not user_id:
            return False
        limit = max(1, int(self.settings.weixin_pending_send_max_per_contact))
        queue = self._pending.setdefault(user_id, [])
        if len(queue) >= limit:
            dropped = queue.pop(0)
            logger.warning(
                "Weixin pending queue full (%s), drop oldest user=%s text=%r",
                limit,
                user_id,
                (dropped.outbound.text or "")[:40],
            )
        queue.append(PendingWeixinMessage(outbound=outbound))
        logger.info(
            "Queued Weixin message until token refresh user=%s pending=%s", user_id, len(queue)
        )
        return True

    async def _flush_pending(self, user_id: str) -> None:
        """尝试补发某联系人的挂起消息；token 仍不可用时原样保留。"""
        async with self._flush_lock:
            items = list(self._pending.get(user_id) or [])
            if not items:
                self._pending.pop(user_id, None)
                return
            ttl = max(0, int(self.settings.weixin_pending_send_ttl_seconds))
            now = time.time()
            kept: list[PendingWeixinMessage] = []
            delivered = 0
            for index, item in enumerate(items):
                if ttl and now - item.created_at > ttl:
                    logger.warning(
                        "Weixin pending message expired user=%s age=%ss",
                        user_id,
                        int(now - item.created_at),
                    )
                    continue
                item.attempts += 1
                try:
                    await self._send_now(item.outbound)
                except (WeixinSessionExpired, WeixinMissingToken):
                    kept.extend(items[index:])
                    break
                except WeixinApiError as exc:
                    if exc.errcode == PREPARE_FAILED_ERRCODE:
                        # token 还是不行，后面的也不用试了，留到下次。
                        kept.extend(items[index:])
                        break
                    logger.warning("Drop pending Weixin message user=%s: %s", user_id, exc)
                    continue
                except WeixinError as exc:
                    logger.warning("Drop pending Weixin message user=%s: %s", user_id, exc)
                    continue
                delivered += 1
            if kept:
                self._pending[user_id] = kept
            else:
                self._pending.pop(user_id, None)
            if delivered:
                logger.info(
                    "Flushed %s pending Weixin message(s) user=%s remaining=%s",
                    delivered,
                    user_id,
                    len(kept),
                )

    def _schedule_flush(self, user_id: str) -> None:
        if not self._pending.get(user_id):
            return
        task = asyncio.create_task(
            self._flush_pending(user_id), name=f"weixin-flush-{user_id}"
        )
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    async def _pending_loop(self) -> None:
        """定期尝试补发：覆盖「限流导致的 -2」这种不需要等对方发消息的情况。"""
        interval = max(15.0, float(self.settings.weixin_pending_send_retry_seconds))
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            for user_id in list(self._pending.keys()):
                try:
                    await self._flush_pending(user_id)
                except Exception:
                    logger.exception("Failed to flush pending Weixin messages user=%s", user_id)

    async def _wait_send_slot(self) -> None:
        """保证两条 sendmessage 之间至少间隔 ``weixin_send_min_interval_seconds``。"""
        interval = max(0.0, float(self.settings.weixin_send_min_interval_seconds))
        if interval <= 0:
            return
        async with self._send_lock:
            loop = asyncio.get_running_loop()
            wait = self._last_send_at + interval - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send_at = loop.time()

    # ---------------------------------------------------------------- 内部工具

    def _default_client_factory(self, credentials: WeixinCredentials | None) -> WeixinIlinkClient:
        return WeixinIlinkClient(
            base_url=credentials.base_url if credentials else self.settings.weixin_base_url,
            cdn_base_url=self.settings.weixin_cdn_base_url,
            token=credentials.token if credentials else None,
            api_timeout_seconds=self.settings.weixin_api_timeout_seconds,
        )

    def _ensure_client(self) -> None:
        if self.client is None:
            self.client = self._client_factory(self.state.credentials)

    def _persist(self) -> None:
        self.store.save(self.state)


def split_text(text: str, max_chars: int) -> list[str]:
    """按长度切分文本，尽量在换行处断开。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = window.rfind("\n")
        if cut < max_chars // 2:
            cut = max_chars
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining.strip():
        chunks.append(remaining.strip())
    return [chunk for chunk in chunks if chunk]


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _message_id_value(message_id: str) -> int | str:
    """纯数字的消息 ID 存成 int，其余（uuid 形式）保持字符串。"""
    text = (message_id or "").strip()
    if text.isdigit() and len(text) <= 18:
        return int(text)
    return text or int(time.time() * 1000)


def _parse_timestamp(raw: dict[str, Any]) -> datetime | None:
    value = raw.get("create_time_ms") or raw.get("create_time") or raw.get("timestamp")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e12:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None
