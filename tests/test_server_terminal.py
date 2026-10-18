"""Guest-bound host terminal HTTP/WS lifecycle without a real network or shell."""

import asyncio
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from plugins.komodo.action_store import ActionStore
from app.csrf import CsrfProtector
from plugins.komodo.guest_links import GuestLinkError, GuestLinkStore
from plugins.komodo.komodo import KomodoClient
from plugins.komodo.server_options import GuestBoundOptionsService
import plugins.komodo.stack_terminal as module


SERVER, OTHER = "b" * 24, "c" * 24
BASE = "/pve-plugins/api/komodo/guests/qemu/119"
COOKIE = "PVEAuthCookie=test-ticket"
PRIVATE = "private-upstream-config-or-error"


@pytest.fixture
def server_terminal(monkeypatch, tmp_path):
    state = {"revision": 1, "server_id": SERVER, "binding": [], "seen": [], "created": [], "deleted": [],
             "sent": [], "sockets": [], "enabled": True, "state": "Ok", "disabled": False,
             "container_disabled": True, "specific": ["Terminal"], "level": "Read", "template": False,
             "login_reply": "LOGGED_IN", "creation_response": None, "binding_change_on_read": False}
    ledger = tmp_path / "actions.sqlite3"

    class Authenticator:
        async def authenticate(self, request):
            if request.cookies.get("PVEAuthCookie") not in ("test-ticket", "renewed-ticket"):
                raise HTTPException(401, "pve_auth_required")
            return "root@pam"

    async def resolve(self, request, guest_type, vmid, *, expected_server_id=None, expected_revision=None):
        state["binding"].append((guest_type, vmid, expected_server_id, expected_revision))
        if guest_type != "qemu" or vmid != 119:
            raise GuestLinkError("guest_not_linked", 409)
        if ((expected_server_id is not None and expected_server_id != state["server_id"])
                or (expected_revision is not None and expected_revision != state["revision"])):
            raise GuestLinkError("guest_link_revision_conflict", 409)
        return {"server_id": state["server_id"], "binding_revision": state["revision"],
                "guest_type": guest_type, "vmid": vmid}

    def core(request):
        assert request.headers["x-api-key"] == "test-key" and request.headers["x-api-secret"] == "test-secret"
        assert "cookie" not in request.headers
        if request.url.path == "/user":
            state["seen"].append("user")
            return httpx.Response(200, json={"enabled": True, "username": "integration", "private": PRIVATE})
        payload = json.loads(request.content)
        variant, params = payload["type"], payload["params"]
        state["seen"].append(variant)
        if request.url.path == "/write":
            assert variant in {"CreateTerminal", "DeleteTerminal"}
            assert params["target"] == {"type": "Server", "params": {"server": SERVER}}
            if variant == "CreateTerminal":
                assert params["recreate"] == "Never" and params["mode"] == "exec"
                assert params["name"].startswith("pve-komodo-")
                with sqlite3.connect(ledger) as db:
                    assert db.execute("SELECT COUNT(*) FROM actions WHERE status IS NULL").fetchone()[0] == 1
                state["created"].append(deepcopy(params))
                return state["creation_response"] or httpx.Response(200, json={"name": params["name"],
                    "target": params["target"], "command": PRIVATE})
            assert params["terminal"] in [entry["name"] for entry in state["created"]]
            state["deleted"].append(deepcopy(params))
            return httpx.Response(200, json={})
        assert request.url.path == "/read"
        if variant == "GetPermission":
            assert params == {"target": {"type": "Server", "id": state["server_id"]}}
            result = {"level": state["level"], "specific": state["specific"]}
        elif variant == "GetServer":
            assert params == {"server": state["server_id"]}
            result = {"_id": {"$oid": state["server_id"]}, "name": "preview-host", "template": state["template"],
                      "config": {"enabled": state["enabled"], "passkey": PRIVATE}, "info": {"private": PRIVATE}}
        else:
            assert variant == "ListServers"
            assert params == {"query": {"names": [state["server_id"]], "templates": "Include"}, "limit": 0}
            result = [{"id": state["server_id"], "info": {"state": state["state"],
                "terminals_disabled": state["disabled"], "container_terminals_disabled": state["container_disabled"]}}]
            if state["binding_change_on_read"]:
                state["revision"] += 1
        return httpx.Response(200, json=result)

    class Socket:
        def __init__(self):
            self.queue, self.closed = asyncio.Queue(), False

        async def send(self, data):
            state["sent"].append(data)
            if isinstance(data, str):
                assert json.loads(data) == {"type": "ApiKeys", "params": {"key": "test-key", "secret": "test-secret"}}
                await self.queue.put(state["login_reply"])
                await self.queue.put(b"fake-host-prompt> ")
            elif data.endswith(b"\x01"):
                await self.queue.put(b"echo:" + data[:-1])
            else:
                assert data.endswith(b"\xff")

        async def recv(self):
            return await self.queue.get()

        async def close(self):
            self.closed = True

    async def connect(url, verify):
        assert url.startswith("ws://core.example:9120/ws/terminal?")
        assert "target%5B" not in url and "target[type]=Server" in url
        query = parse_qs(url.split("?", 1)[1])
        assert set(query) == {"target[type]", "target[params][server]", "terminal"}
        assert query["target[params][server]"] == [SERVER]
        assert query["terminal"][0] in [entry["name"] for entry in state["created"]]
        socket = Socket()
        state["sockets"].append(socket)
        return socket

    monkeypatch.setattr(GuestBoundOptionsService, "resolve", resolve)
    app = FastAPI()
    config = SimpleNamespace(upstream="https://pve.example:8006", komodo_url="http://core.example:9120", tls_context=lambda: False)
    guest_store = GuestLinkStore(str(tmp_path / "links.sqlite3"), komodo_origin=config.komodo_url, pve_origin=config.upstream)
    manager = module.register_server_terminal_routes(app, config, Authenticator(),
        KomodoClient(config.komodo_url, "test-key", "test-secret", transport=httpx.MockTransport(core)),
        CsrfProtector(), guest_store, ActionStore(str(ledger)), ws_connect=connect)
    with TestClient(app) as client:
        state.update(client=client, manager=manager, ledger=ledger, guest_store=guest_store)
        yield state
    assert not manager.active and not manager.cleanup_tasks
    guest_store.close()


