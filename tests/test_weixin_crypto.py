from __future__ import annotations

import base64

import pytest

from app.weixin.crypto import (
    aes_padded_size,
    decrypt_media,
    encrypt_media,
    parse_media_aes_key,
    pkcs7_pad,
    pkcs7_unpad,
)


def test_aes_padded_size_always_adds_padding() -> None:
    assert aes_padded_size(0) == 16
    assert aes_padded_size(1) == 16
    assert aes_padded_size(15) == 16
    assert aes_padded_size(16) == 32
    assert aes_padded_size(17) == 32


def test_pkcs7_roundtrip() -> None:
    for size in (0, 1, 15, 16, 17, 100):
        raw = bytes(range(size % 256))[:size] if size <= 256 else b"x" * size
        padded = pkcs7_pad(raw)
        assert len(padded) % 16 == 0
        assert len(padded) == aes_padded_size(len(raw))
        assert pkcs7_unpad(padded) == raw


def test_pkcs7_unpad_ignores_invalid_padding() -> None:
    assert pkcs7_unpad(b"") == b""
    assert pkcs7_unpad(b"abc") == b"abc"
    assert pkcs7_unpad(b"12345678901234\x05") == b"12345678901234\x05"


def test_parse_media_aes_key_accepts_base64_raw_key() -> None:
    key = b"0123456789abcdef"
    assert parse_media_aes_key(base64.b64encode(key).decode()) == key


def test_parse_media_aes_key_accepts_base64_hex_text() -> None:
    key = b"0123456789abcdef"
    encoded = base64.b64encode(key.hex().encode()).decode()
    assert parse_media_aes_key(encoded) == key


def test_parse_media_aes_key_accepts_plain_hex() -> None:
    key = b"0123456789abcdef"
    assert parse_media_aes_key(key.hex()) == key


def test_parse_media_aes_key_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_media_aes_key("")
    with pytest.raises(ValueError):
        parse_media_aes_key("not-a-key")


def test_encrypt_decrypt_roundtrip() -> None:
    key = b"0123456789abcdef"
    raw = b"hello weixin media" * 7
    encrypted = encrypt_media(key, raw)
    assert encrypted != raw
    assert len(encrypted) % 16 == 0
    assert decrypt_media(key, encrypted) == raw
