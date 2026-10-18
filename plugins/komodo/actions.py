"""Selected-resource actions with fixed Komodo RPC mappings.

Authentication, PVE authorization, CSRF and request deduplication belong to the
calling API. This module independently validates the service account and every
selected resource before the first write. It never retries a submission.
"""

import asyncio
import re
import ssl
from time import monotonic

import httpx


_OBJECT_ID = re.compile(r"[0-9a-fA-F]{24}")
_SECTIONS = {
    "servers": ("ListServers", "Server", "server"),
    "stacks": ("ListStacks", "Stack", "stack"),
    "procedures": ("ListProcedures", "Procedure", "procedure"),
}
_ACTIONS = {
    "servers": {
        "prune_containers": ("/execute", "PruneContainers"),
        "prune_networks": ("/execute", "PruneNetworks"),
        "prune_volumes": ("/execute", "PruneVolumes"),
        "prune_images": ("/execute", "PruneImages"),
        "prune_system": ("/execute", "PruneSystem"),
        "restart_all_containers": ("/execute", "RestartAllContainers"),
        "pause_all_containers": ("/execute", "PauseAllContainers"),
        "stop_all_containers": ("/execute", "StopAllContainers"),
        "prune_buildx": ("/execute", "PruneBuildx"),
        "delete": ("/write", "DeleteServer"),
    },
    "stacks": {
        "check_update": ("/write", "CheckStackForUpdate"),
        "pull": ("/execute", "PullStack"),
        "deploy": ("/execute", "DeployStack"),
        "restart": ("/execute", "RestartStack"),
        "pause": ("/execute", "PauseStack"),
        "stop": ("/execute", "StopStack"),
        "destroy": ("/execute", "DestroyStack"),
        "delete": ("/write", "DeleteStack"),
    },
    "procedures": {"run": ("/execute", "RunProcedure"), "delete": ("/write", "DeleteProcedure")},
}
_DISPATCH_SECONDS = 45.0
_REQUEST_SECONDS = 10.0


