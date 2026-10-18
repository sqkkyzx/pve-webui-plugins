"""Inventory API authorization, guest binding, schema and durable receipts."""

import json
import sqlite3
import uuid
from urllib.parse import parse_qs, quote

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest

from plugins.komodo.actions import KomodoActionError
from plugins.komodo.guest_links import GuestLinkError
from app.main import Settings, create_app
from app.pve_auth import PveAuthenticator
from plugins.komodo.server_options import GuestBoundOptionsService
import plugins.komodo.server_inventory_routes as routes


SERVER, CONTAINER, VERSION = "a" * 24, "b" * 64, "c" * 64
BASE = "/pve-plugins/api/komodo/guests/qemu/101"
COOKIE = "PVEAuthCookie=test-ticket"


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    ledger = tmp_path / "actions.sqlite3"
    state = {"reads": [], "writes": [], "resolve": [], "revision": 1, "change_read": False,
             "change_write": False, "error": None, "unknown": False}

    async def authenticate(self, request):
        if request.cookies.get("PVEAuthCookie") != "test-ticket":
            raise HTTPException(401, "pve_auth_required")
        return "root@pam"

    async def resolve(self, request, guest_type, vmid, *, expected_server_id=None, expected_revision=None):
        state["resolve"].append((guest_type, vmid, expected_server_id, expected_revision))
        if expected_server_id is not None and expected_server_id != SERVER or expected_revision is not None and expected_revision != state["revision"]:
            raise GuestLinkError("guest_link_revision_conflict", 409)
        return {"server_id": SERVER, "binding_revision": state["revision"]}

    async def read(server_id, *, kind, **options):
        state["reads"].append((server_id, kind))
        if state["change_read"]:
            state["revision"] += 1
        return {"server_id": server_id, "server_name": "host", "can_execute": True, "can_write": True,
                "containers": [{"id": CONTAINER, "version": VERSION, "name": "app"}]} if kind == "docker" else {
                "server_id": server_id, "server_name": "host", "stacks": [], "deployments": [], "repos": []}

    async def write(server_id, *, section, action, targets, name, before_write, **options):
        if state["change_write"]:
            state["revision"] += 1
        await before_write()
        if state["error"]:
            raise state["error"]
        with sqlite3.connect(ledger) as db:
            assert db.execute("SELECT COUNT(*) FROM actions WHERE status IS NULL").fetchone()[0] == 1
        state["writes"].append({"server_id": server_id, "section": section, "action": action, "targets": targets, "name": name})
        if state["unknown"]:
            raise RuntimeError("PRIVATE_UPSTREAM_FAILURE")
        return {"section": section, "action": action, "results": [{"id": targets[0]["id"] if targets else name,
                "ok": True, "status": "submitted", "operation_id": "d" * 24, "error_code": None}]}

    monkeypatch.setattr(PveAuthenticator, "authenticate", authenticate)
    monkeypatch.setattr(GuestBoundOptionsService, "resolve", resolve)
    monkeypatch.setattr(routes, "get_server_inventory", read)
    monkeypatch.setattr(routes, "perform_inventory_action", write)
    client = TestClient(create_app(Settings("https://pve.example:8006", komodo_url="http://core:9120",
        komodo_api_key="key", komodo_api_secret="secret", action_state_file=str(ledger))))
    result = client.get(BASE + "/docker", headers={"cookie": COOKIE})
    assert result.status_code == 200
    state["headers"] = {"cookie": COOKIE, "origin": "http://testserver", "x-pve-plugins-csrf": result.json()["csrf_token"]}
    state["ledger"] = ledger
    return client, state


def command(**changes):
    return {"section": "containers", "action": "restart", "server_id": SERVER, "binding_revision": 1,
            "request_id": str(uuid.uuid4()), "targets": [{"id": CONTAINER, "version": VERSION}], **changes}


