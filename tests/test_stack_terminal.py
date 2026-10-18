"""Terminal HTTP/PVE/CSRF/ledger/WS lifecycle with no real network or shell."""

import asyncio
from contextlib import ExitStack
from copy import deepcopy
import json
import sqlite3
import ssl
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, quote
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import httpx
import pytest

from plugins.komodo.action_store import ActionStore
from app.csrf import CsrfProtector
from plugins.komodo.komodo import KomodoClient
from app.pve_auth import PveAuthenticator
import plugins.komodo.stack_terminal as module


PVE, CORE = "https://pve.example:8006", "http://core.example:9120"
STACK, SERVER, OTHER = "a" * 24, "b" * 24, "c" * 24
CONTAINER, REPLACEMENT = "d" * 64, "e" * 64
KEY, SECRET, PRIVATE = "terminal-key", "terminal-secret", "private-config-and-upstream-error"
TICKET = "PVE:root@pam:1234ABCD::signature+value="
RENEWED = "PVE:root@pam:1234ABCE::another-signature="
COOKIE = "PVEAuthCookie=" + quote(TICKET, safe="")
BASE = "/pve-komodo/api/stacks/" + STACK
CONTAINER_BASE = "/pve-komodo/api/servers/" + SERVER + "/containers/" + CONTAINER


