"""Guest-bound Docker inventory and hosted resources using fixed Komodo RPCs.

Only selected display fields leave this module. Every mutation revalidates the
server, permission, selected identity and version before dispatch, never retries
an ambiguous submission, and delegates durable deduplication to the route.
"""

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime

import httpx

from plugins.komodo.actions import KomodoActionError, _ACTIONS as RESOURCE_ACTIONS, _json, _object_id, _params, _result, _submitted_result
from plugins.komodo.containers import _ACTIONS as CONTAINER_ACTIONS, _container_id, _names
from plugins.komodo.dashboard import _text
from plugins.komodo.guest_links import GuestLinkError
from plugins.komodo.stack_details import _credentials, _now, _redact


_HASH = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
_STACK_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,255}")
_STATES = {"running", "created", "restarting", "stopping", "removing", "paused", "exited", "dead"}
_CONTAINER_ALLOWED = {"start": {"created", "exited"}, "restart": {"running"}, "pause": {"running"},
                      "unpause": {"paused"}, "stop": {"running", "paused"}, "destroy": _STATES}
_DOCKER_READS = {"containers": "ListContainers", "networks": "ListNetworks", "volumes": "ListVolumes", "images": "ListImages"}
_RESOURCE_READS = {"stacks": "ListStacks", "deployments": "ListDeployments", "repos": "ListRepos"}
_DELETE = {"networks": "DeleteNetwork", "volumes": "DeleteVolume", "images": "DeleteImage"}
_STACK_ACTIONS = ("check_update", "pull", "deploy", "restart", "stop", "destroy", "delete")
_MAX_ROWS = 10_000


def _fail(code, status=502):
    raise KomodoActionError(code, status)


