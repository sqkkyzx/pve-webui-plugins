"""Bounded read-only node-temperature collectors, independent of Komodo."""

import asyncio
from dataclasses import dataclass, field
import ipaddress
import json
import math
from pathlib import Path
import re
import ssl
import time
from urllib.parse import urlsplit

import certifi
import httpx

from plugins.temperatures.node_exporter_temperatures import parse_node_exporter_temperatures


NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}\Z")
_SENSOR_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MAX_BYTES = 262_144
_MAX_METRICS_BYTES = 4 * 1024 * 1024
_CACHE_SECONDS = 5.0
_STALE_SECONDS = 30.0
_FUTURE_SECONDS = 10.0
_FIELDS = {"id", "chip", "label", "category", "celsius", "max_celsius", "critical_celsius"}


def _now():
    return time.time()


def _monotonic():
    return time.monotonic()


@dataclass(frozen=True, repr=False)
class TemperatureSource:
    origin: str
    token: str | None = field(repr=False)
    verify: ssl.SSLContext | bool = field(repr=False)
    kind: str = "collector"


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("invalid_json_number")


def _json(value):
    return json.loads(value, object_pairs_hook=_object, parse_constant=_constant)


def _origin(value, allow_http):
    if (not isinstance(value, str) or not value or any(ord(char) <= 32 or ord(char) >= 127 for char in value)
            or any(char in value for char in "\\?#")):
        raise ValueError("invalid_origin")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or parsed.netloc.endswith(":")):
        raise ValueError("invalid_origin")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("invalid_origin")
    if parsed.scheme == "http" and not allow_http:
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            raise ValueError("http_requires_explicit_opt_in")
    return str(httpx.URL(value)).rstrip("/")


def load_temperature_sources(filename):
    """Validate once at startup; never include configuration values in errors."""
    if filename is None:
        return {}
    try:
        if not isinstance(filename, (str, Path)) or not str(filename):
            raise ValueError("invalid_file")
        path = Path(filename)
        with path.open("rb") as handle:
            encoded = handle.read(_MAX_BYTES + 1)
        if len(encoded) > _MAX_BYTES:
            raise ValueError("configuration_too_large")
        data = _json(encoded)
        if not isinstance(data, dict) or set(data) != {"nodes"} or not isinstance(data["nodes"], dict) or len(data["nodes"]) > 64:
            raise ValueError("invalid_configuration")
        sources = {}
        for node, source in data["nodes"].items():
            if (not isinstance(node, str) or NODE.fullmatch(node) is None or not isinstance(source, dict)
                    or set(source) - {"url", "token", "ca_file", "allow_http", "type"} or "url" not in source):
                raise ValueError("invalid_source")
            kind = source.get("type", "collector")
            if kind not in {"collector", "node_exporter"}:
                raise ValueError("invalid_source_type")
            token = source.get("token")
            if kind == "collector" and token is None:
                raise ValueError("invalid_token")
            if token is not None and (not isinstance(token, str) or not 32 <= len(token) <= 256
                    or any(ord(char) <= 32 or ord(char) >= 127 for char in token)):
                raise ValueError("invalid_token")
            allow_http = source.get("allow_http", False)
            if type(allow_http) is not bool:
                raise ValueError("invalid_http_policy")
            origin = _origin(source["url"], allow_http)
            ca_file = source.get("ca_file")
            if ca_file is not None:
                if not isinstance(ca_file, str) or not ca_file or "\x00" in ca_file:
                    raise ValueError("invalid_ca_file")
                ca_path = Path(ca_file)
                if not ca_path.is_absolute():
                    ca_path = path.parent / ca_path
                if not ca_path.is_file():
                    raise ValueError("invalid_ca_file")
                ca_file = str(ca_path)
            context = ssl.create_default_context(cafile=ca_file or certifi.where())
            sources[node] = TemperatureSource(origin, token, context, kind)
        return sources
    except Exception:
        raise ValueError("Invalid PVE_TEMPERATURES_SOURCES_FILE configuration") from None


def _label(value):
    return (isinstance(value, str) and 1 <= len(value) <= 128 and value.strip()
            and not any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value))


def _number(value):
    return type(value) in {int, float} and -100 <= value <= 250 and math.isfinite(value)


