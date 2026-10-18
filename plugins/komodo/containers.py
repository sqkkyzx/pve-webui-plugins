"""Container list DTOs and actions bound to an exact server/container pair.

Only fixed Komodo RPCs are used. Docker's full container ID is forwarded to
execution APIs, so a replacement container reusing a name is never selected.
"""

import asyncio
from datetime import datetime, timezone
import re
import ssl
from time import monotonic

import httpx

from plugins.komodo.actions import (
    KomodoActionError, _json, _object_id, _preflight_resources, _preflight_status,
    _result, _submitted_result,
)
from plugins.komodo.dashboard import _tags_index, _text


_CONTAINER_ID = re.compile(r"[0-9a-fA-F]{64}")
_COMPOSITE_ID = re.compile(r"([0-9a-fA-F]{24}):([0-9a-fA-F]{64})")
_DOCKER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}")
_STATES = {"running", "created", "restarting", "stopping", "removing", "paused", "exited", "dead"}
_ACTIONS = {
    "start": "StartContainer", "restart": "RestartContainer", "pause": "PauseContainer",
    "unpause": "UnpauseContainer", "stop": "StopContainer", "destroy": "DestroyContainer",
}
_REQUEST_SECONDS = 10.0
_DISPATCH_SECONDS = 45.0
_STACK_SECONDS = 8.0
_STACK_LIMIT = 10_000


def _container_id(value) -> str | None:
    return value.lower() if isinstance(value, str) and _CONTAINER_ID.fullmatch(value) else None


def _composite(row: dict) -> str | None:
    server_id, container_id = _object_id(row.get("server_id")), _container_id(row.get("id"))
    return f"{server_id}:{container_id}" if server_id and container_id else None


def _error(errors: list[str], code: str) -> None:
    if code not in errors:
        errors.append(code)


def _names(value) -> list[str]:
    if not isinstance(value, list):
        return []
    # Source lists contain Docker network/volume names, never bind mount paths.
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and _DOCKER_NAME.fullmatch(item)))


