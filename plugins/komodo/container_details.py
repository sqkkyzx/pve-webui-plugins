"""Exact server/full-ID container detail RPCs; callers own PVE auth and receipts.

Komodo's container page describes its Server resource. The shared description
is explicitly identified as such; Docker has no mutable container-description
API. No configuration is rewritten to save this metadata.
"""

import asyncio
import hashlib
import json
import re
import ssl

import httpx

from plugins.komodo.actions import KomodoActionError, _json, _object_id, _result, _submitted_result
from plugins.komodo.alerts import _timestamp
from plugins.komodo.containers import _ACTIONS, _container_id, _identity, _names, _ports
from plugins.komodo.dashboard import _text
from plugins.komodo.server_summary import description_version
from plugins.komodo.stack_details import _credentials, _log_text, _now, _redact
from plugins.komodo.updates import _row as _update_row

_STATES = {"running", "created", "restarting", "stopping", "removing", "paused", "exited", "dead"}
_SECONDS = 10.0


def _fail(code, status=502):
    raise KomodoActionError(code, status)


def _ids(server_id, container_id):
    if not isinstance(server_id, str) or _object_id(server_id) is None or _container_id(container_id) is None:
        _fail("invalid_container_identity", 422)
    return server_id.lower(), container_id.lower()


def _version(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


async def _read(variant, params, *, origin, headers, verify, transport):
    try:
        async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(_SECONDS, connect=5.0)) as client:
            async with asyncio.timeout(_SECONDS):
                response = await client.post(origin + "/read", headers=headers, json={"type": variant, "params": params})
    except (httpx.HTTPError, TimeoutError):
        _fail("upstream_unavailable")
    if response.status_code in {401, 403, 404}:
        _fail({401: "invalid_credentials", 403: "permission_denied", 404: "container_unavailable"}[response.status_code],
              403 if response.status_code == 403 else 404 if response.status_code == 404 else 502)
    if response.status_code != 200 or (data := _json(response)) is None:
        _fail("upstream_unavailable" if response.status_code != 200 else "invalid_response")
    return data


async def _snapshot(server_id, container_id, *, origin, key, secret, verify, transport):
    server_id, container_id = _ids(server_id, container_id)
    origin, headers, secrets = _credentials(origin, key, secret)
    try:
        async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(_SECONDS, connect=5.0)) as client:
            await _identity(client, origin, headers)
    except (httpx.HTTPError, TimeoutError):
        _fail("upstream_unavailable")
    options = dict(origin=origin, headers=headers, verify=verify, transport=transport)
    server, listing, permission, core, containers = await asyncio.gather(*(
        _read(variant, params, **options) for variant, params in (
            ("GetServer", {"server": server_id}),
            ("ListServers", {"query": {"names": [server_id], "templates": "Include"}, "limit": 0}),
            ("GetPermission", {"target": {"type": "Server", "id": server_id}}),
            ("GetCoreInfo", {}), ("ListContainers", {"server": server_id}),
        )))
    if (not isinstance(server, dict) or _object_id(server.get("_id")) != server_id
            or server.get("template") is not False or not isinstance(server.get("config"), dict)
            or _text(server.get("name")) is None):
        _fail("container_server_unavailable", 409)
    if (not isinstance(permission, dict) or permission.get("level") not in {"None", "Read", "Execute", "Write"}
            or not isinstance(permission.get("specific"), list) or not isinstance(core, dict)
            or type(core.get("ui_write_disabled")) is not bool or not isinstance(listing, list)
            or not isinstance(containers, list)):
        _fail("invalid_response")
    level = permission["level"]
    if level == "None":
        _fail("permission_denied", 403)
    servers = [row for row in listing if isinstance(row, dict) and _object_id(row.get("id")) == server_id]
    info = servers[0].get("info") if len(servers) == 1 and servers[0].get("template") is False else None
    if not isinstance(info, dict):
        _fail("container_server_unavailable", 409)
    matches = [row for row in containers if isinstance(row, dict) and _container_id(row.get("id")) == container_id]
    if len(matches) != 1 or _object_id(matches[0].get("server_id")) != server_id or _text(matches[0].get("name")) is None:
        _fail("container_unavailable", 409)
    row = matches[0]
    state = row.get("state") if row.get("state") in _STATES else "unknown"
    enabled = server["config"].get("enabled") is True and info.get("state") == "Ok"
    specific = permission["specific"]
    description = server.get("description")
    valid_description = (isinstance(description, str) and len(description) <= 16384 and
                         not any(ord(c) < 32 and c not in "\r\n\t" or ord(c) == 127 for c in description))
    safe_description = _redact(description, secrets) if valid_description else None
    return {"server_id": server_id, "server_name": server["name"], "container_id": container_id,
            "container_name": row["name"], "state": state, "status": _text(row.get("status")),
            # State/identity change invalidates a pending execution, while
            # ordinary CPU/network sample changes do not.
            "version": _version([server_id, container_id, row["name"], state, enabled]),
            "description": safe_description, "description_scope": "server",
            "description_version": description_version(server_id, description) if valid_description else None,
            "can_edit_description": bool(level == "Write" and not core["ui_write_disabled"]
                                          and valid_description and safe_description == description),
            "can_execute": bool(enabled and level in {"Execute", "Write"}),
            "can_logs": bool(enabled and "Logs" in specific), "can_inspect": bool(enabled and "Inspect" in specific),
            "can_terminal": bool(enabled and "Terminal" in specific and info.get("container_terminals_disabled") is False and state == "running"),
            "can_attach": bool(enabled and "Terminal" in specific and info.get("container_terminals_disabled") is False and state == "running"),
            "_row": row, "_options": options, "_secrets": secrets}


