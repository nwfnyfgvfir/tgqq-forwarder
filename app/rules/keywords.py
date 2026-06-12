from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Sequence

_KEYWORDS_COMMENT_PREFIX = "tgqq-keywords:"
_KEYWORDS_COMMENT_RE = re.compile(r"^\(\?#tgqq-keywords:([A-Za-z0-9_\-=]+)\)")
_KEYWORD_SPLIT_RE = re.compile(r"[,，;；]")


def split_keyword_args(values: Sequence[str]) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in _KEYWORD_SPLIT_RE.split(value):
            keyword = part.strip()
            if keyword and keyword not in seen:
                seen.add(keyword)
                keywords.append(keyword)
    return keywords


def keywords_to_text_include_regex(keywords: Sequence[str]) -> str:
    cleaned = [keyword for keyword in keywords if keyword]
    payload = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode()
    alternatives = "|".join(re.escape(keyword) for keyword in cleaned)
    return f"(?#{_KEYWORDS_COMMENT_PREFIX}{encoded})(?i:(?:{alternatives}))"


def keywords_from_text_include_regex(pattern: str | None) -> list[str]:
    if not pattern:
        return []
    match = _KEYWORDS_COMMENT_RE.match(pattern)
    if not match:
        return []
    try:
        raw = base64.urlsafe_b64decode(match.group(1).encode())
        decoded = json.loads(raw.decode())
    except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, str) and item]
