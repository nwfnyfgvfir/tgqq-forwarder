from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# 翻译后端为 DeepLX：POST 配置的 URL，JSON body 形如
# {"text": "<单条文本>", "source_lang": "auto", "target_lang": "ZH"}。
# URL 形如 https://api.deeplx.org/<apikey>/translate，key 在 URL 路径中，属凭据，
# 禁止记录/回显该 URL、请求体与响应体。
SAFE_ERROR = "翻译服务暂时不可用"

# 仅统计汉字区间判定中文；日文假名（U+3040–U+30FF）不属于中文，按非中文处理。
_HAN_RANGES = (
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0xF900, 0xFAFF),  # CJK 兼容汉字
    (0x20000, 0x2A6DF),  # CJK 扩展 B
)
_CHINESE_RATIO = 0.5


@dataclass(slots=True)
class TranslationOutcome:
    """单条文本的翻译结果；跳过或失败时 text 为 None，error 为安全文案。"""

    language: str | None = None
    text: str | None = None
    skipped: bool = False
    error: str | None = None


def detect_is_chinese(text: str) -> bool:
    """本地离线判定：汉字占非空白字符比例 >= 0.5 视为中文（假名不计入汉字）。"""
    han = 0
    non_space = 0
    for char in text:
        if char.isspace():
            continue
        non_space += 1
        code = ord(char)
        if any(low <= code <= high for low, high in _HAN_RANGES):
            han += 1
    if non_space == 0:
        return False
    return han / non_space >= _CHINESE_RATIO


class DeepLXTranslator:
    """DeepLX 翻译后端；不记录 URL（含 key）、原文与响应，整体失败由调用方 fail-open。"""

    def __init__(self, settings, *, client=None, transport=None) -> None:
        self.settings = settings
        self._transport = transport
        self._client = client

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

    async def _request_one(self, text: str, target: str) -> str:
        """只翻译一条文本；返回译文，结构异常时抛异常由调用方转成安全错误。"""
        response = await self._get_client().post(
            self.settings.rss_translate_api_url,
            headers={"Content-Type": "application/json"},
            json={"text": text, "source_lang": "auto", "target_lang": target},
        )
        response.raise_for_status()
        return self._parse(response.json())

    @staticmethod
    def _parse(data) -> str:
        """解析 DeepLX 响应；兼容 {"code":200,"data":...}、translations 结构与裸字符串。"""
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            code = data.get("code")
            if code is not None and str(code) != "200":
                raise ValueError("deeplx non-200 code")
            translated = data.get("data")
            if isinstance(translated, str):
                return translated
            translations = data.get("translations")
            if isinstance(translations, list) and translations:
                first = translations[0]
                if isinstance(first, dict) and isinstance(first.get("text"), str):
                    return first["text"]
        raise ValueError("unexpected deeplx payload")

    async def translate(
        self, texts: list[str], *, target: str = "ZH"
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
            # 中文源本地判定直接跳过，不消耗翻译额度。
            if detect_is_chinese(text):
                results[index] = TranslationOutcome(language="zh", skipped=True)
                continue
            if len(text) > limit:
                text = text[:limit]
                truncated = True
            pending.append((index, text))
        if truncated:
            logger.warning("RSS 翻译文本超长，已按上限截断后翻译")
        # 逐条顺序请求，不并发；任何单条失败只影响该条，不抛给调用方。
        for index, text in pending:
            try:
                translated = await self._request_one(text, target)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 不记录异常细节（可能含含 key 的 URL 或请求/响应体），由调用方回退原文。
                logger.warning("RSS 翻译失败，按原文处理")
                results[index] = TranslationOutcome(error=SAFE_ERROR)
                continue
            if translated.strip() == text.strip():
                # 译文与原文相同（含无法翻译时粘贴原文），视为无需翻译。
                results[index] = TranslationOutcome(language="auto", skipped=True)
            else:
                # DeepLX 服务端自动检测语言但不回传语言，统一标记为 auto。
                results[index] = TranslationOutcome(language="auto", text=translated)
        return results
