"""Real auth, mock HTTP, durable creation and optional guest association."""

import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, quote
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from plugins.komodo.action_store import ActionStore
from app.csrf import CsrfProtector
from plugins.komodo.guest_links import GuestLinkError, GuestLinkStore
from plugins.komodo.komodo import KomodoClient
from app.pve_auth import PveAuthenticator
from plugins.komodo.server_create_routes import register_server_create_routes


SERVER, OTHER = "a" * 24, "b" * 24
PVE, CORE, KEY, SECRET = "https://pve.example:8006", "http://core:9120", "create-key", "create-private-secret"
TICKET = "PVE:root@pam:1234ABCD::signature+value="
COOKIE = "PVEAuthCookie=" + quote(TICKET, safe="")
UUID = "9f376f89-d6b7-4dc8-84a4-f4769dc9b9bb"


@pytest.fixture
def integrated(tmp_path):
    state = {"events": [], "writes": [], "uuid": UUID, "mac": "02:00:00:00:00:01", "fingerprints": 0,
        "change_before": False, "change_after": False, "race_link": False, "permission": True,
        "guest_failure": False, "core_response": None, "with_store": True,
        "guests": [{"type": "qemu", "vmid": 101, "name": "vm", "node": "pve-one", "status": "running"},
                   {"type": "lxc", "vmid": 102, "name": "ct", "node": "pve-one", "status": "running"},
                   {"type": "qemu", "vmid": 103, "name": "template", "node": "pve-one", "template": 1}]}
    links_path, ledger_path = tmp_path / "links.sqlite3", tmp_path / "actions.sqlite3"
    store = GuestLinkStore(str(links_path), komodo_origin=CORE, pve_origin=PVE)
    state.update(store=store, links_path=links_path, ledger_path=ledger_path)

    def pve(request):
        assert str(request.url).startswith(PVE + "/") and request.headers["cookie"] == COOKIE
        assert "x-api-key" not in request.headers and "authorization" not in request.headers
        path = request.url.path
        state["events"].append(path)
        if path == "/api2/json/access/permissions":
            return httpx.Response(200, json={"data": {"/": {"Sys.Audit": 1}}})
        if path == "/api2/json/access/ticket":
            assert parse_qs(request.content.decode())["password"] == [TICKET]
            return httpx.Response(200, json={"data": {"username": "root@pam"}})
        assert request.method == "GET", "creating a Komodo Server must never mutate PVE"
        if state["guest_failure"]:
            return httpx.Response(503, text=SECRET)
        if path == "/api2/json/cluster/resources":
            return httpx.Response(200, json={"data": state["guests"]})
        assert path in {"/api2/json/nodes/pve-one/qemu/101/config", "/api2/json/nodes/pve-one/lxc/102/config"}
        state["fingerprints"] += 1
        if state["change_before"] and state["fingerprints"] == 2:
            state["uuid"] = "ec5c382f-b7d3-4fa2-9d37-08e72fc3d826"
        return httpx.Response(200, json={"data": {"smbios1": "uuid=" + state["uuid"], "net0": "hwaddr=" + state["mac"]}})

    def core(request):
        assert str(request.url).startswith(CORE + "/")
        assert request.headers["x-api-key"] == KEY and request.headers["x-api-secret"] == SECRET
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if request.url.path == "/user":
            return httpx.Response(200, json={"enabled": True, "username": "integration", "admin": state["permission"]},
                headers={"set-cookie": "private=must-not-replay; Path=/"})
        body = json.loads(request.content)
        state["events"].append(body["type"])
        if request.url.path == "/read":
            assert body == {"type": "GetCoreInfo", "params": {}}
            return httpx.Response(200, json={"ui_write_disabled": False, "disable_non_admin_create": True})
        assert request.url.path == "/write" and body == {"type": "CreateServer", "params": {"name": "New Server 中文"}}
        with sqlite3.connect(ledger_path) as db:
            assert db.execute("SELECT COUNT(*) FROM actions WHERE status IS NULL").fetchone()[0] == 1
        if state["fingerprints"]:
            assert state["events"][-2].endswith("/config"), "fingerprint checked after permission immediately before create"
        state["writes"].append(body)
        if state["change_after"]:
            state["uuid"] = "ec5c382f-b7d3-4fa2-9d37-08e72fc3d826"
        if state["race_link"]:
            store.save(OTHER, {"guest_type": "qemu", "vmid": 101}, {"kind": "vm_uuid", "values": [UUID], "strength": "strong"}, expected_revision=0)
        response = state["core_response"]
        if isinstance(response, Exception):
            raise response
        return response if response is not None else httpx.Response(200, json={"_id": {"$oid": SERVER}, "name": "New Server 中文",
            "template": False, "config": {"passkey": SECRET}, "info": {"private_key": SECRET}})

    def make(with_store=True):
        app = FastAPI()
        auth = PveAuthenticator(PVE, transport=httpx.MockTransport(pve))
        komodo = KomodoClient(CORE, KEY, SECRET, transport=httpx.MockTransport(core))
        settings = SimpleNamespace(upstream=PVE, komodo_url=CORE, tls_context=lambda: True)
        register_server_create_routes(app, settings, auth, komodo, CsrfProtector(), store if with_store else None,
            ActionStore(str(ledger_path)), set(), transport=httpx.MockTransport(pve))
        return TestClient(app)
    state["make"] = make
    with make() as client:
        state["browser"] = client
        options = client.get("/pve-komodo/api/server-create-options", headers={"cookie": COOKIE})
        assert options.status_code == 200
        state["headers"] = {"cookie": COOKIE, "origin": "http://testserver", "x-pve-komodo-csrf": options.json()["csrf_token"]}
        yield state
    store.close()


