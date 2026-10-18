"""Standalone server aliases preserve auth, exact context and guest-link guards."""

from copy import deepcopy
import sqlite3
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from plugins.komodo.actions import KomodoActionError
from plugins.komodo.guest_links import GuestLinkStore, PveGuestClient
from plugins.komodo.komodo import KomodoClient
from app.main import Settings, create_app
from app.pve_auth import PveAuthenticator
import plugins.komodo.server_inventory_routes as inventory_routes


SERVER, OTHER, CONTAINER = "a" * 24, "b" * 24, "c" * 64
VERSION = "d" * 64
BASE = "/pve-plugins/api/komodo/servers/" + SERVER
COOKIE = "PVEAuthCookie=test-ticket"


@pytest.fixture
def direct(monkeypatch, tmp_path):
    ledger, links_path = tmp_path / "actions.sqlite3", tmp_path / "links.sqlite3"
    settings = Settings("https://pve.example:8006", komodo_url="http://core:9120", komodo_api_key="key",
                        komodo_api_secret="secret", action_state_file=str(ledger), guest_links_file=str(links_path))
    store = GuestLinkStore(str(links_path), komodo_origin=settings.komodo_url, pve_origin=settings.upstream)
    state = {"reads": [], "writes": [], "bind_on_read": False, "bind_on_write": False, "error": None, "unknown": False}

    def bind():
        return store.save(SERVER, {"type": "qemu", "vmid": 119},
            {"kind": "vm_uuid", "strength": "strong", "values": ["f39d4086-245a-471b-9d67-d4cbdc12139b"]},
            expected_revision=store.revision(SERVER), confirmed=True)

    async def authenticate(self, request):
        if request.cookies.get("PVEAuthCookie") != "test-ticket":
            raise HTTPException(401, "pve_auth_required")
        return "root@pam"

    async def no_guest_discovery(self, *args):
        raise AssertionError("standalone server access must not inspect an unrelated PVE guest")

    def read(name):
        async def operation(self, server_id, **kwargs):
            assert server_id == SERVER
            state["reads"].append((name, server_id))
            if state["bind_on_read"]:
                state["bind_on_read"] = False
                bind()
            return {"server_id": server_id, "server_name": "standalone", "version": VERSION,
                    "config": {"enabled": True}, "can_execute": True, "can_write": True, "can_rotate": True,
                    "description": "", "description_version": VERSION, "stats": [], **kwargs}
        return operation

    async def capability(self, server_id):
        state["reads"].append(("capabilities", server_id))
        return {"can_execute": state["error"] != "permission"}

    async def dispatch(name, server_id, before_write, payload):
        assert server_id == SERVER
        if state["bind_on_write"]:
            state["bind_on_write"] = False
            bind()
        await before_write()
        if state["error"]:
            raise KomodoActionError("permission_denied", 403)
        with sqlite3.connect(ledger) as db:
            assert db.execute("SELECT COUNT(*) FROM actions WHERE status IS NULL").fetchone()[0] == 1
        state["writes"].append((name, server_id, deepcopy(payload)))
        if state["unknown"]:
            raise RuntimeError("PRIVATE_UPSTREAM_FAILURE")
        return {"ok": True, "status": "completed", "operation_id": None, "error_code": None}

    async def save_options(self, server_id, *, before_write, **payload):
        return await dispatch("options", server_id, before_write, payload)

    async def save_description(self, server_id, *, before_write, **payload):
        return await dispatch("description", server_id, before_write, payload)

    async def execute(self, section, action, ids, *, before_write):
        result = await dispatch("actions", ids[0], before_write, {"section": section, "action": action})
        return {"section": section, "action": action, "results": [{"id": ids[0], **result}]}

    async def inventory_read(server_id, *, kind, **options):
        return {**await read(kind)(None, server_id), **({"containers": []} if kind == "docker" else {"stacks": [], "deployments": [], "repos": []})}

    async def inventory_write(server_id, *, before_write, section, action, targets, name, **options):
        result = await dispatch("inventory-actions", server_id, before_write,
                                {"section": section, "action": action, "targets": targets, "name": name})
        return {"section": section, "action": action, "results": [{"id": targets[0]["id"], **result}]}

    monkeypatch.setattr(PveAuthenticator, "authenticate", authenticate)
    monkeypatch.setattr(PveGuestClient, "list_guests", no_guest_discovery)
    monkeypatch.setattr(PveGuestClient, "fingerprint", no_guest_discovery)
    for name, method in [("summary", "server_summary"), ("history", "server_history"), ("options", "server_options")]:
        monkeypatch.setattr(KomodoClient, method, read(name))
    monkeypatch.setattr(KomodoClient, "server_summary_capabilities", capability)
    monkeypatch.setattr(KomodoClient, "update_server_options", save_options)
    monkeypatch.setattr(KomodoClient, "update_server_description", save_description)
    monkeypatch.setattr(KomodoClient, "perform_action", execute)
    monkeypatch.setattr(inventory_routes, "get_server_inventory", inventory_read)
    monkeypatch.setattr(inventory_routes, "perform_inventory_action", inventory_write)
    with TestClient(create_app(settings)) as client:
        response = client.get(BASE + "/summary", headers={"cookie": COOKIE})
        assert response.status_code == 200
        state.update(client=client, store=store, bind=bind, ledger=ledger,
            headers={"cookie": COOKIE, "origin": "http://testserver", "x-pve-plugins-csrf": response.json()["csrf_token"]})
        yield state
    store.close()