def read(state):
    response = state["client"].get(BASE + "/terminal-services", headers={"cookie": COOKIE})
    assert response.status_code == 200
    for secret in (PRIVATE, "test-key", "test-secret", "test-ticket"):
        assert secret not in response.text
    return response.json()


def issue(state, *, shell="default", command=None, request_id=None):
    data = read(state)
    body = {"server_id": SERVER, "binding_revision": 1, "shell": shell,
            "request_id": request_id or str(uuid.uuid4())}
    if command is not None:
        body["command"] = command
    headers = {"cookie": COOKIE, "origin": "http://testserver", "x-pve-plugins-csrf": data["csrf_token"]}
    result = state["client"].post(BASE + "/terminals", headers=headers, json=body)
    assert result.status_code == 200
    return result.json(), data["csrf_token"], body


def open_socket(state, grant, token, *, base=BASE, cookie=COOKIE):
    context = state["client"].websocket_connect(base + "/terminals/ws", headers={"cookie": cookie, "origin": "http://testserver"})
    socket = context.__enter__()
    socket.send_json({"type": "open", "grant": grant["grant"], "csrf_token": token})
    return context, socket


def test_snapshot_grant_is_read_only_and_default_resolves_on_periphery(server_terminal):
    data = read(server_terminal)
    assert data["server_id"] == SERVER and data["binding_revision"] == 1 and data["server_name"] == "preview-host"
    assert data["shells"] == ["default", "sh", "bash", "custom"]
    assert all(data["options"].values()) and data["can_open"]
    assert server_terminal["binding"] == [("qemu", 119, None, None), ("qemu", 119, SERVER, 1)]
    grant, _, _ = issue(server_terminal)
    assert set(grant) == {"grant", "session_id", "expires_in"}
    assert server_terminal["created"] == server_terminal["deleted"] == []
    assert set(server_terminal["seen"]) == {"user", "GetPermission", "GetServer", "ListServers"}


@pytest.mark.parametrize("shell,command,expected", [("default", None, None), ("sh", None, "sh"),
    ("bash", None, "bash"), ("custom", "sh -l", "sh -l")])
def test_shared_forwarding_uses_server_target_and_deletes_only_own_pty(server_terminal, shell, command, expected):
    grant, token, _ = issue(server_terminal, shell=shell, command=command)
    context, socket = open_socket(server_terminal, grant, token)
    try:
        assert socket.receive_json() == {"type": "ready", "session_id": grant["session_id"]}
        assert socket.receive_bytes() == b"fake-host-prompt> "
        socket.send_json({"type": "data", "data": "你好\x03"})
        assert socket.receive_bytes() == "echo:你好\x03".encode()
        socket.send_json({"type": "resize", "rows": 45, "cols": 140})
        socket.send_json({"type": "close"})
        assert socket.receive()["type"] == "websocket.close"
    finally:
        context.__exit__(None, None, None)
    assert len(server_terminal["created"]) == len(server_terminal["deleted"]) == 1
    assert server_terminal["created"][0]["command"] == expected
    assert all(socket.closed for socket in server_terminal["sockets"])
    with sqlite3.connect(server_terminal["ledger"]) as db:
        assert db.execute("SELECT status,result FROM actions").fetchall() == [(200, '{"detail":"terminal_session_requested"}')]


