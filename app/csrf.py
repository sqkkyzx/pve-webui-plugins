"""CSRF tokens bound to an authenticated PVE browser session.

These are integration tokens, independent of PVE's CSRF header. They do not
authorize a user: every write must first authenticate the existing PVE session.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


_TOKEN = re.compile(r"v1\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]{43})\Z")
_BINDING = re.compile(r"[0-9a-f]{64}\Z")
_MAX_TOKEN_LENGTH = 2048
_COOKIE_DOMAIN = b"pve-plugins-csrf-cookie\0"
_TOKEN_DOMAIN = b"pve-plugins-csrf-token\0"


def _origin_error() -> HTTPException:
    return HTTPException(403, "invalid_write_origin")


def _authority(value: str, *, origin: bool = False) -> tuple[str, int | None, str | None]:
    # Origin is a single serialized origin, not a URL or a list of origins.
    # Ignore forwarded host headers: only the actual request Host is compared.
    if (
        not value or len(value) > 512
        or any(ord(char) <= 32 or ord(char) >= 127 or char in "\\?#@" for char in value)
    ):
        raise _origin_error()
    try:
        parsed = urlsplit(value if origin else "//" + value)
        if (
            (origin and parsed.scheme not in {"http", "https"})
            or (not origin and parsed.scheme)
            or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or parsed.netloc.endswith(":")
        ):
            raise _origin_error()
        port = parsed.port
        if port is not None and port < 1:
            raise _origin_error()
        hostname = parsed.hostname
        # Reject URL parser edge cases (percent escapes and malformed DNS hosts)
        # before using their result as a browser-origin comparison.
        if ":" in hostname:
            # urlsplit validates bracketed IP literals. A host containing colons
            # must retain IPv6 brackets in its serialized authority.
            if not parsed.netloc.startswith("["):
                raise _origin_error()
        elif re.fullmatch(r"[A-Za-z0-9._-]+", hostname) is None:
            raise _origin_error()
        return hostname.lower(), port, parsed.scheme if origin else None
    except (ValueError, UnicodeError):
        raise _origin_error() from None


def verify_write_origin(request: Request) -> None:
    """Require same-origin browser writes in addition to a valid CSRF token.

    The existing Nginx terminates TLS and overrides X-Forwarded-Proto while
    Uvicorn deliberately ignores proxy headers. Accept that header only as a
    single HTTP(S) scheme hint; it cannot change the compared Host or replace
    session/token verification. Non-browser clients without these browser
    headers still require the same session-bound CSRF token.
    """
    hosts = request.headers.getlist("host")
    if len(hosts) != 1:
        raise _origin_error()
    host, port, _ = _authority(hosts[0])

    fetch_sites = request.headers.getlist("sec-fetch-site")
    if fetch_sites and (len(fetch_sites) != 1 or fetch_sites[0].lower() not in {"same-origin", "none"}):
        raise _origin_error()

    origins = request.headers.getlist("origin")
    if not origins:
        return
    if len(origins) != 1:
        raise _origin_error()
    origin_host, origin_port, origin_scheme = _authority(origins[0], origin=True)

    schemes = request.headers.getlist("x-forwarded-proto")
    if schemes:
        if len(schemes) != 1 or schemes[0] not in {"http", "https"}:
            raise _origin_error()
        scheme = schemes[0]
    else:
        scheme = request.scope.get("scheme", "http")
    if scheme not in {"http", "https"} or origin_scheme != scheme:
        raise _origin_error()
    default_port = 443 if scheme == "https" else 80
    if (origin_host, origin_port or default_port) != (host, port or default_port):
        raise _origin_error()


def _csrf_error() -> HTTPException:
    return HTTPException(403, "invalid_csrf_token")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _user_bytes(user: str) -> bytes:
    if (
        not isinstance(user, str) or not user or len(user) > 64
        or any(ord(char) < 32 or ord(char) == 127 for char in user)
    ):
        raise _csrf_error()
    try:
        return user.encode("utf-8")
    except UnicodeError:
        raise _csrf_error() from None


class CsrfProtector:
    def __init__(self, secret: bytes | None = None, ttl_seconds: int = 3600) -> None:
        if secret is not None and (not isinstance(secret, bytes) or len(secret) < 32):
            raise ValueError("CSRF secret must contain at least 32 bytes")
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("CSRF lifetime must be a positive integer")
        self._secret = secret if secret is not None else secrets.token_bytes(32)
        self._ttl = ttl_seconds

    def _binding(self, request: Request) -> str:
        cookie = request.cookies.get("PVEAuthCookie", "")
        if not cookie or len(cookie) > 4096:
            raise _csrf_error()
        try:
            cookie_bytes = cookie.encode("ascii")
        except UnicodeError:
            raise _csrf_error() from None
        return hmac.new(self._secret, _COOKIE_DOMAIN + cookie_bytes, hashlib.sha256).hexdigest()

    def issue(self, request: Request, user: str) -> str:
        _user_bytes(user)
        payload = {
            "user": user,
            "binding": self._binding(request),
            "issued_at": int(time.time()),
        }
        body = _encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii"))
        signature = hmac.new(self._secret, _TOKEN_DOMAIN + body.encode("ascii"), hashlib.sha256).digest()
        return "v1." + body + "." + _encode(signature)

    def verify(self, request: Request, user: str, token: str) -> None:
        verify_write_origin(request)
        user_bytes = _user_bytes(user)
        binding = self._binding(request)
        if not isinstance(token, str) or len(token) > _MAX_TOKEN_LENGTH:
            raise _csrf_error()
        match = _TOKEN.fullmatch(token)
        if match is None:
            raise _csrf_error()
        body, encoded_signature = match.groups()
        try:
            signature = _decode(encoded_signature)
            expected = hmac.new(self._secret, _TOKEN_DOMAIN + body.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise _csrf_error()
            payload = json.loads(_decode(body))
            if not isinstance(payload, dict) or set(payload) != {"user", "binding", "issued_at"}:
                raise _csrf_error()
            payload_user = _user_bytes(payload["user"])
            payload_binding = payload["binding"]
            issued_at = payload["issued_at"]
            if not isinstance(payload_binding, str) or _BINDING.fullmatch(payload_binding) is None:
                raise _csrf_error()
            if not hmac.compare_digest(payload_user, user_bytes) or not hmac.compare_digest(payload_binding, binding):
                raise _csrf_error()
            now = int(time.time())
            if type(issued_at) is not int or issued_at < 0 or issued_at > now or now - issued_at >= self._ttl:
                raise _csrf_error()
        except (ValueError, UnicodeError):
            raise _csrf_error() from None