def test_both_reads_require_auth_recheck_binding_and_are_not_cacheable(boundary):
    client, state = boundary
    for kind in ("docker", "resources"):
        state["resolve"].clear()
        response = client.get(BASE + "/" + kind, headers={"cookie": COOKIE})
        assert response.status_code == 200 and response.json()["binding_revision"] == 1
        assert "no-store" in response.headers["cache-control"]
        assert state["resolve"] == [("qemu", 101, None, None), ("qemu", 101, SERVER, 1)]
        assert client.get(BASE + "/" + kind).status_code == 401
    assert client.post(BASE + "/inventory-actions", json=command()).status_code == 401
    assert not state["writes"]


def test_binding_changed_during_read_does_not_publish_stale_server_inventory(boundary):
    client, state = boundary
    state["change_read"] = True
    response = client.get(BASE + "/docker", headers={"cookie": COOKIE})
    assert response.status_code == 409 and response.json()["detail"] == "guest_link_revision_conflict"


@pytest.mark.parametrize("context", ["node/101", "qemu/0101", "qemu/99", "qemu/1000000000"])
def test_guest_type_vmid_are_strict_before_dispatch(boundary, context):
    client, state = boundary
    prefix = "/pve-plugins/api/komodo/guests/" + context
    for kind in ("docker", "resources"):
        assert client.get(prefix + "/" + kind, headers={"cookie": COOKIE}).status_code == 422
    assert client.post(prefix + "/inventory-actions", headers=state["headers"], json=command()).status_code == 422
    assert not state["writes"]


@pytest.mark.parametrize("query", ["?_dc=1", "?server_id=" + SERVER, "?a=1&a=2"])
def test_no_query_can_retarget_or_alter_inventory_semantics(boundary, query):
    client, state = boundary
    assert client.get(BASE + "/docker" + query, headers={"cookie": COOKIE}).status_code == 422
    assert client.post(BASE + "/inventory-actions" + query, headers=state["headers"], json=command()).status_code == 422
    assert not state["writes"]


@pytest.mark.parametrize("header,value", [("x-pve-plugins-csrf", "bad"), ("origin", "https://evil.example"), ("cookie", "PVEAuthCookie=other-ticket")])
def test_every_write_requires_existing_session_csrf_and_same_origin(boundary, header, value):
    client, state = boundary
    response = client.post(BASE + "/inventory-actions", headers={**state["headers"], header: value}, json=command())
    assert response.status_code in {401, 403} and not state["writes"]


@pytest.mark.parametrize("changes", [
    {"section": "deployments"}, {"section": "repos"}, {"section": "networks", "action": "prune"},
    {"action": "execute_arbitrary"}, {"action": "create_stack"}, {"binding_revision": True}, {"server_id": "bad"},
    {"targets": []}, {"targets": [{"id": CONTAINER, "version": "bad"}]},
    {"targets": [{"id": CONTAINER, "version": VERSION, "name": "replacement"}]},
    {"targets": [{"id": CONTAINER, "version": VERSION}] * 2}, {"request_id": "bad"},
    {"name": "unexpected"}, {"config": {"passkey": "PRIVATE"}},
    {"section": "stacks", "action": "create_stack", "targets": [], "name": "unsafe; shell"},
    {"section": "stacks", "action": "create_stack", "targets": [], "name": SERVER},
    {"section": "stacks", "action": "create_stack", "targets": [], "name": "My.Stack"},
])
def test_fixed_schema_rejects_arbitrary_routes_mixed_inputs_and_duplicate_targets(boundary, changes):
    client, state = boundary
    response = client.post(BASE + "/inventory-actions", headers=state["headers"], json=command(**changes))
    assert response.status_code == 422 and not state["writes"]


def test_selected_action_is_reserved_before_write_and_same_uuid_never_replays(boundary):
    client, state = boundary
    value = command()
    first = client.post(BASE + "/inventory-actions", headers=state["headers"], json=value)
    assert first.status_code == 200 and first.json()["results"][0]["status"] == "submitted"
    assert state["writes"] == [{"server_id": SERVER, "section": "containers", "action": "restart", "targets": value["targets"], "name": None}]
    assert client.post(BASE + "/inventory-actions", headers=state["headers"], json=value).json() == first.json()
    assert len(state["writes"]) == 1
    assert client.post(BASE + "/inventory-actions", headers=state["headers"], json={**value, "action": "stop"}).status_code == 409


