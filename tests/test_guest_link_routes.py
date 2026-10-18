import json
from urllib.parse import quote

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app


SERVER = "a" * 24
OTHER = "b" * 24
TICKET = "PVE:root@pam:1234ABCD::signature+value="
COOKIE = "PVEAuthCookie=" + quote(TICKET, safe="")
UUID = "9f376f89-d6b7-4dc8-84a4-f4769dc9b9bb"
SECRET = "private-config-secret"


def fixture(tmp_path, *, configured=True):
    state = {
        "servers": [{"id": SERVER, "name": "docker-guest", "template": False},
                    {"id": OTHER, "name": "other-guest", "template": False}],
        "guests": [{"type": "qemu", "vmid": 101, "node": "pve-one", "name": "docker-guest", "status": "running"},
                   {"type": "lxc", "vmid": 102, "node": "pve-one", "name": "other-guest", "status": "stopped"}],
        "uuid": UUID, "config_status": 200, "seen": [], "komodo_status": 200,
    }

    def pve(request):
        state["seen"].append(("pve", request.method, request.url.path))
        assert request.headers["cookie"] == COOKIE
        assert "x-api-key" not in request.headers and "x-api-secret" not in request.headers
        assert str(request.url).startswith("https://pve.example:8006/")
        if request.url.path == "/api2/json/access/permissions":
            return httpx.Response(200, json={"data": {"/": {"Sys.Audit": 1}}})
        if request.url.path == "/api2/json/access/ticket":
            assert request.method == "POST"
            return httpx.Response(200, json={"data": {"username": "root@pam"}})
        assert request.method == "GET", "PVE guest configuration must never be modified"
        if request.url.path == "/api2/json/cluster/resources":
            assert dict(request.url.params) == {"type": "vm"}
            return httpx.Response(200, json={"data": state["guests"]})
        assert request.url.path in {
            "/api2/json/nodes/" + node + "/" + kind + "/" + str(vmid) + "/config"
            for node in ["pve-one", "pve-two"] for kind, vmid in [("qemu", 101), ("lxc", 102)]
        }
        return httpx.Response(state["config_status"], json={"data": {
            "smbios1": "uuid=" + state["uuid"], "net0": "name=eth0,hwaddr=02:AB:CD:00:00:01",
            "password": SECRET, "sshkeys": SECRET, "description": SECRET,
        }})

    def komodo(request):
        state["seen"].append(("komodo", request.method, request.url.path))
        assert "cookie" not in request.headers
        assert request.headers["x-api-key"] == "key" and request.headers["x-api-secret"] == "secret"
        if request.url.path == "/user":
            return httpx.Response(200, json={"username": "integration", "enabled": True})
        assert request.method == "POST" and request.url.path == "/read"
        assert json.loads(request.content) == {"type": "ListServers", "params": {
            "query": {"templates": "Include"}, "limit": 0,
        }}
        return httpx.Response(state["komodo_status"], json=state["servers"])

    settings = Settings(
        "https://pve.example:8006", komodo_url="http://komodo.example:9120",
        komodo_api_key="key", komodo_api_secret="secret",
        guest_links_file=str(tmp_path / "guest-links.sqlite3") if configured else None,
    )
    def client():
        return TestClient(create_app(settings, pve_auth_transport=httpx.MockTransport(pve),
                                    komodo_transport=httpx.MockTransport(komodo)))
    return client, state


def credentials(client):
    response = client.get("/pve-plugins/api/komodo/guest-links", headers={"cookie": COOKIE})
    assert response.status_code == 200
    assert SECRET not in response.text
    return {"cookie": COOKIE, "origin": "http://testserver", "x-pve-plugins-csrf": response.json()["csrf_token"]}


def binding(**changes):
    return {"server_id": SERVER, "guest_type": "qemu", "vmid": 101, "expected_revision": 0, **changes}


def test_bind_list_migrate_unlink_restart_and_revision_aba(tmp_path):
    factory, state = fixture(tmp_path)
    client = factory()
    headers = credentials(client)
    response = client.post("/pve-plugins/api/komodo/guest-links", headers=headers, json=binding())
    assert response.status_code == 200
    link = response.json()["link"]
    assert (link["status"], link["can_navigate"], link["revision"], link["guest_name"]) == ("linked", True, 1, "docker-guest")
    state["guests"][0].update(node="pve-two", name="renamed")
    restarted = factory()
    data = restarted.get("/pve-plugins/api/komodo/guest-links", headers={"cookie": COOKIE}).json()
    assert data["links"][0]["node"] == "pve-two" and data["links"][0]["status"] == "linked"
    assert data["links"][0]["guest_name"] == "renamed"
    assert SECRET not in json.dumps(data) and "values" not in data["links"][0]
    headers = credentials(restarted)
    assert restarted.post("/pve-plugins/api/komodo/guest-links/unlink", headers=headers, json={"server_id": SERVER, "expected_revision": 1}).json() == {"removed": True, "revision": 2}
    data = restarted.get("/pve-plugins/api/komodo/guest-links", headers={"cookie": COOKIE}).json()
    assert data["links"] == [] and data["servers"][0]["link_revision"] == 2
    assert restarted.post("/pve-plugins/api/komodo/guest-links", headers=headers, json=binding()).status_code == 409
    assert restarted.post("/pve-plugins/api/komodo/guest-links", headers=headers, json=binding(expected_revision=2)).json()["link"]["revision"] == 3


