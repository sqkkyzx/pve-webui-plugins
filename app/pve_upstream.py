"""Fixed PVE node parsing and session-independent HTTP failover.

Clients borrow one connection pool without sharing their cookie jars. Only
read requests and explicitly marked ticket verification can change nodes.
"""

import asyncio
import ipaddress
import re
import ssl
import time
from urllib.parse import urlsplit

import httpx


MAX_UPSTREAMS = 16
FAILURE_COOLDOWN_SECONDS = 5.0
REQUEST_BUDGET_SECONDS = 10.0
CONNECT_TIMEOUT_SECONDS = 2.0
VERIFY_TICKET_EXTENSION = "pve_verify_ticket"
_RETRY_STATUSES = {502, 503, 504}
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def parse_upstream_nodes(value: str) -> tuple[str, ...]:
    """Parse bare IP nodes, with HTTPS and port 8006 as fixed defaults."""
    if not isinstance(value, str):
        raise ValueError("PVE_UPSTREAM must contain comma-separated IP addresses")
    entries = value.split(",")
    if not 1 <= len(entries) <= MAX_UPSTREAMS:
        raise ValueError("PVE_UPSTREAM must contain between 1 and 16 nodes")
    origins = []
    for entry in entries:
        node = entry.strip(" \t")
        try:
            if not node or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in node) or "%" in node:
                raise ValueError
            port = 8006
            if node.startswith("["):
                match = re.fullmatch(r"\[([^\[\]]+)\](?::([0-9]+))?", node)
                if match is None:
                    raise ValueError
                address = ipaddress.IPv6Address(match[1])
                if match[2] is not None:
                    port = int(match[2])
            elif node.count(":") == 1:
                host, raw_port = node.split(":")
                if not raw_port.isascii() or not raw_port.isdecimal():
                    raise ValueError
                address = ipaddress.IPv4Address(host)
                port = int(raw_port)
            else:
                address = ipaddress.ip_address(node)
            if not 1 <= port <= 65535:
                raise ValueError
            host = f"[{address}]" if address.version == 6 else str(address)
            origin = f"https://{host}:{port}"
            if origin in origins:
                raise ValueError
            origins.append(origin)
        except ValueError:
            raise ValueError("PVE_UPSTREAM must contain unique IP addresses with valid optional ports") from None
    return tuple(origins)


def parse_upstream_server_names(value: str | None, node_count: int) -> tuple[str, ...]:
    """Parse optional, positional TLS DNS identities without accepting IPs."""
    if value is None or value == "":
        return ()
    if not isinstance(value, str):
        raise ValueError("PVE_TLS_SERVER_NAMES must contain aligned DNS names")
    names = tuple(item.strip(" \t").lower() for item in value.split(","))
    if len(names) != node_count:
        raise ValueError("PVE_TLS_SERVER_NAMES must have one DNS name per PVE node")
    for name in names:
        if len(name) > 253 or not name.isascii() or any(_DNS_LABEL.fullmatch(label) is None for label in name.split(".")):
            raise ValueError("PVE_TLS_SERVER_NAMES must contain valid DNS names")
        try:
            ipaddress.ip_address(name)
        except ValueError:
            continue
        raise ValueError("PVE_TLS_SERVER_NAMES must contain DNS names rather than IP addresses")
    return names


def canonical_upstream_origin(value: str) -> str:
    """Validate explicit Settings/test origins independently of ENV IP syntax."""
    try:
        if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or any(char in value for char in "?#\\%")
                or parsed.port == 0):
            raise ValueError
        return str(httpx.URL(value)).rstrip("/")
    except (ValueError, TypeError, httpx.InvalidURL):
        raise ValueError("PVE upstream requires a fixed HTTPS origin") from None


class PveUpstreamState:
    """Shared node health without storing credentials, cookies or user identity."""

    def __init__(self, origins: tuple[str, ...], *, clock=time.monotonic):
        self.origins = origins
        self.clock = clock
        self.failed_until: dict[str, float] = {}

    def candidates(self) -> tuple[str, ...]:
        now = self.clock()
        healthy = tuple(origin for origin in self.origins if self.failed_until.get(origin, 0) <= now)
        return healthy + tuple(origin for origin in self.origins if origin not in healthy)

    def failed(self, origin: str) -> None:
        self.failed_until[origin] = self.clock() + FAILURE_COOLDOWN_SECONDS

    def succeeded(self, origin: str) -> None:
        self.failed_until.pop(origin, None)


