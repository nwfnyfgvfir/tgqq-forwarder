"""Portable, administrator-owned backups of all persisted application state."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from uuid import uuid4
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from sqlalchemy.engine import make_url

from app.config import RESTORED_SETTINGS_FILENAME, Settings

FORMAT = "tgqq-full-backup"
CONTROL_DIR = ".backup"
_ID = re.compile(r"[a-f0-9]{32}")
_SQLITE_HEADER = b"SQLite format 3\0"
logger = logging.getLogger(__name__)


class BackupError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class DataDirectoryLock:
    """Keep restore and a running application from opening the same persisted state."""

    def __init__(self, root: Path) -> None:
        self.path = root.resolve() / CONTROL_DIR / "runtime.lock"
        self.handle = None

    def acquire(self) -> None:
        if self.handle is not None:
            return
        if _is_link(self.path.parent) or _is_link(self.path):
            raise BackupError("数据锁路径不能是链接")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.path.open("a+b")
        os.chmod(self.path, 0o600)
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise BackupError("数据目录正被运行中的服务使用，请先停止服务", 409) from None
        self.handle = handle

    def release(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()


def _json_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\0" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or any(":" in part or part.endswith((".", " ")) for part in path.parts)
        or any(ord(char) < 32 for char in name)
    ):
        raise BackupError("备份包含不安全的文件路径")
    if os.name == "nt" and any(
        part.split(".")[0].upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"{prefix}{index}" for prefix in ("COM", "LPT") for index in range(1, 10)),
        }
        for part in path.parts
    ):
        raise BackupError("备份包含当前系统不支持的文件名")
    return path


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _sqlite_file(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as source:
        return source.read(16) == _SQLITE_HEADER


def _copy_snapshot(source: Path, destination: Path) -> None:
    if _is_link(source):
        raise BackupError("数据中包含符号链接或目录联接，请先改为真实文件")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _sqlite_file(source):
        deadline = time.monotonic() + 30

        def progress(_status, _remaining, _total):
            if time.monotonic() > deadline:
                raise BackupError("数据库持续繁忙，备份未完成，请稍后重试", 409)

        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination)) as snapshot:
                original.backup(snapshot, pages=256, progress=progress, sleep=0.05)
    else:
        shutil.copyfile(source, destination)
    os.chmod(destination, 0o600)


class FullBackupService:
    def __init__(self, settings: Settings, *, project_dir: Path | None = None) -> None:
        self.settings = settings
        self.root = settings.data_dir.resolve()
        self.control = self.root / CONTROL_DIR
        self.project_dir = (project_dir or Path.cwd()).resolve()
        if self.root == Path(self.root.anchor) or self.project_dir.is_relative_to(self.root):
            raise BackupError("DATA_DIR 必须是专用数据目录，不能是项目目录或其上级目录")

    def _ensure_control(self) -> None:
        if _is_link(self.control):
            raise BackupError("备份工作目录不能是链接")
        self.control.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.control, 0o700)

    def _private(self, path: Path) -> Path:
        path = path.resolve()
        if path == self.control.resolve() or not path.is_relative_to(self.control.resolve()):
            raise BackupError("备份工作路径超出允许范围")
        return path

    def remove_private(self, path: Path) -> None:
        path = self._private(path)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def _database_path(self) -> Path:
        url = make_url(self.settings.database_url)
        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
            raise BackupError("完整备份目前支持项目默认的文件型 SQLite 数据库")
        return Path(url.database).resolve()

    def _copy_tree(self, source: Path, destination: Path) -> None:
        if not source.exists():
            return
        if _is_link(source):
            raise BackupError("数据目录不能包含符号链接或目录联接")
        destination.mkdir(parents=True, exist_ok=True)
        for current, folders, files in os.walk(source, followlinks=False):
            current_path = Path(current)
            if source == self.root and current_path == source:
                folders[:] = [name for name in folders if name.casefold() != CONTROL_DIR]
                files = [name for name in files if name.casefold() != RESTORED_SETTINGS_FILENAME]
            for name in folders:
                if _is_link(current_path / name):
                    raise BackupError("数据目录不能包含符号链接或目录联接")
                (destination / (current_path / name).relative_to(source)).mkdir(exist_ok=True)
            for name in files:
                path = current_path / name
                # SQLite snapshots include committed WAL state, never copy live sidecars.
                if any(
                    name.endswith(suffix) and _sqlite_file(path.with_name(name[: -len(suffix)]))
                    for suffix in ("-wal", "-shm", "-journal")
                ):
                    continue
                _copy_snapshot(path, destination / path.relative_to(source))

    def _collect(self, payload: Path) -> dict:
        data = payload / "data"
        self._copy_tree(self.root, data)
        mappings: list[tuple[Path, PurePosixPath]] = [(self.root, PurePosixPath("data"))]
        external = PurePosixPath("data", "_restored_external_" + uuid4().hex[:12])

        def mapped(path: Path, label: str) -> str:
            path = path.resolve()
            for original, relative in mappings:
                if path == original or (original.is_dir() and path.is_relative_to(original)):
                    return (relative / path.relative_to(original)).as_posix()
            if self.root.is_relative_to(path) or self.project_dir.is_relative_to(path):
                raise BackupError("自定义数据路径范围过大，无法创建完整备份")
            relative = external / label
            mappings.append((path, relative))
            target = payload.joinpath(*relative.parts)
            if path.is_dir():
                self._copy_tree(path, target)
            elif path.is_file():
                _copy_snapshot(path, target)
            return relative.as_posix()

        paths = {
            name: mapped(getattr(self.settings, name), name)
            for name in type(self.settings).model_fields
            if isinstance(getattr(self.settings, name), Path)
        }
        database = self._database_path()
        if not database.is_file():
            raise BackupError("数据库文件不存在，无法创建完整备份")
        database_name = mapped(database, "database.sqlite3")
        _copy_snapshot(database, payload.joinpath(*PurePosixPath(database_name).parts))
        accounts = []
        for account in self.settings.telegram_accounts:
            accounts.append(
                {
                    "id": account.id,
                    "enabled": account.enabled,
                    "phone": account.phone,
                    "api_id": account.api_id,
                    "api_hash": account.api_hash,
                    "session": mapped(
                        account.session_path,
                        "account-"
                        + hashlib.sha256(account.id.encode()).hexdigest()[:16]
                        + ".session",
                    ),
                }
            )
        ordinary = {
            name: getattr(self.settings, name)
            for name in type(self.settings).model_fields
            if name not in {*paths, "database_url", "telegram_accounts", "telegram_accounts_json"}
        }
        config = {
            "settings": ordinary,
            "paths": paths,
            "database": database_name,
            "accounts": accounts,
        }
        _json_write(payload / "config.json", config)
        env = self.project_dir / ".env"
        if not env.is_file():
            env = self.root / ".restored.env"
        if env.is_file():
            _copy_snapshot(env, payload / "environment.env")
        return config

    def create(self, *, rollback_id: str | None = None) -> Path:
        if rollback_id is not None and not _ID.fullmatch(rollback_id):
            raise BackupError("备份编号无效")
        self._ensure_control()
        name = ("rollback-" + rollback_id if rollback_id else "export-" + uuid4().hex) + ".zip"
        archive = self.control / name
        try:
            with TemporaryDirectory(prefix="build-", dir=self.control) as temporary:
                payload = Path(temporary)
                self._configuration(self._collect(payload))
                files = {}
                total = 0
                for path in sorted(payload.rglob("*")):
                    if not path.is_file():
                        continue
                    key = path.relative_to(payload).as_posix()
                    _safe_name(key)
                    size = path.stat().st_size
                    total += size
                    if total > self.settings.backup_max_unpacked_mb * 1024 * 1024:
                        raise BackupError("数据超过 BACKUP_MAX_UNPACKED_MB，请调大上限后重试", 413)
                    files[key] = {"size": size, "sha256": _digest(path)}
                if len(files) > self.settings.backup_max_files:
                    raise BackupError("文件数量超过 BACKUP_MAX_FILES", 413)
                manifest = {
                    "format": FORMAT,
                    "version": 1,
                    "created_at": datetime.now(UTC).isoformat(),
                    "files": files,
                    "directories": [
                        path.relative_to(payload).as_posix()
                        for path in sorted(payload.rglob("*"))
                        if path.is_dir()
                    ],
                }
                with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=1) as output:
                    os.chmod(archive, 0o600)
                    output.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
                    for key in files:
                        output.write(payload.joinpath(*PurePosixPath(key).parts), key)
                if archive.stat().st_size > self.settings.backup_max_archive_mb * 1024 * 1024:
                    raise BackupError("备份超过 BACKUP_MAX_ARCHIVE_MB，请调大上限后重试", 413)
            return archive
        except BaseException:
            archive.unlink(missing_ok=True)
            raise

    def new_upload(self) -> tuple[str, Path]:
        self._ensure_control()
        self._expire_staging()
        token = uuid4().hex
        return token, self.control / ("upload-" + token + ".zip")

    def _upload(self, token: str) -> Path:
        if not _ID.fullmatch(token):
            raise BackupError("备份编号无效")
        return self.control / ("upload-" + token + ".zip")

    def _configuration(self, config: dict) -> dict:
        if not isinstance(config, dict) or set(config) != {
            "settings",
            "paths",
            "database",
            "accounts",
        }:
            raise BackupError("备份配置格式无效")
        if not isinstance(config["settings"], dict) or not isinstance(config["paths"], dict):
            raise BackupError("备份配置格式无效")
        fields = type(self.settings).model_fields
        path_fields = {name for name in fields if isinstance(getattr(self.settings, name), Path)}
        if set(config["paths"]) != path_fields or config["paths"].get("data_dir") != "data":
            raise BackupError("备份路径配置与当前版本不兼容")
        if set(config["settings"]) != set(fields) - path_fields - {
            "database_url",
            "telegram_accounts",
            "telegram_accounts_json",
        }:
            raise BackupError("备份配置与当前版本不兼容，请使用相同版本还原")

        def destination(name: str) -> str:
            relative = _safe_name(name)
            if (
                not relative.parts
                or relative.parts[0] != "data"
                or (len(relative.parts) > 1 and relative.parts[1].casefold() == CONTROL_DIR)
            ):
                raise BackupError("备份配置的路径超出数据目录")
            return str(self.root.joinpath(*relative.parts[1:]))

        effective = dict(config["settings"])
        effective.update({name: destination(value) for name, value in config["paths"].items()})
        effective["database_url"] = (
            "sqlite+aiosqlite:///" + Path(destination(config["database"])).as_posix()
        )
        accounts = []
        if not isinstance(config["accounts"], list):
            raise BackupError("备份账号配置无效")
        for account in config["accounts"]:
            if set(account) != {"id", "enabled", "phone", "api_id", "api_hash", "session"}:
                raise BackupError("备份账号配置无效")
            accounts.append(
                {key: value for key, value in account.items() if key != "session"}
                | {"session_path": destination(account["session"])}
            )
        effective["telegram_accounts_json"] = json.dumps(accounts, ensure_ascii=False)
        try:
            Settings(_env_file=None, **effective)
        except Exception:
            raise BackupError("备份中的配置未通过校验") from None
        return effective

    def validate(self, archive: Path) -> tuple[dict, dict]:
        self._ensure_control()
        if archive.stat().st_size > self.settings.backup_max_archive_mb * 1024 * 1024:
            raise BackupError("备份文件超过允许的大小", 413)
        try:
            with ZipFile(archive) as bundle:
                entries = bundle.infolist()
                if len(entries) > self.settings.backup_max_files + 1:
                    raise BackupError("备份文件数量超过上限", 413)
                names = set()
                total = 0
                for entry in entries:
                    name = _safe_name(entry.filename).as_posix()
                    if name.casefold() in names or entry.is_dir() or entry.flag_bits & 1:
                        raise BackupError("备份包含重复路径、目录条目或不支持的加密格式")
                    names.add(name.casefold())
                    kind = stat.S_IFMT(entry.external_attr >> 16)
                    if kind not in (0, stat.S_IFREG):
                        raise BackupError("备份不能包含链接或特殊文件")
                    total += entry.file_size
                    if total > self.settings.backup_max_unpacked_mb * 1024 * 1024:
                        raise BackupError("备份解压后超过允许的大小", 413)
                if bundle.getinfo("manifest.json").file_size > 16 * 1024 * 1024:
                    raise BackupError("备份清单过大")
                manifest = json.loads(bundle.read("manifest.json"))
                if (
                    not isinstance(manifest, dict)
                    or set(manifest) != {"format", "version", "created_at", "files", "directories"}
                    or manifest.get("format") != FORMAT
                    or manifest.get("version") != 1
                ):
                    raise BackupError("不是受支持的 TGQQ 完整备份")
                if datetime.fromisoformat(manifest["created_at"]).tzinfo is None:
                    raise BackupError("备份时间无效")
                files = manifest["files"]
                if not isinstance(files, dict) or set(files) | {"manifest.json"} != {
                    i.filename for i in entries
                }:
                    raise BackupError("备份清单与文件不一致")
                for name, meta in files.items():
                    relative = _safe_name(name)
                    if not (
                        name in {"config.json", "environment.env"} or relative.parts[0] == "data"
                    ):
                        raise BackupError("备份包含未允许的文件")
                    if (
                        (len(relative.parts) > 1 and relative.parts[1].casefold() == CONTROL_DIR)
                        or name.casefold() == "data/" + RESTORED_SETTINGS_FILENAME
                        or name == "data"
                    ):
                        raise BackupError("备份不能覆盖备份控制文件")
                    if not isinstance(meta, dict) or set(meta) != {"size", "sha256"}:
                        raise BackupError("备份清单格式无效")
                    if meta["size"] != bundle.getinfo(name).file_size:
                        raise BackupError("备份文件大小校验失败")
                    with bundle.open(name) as source:
                        if hashlib.file_digest(source, "sha256").hexdigest() != meta["sha256"]:
                            raise BackupError("备份文件完整性校验失败")
                directories = manifest.get("directories", [])
                if (
                    not isinstance(directories, list)
                    or len(directories) > self.settings.backup_max_files
                ):
                    raise BackupError("备份目录数量超过上限", 413)
                for name in directories:
                    relative = _safe_name(name)
                    if (
                        relative.parts[0] != "data"
                        or (len(relative.parts) > 1 and relative.parts[1].casefold() == CONTROL_DIR)
                        or name.casefold() == "data/" + RESTORED_SETTINGS_FILENAME
                    ):
                        raise BackupError("备份包含不安全的目录")
                file_keys = {name.casefold() for name in files}
                for name in [*files, *directories]:
                    if any(
                        parent.as_posix().casefold() in file_keys
                        for parent in _safe_name(name).parents
                        if parent.as_posix() != "."
                    ):
                        raise BackupError("备份的文件和目录路径冲突")
                if bundle.getinfo("config.json").file_size > 1024 * 1024:
                    raise BackupError("备份配置文件过大")
                config = json.loads(bundle.read("config.json"))
                self._configuration(config)
                if config["database"] not in files:
                    raise BackupError("备份缺少数据库")
                with TemporaryDirectory(prefix="verify-", dir=self.control) as temporary:
                    database = Path(temporary) / "snapshot.sqlite3"
                    with bundle.open(config["database"]) as source, database.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
                        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                            raise BackupError("备份数据库完整性校验失败")
                return manifest, config
        except BackupError:
            raise
        except (BadZipFile, sqlite3.Error, KeyError, ValueError, TypeError, OSError):
            raise BackupError("备份无效、损坏或与当前版本不兼容") from None

    def inspect(self, token: str, owner_id: int) -> dict:
        archive = self._upload(token)
        manifest, _config = self.validate(archive)
        summary = {
            "id": token,
            "created_at": manifest["created_at"],
            "file_count": len(manifest["files"]),
            "unpacked_bytes": sum(meta["size"] for meta in manifest["files"].values()),
            "archive_bytes": archive.stat().st_size,
            "includes": [
                "规则与全部目标",
                "RSS 订阅与历史",
                "转发日志与暂停状态",
                "全部配置及凭据",
                "Telegram 与微信登录会话",
                "已保存的媒体和日志文件",
            ],
        }
        _json_write(
            self.control / ("upload-" + token + ".json"),
            {
                "owner_id": owner_id,
                "sha256": _digest(archive),
                "summary": summary,
            },
        )
        return summary

    def schedule_restore(self, token: str, owner_id: int) -> dict:
        archive = self._upload(token)
        if (self.control / "pending.json").exists():
            raise BackupError("已有待执行的还原，请先重启完成还原", 409)
        try:
            metadata = json.loads(archive.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise BackupError("上传的备份不存在或已过期", 404) from None
        if metadata["owner_id"] != owner_id:
            raise BackupError("不能还原其他管理员上传的备份", 403)
        if time.time() - archive.stat().st_mtime > 86400:
            raise BackupError("上传的备份已过期，请重新上传", 410)
        if metadata["sha256"] != _digest(archive):
            raise BackupError("备份在校验后发生变化，请重新上传")
        self.validate(archive)
        _json_write(self.control / "pending.json", {"id": token, "sha256": metadata["sha256"]})
        return metadata["summary"]

    def _expire_staging(self) -> None:
        if (self.control / "pending.json").exists():
            return
        for path in self.control.glob("upload-*.zip"):
            if time.time() - path.stat().st_mtime > 86400:
                self.remove_private(path)
                self.remove_private(path.with_suffix(".json"))

    def _recover(self) -> None:
        journal_path = self.control / "journal.json"
        if not journal_path.exists():
            return
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        work = self._private(self.control / journal["work"])
        if not journal.get("committed"):
            for operation in reversed(journal["operations"]):
                source, destination = (Path(item) for item in operation)
                if not (source.is_relative_to(self.root) and destination.is_relative_to(self.root)):
                    raise BackupError("还原恢复日志包含非法路径，已停止启动")
                if destination.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, source)
                elif destination.exists():
                    raise BackupError("还原恢复路径发生冲突，已停止启动")
        try:
            journal_path.unlink()
            self.remove_private(work)
        except OSError:
            # Restored data is already committed (or fully rolled back). Housekeeping
            # must not report a failed restore or keep the old settings cache active.
            logger.warning("Restore completed; temporary backup cleanup will require a retry")

    def _apply(self, archive: Path, manifest: dict, config: dict) -> None:
        work = self.control / ("restore-" + uuid4().hex)
        work.mkdir(mode=0o700)
        payload, old = work / "payload", work / "old"
        payload.mkdir()
        old.mkdir()
        journal = {"work": work.name, "operations": [], "committed": False}
        journal_path = self.control / "journal.json"
        try:
            with ZipFile(archive) as bundle:
                for name in manifest.get("directories", []):
                    payload.joinpath(*_safe_name(name).parts).mkdir(parents=True, exist_ok=True)
                for name in manifest["files"]:
                    target = payload.joinpath(*_safe_name(name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(name) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    os.chmod(target, 0o600)
            data = payload / "data"
            _json_write(data / RESTORED_SETTINGS_FILENAME, self._configuration(config))
            if (payload / "environment.env").exists():
                shutil.copyfile(payload / "environment.env", data / ".restored.env")
                os.chmod(data / ".restored.env", 0o600)

            def move(source: Path, destination: Path) -> None:
                if destination.exists():
                    raise BackupError("还原目标发生冲突")
                journal["operations"].append([str(source), str(destination)])
                _json_write(journal_path, journal)
                os.replace(source, destination)

            for child in sorted(self.root.iterdir()):
                if child.name != CONTROL_DIR:
                    move(child, old / child.name)
            for child in sorted(data.iterdir()):
                move(child, self.root / child.name)
            journal["committed"] = True
            _json_write(journal_path, journal)
        except BaseException:
            if journal_path.exists():
                self._recover()
            else:
                self.remove_private(work)
            raise
        self._recover()

    def apply_pending(self) -> bool:
        """Called only before any database, sender, or login session is opened."""
        if not self.control.exists():
            return False
        if not any((self.control / name).exists() for name in ("journal.json", "pending.json")):
            return False
        with DataDirectoryLock(self.root):
            return self._apply_pending_locked()

    def recover_interrupted(self) -> None:
        if (self.control / "journal.json").exists():
            with DataDirectoryLock(self.root):
                self._recover()

    def _apply_pending_locked(self) -> bool:
        recovering = (self.control / "journal.json").exists()
        self._recover()
        pending_path = self.control / "pending.json"
        if not pending_path.exists():
            return recovering
        rollback_id = None
        try:
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            archive = self._upload(pending["id"])
            if pending["sha256"] != _digest(archive):
                raise BackupError("待还原备份完整性校验失败")
            manifest, config = self.validate(archive)
            candidate = uuid4().hex
            self.create(rollback_id=candidate)
            rollback_id = candidate
            self._apply(archive, manifest, config)
        except Exception as error:
            # Failed journal recovery must prevent opening a partially restored database.
            if (self.control / "journal.json").exists():
                raise BackupError("还原回退未完成，已停止启动，请检查数据目录") from None
            _json_write(
                self.control / "last-restore.json",
                {
                    "status": "failed",
                    "message": "还原未完成，已保留原有数据",
                    "error_kind": type(error).__name__,
                    "rollback_id": rollback_id,
                    "finished_at": datetime.now(UTC).isoformat(),
                },
            )
            pending_path.unlink(missing_ok=True)
            return recovering
        _json_write(
            self.control / "last-restore.json",
            {
                "status": "success",
                "message": "完整还原已完成",
                "rollback_id": rollback_id,
                "finished_at": datetime.now(UTC).isoformat(),
                "backup_created_at": manifest["created_at"],
            },
        )
        pending_path.unlink(missing_ok=True)
        self.remove_private(archive)
        self.remove_private(archive.with_suffix(".json"))
        return True

    def rollback_archive(self, token: str) -> Path:
        if not _ID.fullmatch(token):
            raise BackupError("备份编号无效")
        path = self.control / ("rollback-" + token + ".zip")
        if not path.is_file():
            raise BackupError("还原前备份不存在", 404)
        return path

    def status(self) -> dict:
        last = None
        if (self.control / "last-restore.json").exists():
            last = json.loads((self.control / "last-restore.json").read_text(encoding="utf-8"))
        snapshots = [
            {
                "id": path.stem.removeprefix("rollback-"),
                "bytes": path.stat().st_size,
                "created_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            }
            for path in sorted(
                self.control.glob("rollback-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            if _ID.fullmatch(path.stem.removeprefix("rollback-"))
        ]
        return {
            "pending_restore": (self.control / "pending.json").exists(),
            "last_restore": last,
            "rollback_backups": snapshots,
            "max_archive_bytes": self.settings.backup_max_archive_mb * 1024 * 1024,
        }


def cli(argv: list[str] | None = None) -> None:
    from app.config import get_settings

    parser = argparse.ArgumentParser(description="Create, inspect, or schedule a full TGQQ backup")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("output", type=Path)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("archive", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    service = FullBackupService(get_settings())
    try:
        if args.command == "create":
            output = args.output.resolve()
            if output.exists():
                raise BackupError("输出文件已存在，请选择新的备份文件名")
            archive = service.create()
            try:
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open("rb") as source, output.open("xb") as target:
                    shutil.copyfileobj(source, target)
                os.chmod(output, 0o600)
            finally:
                service.remove_private(archive)
            print(f"完整备份已保存：{output}")
        elif args.command == "inspect":
            manifest, _config = service.validate(args.archive.resolve())
            print(
                json.dumps(
                    {"created_at": manifest["created_at"], "file_count": len(manifest["files"])},
                    ensure_ascii=False,
                )
            )
        else:
            if not args.confirm:
                raise BackupError("还原会覆盖全部应用数据，请添加 --confirm 确认")
            token, upload = service.new_upload()
            shutil.copyfile(args.archive.resolve(), upload)
            os.chmod(upload, 0o600)
            service.inspect(token, 0)
            service.schedule_restore(token, 0)
            print("备份已验证并安排还原；启动或重启服务后生效。")
    except BackupError as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    cli()
