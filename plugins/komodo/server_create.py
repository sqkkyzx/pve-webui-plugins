"""Minimal native CreateServer, with explicit permission and no retry.

The resource returned by Core contains private configuration and keys. Only its
new immutable identity and the validated name leave this module.
"""

import re

import httpx

from plugins.komodo.actions import KomodoActionError, _json, _object_id
from plugins.komodo.server_inventory import _read, _session


def validate_server_name(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 512 or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or re.fullmatch(r"[0-9a-fA-F]{24}", value)):
        raise KomodoActionError("invalid_server_name", 422)
    return value


async def _capabilities(session):
    core = await _read(session, "GetCoreInfo", {})
    if (not isinstance(core, dict) or type(core.get("ui_write_disabled")) is not bool
            or type(core.get("disable_non_admin_create")) is not bool):
        raise KomodoActionError("server_create_policy_unavailable")
    user = session[4]
    return bool(not core["ui_write_disabled"] and (user.get("admin") is True or
                (core["disable_non_admin_create"] is False and user.get("create_server_permissions") is True)))


async def get_server_create_capabilities(*, origin, key, secret, verify=True, transport=None):
    async with _session(origin=origin, key=key, secret=secret, verify=verify, transport=transport) as session:
        return {"can_create": await _capabilities(session)}


def create_result(name, *, status, server_id=None, error_code=None):
    return {"ok": status == "completed", "created": True if status in {"completed", "created_unlinked"} else None if status == "outcome_unknown" else False,
            "status": status, "server_id": server_id, "server_name": name, "operation_id": None,
            "error_code": error_code, "link": None, "link_error": None}


async def create_server(name, *, before_write=None, origin, key, secret, verify=True, transport=None):
    name = validate_server_name(name)
    async with _session(origin=origin, key=key, secret=secret, verify=verify, transport=transport) as session:
        if any(value in name for value in session[3]):
            raise KomodoActionError("invalid_server_name", 422)
        if not await _capabilities(session):
            raise KomodoActionError("server_create_forbidden", 403)
        if before_write:
            await before_write()
        client, rpc_origin, headers, _, _ = session
        client.cookies.clear()
        try:
            response = await client.post(rpc_origin + "/write", headers=headers,
                                        json={"type": "CreateServer", "params": {"name": name}})
        except (httpx.HTTPError, TimeoutError):
            return create_result(name, status="outcome_unknown", error_code="outcome_unknown")
        if 400 <= response.status_code < 500 and response.status_code != 408:
            return create_result(name, status="failed", error_code={401: "invalid_credentials", 403: "server_create_forbidden",
                409: "server_name_conflict", 429: "rate_limited"}.get(response.status_code, "server_create_rejected"))
        data = _json(response)
        identifier = _object_id(data.get("_id")) if isinstance(data, dict) else None
        if (200 <= response.status_code < 300 and identifier and data.get("name") == name and data.get("template") is False):
            return create_result(name, status="completed", server_id=identifier)
        return create_result(name, status="outcome_unknown", error_code="outcome_unknown")