def _port(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535 else None


def _ports(value) -> list[str]:
    result = []
    if not isinstance(value, list):
        return result
    for binding in value:
        if not isinstance(binding, dict):
            continue
        private = _port(binding.get("PrivatePort"))
        public = _port(binding.get("PublicPort"))
        protocol = binding.get("Type", "")
        if private is None or not isinstance(protocol, str) or protocol not in {"", "tcp", "udp", "sctp"}:
            continue
        # IP is deliberately discarded. IPv4/IPv6 duplicate bindings collapse
        # into one display value without exposing the server's bind addresses.
        display = f"{public}→{private}" if public is not None else str(private)
        if protocol:
            display += "/" + protocol
        if display not in result:
            result.append(display)
    return result


async def _identity(client: httpx.AsyncClient, origin: str, headers: dict) -> None:
    async with asyncio.timeout(_REQUEST_SECONDS):
        response = await client.get(origin + "/user", headers=headers)
    if response.status_code in {401, 403}:
        raise KomodoActionError("invalid_credentials")
    _preflight_status(response)
    user = _json(response)
    if isinstance(user, dict) and user.get("enabled") is False:
        raise KomodoActionError("invalid_credentials")
    if (
        not isinstance(user, dict) or user.get("enabled") is not True
        or not isinstance(user.get("username"), str) or not user["username"].strip()
    ):
        raise KomodoActionError("invalid_response")


async def _read(client: httpx.AsyncClient, origin: str, headers: dict, variant: str, params: dict) -> httpx.Response:
    client.cookies.clear()
    async with asyncio.timeout(_REQUEST_SECONDS):
        return await client.post(origin + "/read", headers=headers, json={"type": variant, "params": params})


def _server_index(rows: list, errors: list[str]) -> dict[str, dict]:
    index = {}
    for row in rows:
        if not isinstance(row, dict):
            _error(errors, "servers_invalid_response")
            continue
        identifier, name = _object_id(row.get("id")), _text(row.get("name"))
        if identifier is None or name is None or not isinstance(row.get("template"), bool):
            _error(errors, "servers_invalid_response")
            continue
        if row["template"]:
            continue
        if identifier in index:
            _error(errors, "servers_invalid_response")
            continue
        index[identifier] = {"id": identifier, "name": name, "tags": row.get("tags", [])}
    return index


def _stack_index(rows: list, errors: list[str]) -> dict[str, dict]:
    """Select only server-mode resource identities, never compose/config text."""
    stacks, duplicates = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            _error(errors, "stacks_invalid_response")
            continue
        identifier, name = _object_id(row.get("id")), _text(row.get("name"))
        if identifier is None or name is None or not isinstance(row.get("template"), bool):
            _error(errors, "stacks_invalid_response")
            continue
        if row["template"]:
            continue
        if identifier in stacks or identifier in duplicates:
            stacks.pop(identifier, None)
            duplicates.add(identifier)
            _error(errors, "stacks_invalid_response")
            continue
        info = row.get("info")
        if not isinstance(info, dict) or not isinstance(info.get("swarm_id", ""), str):
            _error(errors, "stacks_invalid_response")
            continue
        # Swarm takes precedence even when both target IDs are configured.
        # A Swarm service is not an exact server/container identity.
        if info.get("swarm_id"):
            continue
        server_id = _object_id(info.get("server_id"))
        if server_id is None:
            if info.get("server_id") is not None and info.get("server_id") != "":
                _error(errors, "stacks_invalid_response")
            continue
        stacks[identifier] = {"id": identifier, "name": name, "server_id": server_id}
    return stacks


async def _stack_sources(*, origin, headers, verify, transport):
    """An optional, bounded enrichment on an isolated cookie jar.

    Run beside the existing list reads so a slow service cache does not extend
    their worst-case deadline. An empty stack selection must never call the
    all-services RPC, where an empty filter would mean every visible stack.
    """
    errors, stacks, services = [], {}, []
    section = "stacks"
    try:
        async with asyncio.timeout(_STACK_SECONDS):
            async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False,
                    follow_redirects=False, timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0)) as client:
                response = await _read(client, origin, headers, "ListStacks", {
                    "query": {"templates": "Exclude"}, "limit": 0,
                })
                if response.status_code != 200:
                    _error(errors, section + ("_permission_denied" if response.status_code in {401, 403} else "_upstream_unavailable"))
                    return stacks, services, errors
                rows = _json(response)
                if not isinstance(rows, list) or len(rows) > _STACK_LIMIT:
                    _error(errors, "stacks_invalid_response")
                    return stacks, services, errors
                stacks = _stack_index(rows, errors)
                if not stacks:
                    return stacks, services, errors
                section = "stack_services"
                response = await _read(client, origin, headers, "ListAllStackServices", {
                    "stacks": sorted(stacks), "limit": 0,
                })
                if response.status_code != 200:
                    _error(errors, section + ("_permission_denied" if response.status_code in {401, 403} else "_upstream_unavailable"))
                    return stacks, services, errors
                rows = _json(response)
                if not isinstance(rows, list):
                    _error(errors, "stack_services_invalid_response")
                else:
                    services = rows
    except (httpx.HTTPError, TimeoutError):
        _error(errors, section + "_upstream_unavailable")
    return stacks, services, errors


