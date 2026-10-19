"""Bounded SafeLine client with separate read and write endpoint allowlists."""

import asyncio
import codecs
import json
import re

import httpx

from plugins.safeline.connection import SafeLineError


_PATHS = {"/api/open/" + name for name in (
    "system", "system/edition", "site", "site/group", "policy", "policy/detail", "cert",
    "ipgroup", "ipgroup/detail", "events", "events/rule", "records", "records/rule",
    "security_posture/statistics", "security_posture/trends", "security_posture/realtime", "global/proxy", "site/group/switch",
    "global/mode", "auth_defense/source", "portal", "site/0/acl", "records/challenge", "challenge/config",
    "records/acl", "records/waiting", "cloud/policies", "ja4",
)} | {"/api/stat/" + name for name in (
    "qps", "advance/access", "advance/attack", "advance/trend/access", "advance/trend/intercept",
    "advance/error_status_code", "advance/location",
)}
_SITE = re.compile(r"/api/open/site/[1-9][0-9]*(?:/(?:proxy|semantics|acl|waiting|chaos|excludes|forwarding_rules|nginx_config|resources|log(?:/limit)?))?\Z")
_TAMPER_READ = re.compile(r"/api/business/site/[1-9][0-9]*/anti_tamper\Z")
_CERT = re.compile(r"/api/open/cert/[1-9][0-9]*\Z")
_RECORD = re.compile(r"/api/open/record/(?:rule/)?[A-Za-z0-9_-]{1,128}\Z")
_WRITES = {("PUT", "/api/open/site"), ("PUT", "/api/open/site/mode"),
           ("PUT", "/api/open/policy"), ("PUT", "/api/open/policy/switch"),
           ("PUT", "/api/open/ipgroup"), ("POST", "/api/open/ipgroup"), ("POST", "/api/open/ipgroup/append"), ("POST", "/api/open/cert"),
           ("PUT", "/api/open/global/proxy"), ("PUT", "/api/commercial/site/group/switch"),
           ("PUT", "/api/open/site/challenge"), ("PUT", "/api/open/site/defense"), ("POST", "/api/open/security_posture/site"),
           ("POST", "/api/open/challenge/config"), ("POST", "/api/open/policy"), ("PUT", "/api/open/policy/orders"),
           ("POST", "/api/open/cloud/policies/subscribe"), ("PUT", "/api/open/site/0/acl"), ("PUT", "/api/acl/relieve"),
           ("DELETE", "/api/open/policy")}
_PROXY_WRITE = re.compile(r"/api/open/site/[1-9][0-9]*/proxy\Z")
_APP_WRITES = {
    "PUT": re.compile(r"/api/open/site/[1-9][0-9]*/(?:acl(?:/[1-9][0-9]*)?|semantics|nginx_config|forwarding_rules/[1-9][0-9]*(?:/switch)?)\Z"),
    "POST": re.compile(r"/api/open/site/[1-9][0-9]*/(?:acl|waiting|chaos|excludes|forwarding_rules|log/limit)\Z"),
    "DELETE": re.compile(r"/api/open/site/[1-9][0-9]*/(?:resources|forwarding_rules/[1-9][0-9]*|acl/[1-9][0-9]*)\Z"),
}
_TAMPER_WRITES = {"POST": re.compile(r"/api/business/site/[1-9][0-9]*/anti_tamper\Z"),
                  "PUT": re.compile(r"/api/business/(?:site/[1-9][0-9]*/anti_tamper|anti_tamper/[1-9][0-9]*)\Z"),
                  "DELETE": re.compile(r"/api/business/anti_tamper/[1-9][0-9]*\Z")}
