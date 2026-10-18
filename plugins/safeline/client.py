"""Read-only, bounded SafeLine API client with an explicit endpoint allowlist."""

import asyncio
import json
import re

import httpx

from plugins.safeline.connection import SafeLineError


_PATHS = {"/api/open/" + name for name in (
    "system", "system/edition", "site", "site/group", "policy", "policy/detail", "cert",
    "ipgroup", "ipgroup/detail", "events", "events/rule", "records", "records/rule",
)} | {"/api/stat/" + name for name in (
    "qps", "advance/access", "advance/attack", "advance/trend/access", "advance/trend/intercept",
)}
_SITE = re.compile(r"/api/open/site/[1-9][0-9]*(?:/(?:proxy|semantics|acl))?\Z")
_QUERY = {"page", "page_size", "action", "id", "start", "end", "ip", "host", "port", "event_id",
          "policy_rule_id", "begin_time", "end_time", "site_id", "time_preset", "url", "attack_type"}


class _BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport):
        self.transport = transport

    async def handle_async_request(self, request):
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        pass


class SafeLineClient:
    def __init__(self, connection, *, transport=None):
        self.connection = connection
        self.transport = transport
        self._slots = asyncio.Semaphore(5)

    async def get(self, path, params=None):
        if (path not in _PATHS and not _SITE.fullmatch(path)) or (params and not set(params) <= _QUERY):
            raise SafeLineError("invalid_safeline_endpoint", 422)
        if self.connection is None:
            raise SafeLineError("safeline_not_configured", 409)
        config = self.connection
        headers = {"X-SLCE-API-TOKEN": config.token, "Accept": "application/json"}
        extensions = {}
        if config.tls_server_name:
            extensions["sni_hostname"] = config.tls_server_name
            port = httpx.URL(config.url).port
            headers["Host"] = config.tls_server_name + (":" + str(port) if port and port != 443 else "")
        try:
            async with self._slots, httpx.AsyncClient(
                verify=config.verify(), trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(15, connect=5), transport=_BorrowedTransport(self.transport) if self.transport else None,
            ) as client:
                async with client.stream("GET", config.url + path, params=params, headers=headers, extensions=extensions) as response:
                    if response.status_code in {401, 403}:
                        raise SafeLineError("safeline_invalid_credentials" if response.status_code == 401 else "safeline_permission_denied")
                    if response.status_code == 404:
                        raise SafeLineError("safeline_endpoint_unavailable", 404)
                    if response.status_code in {301, 302, 303, 307, 308, 467}:
                        raise SafeLineError("safeline_sso_required")
                    if response.status_code != 200:
                        raise SafeLineError("safeline_upstream_error")
                    if "application/json" not in response.headers.get("content-type", "").lower():
                        raise SafeLineError("safeline_unexpected_response")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 8_000_000:
                            raise SafeLineError("safeline_response_too_large")
                        body.extend(chunk)
            result = json.loads(body)
            if not isinstance(result, dict) or "data" not in result:
                raise SafeLineError("safeline_unexpected_response")
            if result.get("err"):
                raise SafeLineError("safeline_business_error")
            return result["data"]
        except httpx.TimeoutException:
            raise SafeLineError("safeline_timeout") from None
        except httpx.HTTPError as error:
            code = "safeline_tls_error" if "CERTIFICATE_VERIFY_FAILED" in str(error) else "safeline_unreachable"
            raise SafeLineError(code) from None
        except (ValueError, UnicodeError, RecursionError):
            raise SafeLineError("safeline_unexpected_response") from None

    async def status(self):
        system = await self.get("/api/open/system")
        if not isinstance(system, dict) or not isinstance(system.get("version"), str):
            raise SafeLineError("safeline_unexpected_response")
        result = {"connected": True, "version": system["version"].replace(self.connection.token, "[REDACTED]")[:100]}
        try:
            edition = await self.get("/api/open/system/edition")
            result["edition"] = {k: v.replace(self.connection.token, "[REDACTED]") if isinstance(v, str) else v
                                 for k, v in edition.items() if k in {"version", "edition", "name", "type"} and isinstance(v, (str, int, bool))} if isinstance(edition, dict) else {}
        except SafeLineError as error:
            result["edition_error"] = error.code
        return result