def _attach_stacks(containers, servers, stacks, services, stack_errors, errors):
    """Use only exact IDs reported by Komodo's stack service cache.

    The cache may omit replicas or offline containers. Unassigned therefore
    means no confirmed association, not proof of a manually created container.
    Duplicate claims from one stack are harmless; different stacks are an
    explicit conflict, never first/last wins.
    """
    for code in stack_errors:
        _error(errors, code)
    visible = {key: stack for key, stack in stacks.items() if stack["server_id"] in servers}
    by_container = {row["id"]: set() for row in containers}
    for service in services:
        if not isinstance(service, dict):
            _error(stack_errors, "stack_services_invalid_response")
            continue
        stack_id = _object_id(service.get("stack_id"))
        stack = stacks.get(stack_id)
        if stack is None:
            _error(stack_errors, "stack_services_invalid_response")
            continue
        container = service.get("container")
        if container is None:
            continue
        identifier = _composite(container) if isinstance(container, dict) else None
        if (identifier is None or identifier.split(":")[0] != stack["server_id"]
                or service.get("swarm_service") is not None):
            _error(stack_errors, "stack_services_invalid_response")
            continue
        if stack_id in visible and identifier in by_container:
            by_container[identifier].add(stack_id)
        elif stack_id in visible:
            # The two caches are read independently. A replaced/deleted exact
            # ID is not evidence that the current same-name container belongs
            # to this stack, nor proof that it is independent.
            _error(stack_errors, "stack_services_stale_response")
    for code in stack_errors:
        _error(errors, code)
    for row in containers:
        claims = by_container[row["id"]]
        row["stack_id"] = next(iter(claims)) if len(claims) == 1 else None
        row["stack_association"] = "matched" if claims else "unknown" if stack_errors else "unassigned"
        if len(claims) > 1:
            row["stack_association"] = "conflict"
            _error(errors, "stack_membership_conflict")
    return list(visible.values())


async def list_containers(*, origin: str | None, key: str | None, secret: str | None,
                          verify: ssl.SSLContext | bool, transport: httpx.AsyncBaseTransport | None) -> dict:
    if not origin or not key or not secret:
        raise KomodoActionError("not_configured", 503)
    headers = {"x-api-key": key, "x-api-secret": secret}
    raw = {"servers": [], "containers": [], "tags": []}
    errors = []
    async with httpx.AsyncClient(
        verify=verify, transport=transport, trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0),
    ) as client:
        try:
            await _identity(client, origin, headers)
        except (httpx.HTTPError, TimeoutError):
            raise KomodoActionError("upstream_unavailable") from None
        stack_task = asyncio.create_task(_stack_sources(origin=origin, headers=headers,
            verify=verify, transport=transport))
        try:
            for section, variant, params in (
                ("servers", "ListServers", {"query": {"templates": "Include"}, "limit": 0}),
                ("containers", "ListAllContainers", {"limit": 0}),
                ("tags", "ListTags", {}),
            ):
                try:
                    response = await _read(client, origin, headers, variant, params)
                except (httpx.HTTPError, TimeoutError):
                    _error(errors, section + "_upstream_unavailable")
                    continue
                if response.status_code == 401:
                    raise KomodoActionError("invalid_credentials")
                if response.status_code != 200:
                    _error(errors, section + ("_permission_denied" if response.status_code == 403 else "_upstream_unavailable"))
                    continue
                rows = _json(response)
                if not isinstance(rows, list):
                    _error(errors, section + "_invalid_response")
                    continue
                raw[section] = rows
            stacks, services, stack_errors = await stack_task
        finally:
            if not stack_task.done():
                stack_task.cancel()
            await asyncio.gather(stack_task, return_exceptions=True)

    servers = _server_index(raw["servers"], errors)
    tags = _tags_index(raw["tags"], errors)
    containers = []
    seen = set()
    for item in raw["containers"]:
        if not isinstance(item, dict):
            _error(errors, "containers_invalid_response")
            continue
        identifier = _composite(item)
        name = _text(item.get("name"))
        if identifier is None or name is None or identifier in seen:
            _error(errors, "containers_invalid_response")
            continue
        server_id, container_id = identifier.split(":")
        server = servers.get(server_id)
        if server is None:
            # No rows from template, malformed, or inaccessible servers.
            continue
        seen.add(identifier)
        state = item.get("state")
        image = _text(item.get("image"), limit=1024)
        if image and ("://" in image or any(character.isspace() for character in image)):
            image = None
        networks = _names(item.get("networks"))
        if not networks:
            mode = item.get("network_mode")
            if isinstance(mode, str) and _DOCKER_NAME.fullmatch(mode):
                networks = [mode]
        server_tags = server["tags"] if isinstance(server["tags"], list) else []
        containers.append({
            "id": identifier, "container_id": container_id,
            "server_id": server_id, "server_name": server["name"], "name": name,
            "state": state if isinstance(state, str) and state in _STATES else "unknown",
            "image": image, "networks": networks, "ports": _ports(item.get("ports")),
            "volumes": _names(item.get("volumes")),
            "tags": list(dict.fromkeys(tags[tag] for tag in server_tags if isinstance(tag, str) and tag in tags)),
        })
    stack_rows = _attach_stacks(containers, servers, stacks, services, stack_errors, errors)
    return {
        "containers": containers,
        "servers": [{"id": server["id"], "name": server["name"]} for server in servers.values()],
        "stacks": stack_rows,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "errors": errors,
    }