async def _still_present(snapshot):
    rows = await _read("ListContainers", {"server": snapshot["server_id"]}, **snapshot["_options"])
    matches = [row for row in rows if isinstance(row, dict) and _container_id(row.get("id")) == snapshot["container_id"]] if isinstance(rows, list) else []
    if (len(matches) != 1 or _object_id(matches[0].get("server_id")) != snapshot["server_id"]
            or matches[0].get("name") != snapshot["container_name"]):
        _fail("container_unavailable", 409)


async def _inspect(snapshot, *, recheck=True):
    raw = await _read("InspectContainer", {"server": snapshot["server_id"], "container": snapshot["container_id"]}, **snapshot["_options"])
    if not isinstance(raw, dict) or _container_id(raw.get("Id")) != snapshot["container_id"]:
        _fail("container_identity_changed", 409)
    if recheck:
        await _still_present(snapshot)
    # Inspect is explicitly requested and permission gated. Preserve its native
    # JSON shape; redact integration credentials wherever they occur.
    if len(json.dumps(raw, ensure_ascii=True, allow_nan=False).encode()) > 4_194_304:
        _fail("container_inspect_too_large")
    return _redact(raw, snapshot["_secrets"])


def _integer(value):
    return value if type(value) is int and 0 <= value <= 9_007_199_254_740_991 else None