_QUERY = {"page", "page_size", "action", "id", "start", "end", "ip", "host", "port", "event_id",
          "policy_rule_id", "begin_time", "end_time", "site_id", "time_preset", "url", "attack_type",
          "upstream", "global", "size", "top", "type", "count", "changed", "filename", "ja4_fingerprint",
          "site", "started_at_begin", "started_at_end", "begin"}


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
        if (path not in _PATHS and not _SITE.fullmatch(path) and not _CERT.fullmatch(path) and not _TAMPER_READ.fullmatch(path) and not _RECORD.fullmatch(path)) or (params and not set(params) <= _QUERY):
            raise SafeLineError("invalid_safeline_endpoint", 422)
        return await self._request("GET", path, params=params)

    async def write(self, method, path, payload):
        if ((method, path) not in _WRITES and not (method == "PUT" and _PROXY_WRITE.fullmatch(path)) and
                not (method in _APP_WRITES and _APP_WRITES[method].fullmatch(path)) and
                not (method in _TAMPER_WRITES and _TAMPER_WRITES[method].fullmatch(path))):
            raise SafeLineError("invalid_safeline_endpoint", 422)
        return await self._request(method, path, payload=payload)

    async def _request(self, method, path, *, params=None, payload=None):
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
                async with client.stream(method, config.url + path, params=params, json=payload, headers=headers, extensions=extensions) as response:
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

    async def log_lines(self, identifier, filename):
        """Read a short SSE snapshot, never keep a browser-facing stream open."""
        if not re.fullmatch(r"[1-9][0-9]*", identifier) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", filename) or filename in {".", ".."}:
            raise SafeLineError("invalid_safeline_query", 422)
        if self.connection is None:
            raise SafeLineError("safeline_not_configured", 409)
        config = self.connection
        headers = {"X-SLCE-API-TOKEN": config.token, "Accept": "text/event-stream"}
        extensions = {}
        if config.tls_server_name:
            extensions["sni_hostname"] = config.tls_server_name
            port = httpx.URL(config.url).port
            headers["Host"] = config.tls_server_name + (":" + str(port) if port and port != 443 else "")
        lines, buffer, size, truncated = [], "", 0, False
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            async with asyncio.timeout(3), self._slots, httpx.AsyncClient(
                verify=config.verify(), trust_env=False, follow_redirects=False, timeout=httpx.Timeout(3, connect=3),
                transport=_BorrowedTransport(self.transport) if self.transport else None,
            ) as client:
                async with client.stream("GET", config.url + "/api/open/site/" + identifier + "/log/detail", params={"filename": filename}, headers=headers, extensions=extensions) as response:
                    if response.status_code in {401, 403}:
                        raise SafeLineError("safeline_invalid_credentials" if response.status_code == 401 else "safeline_permission_denied")
                    if response.status_code == 404:
                        raise SafeLineError("safeline_endpoint_unavailable", 404)
                    if response.status_code in {301, 302, 303, 307, 308, 467}:
                        raise SafeLineError("safeline_sso_required")
                    if response.status_code != 200 or "text/event-stream" not in response.headers.get("content-type", ""):
                        raise SafeLineError("safeline_unexpected_response")
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 262144:
                            truncated = True; break
                        buffer = (buffer + decoder.decode(chunk)).replace("\r\n", "\n")
                        while "\n\n" in buffer:
                            block, buffer = buffer.split("\n\n", 1)
                            event, data = "", []
                            for part in block.split("\n"):
                                if part.startswith("event:"):
                                    event = part[6:].strip()
                                elif part.startswith("data:"):
                                    value = part[5:]
                                    data.append(value[1:] if value.startswith(" ") else value)
                            if event == "line":
                                lines.append("\n".join(data)[:8192])
                            if len(lines) >= 200:
                                truncated = True; break
                        if truncated:
                            break
        except (TimeoutError, httpx.TimeoutException):
            if not lines:
                raise SafeLineError("safeline_timeout") from None
            truncated = True
        except httpx.HTTPError as error:
            raise SafeLineError("safeline_tls_error" if "CERTIFICATE_VERIFY_FAILED" in str(error) else "safeline_unreachable") from None
        return lines, truncated
