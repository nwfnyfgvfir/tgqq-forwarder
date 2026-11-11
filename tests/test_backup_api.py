from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from urllib.parse import urlencode

import httpx
import pytest
from test_backup import isolated_settings

from app.backup import FullBackupService
from app.rules.service import ForwardRuleService
from app.storage.db import Database
from app.web.app import create_mini_app

TOKEN = "123456:BACKUP_TEST_TOKEN"
PREFIX = "/api/system/backups"


def authentication(user_id=1) -> str:
    pairs = {"auth_date": "1700000000", "user": json.dumps({"id": user_id, "first_name": "Admin"})}
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


@pytest.fixture
async def context(tmp_path):
    config = isolated_settings(tmp_path, tg_admin_bot_token=TOKEN, mini_app_auth_ttl_seconds=0)
    db = Database(config.database_url)
    await db.init()
    (config.media_dir / "example.bin").write_bytes(b"media to back up")
    restarts = []
    app = create_mini_app(
        settings=config,
        service=ForwardRuleService(db),
        account_manager_getter=lambda: None,
        qq_status_getter=lambda: "stopped",
        qq_targets_getter=lambda: [],
        queue_status_getter=lambda: {},
        restart_callback=lambda: restarts.append(True),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.headers["X-Telegram-Init-Data"] = authentication()
        yield client, config, FullBackupService(config), restarts
    await db.dispose()


async def test_all_backup_routes_require_authenticated_administrator(context) -> None:
    client, config, _service, _restarts = context
    for identity, expected in [(None, 401), (999, 403)]:
        if identity is None:
            client.headers.pop("X-Telegram-Init-Data")
        else:
            client.headers["X-Telegram-Init-Data"] = authentication(identity)
        for method, suffix, kwargs in [
            ("GET", "", {}),
            ("POST", "/download", {}),
            ("POST", "/inspect", {"content": b"untrusted zip"}),
            ("POST", "/restore", {"json": {"id": "a" * 32, "confirm": True}}),
            ("GET", "/rollback/" + "a" * 32, {}),
        ]:
            response = await client.request(method, PREFIX + suffix, **kwargs)
            assert response.status_code == expected, response.text
    assert not list((config.data_dir / ".backup").glob("upload-*"))


async def test_download_inspect_confirm_and_restart_are_safe_and_complete(context) -> None:
    client, config, service, restarts = context
    status = await client.get(PREFIX)
    assert status.status_code == 200
    assert status.json()["automatic_restart"]
    response = await client.post(PREFIX + "/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert not list(service.control.glob("export-*.zip"))
    inspected = await client.post(
        PREFIX + "/inspect", content=response.content, headers={"Content-Type": "application/zip"}
    )
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["file_count"] > 0
    for secret in [TOKEN, "source-ai-secret", "source-qq-secret", str(config.data_dir)]:
        assert secret not in inspected.text
    token = inspected.json()["id"]
    unconfirmed = await client.post(PREFIX + "/restore", json={"id": token})
    assert unconfirmed.status_code == 400
    assert not (service.control / "pending.json").exists()
    client.headers["X-Telegram-Init-Data"] = authentication(2)
    forbidden = await client.post(PREFIX + "/restore", json={"id": token, "confirm": True})
    assert forbidden.status_code == 403
    client.headers["X-Telegram-Init-Data"] = authentication()
    restored = await client.post(PREFIX + "/restore", json={"id": token, "confirm": True})
    assert restored.status_code == 202, restored.text
    assert restarts == [True]
    assert (config.media_dir / "example.bin").read_bytes() == b"media to back up"
    assert (service.control / "pending.json").exists()
    duplicate = await client.post(PREFIX + "/restore", json={"id": token, "confirm": True})
    assert duplicate.status_code == 409


async def test_invalid_and_oversized_uploads_leave_no_partial_files(context) -> None:
    client, config, service, _restarts = context
    config.backup_max_archive_mb = 1
    malformed = await client.post(PREFIX + "/inspect", content=b"not a zip")
    assert malformed.status_code == 400
    assert not list(service.control.glob("upload-*"))

    async def chunks():
        yield b"a" * (512 * 1024)
        yield b"b" * (512 * 1024 + 1)

    too_large = await client.post(PREFIX + "/inspect", content=chunks())
    assert too_large.status_code == 413
    assert not list(service.control.glob("upload-*"))
    missing = await client.get(PREFIX + "/rollback/" + "a" * 32)
    assert missing.status_code == 404


async def test_backup_operations_are_serialized_and_cancelled_exports_are_cleaned(
    context, monkeypatch
) -> None:
    client, _config, service, _restarts = context
    archive = service.create()
    entered, release = threading.Event(), threading.Event()

    def slow_create(self):
        entered.set()
        release.wait(5)
        return archive

    monkeypatch.setattr(FullBackupService, "create", slow_create)
    pending = asyncio.create_task(client.post(PREFIX + "/download"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        busy = await client.get(PREFIX)
        assert busy.status_code == 409
        pending.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not archive.exists()
