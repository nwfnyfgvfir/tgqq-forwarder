from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from app.backup import BackupError, DataDirectoryLock, FullBackupService, cli
from app.config import RESTORED_SETTINGS_FILENAME, Settings, clear_settings_cache, get_settings
from app.storage.db import Database
from app.storage.models import AppSetting, ForwardRule, RssEntry, RssSubscription


def isolated_settings(base: Path, **overrides) -> Settings:
    data = base / "data"
    values = {
        "_env_file": None,
        "data_dir": data,
        "log_dir": data / "logs",
        "media_dir": data / "media",
        "telegram_sessions_dir": base / "sessions",
        "telegram_session_path": base / "sessions" / "account.session",
        "weixin_state_dir": base / "weixin",
        "database_url": f"sqlite+aiosqlite:///{data / 'app.db'}",
        "rss_translate_api_key": "source-ai-secret",
        "qq_ai_moderation_transcribe_api_key": "source-speech-secret",
        "qq_bot_secret": "source-qq-secret",
        "admin_telegram_user_ids": [1, 2],
    }
    values.update(overrides)
    settings = Settings(**values)
    settings.ensure_runtime_dirs()
    return settings


@pytest.fixture
async def populated(tmp_path):
    config = isolated_settings(tmp_path / "source")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("QQ_BOT_SECRET=original-env-secret\n", encoding="utf-8")
    db = Database(config.database_url)
    await db.init()
    async with db.session() as session:
        session.add(
            ForwardRule(
                name="all-targets",
                qq_target_id="group",
                message_template="{text}",
                qq_skip_keywords=["blocked"],
            )
        )
        sub = RssSubscription(
            name="feed",
            feed_url="https://example.com/feed",
            tg_chat_id="-100",
            message_template="{title}",
        )
        session.add(sub)
        await session.flush()
        session.add(
            RssEntry(
                subscription_id=sub.id,
                entry_key="entry",
                title="saved entry",
                link="https://example.com/a",
                summary="summary",
                status="sent",
            )
        )
        session.add(AppSetting(key="forwarding_paused", value="true"))
    (config.media_dir / "image.bin").write_bytes(b"original media")
    (config.log_dir / "app.log").write_text("original log\n", encoding="utf-8")
    (config.data_dir / "empty-folder").mkdir()
    (config.weixin_state_dir / "credentials.json").write_text('{"token":"weixin-secret"}')
    with closing(sqlite3.connect(config.telegram_session_path)) as connection:
        connection.execute("CREATE TABLE sessions (value TEXT)")
        connection.execute("INSERT INTO sessions VALUES ('telegram-session-secret')")
        connection.commit()
    try:
        yield config, FullBackupService(config, project_dir=project), db
    finally:
        await db.dispose()
        clear_settings_cache()


def staged(service: FullBackupService, source: Path, owner=1) -> str:
    token, path = service.new_upload()
    shutil.copyfile(source, path)
    service.inspect(token, owner)
    service.schedule_restore(token, owner)
    return token


def change_archive(source: Path, destination: Path, change) -> Path:
    with ZipFile(source) as bundle:
        files = {name: bundle.read(name) for name in bundle.namelist()}
    change(files)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as bundle:
        for name, value in files.items():
            bundle.writestr(name, value)
    return destination


def replace_checked_file(files: dict, name: str, content: bytes) -> None:
    files[name] = content
    manifest = json.loads(files["manifest.json"])
    manifest["files"][name] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    files["manifest.json"] = json.dumps(manifest).encode()