def _information(snapshot, inspected):
    row = snapshot["_row"]
    raw = inspected if isinstance(inspected, dict) else {}
    config = raw.get("Config") if isinstance(raw.get("Config"), dict) else {}
    state = raw.get("State") if isinstance(raw.get("State"), dict) else {}
    host = raw.get("HostConfig") if isinstance(raw.get("HostConfig"), dict) else {}
    restart = host.get("RestartPolicy") if isinstance(host.get("RestartPolicy"), dict) else {}
    strings = lambda value: [_text(item, limit=8192) for item in value] if isinstance(value, list) and len(value) <= 1000 else None
    labels = config.get("Labels")
    labels = {key: value for key, value in labels.items() if _text(key) is not None and _text(value, limit=8192) is not None} if isinstance(labels, dict) and len(labels) <= 1000 else None
    health = state.get("Health")
    return {"server_name": snapshot["server_name"], "image": _text(row.get("image"), limit=4096),
            "image_id": _text(row.get("image_id")), "network_mode": _text(row.get("network_mode")),
            "networks": _names(row.get("networks")), "ports": _ports(row.get("ports")), "volumes": _names(row.get("volumes")),
            "created": _text(raw.get("Created")) if raw else _integer(row.get("created")),
            "started_at": _text(state.get("StartedAt")), "finished_at": _text(state.get("FinishedAt")),
            "restart_count": _integer(raw.get("RestartCount")), "platform": _text(raw.get("Platform")), "driver": _text(raw.get("Driver")),
            "hostname": _text(config.get("Hostname")), "user": _text(config.get("User")), "working_dir": _text(config.get("WorkingDir")),
            "entrypoint": strings(config.get("Entrypoint")), "command": strings(config.get("Cmd")), "labels": labels,
            "health": _text(health.get("Status")) if isinstance(health, dict) else None,
            "exit_code": _integer(state.get("ExitCode")), "oom_killed": state.get("OOMKilled") if type(state.get("OOMKilled")) is bool else None,
            "pid": _integer(state.get("Pid")), "restart_policy": _text(restart.get("Name")),
            "stack_id": None, "stack_name": None}


async def _associated_stack(snapshot):
    # Komodo's native matching API is name based. Treat it only as a candidate
    # and verify the current full Docker ID in ListStackServices before showing
    # the association. This cannot relabel a same-name replacement container.
    matched = await _read("GetResourceMatchingContainer", {"server": snapshot["server_id"], "container": snapshot["container_name"]}, **snapshot["_options"])
    target = matched.get("resource") if isinstance(matched, dict) else None
    if target is None:
        return None, None
    if not isinstance(target, dict):
        _fail("invalid_response")
    if target.get("type") != "Stack":
        return None, None
    stack_id = _object_id(target.get("id"))
    if stack_id is None:
        _fail("invalid_response")
    stack, services = await asyncio.gather(
        _read("GetStack", {"stack": stack_id}, **snapshot["_options"]),
        _read("ListStackServices", {"stack": stack_id}, **snapshot["_options"]))
    config = stack.get("config") if isinstance(stack, dict) else None
    if (not isinstance(config, dict) or _object_id(stack.get("_id")) != stack_id or stack.get("template") is not False
            or _object_id(config.get("server_id")) != snapshot["server_id"] or config.get("swarm_id", "") != ""
            or _text(stack.get("name")) is None or not isinstance(services, list)):
        _fail("invalid_response")
    matching = [row for row in services if isinstance(row, dict) and _object_id(row.get("stack_id")) == stack_id
                and isinstance(row.get("container"), dict) and row.get("swarm_service") is None
                and _object_id(row["container"].get("server_id")) == snapshot["server_id"]
                and _container_id(row["container"].get("id")) == snapshot["container_id"]]
    return (stack_id, stack["name"]) if len(matching) == 1 else (None, None)


