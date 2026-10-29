from __future__ import annotations

import asyncio
import logging
import re
import weakref
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.rss.formatting import is_essentially_same

logger = logging.getLogger(__name__)

# 翻译后端为 OpenAI 兼容的 Chat Completions 接口：POST 配置的接口地址，
# 请求体形如 {"model": ..., "messages": [...], "temperature": 0}，响应取
# choices[0].message.content。API Key 与接口地址均视为凭据，禁止记录/回显
# 请求头、请求体、响应体或完整 URL。
SAFE_ERROR = "翻译服务暂时不可用"

# 可重试的瞬时状态码：限流、超时与上游故障。
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
# 单次退避上限，避免上游长时间不可用时无限等待。
_MAX_BACKOFF_SECONDS = 30.0
# 尊重 Retry-After 时的等待上限。
_MAX_RETRY_AFTER_SECONDS = 60.0

# 同一事件循环内所有 AITranslator 实例共享的并发闸门：service 的预览与 worker 的
# 发送/试发各自持有实例，只有共享闸门才能避免同时压垮上游。key 为事件循环，
# 测试各自建循环时互不影响（与 RssPublisher 的 bot 限流闸门同一模式）。
_GATES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _translation_gate(limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    entry = _GATES.get(loop)
    if entry is None or entry[0] != limit:
        entry = (limit, asyncio.Semaphore(limit))
        _GATES[loop] = entry
    return entry[1]


def _is_retryable(error: Exception) -> bool:
    """仅瞬时故障可重试；4xx（鉴权、参数、模型不存在等）立即失败。"""
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in _RETRYABLE_STATUS
    return isinstance(error, httpx.TransportError)


def _retry_delay(error: Exception, attempt: int, backoff: float) -> float:
    """优先尊重上游 Retry-After，否则按指数退避。"""
    if isinstance(error, httpx.HTTPStatusError):
        header = error.response.headers.get("retry-after")
        if header:
            try:
                return max(0.0, min(_MAX_RETRY_AFTER_SECONDS, float(header)))
            except ValueError:
                pass
    return min(_MAX_BACKOFF_SECONDS, backoff * (2**attempt))


def _error_kind(error: Exception) -> str:
    """把异常归一成不含 URL/请求体/响应体的安全分类，便于排障。"""
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, httpx.TimeoutException):
        return "超时"
    if isinstance(error, httpx.TransportError):
        return "网络错误"
    return "响应格式异常"


# 仅统计汉字区间判定中文；日文假名（U+3040–U+30FF）不属于中文，按非中文处理。
_HAN_RANGES = (
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0xF900, 0xFAFF),  # CJK 兼容汉字
    (0x20000, 0x2A6DF),  # CJK 扩展 B
)
_CHINESE_RATIO = 0.5

# 判定语言前先剔除链接与 @提及/#话题，避免长 URL、版本号稀释正文的汉字比例。
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# 只删 ASCII 的 @user / #tag：`#中文标题` 这种前缀要保留给汉字计数。
_MENTION_RE = re.compile(r"[@#][A-Za-z0-9_-]+")

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


def _is_han(code: int) -> bool:
    return any(low <= code <= high for low, high in _HAN_RANGES)


def _is_kana_or_hangul(code: int) -> bool:
    return 0x3040 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7AF


def detect_is_chinese(text: str) -> bool:
    """本地离线判定：汉字占「字母类字符」的比例 >= 0.5 视为中文。

    只看汉字 / 假名 / 谚文 / 拉丁等字母字符，先剔除 URL 与 @提及：
    中文正文里夹一条长链接时，旧算法按「非空白字符」算分母，链接、数字、
    标点和 emoji 会把比例压到 0.5 以下，导致中文被送去翻译。假名不计入汉字，
    所以日文仍按非中文处理。
    """
    cleaned = _MENTION_RE.sub(" ", _URL_RE.sub(" ", text))
    han = 0
    letters = 0
    for char in cleaned:
        code = ord(char)
        if _is_han(code):
            han += 1
            letters += 1
        elif _is_kana_or_hangul(code) or char.isalpha():
            letters += 1
    if letters == 0:
        return False
    return han / letters >= _CHINESE_RATIO


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

    async def _request_one_with_retry(self, text: str, target: str) -> str:
        """单条请求带并发闸门与瞬时错误退避重试；4xx 与解析异常不重试。"""
        gate = _translation_gate(self.settings.rss_translate_max_concurrency)
        attempts = self.settings.rss_translate_max_attempts
        backoff = self.settings.rss_translate_retry_backoff_seconds
        for attempt in range(attempts):
            try:
                # 只在实际请求期间占用闸门，退避等待时不阻塞其他调用。
                async with gate:
                    return await self._request_one(text, target)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if attempt == attempts - 1 or not _is_retryable(error):
                    raise
                delay = _retry_delay(error, attempt, backoff)
                logger.warning(
                    "RSS 翻译请求失败（%s），%.1fs 后重试（第 %d/%d 次）",
                    _error_kind(error),
                    delay,
                    attempt + 2,
                    attempts,
                )
                await asyncio.sleep(delay)
        raise RuntimeError("translation retry loop exhausted")

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
        # 逐条顺序请求（并发上限由共享闸门统一控制）；任何单条失败只影响该条，不抛给调用方。
        for index, text in pending:
            try:
                translated = await self._request_one_with_retry(text, target)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # 只记录安全分类（状态码/超时/网络/格式），不含 URL、key 或请求/响应体。
                logger.warning("RSS 翻译失败（%s），按原文处理", _error_kind(error))
                results[index] = TranslationOutcome(error=SAFE_ERROR)
                continue
            if is_essentially_same(translated, text):
                # 译文与原文实质相同（模型原样返回，或只改了标点/词序）视为无需翻译。
                results[index] = TranslationOutcome(language="auto", skipped=True)
            else:
                # 服务端不保证回传检测到的源语言，统一标记为 auto。
                results[index] = TranslationOutcome(language="auto", text=translated)
        return results
