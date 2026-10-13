from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Edge 浏览器内置翻译的免密钥非官方端点：无 SLA，可能限流或随时变更。
AUTH_URL = "https://edge.microsoft.com/translate/auth"
TRANSLATE_URL = "https://api-edge.cognitive.microsofttranslator.com/translate"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0"
)
SAFE_ERROR = "翻译服务暂时不可用"
_EXPIRY_SKEW_SECONDS = 60.0
_FALLBACK_TOKEN_TTL_SECONDS = 300.0


class _TokenRejected(Exception):
    """token 被端点拒绝（401/403），需要刷新后重试。"""


@dataclass(slots=True)
class TranslationOutcome:
    """单条文本的翻译结果；跳过或失败时 text 为 None，error 为安全文案。"""

    language: str | None = None
    text: str | None = None
    skipped: bool = False
    error: str | None = None


def _token_expiry(token: str) -> float:
    """解析 JWT payload 的 exp；解析失败时退回保守的短期有效期。"""
    try:
        payload = token.split(".")[1]
        padding = "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload + padding))
        exp = data.get("exp")
        if isinstance(exp, (int, float)) and exp > 0:
            return float(exp)
    except Exception:
        pass
    return time.time() + _FALLBACK_TOKEN_TTL_SECONDS


class BingTranslator:
    """Edge 免密钥翻译端点；不记录 token 与原文，整体失败由调用方 fail-open。"""

    def __init__(self, settings, *, client=None, transport=None) -> None:
        self.settings = settings
        self._transport = transport
        self._client = client
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = asyncio.Lock()

    def _get_client(self) -> httpx.AsyncClient:
        # 惰性创建；trust_env 保持默认 True，使容器 HTTP(S)_PROXY 生效。
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.settings.rss_translate_timeout_seconds,
                transport=self._transport,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def _token_fresh(self) -> bool:
        return bool(self._token) and time.time() < self._token_expires_at - _EXPIRY_SKEW_SECONDS

    async def _fetch_token(self) -> str:
        response = await self._get_client().get(
            AUTH_URL, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
        )
        if response.status_code in {401, 403}:
            raise _TokenRejected
        response.raise_for_status()
        token = response.text.strip()
        if not token:
            raise ValueError("empty translate token")
        self._token = token
        self._token_expires_at = _token_expiry(token)
        return token

    async def _post(self, texts: list[str], target: str, token: str):
        response = await self._get_client().post(
            TRANSLATE_URL,
            params={"api-version": "3.0", "to": target},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            json=[{"Text": text} for text in texts],
        )
        if response.status_code in {401, 403}:
            raise _TokenRejected
        response.raise_for_status()
        return response.json()

    async def _request(self, texts: list[str], target: str):
        async with self._lock:
            try:
                token = await self._get_token()
                return await self._post(texts, target, token)
            except _TokenRejected:
                # 刷新一次后重试一次；再次失败按整体失败处理。
                token = await self._fetch_token()
                return await self._post(texts, target, token)

    async def _get_token(self) -> str:
        if self._token_fresh():
            return self._token  # type: ignore[return-value]
        return await self._fetch_token()

    @staticmethod
    def _parse(data, expected: int) -> list[TranslationOutcome]:
        if not isinstance(data, list) or len(data) != expected:
            raise ValueError("unexpected translate payload")
        outcomes: list[TranslationOutcome] = []
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("unexpected translate item")
            detected = item.get("detectedLanguage")
            language = detected.get("language") if isinstance(detected, dict) else None
            language = language if isinstance(language, str) else None
            if language is not None and language.lower().startswith("zh"):
                outcomes.append(TranslationOutcome(language=language, skipped=True))
                continue
            translations = item.get("translations")
            text = None
            if isinstance(translations, list) and translations:
                first = translations[0]
                if isinstance(first, dict) and isinstance(first.get("text"), str):
                    text = first["text"]
            if not text:
                raise ValueError("missing translate text")
            outcomes.append(TranslationOutcome(language=language, text=text))
        return outcomes

    async def translate(
        self, texts: list[str], *, target: str = "zh-Hans"
    ) -> list[TranslationOutcome]:
        limit = self.settings.rss_translate_max_chars
        results: list[TranslationOutcome] = [TranslationOutcome() for _ in texts]
        pending: list[tuple[int, str]] = []
        truncated = False
        for index, raw in enumerate(texts):
            text = raw or ""
            if not text.strip():
                results[index] = TranslationOutcome(skipped=True)
                continue
            if len(text) > limit:
                text = text[:limit]
                truncated = True
            pending.append((index, text))
        if truncated:
            logger.warning("RSS 翻译文本超长，已按上限截断后翻译")
        if not pending:
            return results
        try:
            data = await self._request([text for _, text in pending], target)
            parsed = self._parse(data, len(pending))
        except asyncio.CancelledError:
            raise
        except Exception:
            # 不记录异常细节（可能含 token 或端点 URL），由调用方回退原文。
            logger.warning("RSS 翻译失败，按原文处理")
            for index, _ in pending:
                results[index] = TranslationOutcome(error=SAFE_ERROR)
            return results
        for (index, _), outcome in zip(pending, parsed, strict=True):
            results[index] = outcome
        return results
