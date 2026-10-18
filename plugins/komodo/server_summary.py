"""Read-only Server overview and sampled history for a bound PVE guest.

The caller resolves the guest association and authenticates PVE. Every request
here targets one fixed Komodo Server and returns selected display fields only.
"""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
import math
import re
import ssl

import httpx

from plugins.komodo.actions import KomodoActionError, _json, _object_id
from plugins.komodo.alerts import _timestamp
from plugins.komodo.containers import _container_id, _identity
from plugins.komodo.dashboard import _integer, _load_average, _number, _percentage, _text
from plugins.komodo.komodo import KomodoDashboardError
from plugins.komodo.updates import _row as _update_row


HISTORY_INTERVALS = ("5-sec", "15-sec", "30-sec", "1-min", "5-min", "15-min", "30-min", "1-hr", "6-hr", "1-day")
_POLL = re.compile(r"([1-9][0-9]{0,8})-(sec|min|hr|day|wk)")
_PERCENT = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)%")
_SIZE = r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\s?(?:B|kB|KB|MB|GB|TB|PB|KiB|MiB|GiB|TiB|PiB)"
_IO = re.compile(rf"{_SIZE}\s*/\s*{_SIZE}")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
_REQUEST_SECONDS = 10.0


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _server_id(value):
    if not isinstance(value, str) or _object_id(value) is None:
        raise KomodoDashboardError("invalid_request", 400)
    return value.lower()


def _seconds(value):
    match = _POLL.fullmatch(value) if isinstance(value, str) else None
    return int(match[1]) * {"sec": 1, "min": 60, "hr": 3600, "day": 86400, "wk": 604800}[match[2]] if match else None


def _rate(value, seconds):
    value = _number(value)
    return value / seconds if value is not None and seconds else None


def _plain(value, limit=16_384):
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 and char not in "\n\r\t" or ord(char) == 127 for char in value):
        return None
    return value


def description_version(server_id, description):
    if _plain(description) is None:
        return None
    return hashlib.sha256(json.dumps([server_id, description], ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


async def _read(variant, params, *, section, origin, headers, verify, transport):
    try:
        async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0)) as client:
            async with asyncio.timeout(_REQUEST_SECONDS):
                response = await client.post(origin + "/read", headers=headers, json={"type": variant, "params": params})
    except (httpx.HTTPError, TimeoutError):
        return None, section + "_unavailable"
    if response.status_code == 401:
        raise KomodoDashboardError("invalid_credentials")
    if response.status_code == 403:
        return None, section + "_permission_denied"
    if response.status_code != 200:
        return None, section + "_unavailable"
    payload = _json(response)
    if payload is None:
        return None, section + "_invalid_response"
    return payload, None


async def _authenticate(*, origin, key, secret, verify, transport):
    if not origin or not key or not secret:
        raise KomodoDashboardError("not_configured", 503)
    headers = {"x-api-key": key, "x-api-secret": secret}
    try:
        async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0)) as client:
            await _identity(client, origin, headers)
    except KomodoActionError as error:
        raise KomodoDashboardError(error.code, error.status_code) from None
    except (httpx.HTTPError, TimeoutError):
        raise KomodoDashboardError("upstream_unavailable") from None
    return headers


async def _sections(calls, **client_options):
    results = await asyncio.gather(*(_read(variant, params, section=section, **client_options)
                                     for section, variant, params in calls))
    data, errors = {}, []
    for (section, _, _), (payload, error) in zip(calls, results):
        data[section] = payload
        if error:
            errors.append(error)
    return data, errors


def _server(payload, identifier, error):
    if error:
        raise KomodoDashboardError(error, 403 if error.endswith("permission_denied") else 502)
    if (not isinstance(payload, dict) or _object_id(payload.get("_id")) != identifier
            or payload.get("template") is not False or _text(payload.get("name")) is None):
        raise KomodoDashboardError("komodo_server_unavailable", 409)
    return payload


