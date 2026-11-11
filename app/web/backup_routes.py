from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import aiofiles
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.backup import BackupError, FullBackupService
from app.web.errors import api_error

logger = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


class RestoreRequest(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    confirm: bool = False


def create_backup_router(
    service: FullBackupService, require_admin_session, restart_callback: Callable | None = None
) -> APIRouter:
    router = APIRouter(prefix="/api/system/backups", tags=["backups"])
    operation = asyncio.Lock()

    async def run(function, *args, on_cancel=None):
        if operation.locked():
            raise api_error(409, "backup_busy", "备份操作正在进行，请稍后重试")
        async with operation:
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A disconnected browser must not leave an unobserved file mutation running.
                result = await task
                if on_cancel is not None:
                    await asyncio.to_thread(on_cancel, result)
                raise
            except BackupError as error:
                raise api_error(error.status_code, "backup_error", str(error)) from None
            except Exception:
                logger.error("Backup operation failed", exc_info=True)
                raise api_error(
                    500, "backup_failed", "备份操作失败，请检查磁盘空间和文件权限"
                ) from None

    @router.get("")
    async def status(session=Depends(require_admin_session)):
        return await run(service.status) | {"automatic_restart": restart_callback is not None}

    @router.post("/download")
    async def download(session=Depends(require_admin_session)):
        archive = await run(service.create, on_cancel=service.remove_private)
        return FileResponse(
            archive,
            filename="tgqq-full-backup.zip",
            media_type="application/zip",
            headers=_NO_STORE,
            background=BackgroundTask(service.remove_private, archive),
        )

    @router.post("/inspect")
    async def inspect(request: Request, session=Depends(require_admin_session)):
        if operation.locked():
            raise api_error(409, "backup_busy", "备份操作正在进行，请稍后重试")
        maximum = service.settings.backup_max_archive_mb * 1024 * 1024
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise api_error(400, "invalid_length", "上传长度无效") from None
        if length < 0 or length > maximum:
            raise api_error(413, "backup_too_large", "备份文件超过允许大小")
        token, archive = service.new_upload()
        success = False
        try:
            async with operation:
                size = 0
                async with aiofiles.open(archive, "xb") as output:
                    archive.chmod(0o600)
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > maximum:
                            raise api_error(413, "backup_too_large", "备份文件超过允许大小")
                        await output.write(chunk)
            summary = await run(service.inspect, token, session.user.id)
            success = True
            return summary
        finally:
            if not success:
                await asyncio.to_thread(service.remove_private, archive)
                await asyncio.to_thread(service.remove_private, archive.with_suffix(".json"))

    @router.post("/restore", status_code=202)
    async def restore(payload: RestoreRequest, session=Depends(require_admin_session)):
        if not payload.confirm:
            raise api_error(400, "restore_confirmation_required", "请先确认覆盖当前全部数据")
        summary = await run(service.schedule_restore, payload.id, session.user.id)
        return JSONResponse(
            status_code=202,
            content={
                "scheduled": True,
                "automatic_restart": restart_callback is not None,
                "message": "备份已验证，服务将重启并还原"
                if restart_callback
                else "备份已验证，请重启服务完成还原",
                "backup": summary,
            },
            headers=_NO_STORE,
            background=BackgroundTask(restart_callback) if restart_callback else None,
        )

    @router.get("/rollback/{token}")
    async def rollback_download(token: str, session=Depends(require_admin_session)):
        path = await run(service.rollback_archive, token)
        return FileResponse(
            path,
            filename="tgqq-before-restore.zip",
            media_type="application/zip",
            headers=_NO_STORE,
        )

    return router