def _version(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _valid_id(server_id):
    if not isinstance(server_id, str) or _object_id(server_id) is None:
        _fail("invalid_inventory_server", 422)
    return server_id.lower()


def _integer(value):
    return value if type(value) is int and 0 <= value <= 9_007_199_254_740_991 else None


def _creation(value):
    value = _text(value)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value if parsed.tzinfo is not None and parsed.year >= 1970 else None
    except ValueError:
        return None


def _stack_config_digest(resource, identifier, server_id):
    config = resource.get("config") if isinstance(resource, dict) else None
    if (not isinstance(config, dict) or _object_id(resource.get("_id")) != identifier
            or resource.get("template") is not False or _text(resource.get("name")) is None
            or _object_id(config.get("server_id")) != server_id or config.get("swarm_id", "") != ""):
        _fail("inventory_resource_changed", 409)
    # Config can contain secrets. Only the digest participates in the opaque
    # version, never the config or a separately exposed low-level digest.
    try:
        encoded = json.dumps([resource["name"], config], sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode()
    except (TypeError, ValueError):
        _fail("inventory_invalid_response")
    if len(encoded) > 2_097_152:
        _fail("inventory_invalid_response")
    return hashlib.sha256(encoded).hexdigest()


@asynccontextmanager
async def _session(*, origin, key, secret, verify=True, transport=None):
    origin, headers, secrets = _credentials(origin, key, secret)
    async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                 timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        try:
            response = await client.get(origin + "/user", headers=headers)
            if response.status_code in {401, 403}:
                _fail("invalid_credentials")
            user = _json(response)
            if (response.status_code != 200 or not isinstance(user, dict) or user.get("enabled") is not True
                    or _text(user.get("username")) is None):
                _fail("invalid_response")
            # Do not accept or replay an upstream cookie alongside API credentials.
            client.cookies.clear()
            yield client, origin, headers, secrets, user
        except (httpx.HTTPError, TimeoutError):
            _fail("inventory_upstream_unavailable")


async def _read(session, variant, params):
    client, origin, headers, _, _ = session
    client.cookies.clear()
    try:
        response = await client.post(origin + "/read", headers=headers, json={"type": variant, "params": params})
    except (httpx.HTTPError, TimeoutError):
        _fail("inventory_upstream_unavailable")
    if response.status_code in {401, 403}:
        _fail("invalid_credentials" if response.status_code == 401 else "permission_denied", 502 if response.status_code == 401 else 403)
    if response.status_code != 200:
        _fail("inventory_upstream_unavailable")
    data = _json(response)
    if data is None:
        _fail("inventory_invalid_response")
    return data


async def _base(session, server_id):
    server, listing, permission, core = await asyncio.gather(*(
        _read(session, variant, params) for variant, params in (
            ("GetServer", {"server": server_id}),
            ("ListServers", {"query": {"names": [server_id], "templates": "Include"}, "limit": 0}),
            ("GetPermission", {"target": {"type": "Server", "id": server_id}}), ("GetCoreInfo", {}))))
    if (not isinstance(server, dict) or _object_id(server.get("_id")) != server_id
            or server.get("template") is not False or not isinstance(server.get("config"), dict)
            or _text(server.get("name")) is None or not isinstance(listing, list)):
        _fail("inventory_server_unavailable", 409)
    matches = [row for row in listing if isinstance(row, dict) and _object_id(row.get("id")) == server_id]
    if len(matches) != 1 or matches[0].get("template") is not False or not isinstance(matches[0].get("info"), dict):
        _fail("inventory_server_unavailable", 409)
    if (not isinstance(permission, dict) or permission.get("level") not in {"None", "Read", "Execute", "Write"}
            or not isinstance(permission.get("specific"), list) or not isinstance(core, dict)
            or type(core.get("ui_write_disabled")) is not bool):
        _fail("inventory_invalid_response")
    if permission["level"] == "None":
        _fail("permission_denied", 403)
    state = matches[0]["info"].get("state")
    state = state if isinstance(state, str) and state in {"Ok", "NotOk", "Disabled"} else "unknown"
    online = server["config"].get("enabled") is True and state == "Ok"
    user = session[4]
    return {"server_id": server_id, "server_name": server["name"], "server_state": state,
            "can_execute": online and permission["level"] in {"Execute", "Write"},
            "can_write": permission["level"] == "Write" and not core["ui_write_disabled"],
            "can_create_stack": bool(not core["ui_write_disabled"] and "Attach" in permission["specific"]
                                     and (user.get("admin") is True or core.get("disable_non_admin_create") is False)),
            "_online": online, "_write_disabled": core["ui_write_disabled"]}


def _rows(value):
    if not isinstance(value, list) or len(value) > _MAX_ROWS or any(not isinstance(row, dict) for row in value):
        _fail("inventory_invalid_response")
    return value


def _docker_row(section, raw, base):
    name = _text(raw.get("name"), limit=4096)
    if name is None:
        _fail("inventory_invalid_response")
    if section == "containers":
        identifier = _container_id(raw.get("id"))
        if identifier is None or _object_id(raw.get("server_id")) != base["server_id"]:
            _fail("inventory_invalid_response")
        state = raw.get("state")
        state = state if isinstance(state, str) and state in _STATES else "unknown"
        row = {"id": identifier, "name": name, "state": state, "status": _text(raw.get("status")),
               "image": _text(raw.get("image"), limit=4096), "network_mode": _text(raw.get("network_mode")),
               "networks": _names(raw.get("networks")), "allowed_actions": [action for action, states in _CONTAINER_ALLOWED.items()
                   if base["can_execute"] and state in states]}
        # Keep the exact container-detail version contract for jump/action parity.
        row["version"] = _version([base["server_id"], identifier, name, state, base["_online"]])
        return row
    if section == "networks":
        identifier = _container_id(raw.get("id"))
        if identifier is None or _NAME.fullmatch(name) is None:
            _fail("inventory_invalid_response")
        row = {"id": identifier, "name": name, "driver": _text(raw.get("driver")), "scope": _text(raw.get("scope")),
               "attachable": raw.get("attachable") if type(raw.get("attachable")) is bool else None,
               "ipam_driver": _text(raw.get("ipam_driver")), "created": _text(raw.get("created")),
               "system": name in {"bridge", "none", "host"}, "builtin": name in {"bridge", "none", "host"}}
    elif section == "volumes":
        if _NAME.fullmatch(name) is None:
            _fail("inventory_invalid_response")
        row = {"id": name, "name": name, "driver": _text(raw.get("driver")), "scope": _text(raw.get("scope")),
               "created": _creation(raw.get("created"))}
    else:
        identifier = raw.get("id")
        if not isinstance(identifier, str) or _IMAGE.fullmatch(identifier) is None:
            _fail("inventory_invalid_response")
        row = {"id": identifier, "name": name, "created": _integer(raw.get("created")), "size": _integer(raw.get("size"))}
    in_use = raw.get("in_use") if type(raw.get("in_use")) is bool else None
    row["in_use"] = in_use
    identified = section != "volumes" or row["created"] is not None
    row["allowed_actions"] = ["delete"] if base["can_execute"] and in_use is False and not row.get("system") and identified else []
    row["version"] = _version([base["server_id"], section, row])
    return row


async def _docker(session, base, *, selected_section=None):
    sections = [selected_section] if selected_section else list(_DOCKER_READS)
    result = {**{key: value for key, value in base.items() if not key.startswith("_")}, "errors": [], "updated_at": _now()}
    if not base["_online"]:
        result.update({section: [] for section in sections}, errors=["server_disconnected"])
        return result
    responses = await asyncio.gather(*(_read(session, _DOCKER_READS[section], {"server": base["server_id"]}) for section in sections), return_exceptions=True)
    for section, raw in zip(sections, responses):
        try:
            if isinstance(raw, Exception):
                raise raw
            rows = [_docker_row(section, item, base) for item in _rows(raw)]
            if len({row["id"] for row in rows}) != len(rows):
                _fail("inventory_invalid_response")
            result[section] = rows
        except KomodoActionError as error:
            if error.code == "invalid_credentials" or selected_section:
                raise
            result[section] = []
            result["errors"].append(section + ("_permission_denied" if error.status_code == 403 else "_unavailable"))
    return _redact(result, session[3])


async def _resource_row(session, section, raw, base):
    identifier, name = _object_id(raw.get("id")), _text(raw.get("name"))
    info = raw.get("info")
    if identifier is None or name is None or type(raw.get("template")) is not bool or not isinstance(info, dict):
        _fail("inventory_invalid_response")
    swarm_id = info.get("swarm_id", "")
    if raw["template"] or _object_id(info.get("server_id")) != base["server_id"] or not isinstance(swarm_id, str) or swarm_id:
        return None
    row = {"id": identifier, "name": name, "state": _text(info.get("state")), "server_name": base["server_name"],
           "tags": [_text(item) for item in raw.get("tags", []) if _text(item) is not None] if isinstance(raw.get("tags"), list) else [],
           "allowed_actions": []}
    config_digest = None
    if section == "stacks":
        permission, resource = await asyncio.gather(
            _read(session, "GetPermission", {"target": {"type": "Stack", "id": identifier}}),
            _read(session, "GetStack", {"stack": identifier}))
        if not isinstance(permission, dict) or permission.get("level") not in {"None", "Read", "Execute", "Write"}:
            _fail("inventory_invalid_response")
        level = permission["level"]
        if level == "None":
            return None
        config_digest = _stack_config_digest(resource, identifier, base["server_id"])
        row["name"] = resource["name"]
        row["source"] = "host" if info.get("files_on_host") is True else "git" if info.get("repo") or info.get("linked_repo") else "ui" if info.get("file_contents") else "none"
        row["allowed_actions"] = [action for action in _STACK_ACTIONS if
            (action == "delete" and level == "Write" and not base["_write_disabled"])
            or (action != "delete" and base["_online"] and level in {"Execute", "Write"})]
    elif section == "deployments":
        row["image"] = _text(info.get("image"), limit=4096)
    else:
        row.update(repo=_text(info.get("repo"), limit=4096), branch=_text(info.get("branch")))
    row["version"] = _version([base["server_id"], section, row, config_digest])
    return row


async def _resources(session, base, *, selected_section=None, selected_ids=None):
    sections = [selected_section] if selected_section else list(_RESOURCE_READS)
    result = {**{key: value for key, value in base.items() if not key.startswith("_")}, "errors": [], "updated_at": _now()}
    responses = await asyncio.gather(*(_read(session, _RESOURCE_READS[section], {
        "query": {"templates": "Exclude", "specific": {"server_ids": [base["server_id"]]}}, "limit": 0}) for section in sections), return_exceptions=True)
    for section, raw in zip(sections, responses):
        try:
            if isinstance(raw, Exception):
                raise raw
            rows = []
            # Bound concurrency avoids N concurrent permission lookups on large servers.
            semaphore = asyncio.Semaphore(8)
            async def convert(item):
                async with semaphore:
                    return await _resource_row(session, section, item, base)
            source = _rows(raw)
            if selected_ids is not None:
                source = [item for item in source if _object_id(item.get("id")) in selected_ids]
            rows = [row for row in await asyncio.gather(*(convert(item) for item in source)) if row is not None]
            if len({row["id"] for row in rows}) != len(rows):
                _fail("inventory_invalid_response")
            result[section] = rows
        except KomodoActionError as error:
            if error.code == "invalid_credentials" or selected_section:
                raise
            result[section] = []
            result["errors"].append(section + ("_permission_denied" if error.status_code == 403 else "_unavailable"))
    return _redact(result, session[3])


async def get_server_inventory(server_id, *, kind, origin, key, secret, verify=True, transport=None):
    server_id = _valid_id(server_id)
    if not isinstance(kind, str) or kind not in {"docker", "resources"}:
        _fail("invalid_inventory_kind", 422)
    async with _session(origin=origin, key=key, secret=secret, verify=verify, transport=transport) as session:
        base = await _base(session, server_id)
        return await (_docker(session, base) if kind == "docker" else _resources(session, base))


async def perform_inventory_action(server_id, *, section, action, targets, name=None, before_write=None,
                                   origin, key, secret, verify=True, transport=None):
    server_id = _valid_id(server_id)
    create = section == "stacks" and action == "create_stack"
    if (section not in {*_DOCKER_READS, "stacks"} or
            action not in ({*CONTAINER_ACTIONS} if section == "containers" else {"delete"} if section in _DELETE else {*_STACK_ACTIONS, "create_stack"})):
        _fail("invalid_inventory_action", 422)
    if create:
        if targets or not isinstance(name, str) or _STACK_NAME.fullmatch(name) is None or _object_id(name) is not None:
            _fail("invalid_inventory_action", 422)
    elif (name is not None or not isinstance(targets, list) or not 1 <= len(targets) <= 50
          or any(not isinstance(target, dict) or set(target) != {"id", "version"} or not isinstance(target["id"], str)
                 or not isinstance(target["version"], str) or _HASH.fullmatch(target["version"]) is None for target in targets)
          or len({target["id"] for target in targets}) != len(targets)):
        _fail("invalid_inventory_action", 422)
    async with _session(origin=origin, key=key, secret=secret, verify=verify, transport=transport) as session:
        base = await _base(session, server_id)
        if create:
            if not base["can_create_stack"]:
                _fail("inventory_action_forbidden", 403)
            endpoint, variant, params = "/write", "CreateStack", {"name": name, "config": {"server_id": server_id}}
            selected = [{"id": name}]
        else:
            async def validate(current):
                data = await (_resources(session, current, selected_section=section, selected_ids={target["id"] for target in targets}) if section == "stacks"
                              else _docker(session, current, selected_section=section))
                index = {row["id"]: row for row in data[section]}
                for target in targets:
                    row = index.get(target["id"])
                    if row is None or row["version"] != target["version"]:
                        _fail("inventory_version_conflict", 409)
                    if action not in row["allowed_actions"]:
                        _fail("inventory_action_forbidden", 403)
                return index
            await validate(base)
            selected = targets
        results, stopped = [], False
        for target in selected:
            identifier = target["id"]
            if stopped:
                results.append(_result(identifier, "not_submitted", error_code="not_submitted"))
                continue
            # Recheck the binding and current upstream state immediately before
            # each write. All selections were checked before the first write.
            try:
                current = await _base(session, server_id)
                if create:
                    if not current["can_create_stack"]:
                        _fail("inventory_action_forbidden", 403)
                else:
                    data = await (_resources(session, current, selected_section=section, selected_ids={identifier}) if section == "stacks"
                                  else _docker(session, current, selected_section=section))
                    matches = [row for row in data[section] if row["id"] == identifier]
                    if len(matches) != 1 or matches[0]["version"] != target["version"]:
                        _fail("inventory_version_conflict", 409)
                    if action not in matches[0]["allowed_actions"]:
                        _fail("inventory_action_forbidden", 403)
                    if section == "stacks":
                        # _resources fetched GetStack and included its private
                        # config digest in the version. The initial whole-batch
                        # check does the same for every selection before writes.
                        endpoint, variant = RESOURCE_ACTIONS["stacks"][action]
                        params = _params("stacks", action, identifier)
                    elif section == "containers":
                        endpoint, variant = "/execute", CONTAINER_ACTIONS[action]
                        params = {"server": server_id, "container": identifier}
                    else:
                        endpoint, variant = "/execute", _DELETE[section]
                        # ID is accepted by Docker network/image removal. Volumes
                        # have no immutable ID; its name+creation version is checked.
                        params = {"server": server_id, "name": identifier}
                if before_write:
                    await before_write()
            except (KomodoActionError, GuestLinkError) as error:
                if not results:
                    raise
                results.append(_result(identifier, "not_submitted", error_code=error.code))
                stopped = True
                continue
            client, rpc_origin, headers, _, _ = session
            client.cookies.clear()
            try:
                response = await client.post(rpc_origin + endpoint, headers=headers, json={"type": variant, "params": params})
                if create:
                    raw = _json(response)
                    new_id = _object_id(raw.get("_id")) if isinstance(raw, dict) else None
                    config = raw.get("config") if isinstance(raw, dict) else None
                    if (response.status_code == 200 and new_id and raw.get("name") == name and isinstance(config, dict)
                            and _object_id(config.get("server_id")) == server_id and not config.get("swarm_id", "")):
                        result = _result(new_id, "completed")
                    elif 400 <= response.status_code < 500 and response.status_code != 408:
                        result = _result(identifier, "failed", error_code="request_rejected")
                    else:
                        result = _result(identifier, "outcome_unknown", error_code="outcome_unknown")
                else:
                    result = _submitted_result(response, "stacks" if section == "stacks" else "servers", "destroy" if section in _DELETE else action,
                                               identifier if section == "stacks" else server_id, variant)
                    result["id"] = identifier
            except (httpx.HTTPError, TimeoutError):
                result = _result(identifier, "outcome_unknown", error_code="outcome_unknown")
            results.append(result)
            stopped = not result["ok"]
        return {"section": section, "action": action, "results": results}
