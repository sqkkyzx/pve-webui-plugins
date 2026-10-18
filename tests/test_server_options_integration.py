"""Full API chain using real auth, associations, Komodo client and ledger.

Only HTTP transports are mocked. PVE receives read-only discovery plus ticket
verification; Komodo mutations are confined to this in-memory test transport.
"""

from copy import deepcopy
import json
import sqlite3
from urllib.parse import parse_qs, quote
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from plugins.komodo.guest_links import GuestLinkStore
from app.main import Settings, create_app
from plugins.komodo.server_options import BOOL_FIELDS, NUMBER_FIELDS


PVE, KOMODO = "https://pve.example:8006", "http://core:9120"
SERVER = "a" * 24
TICKET = "PVE:root@pam:1234ABCD::signature+value="
COOKIE = "PVEAuthCookie=" + quote(TICKET, safe="")
VM_UUID = "9f376f89-d6b7-4dc8-84a4-f4769dc9b9bb"
KEY, SECRET = "integration-test-key", "integration-test-secret"
PASSKEY, PRIVATE_KEY = "private-passkey-value", "private-key-value"
PATH = "/pve-plugins/api/komodo/guests/qemu/101/options"


@pytest.fixture
def integrated(tmp_path):
    config = {field: True for field in BOOL_FIELDS}
    config.update({field: 95.5 for field in NUMBER_FIELDS})
    config.update(address="wss://periphery.example:8120", external_address="docker.example",
                  region="east", ignore_mounts=["/dev/loop0"], maintenance_windows=[], passkey=PASSKEY)
    state = {
        "uuid": VM_UUID, "seen": [], "writes": [], "preflight_change": None,
        "server": {"_id": {"$oid": SERVER}, "name": "docker-guest", "template": False,
                   "updated_at": 1792256400123, "config": config,
                   "info": {"public_key": "configured-public-key", "private_key": PRIVATE_KEY}},
    }
    links_file, actions_file = tmp_path / "guest-links.sqlite3", tmp_path / "actions.sqlite3"
    state.update(links_file=links_file, actions_file=actions_file)

    def pve(request):
        state["seen"].append(("pve", request.method, request.url.path))
        assert str(request.url).startswith(PVE + "/")
        assert request.headers["cookie"] == COOKIE
        assert "x-api-key" not in request.headers and "x-api-secret" not in request.headers
        assert "authorization" not in request.headers
        if request.url.path == "/api2/json/access/permissions":
            assert request.method == "GET" and request.url.query == b""
            return httpx.Response(200, json={"data": {"/": {"Sys.Audit": 1}}}, headers={
                "set-cookie": "unexpected-upstream-cookie=must-not-replay; Path=/",
            })
        if request.url.path == "/api2/json/access/ticket":
            assert request.method == "POST"
            assert parse_qs(request.content.decode()) == {
                "username": ["root@pam"], "password": [TICKET], "path": ["/"], "privs": ["Sys.Audit"],
            }
            return httpx.Response(200, json={"data": {"username": "root@pam"}})
        assert request.method == "GET", "PVE guest configuration must remain read-only"
        if request.url.path == "/api2/json/cluster/resources":
            assert dict(request.url.params) == {"type": "vm"}
            return httpx.Response(200, json={"data": [{
                "type": "qemu", "vmid": 101, "node": "pve-one", "name": "docker-guest", "status": "running",
            }]})
        assert request.url.path == "/api2/json/nodes/pve-one/qemu/101/config"
        return httpx.Response(200, json={"data": {"smbios1": "uuid=" + state["uuid"], "password": SECRET, "sshkeys": PRIVATE_KEY}})

    def komodo(request):
        state["seen"].append(("komodo", request.method, request.url.path))
        assert str(request.url).startswith(KOMODO + "/")
        assert request.headers["x-api-key"] == KEY and request.headers["x-api-secret"] == SECRET
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if request.url.path == "/user":
            assert request.method == "GET"
            return httpx.Response(200, json={"username": "integration", "enabled": True}, headers={
                "set-cookie": "PVEAuthCookie=must-not-replay; Path=/",
            })
        assert request.method == "POST"
        payload = json.loads(request.content)
        variant = payload["type"]
        if request.url.path == "/write":
            # This is only a MockTransport callback. No actual endpoint can be
            # reached from these clients, including the configured fake hosts.
            assert payload == {"type": "UpdateServer", "params": {"id": SERVER, "config": {"region": "west"}}}
            with sqlite3.connect(actions_file) as db:
                assert db.execute("SELECT COUNT(*) FROM actions WHERE status IS NULL").fetchone()[0] == 1
            state["writes"].append(payload)
            state["server"]["config"].update(payload["params"]["config"])
            state["server"]["updated_at"] += 1
            return httpx.Response(200, json=deepcopy(state["server"]))
        assert request.url.path == "/read"
        if variant == "ListServers":
            assert payload["params"] in [
                {"query": {"templates": "Include"}, "limit": 0},
                {"query": {"names": [SERVER], "templates": "Include"}, "limit": 0},
            ]
            response = [{"id": SERVER, "name": "docker-guest", "template": False,
                         "info": {"state": "Ok", "public_key": "observed-cache-key"}}]
        elif variant == "GetServer":
            assert payload["params"] == {"server": SERVER}
            change = state["preflight_change"]
            state["preflight_change"] = None
            if change == "unlink":
                # A separate real store connection emulates another browser
                # unlinking while Komodo preflight is in flight.
                database = GuestLinkStore(str(links_file), komodo_origin=KOMODO, pve_origin=PVE)
                database.unlink(SERVER, expected_revision=1)
                database.close()
            elif change == "identity":
                state["uuid"] = "ec5c382f-b7d3-4fa2-9d37-08e72fc3d826"
            elif change == "config":
                state["server"]["config"]["region"] = "changed-in-another-client"
                state["server"]["updated_at"] += 1
            response = deepcopy(state["server"])
        elif variant == "GetCoreInfo":
            assert payload["params"] == {}
            response = {"ui_write_disabled": False, "webhook_base_url": SECRET}
        elif variant == "GetPermission":
            assert payload["params"] == {"target": {"type": "Server", "id": SERVER}}
            response = {"level": "Write", "specific": []}
        else:
            pytest.fail("unexpected or non-allowlisted RPC")
        return httpx.Response(200, json=response, headers={"set-cookie": "private-cookie=must-not-replay; Path=/"})

    settings = Settings(PVE, komodo_url=KOMODO, komodo_api_key=KEY, komodo_api_secret=SECRET,
                        action_state_file=str(actions_file), guest_links_file=str(links_file))
    application = create_app(settings, pve_auth_transport=httpx.MockTransport(pve),
                             komodo_transport=httpx.MockTransport(komodo))
    with TestClient(application) as browser:
        state["browser"] = browser
        yield state