async def get_container_details(server_id, container_id, *, origin, key, secret, verify: ssl.SSLContext | bool = True, transport=None):
    snapshot = await _snapshot(server_id, container_id, origin=origin, key=key, secret=secret, verify=verify, transport=transport)
    errors, inspected, updates = [], None, []
    tasks = {"updates": _read("ListUpdates", {"page": 0, "query": {"target.type": "Server", "target.id": snapshot["server_id"]}}, **snapshot["_options"]),
             "stack_association": _associated_stack(snapshot)}
    if snapshot["can_inspect"]:
        # The complete overview rechecks identity after every enrichment has
        # finished, so it needs no extra mid-request ListContainers call.
        tasks["inspect"] = _inspect(snapshot, recheck=False)
    sections = dict(zip(tasks, await asyncio.gather(*tasks.values(), return_exceptions=True)))
    for name, result in sections.items():
        if isinstance(result, Exception):
            if not isinstance(result, KomodoActionError) or result.code in {"container_identity_changed", "container_unavailable", "invalid_credentials"}:
                raise result
            errors.append(name + "_unavailable")
            sections[name] = None
    inspected = sections.get("inspect")
    try:
        raw = sections["updates"]
        if not isinstance(raw, dict) or not isinstance(raw.get("updates"), list):
            _fail("invalid_response")
        for item in raw["updates"][:100]:
            if not isinstance(item, dict):
                _fail("invalid_response")
            update = _update_row(item)
            if update["resource_type"] != "Server" or update["resource_id"] != snapshot["server_id"]:
                _fail("invalid_response")
            update["resource_name"] = snapshot["server_name"]
            updates.append(update)
        updates.sort(key=lambda item: item["start_ts"] if item["start_ts"] is not None else -1, reverse=True)
    except KomodoActionError as error:
        if error.code == "invalid_credentials":
            raise
        updates = []
        errors.append("updates_unavailable")
    information = _information(snapshot, inspected)
    if sections["stack_association"] is not None:
        information["stack_id"], information["stack_name"] = sections["stack_association"]
    await _still_present(snapshot)
    stats = snapshot["_row"].get("stats")
    stats = stats if isinstance(stats, dict) else {}
    dto = {key: value for key, value in snapshot.items() if not key.startswith("_")}
    dto.update(information=information,
               stats={target: _text(stats.get(source), limit=128) for source, target in (
                   ("cpu_perc", "cpu_percent"), ("mem_perc", "memory_percent"), ("mem_usage", "memory_usage"),
                   ("net_io", "network_io"), ("block_io", "block_io"), ("pids", "pids"))},
               updates=updates[:20], updates_scope="server", errors=list(dict.fromkeys(errors)), updated_at=_now())
    return _redact(dto, snapshot["_secrets"])


async def get_container_inspect(server_id, container_id, *, origin, key, secret, verify=True, transport=None):
    snapshot = await _snapshot(server_id, container_id, origin=origin, key=key, secret=secret, verify=verify, transport=transport)
    if not snapshot["can_inspect"]:
        _fail("container_inspect_forbidden", 403)
    inspected = await _inspect(snapshot)
    return {"server_id": snapshot["server_id"], "container_id": snapshot["container_id"], "inspect": inspected, "updated_at": _now()}


async def get_container_logs(server_id, container_id, *, tail=100, timestamps=False, search="", invert=False, stream="both",
                             origin, key, secret, verify=True, transport=None):
    if (type(tail) is not int or not 1 <= tail <= 5000 or type(timestamps) is not bool or type(invert) is not bool
            or not isinstance(search, str) or len(search) > 1000 or any(ord(c) < 32 or ord(c) == 127 for c in search)
            or stream not in {"both", "stdout", "stderr"}):
        _fail("invalid_container_log_query", 422)
    snapshot = await _snapshot(server_id, container_id, origin=origin, key=key, secret=secret, verify=verify, transport=transport)
    if not snapshot["can_logs"]:
        _fail("container_logs_forbidden", 403)
    params = {"server": snapshot["server_id"], "container": snapshot["container_id"], "timestamps": timestamps}
    variant = "SearchContainerLog" if search else "GetContainerLog"
    params.update(dict(terms=[search], combinator="And", invert=invert) if search else dict(tail=tail))
    raw = await _read(variant, params, **snapshot["_options"])
    if not isinstance(raw, dict) or type(raw.get("success")) is not bool:
        _fail("invalid_response")
    stdout, clipped_out = _log_text(raw.get("stdout"), snapshot["_secrets"])
    stderr, clipped_err = _log_text(raw.get("stderr"), snapshot["_secrets"])
    await _still_present(snapshot)
    # SearchContainerLog's native `2>&1 | grep` merges both streams into
    # stdout. A previously selected stderr tab must not hide all matches.
    effective_stream = "stdout" if search else stream
    return {"server_id": snapshot["server_id"], "container_id": snapshot["container_id"],
            "stdout": stdout if effective_stream != "stderr" else "", "stderr": stderr if effective_stream != "stdout" else "",
            "success": raw["success"], "start_ts": _timestamp(raw.get("start_ts")), "end_ts": _timestamp(raw.get("end_ts")),
            "tail": 5000 if search else tail, "timestamps": timestamps, "search_scope": "last_5000" if search else "tail",
            "stream": effective_stream, "search_streams_merged": bool(search), "truncated": clipped_out or clipped_err, "updated_at": _now()}


