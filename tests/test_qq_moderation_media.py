from __future__ import annotations

import asyncio
import base64
import json
import shutil
import sys
import wave
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.channels.router import DeliveryRouter
from app.config import Settings
from app.qq_official.moderation import QQ_VIOLATION_NOTICE, AIContentModerator
from app.qq_official.moderation_media import MediaPreparer, run_media_command
from app.rules.models import TelegramForwardMessage
from app.storage.models import ForwardRule


def settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "qq_ai_moderation_enabled": True,
        "qq_ai_moderation_image_max_side": 256,
        "qq_ai_moderation_api_url": "https://ai.example/v1/chat/completions",
    }
    values.update(overrides)
    return Settings(**values)


def verdict(blocked=False) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "blocked": blocked,
                                "categories": ["sexual"] if blocked else [],
                            }
                        )
                    }
                }
            ]
        },
    )


def image(path: Path, color: str) -> Path:
    with Image.new("RGB", (640, 480), color) as pixels:
        pixels.save(path)
    return path


def decoded(data_url: str) -> Image.Image:
    pixels = Image.open(BytesIO(base64.b64decode(data_url.split(",", 1)[1])))
    pixels.load()
    return pixels


def audio(path: Path, seconds: int = 1) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000 * seconds)
    return path


def require_ffmpeg() -> None:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("FFmpeg and ffprobe are required for media integration tests")


async def video(path: Path) -> Path:
    require_ffmpeg()
    await run_media_command(
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=80x60:rate=4",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=16000",
        "-t",
        "2.5",
        "-c:v",
        "mpeg4",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    )
    return path


class QQSender:
    def __init__(self) -> None:
        self.calls = []

    async def send(self, outbound) -> None:
        self.calls.append(outbound)


def rule(**overrides) -> ForwardRule:
    return ForwardRule(
        name="rule",
        qq_target_type="group",
        qq_target_id="group",
        message_template="{text}",
        **overrides,
    )


def message(paths: list[Path], media_type="photo") -> TelegramForwardMessage:
    return TelegramForwardMessage(
        message_id=1,
        chat_id=1,
        chat_title="source",
        chat_type="channel",
        sender_id=1,
        sender_username=None,
        sender_display_name=None,
        sender_is_bot=False,
        text="ordinary caption",
        date=None,
        media_path=paths[0],
        media_type=media_type,
        media_paths=paths,
        media_types=[media_type] * len(paths),
    )


async def test_image_pixels_are_reviewed_and_changed_content_never_reuses_safe_cache(
    tmp_path,
) -> None:
    path = image(tmp_path / "same-name.png", "blue")
    seen = []

    def handle(request):
        content = json.loads(request.content)["messages"][1]["content"]
        if isinstance(content, str):
            return verdict()
        with decoded(content[1]["image_url"]["url"]) as pixels:
            assert max(pixels.size) <= 256
            red, _, blue = pixels.getpixel((0, 0))
            seen.append((red, blue))
            return verdict(red > blue)

    moderator = AIContentModerator(settings(), transport=httpx.MockTransport(handle))
    try:
        first = await moderator.check("caption", media_paths=[path])
        assert not first.blocked and not first.unavailable
        assert not (await moderator.check("caption", media_paths=[path])).blocked
        assert len(seen) == 1
        image(path, "red")
        changed = await moderator.check("caption", media_paths=[path])
        assert changed.blocked and not changed.unavailable
        assert len(seen) == 2
    finally:
        await moderator.close()


async def test_animated_images_sample_first_and_last_frames(tmp_path) -> None:
    path = tmp_path / "animation.gif"
    frames = [Image.new("RGB", (40, 40), color) for color in ["red", "green", "yellow", "blue"]]
    try:
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)
    finally:
        for frame in frames:
            frame.close()
    preparer = MediaPreparer(settings(qq_ai_moderation_max_video_frames=2))
    async with preparer.prepare(path) as prepared:
        assert len(prepared.images) == 2
        with decoded(prepared.images[0]) as first, decoded(prepared.images[1]) as last:
            assert first.getpixel((0, 0))[0] > 200
            assert last.getpixel((0, 0))[2] > 200


async def test_video_prepares_real_frames_audio_and_cleans_temporary_files(tmp_path) -> None:
    path = await video(tmp_path / "clip.mp4")
    preparer = MediaPreparer(settings(qq_ai_moderation_max_video_frames=3))
    async with preparer.prepare(path) as prepared:
        assert 1 <= len(prepared.images) <= 3
        assert len(prepared.audio_chunks) == 1
        chunk = prepared.audio_chunks[0]
        assert chunk.stat().st_size > 0
        assert len(await preparer.read_audio(chunk)) > 0
        temporary_dir = chunk.parent
    assert not temporary_dir.exists()


async def test_subsecond_video_still_produces_a_real_frame(tmp_path) -> None:
    require_ffmpeg()
    path = tmp_path / "short.mp4"
    await run_media_command(
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=80x60:r=8:d=0.25",
        "-c:v",
        "mpeg4",
        str(path),
    )
    preparer = MediaPreparer(settings())
    async with preparer.prepare(path) as prepared:
        assert len(prepared.images) == 1
        with decoded(prepared.images[0]) as pixels:
            assert pixels.getpixel((0, 0))[0] > 200