@pytest.mark.parametrize("change", ["rebind", "revision", "permission", "disabled", "offline", "template", "enabled"])
def test_ws_revalidates_binding_and_host_policy_after_grant(server_terminal, change):
    grant, token, _ = issue(server_terminal)
    if change == "rebind":
        server_terminal["server_id"] = OTHER
    elif change == "revision":
        server_terminal["revision"] += 1
    elif change == "permission":
        server_terminal["specific"] = []
    elif change == "offline":
        server_terminal["state"] = "NotOk"
    elif change == "enabled":
        server_terminal["enabled"] = False
    else:
        server_terminal[change] = True
    context, socket = open_socket(server_terminal, grant, token)
    assert socket.receive_json()["type"] == "error"
    context.__exit__(None, None, None)
    assert not server_terminal["created"] and not server_terminal["deleted"]


@pytest.mark.parametrize("change", ["revision", "permission"])
def test_active_session_rechecks_and_cleans_own_host_pty(server_terminal, monkeypatch, change):
    monkeypatch.setattr(module, "_TICK_SECONDS", 0.01)
    monkeypatch.setattr(module, "_REVALIDATE_SECONDS", 0.02)
    grant, token, _ = issue(server_terminal)
    context, socket = open_socket(server_terminal, grant, token)
    assert socket.receive_json()["type"] == "ready"
    socket.receive_bytes()
    if change == "revision":
        server_terminal["revision"] += 1
    else:
        server_terminal["specific"] = []
    assert socket.receive_json()["type"] == "error"
    context.__exit__(None, None, None)
    assert len(server_terminal["created"]) == len(server_terminal["deleted"]) == 1


def test_binding_changed_during_preflight_fails_closed(server_terminal):
    server_terminal["binding_change_on_read"] = True
    response = server_terminal["client"].get(BASE + "/terminal-services", headers={"cookie": COOKIE})
    assert response.status_code == 409 and response.json()["detail"] == "guest_link_revision_conflict"


@pytest.mark.parametrize("change", [{"binding_revision": True}, {"binding_revision": 0}, {"server_id": OTHER},
    {"binding_revision": 2}, {"shell": "attach"}, {"shell": "sh", "command": "bash"},
    {"shell": "custom"}, {"shell": "custom", "command": "\n"}, {"unknown": "value"},
    {"target": {"type": "Container"}}, {"request_id": str(uuid.uuid1())}])
def test_fixed_command_schema_and_binding_do_not_allow_arbitrary_targets(server_terminal, change):
    data = read(server_terminal)
    body = {"server_id": SERVER, "binding_revision": 1, "shell": "default", "request_id": str(uuid.uuid4()), **change}
    response = server_terminal["client"].post(BASE + "/terminals", json=body,
        headers={"cookie": COOKIE, "origin": "http://testserver", "x-pve-plugins-csrf": data["csrf_token"]})
    assert response.status_code in {409, 422}
    assert not server_terminal["created"]


@pytest.mark.parametrize("field,value", [("cookie", "PVEAuthCookie=unknown"), ("origin", "https://evil.example"),
    ("x-pve-plugins-csrf", "forged")])
def test_authorization_fails_before_komodo_requests(server_terminal, field, value):
    data = read(server_terminal)
    server_terminal["seen"].clear()
    headers = {"cookie": COOKIE, "origin": "http://testserver", "x-pve-plugins-csrf": data["csrf_token"], field: value}
    response = server_terminal["client"].post(BASE + "/terminals", json={}, headers=headers)
    assert response.status_code in {401, 403} and not server_terminal["seen"]


@pytest.mark.parametrize("base", [BASE.replace("qemu", "lxc"), BASE.replace("119", "120")])
def test_grants_cannot_cross_guest_context(server_terminal, base):
    grant, token, _ = issue(server_terminal)
    context, socket = open_socket(server_terminal, grant, token, base=base)
    assert socket.receive_json() == {"type": "error", "code": "terminal_grant_invalid"}
    context.__exit__(None, None, None)
    assert not server_terminal["created"]


def test_disconnect_cleanup_no_replay_and_no_retry_after_unknown_login(server_terminal):
    grant, token, body = issue(server_terminal)
    server_terminal["login_reply"] = PRIVATE
    context, socket = open_socket(server_terminal, grant, token)
    result = socket.receive_json()
    assert result["type"] == "error" and PRIVATE not in repr(result)
    context.__exit__(None, None, None)
    grant, token, _ = issue(server_terminal, request_id=body["request_id"])
    context, socket = open_socket(server_terminal, grant, token)
    assert socket.receive_json() == {"type": "error", "code": "terminal_already_requested"}
    context.__exit__(None, None, None)
    assert len(server_terminal["created"]) == len(server_terminal["deleted"]) == 1


