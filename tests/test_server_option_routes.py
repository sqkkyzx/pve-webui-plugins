"""API boundary checks; upstream/guest fingerprint logic has separate tests."""

import json
import sqlite3
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from plugins.komodo.actions import KomodoActionError
from plugins.komodo.guest_links import GuestLinkError
from plugins.komodo.komodo import KomodoClient
from app.main import Settings, create_app
from app.pve_auth import PveAuthenticator
from plugins.komodo.server_options import GuestBoundOptionsService


SERVER = "a" * 24
VERSION = "b" * 64
PATH = "/pve-plugins/api/komodo/guests/qemu/101/options"
COOKIE = "PVEAuthCookie=test-ticket"


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    state = {"reads": [], "writes": [], "resolve": [], "revision": 1, "change_before_write": False,
             "error": None, "unknown": False}
    ledger = tmp_path / "actions.sqlite3"

    async def authenticate(self, request):
        if request.cookies.get("PVEAuthCookie") != "test-ticket":
            raise HTTPException(401, "pve_auth_required")
        return "root@pam"

    async def resolve(self, request, guest_type, vmid, *, expected_server_id=None, expected_revision=None):
        state["resolve"].append((guest_type, vmid, expected_server_id, expected_revision))
        if expected_server_id is not None and expected_server_id != SERVER:
            raise GuestLinkError("guest_link_revision_conflict", 409)
        if expected_revision is not None and expected_revision != state["revision"]:
            raise GuestLinkError("guest_link_revision_conflict", 409)
        return {"server_id": SERVER, "binding_revision": state["revision"], "guest_type": guest_type, "vmid": vmid}

    async def read(self, server_id):
        state["reads"].append(server_id)
        return {"server_id": server_id, "server_name": "docker-guest", "server_state": "Ok",
                "config": {"enabled": True}, "public_key": "", "version": VERSION,
                "can_write": True, "can_rotate": True}

    async def write(self, server_id, *, action, base_version, config=None, public_key=None, before_write=None):
        if state["change_before_write"]:
            state["revision"] += 1
        await before_write()
        if state["error"]:
            raise state["error"]
        with sqlite3.connect(ledger) as db:
            assert db.execute("SELECT COUNT(*) FROM actions WHERE status IS NULL").fetchone()[0] == 1
        state["writes"].append({"server_id": server_id, "action": action, "base_version": base_version,
                                "config": config, "public_key": public_key})
        if state["unknown"]:
            raise RuntimeError("PRIVATE_UPSTREAM_FAILURE")
        return {"ok": True, "status": "completed", "operation_id": None, "error_code": None}

    monkeypatch.setattr(PveAuthenticator, "authenticate", authenticate)
    monkeypatch.setattr(GuestBoundOptionsService, "resolve", resolve)
    monkeypatch.setattr(KomodoClient, "server_options", read)
    monkeypatch.setattr(KomodoClient, "update_server_options", write)
    settings = Settings("https://pve.example:8006", komodo_url="http://core:9120",
                        komodo_api_key="key", komodo_api_secret="secret", action_state_file=str(ledger))
    client = TestClient(create_app(settings))
    response = client.get(PATH, headers={"cookie": COOKIE})
    assert response.status_code == 200
    state["headers"] = {"cookie": COOKIE, "origin": "http://testserver",
                        "x-pve-plugins-csrf": response.json()["csrf_token"]}
    state["ledger"] = ledger
    return client, state


def command(**changes):
    return {"action": "save", "server_id": SERVER, "binding_revision": 1, "base_version": VERSION,
            "request_id": str(uuid.uuid4()), "config": {"enabled": False}, **changes}


def test_read_requires_session_and_rechecks_binding_after_read(boundary):
    client, state = boundary
    state["resolve"].clear()
    response = client.get(PATH, headers={"cookie": COOKIE})
    assert response.status_code == 200
    assert state["resolve"] == [("qemu", 101, None, None), ("qemu", 101, SERVER, 1)]
    assert "no-store" in response.headers["cache-control"]
    assert response.json()["binding_revision"] == 1
    assert client.get(PATH).status_code == 401
    assert client.post(PATH, json=command()).status_code == 401
    assert not state["writes"]


@pytest.mark.parametrize("path", [PATH + "?_dc=1", PATH + "?server_id=" + SERVER,
                                  PATH.replace("qemu", "node"), PATH.replace("101", "0101"),
                                  PATH.replace("101", "99"), PATH.replace("101", "1000000000")])