@pytest.fixture
def terminal(tmp_path):
    state = {"seen": [], "created": [], "deleted": [], "sent": [], "sockets": [], "pve_status": 200,
             "level": "Read", "specific": ["Terminal"], "server_state": "Ok", "enabled": True,
             "disabled": False, "swarm": "", "template": False, "current_id": CONTAINER,
             "service_id": CONTAINER, "container_state": "running", "overrides": {},
             "login_reply": "LOGGED_IN", "connect_error": None, "create_response": None,
             "create_delay": 0, "creation_started": threading.Event(), "creation_order": []}
    ledger_path = tmp_path / "actions.sqlite3"

    def pve(request):
        state["seen"].append(("pve", request.url.path))
        assert str(request.url).startswith(PVE + "/")
        assert not any(name in request.headers for name in ("x-api-key", "x-api-secret", "authorization"))
        raw = request.headers["cookie"]
        assert raw in (COOKIE, "PVEAuthCookie=" + quote(RENEWED, safe=""))
        if request.url.path == "/api2/json/access/permissions":
            assert request.method == "GET" and request.url.query == b""
            return httpx.Response(state["pve_status"], json={"data": {}}, headers={"set-cookie": "private-cookie=do-not-replay"})
        assert request.method == "POST" and request.url.path == "/api2/json/access/ticket"
        form = parse_qs(request.content.decode())
        assert form == {"username": ["root@pam"], "password": [TICKET if raw == COOKIE else RENEWED],
                        "path": ["/"], "privs": ["Sys.Audit"]}
        return httpx.Response(200, json={"data": {"username": "root@pam"}})

    def core(request):
        assert str(request.url).startswith(CORE + "/")
        assert request.headers["x-api-key"] == KEY and request.headers["x-api-secret"] == SECRET
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if request.url.path == "/user":
            assert request.method == "GET"
            state["seen"].append(("core", "user"))
            return httpx.Response(200, json={"username": "integration", "enabled": True},
                                  headers={"set-cookie": "PVEAuthCookie=do-not-replay; Path=/"})
        payload = json.loads(request.content)
        variant, params = payload["type"], payload["params"]
        state["seen"].append(("core", variant))
        assert request.method == "POST" and request.url.path in ("/read", "/write")
        if request.url.path == "/write":
            assert variant in ("CreateTerminal", "DeleteTerminal"), "no execute/arbitrary writes are permitted"
            assert params["target"] == {"type": "Container", "params": {"server": SERVER, "container": CONTAINER}}
            if variant == "CreateTerminal":
                assert set(params) == {"name", "target", "command", "mode", "recreate"}
                if "expected_terminal" in state:
                    assert (params["command"], params["mode"]) == state["expected_terminal"]
                else:
                    assert params["command"] in ("sh", "bash") and params["mode"] == "exec"
                assert params["recreate"] == "Never"
                assert params["name"].startswith("pve-komodo-")
                with sqlite3.connect(ledger_path) as db:
                    assert db.execute("SELECT COUNT(*) FROM actions WHERE status IS NULL").fetchone()[0] >= 1
                state["created"].append(deepcopy(params))
                state["creation_order"].append("create-acknowledged")
                return state["create_response"] if state["create_response"] is not None else httpx.Response(200, json={
                    "name": params["name"], "target": params["target"], "command": PRIVATE,
                })
            assert params["terminal"] in [entry["name"] for entry in state["created"]]
            state["deleted"].append(deepcopy(params))
            state["creation_order"].append("delete-owned")
            return httpx.Response(200, json={})
        if variant == "GetStack":
            assert params == {"stack": STACK}
            result = {"_id": {"$oid": STACK}, "name": "test-stack", "template": state["template"],
                      "config": {"server_id": SERVER, "swarm_id": state["swarm"], "environment": PRIVATE}}
        elif variant == "GetPermission":
            assert params in ({"target": {"type": "Stack", "id": STACK}}, {"target": {"type": "Server", "id": SERVER}})
            result = {"level": state["level"], "specific": state["specific"]}
        elif variant == "GetServer":
            assert params == {"server": SERVER}
            result = {"_id": {"$oid": SERVER}, "template": False, "config": {"enabled": state["enabled"], "passkey": PRIVATE}}
        elif variant == "ListServers":
            assert params == {"query": {"names": [SERVER], "templates": "Include"}, "limit": 0}
            result = [{"id": SERVER, "info": {"state": state["server_state"], "container_terminals_disabled": state["disabled"],
                                               "private_key": PRIVATE}}]
        elif variant == "ListStackServices":
            assert params == {"stack": STACK}
            result = [{"stack_id": STACK, "service": "app", "container": {
                "server_id": SERVER, "id": state["service_id"], "name": "same-name", "state": "running", "env": PRIVATE,
            }, "image_digests": PRIVATE}]
        else:
            assert variant == "ListContainers" and params == {"server": SERVER}
            result = [{"server_id": SERVER, "id": state["current_id"], "name": "same-name", "state": state["container_state"], "labels": PRIVATE}]
        override = state["overrides"].get(variant)
        if isinstance(override, Exception):
            raise override
        return override if override is not None else httpx.Response(200, json=result,
                      headers={"set-cookie": "another-private-cookie=do-not-replay; Path=/"})

    async def core_transport(request):
        if request.url.path == "/write" and json.loads(request.content)["type"] == "CreateTerminal":
            state["creation_started"].set()
            if state["create_delay"]:
                await asyncio.sleep(state["create_delay"])
        return core(request)

    class FakeSocket:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.closed = False

        async def send(self, data):
            state["sent"].append(data)
            if isinstance(data, str):
                assert json.loads(data) == {"type": "ApiKeys", "params": {"key": KEY, "secret": SECRET}}
                await self.queue.put(state["login_reply"])
                await self.queue.put(b"shell-prompt> ")
            elif data.endswith(b"\x01"):
                await self.queue.put(b"echo:" + data[:-1])
            else:
                assert data.endswith(b"\xff")

        async def recv(self):
            return await self.queue.get()

        async def close(self):
            self.closed = True

    async def connect(url, verify):
        # Core's serde_qs parser requires literal brackets for nested fields.
        assert 'target[type]=Container' in url and 'target%5B' not in url
        assert url.startswith("ws://core.example:9120/ws/terminal?")
        assert isinstance(verify, ssl.SSLContext) and verify.check_hostname
        assert verify.verify_mode == ssl.CERT_REQUIRED
        query = parse_qs(url.split("?", 1)[1])
        assert set(query) == {"target[type]", "target[params][server]", "target[params][container]", "terminal"}
        assert query["target[type]"] == ["Container"] and query["target[params][server]"] == [SERVER]
        assert query["target[params][container]"] == [CONTAINER]
        assert query["terminal"][0].startswith("pve-komodo-")
        assert query["terminal"][0] in [entry["name"] for entry in state["created"]]
        with sqlite3.connect(ledger_path) as db:
            assert db.execute("SELECT COUNT(*) FROM actions WHERE status IS NULL").fetchone()[0] >= 1
        if state["connect_error"]:
            raise state["connect_error"]
        socket = FakeSocket()
        state["sockets"].append(socket)
        return socket

    app = FastAPI()
    authenticator = PveAuthenticator(PVE, transport=httpx.MockTransport(pve))
    manager = module.register_stack_terminal_routes(app, SimpleNamespace(upstream=PVE, komodo_url=CORE),
        authenticator, KomodoClient(CORE, KEY, SECRET, transport=httpx.MockTransport(core_transport)), CsrfProtector(),
        ActionStore(str(ledger_path)), ws_connect=connect)
    with TestClient(app) as browser:
        state.update(browser=browser, manager=manager, ledger_path=ledger_path)
        yield state
    assert not manager.active and not manager.cleanup_tasks


def assert_private(response):
    for value in (KEY, SECRET, PRIVATE, TICKET, RENEWED, "do-not-replay"):
        assert value not in response.text