@pytest.mark.parametrize("changes", [{"server_id": "e" * 24}, {"binding_revision": 2}])
def test_pending_confirmation_cannot_target_changed_guest_association(boundary, changes):
    client, state = boundary
    assert client.post(BASE + "/inventory-actions", headers=state["headers"], json=command(**changes)).status_code == 409
    assert not state["writes"]


def test_mapping_change_during_preflight_blocks_write_and_is_receipted(boundary):
    client, state = boundary
    state["change_write"] = True
    value = command()
    response = client.post(BASE + "/inventory-actions", headers=state["headers"], json=value)
    assert response.status_code == 409 and not state["writes"]
    assert response.json()["detail"] == "guest_link_revision_conflict"


def test_unknown_failure_is_sanitized_and_returned_from_durable_receipt(boundary):
    client, state = boundary
    state["unknown"] = True
    value = command()
    first = client.post(BASE + "/inventory-actions", headers=state["headers"], json=value)
    assert first.status_code == 200 and first.json()["results"][0]["status"] == "outcome_unknown"
    assert "PRIVATE" not in first.text
    assert client.post(BASE + "/inventory-actions", headers=state["headers"], json=value).json() == first.json()
    assert len(state["writes"]) == 1


def test_new_stack_uses_explicit_fixed_action_and_only_name(boundary):
    client, state = boundary
    value = command(section="stacks", action="create_stack", targets=[], name="new-stack")
    response = client.post(BASE + "/inventory-actions", headers=state["headers"], json=value)
    assert response.status_code == 200
    assert state["writes"][0] == {"server_id": SERVER, "section": "stacks", "action": "create_stack", "targets": [], "name": "new-stack"}


def test_body_type_size_and_json_are_checked_before_upstream_dispatch(boundary):
    client, state = boundary
    assert client.post(BASE + "/inventory-actions", headers=state["headers"], content="text").status_code == 415
    headers = {**state["headers"], "content-type": "application/json"}
    assert client.post(BASE + "/inventory-actions", headers=headers, content="{" + "x" * 32768).status_code == 413
    assert client.post(BASE + "/inventory-actions", headers=headers, content="{bad").status_code == 422
    assert not state["writes"]


