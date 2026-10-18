"""Guest-bound Komodo Server options with strict partial writes.

The route owns PVE authorization, CSRF and durable request reservations. This
module revalidates the guest association and exposes only the original Server
configuration page's fields, never deprecated passkeys or private key material.
"""

import asyncio
from datetime import date
import hashlib
import json
import math
import re
import ssl
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from plugins.komodo.actions import KomodoActionError, _json, _object_id, _result, _submitted_result
from plugins.komodo.containers import _identity
from plugins.komodo.guest_links import GuestLinkError, GuestLinkStore, PveGuestClient, assess_link


BOOL_FIELDS = (
    "enabled", "insecure_tls", "auto_rotate_keys", "stats_monitoring", "auto_prune",
    "send_unreachable_alerts", "send_version_mismatch_alerts", "send_cpu_alerts", "send_mem_alerts", "send_disk_alerts",
)
NUMBER_FIELDS = ("cpu_warning", "cpu_critical", "mem_warning", "mem_critical", "disk_warning", "disk_critical")
STRING_FIELDS = ("address", "external_address", "region")
CONFIG_FIELDS = (*BOOL_FIELDS, *STRING_FIELDS, "ignore_mounts", *NUMBER_FIELDS, "maintenance_windows")
WINDOW_FIELDS = ("name", "description", "schedule_type", "day_of_week", "date", "hour", "minute", "duration_minutes", "timezone", "enabled")
_WINDOW_DEFAULTS = {"description": "", "schedule_type": "Daily", "day_of_week": "", "date": "", "hour": 0, "minute": 0, "timezone": "", "enabled": True}
_WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
_HASH = re.compile(r"[0-9a-f]{64}")
_REQUEST_SECONDS = 10.0


def _invalid(code="invalid_server_options", status=422):
    raise KomodoActionError(code, status)