def issue(state, *, request_id=None, cookie=COOKIE, shell="sh"):
    browser = state["browser"]
    response = browser.get(BASE + "/terminal-services", headers={"cookie": cookie})
    assert response.status_code == 200
    assert_private(response)
    token = response.json()["csrf_token"]
    command = {"service": "app", "server_id": SERVER, "container_id": CONTAINER,
               "shell": shell, "request_id": request_id or str(uuid.uuid4())}
    created = browser.post(BASE + "/terminals", headers={"cookie": cookie, "origin": "http://testserver", "x-pve-komodo-csrf": token}, json=command)
    assert created.status_code == 200
    assert_private(created)
    return created.json(), token, command


def open_socket(state, grant, token, *, cookie=COOKIE, path=BASE, origin="http://testserver"):
    context = state["browser"].websocket_connect(path + "/terminals/ws", headers={"cookie": cookie, "origin": origin})
    socket = context.__enter__()
    socket.send_json({"type": "open", "grant": grant["grant"], "csrf_token": token})
    return context, socket


def test_service_list_and_grant_do_not_create_a_shell_or_expose_unselected_config(terminal):
    grant, token, command = issue(terminal)
    assert set(grant) == {"grant", "session_id", "expires_in"} and grant["expires_in"] == 60
    uuid.UUID(grant["session_id"])
    assert terminal["created"] == terminal["deleted"] == []
    response = terminal["browser"].get(BASE + "/terminal-services", headers={"cookie": COOKIE})
    assert response.json()["services"] == [{"service": "app", "server_id": SERVER, "container_id": CONTAINER,
                                             "name": "same-name", "state": "running", "can_open": True}]
    assert response.json()["shells"] == ["sh", "bash"]


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_real_auth_csrf_full_id_ledger_then_binary_unicode_and_resize_forwarding_with_owned_cleanup(terminal, shell):
    grant, token, _ = issue(terminal, shell=shell)
    context, socket = open_socket(terminal, grant, token)
    try:
        assert socket.receive_json() == {"type": "ready", "session_id": grant["session_id"]}
        assert socket.receive_bytes() == b"shell-prompt> "
        socket.send_json({"type": "data", "data": "你好\x03"})
        assert socket.receive_bytes() == "echo:你好\x03".encode()
        socket.send_json({"type": "resize", "rows": 40, "cols": 120})
        socket.send_json({"type": "close"})
        assert socket.receive()["type"] == "websocket.close"
    finally:
        context.__exit__(None, None, None)
    assert len(terminal["created"]) == len(terminal["deleted"]) == 1
    assert terminal["created"][0]["command"] == shell
    assert json.loads(terminal["sent"][-1][:-1]) == {"rows": 40, "cols": 120}
    assert all(sock.closed for sock in terminal["sockets"])
    with sqlite3.connect(terminal["ledger_path"]) as db:
        rows = db.execute("SELECT status,result FROM actions").fetchall()
    assert rows == [(200, '{"detail":"terminal_session_requested"}')]
    assert grant["grant"] not in repr(rows) and SECRET not in repr(rows)


def test_disconnect_cleans_own_shell_and_consumed_grant_or_duplicate_uuid_cannot_create_again(terminal):
    grant, token, command = issue(terminal)
    context, socket = open_socket(terminal, grant, token)
    assert socket.receive_json()["type"] == "ready"
    assert socket.receive_bytes() == b"shell-prompt> "
    context.__exit__(None, None, None)
    context, socket = open_socket(terminal, grant, token)
    assert socket.receive_json() == {"type": "error", "code": "terminal_grant_invalid"}
    context.__exit__(None, None, None)
    second, token, _ = issue(terminal, request_id=command["request_id"])
    context, socket = open_socket(terminal, second, token)
    assert socket.receive_json() == {"type": "error", "code": "terminal_already_requested"}
    context.__exit__(None, None, None)
    assert len(terminal["created"]) == len(terminal["deleted"]) == 1


@pytest.mark.parametrize("change", ["replacement", "service_replaced", "permission", "disabled", "offline", "template", "swarm"])
def test_ws_rechecks_current_service_full_id_permissions_and_policy_after_http_grant(terminal, change):
    grant, token, _ = issue(terminal)
    if change == "replacement":
        terminal["current_id"] = REPLACEMENT
    elif change == "service_replaced":
        terminal["service_id"] = REPLACEMENT
    elif change == "permission":
        terminal["specific"] = []
    elif change == "disabled":
        terminal["disabled"] = True
    elif change == "offline":
        terminal["server_state"] = "NotOk"
    elif change == "template":
        terminal["template"] = True
    else:
        terminal["swarm"] = OTHER
    context, socket = open_socket(terminal, grant, token)
    result = socket.receive_json()
    assert result["type"] == "error" and PRIVATE not in json.dumps(result)
    context.__exit__(None, None, None)
    assert terminal["created"] == terminal["deleted"] == []


