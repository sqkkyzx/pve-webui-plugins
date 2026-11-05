"""Bounded, credential-free favicon discovery for registered site URLs."""

import asyncio
from collections import OrderedDict
from html.parser import HTMLParser
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.icons import icon_url


MAX_HTML_BYTES = 256 * 1024
DISCOVERY_TIMEOUT = 3.0
MAX_REDIRECTS = 3
CACHE_SECONDS = 300.0
CACHE_LIMIT = 100


def fallback_icon(url):
    parsed = urlsplit(url)
    return icon_url(urlunsplit((parsed.scheme, parsed.netloc, "/favicon.ico", "", "")))


class IconLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bases = []
        self.links = []

    def handle_starttag(self, tag, attributes):
        values = {}
        for key, value in attributes:
            values.setdefault(key, value)
        href = values.get("href")
        if (not isinstance(href, str) or not href.strip() or len(href) > 2048
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
                       or char == "\\" for char in href)):
            return
        if tag == "base":
            self.bases.append(href.strip())
        elif tag == "link":
            rel = set((values.get("rel") or "").lower().split())
            if "icon" in rel:
                self.links.append((0, href.strip()))
            elif rel & {"apple-touch-icon", "apple-touch-icon-precomposed"}:
                self.links.append((1, href.strip()))


def parse_icon(html, page_url):
    parser = IconLinks()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError):
        return None
    base = page_url
    for href in parser.bases:
        try:
            base = icon_url(urljoin(page_url, href))
            break
        except ValueError:
            continue
    for priority in (0, 1):
        for candidate_priority, href in parser.links:
            if candidate_priority != priority:
                continue
            try:
                return icon_url(urljoin(base, href))
            except ValueError:
                continue
    return None


class _BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport):
        self.transport = transport

    async def handle_async_request(self, request):
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        # Injected transports belong to the caller and can span discoveries
        # and replacement plugin generations.
        pass


class SiteIconDiscovery:
    def __init__(self, *, transport=None):
        self.transport = transport
        self._cache = OrderedDict()
        self._pending = {}
        self._slots = asyncio.Semaphore(4)
        self._closed = False

    async def discover(self, url):
        fallback = fallback_icon(url)
        cached = self._cache.get(url)
        if cached:
            expires, icon = cached
            if expires > time.monotonic():
                self._cache.move_to_end(url)
                return icon
            del self._cache[url]
        if self._closed:
            return fallback
        pending = self._pending.get(url)
        if pending is None:
            # Registered sites are capped at 100. Bound queued generations too
            # if URLs are edited while older discoveries are still running.
            if len(self._pending) >= CACHE_LIMIT:
                return fallback
            pending = asyncio.create_task(self._fetch(url))
            self._pending[url] = pending
            pending.add_done_callback(lambda task: self._remember(url, task))
        return await asyncio.shield(pending)

    def _remember(self, url, task):
        if self._pending.get(url) is task:
            del self._pending[url]
        if self._closed or task.cancelled():
            return
        try:
            icon = task.result()
        except Exception:
            icon = fallback_icon(url)
        self._cache[url] = (time.monotonic() + CACHE_SECONDS, icon)
        self._cache.move_to_end(url)
        while len(self._cache) > CACHE_LIMIT:
            self._cache.popitem(last=False)

    async def _fetch(self, url):
        fallback = fallback_icon(url)
        try:
            async with asyncio.timeout(DISCOVERY_TIMEOUT):
                async with self._slots:
                    async with httpx.AsyncClient(
                        transport=_BorrowedTransport(self.transport) if self.transport is not None else None,
                        trust_env=False, follow_redirects=False,
                        timeout=httpx.Timeout(DISCOVERY_TIMEOUT),
                        headers={"Accept": "text/html, application/xhtml+xml", "Accept-Encoding": "identity"},
                    ) as client:
                        target = icon_url(url)
                        for redirect in range(MAX_REDIRECTS + 1):
                            # Even cookies supplied by the remote page must not
                            # be sent into later redirect requests.
                            client.cookies.clear()
                            async with client.stream("GET", target) as response:
                                if response.status_code in {301, 302, 303, 307, 308}:
                                    if redirect == MAX_REDIRECTS or not response.headers.get("location"):
                                        return fallback
                                    location = response.headers["location"]
                                    if any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159
                                           or char == "\\" for char in location):
                                        return fallback
                                    target = icon_url(urljoin(target, location))
                                    continue
                                if response.status_code != 200:
                                    return fallback
                                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                                if content_type not in {"", "text/html", "application/xhtml+xml"}:
                                    return fallback
                                # Reject ignored identity negotiation before
                                # HTTPX could inflate a compressed response.
                                if response.headers.get("content-encoding", "identity").lower() != "identity":
                                    return fallback
                                length = response.headers.get("content-length", "")
                                if length.isdigit() and int(length) > MAX_HTML_BYTES:
                                    return fallback
                                body = bytearray()
                                async for chunk in response.aiter_bytes():
                                    if len(body) + len(chunk) > MAX_HTML_BYTES:
                                        return fallback
                                    body.extend(chunk)
                                html = body.decode(response.encoding or "utf-8", errors="replace")
                                return parse_icon(html, target) or fallback
        except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, ValueError, UnicodeError, LookupError):
            return fallback
        return fallback

    async def close(self):
        self._closed = True
        tasks = tuple(self._pending.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        self._cache.clear()