def command(**changes):
    return {"name": "New Server 中文", "request_id": str(uuid.uuid4()), **changes}


def test_options_auth_and_real_native_creation_permission(integrated):
    state = integrated; client = state["browser"]
    assert client.get("/pve-komodo/api/server-create-options").status_code == 401
    response = client.get("/pve-komodo/api/server-create-options", headers={"cookie": COOKIE})
    assert response.json()["can_create"] is True and response.json()["can_link"] is True
    assert [row["vmid"] for row in response.json()["guests"]] == [101, 102]
    state["permission"] = False
    assert client.get("/pve-komodo/api/server-create-options", headers={"cookie": COOKIE}).json()["can_create"] is False
    assert client.post("/pve-komodo/api/servers/create", headers=state["headers"], json=command()).status_code == 403
    assert not state["writes"]


def test_unbound_creation_works_and_does_not_fetch_or_persist_guest_fingerprint(integrated):
    state = integrated; client = state["browser"]
    value = command()
    response = client.post("/pve-komodo/api/servers/create", headers=state["headers"], json=value)
    assert response.status_code == 200 and response.json()["created"] is True and response.json()["link"] is None
    assert response.json()["server_id"] == SERVER and state["fingerprints"] == 0 and state["store"].list() == []
    assert client.post("/pve-komodo/api/servers/create", headers=state["headers"], json=value).json() == response.json()
    assert len(state["writes"]) == 1 and SECRET not in response.text
    assert client.post("/pve-komodo/api/servers/create", headers=state["headers"], json={**value, "name": "changed"}).status_code == 409


@pytest.mark.parametrize("guest_type,vmid,strength", [("qemu", 101, "strong"), ("lxc", 102, "weak")])
def test_optional_link_rechecks_exact_identity_before_and_after_create(integrated, guest_type, vmid, strength):
    state = integrated; client = state["browser"]
    response = client.post("/pve-komodo/api/servers/create", headers=state["headers"], json=command(guest_type=guest_type, vmid=vmid))
    assert response.status_code == 200
    data = response.json()
    assert data["created"] is True and data["status"] == "completed" and data["link"]["server_id"] == SERVER
    assert data["link"]["guest_type"] == guest_type and data["link"]["vmid"] == vmid and data["link"]["revision"] == 1
    assert data["link"]["identity_strength"] == strength and state["fingerprints"] == 3
    assert len(state["writes"]) == 1 and len(state["store"].list()) == 1
    assert SECRET not in response.text and UUID not in response.text and "02:00:00" not in response.text


def test_replaced_guest_before_create_prevents_any_upstream_mutation(integrated):
    state = integrated; state["change_before"] = True
    response = state["browser"].post("/pve-komodo/api/servers/create", headers=state["headers"], json=command(guest_type="qemu", vmid=101))
    assert response.status_code == 409 and response.json()["detail"] == "guest_identity_changed"
    assert not state["writes"] and state["store"].list() == []


@pytest.mark.parametrize("failure", ["change_after", "race_link", "store_failure"])
def test_created_but_unlinked_is_a_durable_receipt_and_never_deleted_or_recreated(integrated, failure, monkeypatch):
    state = integrated; client = state["browser"]
    if failure == "store_failure":
        def unavailable(*args, **kwargs):
            raise GuestLinkError("guest_link_store_unavailable")
        monkeypatch.setattr(state["store"], "save", unavailable)
    else:
        state[failure] = True
    value = command(guest_type="qemu", vmid=101)
    first = client.post("/pve-komodo/api/servers/create", headers=state["headers"], json=value)
    assert first.status_code == 200 and first.json()["created"] is True and first.json()["status"] == "created_unlinked"
    assert first.json()["server_id"] == SERVER and first.json()["link"] is None and first.json()["link_error"]
    assert client.post("/pve-komodo/api/servers/create", headers=state["headers"], json=value).json() == first.json()
    assert len(state["writes"]) == 1 and SECRET not in first.text