def _current(stats, info, server_info):
    stats = stats if isinstance(stats, dict) else {}
    info = info if isinstance(info, dict) else {}
    server_info = server_info if isinstance(server_info, dict) else {}
    disks = []
    raw_disks = stats.get("disks")
    if isinstance(raw_disks, list):
        for disk in raw_disks:
            if not isinstance(disk, dict):
                continue
            disks.append({"mount": _text(disk.get("mount"), limit=2048),
                          "file_system": _text(disk.get("file_system")),
                          "used_gb": _number(disk.get("used_gb")), "total_gb": _number(disk.get("total_gb")),
                          "percent": _percentage(disk.get("used_gb"), disk.get("total_gb"))})
    # An absent disk sample is unknown. Never turn an empty list into a fake
    # zero-capacity/zero-usage reading or silently sum only valid disks.
    used = sum(disk["used_gb"] for disk in disks) if disks and all(disk["used_gb"] is not None for disk in disks) else None
    total = sum(disk["total_gb"] for disk in disks) if disks and all(disk["total_gb"] is not None for disk in disks) else None
    used = used if used is not None and math.isfinite(used) else None
    total = total if total is not None and math.isfinite(total) else None
    seconds = _seconds(stats.get("polling_rate"))
    version = server_info.get("version")
    version = version if isinstance(version, str) and len(version) <= 128 and _VERSION.fullmatch(version) else None
    return {
        "cpu_percent": _number(stats.get("cpu_perc")), "load_average": _load_average(stats),
        "memory_used_gb": _number(stats.get("mem_used_gb")), "memory_total_gb": _number(stats.get("mem_total_gb")),
        "memory_cache_gb": _number(stats.get("mem_buff_cache_gb")), "memory_arc_gb": _number(stats.get("mem_zfs_arc_gb")),
        "memory_percent": _percentage(stats.get("mem_used_gb"), stats.get("mem_total_gb")),
        "disk_used_gb": used, "disk_total_gb": total, "disk_percent": _percentage(used, total),
        "network_ingress_bytes_per_second": _rate(stats.get("network_ingress_bytes"), seconds),
        "network_egress_bytes_per_second": _rate(stats.get("network_egress_bytes"), seconds),
        "core_count": _integer(info.get("core_count")) or _integer(server_info.get("core_count")),
        "logical_core_count": _integer(info.get("logical_core_count")) or _integer(server_info.get("logical_core_count")),
        "version": version, "refresh_ts": _timestamp(stats.get("refresh_ts")), "uptime_seconds": None,
    }, disks


def _formatted(value, pattern):
    return value if isinstance(value, str) and len(value) <= 128 and pattern.fullmatch(value) else None


def _containers(payload, identifier):
    if not isinstance(payload, list):
        return [], "containers_invalid_response"
    result, invalid = [], False
    for item in payload:
        if not isinstance(item, dict):
            invalid = True
            continue
        if item.get("server_id") is not None and _object_id(item.get("server_id")) != identifier:
            invalid = True
            continue
        stats = item.get("stats")
        if stats is None:
            continue  # The original Server stats table only includes measured containers.
        if not isinstance(stats, dict) or _container_id(item.get("id")) is None or _text(item.get("name")) is None:
            invalid = True
            continue
        state = item.get("state")
        result.append({"id": _container_id(item["id"]), "name": item["name"],
                       "state": state if isinstance(state, str) and state in {"running", "paused", "created", "restarting", "exited", "dead", "stopping", "removing"} else "unknown",
                       "cpu_percent": _formatted(stats.get("cpu_perc"), _PERCENT),
                       "memory_percent": _formatted(stats.get("mem_perc"), _PERCENT),
                       "memory_usage": _formatted(stats.get("mem_usage"), _IO),
                       "network_io": _formatted(stats.get("net_io"), _IO),
                       "block_io": _formatted(stats.get("block_io"), _IO),
                       "pids": _formatted(stats.get("pids"), re.compile(r"[0-9]{1,12}"))})
    return result, "containers_invalid_response" if invalid else None


def _processes(payload):
    if not isinstance(payload, list):
        return [], "processes_invalid_response"
    result, invalid = [], False
    for process in payload:
        if not isinstance(process, dict):
            invalid = True
            continue
        name = _text(process.get("name"))
        if name is None:
            invalid = True
            continue
        pid = process.get("pid")
        result.append({"pid": pid if type(pid) is int and 0 <= pid <= 4_294_967_295 else None,
                       "name": name, "exe": _text(process.get("exe"), limit=4096),
                       "cpu_percent": _number(process.get("cpu_perc")), "memory_mb": _number(process.get("mem_mb"))})
    return result, "processes_invalid_response" if invalid else None


