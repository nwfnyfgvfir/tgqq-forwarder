"""二维码渲染。

真实接口返回的 ``qrcode_img_content`` 并不是图片的 base64，而是**扫码目标 URL**，
形如 ``https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=...&bot_type=3``。

所以二维码需要本地生成。这里用 ``segno``（纯 Python，无 Pillow 依赖）同时支持
PNG 落盘（给 Web 端内联显示）和终端 ASCII（给无图形界面的 VPS 扫码）。
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# PNG / JPEG / GIF / WEBP 的魔数，用来判断 base64 内容是不是真的图片。
_IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"RIFF",
)

QRCODE_SCALE = 8
QRCODE_BORDER = 2


def looks_like_url(value: str) -> bool:
    text = (value or "").strip().lower()
    return text.startswith(("http://", "https://"))


def is_image_bytes(data: bytes) -> bool:
    return bool(data) and any(data.startswith(magic) for magic in _IMAGE_MAGIC)


def decode_image_base64(value: str) -> bytes | None:
    """base64 → 图片字节；不是图片则返回 None（避免把乱码写成 .png）。"""
    content = (value or "").strip()
    if not content:
        return None
    if content.startswith("data:") and "," in content:
        content = content.split(",", 1)[1]
    try:
        binary = base64.b64decode(content, validate=False)
    except Exception:
        return None
    return binary if is_image_bytes(binary) else None


def render_qr_png(content: str, path: Path, *, scale: int = QRCODE_SCALE) -> bool:
    """把扫码内容渲染成 PNG 落盘，返回是否成功。"""
    try:
        import segno
    except ImportError:  # pragma: no cover - 依赖缺失时降级
        logger.warning("segno 未安装，无法本地生成二维码图片（pip install segno）")
        return False
    try:
        qr = segno.make(content, error="m")
        path.parent.mkdir(parents=True, exist_ok=True)
        qr.save(str(path), scale=scale, border=QRCODE_BORDER)
    except Exception:
        logger.exception("生成微信登录二维码失败")
        return False
    return True


def render_qr_terminal(content: str) -> bool:
    """在终端直接打印二维码，返回是否成功。"""
    try:
        import segno
    except ImportError:  # pragma: no cover
        return False
    try:
        segno.make(content, error="m").terminal(compact=True)
    except Exception:
        logger.debug("终端二维码渲染失败", exc_info=True)
        return False
    return True
