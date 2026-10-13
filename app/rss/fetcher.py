from __future__ import annotations

import asyncio
import calendar
import hashlib
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree
from xml.parsers import expat

import feedparser
import httpx

from app.config import Settings
from app.rss.errors import RssError
from app.rss.formatting import plain_text
from app.rss.schemas import validate_http_url


@dataclass(slots=True)
class FeedEntry:
    entry_key: str
    title: str
    link: str
    summary: str
    published_at: datetime | None = None


@dataclass(slots=True)
class FeedResult:
    feed_title: str = ""
    entries: list[FeedEntry] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    warnings: list[str] = field(default_factory=list)


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def _host(value: str) -> str:
    value = value.lower().rstrip(".")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value.encode("idna").decode("ascii")


class RssFetcher:
    """逐跳验证所有 DNS 地址并固定连接 IP；不替代防火墙/独立出口强隔离。"""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str, int], Awaitable[list[str]]] | None = None,
    ) -> None:
        self.settings = settings
        self.resolver = resolver or _resolve
        self._transport = transport
        self.client = self._new_client()

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.settings.rss_request_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
            # 固定 IP 后不同域名可能共享 IP，禁止复用连接以避免 SNI/证书跨主机混用。
            limits=httpx.Limits(max_keepalive_connections=0),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def validate_url(self, url: str) -> tuple[str, str]:
        try:
            url = validate_http_url(url)
            parts = urlsplit(url)
            host = _host(parts.hostname or "")
            try:
                addresses = [str(ipaddress.ip_address(host))]
            except ValueError:
                addresses = await self.resolver(
                    host, parts.port or (443 if parts.scheme == "https" else 80)
                )
            if not addresses:
                raise RssError("rss_dns_failed", 422, "订阅主机解析失败")
            allowed = host in self.settings.rss_allowed_private_hosts
            for address in addresses:
                ip = ipaddress.ip_address(address)
                mapped = getattr(ip, "ipv4_mapped", None)
                blocked = any(
                    not candidate.is_global
                    or candidate.is_multicast
                    or candidate.is_reserved
                    or candidate.is_loopback
                    or candidate.is_link_local
                    or candidate.is_private
                    or candidate.is_unspecified
                    for candidate in ([ip, mapped] if mapped else [ip])
                )
                if not allowed and blocked:
                    raise RssError("rss_private_host", 422, "订阅地址禁止访问非公网主机")
            return host, str(ipaddress.ip_address(addresses[0]))
        except RssError:
            raise
        except (ValueError, UnicodeError, OSError):
            raise RssError("rss_invalid_url", 422, "订阅地址无效或主机解析失败") from None

    async def fetch(
        self, feed_url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FeedResult:
        if self.client.is_closed:
            self.client = self._new_client()
        try:
            async with asyncio.timeout(self.settings.rss_request_timeout_seconds):
                return await self._fetch(feed_url, etag, last_modified)
        except RssError:
            raise
        except (httpx.HTTPError, TimeoutError, OSError, ValueError):
            raise RssError("rss_fetch_failed", 502, "订阅抓取失败或超时") from None

    async def _fetch(self, url: str, etag: str | None, modified: str | None) -> FeedResult:
        headers = {
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
            "Accept-Encoding": "identity",
        }
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
        for hop in range(6):
            host, address = await self.validate_url(url)
            original = httpx.URL(url)
            # URL 使用验证后的 IP；Host/SNI 保持源主机，TLS 仍校验证书主机名。
            target = original.copy_with(host=address)
            request_headers = {**headers, "Host": original.netloc.decode("ascii")}
            async with self.client.stream(
                "GET", target, headers=request_headers, extensions={"sni_hostname": host}
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or hop == 5:
                        raise RssError("rss_redirect_limit", 502, "订阅重定向无效或次数过多")
                    next_url = urljoin(url, location)
                    if httpx.URL(next_url).netloc != original.netloc:
                        headers.pop("If-None-Match", None)
                        headers.pop("If-Modified-Since", None)
                    url = next_url
                    continue
                if response.status_code == 304:
                    return FeedResult(
                        not_modified=True,
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                    )
                if response.status_code != 200:
                    raise RssError("rss_http_error", 502, "订阅服务器返回错误状态")
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" in content_type or "application/xhtml" in content_type:
                    raise RssError("rss_invalid_feed", 422, "订阅地址返回 HTML 而非 RSS/Atom")
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding != "identity":
                    raise RssError("rss_compression_unsupported", 422, "不支持压缩订阅响应")
                length = response.headers.get("content-length")
                if length is not None:
                    if not length.isascii() or not length.isdecimal():
                        raise RssError("rss_invalid_content_length", 422, "订阅响应长度无效")
                    try:
                        size = int(length)
                    except ValueError:
                        raise RssError(
                            "rss_invalid_content_length", 422, "订阅响应长度无效"
                        ) from None
                    if size > self.settings.rss_max_feed_bytes:
                        raise RssError("rss_feed_too_large", 422, "订阅内容超过大小限制")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > self.settings.rss_max_feed_bytes:
                        raise RssError("rss_feed_too_large", 422, "订阅内容超过大小限制")
                    body.extend(chunk)
                result = await asyncio.to_thread(
                    parse_feed, bytes(body), url, self.settings.rss_max_entries_per_feed
                )
                result.etag = response.headers.get("etag")
                result.last_modified = response.headers.get("last-modified")
                return result
        raise RssError("rss_redirect_limit", 502, "订阅重定向次数过多")


def _entry_link(raw, feed_url: str) -> str:
    candidates = [raw.get("link", "")]
    candidates += [
        link.get("href", "")
        for link in raw.get("links", [])
        if link.get("rel", "alternate") == "alternate"
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return validate_http_url(urljoin(feed_url, candidate), allow_fragment=True)
        except ValueError:
            continue
    return ""


def _reject_xml_declaration(*args) -> None:
    raise ValueError("禁止 XML DTD/实体声明")


def _feedparser_input(body: bytes) -> bytes | str:
    # feedparser 的 bytes 流路径不解码 UTF-16 BOM，先解码为 str 再解析；
    # expat 与 ElementTree 直接接受带 BOM 的 bytes。
    if body[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return body.decode("utf-16")
    return body


def parse_feed(body: bytes, feed_url: str, max_entries: int = 500) -> FeedResult:
    try:
        # Expat 按 XML 语法识别真实声明，不误拒绝 CDATA/注释中的 HTML DOCTYPE。
        parser = expat.ParserCreate()
        parser.StartDoctypeDeclHandler = _reject_xml_declaration
        parser.EntityDeclHandler = _reject_xml_declaration
        parser.ExternalEntityRefHandler = _reject_xml_declaration
        parser.Parse(body, True)
        root = ElementTree.fromstring(body)
        root_name = root.tag.split("}")[-1].lower()
        if root_name not in {"rss", "rdf", "feed"}:
            raise ValueError
        if root_name in {"rss", "rdf"} and not any(
            child.tag.split("}")[-1].lower() == "channel" for child in root
        ):
            raise ValueError
        parsed = feedparser.parse(_feedparser_input(body), sanitize_html=False)
        if not parsed.version or parsed.get("bozo"):
            raise ValueError
    except (ValueError, ElementTree.ParseError, expat.ExpatError):
        raise RssError("rss_invalid_feed", 422, "内容不是有效的 RSS/Atom 订阅") from None
    entries = []
    seen = set()
    for raw in parsed.entries:
        title = plain_text(str(raw.get("title", "")))
        summary = plain_text(
            str(
                raw.get("summary", "")
                or next((item.get("value", "") for item in raw.get("content", [])), "")
            )
        )
        link = _entry_link(raw, feed_url)
        date = raw.get("published_parsed") or raw.get("updated_parsed")
        try:
            published = datetime.fromtimestamp(calendar.timegm(date), UTC) if date else None
        except (ValueError, OverflowError, OSError):
            published = None
        identity = raw.get("id") or raw.get("guid")
        if identity:
            source = f"guid:{identity}"
        elif link:
            source = f"link:{link}"
        else:
            source = f"hash:{title}\n{summary}\n{published.isoformat() if published else ''}"
        key = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        entries.append(FeedEntry(key, title, link, summary, published))
    # 无日期假设源为新→旧，先反转；混合源保留反转后的无日期位置，
    # 有日期位置按发布时间旧→新填入，同日期沿用源顺序的稳定排序。
    dated = sorted((e for e in entries if e.published_at), key=lambda e: e.published_at)
    dated_iter = iter(dated)
    entries = [next(dated_iter) if e.published_at else e for e in reversed(entries)]
    warnings = []
    if len(entries) > max_entries:
        entries = entries[-max_entries:]
        warnings.append("订阅条目已按数量上限截取最新部分")
    if any(not e.link for e in entries):
        warnings.append("部分条目没有有效 HTTP(S) 原文链接，将跳过发布")
    return FeedResult(plain_text(str(parsed.feed.get("title", ""))), entries, warnings=warnings)