@pytest.mark.parametrize("changes", [{"shell": "sh; curl attacker"}, {"shell": "attach"}, {"service": "app;rm"},
    {"container_id": "same-name"}, {"server_id": OTHER, "params": {"type": "Server"}}, {"request_id": str(uuid.uuid1())}])
def test_grant_schema_rejects_command_url_rpc_or_name_injection(terminal, changes):
    _, token, command = issue(terminal)
    before = len(terminal["seen"])
    response = terminal["browser"].post(BASE + "/terminals", headers={"cookie": COOKIE, "origin": "http://testserver",
        "x-pve-komodo-csrf": token}, json={**command, **changes})
    assert response.status_code == 422
    assert not any(source == "core" for source, _ in terminal["seen"][before:])
    assert terminal["created"] == []
    assert_private(response)


@pytest.mark.parametrize("kind", ["anonymous", "expired", "no_csrf", "cross_origin"])
def test_http_denials_never_use_komodo_credentials_for_unauthorized_requests(terminal, kind):
    token = "forged"
    if kind in ("no_csrf", "cross_origin"):
        _, token, _ = issue(terminal)
    if kind == "expired":
        terminal["pve_status"] = 401
    terminal["seen"].clear()
    headers = {"cookie": COOKIE} if kind != "anonymous" else {}
    headers["origin"] = "http://evil.example" if kind == "cross_origin" else "http://testserver"
    if kind != "no_csrf":
        headers["x-pve-komodo-csrf"] = token
    response = terminal["browser"].post(BASE + "/terminals", headers=headers, json={})
    assert response.status_code == (401 if kind in ("anonymous", "expired") else 403)
    assert not any(source == "core" for source, _ in terminal["seen"])


@pytest.mark.parametrize("kind", ["bad_csrf", "wrong_stack", "renewed_cookie", "expired_grant"])
def test_ws_grants_require_matching_cookie_user_stack_and_unexpired_csrf(terminal, kind):
    grant, token, _ = issue(terminal)
    cookie, path = COOKIE, BASE
    if kind == "bad_csrf":
        token = "forged"
    elif kind == "wrong_stack":
        path = "/pve-komodo/api/stacks/" + OTHER
    elif kind == "renewed_cookie":
        cookie = "PVEAuthCookie=" + quote(RENEWED, safe="")
        token = terminal["browser"].get(BASE + "/terminal-services", headers={"cookie": cookie}).json()["csrf_token"]
    else:
        terminal["manager"].grants[grant["grant"]].expires = 0
    context, socket = open_socket(terminal, grant, token, cookie=cookie, path=path)
    assert socket.receive_json()["type"] == "error"
    context.__exit__(None, None, None)
    assert terminal["created"] == []


@pytest.mark.parametrize("origin", [None, "https://evil.example", "null"])
def test_ws_origin_is_mandatory_exact_and_checked_before_auth_or_accept(terminal, origin):
    headers = {"cookie": COOKIE}
    if origin is not None:
        headers["origin"] = origin
    with pytest.raises(WebSocketDisconnect):
        with terminal["browser"].websocket_connect(BASE + "/terminals/ws", headers=headers):
            pytest.fail("cross-origin socket accepted")
    assert terminal["seen"] == []


@pytest.mark.parametrize("path", [BASE + "/terminal-services?_dc=1", BASE + "/terminal-services?page=0",
                                  "/pve-komodo/api/stacks/not-an-id/terminal-services"])
def test_terminal_targets_reject_unknown_query_or_invalid_id_before_komodo(terminal, path):
    response = terminal["browser"].get(path, headers={"cookie": COOKIE})
    assert response.status_code == 422
    assert not any(source == "core" for source, _ in terminal["seen"])


@pytest.mark.parametrize("change", ["server_mismatch", "ambiguous_service", "wrong_stack", "non_json"])
def test_malformed_upstream_membership_never_enables_a_shell_or_leaks_errors(terminal, change):
    if change == "non_json":
        terminal["overrides"]["ListStackServices"] = httpx.Response(200, text=PRIVATE)
    else:
        rows = [{"stack_id": STACK, "service": "app", "container": {"id": CONTAINER, "server_id": SERVER}}]
        if change == "server_mismatch":
            rows[0]["container"]["server_id"] = OTHER
        elif change == "ambiguous_service":
            rows.append(deepcopy(rows[0]))
        else:
            rows[0]["stack_id"] = OTHER
        terminal["overrides"]["ListStackServices"] = httpx.Response(200, json=rows)
    response = terminal["browser"].get(BASE + "/terminal-services", headers={"cookie": COOKIE})
    if change == "server_mismatch":
        assert response.status_code == 200 and response.json()["services"][0]["can_open"] is False
    else:
        assert response.status_code == 502
    assert terminal["created"] == []
    assert_private(response)