async def update_container_description(server_id, container_id, *, description, base_version, before_write=None,
                                       origin, key, secret, verify=True, transport=None):
    if (not isinstance(description, str) or len(description) > 16384
            or any(ord(c) < 32 and c not in "\r\n\t" or ord(c) == 127 for c in description)
            or not isinstance(base_version, str) or re.fullmatch(r"[0-9a-f]{64}", base_version) is None):
        _fail("invalid_container_description", 422)
    snapshot = await _snapshot(server_id, container_id, origin=origin, key=key, secret=secret, verify=verify, transport=transport)
    if not snapshot["can_edit_description"]:
        _fail("container_description_read_only", 403)
    if snapshot["description_version"] != base_version:
        _fail("container_description_conflict", 409)
    if _redact(description, snapshot["_secrets"]) != description:
        _fail("invalid_container_description", 422)
    if before_write:
        await before_write()
    await _still_present(snapshot)
    result = _result(f"{snapshot['server_id']}:{snapshot['container_id']}", "outcome_unknown", error_code="outcome_unknown")
    try:
        async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(_SECONDS, connect=5.0)) as client:
            async with asyncio.timeout(_SECONDS):
                response = await client.post(snapshot["_options"]["origin"] + "/write", headers=snapshot["_options"]["headers"],
                    json={"type": "UpdateResourceMeta", "params": {"target": {"type": "Server", "id": snapshot["server_id"]}, "description": description}})
    except (httpx.HTTPError, TimeoutError):
        pass
    else:
        if response.status_code == 200 and _json(response) == {}:
            result = _result(result["id"], "completed")
        elif 400 <= response.status_code < 500 and response.status_code != 408:
            result = _result(result["id"], "failed", error_code="container_description_rejected")
    return {field: result[field] for field in ("ok", "status", "operation_id", "error_code")}


async def perform_container_detail_action(server_id, container_id, *, action, base_version, before_write=None,
                                          origin, key, secret, verify=True, transport=None):
    if action not in _ACTIONS or not isinstance(base_version, str) or re.fullmatch(r"[0-9a-f]{64}", base_version) is None:
        _fail("invalid_container_action", 422)
    snapshot = await _snapshot(server_id, container_id, origin=origin, key=key, secret=secret, verify=verify, transport=transport)
    if not snapshot["can_execute"]:
        _fail("permission_denied", 403)
    if snapshot["version"] != base_version:
        _fail("container_version_conflict", 409)
    allowed = {"restart": {"running"}, "pause": {"running"}, "unpause": {"paused"}, "start": {"exited", "created"},
               "stop": {"running", "paused"}, "destroy": _STATES}
    if snapshot["state"] not in allowed[action]:
        _fail("container_state_conflict", 409)
    if before_write:
        await before_write()
    await _still_present(snapshot)
    identifier = f"{snapshot['server_id']}:{snapshot['container_id']}"
    try:
        async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(_SECONDS, connect=5.0)) as client:
            async with asyncio.timeout(_SECONDS):
                response = await client.post(snapshot["_options"]["origin"] + "/execute", headers=snapshot["_options"]["headers"],
                    json={"type": _ACTIONS[action], "params": {"server": snapshot["server_id"], "container": snapshot["container_id"]}})
        result = _submitted_result(response, "servers", action, snapshot["server_id"], _ACTIONS[action])
        result["id"] = identifier
    except (httpx.HTTPError, TimeoutError):
        result = _result(identifier, "outcome_unknown", error_code="outcome_unknown")
    return {"section": "containers", "action": action, "results": [result]}