async def test_all_audio_tracks_are_extracted_and_reviewed(tmp_path) -> None:
    require_ffmpeg()
    path = tmp_path / "two-tracks.mka"
    await run_media_command(
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=16000:duration=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:sample_rate=16000:duration=1",
        "-map",
        "0:a",
        "-map",
        "1:a",
        "-c:a",
        "pcm_s16le",
        str(path),
    )
    transcripts = []

    def handle(request):
        if request.url.path.endswith("/audio/transcriptions"):
            text = "first track" if not transcripts else "restricted second track"
            transcripts.append(text)
            return httpx.Response(200, json={"text": text})
        content = json.loads(request.content)["messages"][1]["content"]
        return verdict("restricted second track" in content)

    moderator = AIContentModerator(settings(), transport=httpx.MockTransport(handle))
    try:
        decision = await moderator.check("caption", media_paths=[path])
        assert decision.blocked and not decision.unavailable
        assert transcripts == ["first track", "restricted second track"]
    finally:
        await moderator.close()


async def test_capped_video_sampling_covers_late_content_instead_of_only_the_start(
    tmp_path,
) -> None:
    require_ffmpeg()
    path = tmp_path / "red-then-blue.mp4"
    await run_media_command(
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=80x60:r=4:d=4",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=80x60:r=4:d=4",
        "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map",
        "[v]",
        "-c:v",
        "mpeg4",
        str(path),
    )
    preparer = MediaPreparer(settings(qq_ai_moderation_max_video_frames=2))
    async with preparer.prepare(path) as prepared:
        assert len(prepared.images) == 2
        assert prepared.audio_chunks == []
        with decoded(prepared.images[0]) as first, decoded(prepared.images[-1]) as last:
            assert first.getpixel((0, 0))[0] > 200
            assert last.getpixel((0, 0))[2] > 200


async def test_audio_is_fully_segmented_and_last_segment_can_block_message(tmp_path) -> None:
    require_ffmpeg()
    path = audio(tmp_path / "long-audio.wav", seconds=65)
    speech_requests = []
    contexts = []

    def handle(request):
        if request.url.path.endswith("/audio/transcriptions"):
            speech_requests.append(request)
            assert "multipart/form-data; boundary=" in request.headers["content-type"]
            assert b"gpt-transcribe" in request.content
            assert b'filename="audio.mp3"' in request.content
            text = (
                "ordinary first minute" if len(speech_requests) == 1 else "restricted final segment"
            )
            return httpx.Response(200, json={"text": text})
        content = json.loads(request.content)["messages"][1]["content"]
        contexts.append(content)
        return verdict("restricted final segment" in content)

    moderator = AIContentModerator(settings(), transport=httpx.MockTransport(handle))
    try:
        decision = await moderator.check("caption", media_paths=[path])
        assert decision.blocked and not decision.unavailable
        assert len(speech_requests) == 2
        assert "ordinary first minute" in contexts[-1]
        assert "restricted final segment" in contexts[-1]
        assert "restricted final segment" in decision.extracted_text
        await moderator.check("caption", media_paths=[path])
        assert len(speech_requests) == 2
    finally:
        await moderator.close()


async def test_video_visual_content_and_internal_audio_are_both_reviewed(tmp_path) -> None:
    path = await video(tmp_path / "clip.mp4")
    counts = {"vision": 0, "speech": 0}

    def handle(request):
        if request.url.path.endswith("/audio/transcriptions"):
            counts["speech"] += 1
            return httpx.Response(200, json={"text": "restricted spoken content"})
        content = json.loads(request.content)["messages"][1]["content"]
        if isinstance(content, list):
            counts["vision"] += 1
            return verdict()
        return verdict("restricted spoken content" in content)

    moderator = AIContentModerator(settings(), transport=httpx.MockTransport(handle))
    try:
        result = await moderator.check("ordinary caption", media_paths=[path])
        assert result.blocked and not result.unavailable
        assert counts["vision"] > 0 and counts["speech"] == 1
    finally:
        await moderator.close()


async def test_second_image_violation_blocks_entire_album_before_first_qq_send(tmp_path) -> None:
    paths = [image(tmp_path / "first.png", "blue"), image(tmp_path / "second.png", "red")]
    qq = QQSender()
    vision_calls = []

    def handle(request):
        assert not qq.calls
        content = json.loads(request.content)["messages"][1]["content"]
        if isinstance(content, str):
            return verdict()
        vision_calls.append(content)
        with decoded(content[1]["image_url"]["url"]) as pixels:
            red, _, blue = pixels.getpixel((0, 0))
            return verdict(red > blue)

    config = settings()
    router = DeliveryRouter(config, qq)
    router.qq_moderator = AIContentModerator(config, transport=httpx.MockTransport(handle))
    try:
        result = await router.deliver(rule(), message(paths), "ordinary caption")
        assert result.blocked
        assert len(vision_calls) == 2
        assert [item.text for item in qq.calls] == [QQ_VIOLATION_NOTICE]
        assert qq.calls[0].media_paths == []
    finally:
        await router.close()