@pytest.mark.parametrize("message", [{"type": "resize", "rows": True, "cols": 80}, {"type": "resize", "rows": 20, "cols": 65536},
    {"type": "data", "data": "x" * 16385}, {"type": "execute", "command": "sh"}, {"type": "data", "data": "x", "target": OTHER}])
def test_invalid_client_frames_fail_closed_and_clean_only_the_owned_pty(terminal, message):
    grant, token, _ = issue(terminal)
    context, socket = open_socket(terminal, grant, token)
    socket.receive_json(); socket.receive_bytes()
    socket.send_json(message)
    assert socket.receive_json() == {"type": "error", "code": "invalid_terminal_message"}
    context.__exit__(None, None, None)
    assert len(terminal["created"]) == len(terminal["deleted"]) == 1
    assert len(terminal["sent"]) == 1, "no invalid input is forwarded upstream"


@pytest.mark.parametrize("mode", ["login_error", "connect_timeout"])
def test_unknown_upstream_setup_never_retries_or_returns_sensitive_error_text(terminal, mode):
    grant, token, command = issue(terminal)
    if mode == "login_error":
        terminal["login_reply"] = PRIVATE
    else:
        terminal["connect_error"] = TimeoutError(PRIVATE)
    context, socket = open_socket(terminal, grant, token)
    response = socket.receive_json()
    assert response["type"] == "error" and PRIVATE not in repr(response)
    context.__exit__(None, None, None)
    again, token, _ = issue(terminal, request_id=command["request_id"])
    context, socket = open_socket(terminal, again, token)
    assert socket.receive_json() == {"type": "error", "code": "terminal_already_requested"}
    context.__exit__(None, None, None)
    assert len(terminal["created"]) == 1
    assert len(terminal["deleted"]) == 1


@pytest.mark.parametrize("mode", ["expiry", "idle", "revoked", "pve_expired"])
def test_active_session_has_bounded_lifetime_idle_and_rechecks_pve_and_terminal_permissions(terminal, monkeypatch, mode):
    monkeypatch.setattr(module, "_TICK_SECONDS", 0.01)
    monkeypatch.setattr(module, "_REVALIDATE_SECONDS", 0.02)
    if mode == "expiry":
        monkeypatch.setattr(module, "_LIFETIME_SECONDS", 0.02)
    elif mode == "idle":
        monkeypatch.setattr(module, "_IDLE_SECONDS", 0.02)
    grant, token, _ = issue(terminal)
    context, socket = open_socket(terminal, grant, token)
    socket.receive_json(); socket.receive_bytes()
    if mode == "revoked":
        terminal["specific"] = []
    elif mode == "pve_expired":
        terminal["pve_status"] = 401
    result = socket.receive_json()
    assert result["type"] == "error" and PRIVATE not in repr(result)
    context.__exit__(None, None, None)
    assert len(terminal["deleted"]) == 1


def test_active_connection_cap_fails_closed_without_an_extra_upstream_shell(terminal):
    with ExitStack() as stack:
        for _ in range(4):
            grant, token, _ = issue(terminal)
            context, socket = open_socket(terminal, grant, token)
            stack.callback(context.__exit__, None, None, None)
            assert socket.receive_json()["type"] == "ready"
            socket.receive_bytes()
        grant, token, _ = issue(terminal)
        context, socket = open_socket(terminal, grant, token)
        assert socket.receive_json() == {"type": "error", "code": "terminal_limit_reached"}
        context.__exit__(None, None, None)
        assert len(terminal["created"]) == 4
    assert len(terminal["deleted"]) == 4


def test_https_terminated_proxy_origin_is_accepted_without_trusting_forwarded_host(terminal):
    grant, token, _ = issue(terminal)
    with terminal["browser"].websocket_connect(BASE + "/terminals/ws", headers={
        "cookie": COOKIE, "origin": "https://testserver", "x-forwarded-proto": "https",
    }) as socket:
        socket.send_json({"type": "open", "grant": grant["grant"], "csrf_token": token})
        assert socket.receive_json()["type"] == "ready"
        socket.receive_bytes()
    assert len(terminal["deleted"]) == 1


def test_upstream_text_errors_are_never_forwarded_to_the_terminal(terminal):
    grant, token, _ = issue(terminal)
    context, socket = open_socket(terminal, grant, token)
    socket.receive_json(); socket.receive_bytes()
    socket.portal.call(terminal["sockets"][0].queue.put, PRIVATE)
    assert socket.receive_json() == {"type": "error", "code": "terminal_stream_closed"}
    context.__exit__(None, None, None)
    assert len(terminal["deleted"]) == 1