def test_auth_csrf_origin_and_schema_are_checked_before_guest_or_komodo_reads(tmp_path):
    factory, state = fixture(tmp_path)
    client = factory()
    assert client.get("/pve-plugins/api/komodo/guest-links").status_code == 401
    assert client.post("/pve-plugins/api/komodo/guest-links", json=binding()).status_code == 401
    assert state["seen"] == []
    headers = credentials(client)
    state["seen"].clear()
    for override in [{"x-pve-plugins-csrf": "wrong"}, {"origin": "https://evil.invalid"}]:
        response = client.post("/pve-plugins/api/komodo/guest-links", headers={**headers, **override}, json=binding())
        assert response.status_code == 403
    assert not any(source == "komodo" or path.endswith("/config") for source, method, path in state["seen"])
    for payload in [binding(vmid=True), binding(vmid="101"), binding(guest_type="../qemu"), binding(expected_revision=-1), binding(node="pve-one")]:
        assert client.post("/pve-plugins/api/komodo/guest-links", headers=headers, json=payload).status_code == 422
    response = client.get("/pve-plugins/api/komodo/guest-links?node=evil", headers={"cookie": COOKIE})
    assert response.status_code == 422
    assert response.headers["cache-control"] == "private, no-store"


def test_target_conflicts_missing_templates_and_local_orphan_cleanup(tmp_path):
    factory, state = fixture(tmp_path)
    client = factory()
    headers = credentials(client)
    assert client.post("/pve-plugins/api/komodo/guest-links", headers=headers, json=binding()).status_code == 200
    conflict = client.post("/pve-plugins/api/komodo/guest-links", headers=headers, json=binding(server_id=OTHER))
    assert conflict.status_code == 409 and conflict.json()["detail"] == "pve_guest_already_linked"
    state["servers"] = [state["servers"][1]]
    link = client.get("/pve-plugins/api/komodo/guest-links", headers={"cookie": COOKIE}).json()["links"][0]
    assert link["server_name"] is None and link["server_unavailable"] and not link["can_navigate"]
    state["komodo_status"] = 503
    assert client.post("/pve-plugins/api/komodo/guest-links/unlink", headers=headers, json={"server_id": SERVER, "expected_revision": 1}).status_code == 200
    state["komodo_status"] = 200
    state["servers"][0]["template"] = True
    assert client.post("/pve-plugins/api/komodo/guest-links", headers=headers, json=binding(server_id=OTHER)).status_code == 409
    state["servers"][0]["template"] = False
    state["guests"][0]["template"] = 1
    assert client.post("/pve-plugins/api/komodo/guest-links", headers=headers, json=binding(server_id=OTHER)).status_code == 409


@pytest.mark.parametrize("change,status", [("uuid", "needs_confirmation"), ("missing", "missing"), ("config_error", "unavailable")])
def test_identity_and_visibility_changes_preserve_mapping_but_disable_navigation(tmp_path, change, status):
    factory, state = fixture(tmp_path)
    client = factory()
    headers = credentials(client)
    assert client.post("/pve-plugins/api/komodo/guest-links", headers=headers, json=binding()).status_code == 200
    if change == "uuid": state["uuid"] = "ec5c382f-b7d3-4fa2-9d37-08e72fc3d826"
    if change == "missing": state["guests"] = state["guests"][1:]
    if change == "config_error": state["config_status"] = 403
    response = client.get("/pve-plugins/api/komodo/guest-links", headers={"cookie": COOKIE})
    assert response.status_code == 200 and SECRET not in response.text
    link = response.json()["links"][0]
    assert link["status"] == status and not link["can_navigate"] and link["revision"] == 1


def test_ct_binding_has_weak_identity_and_cannot_be_fabricated_by_client(tmp_path):
    factory, state = fixture(tmp_path)
    client = factory()
    headers = credentials(client)
    response = client.post("/pve-plugins/api/komodo/guest-links", headers=headers, json=binding(guest_type="lxc", vmid=102))
    assert response.status_code == 200
    assert response.json()["link"]["identity_strength"] == "weak"
    assert "02:ab:cd" not in response.text


def test_unconfigured_store_never_silently_creates_ephemeral_links(tmp_path):
    factory, state = fixture(tmp_path, configured=False)
    response = factory().get("/pve-plugins/api/komodo/guest-links", headers={"cookie": COOKIE})
    assert response.status_code == 503 and response.json()["detail"] == "guest_link_store_not_configured"
    assert not any(source == "komodo" for source, method, path in state["seen"])
