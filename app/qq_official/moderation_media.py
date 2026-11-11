"""Prepare real image pixels, video frames and complete audio tracks for review."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import subprocess
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import Settings


class MediaReviewError(Exception):
    """Media cannot be fully prepared within the configured review limits."""


@dataclass(slots=True)
class PreparedMedia:
    images: list[str]
    audio_chunks: list[Path]


async def _blocking[T](func: Callable[..., T], *args) -> T:
    # Wait for file handles to close before removing temporary files on cancellation.
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await task
        raise


async def run_media_command(*args: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        stdout, _stderr = await process.communicate()
    except asyncio.CancelledError:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.communicate()
        raise
    if process.returncode:
        # Decoder stderr can contain private filenames and metadata.
        raise MediaReviewError("media decoder failed")
    return stdout


def _encode_image(image: Image.Image, max_side: int) -> str:
    oriented = ImageOps.exif_transpose(image)
    try:
        oriented.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        with oriented.convert("RGB") as rgb, BytesIO() as output:
            rgb.save(output, format="JPEG", quality=85)
            return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    finally:
        oriented.close()


def _image_frames(path: Path, max_side: int, max_frames: int) -> list[str] | None:
    try:
        image = Image.open(path)
    except UnidentifiedImageError:
        return None
    with image:
        if image.width * image.height > 40_000_000:
            raise MediaReviewError("image exceeds decoded pixel limit")
        count = getattr(image, "n_frames", 1)
        selected = (
            sorted(
                {
                    round(i * (count - 1) / max(1, max_frames - 1))
                    for i in range(min(count, max_frames))
                }
            )
            if count > max_frames
            else range(count)
        )
        frames = []
        for index in selected:
            image.seek(index)
            frames.append(_encode_image(image, max_side))
        return frames


def _content_digest(paths: list[Path], max_bytes: int) -> str:
    digest = hashlib.sha256()
    for path in paths:
        if path.stat().st_size > max_bytes:
            raise MediaReviewError("media exceeds file size limit")
        item_digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                size += len(block)
                if size > max_bytes:
                    raise MediaReviewError("media exceeds file size limit")
                item_digest.update(block)
        digest.update(item_digest.digest())
    return digest.hexdigest()


class MediaPreparer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def content_digest(self, paths: list[Path]) -> str:
        return await _blocking(
            _content_digest, paths, self.settings.qq_ai_moderation_max_media_mb * 1024 * 1024
        )

    async def read_audio(self, path: Path) -> bytes:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise MediaReviewError("audio segment exceeds upload limit")
        return await _blocking(path.read_bytes)

    @asynccontextmanager
    async def prepare(self, path: Path) -> AsyncIterator[PreparedMedia]:
        settings = self.settings
        path = path.resolve(strict=True)
        images = await _blocking(
            _image_frames,
            path,
            settings.qq_ai_moderation_image_max_side,
            settings.qq_ai_moderation_max_video_frames,
        )
        if images is not None:
            yield PreparedMedia(images, [])
            return
        probe = json.loads(
            await run_media_command(
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_entries",
                "format=duration:stream=codec_type,duration",
                "-of",
                "json",
                str(path),
            )
        )
        streams = probe.get("streams", [])
        has_video = any(stream.get("codec_type") == "video" for stream in streams)
        audio_count = sum(stream.get("codec_type") == "audio" for stream in streams)
        has_audio = audio_count > 0
        if not (has_video or has_audio):
            raise MediaReviewError("unsupported media type")
        durations = [
            probe.get("format", {}).get("duration"),
            *(stream.get("duration") for stream in streams),
        ]
        duration = max(
            (float(value) for value in durations if value not in (None, "N/A")), default=0
        )
        if (
            not math.isfinite(duration)
            or not 0 < duration <= settings.qq_ai_moderation_max_duration_seconds
        ):
            raise MediaReviewError("media duration is unknown or exceeds limit")
        with TemporaryDirectory(prefix="tgqq-review-") as temp:
            directory = Path(temp)
            prefix = (
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(path),
            )
            images = []
            if has_video:
                interval = max(
                    settings.qq_ai_moderation_video_frame_interval_seconds,
                    duration / settings.qq_ai_moderation_max_video_frames,
                )
                side = settings.qq_ai_moderation_image_max_side
                frame_filter = f"scale={side}:{side}:force_original_aspect_ratio=decrease"
                if duration >= interval:
                    frame_filter = f"fps=1/{interval}:start_time=0," + frame_filter
                await run_media_command(
                    *prefix,
                    "-map",
                    "0:v:0",
                    "-an",
                    "-vf",
                    frame_filter,
                    "-q:v",
                    "3",
                    "-frames:v",
                    str(settings.qq_ai_moderation_max_video_frames if duration >= interval else 1),
                    str(directory / "frame-%05d.jpg"),
                )
                frame_paths = sorted(directory.glob("frame-*.jpg"))
                if not frame_paths:
                    # Sub-second clips may finish before the fps filter emits a sample.
                    await run_media_command(
                        *prefix,
                        "-map",
                        "0:v:0",
                        "-an",
                        "-vf",
                        f"scale={side}:{side}:force_original_aspect_ratio=decrease",
                        "-frames:v",
                        "1",
                        str(directory / "frame-first.jpg"),
                    )
                    frame_paths = sorted(directory.glob("frame-*.jpg"))
                    if not frame_paths:
                        raise MediaReviewError("video has no decodable frames")
                for frame_path in frame_paths:
                    images.extend(await _blocking(_image_frames, frame_path, side, 1) or [])
            for audio_index in range(audio_count):
                await run_media_command(
                    *prefix,
                    "-map",
                    f"0:a:{audio_index}",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "64k",
                    "-f",
                    "segment",
                    "-segment_time",
                    "60",
                    "-reset_timestamps",
                    "1",
                    str(directory / f"audio-{audio_index:03d}-%05d.mp3"),
                )
            chunks = sorted(directory.glob("audio-*.mp3"))
            if has_audio and not chunks:
                raise MediaReviewError("audio has no decodable segments")
            yield PreparedMedia(images, chunks)
