"""扫码登录态持久化。

一个微信账号对应一个目录：``<weixin_state_dir>/<account_id>/credentials.json``，
文件权限 0600（仅当前用户可读写），与 Telegram 的 session 文件同等敏感。

除 ``bot_token`` 外还会保存：

* ``get_updates_buf``：长轮询游标，重启后不会重复消费旧消息；
* ``context_tokens``：按联系人保存的会话上下文令牌，发送消息必须带上；
* ``contacts``：最近互动过的联系人，供 Web 端选择目标。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.weixin.models import (
    DEFAULT_BASE_URL,
    DEFAULT_CDN_BASE_URL,
    WeixinContact,
    WeixinCredentials,
)

logger = logging.getLogger(__name__)

STATE_FILE_NAME = "credentials.json"
CONTACT_LIMIT = 200


@dataclass(slots=True)
class WeixinState:
    credentials: WeixinCredentials | None = None
    get_updates_buf: str = ""
    context_tokens: dict[str, str] = field(default_factory=dict)
    contacts: dict[str, WeixinContact] = field(default_factory=dict)


class WeixinStateStore:
    def __init__(self, root: Path, account_id: str) -> None:
        self.root = Path(root) / account_id
        self.path = self.root / STATE_FILE_NAME
        self.account_id = account_id

    def load(self) -> WeixinState:
        if not self.path.exists():
            return WeixinState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception("Failed to read Weixin credentials at %s", self.path)
            return WeixinState()
        if not isinstance(raw, dict):
            return WeixinState()

        token = str(raw.get("token") or "").strip()
        credentials = None
        if token:
            credentials = WeixinCredentials(
                token=token,
                account_id=str(raw.get("account_id") or ""),
                user_id=str(raw.get("user_id") or ""),
                base_url=str(raw.get("base_url") or DEFAULT_BASE_URL),
                cdn_base_url=str(raw.get("cdn_base_url") or DEFAULT_CDN_BASE_URL),
            )

        context_tokens: dict[str, str] = {}
        raw_tokens = raw.get("context_tokens")
        if isinstance(raw_tokens, dict):
            for key, value in raw_tokens.items():
                if isinstance(key, str) and isinstance(value, str) and value:
                    context_tokens[key] = value

        contacts: dict[str, WeixinContact] = {}
        raw_contacts = raw.get("contacts")
        if isinstance(raw_contacts, dict):
            for key, value in raw_contacts.items():
                if not isinstance(key, str) or not key:
                    continue
                item = value if isinstance(value, dict) else {}
                last_seen = item.get("last_seen")
                token_updated_at = item.get("token_updated_at")
                contacts[key] = WeixinContact(
                    user_id=key,
                    name=str(item.get("name") or "") or None,
                    last_seen=float(last_seen) if isinstance(last_seen, (int, float)) else 0.0,
                    has_context_token=key in context_tokens,
                    token_updated_at=(
                        float(token_updated_at)
                        if isinstance(token_updated_at, (int, float))
                        else 0.0
                    ),
                    token_stale=bool(item.get("token_stale")),
                )

        return WeixinState(
            credentials=credentials,
            get_updates_buf=str(raw.get("get_updates_buf") or ""),
            context_tokens=context_tokens,
            contacts=contacts,
        )

    def save(self, state: WeixinState) -> None:
        payload: dict[str, object] = {
            "account_id": state.credentials.account_id if state.credentials else self.account_id,
            "user_id": state.credentials.user_id if state.credentials else "",
            "base_url": state.credentials.base_url if state.credentials else DEFAULT_BASE_URL,
            "cdn_base_url": state.credentials.cdn_base_url
            if state.credentials
            else DEFAULT_CDN_BASE_URL,
            "token": state.credentials.token if state.credentials else "",
            "get_updates_buf": state.get_updates_buf,
            "context_tokens": dict(state.context_tokens),
            "contacts": {
                user_id: {
                    "name": contact.name,
                    "last_seen": contact.last_seen,
                }
                for user_id, contact in _recent_contacts(state.contacts).items()
            },
            "saved_at": time.time(),
        }
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _restrict_permissions(tmp_path)
            os.replace(tmp_path, self.path)
            _restrict_permissions(self.path)
        except OSError:
            logger.exception("Failed to persist Weixin credentials to %s", self.path)

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to remove Weixin credentials at %s", self.path)


def _recent_contacts(contacts: dict[str, WeixinContact]) -> dict[str, WeixinContact]:
    if len(contacts) <= CONTACT_LIMIT:
        return contacts
    ordered = sorted(contacts.items(), key=lambda item: item[1].last_seen, reverse=True)
    return dict(ordered[:CONTACT_LIMIT])


def _restrict_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows 上 chmod 语义有限，忽略即可
        pass
