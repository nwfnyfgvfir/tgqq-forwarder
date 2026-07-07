from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Sequence

from app.rules.models import iter_visible_url_spans

_KEYWORDS_COMMENT_PREFIX = "tgqq-keywords:"
_KEYWORDS_COMMENT_RE = re.compile(r"^\(\?#tgqq-keywords:([A-Za-z0-9_\-=]+)\)")
_KEYWORD_SPLIT_RE = re.compile(r"[,，;；\s]+")


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


def is_keyword_text_include_regex(pattern: str | None) -> bool:
    return bool(pattern and _KEYWORDS_COMMENT_RE.match(pattern))


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


def unique_keywords(keywords: Sequence[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        if not keyword:
            continue
        key = keyword.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(keyword)
    return cleaned


def highlight_keywords_for_markdown(text: str, keywords: Sequence[str]) -> str:
    if not text or not keywords:
        return text

    cleaned_keywords = unique_keywords(keywords)
    if not cleaned_keywords:
        return text

    keyword_re = _keyword_pattern(cleaned_keywords)

    parts: list[str] = []
    position = 0
    for start, end in iter_visible_url_spans(text):
        if position < start:
            parts.append(_highlight_segment(text[position:start], keyword_re))
        parts.append(text[start:end])
        position = end
    if position < len(text):
        parts.append(_highlight_segment(text[position:], keyword_re))
    return "".join(parts)


def detect_keyword_matches(text: str, keywords: Sequence[str]) -> list[str]:
    if not text or not keywords:
        return []
    cleaned_keywords = unique_keywords(keywords)
    if not cleaned_keywords:
        return []
    keyword_re = _keyword_pattern(cleaned_keywords)
    matched_keys: set[str] = set()

    position = 0
    for start, end in iter_visible_url_spans(text):
        if position < start:
            _collect_matches(text[position:start], keyword_re, matched_keys)
        position = end
    if position < len(text):
        _collect_matches(text[position:], keyword_re, matched_keys)

    return [keyword for keyword in cleaned_keywords if keyword.casefold() in matched_keys]


def _keyword_pattern(keywords: Sequence[str]) -> re.Pattern[str]:
    alternatives = "|".join(
        re.escape(keyword) for keyword in sorted(keywords, key=len, reverse=True)
    )
    return re.compile(alternatives, re.IGNORECASE)


def _collect_matches(segment: str, keyword_re: re.Pattern[str], matched_keys: set[str]) -> None:
    for match in keyword_re.finditer(segment):
        matched_keys.add(match.group(0).casefold())


def _highlight_segment(segment: str, keyword_re: re.Pattern[str]) -> str:
    return keyword_re.sub(lambda match: f"***{match.group(0)}***", segment)
