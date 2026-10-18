"""Validate existing PVE browser sessions before accessing integration APIs.

PVE remains the authority for tickets, enabled users, and completed two-factor
authentication. The integration separately restricts access to an explicit
administrator allowlist; a PVE login alone does not grant integration access.
"""

import re
import ssl
from urllib.parse import unquote, urlsplit

from fastapi import HTTPException, Request
import httpx


_USER = re.compile(r"[^\s:/]+@[A-Za-z][A-Za-z0-9._-]+\Z")
_TICKET = re.compile(r"PVE:([^:\s/]+):[0-9A-Fa-f]{8,16}::[A-Za-z0-9+/=_-]+\Z")
_MAX_COOKIE_LENGTH = 4096


def _valid_user(value: str) -> bool:
    return 3 <= len(value) <= 64 and _USER.fullmatch(value) is not None


class PveAuthenticator:
    def __init__(
        self,
        upstream: str,
        *,
        verify: ssl.SSLContext | bool = True,
        allowed_users: tuple[str, ...] = ("root@pam",),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        origin = urlsplit(upstream)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            raise ValueError("PVE authentication requires a fixed HTTPS origin")
        _ = origin.port
        if any(not isinstance(user, str) or not _valid_user(user) for user in allowed_users):
            raise ValueError("PVE allowed users must be valid full user IDs")
        self.upstream = upstream.rstrip("/")
        self.verify = verify
        self.allowed_users = frozenset(allowed_users)
        self.transport = transport

    async def authenticate(self, request: Request) -> str:
        raw_cookie = request.cookies.get("PVEAuthCookie", "")
        # Keep the exact cookie value for the authenticated GET, but decode once
        # for the ticket form. Reject controls and delimiters before constructing
        # a Cookie header; neither other browser cookies nor auth headers pass on.
        if (
            not raw_cookie
            or len(raw_cookie) > _MAX_COOKIE_LENGTH
            or any(ord(char) < 0x21 or ord(char) > 0x7E or char in ";," for char in raw_cookie)
        ):
            raise HTTPException(401, "A valid PVE browser session is required")
        try:
            ticket = unquote(raw_cookie, errors="strict")
        except UnicodeError:
            raise HTTPException(401, "A valid PVE browser session is required") from None
        match = _TICKET.fullmatch(ticket)
        if match is None or not _valid_user(match[1]):
            raise HTTPException(401, "A valid PVE browser session is required")
        candidate = match[1]
        if candidate not in self.allowed_users:
            raise HTTPException(403, "This PVE user is not allowed to use the integration")

        headers = {"Cookie": f"PVEAuthCookie={raw_cookie}", "Accept": "application/json"}
        try:
            # A client per authentication prevents upstream Set-Cookie state from
            # crossing visitors. Explicit headers keep both checks on the ticket
            # supplied by this request, including when PVE sends Set-Cookie.
            async with httpx.AsyncClient(
                verify=self.verify,
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                permissions = await client.get(
                    self.upstream + "/api2/json/access/permissions", headers=headers,
                )
                # This authenticated endpoint checks the original cookie, user
                # enabled status, and completed TFA. Its response is a permission
                # map, not an identity assertion; never supply another userid.
                self._data(permissions)
                client.cookies.clear()
                identity = await client.post(
                    self.upstream + "/api2/json/access/ticket",
                    headers=headers,
                    data={
                        "username": candidate,
                        "password": ticket,
                        # Both fixed fields are essential: without them the PVE
                        # endpoint renews tickets instead of only verifying one.
                        "path": "/",
                        "privs": "Sys.Audit",
                    },
                )
                data = self._data(identity)
        except httpx.HTTPError:
            # Exception text and response bodies can contain the user's ticket.
            raise HTTPException(503, "PVE session verification is unavailable") from None

        if data.get("username") != candidate:
            raise HTTPException(401, "PVE session identity could not be verified")
        if "ticket" in data or "CSRFPreventionToken" in data:
            raise HTTPException(503, "Unexpected PVE session verification response")
        return candidate

    @staticmethod
    def _data(response: httpx.Response) -> dict:
        if response.status_code == 401:
            raise HTTPException(401, "The PVE session is invalid or expired")
        if response.status_code == 403:
            raise HTTPException(403, "The PVE session does not have the required permission")
        if response.status_code != 200:
            raise HTTPException(503, "PVE session verification is unavailable")
        try:
            payload = response.json()
        except ValueError:
            raise HTTPException(503, "Unexpected PVE session verification response") from None
        if not isinstance(payload, dict) or "errors" in payload or payload.get("success", True) is not True:
            raise HTTPException(503, "Unexpected PVE session verification response")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise HTTPException(503, "Unexpected PVE session verification response")
        if "NeedTFA" in payload or "NeedTFA" in data:
            raise HTTPException(401, "A completed PVE browser session is required")
        return data