def test_actual_websocket_connector_refuses_redirects_without_contacting_another_origin(monkeypatch):
    from websockets.asyncio.client import connect
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    attempts, aborted = [], []

    async def create_connection(client):
        attempts.append(client.uri)
        assert client.proxy is None

        async def handshake(*args):
            raise InvalidStatus(Response(302, "Found", Headers({"Location": "ws://untrusted.example/steal"})))

        return SimpleNamespace(handshake=handshake, transport=SimpleNamespace(abort=lambda: aborted.append(True)))

    monkeypatch.setattr(connect, "create_connection", create_connection)
    with pytest.raises(InvalidStatus):
        asyncio.run(module._default_connect("ws://core.example/ws/terminal", True))
    assert attempts == ["ws://core.example/ws/terminal"] and aborted == [True]


@pytest.mark.parametrize("verify", [True, False])
def test_actual_websocket_connector_applies_explicit_tls_policy_and_never_environment_proxies(monkeypatch, verify):
    from websockets.asyncio.client import connect

    async def create_connection(client):
        assert client.proxy is None
        context = client.connection_kwargs["ssl"]
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == (ssl.CERT_REQUIRED if verify else ssl.CERT_NONE)

        async def handshake(*args):
            return None

        return SimpleNamespace(handshake=handshake, start_keepalive=lambda: None)

    monkeypatch.setattr(connect, "create_connection", create_connection)
    asyncio.run(module._default_connect("wss://core.example/ws/terminal", verify))


def test_disconnect_during_slow_creation_waits_for_the_same_ack_before_deleting_owned_shell(terminal):
    grant, token, _ = issue(terminal)
    terminal["create_delay"] = 0.05
    context, socket = open_socket(terminal, grant, token)
    assert terminal["creation_started"].wait(timeout=1)
    context.__exit__(None, None, None)
    assert terminal["creation_order"] == ["create-acknowledged", "delete-owned"]
    assert len(terminal["created"]) == len(terminal["deleted"]) == 1
    assert not terminal["manager"].active
    with sqlite3.connect(terminal["ledger_path"]) as db:
        assert db.execute("SELECT status FROM actions").fetchall() == [(200,)]


@pytest.mark.parametrize("response", [
    httpx.Response(500, text=PRIVATE),
    httpx.Response(200, json={"name": "an-existing-foreign-terminal", "target": {"type": "Server", "params": {"server": OTHER}}, "command": PRIVATE}),
])
def test_unconfirmed_creation_never_connects_or_deletes_a_returned_foreign_terminal(terminal, response):
    grant, token, _ = issue(terminal)
    terminal["create_response"] = response
    context, socket = open_socket(terminal, grant, token)
    result = socket.receive_json()
    assert result["type"] == "error" and PRIVATE not in repr(result)
    context.__exit__(None, None, None)
    assert not terminal["sockets"] and len(terminal["created"]) == len(terminal["deleted"]) == 1
    assert terminal["deleted"][0]["terminal"] == terminal["created"][0]["name"]


def issue_container(state, *, shell="sh", command=None, request_id=None, cookie=COOKIE):
    response = state["browser"].get(CONTAINER_BASE + "/terminal-services", headers={"cookie": cookie})
    assert response.status_code == 200
    assert_private(response)
    token = response.json()["csrf_token"]
    payload = {"shell": shell, "request_id": request_id or str(uuid.uuid4())}
    if command is not None:
        payload["command"] = command
    response = state["browser"].post(CONTAINER_BASE + "/terminals", headers={"cookie": cookie,
        "origin": "http://testserver", "x-pve-komodo-csrf": token}, json=payload)
    assert response.status_code == 200
    assert_private(response)
    return response.json(), token, payload


def test_container_terminal_options_use_full_identity_without_any_stack_membership(terminal):
    response = terminal["browser"].get(CONTAINER_BASE + "/terminal-services", headers={"cookie": COOKIE})
    assert response.status_code == 200
    assert_private(response)
    result = response.json()
    assert set(result) == {"server_id", "container_id", "name", "state", "can_open", "shells", "options", "csrf_token"}
    assert {key: result[key] for key in ("server_id", "container_id", "name", "state", "can_open")} == {
        "server_id": SERVER, "container_id": CONTAINER, "name": "same-name", "state": "running", "can_open": True}
    assert result["shells"] == ["sh", "bash", "attach", "custom"]
    assert result["options"] == {"sh": True, "bash": True, "attach": True, "custom": True}
    assert not any(variant in ("GetStack", "ListStackServices") for _, variant in terminal["seen"])
    assert terminal["created"] == terminal["deleted"] == []


