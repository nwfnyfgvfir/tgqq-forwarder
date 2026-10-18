"""iLink 媒体加解密。

协议细节（与 AstrBot ``weixin_oc`` 一致）：

* 算法：AES-ECB + PKCS#7 填充，密钥长度 16 字节。
* 上传前把明文按 PKCS#7 补齐后加密，``filesize`` 上报密文长度（``aes_padded_size``）。
* ``aes_key`` 有两种传递形式：16 字节原始密钥的 base64，或 32 个十六进制字符的 base64。
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK_SIZE = 16


def aes_padded_size(size: int) -> int:
    """PKCS#7 补齐后的密文长度。"""
    remainder = size % BLOCK_SIZE
    padding = BLOCK_SIZE - remainder
    return size + padding


def pkcs7_pad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    if pad_len == 0:
        pad_len = block_size
    return data + bytes([pad_len]) * pad_len


def pkcs7_unpad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    if not data:
        return data
    pad_len = data[-1]
    if pad_len <= 0 or pad_len > block_size:
        return data
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        return data
    return data[:-pad_len]


def parse_media_aes_key(value: str) -> bytes:
    """把协议里的 ``aes_key`` 字符串还原成 16 字节密钥。

    兼容三种写法：base64(原始 16 字节)、base64(32 位 hex 文本)、直接 32 位 hex 文本。
    """
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError("empty media aes key")

    try:
        padded = normalized + "=" * (-len(normalized) % 4)
        decoded = base64.b64decode(padded, validate=False)
    except Exception:  # pragma: no cover - 只有异常输入才会走到
        decoded = b""

    if len(decoded) == 16:
        return decoded

    decoded_text = decoded.decode("ascii", errors="ignore")
    if len(decoded) == 32 and _is_hex(decoded_text):
        return bytes.fromhex(decoded_text)

    if len(normalized) == 32 and _is_hex(normalized):
        return bytes.fromhex(normalized)

    raise ValueError("unsupported media aes key format")


def _is_hex(text: str) -> bool:
    return bool(text) and all(char in "0123456789abcdefABCDEF" for char in text)


def aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    return decryptor.update(data) + decryptor.finalize()


def encrypt_media(key: bytes, raw: bytes) -> bytes:
    """明文 -> 密文（含 PKCS#7 补齐）。"""
    return aes_ecb_encrypt(key, pkcs7_pad(raw))


def decrypt_media(key: bytes, encrypted: bytes) -> bytes:
    """密文 -> 明文（去除 PKCS#7 补齐）。"""
    return pkcs7_unpad(aes_ecb_decrypt(key, encrypted))