def validate_temperatures(data, node, now):
    if (not isinstance(data, dict) or set(data) != {"version", "node", "sampled_at", "sensors"}
            or type(data["version"]) is not int or data["version"] != 1 or data["node"] != node
            or type(data["sampled_at"]) not in {int, float} or not 0 <= data["sampled_at"] <= now + _FUTURE_SECONDS
            or not math.isfinite(data["sampled_at"])
            or not isinstance(data["sensors"], list) or len(data["sensors"]) > 256):
        raise ValueError("invalid_temperature_response")
    sensors, seen = [], set()
    for row in data["sensors"]:
        if (not isinstance(row, dict) or set(row) != _FIELDS or not isinstance(row["id"], str)
                or _SENSOR_ID.fullmatch(row["id"]) is None or row["id"] in seen
                or not _label(row["chip"]) or not _label(row["label"])
                or not isinstance(row["category"], str) or row["category"] not in {"cpu", "board", "disk", "other"}
                or not _number(row["celsius"])
                or any(row[field] is not None and not _number(row[field]) for field in ("max_celsius", "critical_celsius"))):
            raise ValueError("invalid_temperature_response")
        seen.add(row["id"])
        sensors.append({field: row[field] for field in _FIELDS})
    return {"version": 1, "node": node, "sampled_at": data["sampled_at"], "sensors": sensors}


def _public(node, payload, *, unavailable=False):
    response = {"version": 1, "node": node, "sampled_at": None, "sensors": [], "status": "unavailable" if unavailable else "not_configured"}
    if payload is None:
        return response
    response["sampled_at"] = payload["sampled_at"]
    age = _now() - payload["sampled_at"]
    if age > _STALE_SECONDS:
        response["status"] = "stale"
    elif age < -_FUTURE_SECONDS:
        response.update(sampled_at=None, status="unavailable")
    else:
        response.update(sensors=[dict(row) for row in payload["sensors"]], status="ok" if payload["sensors"] else "no_sensors")
    return response


class TemperatureService:
    def __init__(self, sources, *, transport=None):
        self.sources = sources
        self.transport = transport
        self._exporter_verify = ssl.create_default_context(cafile=certifi.where())
        self._cache = {}
        self._locks = {node: asyncio.Lock() for node in sources}

    def discovered_source(self, origin):
        return TemperatureSource(origin, None, self._exporter_verify, "node_exporter")

    async def _fetch(self, node, source):
        try:
            exporter = source.kind == "node_exporter"
            limit = _MAX_METRICS_BYTES if exporter else _MAX_BYTES
            headers = {"Accept": "text/plain; version=0.0.4" if exporter else "application/json", "Accept-Encoding": "identity"}
            if source.token is not None:
                headers["Authorization"] = "Bearer " + source.token
            async with asyncio.timeout(4):
                async with httpx.AsyncClient(verify=source.verify, transport=self.transport, trust_env=False,
                        follow_redirects=False, timeout=httpx.Timeout(4.0, connect=4.0)) as client:
                    async with client.stream("GET", source.origin + ("/metrics" if exporter else "/v1/temperatures"),
                            params={"collect[]": "hwmon"} if exporter else None, headers=headers) as response:
                        if response.status_code != 200 or response.headers.get("content-encoding", "identity").lower() != "identity":
                            return None
                        if exporter and response.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in {
                                "text/plain", "application/openmetrics-text"}:
                            return None
                        length = response.headers.get("content-length")
                        if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > limit):
                            return None
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > limit:
                                return None
                            body.extend(chunk)
                        if exporter:
                            payload = validate_temperatures(parse_node_exporter_temperatures(
                                bytes(body).decode("utf-8"), node, _now()), node, _now())
                        else:
                            payload = validate_temperatures(_json(bytes(body)), node, _now())
                        if any((source.token is not None and source.token in row[field]) or source.origin in row[field]
                               for row in payload["sensors"] for field in ("id", "chip", "label")):
                            return None
                        return payload
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, OverflowError):
            return None

    async def get(self, node, source=None):
        source = source or self.sources.get(node)
        if source is None:
            return _public(node, None)
        if node not in self._locks:
            if len(self._locks) >= 256:
                return _public(node, None, unavailable=True)
            self._locks[node] = asyncio.Lock()
        async with self._locks[node]:
            cached = self._cache.get(node)
            identity = (source.origin, source.kind, source.token)
            if cached is None or _monotonic() >= cached[0] or cached[2] != identity:
                payload = await self._fetch(node, source)
                cached = (_monotonic() + _CACHE_SECONDS, payload, identity)
                self._cache[node] = cached
            return _public(node, cached[1], unavailable=cached[1] is None)