@pytest.mark.parametrize("replace_guest", [False, True])
def test_full_transport_chain_verifies_pve_fingerprint_after_permissions_before_mutation(tmp_path, replace_guest):
    """No auth/service mocks: only external HTTP is replaced by fake transports."""
    pve_origin, core_origin = "https://pve.example:8006", "http://core:9120"
    ticket = "PVE:root@pam:1234ABCD::signature+value="
    cookie = "PVEAuthCookie=" + quote(ticket, safe="")
    fingerprint = "9f376f89-d6b7-4dc8-84a4-f4769dc9b9bb"
    ledger = tmp_path / "actions.sqlite3"
    state = {"events": [], "writes": [], "uuid": fingerprint, "replace": False}

    def pve(request):
        assert str(request.url).startswith(pve_origin + "/")
        assert request.headers["cookie"] == cookie and "x-api-key" not in request.headers
        path = request.url.path
        state["events"].append(path)
        if path == "/api2/json/access/permissions":
            return httpx.Response(200, json={"data": {"/": {"Sys.Audit": 1}}})
        if path == "/api2/json/access/ticket":
            assert parse_qs(request.content.decode())["password"] == [ticket]
            return httpx.Response(200, json={"data": {"username": "root@pam"}})
        assert request.method == "GET", "guest inventory cannot mutate PVE"
        if path == "/api2/json/cluster/resources":
            return httpx.Response(200, json={"data": [{"type": "qemu", "vmid": 101, "node": "pve-one", "name": "host", "status": "running"}]})
        assert path == "/api2/json/nodes/pve-one/qemu/101/config"
        return httpx.Response(200, json={"data": {"smbios1": "uuid=" + state["uuid"]}})

    def core(request):
        assert str(request.url).startswith(core_origin + "/")
        assert request.headers["x-api-key"] == "key" and request.headers["x-api-secret"] == "secret"
        assert "cookie" not in request.headers
        if request.url.path == "/user":
            return httpx.Response(200, json={"username": "integration", "enabled": True}, headers={"set-cookie": "ignored=must-not-replay; Path=/"})
        payload = json.loads(request.content)
        variant = payload["type"]
        state["events"].append(variant)
        if request.url.path == "/execute":
            assert payload == {"type": "RestartContainer", "params": {"server": SERVER, "container": CONTAINER}}
            assert max(i for i, event in enumerate(state["events"]) if event.endswith("/config")) > max(
                i for i, event in enumerate(state["events"]) if event == "GetPermission")
            with sqlite3.connect(ledger) as db:
                assert db.execute("SELECT COUNT(*) FROM actions WHERE status IS NULL").fetchone()[0] == 1
            state["writes"].append(payload)
            return httpx.Response(200, json={"_id": "d" * 24, "operation": variant,
                "target": {"type": "Server", "id": SERVER}, "status": "Queued", "success": False, "logs": ["PRIVATE"]})
        assert request.url.path == "/read"
        if state["replace"] and variant == "ListContainers":
            state["uuid"] = "ec5c382f-b7d3-4fa2-9d37-08e72fc3d826"
        data = {
            "GetServer": {"_id": SERVER, "name": "host", "template": False, "config": {"enabled": True}},
            "ListServers": [{"id": SERVER, "name": "host", "template": False, "info": {"state": "Ok"}}],
            "GetPermission": {"level": "Write", "specific": ["Attach"]},
            "GetCoreInfo": {"ui_write_disabled": False, "disable_non_admin_create": False},
            "ListContainers": [{"id": CONTAINER, "name": "app", "server_id": SERVER, "state": "running"}],
            "ListNetworks": [], "ListVolumes": [], "ListImages": [],
        }[variant]
        return httpx.Response(200, json=data)

    application = create_app(Settings(pve_origin, komodo_url=core_origin, komodo_api_key="key", komodo_api_secret="secret",
        action_state_file=str(ledger), guest_links_file=str(tmp_path / "links.sqlite3")),
        pve_auth_transport=httpx.MockTransport(pve), komodo_transport=httpx.MockTransport(core))
    with TestClient(application) as browser:
        listing = browser.get("/pve-plugins/api/komodo/guest-links", headers={"cookie": cookie})
        assert listing.status_code == 200
        headers = {"cookie": cookie, "origin": "http://testserver", "x-pve-plugins-csrf": listing.json()["csrf_token"]}
        binding = browser.post("/pve-plugins/api/komodo/guest-links", headers=headers, json={"server_id": SERVER,
            "guest_type": "qemu", "vmid": 101, "expected_revision": 0})
        assert binding.status_code == 200
        opened = browser.get(BASE + "/docker", headers={"cookie": cookie})
        assert opened.status_code == 200
        row = opened.json()["containers"][0]
        value = command(targets=[{"id": row["id"], "version": row["version"]}])
        state["replace"] = replace_guest
        first = browser.post(BASE + "/inventory-actions", headers=headers, json=value)
        if replace_guest:
            assert first.status_code == 409 and not state["writes"]
        else:
            assert first.status_code == 200 and first.json()["results"][0]["status"] == "submitted"
            assert len(state["writes"]) == 1
            assert browser.post(BASE + "/inventory-actions", headers=headers, json=value).json() == first.json()
            assert len(state["writes"]) == 1
        assert "no-store" in first.headers["cache-control"] and "PRIVATE" not in first.text