def _text(value, limit, *, required=False):
    if (not isinstance(value, str) or len(value) > limit or required and not value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        _invalid()
    return value


def _address(value):
    value = _text(value, 2048)
    if not value:
        return value
    if any(character.isspace() for character in value) or any(character in value for character in "\\?#"):
        _invalid()
    try:
        parsed = urlsplit(value if "://" in value else "//" + value)
        if (parsed.scheme not in {"", "http", "https", "ws", "wss"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.port == 0):
            _invalid()
    except ValueError:
        _invalid()
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid()
    try:
        finite = math.isfinite(value)
    except (OverflowError, ValueError):
        finite = False
    if not finite:
        _invalid()
    return value


def _strings(value):
    if not isinstance(value, list) or len(value) > 256:
        _invalid()
    return [_text(item, 2048) for item in value]


def _windows(value):
    if not isinstance(value, list) or len(value) > 128:
        _invalid()
    result = []
    for window in value:
        if not isinstance(window, dict) or set(window) - set(WINDOW_FIELDS):
            _invalid()
        merged = {**_WINDOW_DEFAULTS, **window}
        if not {"name", "duration_minutes"} <= set(window):
            _invalid()
        for field, limit in (("name", 256), ("description", 4096), ("day_of_week", 32), ("date", 10), ("timezone", 128)):
            _text(merged[field], limit, required=field == "name")
        if (not isinstance(merged["schedule_type"], str) or merged["schedule_type"] not in {"Daily", "Weekly", "OneTime"}
                or type(merged["enabled"]) is not bool):
            _invalid()
        for field, minimum, maximum in (("hour", 0, 23), ("minute", 0, 59), ("duration_minutes", 1, 4_294_967_295)):
            if type(merged[field]) is not int or not minimum <= merged[field] <= maximum:
                _invalid()
        if merged["day_of_week"] and merged["day_of_week"] not in _WEEKDAYS:
            _invalid()
        if merged["schedule_type"] == "Weekly" and not merged["day_of_week"]:
            _invalid()
        if merged["date"]:
            try:
                if date.fromisoformat(merged["date"]).isoformat() != merged["date"]:
                    _invalid()
            except ValueError:
                _invalid()
        if merged["schedule_type"] == "OneTime" and not merged["date"]:
            _invalid()
        if merged["timezone"]:
            try:
                ZoneInfo(merged["timezone"])
            except (ZoneInfoNotFoundError, ValueError):
                _invalid()
        result.append({field: merged[field] for field in WINDOW_FIELDS})
    return result


def validate_patch(config) -> dict:
    if not isinstance(config, dict) or not config or set(config) - set(CONFIG_FIELDS):
        _invalid()
    result = {}
    for field, value in config.items():
        if field in BOOL_FIELDS:
            if type(value) is not bool:
                _invalid()
            result[field] = value
        elif field in NUMBER_FIELDS:
            # Core does not impose 0..100 or warning<=critical. Preserve those
            # semantics rather than modifying legitimate existing thresholds.
            result[field] = _number(value)
        elif field in {"address", "external_address"}:
            result[field] = _address(value)
        elif field == "region":
            result[field] = _text(value, 256)
        elif field == "ignore_mounts":
            result[field] = _strings(value)
        elif field == "maintenance_windows":
            result[field] = _windows(value)
    return result


def validate_public_key(value) -> str:
    # The original UI and Core accept an explicit string, including empty to
    # clear the per-server value. Do not invent a base64/algorithm restriction.
    return _text(value, 8192)


class GuestBoundOptionsService:
    def __init__(self, store: GuestLinkStore | None, pve: PveGuestClient) -> None:
        self.store, self.pve = store, pve

    async def resolve(self, request, guest_type: str, vmid: int, *, expected_server_id: str | None = None,
                      expected_revision: int | None = None) -> dict:
        if not isinstance(guest_type, str) or guest_type not in {"qemu", "lxc"} or type(vmid) is not int or not 100 <= vmid <= 999_999_999:
            _invalid("invalid_guest_options_target")
        if self.store is None:
            _invalid("guest_link_store_not_configured", 503)
        links = [link for link in self.store.list() if link["guest_type"] == guest_type and link["vmid"] == vmid]
        if len(links) != 1:
            _invalid("guest_not_linked", 409)
        link = links[0]
        if expected_server_id is not None and (not isinstance(expected_server_id, str) or expected_server_id.lower() != link["server_id"]):
            _invalid("guest_link_revision_conflict", 409)
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != link["revision"]):
            _invalid("guest_link_revision_conflict", 409)
        guests = await self.pve.list_guests(request)
        guest = next((guest for guest in guests if guest["guest_type"] == guest_type and guest["vmid"] == vmid), None)
        if guest is None or guest["template"]:
            _invalid("pve_guest_inaccessible", 409)
        identity = await self.pve.fingerprint(request, guest)
        assessed = assess_link(link, guests, identity)
        if not assessed["can_navigate"]:
            _invalid("guest_identity_changed", 409)
        # An unlink/rebind may have occurred while the read-only PVE checks ran.
        latest = self.store.get(link["server_id"])
        if latest != link:
            _invalid("guest_link_revision_conflict", 409)
        return {"server_id": link["server_id"], "binding_revision": link["revision"], "guest_type": guest_type, "vmid": vmid}


async def _rpc(variant: str, params: dict, *, origin: str, headers: dict, verify, transport):
    async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                 timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0)) as client:
        async with asyncio.timeout(_REQUEST_SECONDS):
            response = await client.post(origin + "/read", headers=headers, json={"type": variant, "params": params})
    if response.status_code == 401:
        _invalid("invalid_credentials", 502)
    if response.status_code == 403:
        _invalid("permission_denied", 403)
    if response.status_code == 404:
        _invalid("komodo_server_unavailable", 409)
    if response.status_code != 200:
        _invalid("upstream_unavailable", 502)
    return _json(response)


