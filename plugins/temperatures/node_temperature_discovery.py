"""Discover fixed node_exporter origins from the authenticated PVE inventory.

PVE/API2/Cluster.pm's GET /cluster/status also returns a local node with its
resolved IP and online=1 when no cluster is configured. Never resolve a browser
hostname or carry PVE credentials on to the discovered exporter.
"""

import asyncio
from dataclasses import dataclass
import ipaddress
import json
import re
from urllib.parse import urlsplit

import httpx


_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}\Z")
_MAX_BYTES = 262_144
_TIMEOUT_SECONDS = 5.0


class DiscoveryError(Exception):
    code = "node_temperature_discovery_failed"

    def __init__(self):
        super().__init__(self.code)


@dataclass(frozen=True, repr=False)
class DiscoveryTarget:
    node: str
    origin: str
    address: str


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("invalid_number")


def _address(value):
    if not isinstance(value, str) or len(value) > 45 or "%" in value:
        raise ValueError("invalid_address")
    address = ipaddress.ip_address(value)
    if (address.is_loopback or address.is_unspecified or address.is_link_local
            or address.is_multicast or address.is_reserved
            or isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None):
        raise ValueError("invalid_address")
    return address


def _target(payload, requested_node):
    if (not isinstance(payload, dict) or "errors" in payload or "NeedTFA" in payload
            or payload.get("success", True) is not True or not isinstance(payload.get("data"), list)):
        raise ValueError("invalid_inventory")
    nodes, addresses = {}, set()
    for row in payload["data"]:
        if not isinstance(row, dict):
            raise ValueError("invalid_inventory")
        if row.get("type") != "node":
            continue
        name, online = row.get("name"), row.get("online")
        if (not isinstance(name, str) or _NODE.fullmatch(name) is None or name in nodes
                or type(online) not in {int, bool} or online not in {0, 1}):
            raise ValueError("invalid_inventory")
        address = None
        if online:
            address = _address(row.get("ip"))
            if address in addresses:
                raise ValueError("conflicting_address")
            addresses.add(address)
        nodes[name] = address
    address = nodes.get(requested_node)
    if address is None:
        return None
    literal = str(address)
    host = "[" + literal + "]" if address.version == 6 else literal
    return DiscoveryTarget(requested_node, "http://" + host + ":9100", literal)


class NodeTemperatureDiscovery:
    def __init__(self, authenticator, *, transport=None):
        # The authenticator is configured by the application, never by a request.
        upstream = authenticator.upstream
        parsed = urlsplit(upstream)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment):
            raise ValueError("Node temperature discovery requires a fixed PVE HTTPS origin")
        _ = parsed.port
        self.upstream = upstream.rstrip("/")
        self.verify = authenticator.verify
        self.transport = transport if transport is not None else getattr(authenticator, "transport", None)

    async def resolve(self, request, node):
        """Call only after PVE authentication; no node/IP cache crosses sessions."""
        cookie = request.cookies.get("PVEAuthCookie", "")
        if (not isinstance(node, str) or _NODE.fullmatch(node) is None or not cookie or len(cookie) > 4096
                or any(ord(char) < 0x21 or ord(char) > 0x7E or char in ";," for char in cookie)):
            raise DiscoveryError()
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(verify=self.verify, transport=self.transport, trust_env=False,
                        follow_redirects=False, timeout=httpx.Timeout(_TIMEOUT_SECONDS)) as client:
                    async with client.stream("GET", self.upstream + "/api2/json/cluster/status", headers={
                            "Cookie": "PVEAuthCookie=" + cookie, "Accept": "application/json",
                            "Accept-Encoding": "identity"}) as response:
                        if response.status_code != 200 or response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise DiscoveryError()
                        length = response.headers.get("content-length")
                        if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > _MAX_BYTES):
                            raise DiscoveryError()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > _MAX_BYTES:
                                raise DiscoveryError()
                            body.extend(chunk)
                        payload = json.loads(bytes(body), object_pairs_hook=_object, parse_constant=_constant)
                        return _target(payload, node)
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, OverflowError, RecursionError):
            raise DiscoveryError() from None
