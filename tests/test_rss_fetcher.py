from __future__ import annotations

import gzip

import httpx
import pytest

from app.config import Settings
from app.rss.errors import RssError
from app.rss.fetcher import RssFetcher, parse_feed

URL = "https://example.com/feed"
EMPTY = b'<?xml version="1.0"?><rss version="2.0"><channel><title>RSS</title></channel></rss>'
RSS = b"""<rss version="2.0"><channel><title>Tech</title>
<item><guid>new</guid><title>New</title><link>/new</link>
<pubDate>Tue, 13 Oct 2026 12:00:00 GMT</pubDate>
<description><![CDATA[<p>Safe &amp; good</p>
<script>bad()</script><style>bad</style>]]></description>
</item><item><guid>old</guid><title>Old</title><link>/old</link>
<pubDate>Mon, 12 Oct 2026 12:00:00 GMT</pubDate></item>
<item><guid>new</guid><title>Duplicate</title></item></channel></rss>"""
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title>
<entry><id>atom-1</id><title>Entry</title><link href="/atom-1"/>
<updated>2026-10-13T10:00:00Z</updated><content type="html">&lt;p&gt;Hello&lt;/p&gt;</content>
</entry></feed>"""


async def public_dns(host, port) -> list[str]:
    return ["93.184.216.34"]


def fetcher(handler, **settings) -> RssFetcher:
    return RssFetcher(
        Settings(_env_file=None, **settings),
        transport=httpx.MockTransport(handler),
        resolver=public_dns,
    )


def test_rss_atom_guid_dedupe_order_and_safe_html() -> None:
    result = parse_feed(RSS, URL)
    assert result.feed_title == "Tech"
    assert [e.title for e in result.entries] == ["Old", "New"]
    assert result.entries[1].link == "https://example.com/new"
    assert result.entries[1].summary == "Safe & good"
    assert result.entries[0].published_at.tzinfo is not None
    assert (
        parse_feed(RSS.replace(b"<title>New</title>", b"<title>Changed</title>"), URL)
        .entries[1]
        .entry_key
        == result.entries[1].entry_key
    )
    atom = parse_feed(ATOM, URL)
    assert atom.feed_title == "Atom"
    assert atom.entries[0].summary == "Hello"
    assert atom.entries[0].link == "https://example.com/atom-1"
    anchored = parse_feed(ATOM.replace(b'/atom-1"', b'/atom-1#section"'), URL)
    assert anchored.entries[0].link == "https://example.com/atom-1#section"
    assert parse_feed(EMPTY, URL).entries == []


def test_no_date_order_hash_fallback_and_link_scheme() -> None:
    body = b"""<rss version="2.0"><channel><title>RSS</title>
    <item><title>A</title><link>/a</link></item><item><title>B</title></item>
    <item><title>C</title><link>javascript:bad()</link></item>
    <item><title>B</title></item></channel></rss>"""
    result = parse_feed(body, URL)
    assert [e.title for e in result.entries] == ["C", "B", "A"]
    assert result.entries[0].link == ""
    assert result.warnings
    assert parse_feed(body, URL).entries[1].entry_key == result.entries[1].entry_key
    changed = body.replace(b"<title>A</title>", b"<title>Changed</title>")
    assert parse_feed(changed, URL).entries[-1].entry_key == result.entries[-1].entry_key
    limited = parse_feed(body, URL, max_entries=2)
    assert [e.title for e in limited.entries] == ["B", "A"] and limited.warnings


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body>login</body></html>",
        b"not a feed",
        b"<rss>",
        b'<rss version="2.0"><channel><title>Broken</channel></rss>',
        b'<!DOCTYPE rss [<!ENTITY x "danger">]><rss version="2.0"><channel/></rss>',
        b"<root/>",
        b"<rss/>",
        b'<rss version="2.0"><title>No channel</title></rss>',
    ],
)
def test_rejects_html_malformed_and_entities(body) -> None:
    with pytest.raises(RssError) as error:
        parse_feed(body, URL)
    assert error.value.code == "rss_invalid_feed"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_cdata_html_doctype_allowed_but_real_dtd_rejected(encoding) -> None:
    body = (
        '<rss version="2.0"><channel><title>RSS</title><item><title>Safe</title>'
        "<description><![CDATA[<!DOCTYPE html><p>Hello</p>]]></description>"
        "</item></channel></rss>"
    )
    result = parse_feed(body.encode(encoding), URL)
    assert result.entries[0].summary == "Hello"
    for declaration in ("<!DOCTYPE rss>", '<!DOCTYPE rss [<!ENTITY x "danger">]>'):
        with pytest.raises(RssError) as error:
            parse_feed((declaration + body).encode(encoding), URL)
        assert error.value.code == "rss_invalid_feed"


def test_mixed_date_order_has_stable_reverse_source_slots() -> None:
    body = b"""<rss version="2.0"><channel><title>RSS</title>
    <item><title>No date new</title></item>
    <item><title>Old dated</title><pubDate>Mon, 12 Oct 2026 12:00:00 GMT</pubDate></item>
    <item><title>No date old</title></item>
    <item><title>New dated</title><pubDate>Tue, 13 Oct 2026 12:00:00 GMT</pubDate></item>
    </channel></rss>"""
    result = parse_feed(body, URL)
    assert [e.title for e in result.entries] == [
        "Old dated",
        "No date old",
        "New dated",
        "No date new",
    ]
    assert [e.title for e in parse_feed(body, URL, 2).entries] == ["New dated", "No date new"]
    assert [e.entry_key for e in parse_feed(body, URL).entries] == [
        e.entry_key for e in result.entries
    ]


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "0.0.0.0",
        "100.64.0.1",
        "240.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
    ],
)
async def test_blocks_non_public_ip(ip) -> None:
    client = fetcher(lambda request: pytest.fail("不应发送 HTTP 请求"))
    url = f"http://[{ip}]/feed" if ":" in ip else f"http://{ip}/feed"
    try:
        with pytest.raises(RssError) as error:
            await client.fetch(url)
        assert error.value.code == "rss_private_host"
    finally:
        await client.close()


async def test_all_dns_addresses_checked_exact_whitelist_and_ip_pin() -> None:
    calls = []

    async def dns(host, port):
        calls.append(host)
        return ["93.184.216.34", "10.0.0.1"]

    def handler(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "rsshub.internal:8080"
        assert request.extensions["sni_hostname"] == "rsshub.internal"
        return httpx.Response(200, stream=httpx.ByteStream(EMPTY))

    settings = Settings(_env_file=None, rss_allowed_private_hosts=["rsshub.internal"])
    client = RssFetcher(settings, resolver=dns, transport=httpx.MockTransport(handler))
    try:
        result = await client.fetch("http://rsshub.internal:8080/rss")
        assert result.entries == []
        with pytest.raises(RssError) as error:
            await client.fetch("http://other.rsshub.internal:8080/rss")
        assert error.value.code == "rss_private_host"
        assert calls == ["rsshub.internal", "other.rsshub.internal"]
    finally:
        await client.close()


async def test_redirect_revalidates_and_blocks_private_target() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private?secret=value"})

    client = fetcher(handler)
    try:
        with pytest.raises(RssError) as error:
            await client.fetch(URL)
        assert error.value.code == "rss_private_host"
        assert len(requests) == 1 and "secret" not in str(error.value)
    finally:
        await client.close()


async def test_redirect_limit_and_cross_host_cache_headers() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            assert request.headers["if-none-match"] == '"abc"'
            return httpx.Response(302, headers={"location": "https://other.example.com/feed"})
        assert "if-none-match" not in request.headers
        return httpx.Response(302, headers={"location": "/loop"})

    client = fetcher(handler)
    try:
        with pytest.raises(RssError) as error:
            await client.fetch(URL, etag='"abc"')
        assert error.value.code == "rss_redirect_limit" and len(requests) == 6
    finally:
        await client.close()


async def test_conditional_headers_and_304_response() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                stream=httpx.ByteStream(RSS),
                headers={"etag": '"tag"', "last-modified": "Tue, 13 Oct 2026 12:00:00 GMT"},
            )
        assert request.headers["if-none-match"] == '"tag"'
        assert request.headers["if-modified-since"] == "Tue, 13 Oct 2026 12:00:00 GMT"
        return httpx.Response(304)

    client = fetcher(handler)
    try:
        first = await client.fetch(URL)
        second = await client.fetch(URL, etag=first.etag, last_modified=first.last_modified)
        assert second.not_modified and second.entries == []
    finally:
        await client.close()


class Chunks(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.read = 0

    async def __aiter__(self):
        for _ in range(64):
            self.read += 1
            yield b"x" * 65536


@pytest.mark.parametrize("kind", ["header", "stream"])
async def test_default_2mb_limit_for_raw_stream(kind) -> None:
    chunks = Chunks()

    def handler(request):
        if kind == "header":
            return httpx.Response(200, content=b"", headers={"content-length": str(2**21 + 1)})
        return httpx.Response(200, stream=chunks)

    client = fetcher(handler)
    try:
        with pytest.raises(RssError) as error:
            await client.fetch(URL)
        assert error.value.code == "rss_feed_too_large"
        if kind == "stream":
            # 累计字节达到上限立即退出，不消费整个响应体。
            assert chunks.read < 64
    finally:
        await client.close()


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "gzip, identity"])
@pytest.mark.parametrize("bomb", [False, True])
async def test_compressed_response_rejected_without_read_or_decompression(encoding, bomb) -> None:
    compressed = gzip.compress(b"x" * (2**21 + 1) if bomb else EMPTY)
    read = False

    class Compressed(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal read
            read = True
            yield compressed

    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, stream=Compressed(), headers={"content-encoding": encoding})

    client = fetcher(handler)
    try:
        with pytest.raises(RssError) as error:
            await client.fetch(URL)
        assert error.value.code == "rss_compression_unsupported"
        assert error.value.status_code == 422 and "压缩" in error.value.message
        assert not read
    finally:
        await client.close()


@pytest.mark.parametrize("length", ["-1", "bad", "1.2", "+10", "1,2", ""])
async def test_invalid_content_length_rejected(length) -> None:
    client = fetcher(
        lambda request: httpx.Response(
            200, stream=httpx.ByteStream(EMPTY), headers={"content-length": length}
        )
    )
    try:
        with pytest.raises(RssError) as error:
            await client.fetch(URL)
        assert error.value.code == "rss_invalid_content_length" and error.value.status_code == 422
    finally:
        await client.close()


async def test_identity_raw_stream_at_exact_size_limit() -> None:
    body = EMPTY + b" " * (2**21 - len(EMPTY))

    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            stream=httpx.ByteStream(body),
            headers={
                "content-length": str(len(body)),
                "content-encoding": "identity",
            },
        )

    client = fetcher(handler)
    try:
        assert (await client.fetch(URL)).entries == []
    finally:
        await client.close()


@pytest.mark.parametrize("kind", ["html", "status", "timeout", "dns"])
async def test_fetch_failures_do_not_leak_urls_or_secrets(kind) -> None:
    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("sensitive-token-in-error", request=request)
        if kind == "html":
            return httpx.Response(
                200, stream=httpx.ByteStream(EMPTY), headers={"content-type": "text/html"}
            )
        return httpx.Response(500)

    async def dns(host, port):
        raise OSError("sensitive-token-in-error")

    client = fetcher(handler)
    if kind == "dns":
        client.resolver = dns
    try:
        with pytest.raises(RssError) as error:
            await client.fetch(URL + "?token=sensitive-token-in-error")
        assert "sensitive-token" not in str(error.value) and "example.com" not in str(error.value)
    finally:
        await client.close()