def command(kind, **changes):
    payload = {"server_id": SERVER, "binding_revision": 0, "request_id": str(uuid.uuid4())}
    payload.update({
        "options": {"action": "save", "base_version": VERSION, "config": {"enabled": False}},
        "description": {"description": "changed", "base_version": VERSION},
        "actions": {"action": "prune_buildx"},
        "inventory-actions": {"section": "containers", "action": "restart", "targets": [{"id": CONTAINER, "version": VERSION}]},
    }[kind])
    return {**payload, **changes}


@pytest.mark.parametrize("kind", ["summary", "history", "options", "docker", "resources"])
def test_direct_reads_have_zero_revision_private_cache_and_same_dto(direct, kind):
    response = direct["client"].get(BASE + "/" + kind, headers={"cookie": COOKIE})
    assert response.status_code == 200 and response.json()["server_id"] == SERVER
    assert response.json()["binding_revision"] == 0
    assert response.headers["cache-control"] == "private, no-store"
    assert direct["client"].get(BASE + "/" + kind).status_code == 401
    assert not direct["writes"]


@pytest.mark.parametrize("kind", ["summary", "history", "options", "docker", "resources"])
def test_direct_reads_reject_existing_binding_and_binding_added_while_reading(direct, kind):
    direct["bind_on_read"] = True
    response = direct["client"].get(BASE + "/" + kind, headers={"cookie": COOKIE})
    assert response.status_code == 409 and response.json()["detail"] == "server_link_requires_guest"
    before = len(direct["reads"])
    response = direct["client"].get(BASE + "/" + kind, headers={"cookie": COOKIE})
    assert response.status_code == 409 and len(direct["reads"]) == before


@pytest.mark.parametrize("kind", ["options", "description", "actions", "inventory-actions"])
def test_direct_writes_keep_exact_route_payload_binding_csrf_and_durable_deduplication(direct, kind):
    payload = command(kind)
    response = direct["client"].post(BASE + "/" + kind, headers=direct["headers"], json=payload)
    assert response.status_code == 200 and len(direct["writes"]) == 1
    again = direct["client"].post(BASE + "/" + kind, headers=direct["headers"], json=payload)
    assert again.status_code == 200 and again.json() == response.json() and len(direct["writes"]) == 1
    conflict = direct["client"].post(BASE + "/" + kind, headers=direct["headers"], json={**payload, "binding_revision": 1})
    assert conflict.status_code == 409
    with sqlite3.connect(direct["ledger"]) as db:
        assert db.execute("SELECT COUNT(*) FROM actions WHERE status IS NULL").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["options", "description", "actions", "inventory-actions"])
@pytest.mark.parametrize("change", [{"server_id": OTHER}, {"binding_revision": 1}, {"binding_revision": True}])
def test_direct_post_cannot_select_a_different_server_or_guest_revision(direct, kind, change):
    response = direct["client"].post(BASE + "/" + kind, headers=direct["headers"], json=command(kind, **change))
    assert response.status_code in {409, 422} and not direct["writes"]


@pytest.mark.parametrize("kind", ["options", "description", "actions", "inventory-actions"])
def test_binding_added_immediately_before_write_blocks_dispatch_and_is_receipted(direct, kind):
    direct["bind_on_write"] = True
    response = direct["client"].post(BASE + "/" + kind, headers=direct["headers"], json=command(kind))
    assert response.status_code == 409 and response.json()["detail"] == "server_link_requires_guest"
    assert not direct["writes"]
    with sqlite3.connect(direct["ledger"]) as db:
        assert db.execute("SELECT status FROM actions").fetchall() == [(409,)]


@pytest.mark.parametrize("kind", ["options", "description", "actions", "inventory-actions"])
@pytest.mark.parametrize("field,value", [("cookie", "PVEAuthCookie=other"), ("origin", "https://evil.example"), ("x-pve-plugins-csrf", "forged")])
def test_direct_writes_authenticate_before_using_komodo(direct, kind, field, value):
    direct["reads"].clear()
    response = direct["client"].post(BASE + "/" + kind, headers={**direct["headers"], field: value}, json=command(kind))
    assert response.status_code in {401, 403} and not direct["writes"] and not direct["reads"]


@pytest.mark.parametrize("kind", ["summary", "options", "docker", "resources", "terminal-services"])
def test_guest_url_cannot_forge_standalone_context(direct, kind):
    response = direct["client"].get("/pve-plugins/api/komodo/guests/server/" + SERVER + "/" + kind, headers={"cookie": COOKIE})
    assert response.status_code == 422


@pytest.mark.parametrize("kind", ["options", "summary", "history", "docker", "resources", "terminal-services"])
def test_direct_ids_and_queries_are_strict(direct, kind):
    response = direct["client"].get(BASE + "/" + kind + "?_dc=1", headers={"cookie": COOKIE})
    assert response.status_code == 422
    response = direct["client"].get(BASE.replace(SERVER, "not-an-id") + "/" + kind, headers={"cookie": COOKIE})
    assert response.status_code == 422