async def _base_summary(server_id, *, origin, key, secret, verify, transport):
    identifier = _server_id(server_id)
    headers = await _authenticate(origin=origin, key=key, secret=secret, verify=verify, transport=transport)
    options = {"origin": origin, "headers": headers, "verify": verify, "transport": transport}
    base, errors = await _sections((
        ("server", "GetServer", {"server": identifier}),
        ("state", "ListServers", {"query": {"names": [identifier], "templates": "Include"}, "limit": 0}),
        ("permission", "GetPermission", {"target": {"type": "Server", "id": identifier}}),
        ("policy", "GetCoreInfo", {}),
    ), **options)
    server = _server(base["server"], identifier, next((error for error in errors if error.startswith("server_")), None))
    matches = [item for item in base["state"] if isinstance(item, dict) and _object_id(item.get("id")) == identifier] if isinstance(base["state"], list) else []
    info = matches[0].get("info") if len(matches) == 1 else None
    info = info if isinstance(info, dict) else {}
    raw_state = info.get("state")
    state = raw_state if isinstance(raw_state, str) and raw_state in {"Ok", "NotOk", "Disabled"} else "NotOk"
    if not matches and not any(error.startswith("state_") for error in errors):
        errors.append("state_invalid_response")
    policy, permission = base.get("policy"), base.get("permission")
    if policy is not None and (not isinstance(policy, dict) or type(policy.get("ui_write_disabled")) is not bool):
        errors.append("policy_invalid_response")
        policy = None
    if permission is not None and not isinstance(permission, dict):
        errors.append("permission_invalid_response")
        permission = None
    writable = isinstance(policy, dict) and not policy["ui_write_disabled"]
    level = permission.get("level") if isinstance(permission, dict) else None
    enabled = isinstance(server.get("config"), dict) and server["config"].get("enabled") is True
    capabilities = {"server_id": identifier, "server_name": server["name"], "server_state": state, "enabled": enabled,
                    "can_write": bool(writable and level == "Write"),
                    # ui_write_disabled controls config editing in the original
                    # UI; execution buttons use Execute/Write independently.
                    "can_execute": bool(isinstance(level, str) and level in {"Execute", "Write"} and state == "Ok" and enabled)}
    return server, info, capabilities, errors, options


async def get_server_summary_capabilities(server_id, *, origin, key, secret, verify: ssl.SSLContext | bool = True,
                                          transport: httpx.AsyncBaseTransport | None = None):
    _, _, capabilities, errors, _ = await _base_summary(server_id, origin=origin, key=key, secret=secret, verify=verify, transport=transport)
    return {**capabilities, "errors": list(dict.fromkeys(errors)), "updated_at": _now()}


async def get_server_summary(server_id, *, origin, key, secret, verify: ssl.SSLContext | bool = True,
                             transport: httpx.AsyncBaseTransport | None = None):
    server, info, capabilities, errors, options = await _base_summary(server_id, origin=origin, key=key, secret=secret, verify=verify, transport=transport)
    identifier, state = capabilities["server_id"], capabilities["server_state"]
    calls = [("updates", "ListUpdates", {"page": 0, "query": {"target.type": "Server", "target.id": identifier}})]
    if state == "Ok" and capabilities["enabled"]:
        calls.extend((
            ("stats", "GetSystemStats", {"server": identifier}), ("information", "GetSystemInformation", {"server": identifier}),
            ("containers", "ListContainers", {"server": identifier}), ("processes", "ListSystemProcesses", {"server": identifier}),
        ))
    else:
        errors.append("server_disconnected")
    measured, measure_errors = await _sections(calls, **options)
    errors.extend(measure_errors)
    stats, system = measured.get("stats"), measured.get("information")
    if stats is not None and not isinstance(stats, dict):
        errors.append("stats_invalid_response")
        stats = None
    if system is not None and not isinstance(system, dict):
        errors.append("information_invalid_response")
        system = None
    current, disks = _current(stats, system, info)
    containers, container_error = _containers(measured["containers"], identifier) if measured.get("containers") is not None else ([], None)
    processes, process_error = _processes(measured["processes"]) if measured.get("processes") is not None else ([], None)
    errors.extend(error for error in (container_error, process_error) if error)
    updates = []
    raw_updates = measured.get("updates")
    if raw_updates is not None:
        if not isinstance(raw_updates, dict) or not isinstance(raw_updates.get("updates"), list):
            errors.append("updates_invalid_response")
        else:
            for raw in raw_updates["updates"][:100]:
                if not isinstance(raw, dict):
                    errors.append("updates_invalid_response")
                    continue
                row = _update_row(raw)
                if row["resource_type"] != "Server" or row["resource_id"] != identifier:
                    errors.append("updates_invalid_response")
                    continue
                row["resource_name"] = server["name"]
                updates.append(row)
            updates.sort(key=lambda item: item["start_ts"] if item["start_ts"] is not None else -1, reverse=True)
            updates = updates[:20]
    return {**capabilities,
            "description": _plain(server.get("description")),
            "description_version": description_version(identifier, server.get("description")), "current": current,
            "containers": containers, "disks": disks, "processes": processes, "updates": updates,
            "errors": list(dict.fromkeys(errors)), "updated_at": _now()}