class _PveResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream, state, origin):
        self.stream = stream
        self.state = state
        self.origin = origin

    async def __aiter__(self):
        try:
            async for chunk in self.stream:
                yield chunk
        except httpx.TransportError:
            self.state.failed(self.origin)
            raise

    async def aclose(self):
        await self.stream.aclose()


class PveFailoverTransport(httpx.AsyncBaseTransport):
    """Borrowed by short-lived clients and closed only by the application.

    A response stream belongs to its selected node. Failover applies before
    handing that stream to the caller, preserving caller-side streaming limits.
    """

    def __init__(self, state: PveUpstreamState, *, verify: ssl.SSLContext | bool = True,
                 server_names: tuple[str, ...] = (), transport: httpx.AsyncBaseTransport | None = None):
        self.state = state
        self.server_names = dict(zip(state.origins, server_names))
        self._transport = transport
        self._verify = verify
        self._owns_transport = transport is None
        self._closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._closed:
            raise httpx.ConnectError("PVE transport is closed", request=request)
        origin = str(request.url.copy_with(raw_path=b"/")).rstrip("/")
        if request.url.userinfo or origin not in self.state.origins:
            raise httpx.UnsupportedProtocol("PVE request origin is not configured", request=request)
        if self._transport is None:
            self._transport = httpx.AsyncHTTPTransport(verify=self._verify, trust_env=False, retries=0)
        try:
            async with asyncio.timeout(REQUEST_BUDGET_SECONDS):
                return await self._send(request)
        except TimeoutError:
            raise httpx.ReadTimeout("PVE upstream request timed out", request=request) from None

    async def _send(self, request: httpx.Request) -> httpx.Response:
        deadline = asyncio.get_running_loop().time() + REQUEST_BUDGET_SECONDS
        read_only = request.method in {"GET", "HEAD"}
        verify_ticket = (request.method == "POST" and request.url.path == "/api2/json/access/ticket"
                         and request.extensions.get(VERIFY_TICKET_EXTENSION) is True)
        candidates = self.state.candidates()
        if not read_only and not verify_ticket:
            candidates = candidates[:1]
        # Read bodies and the small verifier form can be replayed. General
        # write streams are passed through exactly once.
        body = await request.aread() if read_only or verify_ticket else None
        for index, target in enumerate(candidates):
            remaining = deadline - asyncio.get_running_loop().time()
            attempt_budget = remaining / (len(candidates) - index)
            url = httpx.URL(target).copy_with(raw_path=request.url.raw_path)
            headers = request.headers.copy()
            headers["host"] = url.netloc.decode("ascii")
            extensions = dict(request.extensions)
            extensions.pop(VERIFY_TICKET_EXTENSION, None)
            extensions.pop("sni_hostname", None)
            if target in self.server_names:
                extensions["sni_hostname"] = self.server_names[target]
            timeout = dict(extensions.get("timeout", {}))
            connect_timeout = timeout.get("connect")
            timeout["connect"] = min(CONNECT_TIMEOUT_SECONDS, attempt_budget,
                                     connect_timeout if connect_timeout is not None else CONNECT_TIMEOUT_SECONDS)
            extensions["timeout"] = timeout
            attempt = httpx.Request(request.method, url, headers=headers,
                                    stream=httpx.ByteStream(body) if body is not None else request.stream,
                                    extensions=extensions)
            try:
                try:
                    async with asyncio.timeout(attempt_budget):
                        response = await self._transport.handle_async_request(attempt)
                except TimeoutError:
                    raise httpx.ReadTimeout("PVE upstream response timed out", request=attempt) from None
            except httpx.TransportError as error:
                if isinstance(error, (httpx.UnsupportedProtocol, httpx.LocalProtocolError)):
                    raise
                retryable = read_only or (verify_ticket and isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)))
                self.state.failed(target)
                if not retryable or index == len(candidates) - 1:
                    raise
                continue
            if response.status_code in _RETRY_STATUSES:
                self.state.failed(target)
                if read_only and index < len(candidates) - 1:
                    await response.aclose()
                    continue
            else:
                self.state.succeeded(target)
            response.stream = _PveResponseStream(response.stream, self.state, target)
            return response
        raise AssertionError("PVE failover requires at least one configured node")

    async def aclose(self) -> None:
        """An individual HTTP client never owns the shared pool."""

    async def close_pool(self) -> None:
        if not self._closed:
            self._closed = True
            if self._owns_transport and self._transport is not None:
                await self._transport.aclose()