class KomodoActionError(Exception):
    """A fixed public error raised only before any action is submitted."""

    def __init__(self, code: str, status_code: int = 502) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _object_id(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("$oid")
    return value.lower() if isinstance(value, str) and _OBJECT_ID.fullmatch(value) else None


def _validate(section, action, ids) -> list[str]:
    if (
        not isinstance(section, str) or section not in _ACTIONS
        or not isinstance(action, str) or action not in _ACTIONS[section]
        or not isinstance(ids, list) or not 1 <= len(ids) <= 50
    ):
        raise KomodoActionError("invalid_request", 400)
    normalized = []
    for identifier in ids:
        # Input IDs must be strings, never ObjectId objects or name patterns.
        if not isinstance(identifier, str) or _object_id(identifier) is None:
            raise KomodoActionError("invalid_request", 400)
        identifier = identifier.lower()
        if identifier not in normalized:
            normalized.append(identifier)
    return normalized


def _params(section: str, action: str, identifier: str) -> dict:
    if action == "delete":
        return {"id": identifier}
    params = {_SECTIONS[section][2]: identifier}
    if section == "stacks":
        if action == "check_update":
            params.update(skip_auto_update=True, wait_for_auto_update=False, skip_cache_refresh=False)
        else:
            params["services"] = []
            if action in {"deploy", "stop", "destroy"}:
                params["stop_time"] = None
            if action == "destroy":
                # The upstream DestroyStack DTO exposes no volume/force flag.
                # Default compose down retains volumes and other orphan apps.
                params["remove_orphans"] = False
    return params


def _result(identifier: str, status: str, *, operation_id: str | None = None, error_code: str | None = None) -> dict:
    return {
        "id": identifier, "ok": status in {"submitted", "completed"},
        "status": status, "operation_id": operation_id, "error_code": error_code,
    }


def _json(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return None


def _preflight_status(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise KomodoActionError("invalid_credentials")
    if response.status_code == 403:
        raise KomodoActionError("permission_denied", 403)
    if response.status_code != 200:
        raise KomodoActionError("upstream_unavailable")


def _preflight_resources(response: httpx.Response, section: str, ids: list[str]) -> None:
    _preflight_status(response)
    rows = _json(response)
    if not isinstance(rows, list):
        raise KomodoActionError("invalid_response")
    selected = set(ids)
    visible = {}
    for row in rows:
        if not isinstance(row, dict):
            raise KomodoActionError("invalid_response")
        identifier = _object_id(row.get("id"))
        if identifier not in selected:
            continue
        if identifier in visible:
            raise KomodoActionError("invalid_response")
        if "type" in row and row["type"] != _SECTIONS[section][1]:
            raise KomodoActionError("invalid_response")
        if not isinstance(row.get("template"), bool):
            raise KomodoActionError("invalid_response")
        visible[identifier] = row
    if any(identifier not in visible for identifier in ids):
        raise KomodoActionError("resource_not_found", 404)
    if any(visible[identifier]["template"] for identifier in ids):
        raise KomodoActionError("template_forbidden", 409)


def _submitted_result(response: httpx.Response, section: str, action: str, identifier: str, variant: str) -> dict:
    # A gateway/server error, redirect or malformed success does not establish
    # that the operation was rejected. Conservatively prohibit automatic retry.
    if not 200 <= response.status_code < 300:
        codes = {401: "invalid_credentials", 403: "permission_denied", 404: "resource_not_found", 409: "resource_busy", 429: "rate_limited"}
        # A proxy's Request Timeout can arrive after the write was forwarded.
        # Like transport timeouts, it cannot establish that nothing happened.
        if 400 <= response.status_code < 500 and response.status_code != 408:
            return _result(identifier, "failed", error_code=codes.get(response.status_code, "request_rejected"))
        return _result(identifier, "outcome_unknown", error_code="outcome_unknown")
    data = _json(response)
    if not isinstance(data, dict):
        return _result(identifier, "outcome_unknown", error_code="outcome_unknown")

    if action == "delete":
        # Delete returns a complete resource, including config and environment.
        # Validate only its identity and discard the rest immediately.
        if _object_id(data.get("_id")) == identifier:
            return _result(identifier, "completed")
    elif action == "check_update":
        # This /write response is synchronous, not an Update, and contains
        # service image details which do not belong in the browser result.
        if _object_id(data.get("stack")) == identifier and isinstance(data.get("services"), list):
            return _result(identifier, "completed")
    else:
        operation_id = _object_id(data.get("_id"))
        target = data.get("target")
        if (
            operation_id is not None and data.get("operation") == variant
            and isinstance(target, dict) and target.get("type") == _SECTIONS[section][1]
            and _object_id(target.get("id")) == identifier
            and isinstance(data.get("success"), bool)
        ):
            if isinstance(data.get("status"), str) and data["status"] in {"Queued", "InProgress"}:
                return _result(identifier, "submitted", operation_id=operation_id)
            if data.get("status") == "Complete":
                if data["success"]:
                    return _result(identifier, "completed", operation_id=operation_id)
                return _result(identifier, "failed", operation_id=operation_id, error_code="operation_failed")
    return _result(identifier, "outcome_unknown", error_code="outcome_unknown")


async def perform_action(
    section: str, action: str, ids: list[str], *,
    origin: str | None, key: str | None, secret: str | None,
    verify: ssl.SSLContext | bool, transport: httpx.AsyncBaseTransport | None,
    before_write=None,
) -> dict:
    selected = _validate(section, action, ids)
    if not origin or not key or not secret:
        raise KomodoActionError("not_configured", 503)
    started = monotonic()
    endpoint, variant = _ACTIONS[section][action]
    headers = {"x-api-key": key, "x-api-secret": secret}
    async with httpx.AsyncClient(
        verify=verify, transport=transport, trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0),
    ) as client:
        try:
            async with asyncio.timeout(_REQUEST_SECONDS):
                user_response = await client.get(origin + "/user", headers=headers)
            if user_response.status_code in {401, 403}:
                raise KomodoActionError("invalid_credentials")
            _preflight_status(user_response)
            user = _json(user_response)
            if isinstance(user, dict) and user.get("enabled") is False:
                raise KomodoActionError("invalid_credentials")
            if (
                not isinstance(user, dict) or user.get("enabled") is not True
                or not isinstance(user.get("username"), str) or not user["username"].strip()
            ):
                raise KomodoActionError("invalid_response")
            client.cookies.clear()
            async with asyncio.timeout(_REQUEST_SECONDS):
                resources = await client.post(origin + "/read", headers=headers, json={
                    "type": _SECTIONS[section][0],
                    "params": {"query": {"templates": "Include"}, "limit": 0},
                })
            _preflight_resources(resources, section, selected)
        except (httpx.HTTPError, TimeoutError):
            raise KomodoActionError("upstream_unavailable") from None

        results = []
        stopped = False
        for identifier in selected:
            if stopped or monotonic() - started >= _DISPATCH_SECONDS:
                results.append(_result(identifier, "not_submitted", error_code="not_submitted"))
                continue
            client.cookies.clear()
            if before_write is not None:
                await before_write()
            try:
                async with asyncio.timeout(_REQUEST_SECONDS):
                    response = await client.post(origin + endpoint, headers=headers, json={
                        "type": variant, "params": _params(section, action, identifier),
                    })
                result = _submitted_result(response, section, action, identifier, variant)
            except (httpx.HTTPError, TimeoutError):
                result = _result(identifier, "outcome_unknown", error_code="outcome_unknown")
            results.append(result)
            if not result["ok"]:
                stopped = True
        return {"section": section, "action": action, "results": results}