@pytest.mark.parametrize("outcome", [httpx.ReadTimeout(SECRET), httpx.Response(503, text=SECRET), httpx.Response(200, text="invalid")])
def test_ambiguous_create_never_attempts_association_or_retry(integrated, outcome):
    state = integrated; state["core_response"] = outcome; client = state["browser"]
    value = command(guest_type="qemu", vmid=101)
    first = client.post("/pve-komodo/api/servers/create", headers=state["headers"], json=value)
    assert first.status_code == 200 and first.json()["status"] == "outcome_unknown" and first.json()["created"] is None
    assert first.json()["server_id"] is None and state["store"].list() == []
    assert client.post("/pve-komodo/api/servers/create", headers=state["headers"], json=value).json() == first.json()
    assert len(state["writes"]) == 1 and SECRET not in first.text


def test_guest_discovery_failure_does_not_block_unbound_creation(integrated):
    state = integrated; state["guest_failure"] = True; client = state["browser"]
    response = client.get("/pve-komodo/api/server-create-options", headers={"cookie": COOKIE})
    assert response.json()["can_create"] and response.json()["guests"] == [] and response.json()["errors"]
    assert client.post("/pve-komodo/api/servers/create", headers=state["headers"], json=command()).json()["created"] is True


@pytest.mark.parametrize("selection", [{"guest_type": "qemu", "vmid": 103}, {"guest_type": "qemu", "vmid": 999}])
def test_template_or_inaccessible_guest_cannot_be_associated(integrated, selection):
    state = integrated
    response = state["browser"].post("/pve-komodo/api/servers/create", headers=state["headers"], json=command(**selection))
    assert response.status_code == 409 and not state["writes"]


def test_already_linked_guest_hidden_from_options_and_rejected_before_create(integrated):
    state = integrated
    state["store"].save(OTHER, {"guest_type": "qemu", "vmid": 101},
        {"kind": "vm_uuid", "values": [UUID], "strength": "strong"}, expected_revision=0)
    options = state["browser"].get("/pve-komodo/api/server-create-options", headers={"cookie": COOKIE})
    assert [row["vmid"] for row in options.json()["guests"]] == [102]
    response = state["browser"].post("/pve-komodo/api/servers/create", headers=state["headers"], json=command(guest_type="qemu", vmid=101))
    assert response.status_code == 409 and not state["writes"]


def test_guest_without_stable_fingerprint_prevents_create(integrated):
    state = integrated; state["uuid"] = "not-a-uuid"
    response = state["browser"].post("/pve-komodo/api/servers/create", headers=state["headers"], json=command(guest_type="qemu", vmid=101))
    assert response.status_code == 409 and response.json()["detail"] == "guest_identity_unavailable"
    assert not state["writes"]


@pytest.mark.parametrize("changes", [{"guest_type": "qemu"}, {"vmid": 101}, {"guest_type": "node", "vmid": 101},
    {"guest_type": "qemu", "vmid": True}, {"guest_type": "qemu", "vmid": 99}, {"name": "leading "},
    {"name": ""}, {"name": SERVER}, {"name": "line\nbreak"}, {"request_id": "bad"}, {"config": {"enabled": True}},
    {"public_key": "unexpected"}, {"server_id": SERVER}])
def test_schema_cannot_smuggle_config_keys_routes_or_partial_selection(integrated, changes):
    state = integrated
    assert state["browser"].post("/pve-komodo/api/servers/create", headers=state["headers"], json=command(**changes)).status_code == 422
    assert not state["writes"]


@pytest.mark.parametrize("header,value", [("cookie", "PVEAuthCookie=invalid"), ("origin", "https://evil.example"), ("x-pve-komodo-csrf", "bad")])
def test_creation_requires_pve_session_same_origin_and_csrf(integrated, header, value):
    state = integrated
    response = state["browser"].post("/pve-komodo/api/servers/create", headers={**state["headers"], header: value}, json=command())
    assert response.status_code in {401, 403} and not state["writes"]


def test_no_guest_store_still_allows_unbound_creation(integrated):
    state = integrated
    with state["make"](False) as client:
        options = client.get("/pve-komodo/api/server-create-options", headers={"cookie": COOKIE})
        assert options.json()["can_create"] and options.json()["can_link"] is False and options.json()["guests"] == []
        headers = {"cookie": COOKIE, "origin": "http://testserver", "x-pve-komodo-csrf": options.json()["csrf_token"]}
        assert client.post("/pve-komodo/api/servers/create", headers=headers, json=command(guest_type="qemu", vmid=101)).status_code == 503
        assert client.post("/pve-komodo/api/servers/create", headers=headers, json=command()).json()["created"] is True


def test_fixed_query_content_type_size_and_json_boundary(integrated):
    state = integrated; client = state["browser"]
    assert client.get("/pve-komodo/api/server-create-options?_dc=1", headers={"cookie": COOKIE}).status_code == 422
    assert client.post("/pve-komodo/api/servers/create?name=other", headers=state["headers"], json=command()).status_code == 422
    assert client.post("/pve-komodo/api/servers/create", headers=state["headers"], content="text").status_code == 415
    headers = {**state["headers"], "content-type": "application/json"}
    assert client.post("/pve-komodo/api/servers/create", headers=headers, content="{" + "x" * 8192).status_code == 413
    assert client.post("/pve-komodo/api/servers/create", headers=headers, content="{bad").status_code == 422
    assert not state["writes"]
