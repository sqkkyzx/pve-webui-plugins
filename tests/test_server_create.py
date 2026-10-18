"""Native CreateServer permissions and sanitized non-retrying receipts."""

import asyncio
import json

import httpx
import pytest

from plugins.komodo.actions import KomodoActionError
from plugins.komodo.server_create import create_server, get_server_create_capabilities, validate_server_name


ORIGIN, KEY, SECRET = "http://core:9120", "private-create-key", "private-create-secret"
SERVER = "a" * 24


def upstream(*, user=None, policy=None, response=None, writes=None):
    user = {"enabled": True, "username": "integration", "admin": True, **(user or {})}
    policy = {"ui_write_disabled": False, "disable_non_admin_create": False, **(policy or {})}
    writes = writes if writes is not None else []
    def handle(request):
        assert request.headers["x-api-key"] == KEY and request.headers["x-api-secret"] == SECRET
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if request.url.path == "/user":
            return httpx.Response(200, json=user, headers={"set-cookie": "private=must-not-replay; Path=/"})
        body = json.loads(request.content)
        if request.url.path == "/read":
            assert body == {"type": "GetCoreInfo", "params": {}}
            return httpx.Response(200, json=policy)
        assert request.url.path == "/write" and body["type"] == "CreateServer"
        assert set(body["params"]) == {"name"}
        writes.append(body)
        if isinstance(response, Exception):
            raise response
        return response if response is not None else httpx.Response(200, json={"_id": {"$oid": SERVER},
            "name": body["params"]["name"], "template": False, "config": {"passkey": SECRET}, "info": {"private_key": SECRET}})
    return handle


def run(handler, *, create=False, name="New Server 中文", guard=None):
    options = {"origin": ORIGIN, "key": KEY, "secret": SECRET, "verify": True, "transport": httpx.MockTransport(handler)}
    return asyncio.run(create_server(name, before_write=guard, **options) if create else get_server_create_capabilities(**options))


@pytest.mark.parametrize("admin,create_permission,disabled,non_admin_disabled,allowed", [
    (True, False, False, True, True), (False, True, False, False, True),
    (False, False, False, False, False), (False, True, False, True, False),
    (True, True, True, False, False), (False, True, True, False, False),
])
def test_creation_uses_native_server_specific_creation_permission(admin, create_permission, disabled, non_admin_disabled, allowed):
    handler = upstream(user={"admin": admin, "create_server_permissions": create_permission},
                       policy={"ui_write_disabled": disabled, "disable_non_admin_create": non_admin_disabled})
    assert run(handler)["can_create"] is allowed
    if allowed:
        assert run(handler, create=True)["created"] is True
    else:
        with pytest.raises(KomodoActionError, match="server_create_forbidden"):
            run(handler, create=True)


def test_creation_sends_only_native_name_and_returns_no_resource_secrets_or_config():
    writes = []
    result = run(upstream(writes=writes), create=True)
    assert writes == [{"type": "CreateServer", "params": {"name": "New Server 中文"}}]
    assert result == {"ok": True, "created": True, "status": "completed", "server_id": SERVER,
        "server_name": "New Server 中文", "operation_id": None, "error_code": None, "link": None, "link_error": None}
    assert KEY not in json.dumps(result) and SECRET not in json.dumps(result)


@pytest.mark.parametrize("name", ["", " leading", "trailing ", "line\nbreak", "tab\tname", "a" * 513, "a" * 24, None, True])
def test_names_have_exact_native_semantics_and_no_implicit_normalization(name):
    with pytest.raises(KomodoActionError, match="invalid_server_name"):
        validate_server_name(name)


@pytest.mark.parametrize("response,status", [
    (httpx.ReadTimeout(SECRET), "outcome_unknown"), (httpx.Response(503, text=SECRET), "outcome_unknown"),
    (httpx.Response(408, text=SECRET), "outcome_unknown"), (httpx.Response(302, headers={"location": "https://private.invalid/" + SECRET}), "outcome_unknown"),
    (httpx.Response(200, text="malformed"), "outcome_unknown"), (httpx.Response(409, text=SECRET), "failed"),
    (httpx.Response(403, text=SECRET), "failed"),
])
def test_submissions_are_never_retried_and_errors_are_fixed_safe_codes(response, status):
    writes = []
    result = run(upstream(response=response, writes=writes), create=True)
    assert len(writes) == 1 and result["status"] == status and result["server_id"] is None
    assert result["created"] is (None if status == "outcome_unknown" else False)
    assert SECRET not in json.dumps(result)


def test_guest_guard_can_abort_before_any_creation():
    writes = []
    async def guard():
        raise KomodoActionError("guest_identity_changed", 409)
    with pytest.raises(KomodoActionError, match="guest_identity_changed"):
        run(upstream(writes=writes), create=True, guard=guard)
    assert not writes


def test_known_integration_credentials_cannot_be_used_as_name_and_reflected():
    writes = []
    with pytest.raises(KomodoActionError, match="invalid_server_name"):
        run(upstream(writes=writes), create=True, name=SECRET)
    assert not writes