def _selection(action, ids) -> list[str]:
    if not isinstance(action, str) or action not in _ACTIONS or not isinstance(ids, list) or not 1 <= len(ids) <= 50:
        raise KomodoActionError("invalid_request", 400)
    result = []
    for identifier in ids:
        if not isinstance(identifier, str) or _COMPOSITE_ID.fullmatch(identifier) is None:
            raise KomodoActionError("invalid_request", 400)
        identifier = identifier.lower()
        if identifier not in result:
            result.append(identifier)
    return result


async def perform_container_action(action: str, ids: list[str], *, origin: str | None, key: str | None,
                                   secret: str | None, verify: ssl.SSLContext | bool,
                                   transport: httpx.AsyncBaseTransport | None) -> dict:
    selected = _selection(action, ids)
    if not origin or not key or not secret:
        raise KomodoActionError("not_configured", 503)
    started = monotonic()
    headers = {"x-api-key": key, "x-api-secret": secret}
    async with httpx.AsyncClient(
        verify=verify, transport=transport, trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0),
    ) as client:
        try:
            await _identity(client, origin, headers)
            response = await _read(client, origin, headers, "ListServers", {"query": {"templates": "Include"}, "limit": 0})
            server_ids = list(dict.fromkeys(identifier.split(":")[0] for identifier in selected))
            _preflight_resources(response, "servers", server_ids)
            response = await _read(client, origin, headers, "ListAllContainers", {"limit": 0})
            _preflight_status(response)
            rows = _json(response)
            if not isinstance(rows, list):
                raise KomodoActionError("invalid_response")
            present = set()
            for row in rows:
                if not isinstance(row, dict):
                    raise KomodoActionError("invalid_response")
                identifier = _composite(row)
                if identifier in selected:
                    if identifier in present:
                        raise KomodoActionError("invalid_response")
                    present.add(identifier)
            if any(identifier not in present for identifier in selected):
                raise KomodoActionError("resource_not_found", 404)
        except (httpx.HTTPError, TimeoutError):
            raise KomodoActionError("upstream_unavailable") from None

        results = []
        stopped = False
        variant = _ACTIONS[action]
        for identifier in selected:
            if stopped or monotonic() - started >= _DISPATCH_SECONDS:
                results.append(_result(identifier, "not_submitted", error_code="not_submitted"))
                continue
            server_id, container_id = identifier.split(":")
            # Stop/Destroy signal and time are intentionally omitted, preserving
            # upstream defaults. No force or volume-removal flag is sent.
            params = {"server": server_id, "container": container_id}
            client.cookies.clear()
            try:
                async with asyncio.timeout(_REQUEST_SECONDS):
                    response = await client.post(origin + "/execute", headers=headers, json={"type": variant, "params": params})
                # Core creates a Server-targeted Update for each container API.
                result = _submitted_result(response, "servers", action, server_id, variant)
                result["id"] = identifier
            except (httpx.HTTPError, TimeoutError):
                result = _result(identifier, "outcome_unknown", error_code="outcome_unknown")
            results.append(result)
            if not result["ok"]:
                stopped = True
        return {"section": "containers", "action": action, "results": results}