@pytest.mark.parametrize("shell,command,expected", [
    ("sh", None, ("sh", "exec")), ("bash", None, ("bash", "exec")),
    ("attach", None, (None, "attach")), ("custom", "python -i", ("python -i", "exec")),
])
def test_container_terminal_modes_keep_commands_inside_pinned_container_and_cleanup_owned_pty(terminal, shell, command, expected):
    terminal["expected_terminal"] = expected
    # Core's Terminal permission covers docker attach too; Attach is unrelated.
    assert terminal["specific"] == ["Terminal"]
    grant, token, payload = issue_container(terminal, shell=shell, command=command)
    context, socket = open_socket(terminal, grant, token, path=CONTAINER_BASE)
    try:
        assert socket.receive_json() == {"type": "ready", "session_id": grant["session_id"]}
        assert socket.receive_bytes() == b"shell-prompt> "
        socket.send_json({"type": "data", "data": "\x03你好"})
        assert socket.receive_bytes() == "echo:\x03你好".encode()
        socket.send_json({"type": "resize", "rows": 53, "cols": 150})
        socket.send_json({"type": "close"})
        assert socket.receive()["type"] == "websocket.close"
    finally:
        context.__exit__(None, None, None)
    assert len(terminal["created"]) == len(terminal["deleted"]) == 1
    assert (terminal["created"][0]["command"], terminal["created"][0]["mode"]) == expected
    assert terminal["deleted"][0]["terminal"] == terminal["created"][0]["name"]
    assert json.loads(terminal["sent"][-1][:-1]) == {"rows": 53, "cols": 150}
    assert not any(variant in ("GetStack", "ListStackServices") for _, variant in terminal["seen"])
    with sqlite3.connect(terminal["ledger_path"]) as db:
        assert db.execute("SELECT status,result FROM actions").fetchall() == [(200, '{"detail":"terminal_session_requested"}')]


@pytest.mark.parametrize("changes", [
    {"shell": "custom"}, {"shell": "custom", "command": ""}, {"shell": "custom", "command": "  "},
    {"shell": "custom", "command": "sh\nwhoami"}, {"shell": "custom", "command": "sh\x00"},
    {"shell": "custom", "command": "a" * 2049}, {"shell": "custom", "command": 42},
    {"shell": "sh", "command": "bash"}, {"shell": "attach", "command": "sh"},
    {"shell": "sh; whoami"}, {"server_id": OTHER}, {"container_id": REPLACEMENT},
    {"target": {"type": "Server", "params": {"server": SERVER}}}, {"request_id": str(uuid.uuid1())},
])
def test_container_terminal_schema_rejects_target_overrides_and_invalid_custom_commands(terminal, changes):
    response = terminal["browser"].get(CONTAINER_BASE + "/terminal-services", headers={"cookie": COOKIE})
    token = response.json()["csrf_token"]
    terminal["seen"].clear()
    response = terminal["browser"].post(CONTAINER_BASE + "/terminals", headers={"cookie": COOKIE,
        "origin": "http://testserver", "x-pve-komodo-csrf": token},
        json={"shell": "sh", "request_id": str(uuid.uuid4()), **changes})
    assert response.status_code == 422
    assert not any(source == "core" for source, _ in terminal["seen"])
    assert terminal["created"] == []
    assert_private(response)


@pytest.mark.parametrize("change", ["replaced", "server_mismatch", "duplicate", "paused", "disabled", "offline", "permission"])
def test_container_grant_rechecks_full_id_running_state_server_policy_and_permission_before_create(terminal, change):
    grant, token, _ = issue_container(terminal)
    if change == "replaced":
        terminal["current_id"] = REPLACEMENT
    elif change in ("server_mismatch", "duplicate"):
        rows = [{"server_id": OTHER if change == "server_mismatch" else SERVER, "id": CONTAINER,
                 "name": "same-name", "state": "running"}]
        terminal["overrides"]["ListContainers"] = httpx.Response(200, json=rows * (2 if change == "duplicate" else 1))
    elif change == "paused":
        terminal["container_state"] = "paused"
    elif change == "disabled":
        terminal["disabled"] = True
    elif change == "offline":
        terminal["server_state"] = "NotOk"
    else:
        terminal["specific"] = ["Attach"]
    context, socket = open_socket(terminal, grant, token, path=CONTAINER_BASE)
    result = socket.receive_json()
    assert result["type"] == "error" and PRIVATE not in repr(result)
    context.__exit__(None, None, None)
    assert terminal["created"] == terminal["deleted"] == []