def assert_private(response):
    for value in (KEY, SECRET, PASSKEY, PRIVATE_KEY):
        assert value not in response.text


def bind_and_open(state):
    browser = state["browser"]
    listing = browser.get("/pve-plugins/api/komodo/guest-links", headers={"cookie": COOKIE})
    assert listing.status_code == 200
    assert_private(listing)
    headers = {"cookie": COOKIE, "origin": "http://testserver", "x-pve-plugins-csrf": listing.json()["csrf_token"]}
    binding = browser.post("/pve-plugins/api/komodo/guest-links", headers=headers, json={
        "server_id": SERVER, "guest_type": "qemu", "vmid": 101, "expected_revision": 0,
    })
    assert binding.status_code == 200
    assert binding.json()["link"]["status"] == "linked"
    assert_private(binding)
    opened = browser.get(PATH, headers={"cookie": COOKIE})
    assert opened.status_code == 200
    assert_private(opened)
    data = opened.json()
    assert data["binding_revision"] == 1 and data["can_write"] and data["can_rotate"]
    assert data["public_key"] == "configured-public-key"
    headers["x-pve-plugins-csrf"] = data["csrf_token"]
    command = {"server_id": SERVER, "binding_revision": data["binding_revision"], "base_version": data["version"],
               "request_id": str(uuid.uuid4()), "action": "save", "config": {"region": "west"}}
    return browser, headers, command


def test_real_binding_auth_csrf_client_save_and_durable_duplicate_receipt(integrated):
    browser, headers, command = bind_and_open(integrated)
    first = browser.post(PATH, headers=headers, json=command)
    assert first.status_code == 200 and first.json() == {
        "ok": True, "status": "completed", "operation_id": None, "error_code": None,
    }
    assert_private(first)
    again = browser.post(PATH, headers=headers, json=command)
    assert again.status_code == 200 and again.json() == first.json()
    assert len(integrated["writes"]) == 1
    refreshed = browser.get(PATH, headers={"cookie": COOKIE})
    assert refreshed.status_code == 200 and refreshed.json()["config"]["region"] == "west"
    assert refreshed.json()["version"] != command["base_version"]
    assert_private(refreshed)
    with sqlite3.connect(integrated["actions_file"]) as db:
        rows = db.execute("SELECT fingerprint,status,result FROM actions").fetchall()
    assert len(rows) == 1 and rows[0][1] == 200
    assert json.loads(rows[0][2]) == first.json()
    assert "west" not in repr(rows)


@pytest.mark.parametrize("change,code", [
    ("unlink", "guest_not_linked"), ("identity", "guest_identity_changed"), ("config", "server_options_conflict"),
])
def test_real_preflight_rechecks_mapping_identity_and_config_version_before_write(integrated, change, code):
    browser, headers, command = bind_and_open(integrated)
    integrated["preflight_change"] = change
    response = browser.post(PATH, headers=headers, json=command)
    assert response.status_code == 409 and response.json() == {"detail": code}
    assert_private(response)
    assert integrated["writes"] == []
    assert not any(source == "komodo" and path == "/write" for source, method, path in integrated["seen"])
    with sqlite3.connect(integrated["actions_file"]) as db:
        rows = db.execute("SELECT status,result FROM actions").fetchall()
    assert rows == [(409, json.dumps({"detail": code}, separators=(",", ":")))]