async def test_full_backup_round_trip_migrates_all_persisted_state_and_keeps_rollback(
    populated,
    tmp_path,
    monkeypatch,
) -> None:
    config, source, _db = populated
    archive = source.create()
    manifest, config_snapshot = source.validate(archive)
    with ZipFile(archive) as bundle:
        assert b"source-ai-secret" in bundle.read("config.json")
        assert b"source-speech-secret" in bundle.read("config.json")
        assert b"original-env-secret" in bundle.read("environment.env")
        assert not any(".backup/" in name for name in bundle.namelist())
        assert "data/empty-folder" in manifest["directories"]

    target_config = isolated_settings(tmp_path / "target", qq_bot_secret="target-qq-secret")
    target = FullBackupService(target_config, project_dir=tmp_path / "target-project")
    with closing(sqlite3.connect(target_config.data_dir / "app.db")) as connection:
        connection.execute("CREATE TABLE before_restore(value TEXT)")
        connection.commit()
    extra = target_config.data_dir / "only-current.txt"
    extra.write_text("current data must be backed up")
    token, upload = target.new_upload()
    shutil.copyfile(archive, upload)
    summary = target.inspect(token, 1)
    serialized = json.dumps(summary)
    for private in ["source-ai-secret", "weixin-secret", "source-qq-secret", str(config.data_dir)]:
        assert private not in serialized
    target.schedule_restore(token, 1)
    assert extra.exists(), "scheduling alone must never overwrite active files"
    assert target.apply_pending()
    assert not extra.exists(), "restore replaces state instead of merging stale data"
    assert (target_config.data_dir / "media" / "image.bin").read_bytes() == b"original media"
    assert (target_config.data_dir / "logs" / "app.log").read_text() == "original log\n"
    assert (target_config.data_dir / "empty-folder").is_dir()
    restored = json.loads((target_config.data_dir / RESTORED_SETTINGS_FILENAME).read_text())
    with closing(sqlite3.connect(target_config.data_dir / "app.db")) as connection:
        assert connection.execute("SELECT name FROM forwardrule").fetchall() == [("all-targets",)]
        assert connection.execute("SELECT name FROM rss_subscription").fetchall() == [("feed",)]
        assert connection.execute("SELECT title FROM rss_entry").fetchall() == [("saved entry",)]
        assert connection.execute(
            "SELECT value FROM appsetting WHERE key='forwarding_paused'"
        ).fetchone() == ("true",)
    assert restored["qq_bot_secret"] == "source-qq-secret"
    assert Path(restored["weixin_state_dir"]).is_relative_to(target_config.data_dir)
    assert "weixin-secret" in (Path(restored["weixin_state_dir"]) / "credentials.json").read_text()
    accounts = json.loads(restored["telegram_accounts_json"])
    with closing(sqlite3.connect(accounts[0]["session_path"])) as connection:
        assert connection.execute("SELECT value FROM sessions").fetchone() == (
            "telegram-session-secret",
        )

    monkeypatch.setenv("DATA_DIR", str(target_config.data_dir))
    monkeypatch.setenv("QQ_BOT_SECRET", "old-container-environment")
    clear_settings_cache()
    active = get_settings()
    assert active.qq_bot_secret == "source-qq-secret"
    assert active.qq_ai_moderation_transcribe_api_key == "source-speech-secret"
    assert active.data_dir == target_config.data_dir
    rollback = target.rollback_archive(target.status()["last_restore"]["rollback_id"])
    with ZipFile(rollback) as bundle:
        assert bundle.read("data/only-current.txt") == b"current data must be backed up"
        assert b"target-qq-secret" in bundle.read("config.json")
    assert not target.apply_pending()
    assert not list(target.control.glob("restore-*"))
    assert manifest["format"] == "tgqq-full-backup"
    assert config_snapshot["database"] == "data/app.db"