def test_strict_route_context_and_query(boundary, path):
    client, state = boundary
    assert client.get(path, headers={"cookie": COOKIE}).status_code == 422
    assert client.post(path, headers=state["headers"], json=command()).status_code == 422
    assert not state["writes"]


@pytest.mark.parametrize("changes", [{"csrf": "bad"}, {"origin": "https://evil.example"}, {"cookie": "PVEAuthCookie=other-ticket"}])
def test_write_session_csrf_and_origin(boundary, changes):
    client, state = boundary
    headers = dict(state["headers"])
    headers.update({"x-pve-plugins-csrf" if key == "csrf" else key: value for key, value in changes.items()})
    assert client.post(PATH, headers=headers, json=command()).status_code in {401, 403}
    assert not state["writes"]


@pytest.mark.parametrize("body", [
    {"config": {}}, {"config": {"passkey": "PRIVATE"}}, {"config": {"enabled": "true"}},
    {"config": {"links": ["https://example.com"]}}, {"config": {"maintenance_windows": [{}]}},
    {"public_key": "", "config": {"enabled": True}}, {"unknown": True}, {"binding_revision": True},
    {"server_id": "other"}, {"request_id": "not-a-uuid"}, {"base_version": ""},
    {"action": "rotate_keys", "config": None}, {"action": "public_key", "public_key": None},
])
def test_fixed_command_schema_rejects_unknown_or_mixed_payloads(boundary, body):
    client, state = boundary
    assert client.post(PATH, headers=state["headers"], json=command(**body)).status_code == 422
    assert not state["writes"]


def test_correct_partial_write_deduplicates_and_ledger_does_not_store_config(boundary):
    client, state = boundary
    value = command(config={"region": "PRIVATE_CONFIGURATION_VALUE"})
    first = client.post(PATH, headers=state["headers"], json=value)
    assert first.status_code == 200 and first.json()["status"] == "completed"
    assert state["writes"][0]["config"] == {"region": "PRIVATE_CONFIGURATION_VALUE"}
    assert client.post(PATH, headers=state["headers"], json=value).json() == first.json()
    assert len(state["writes"]) == 1
    assert client.post(PATH, headers=state["headers"], json={**value, "config": {"region": "different"}}).status_code == 409
    with sqlite3.connect(state["ledger"]) as db:
        stored = repr(db.execute("SELECT * FROM actions").fetchall())
    assert "PRIVATE_CONFIGURATION_VALUE" not in stored


@pytest.mark.parametrize("changes", [{"server_id": "c" * 24}, {"binding_revision": 2}])
def test_expected_server_and_revision_cannot_select_another_target(boundary, changes):
    client, state = boundary
    assert client.post(PATH, headers=state["headers"], json=command(**changes)).status_code == 409
    assert not state["writes"]


def test_binding_change_during_preflight_blocks_dispatch(boundary):
    client, state = boundary
    state["change_before_write"] = True
    response = client.post(PATH, headers=state["headers"], json=command())
    assert response.status_code == 409
    assert response.json()["detail"] == "guest_link_revision_conflict"
    assert not state["writes"]


def test_unknown_outcome_keeps_one_receipt_and_never_retries(boundary):
    client, state = boundary
    state["unknown"] = True
    value = command()
    first = client.post(PATH, headers=state["headers"], json=value)
    assert first.status_code == 200 and first.json()["status"] == "outcome_unknown"
    assert "PRIVATE" not in first.text
    assert client.post(PATH, headers=state["headers"], json=value).json() == first.json()
    assert len(state["writes"]) == 1


def test_preflight_error_is_sanitized_and_receipted(boundary):
    client, state = boundary
    state["error"] = KomodoActionError("server_options_changed", 409)
    value = command()
    response = client.post(PATH, headers=state["headers"], json=value)
    assert response.status_code == 409 and response.json() == {"detail": "server_options_changed"}
    assert client.post(PATH, headers=state["headers"], json=value).status_code == 409


def test_rotation_is_a_separate_fixed_action_without_config(boundary):
    client, state = boundary
    value = command(action="rotate_keys")
    del value["config"]
    assert client.post(PATH, headers=state["headers"], json=value).status_code == 200
    assert state["writes"][0]["action"] == "rotate_keys" and state["writes"][0]["config"] is None


def test_size_and_json_type_rejected_before_dispatch(boundary):
    client, state = boundary
    assert client.post(PATH, headers=state["headers"], content="text").status_code == 415
    headers = {**state["headers"], "content-type": "application/json"}
    assert client.post(PATH, headers=headers, content="{" + "x" * 65536).status_code == 413
    assert client.post(PATH, headers=headers, content="{bad").status_code == 422
    assert not state["writes"]