@pytest.mark.parametrize("suffix", ["?_dc=1", "?server_id=" + SERVER])
def test_route_query_is_rejected_before_komodo(server_terminal, suffix):
    response = server_terminal["client"].get(BASE + "/terminal-services" + suffix, headers={"cookie": COOKIE})
    assert response.status_code == 422 and not server_terminal["seen"]


def test_ws_origin_required_before_auth_and_accept(server_terminal):
    with pytest.raises(WebSocketDisconnect):
        with server_terminal["client"].websocket_connect(BASE + "/terminals/ws", headers={"cookie": COOKIE}):
            pytest.fail("missing-origin socket accepted")
    assert not server_terminal["seen"]


DIRECT_BASE = "/pve-plugins/api/komodo/servers/" + SERVER


def direct_issue(state, **changes):
    response = state["client"].get(DIRECT_BASE + "/terminal-services", headers={"cookie": COOKIE})
    assert response.status_code == 200
    data = response.json()
    assert data["binding_revision"] == 0 and data["server_id"] == SERVER
    payload = {"server_id": SERVER, "binding_revision": 0, "shell": "default", "request_id": str(uuid.uuid4()), **changes}
    result = state["client"].post(DIRECT_BASE + "/terminals", json=payload,
        headers={"cookie": COOKIE, "origin": "http://testserver", "x-pve-plugins-csrf": data["csrf_token"]})
    return result, data["csrf_token"]


def bind_terminal_server(state):
    store = state["guest_store"]
    return store.save(SERVER, {"type": "qemu", "vmid": 119},
        {"kind": "vm_uuid", "strength": "strong", "values": ["f39d4086-245a-471b-9d67-d4cbdc12139b"]},
        expected_revision=store.revision(SERVER), confirmed=True)


def test_direct_host_terminal_reuses_default_command_and_precise_cleanup(server_terminal):
    response, token = direct_issue(server_terminal)
    assert response.status_code == 200 and not server_terminal["created"]
    assert server_terminal["binding"] == [], "standalone terminal never inspects an unrelated PVE guest"
    grant = response.json()
    context, socket = open_socket(server_terminal, grant, token, base=DIRECT_BASE)
    assert socket.receive_json() == {"type": "ready", "session_id": grant["session_id"]}
    assert socket.receive_bytes() == b"fake-host-prompt> "
    socket.send_json({"type": "close"})
    assert socket.receive()["type"] == "websocket.close"
    context.__exit__(None, None, None)
    assert len(server_terminal["created"]) == len(server_terminal["deleted"]) == 1
    assert server_terminal["created"][0]["command"] is None
    assert server_terminal["binding"] == []


@pytest.mark.parametrize("changes", [{"server_id": OTHER}, {"binding_revision": 1}, {"binding_revision": True}])
def test_direct_terminal_post_requires_matching_route_server_and_zero_revision(server_terminal, changes):
    response, _ = direct_issue(server_terminal, **changes)
    assert response.status_code in {409, 422} and not server_terminal["created"]


def test_direct_terminal_cannot_bypass_a_new_guest_binding_after_grant(server_terminal):
    response, token = direct_issue(server_terminal)
    assert response.status_code == 200
    bind_terminal_server(server_terminal)
    context, socket = open_socket(server_terminal, response.json(), token, base=DIRECT_BASE)
    assert socket.receive_json() == {"type": "error", "code": "server_link_requires_guest"}
    context.__exit__(None, None, None)
    assert not server_terminal["created"]
    denied = server_terminal["client"].get(DIRECT_BASE + "/terminal-services", headers={"cookie": COOKIE})
    assert denied.status_code == 409 and denied.json()["detail"] == "server_link_requires_guest"


def test_active_direct_terminal_stops_and_cleans_pty_when_server_becomes_guest_bound(server_terminal, monkeypatch):
    monkeypatch.setattr(module, "_TICK_SECONDS", 0.01)
    monkeypatch.setattr(module, "_REVALIDATE_SECONDS", 0.02)
    response, token = direct_issue(server_terminal)
    context, socket = open_socket(server_terminal, response.json(), token, base=DIRECT_BASE)
    assert socket.receive_json()["type"] == "ready"
    socket.receive_bytes()
    bind_terminal_server(server_terminal)
    assert socket.receive_json() == {"type": "error", "code": "server_link_requires_guest"}
    context.__exit__(None, None, None)
    assert len(server_terminal["created"]) == len(server_terminal["deleted"]) == 1


def test_direct_grant_is_not_reusable_on_a_guest_path(server_terminal):
    response, token = direct_issue(server_terminal)
    context, socket = open_socket(server_terminal, response.json(), token, base=BASE)
    assert socket.receive_json() == {"type": "error", "code": "terminal_grant_invalid"}
    context.__exit__(None, None, None)
    assert not server_terminal["created"]