@pytest.mark.parametrize("change", ["stopped", "periphery_disabled", "server_disabled", "server_template"])
def test_container_terminal_options_disable_all_modes_when_current_target_cannot_open(terminal, change):
    if change == "stopped":
        terminal["container_state"] = "exited"
    elif change == "periphery_disabled":
        terminal["disabled"] = True
    elif change == "server_disabled":
        terminal["enabled"] = False
    else:
        terminal["overrides"]["GetServer"] = httpx.Response(200, json={
            "_id": {"$oid": SERVER}, "template": True, "config": {"enabled": True}})
    response = terminal["browser"].get(CONTAINER_BASE + "/terminal-services", headers={"cookie": COOKIE})
    if change == "server_template":
        assert response.status_code == 409
    else:
        assert response.status_code == 200
        assert response.json()["can_open"] is False
        assert response.json()["options"] == {"sh": False, "bash": False, "attach": False, "custom": False}
        response = terminal["browser"].post(CONTAINER_BASE + "/terminals", headers={"cookie": COOKIE,
            "origin": "http://testserver", "x-pve-komodo-csrf": response.json()["csrf_token"]},
            json={"shell": "sh", "request_id": str(uuid.uuid4())})
        assert response.status_code == 409
    assert terminal["created"] == []


@pytest.mark.parametrize("kind", ["wrong_server", "wrong_container", "stack_route", "stack_grant"])
def test_container_grants_are_bound_to_route_kind_server_and_full_container_id(terminal, kind):
    grant, token, _ = issue_container(terminal)
    path = CONTAINER_BASE
    if kind == "wrong_server":
        path = path.replace(SERVER, OTHER)
    elif kind == "wrong_container":
        path = path.replace(CONTAINER, REPLACEMENT)
    elif kind == "stack_route":
        path = BASE
    else:
        grant, token, _ = issue(terminal)
    context, socket = open_socket(terminal, grant, token, path=path)
    assert socket.receive_json() == {"type": "error", "code": "terminal_grant_invalid"}
    context.__exit__(None, None, None)
    assert terminal["created"] == []


def test_container_terminal_duplicate_uuid_never_creates_a_second_pty(terminal):
    grant, token, payload = issue_container(terminal)
    context, socket = open_socket(terminal, grant, token, path=CONTAINER_BASE)
    assert socket.receive_json()["type"] == "ready"
    socket.receive_bytes()
    context.__exit__(None, None, None)
    grant, token, _ = issue_container(terminal, request_id=payload["request_id"])
    context, socket = open_socket(terminal, grant, token, path=CONTAINER_BASE)
    assert socket.receive_json() == {"type": "error", "code": "terminal_already_requested"}
    context.__exit__(None, None, None)
    assert len(terminal["created"]) == len(terminal["deleted"]) == 1


@pytest.mark.parametrize("kind", ["anonymous", "expired", "no_csrf", "cross_origin"])
def test_container_terminal_denials_do_not_contact_core(terminal, kind):
    token = "forged"
    if kind in ("no_csrf", "cross_origin"):
        token = terminal["browser"].get(CONTAINER_BASE + "/terminal-services", headers={"cookie": COOKIE}).json()["csrf_token"]
    if kind == "expired":
        terminal["pve_status"] = 401
    headers = {"cookie": COOKIE} if kind != "anonymous" else {}
    headers["origin"] = "http://evil.example" if kind == "cross_origin" else "http://testserver"
    if kind != "no_csrf":
        headers["x-pve-komodo-csrf"] = token
    terminal["seen"].clear()
    response = terminal["browser"].post(CONTAINER_BASE + "/terminals", headers=headers, json={})
    assert response.status_code == (401 if kind in ("anonymous", "expired") else 403)
    assert not any(source == "core" for source, _ in terminal["seen"])
    assert_private(response)


@pytest.mark.parametrize("path", [
    CONTAINER_BASE + "/terminal-services?_dc=1",
    CONTAINER_BASE.replace(SERVER, "invalid") + "/terminal-services",
    CONTAINER_BASE.replace(CONTAINER, CONTAINER[:12]) + "/terminal-services",
    CONTAINER_BASE.replace(CONTAINER, "same-name") + "/terminal-services",
])
def test_container_terminal_route_rejects_query_names_or_short_ids_before_core(terminal, path):
    response = terminal["browser"].get(path, headers={"cookie": COOKIE})
    assert response.status_code == 422
    assert not any(source == "core" for source, _ in terminal["seen"])


def test_active_container_terminal_revoked_permission_closes_and_cleans_owned_pty(terminal, monkeypatch):
    monkeypatch.setattr(module, "_TICK_SECONDS", 0.01)
    monkeypatch.setattr(module, "_REVALIDATE_SECONDS", 0.02)
    grant, token, _ = issue_container(terminal)
    context, socket = open_socket(terminal, grant, token, path=CONTAINER_BASE)
    assert socket.receive_json()["type"] == "ready"
    socket.receive_bytes()
    terminal["specific"] = []
    assert socket.receive_json() == {"type": "error", "code": "terminal_permission_denied"}
    context.__exit__(None, None, None)
    assert len(terminal["created"]) == len(terminal["deleted"]) == 1
