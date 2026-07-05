from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from time import time

from app.config import Settings

logger = logging.getLogger(__name__)


class MediaCleanupWorker:
    def __init__(self, settings: Settings) -> None:
        self.media_dir = settings.media_dir
        self.interval_seconds = settings.media_cleanup_interval_seconds
        self.retention_seconds = settings.media_retention_seconds
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        if self.interval_seconds <= 0 or self.retention_seconds <= 0:
            logger.info("Media cleanup disabled")
            return
        self._task = asyncio.create_task(self._run(), name="media-cleanup")
        logger.info(
            "Media cleanup started: dir=%s interval=%ss retention=%ss",
            self.media_dir,
            self.interval_seconds,
            self.retention_seconds,
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        logger.info("Media cleanup stopped")

    async def _run(self) -> None:
        await self.cleanup_once()
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                await self.cleanup_once()

    async def cleanup_once(self) -> None:
        await asyncio.to_thread(self._cleanup_sync)

    def _cleanup_sync(self) -> None:
        if not self.media_dir.exists():
            return
        cutoff = time() - self.retention_seconds
        removed_files = 0
        removed_dirs = 0
        for path in sorted(self.media_dir.rglob("*"), reverse=True):
            try:
                if path.is_file() and path.name != ".gitkeep":
                    stat = path.stat()
                    if stat.st_mtime < cutoff:
                        path.unlink()
                        removed_files += 1
                elif path.is_dir() and path != self.media_dir:
                    self._remove_empty_dir(path)
                    removed_dirs += 1
            except FileNotFoundError:
                continue
            except Exception:
                logger.warning("Failed to cleanup media path: %s", path, exc_info=True)
        if removed_files or removed_dirs:
            logger.info(
                "Media cleanup removed %s files and %s empty dirs",
                removed_files,
                removed_dirs,
            )

    @staticmethod
    def _remove_empty_dir(path: Path) -> None:
        try:
            next(path.iterdir())
        except StopIteration:
            path.rmdir()
