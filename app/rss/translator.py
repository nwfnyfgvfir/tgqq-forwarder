from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

# 翻译后端为 OpenAI 兼容的 Chat Completions 接口：POST 配置的接口地址，
# 请求体形如 {"model": ..., "messages": [...], "temperature": 0}，响应取
# choices[0].message.content。API Key 与接口地址均视为凭据，禁止记录/回显
# 请求头、请求体、响应体或完整 URL。
SAFE_ERROR = "翻译服务暂时不可用"

# 仅统计汉字区间判定中文；日文假名（U+3040–U+30FF）不属于中文，按非中文处理。
_HAN_RANGES = (
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0xF900, 0xFAFF),  # CJK 兼容汉字
    (0x20000, 0x2A6DF),  # CJK 扩展 B
)
_CHINESE_RATIO = 0.5

# 常见语言标签到提示词中可读名称的映射；未命中时直接把标签交给模型。
_TARGET_LANGUAGE_NAMES = {
    "zh": "简体中文",
    "zh-cn": "简体中文",
    "zh-hans": "简体中文",
    "zh-hant": "繁體中文",
    "zh-tw": "繁體中文",
    "zh-hk": "繁體中文",
    "en": "English",
    "ja": "日本語",
    "jp": "日本語",
    "ko": "한국어",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "ru": "Русский",
    "pt": "Português",
    "it": "Italiano",
}


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


def build_system_prompt(target: str) -> str:
    """构造翻译系统提示词；target 为语言标签，映射不到时直接使用原文标签。"""
    language = _TARGET_LANGUAGE_NAMES.get(target.strip().lower(), target.strip())
    return (
        f"你是专业翻译引擎。把用户提供的文本翻译成{language}，只输出译文本身，"
        "不要解释、不要添加引号、前后缀或任何多余内容。"
        "URL、邮箱、@用户名、#话题、数字、代码与专有名词保持原样。"
        f"若原文已经是{language}，则原样返回。"
    )


def resolve_endpoint_url(url: str) -> str:
    """把配置的接口地址解析为完整端点。

    允许填写完整端点（.../chat/completions）或基础地址（.../v1），
    后者自动补全 `/chat/completions`；带查询串的地址视为完整端点不改写。
    """
    value = url.strip()
    parts = urlsplit(value)
    if parts.query or parts.fragment:
        return value
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return value
    return value.rstrip("/") + "/chat/completions"


class AITranslator:
    """OpenAI 兼容 Chat Completions 翻译后端；不记录 URL（含 key）、请求与响应。"""

    def __init__(self, settings, *, client=None, transport=None) -> None:
        self.settings = settings
        self._transport = transport
        self._client = client
        self._endpoint: str | None = None

    def _get_client(self) -> httpx.AsyncClient:
        # 惰性创建；trust_env 保持默认 True，使容器 HTTP(S)_PROXY 生效。
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.settings.rss_translate_timeout_seconds,
                transport=self._transport,
            )
        return self._client

    def _endpoint_url(self) -> str:
        if self._endpoint is None:
            self._endpoint = resolve_endpoint_url(self.settings.rss_translate_api_url)
        return self._endpoint

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = self.settings.rss_translate_api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def _request_one(self, text: str, target: str) -> str:
        """只翻译一条文本；返回译文，结构异常时抛异常由调用方转成安全错误。"""
        response = await self._get_client().post(
            self._endpoint_url(),
            headers=self._headers(),
            json={
                "model": self.settings.rss_translate_model,
                "messages": [
                    {"role": "system", "content": build_system_prompt(target)},
                    {"role": "user", "content": text},
                ],
                "temperature": 0,
                "stream": False,
            },
        )
        response.raise_for_status()
        return self._parse(response.json())

    @staticmethod
    def _parse(data) -> str:
        """解析 Chat Completions 响应；兼容 content 为字符串或内容分片列表。"""
        if isinstance(data, dict):
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    message = first.get("message")
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, str) and content.strip():
                            return content
                        if isinstance(content, list):
                            parts = [
                                item.get("text", "")
                                for item in content
                                if isinstance(item, dict) and item.get("type") == "text"
                            ]
                            joined = "".join(part for part in parts if part)
                            if joined.strip():
                                return joined
        raise ValueError("unexpected chat completions payload")

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
                # 服务端不保证回传检测到的源语言，统一标记为 auto。
                results[index] = TranslationOutcome(language="auto", text=translated)
        return results