async def update_server_description(server_id, *, description, base_version, before_write=None,
                                    origin, key, secret, verify: ssl.SSLContext | bool = True,
                                    transport: httpx.AsyncBaseTransport | None = None):
    if (_plain(description) is None or not isinstance(base_version, str)
            or re.fullmatch(r"[0-9a-f]{64}", base_version) is None):
        raise KomodoDashboardError("invalid_server_description", 422)
    server, _, capabilities, _, options = await _base_summary(
        server_id, origin=origin, key=key, secret=secret, verify=verify, transport=transport)
    if not capabilities["can_write"]:
        raise KomodoDashboardError("server_description_read_only", 403)
    if description_version(capabilities["server_id"], server.get("description")) != base_version:
        raise KomodoDashboardError("server_description_conflict", 409)
    if before_write is not None:
        await before_write()
    # Resource metadata has its own RPC. Omit template/tags entirely and never
    # send the Server config or a browser-selected ResourceTarget.
    result = {"ok": False, "status": "outcome_unknown", "operation_id": None, "error_code": "outcome_unknown"}
    try:
        async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0)) as client:
            async with asyncio.timeout(_REQUEST_SECONDS):
                response = await client.post(origin + "/write", headers=options["headers"], json={
                    "type": "UpdateResourceMeta", "params": {
                        "target": {"type": "Server", "id": capabilities["server_id"]}, "description": description,
                    },
                })
    except (httpx.HTTPError, TimeoutError):
        return result
    if response.status_code == 200 and _json(response) == {}:
        return {**result, "ok": True, "status": "completed", "error_code": None}
    if response.status_code in {400, 401, 403, 404, 409, 422}:
        return {**result, "status": "failed", "error_code": "server_description_rejected"}
    return result


async def get_server_history(server_id, *, interval="1-day", origin, key, secret, verify: ssl.SSLContext | bool = True,
                             transport: httpx.AsyncBaseTransport | None = None):
    identifier = _server_id(server_id)
    if not isinstance(interval, str) or interval not in HISTORY_INTERVALS:
        raise KomodoDashboardError("invalid_request", 400)
    headers = await _authenticate(origin=origin, key=key, secret=secret, verify=verify, transport=transport)
    data, errors = await _sections((
        ("server", "GetServer", {"server": identifier}),
        ("history", "GetHistoricalServerStats", {"server": identifier, "granularity": interval, "page": 0}),
    ), origin=origin, headers=headers, verify=verify, transport=transport)
    _server(data["server"], identifier, next((error for error in errors if error.startswith("server_")), None))
    payload = data["history"]
    points, seen = [], set()
    if payload is not None:
        if not isinstance(payload, dict) or not isinstance(payload.get("stats"), list) or len(payload["stats"]) > 200:
            errors.append("history_invalid_response")
        else:
            for record in payload["stats"]:
                if not isinstance(record, dict) or _object_id(record.get("sid")) != identifier:
                    errors.append("history_invalid_response")
                    continue
                ts = _timestamp(record.get("ts"))
                if ts is None or ts in seen:
                    errors.append("history_invalid_response")
                    continue
                seen.add(ts)
                load = _load_average(record)
                points.append({"ts": ts, "load_average": load,
                               "load1": load[0] if load else None, "load5": load[1] if load else None, "load15": load[2] if load else None,
                               "cpu_percent": _number(record.get("cpu_perc")),
                               "memory_used_gb": _number(record.get("mem_used_gb")), "memory_total_gb": _number(record.get("mem_total_gb")),
                               "memory_cache_gb": _number(record.get("mem_buff_cache_gb")), "memory_arc_gb": _number(record.get("mem_zfs_arc_gb")),
                               "memory_percent": _percentage(record.get("mem_used_gb"), record.get("mem_total_gb")),
                               "disk_used_gb": _number(record.get("disk_used_gb")), "disk_total_gb": _number(record.get("disk_total_gb")),
                               "disk_percent": _percentage(record.get("disk_used_gb"), record.get("disk_total_gb")),
                               # Historical records never stored polling_rate.
                               # Granularity selects snapshots, not aggregate
                               # windows: dividing by it would invent a rate.
                               "network_ingress_bytes": _number(record.get("network_ingress_bytes")),
                               "network_egress_bytes": _number(record.get("network_egress_bytes"))})
    points.sort(key=lambda item: item["ts"])
    return {"server_id": identifier, "interval": interval, "points": points, "network_unit": "bytes_per_sample",
            "errors": list(dict.fromkeys(errors)), "updated_at": _now()}