async def test_sqlite_wal_sessions_are_snapshotted_consistently(populated) -> None:
    config, service, _db = populated
    with closing(sqlite3.connect(config.telegram_session_path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE sessions SET value='committed-wal-value'")
        connection.commit()
        connection.execute("UPDATE sessions SET value='uncommitted-value'")
        archive = service.create()
        connection.rollback()
    with ZipFile(archive) as bundle:
        restored_config = json.loads(bundle.read("config.json"))
        session_name = restored_config["accounts"][0]["session"]
        snapshot = service.control / "session-check.sqlite3"
        snapshot.write_bytes(bundle.read(session_name))
        with closing(sqlite3.connect(snapshot)) as connection:
            assert connection.execute("SELECT value FROM sessions").fetchone() == (
                "committed-wal-value",
            )
        snapshot.unlink()
        assert not any(name.endswith(("-wal", "-shm", "-journal")) for name in bundle.namelist())


@pytest.mark.parametrize(
    "bad_path", ["../escape", "/absolute", "C:/outside", "data/../../escape", "data\\escape"]
)
async def test_unsafe_zip_paths_are_rejected_before_any_data_change(
    populated, tmp_path, bad_path
) -> None:
    config, service, _db = populated
    archive = service.create()
    malformed = change_archive(
        archive, tmp_path / "malformed.zip", lambda files: files.update({bad_path: b"bad"})
    )
    before = (config.media_dir / "image.bin").read_bytes()
    with pytest.raises(BackupError):
        service.validate(malformed)
    assert (config.media_dir / "image.bin").read_bytes() == before
    assert not (service.control / "pending.json").exists()


@pytest.mark.parametrize(
    "corruption", ["hash", "database", "configuration", "control", "file_list", "format"]
)
async def test_corrupted_or_incompatible_backup_is_rejected(
    populated, tmp_path, corruption
) -> None:
    _config, service, _db = populated
    archive = service.create()

    def corrupt(files):
        if corruption == "hash":
            files["data/media/image.bin"] = b"changed without updating checksum"
        elif corruption == "database":
            replace_checked_file(files, "data/app.db", b"not a database")
        elif corruption == "configuration":
            config = json.loads(files["config.json"])
            config["paths"]["media_dir"] = "../outside"
            replace_checked_file(files, "config.json", json.dumps(config).encode())
        elif corruption == "control":
            replace_checked_file(files, "data/.backup/pending.json", b"{}")
        elif corruption == "file_list":
            files.pop("data/media/image.bin")
        else:
            manifest = json.loads(files["manifest.json"])
            manifest["version"] = 999
            files["manifest.json"] = json.dumps(manifest).encode()

    malformed = change_archive(archive, tmp_path / "corrupt.zip", corrupt)
    with pytest.raises(BackupError):
        service.validate(malformed)


async def test_zip_symlinks_and_case_collisions_are_rejected(populated, tmp_path) -> None:
    _config, service, _db = populated
    archive = service.create()
    for kind in ["symlink", "case_collision"]:
        malformed = tmp_path / (kind + ".zip")
        shutil.copyfile(archive, malformed)
        with ZipFile(malformed, "a") as bundle:
            if kind == "symlink":
                link = ZipInfo("data/link")
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                bundle.writestr(link, "/outside")
            else:
                bundle.writestr("DATA/MEDIA/IMAGE.BIN", b"collision")
        with pytest.raises(BackupError):
            service.validate(malformed)


async def test_size_limits_are_enforced_before_extracting(populated, tmp_path) -> None:
    _config, service, _db = populated
    archive = service.create()
    bomb = change_archive(
        archive,
        tmp_path / "compressed.zip",
        lambda files: replace_checked_file(files, "data/large.bin", b"a" * (2 * 1024 * 1024)),
    )
    service.settings.backup_max_unpacked_mb = 1
    with pytest.raises(BackupError) as error:
        service.validate(bomb)
    assert error.value.status_code == 413


async def test_restore_failure_rolls_back_all_moves_and_preserves_current_data(
    populated, monkeypatch
) -> None:
    config, service, db = populated
    archive = service.create()
    (config.media_dir / "image.bin").write_bytes(b"current version")
    sentinel = config.data_dir / "current-only.txt"
    sentinel.write_text("keep current state")
    staged(service, archive)
    await db.dispose()
    import app.backup as backup_module

    original_replace = backup_module.os.replace
    failed = False

    def fail_once(source, destination):
        nonlocal failed
        if (
            not failed
            and "payload" in Path(source).parts
            and Path(destination).parent == config.data_dir
        ):
            failed = True
            raise OSError("simulated disk failure")
        return original_replace(source, destination)

    monkeypatch.setattr(backup_module.os, "replace", fail_once)
    assert service.apply_pending() is False
    assert failed
    assert sentinel.read_text() == "keep current state"
    assert (config.media_dir / "image.bin").read_bytes() == b"current version"
    assert service.status()["last_restore"]["status"] == "failed"
    assert not (service.control / "journal.json").exists()
    assert not (service.control / "pending.json").exists()
    assert service.status()["rollback_backups"]


async def test_interrupted_restore_journal_can_recover_before_startup(populated) -> None:
    config, service, _db = populated
    service._ensure_control()
    work = service.control / "restore-interrupted"
    (work / "old").mkdir(parents=True)
    (work / "payload").mkdir()
    original = config.data_dir / "marker.txt"
    original.write_text("old value")
    saved = work / "old" / original.name
    original.replace(saved)
    staged_new = work / "payload" / "new.txt"
    staged_new.write_text("new value")
    new_target = config.data_dir / "new.txt"
    staged_new.replace(new_target)
    (service.control / "journal.json").write_text(
        json.dumps(
            {
                "work": work.name,
                "committed": False,
                "operations": [[str(original), str(saved)], [str(staged_new), str(new_target)]],
            }
        )
    )
    service.recover_interrupted()
    assert original.read_text() == "old value"
    assert not new_target.exists()
    assert not work.exists()


async def test_completed_restore_remains_successful_when_temporary_cleanup_fails(
    populated,
    monkeypatch,
) -> None:
    config, service, db = populated
    archive = service.create()
    (config.media_dir / "image.bin").write_bytes(b"current media")
    staged(service, archive)
    await db.dispose()
    original_remove = service.remove_private

    def fail_work_cleanup(path):
        if path.name.startswith("restore-") and path.is_dir():
            raise PermissionError("temporary directory is busy")
        return original_remove(path)

    monkeypatch.setattr(service, "remove_private", fail_work_cleanup)
    assert service.apply_pending() is True
    assert (config.media_dir / "image.bin").read_bytes() == b"original media"
    assert service.status()["last_restore"]["status"] == "success"
    assert not (service.control / "pending.json").exists()


async def test_running_service_lock_prevents_restore_and_staging_is_bound_to_owner(
    populated,
) -> None:
    config, service, db = populated
    archive = service.create()
    token, upload = service.new_upload()
    shutil.copyfile(archive, upload)
    service.inspect(token, 1)
    with pytest.raises(BackupError) as owner_error:
        service.schedule_restore(token, 2)
    assert owner_error.value.status_code == 403
    service.schedule_restore(token, 1)
    with DataDirectoryLock(config.data_dir):
        with pytest.raises(BackupError) as lock_error:
            service.apply_pending()
        assert lock_error.value.status_code == 409
    await db.dispose()
    assert service.apply_pending()


async def test_changed_upload_is_not_restored(populated) -> None:
    _config, service, _db = populated
    archive = service.create()
    token, upload = service.new_upload()
    shutil.copyfile(archive, upload)
    service.inspect(token, 1)
    with upload.open("ab") as output:
        output.write(b"changed")
    with pytest.raises(BackupError, match="发生变化"):
        service.schedule_restore(token, 1)


async def test_cli_export_inspect_and_restore_confirmation(
    populated, tmp_path, monkeypatch, capsys
) -> None:
    config, _service, _db = populated
    monkeypatch.setattr("app.config.get_settings", lambda: config)
    output = tmp_path / "cli-backup.zip"
    cli(["create", str(output)])
    assert output.is_file()
    cli(["inspect", str(output)])
    assert "file_count" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli(["restore", str(output)])
    assert not (config.data_dir / ".backup" / "pending.json").exists()
    cli(["restore", str(output), "--confirm"])
    assert (config.data_dir / ".backup" / "pending.json").exists()


async def test_startup_applies_pending_restore_before_opening_runtime_and_execs_after_stop(
    populated,
    monkeypatch,
) -> None:
    import app.main as main_module

    config, service, db = populated
    archive = service.create()
    (config.media_dir / "image.bin").write_bytes(b"newer current data")
    staged(service, archive)
    await db.dispose()
    events = []

    class Runtime:
        restart_requested = True

        def __init__(self):
            assert (config.media_dir / "image.bin").read_bytes() == b"original media"
            events.append("constructed")

        async def start(self):
            events.append("started")

        async def run_until_stopped(self):
            events.append("restart requested")

        async def stop(self):
            events.append("closed")

    monkeypatch.setattr(main_module, "Settings", lambda: config)
    monkeypatch.setattr(main_module, "get_settings", lambda: config)
    monkeypatch.setattr(main_module, "ApplicationRuntime", Runtime)
    monkeypatch.setattr(main_module.os, "execv", lambda executable, argv: events.append("exec"))
    await main_module.amain()
    assert events == ["constructed", "started", "restart requested", "closed", "exec"]