async def get_server_options(server_id: str, *, origin: str | None, key: str | None, secret: str | None,
                             verify: ssl.SSLContext | bool, transport: httpx.AsyncBaseTransport | None) -> dict:
    if not isinstance(server_id, str) or _object_id(server_id) is None:
        _invalid()
    server_id = server_id.lower()
    if not origin or not key or not secret:
        _invalid("not_configured", 503)
    headers = {"x-api-key": key, "x-api-secret": secret}
    try:
        async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0)) as client:
            await _identity(client, origin, headers)
        async with asyncio.timeout(_REQUEST_SECONDS):
            server, core, permission, items = await asyncio.gather(*(
                _rpc(variant, params, origin=origin, headers=headers, verify=verify, transport=transport)
                for variant, params in (
                    ("GetServer", {"server": server_id}), ("GetCoreInfo", {}),
                    ("GetPermission", {"target": {"type": "Server", "id": server_id}}),
                    ("ListServers", {"query": {"names": [server_id], "templates": "Include"}, "limit": 0}),
                )
            ))
    except (httpx.HTTPError, TimeoutError):
        _invalid("upstream_unavailable", 502)
    if (not isinstance(server, dict) or _object_id(server.get("_id")) != server_id or server.get("template") is not False
            or not isinstance(server.get("config"), dict) or not isinstance(server.get("info"), dict)
            or not isinstance(server.get("name"), str) or len(server["name"]) > 512):
        _invalid("komodo_server_unavailable", 409)
    if not isinstance(core, dict) or type(core.get("ui_write_disabled")) is not bool or not isinstance(permission, dict):
        _invalid("invalid_response", 502)
    config = {}
    for field in CONFIG_FIELDS:
        if field not in server["config"]:
            config[field] = None
            continue
        try:
            config[field] = validate_patch({field: server["config"][field]})[field]
        except KomodoActionError:
            # Unsupported stored values are not truncated or substituted. The
            # caller must inspect/repair upstream configuration explicitly.
            _invalid("unsupported_server_options", 502)
    public_key = server["info"].get("public_key", "")
    try:
        public_key = validate_public_key(public_key)
    except KomodoActionError:
        _invalid("unsupported_server_options", 502)
    if not isinstance(items, list):
        _invalid("invalid_response", 502)
    matches = [item for item in items if isinstance(item, dict) and _object_id(item.get("id")) == server_id]
    if len(matches) != 1 or not isinstance(matches[0].get("info"), dict):
        _invalid("komodo_server_unavailable", 409)
    raw_state = matches[0]["info"].get("state")
    state = raw_state if isinstance(raw_state, str) and raw_state in {"Ok", "NotOk", "Disabled"} else "NotOk"
    version = hashlib.sha256(json.dumps({
        "server_id": server_id, "config": config, "public_key": public_key, "updated_at": server.get("updated_at"),
    }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    can_write = not core["ui_write_disabled"] and permission.get("level") == "Write"
    return {"server_id": server_id, "server_name": server["name"], "server_state": state,
            "config": config, "public_key": public_key, "version": version,
            "can_write": can_write, "can_rotate": can_write and state == "Ok"}


async def update_server_options(server_id: str, *, action: str, base_version: str, config=None, public_key=None,
                                before_write=None, origin: str | None, key: str | None, secret: str | None,
                                verify: ssl.SSLContext | bool, transport: httpx.AsyncBaseTransport | None) -> dict:
    if not isinstance(action, str) or action not in {"save", "public_key", "rotate_keys"} or not isinstance(base_version, str) or _HASH.fullmatch(base_version) is None:
        _invalid()
    if action == "save":
        if public_key is not None:
            _invalid()
        patch = validate_patch(config)
    elif action == "public_key":
        if config is not None:
            _invalid()
        public_key = validate_public_key(public_key)
    elif config is not None or public_key is not None:
        _invalid()
    snapshot = await get_server_options(server_id, origin=origin, key=key, secret=secret, verify=verify, transport=transport)
    server_id = snapshot["server_id"]
    if not snapshot["can_write"]:
        _invalid("server_options_read_only", 403)
    if snapshot["version"] != base_version:
        _invalid("server_options_conflict", 409)
    if action == "rotate_keys" and not snapshot["can_rotate"]:
        _invalid("komodo_server_unavailable", 409)
    if before_write is not None:
        await before_write()
    variant, params = {
        "save": ("UpdateServer", {"id": server_id, "config": patch if action == "save" else None}),
        "public_key": ("UpdateServerPublicKey", {"server": server_id, "public_key": public_key}),
        "rotate_keys": ("RotateServerKeys", {"server": server_id}),
    }[action]
    try:
        async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0)) as client:
            async with asyncio.timeout(_REQUEST_SECONDS):
                response = await client.post(origin + "/write", headers={"x-api-key": key, "x-api-secret": secret},
                                             json={"type": variant, "params": params})
    except (httpx.HTTPError, TimeoutError):
        return {"ok": False, "status": "outcome_unknown", "operation_id": None, "error_code": "outcome_unknown"}
    if action == "save" and 200 <= response.status_code < 300:
        raw = _json(response)
        if isinstance(raw, dict) and _object_id(raw.get("_id")) == server_id:
            result = _result(server_id, "completed")
        else:
            result = _result(server_id, "outcome_unknown", error_code="outcome_unknown")
    else:
        # Both public-key writes use UpdateServerKey, not their RPC variant.
        result = _submitted_result(response, "servers", action, server_id, "UpdateServerKey")
    return {field: result[field] for field in ("ok", "status", "operation_id", "error_code")}