@pytest.mark.parametrize("keyword", ["restricted phrase", "never present"])
async def test_transcription_survives_ai_failure_for_real_keyword_fallback(
    tmp_path, keyword
) -> None:
    require_ffmpeg()
    path = audio(tmp_path / "speech.wav")
    qq = QQSender()

    def handle(request):
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "restricted phrase spoken in the recording"})
        content = json.loads(request.content)["messages"][1]["content"]
        if "Audio transcript:" in content:
            return httpx.Response(503)
        return verdict()

    config = settings()
    router = DeliveryRouter(config, qq)
    router.qq_moderator = AIContentModerator(config, transport=httpx.MockTransport(handle))
    try:
        result = await router.deliver(
            rule(qq_skip_keywords=[keyword]), message([path], "voice"), "caption"
        )
        assert len(qq.calls) == 1
        if keyword == "restricted phrase":
            assert result.blocked
            assert qq.calls[0].text == QQ_VIOLATION_NOTICE
            assert qq.calls[0].media_paths == []
        else:
            assert not result.blocked
            assert "回落 QQ 屏蔽词检测" in result.note
            assert qq.calls[0].text == "caption"
            assert qq.calls[0].media_paths == [path]
    finally:
        await router.close()


async def test_vision_failure_is_not_cached_as_a_safe_media_result(tmp_path) -> None:
    path = image(tmp_path / "picture.png", "blue")
    vision_calls = []

    def handle(request):
        content = json.loads(request.content)["messages"][1]["content"]
        if isinstance(content, str):
            return verdict()
        vision_calls.append(request)
        return httpx.Response(400)

    moderator = AIContentModerator(settings(), transport=httpx.MockTransport(handle))
    try:
        for _ in range(2):
            result = await moderator.check("caption", media_paths=[path])
            assert result.unavailable and not result.blocked
        assert len(vision_calls) == 2
    finally:
        await moderator.close()


@pytest.mark.parametrize("kind", ["missing", "corrupt", "too_large", "too_long"])
async def test_media_preparation_failures_fall_back_to_keywords(tmp_path, kind) -> None:
    path = tmp_path / "source.dat"
    config = settings(qq_ai_moderation_max_media_mb=1, qq_ai_moderation_max_duration_seconds=1)
    if kind == "corrupt":
        require_ffmpeg()
        path.write_bytes(b"not decodable media")
    elif kind == "too_large":
        path.write_bytes(b"a" * (1024 * 1024 + 1))
    elif kind == "too_long":
        require_ffmpeg()
        audio(path, seconds=2)
    moderator = AIContentModerator(config, transport=httpx.MockTransport(lambda request: verdict()))
    try:
        result = await moderator.check("caption", media_paths=[path])
        assert result.unavailable and not result.blocked
        assert "回落 QQ 屏蔽词检测" in result.reason
    finally:
        await moderator.close()


async def test_temporary_media_is_removed_when_ai_request_times_out(tmp_path) -> None:
    require_ffmpeg()
    path = audio(tmp_path / "speech.wav")
    temporary_dirs = []

    async def handle(request):
        if request.url.path.endswith("/audio/transcriptions"):
            await asyncio.Event().wait()
        return verdict()

    moderator = AIContentModerator(
        settings(qq_ai_moderation_timeout_seconds=3), transport=httpx.MockTransport(handle)
    )
    prepare = moderator.media_preparer.prepare

    @asynccontextmanager
    async def record_temp(path):
        async with prepare(path) as prepared:
            temporary_dirs.append(prepared.audio_chunks[0].parent)
            yield prepared

    moderator.media_preparer.prepare = record_temp
    try:
        decision = await moderator.check("caption", media_paths=[path])
        assert decision.unavailable and not decision.blocked
        assert temporary_dirs
        assert all(not path.exists() for path in temporary_dirs)
    finally:
        await moderator.close()


async def test_cancelled_media_process_is_killed_and_reaped() -> None:
    task = asyncio.create_task(
        run_media_command(sys.executable, "-c", "import time; time.sleep(60)")
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)


async def test_transcriber_endpoint_and_credentials_can_be_overridden(tmp_path) -> None:
    path = tmp_path / "chunk.mp3"
    path.write_bytes(b"ID3 test audio bytes")
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"text": "spoken words"})

    moderator = AIContentModerator(
        settings(
            qq_ai_moderation_api_key="vision-key",
            qq_ai_moderation_transcribe_api_url="https://speech.example/custom?version=1",
            qq_ai_moderation_transcribe_api_key="speech-key",
            qq_ai_moderation_transcribe_model="speech-model",
        ),
        transport=httpx.MockTransport(handle),
    )
    try:
        assert await moderator._transcribe(path) == "spoken words"
        assert str(requests[0].url) == "https://speech.example/custom?version=1"
        assert requests[0].headers["authorization"] == "Bearer speech-key"
        assert b"speech-model" in requests[0].content
        assert b"vision-key" not in requests[0].content
    finally:
        await moderator.close()
