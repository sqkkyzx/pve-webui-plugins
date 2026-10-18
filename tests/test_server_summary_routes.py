"""Real route/auth/binding/client/ledger chain, with HTTP confined to fake transports."""

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
from plugins.komodo.server_summary import HISTORY_INTERVALS


PVE, KOMODO = "https://pve.example:8006", "http://core:9120"
SERVER, OTHER, OPERATION = "a" * 24, "b" * 24, "c" * 24
KEY, SECRET = "summary-integration-key", "summary-integration-secret"
PRIVATE = "summary-private-passkey-and-command-output"
TICKET = "PVE:root@pam:1234ABCD::signature+value="
COOKIE = "PVEAuthCookie=" + quote(TICKET, safe="")
VM_UUID = "9f376f89-d6b7-4dc8-84a4-f4769dc9b9bb"
BASE = "/pve-komodo/api/guests/qemu/101"
TS = 1792256400123
ACTIONS = {
    "restart_all_containers": "RestartAllContainers", "pause_all_containers": "PauseAllContainers",
    "stop_all_containers": "StopAllContainers", "prune_buildx": "PruneBuildx", "prune_system": "PruneSystem",
}


@pytest.fixture
def integrated(tmp_path):
    links_file, actions_file = tmp_path / "links.sqlite3", tmp_path / "actions.sqlite3"
    state = {
        "uuid": VM_UUID, "seen": [], "writes": [], "change": None, "change_on": None,
        "permission": "Execute", "disabled": False, "pve_status": 200, "state": "Ok",
        "overrides": {}, "execute_response": None, "description_response": None, "links_file": links_file, "actions_file": actions_file,
        "server": {"_id": {"$oid": SERVER}, "name": "docker-guest", "template": False,
                   "description": "Operations <script> stays plain text", "updated_at": TS,
                   "config": {"enabled": True, "passkey": PRIVATE}, "info": {"private_key": PRIVATE}},
    }

    def apply_change():
        change, state["change"] = state["change"], None
        if change == "unlink":
            database = GuestLinkStore(str(links_file), komodo_origin=KOMODO, pve_origin=PVE)
            database.unlink(SERVER, expected_revision=1)
            database.close()
        elif change == "identity":
            state["uuid"] = "ec5c382f-b7d3-4fa2-9d37-08e72fc3d826"
        elif change == "permission":
            state["permission"] = "Read"
        elif change == "disabled":
            state["server"]["config"]["enabled"] = False
        elif change == "disconnected":
            state["state"] = "NotOk"
        elif change == "ui_disabled":
            state["disabled"] = True
        elif change == "description":
            state["server"]["description"] = "Another editor saved this first"

    def pve(request):
        state["seen"].append(("pve", request.method, request.url.path))
        assert str(request.url).startswith(PVE + "/")
        assert request.headers["cookie"] == COOKIE
        assert not any(name in request.headers for name in ("x-api-key", "x-api-secret", "authorization"))
        path = request.url.path
        if path == "/api2/json/access/permissions":
            assert request.method == "GET" and request.url.query == b""
            return httpx.Response(state["pve_status"], json={"data": {"/": {"Sys.Audit": 1}}}, headers={
                "set-cookie": "unexpected-upstream-cookie=must-not-replay; Path=/",
            })
        if path == "/api2/json/access/ticket":
            assert request.method == "POST"
            assert parse_qs(request.content.decode()) == {
                "username": ["root@pam"], "password": [TICKET], "path": ["/"], "privs": ["Sys.Audit"],
            }
            return httpx.Response(200, json={"data": {"username": "root@pam"}})
        assert request.method == "GET", "PVE guest discovery must never mutate a guest"
        if path == "/api2/json/cluster/resources":
            assert dict(request.url.params) == {"type": "vm"}
            return httpx.Response(200, json={"data": [{
                "type": "qemu", "vmid": 101, "node": "pve-one", "name": "docker-guest", "status": "running",
            }]})
        assert path == "/api2/json/nodes/pve-one/qemu/101/config"
        return httpx.Response(200, json={"data": {"smbios1": "uuid=" + state["uuid"], "sshkeys": PRIVATE}})

    def komodo(request):
        assert str(request.url).startswith(KOMODO + "/")
        assert request.headers["x-api-key"] == KEY and request.headers["x-api-secret"] == SECRET
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if request.url.path == "/user":
            state["seen"].append(("komodo", "GET", "/user"))
            assert request.method == "GET"
            return httpx.Response(200, json={"username": "integration", "enabled": True}, headers={
                "set-cookie": "PVEAuthCookie=must-not-replay; Path=/",
            })
        assert request.method == "POST"
        payload = json.loads(request.content)
        variant, params = payload["type"], payload["params"]
        state["seen"].append(("komodo", request.url.path, variant))
        if request.url.path == "/execute":
            # This callback is the only possible mutation target; MockTransport
            # prevents every real network request, even to the fake origins.
            assert variant in ACTIONS.values() and params == {"server": SERVER}
            with sqlite3.connect(actions_file) as db:
                assert db.execute("SELECT COUNT(*) FROM actions WHERE status IS NULL").fetchone()[0] == 1
            permission = max(index for index, item in enumerate(state["seen"]) if item == ("komodo", "/read", "GetPermission"))
            fingerprint = max(index for index, item in enumerate(state["seen"]) if item == ("pve", "GET", "/api2/json/nodes/pve-one/qemu/101/config"))
            assert fingerprint > permission, "binding identity must be rechecked after fresh execution permissions"
            state["writes"].append(deepcopy(payload))
            response = state["execute_response"]
            if isinstance(response, Exception):
                raise response
            if response is not None:
                return response
            return httpx.Response(200, json={
                "_id": {"$oid": OPERATION}, "operation": variant, "target": {"type": "Server", "id": SERVER},
                "status": "Queued", "success": False, "commands": [PRIVATE], "other_data": PRIVATE,
            })
        if request.url.path == "/write":
            assert variant == "UpdateResourceMeta"
            assert set(params) == {"target", "description"}
            assert params["target"] == {"type": "Server", "id": SERVER} and isinstance(params["description"], str)
            with sqlite3.connect(actions_file) as db:
                assert db.execute("SELECT COUNT(*) FROM actions WHERE status IS NULL").fetchone()[0] == 1
            permission = max(index for index, item in enumerate(state["seen"]) if item == ("komodo", "/read", "GetPermission"))
            fingerprint = max(index for index, item in enumerate(state["seen"]) if item == ("pve", "GET", "/api2/json/nodes/pve-one/qemu/101/config"))
            assert fingerprint > permission
            state["writes"].append(deepcopy(payload))
            response = state["description_response"]
            if isinstance(response, Exception):
                raise response
            if response is not None:
                return response
            state["server"]["description"] = params["description"]
            return httpx.Response(200, json={})
        assert request.url.path == "/read", "no arbitrary RPC path"
        if variant == "ListServers":
            assert params in ({"query": {"templates": "Include"}, "limit": 0},
                              {"query": {"names": [SERVER], "templates": "Include"}, "limit": 0})
            if "names" not in params["query"] and state["change"] and state["change_on"] == "action_preflight":
                apply_change()
            response = [{"id": SERVER, "name": "docker-guest", "template": False,
                         "info": {"state": state["state"], "version": "2.3.3", "public_key": PRIVATE}}]
        elif variant == "GetPermission":
            assert params == {"target": {"type": "Server", "id": SERVER}}
            response = {"level": state["permission"], "specific": ["Processes"]}
        elif variant == "GetCoreInfo":
            assert params == {}
            response = {"ui_write_disabled": state["disabled"], "webhook_base_url": PRIVATE}
        elif variant == "ListUpdates":
            assert params == {"page": 0, "query": {"target.type": "Server", "target.id": SERVER}}
            response = {"updates": [], "next_page": None}
        elif variant == "GetHistoricalServerStats":
            assert params["server"] == SERVER and params["page"] == 0 and params["granularity"] in HISTORY_INTERVALS
            assert set(params) == {"server", "page", "granularity"}
            response = {"stats": [
                {"sid": SERVER, "ts": TS, "cpu_perc": 20, "network_ingress_bytes": 1000},
                {"sid": SERVER, "ts": TS - 3 * 86_400_000, "cpu_perc": 0, "network_ingress_bytes": 0},
            ], "next_page": None}
        else:
            assert variant in {"GetServer", "GetSystemStats", "GetSystemInformation", "ListContainers", "ListSystemProcesses"}
            assert params == {"server": SERVER}
            response = {
                "GetServer": state["server"],
                "GetSystemStats": {"cpu_perc": 20, "mem_used_gb": 2, "mem_total_gb": 8,
                                   "network_ingress_bytes": 1000, "network_egress_bytes": 250,
                                   "polling_rate": "5-sec", "refresh_ts": TS, "disks": []},
                "GetSystemInformation": {"core_count": 2, "logical_core_count": 4},
                "ListContainers": [], "ListSystemProcesses": [],
            }[variant]
        if state["change"] and state["change_on"] == variant:
            apply_change()
        override = state["overrides"].get(variant)
        if isinstance(override, Exception):
            raise override
        return override if override is not None else httpx.Response(200, json=deepcopy(response), headers={
            "set-cookie": "private-upstream-cookie=must-not-replay; Path=/",
        })

    application = create_app(Settings(PVE, komodo_url=KOMODO, komodo_api_key=KEY, komodo_api_secret=SECRET,
                                      action_state_file=str(actions_file), guest_links_file=str(links_file)),
                             pve_auth_transport=httpx.MockTransport(pve), komodo_transport=httpx.MockTransport(komodo))
    with TestClient(application) as browser:
        state["browser"] = browser
        yield state


def assert_private(response):
    assert response.headers["cache-control"] == "private, no-store"
    for value in (KEY, SECRET, PRIVATE, TICKET, "must-not-replay"):
        assert value not in response.text


def bind_and_open(state):
    browser = state["browser"]
    listing = browser.get("/pve-komodo/api/guest-links", headers={"cookie": COOKIE})
    assert listing.status_code == 200
    headers = {"cookie": COOKIE, "origin": "http://testserver", "x-pve-komodo-csrf": listing.json()["csrf_token"]}
    linked = browser.post("/pve-komodo/api/guest-links", headers=headers, json={
        "server_id": SERVER, "guest_type": "qemu", "vmid": 101, "expected_revision": 0,
    })
    assert linked.status_code == 200 and linked.json()["link"]["status"] == "linked"
    opened = browser.get(BASE + "/summary", headers={"cookie": COOKIE})
    assert opened.status_code == 200
    assert_private(opened)
    headers["x-pve-komodo-csrf"] = opened.json()["csrf_token"]
    command = {"action": "pause_all_containers", "server_id": SERVER, "binding_revision": 1, "request_id": str(uuid.uuid4())}
    return browser, headers, command, opened


def test_summary_is_real_authenticated_bound_dto_with_rate_units_and_csrf(integrated):
    _, _, _, opened = bind_and_open(integrated)
    data = opened.json()
    assert data["server_id"] == SERVER and data["binding_revision"] == 1
    assert data["description"] == "Operations <script> stays plain text"
    assert data["can_execute"] is True and data["can_write"] is False
    assert data["current"]["network_ingress_bytes_per_second"] == 200
    assert data["current"]["network_egress_bytes_per_second"] == 50
    assert data["current"]["refresh_ts"] == TS
    assert integrated["writes"] == []


@pytest.mark.parametrize("interval", HISTORY_INTERVALS)
def test_history_uses_fixed_interval_page_zero_and_preserves_sparse_actual_millisecond_points(integrated, interval):
    browser, headers, _, _ = bind_and_open(integrated)
    response = browser.get(BASE + "/history", headers=headers, params={"interval": interval})
    assert response.status_code == 200
    data = response.json()
    assert data["interval"] == interval and data["binding_revision"] == 1
    assert [row["ts"] for row in data["points"]] == [TS - 3 * 86_400_000, TS]
    assert [row["network_ingress_bytes"] for row in data["points"]] == [0, 1000]
    assert data["network_unit"] == "bytes_per_sample" and "csrf_token" not in data
    assert_private(response)
    assert integrated["writes"] == []


@pytest.mark.parametrize("path", [
    "/summary?interval=1-day", "/summary?_dc=123", "/history?interval=1-day&interval=1-min",
    "/history?interval=1-day&page=0", "/history?interval=bad", "/history?interval=",
])
def test_summary_history_reject_unknown_duplicate_or_invalid_query_before_komodo(integrated, path):
    response = integrated["browser"].get(BASE + path, headers={"cookie": COOKIE})
    assert response.status_code == 422
    assert not any(item[0] == "komodo" for item in integrated["seen"])
    assert_private(response)


@pytest.mark.parametrize("path", ["/pve-komodo/api/guests/vm/101/summary", "/pve-komodo/api/guests/qemu/01/summary",
                                  "/pve-komodo/api/guests/qemu/1000000000/history"])
def test_invalid_guest_target_is_rejected_before_komodo(integrated, path):
    response = integrated["browser"].get(path, headers={"cookie": COOKIE})
    assert response.status_code == 422
    assert not any(item[0] == "komodo" for item in integrated["seen"])


@pytest.mark.parametrize("suffix,method", [("summary", "get"), ("history", "get"), ("actions", "post")])
def test_anonymous_sessions_never_reach_either_upstream(integrated, suffix, method):
    response = getattr(integrated["browser"], method)(BASE + "/" + suffix)
    assert response.status_code == 401 and integrated["seen"] == []
    assert_private(response)


@pytest.mark.parametrize("suffix,method", [("summary", "get"), ("history", "get"), ("actions", "post")])
def test_expired_pve_session_never_uses_komodo_credentials(integrated, suffix, method):
    integrated["pve_status"] = 401
    response = getattr(integrated["browser"], method)(BASE + "/" + suffix, headers={"cookie": COOKIE})
    assert response.status_code == 401
    assert not any(item[0] == "komodo" for item in integrated["seen"])
    assert_private(response)


@pytest.mark.parametrize("kind", ["missing", "forged", "cross_origin", "duplicate"])
def test_write_requires_own_valid_single_csrf_and_same_origin_before_komodo(integrated, kind):
    browser, headers, command, _ = bind_and_open(integrated)
    integrated["seen"].clear()
    if kind == "missing":
        headers.pop("x-pve-komodo-csrf")
    elif kind == "forged":
        headers["x-pve-komodo-csrf"] = "PVE-CSRF-header-is-not-the-integration-token"
    elif kind == "cross_origin":
        headers["origin"] = "https://evil.example"
    else:
        headers = list(headers.items()) + [("x-pve-komodo-csrf", headers["x-pve-komodo-csrf"])]
    response = browser.post(BASE + "/actions", headers=headers, json=command)
    assert response.status_code == 403 and integrated["writes"] == []
    assert not any(item[0] == "komodo" for item in integrated["seen"])
    assert_private(response)


@pytest.mark.parametrize("action,variant", ACTIONS.items())
def test_five_fixed_actions_recheck_before_execute_and_persist_single_duplicate_receipt(integrated, action, variant):
    browser, headers, command, _ = bind_and_open(integrated)
    command["action"] = action
    first = browser.post(BASE + "/actions", headers=headers, json=command)
    assert first.status_code == 200
    assert first.json() == {"section": "servers", "action": action, "results": [{
        "id": SERVER, "ok": True, "status": "submitted", "operation_id": OPERATION, "error_code": None,
    }]}
    assert integrated["writes"] == [{"type": variant, "params": {"server": SERVER}}]
    again = browser.post(BASE + "/actions", headers=headers, json=command)
    assert again.status_code == 200 and again.json() == first.json()
    assert len(integrated["writes"]) == 1
    with sqlite3.connect(integrated["actions_file"]) as db:
        rows = db.execute("SELECT fingerprint,status,result FROM actions").fetchall()
    assert len(rows) == 1 and rows[0][1] == 200 and json.loads(rows[0][2]) == first.json()
    assert PRIVATE not in repr(rows) and SECRET not in repr(rows)
    assert_private(first)


@pytest.mark.parametrize("change,status,code", [
    ("unlink", 409, "guest_not_linked"), ("identity", 409, "guest_identity_changed"),
    ("permission", 403, "server_execution_forbidden"), ("disabled", 403, "server_execution_forbidden"),
    ("disconnected", 403, "server_execution_forbidden"),
])
def test_fresh_guard_blocks_unlink_identity_change_revoked_execute_disabled_or_disconnected(integrated, change, status, code):
    browser, headers, command, _ = bind_and_open(integrated)
    integrated.update(change=change, change_on="action_preflight")
    response = browser.post(BASE + "/actions", headers=headers, json=command)
    assert response.status_code == status and response.json() == {"detail": code}
    assert integrated["writes"] == []
    with sqlite3.connect(integrated["actions_file"]) as db:
        assert db.execute("SELECT status,result FROM actions").fetchall() == [
            (status, json.dumps({"detail": code}, separators=(",", ":"))),
        ]
    assert_private(response)


def test_global_ui_config_write_policy_does_not_remove_execute_permission(integrated):
    browser, headers, command, _ = bind_and_open(integrated)
    integrated.update(change="ui_disabled", change_on="action_preflight")
    response = browser.post(BASE + "/actions", headers=headers, json=command)
    assert response.status_code == 200 and response.json()["results"][0]["status"] == "submitted"
    assert len(integrated["writes"]) == 1


@pytest.mark.parametrize("suffix,rpc", [("summary", "GetSystemStats"), ("history", "GetHistoricalServerStats")])
@pytest.mark.parametrize("change,code", [("unlink", "guest_not_linked"), ("identity", "guest_identity_changed")])
def test_read_rechecks_mapping_after_komodo_so_stale_bound_data_is_not_returned(integrated, suffix, rpc, change, code):
    browser, headers, _, _ = bind_and_open(integrated)
    integrated.update(change=change, change_on=rpc)
    response = browser.get(BASE + "/" + suffix, headers=headers)
    assert response.status_code == 409 and response.json() == {"detail": code}
    assert integrated["writes"] == []
    assert_private(response)


@pytest.mark.parametrize("suffix,rpc,code", [
    ("summary", "ListSystemProcesses", "processes_permission_denied"),
    ("history", "GetHistoricalServerStats", "history_permission_denied"),
])
def test_partial_read_errors_are_safe_dto_and_keep_other_values(integrated, suffix, rpc, code):
    browser, headers, _, _ = bind_and_open(integrated)
    integrated["overrides"][rpc] = httpx.Response(403, text=PRIVATE)
    response = browser.get(BASE + "/" + suffix, headers=headers)
    assert response.status_code == 200 and code in response.json()["errors"]
    if suffix == "summary":
        assert response.json()["current"]["cpu_percent"] == 20
    else:
        assert response.json()["points"] == []
    assert_private(response)


@pytest.mark.parametrize("changes", [
    {"action": "arbitrary_rpc"}, {"server_id": SERVER + ":" + "d" * 64},
    {"binding_revision": True}, {"binding_revision": "1"}, {"request_id": str(uuid.uuid1())},
    {"config": {"passkey": PRIVATE}}, {"params": {"server": OTHER}},
])
def test_action_body_is_strict_and_cannot_inject_rpc_params(integrated, changes):
    browser, headers, command, _ = bind_and_open(integrated)
    integrated["seen"].clear()
    response = browser.post(BASE + "/actions", headers=headers, json={**command, **changes})
    assert response.status_code == 422 and integrated["writes"] == []
    assert not any(item[0] == "komodo" for item in integrated["seen"])
    assert_private(response)


@pytest.mark.parametrize("change", [{"server_id": OTHER}, {"binding_revision": 2}])
def test_client_expected_binding_is_checked_before_reserving_or_dispatching(integrated, change):
    browser, headers, command, _ = bind_and_open(integrated)
    response = browser.post(BASE + "/actions", headers=headers, json={**command, **change})
    assert response.status_code == 409 and response.json() == {"detail": "guest_link_revision_conflict"}
    assert integrated["writes"] == [] and not integrated["actions_file"].exists()


@pytest.mark.parametrize("outcome", ["timeout", "http408", "http502", "malformed", "wrong_target"])
def test_uncertain_submission_is_never_retried_and_duplicate_receipt_stays_unknown(integrated, outcome):
    browser, headers, command, _ = bind_and_open(integrated)
    integrated["execute_response"] = {
        "timeout": httpx.ReadTimeout(PRIVATE), "http408": httpx.Response(408, text=PRIVATE),
        "http502": httpx.Response(502, text=PRIVATE), "malformed": httpx.Response(200, json={"commands": PRIVATE}),
        "wrong_target": httpx.Response(200, json={"_id": {"$oid": OPERATION}, "operation": "PauseAllContainers",
                                                  "target": {"type": "Server", "id": OTHER}, "status": "Queued", "success": False}),
    }[outcome]
    first = browser.post(BASE + "/actions", headers=headers, json=command)
    assert first.status_code == 200 and first.json()["results"][0] == {
        "id": SERVER, "ok": False, "status": "outcome_unknown", "operation_id": None, "error_code": "outcome_unknown",
    }
    again = browser.post(BASE + "/actions", headers=headers, json=command)
    assert again.status_code == 200 and again.json() == first.json() and len(integrated["writes"]) == 1
    assert_private(first)


def test_same_uuid_cannot_change_action_or_cross_into_other_action_route(integrated):
    browser, headers, command, _ = bind_and_open(integrated)
    first = browser.post(BASE + "/actions", headers=headers, json=command)
    assert first.status_code == 200
    changed = browser.post(BASE + "/actions", headers=headers, json={**command, "action": "prune_system"})
    assert changed.status_code == 409 and changed.json() == {"detail": "idempotency_conflict"}
    other_route = browser.post("/pve-komodo/api/action", headers=headers, json={
        "section": "servers", "action": command["action"], "ids": [SERVER], "request_id": command["request_id"],
    })
    assert other_route.status_code == 409 and other_route.json() == {"detail": "idempotency_conflict"}
    assert len(integrated["writes"]) == 1


def open_description(state):
    state["permission"] = "Write"
    browser, headers, command, opened = bind_and_open(state)
    command.pop("action")
    command.update(base_version=opened.json()["description_version"], description="中文备注 <b>plain text</b>\nline 2\tend")
    return browser, headers, command


@pytest.mark.parametrize("description", ["中文备注 <b>plain text</b>\nline 2\tend", "", "😀" * 16_384])
def test_description_saves_only_server_metadata_and_replays_receipt_without_writing_twice(integrated, description):
    browser, headers, command = open_description(integrated)
    command["description"] = description
    first = browser.post(BASE + "/description", headers=headers, json=command)
    assert first.status_code == 200 and first.json() == {
        "ok": True, "status": "completed", "operation_id": None, "error_code": None,
    }
    assert integrated["writes"] == [{"type": "UpdateResourceMeta", "params": {
        "target": {"type": "Server", "id": SERVER}, "description": description,
    }}]
    again = browser.post(BASE + "/description", headers=headers, json=command)
    assert again.json() == first.json() and len(integrated["writes"]) == 1
    refreshed = browser.get(BASE + "/summary", headers=headers).json()
    assert refreshed["description"] == description and refreshed["description_version"] != command["base_version"]
    assert integrated["server"]["config"]["passkey"] == PRIVATE
    with sqlite3.connect(integrated["actions_file"]) as db:
        stored = db.execute("SELECT fingerprint,status,result FROM actions").fetchall()
    assert description not in repr(stored) if description else True
    assert PRIVATE not in repr(stored)
    assert_private(first)


@pytest.mark.parametrize("change,trigger,status,code", [
    ("description", "action_preflight", 409, "server_description_conflict"),
    ("permission", "GetServer", 403, "server_description_read_only"),
    ("ui_disabled", "GetServer", 403, "server_description_read_only"),
    ("unlink", "GetPermission", 409, "guest_not_linked"),
    ("identity", "GetPermission", 409, "guest_identity_changed"),
])
def test_description_checks_fresh_write_permission_content_version_and_guest_identity(integrated, change, trigger, status, code):
    browser, headers, command = open_description(integrated)
    if change == "description":
        integrated["server"]["description"] = "Another editor saved this first"
    else:
        integrated.update(change=change, change_on=trigger)
    response = browser.post(BASE + "/description", headers=headers, json=command)
    assert response.status_code == status and response.json() == {"detail": code}
    assert integrated["writes"] == []
    assert_private(response)


@pytest.mark.parametrize("changes", [
    {"description": None}, {"description": False}, {"description": "x" * 16_385}, {"description": "bad\x00value"},
    {"target": {"type": "Server", "id": OTHER}}, {"tags": []}, {"template": True},
    {"base_version": "f" * 63}, {"binding_revision": True}, {"request_id": str(uuid.uuid1())},
])
def test_description_accepts_no_other_resource_fields_or_unvalidated_text(integrated, changes):
    browser, headers, command = open_description(integrated)
    integrated["seen"].clear()
    response = browser.post(BASE + "/description", headers=headers, json={**command, **changes})
    assert response.status_code == 422 and integrated["writes"] == []
    assert not any(item[0] == "komodo" for item in integrated["seen"])
    assert_private(response)


@pytest.mark.parametrize("kind,status", [("anonymous", 401), ("csrf", 403), ("origin", 403), ("expected_server", 409)])
def test_description_requires_pve_session_csrf_same_origin_and_expected_mapping(integrated, kind, status):
    browser, headers, command = open_description(integrated)
    if kind == "anonymous":
        headers.pop("cookie")
    elif kind == "csrf":
        headers.pop("x-pve-komodo-csrf")
    elif kind == "origin":
        headers["origin"] = "https://evil.example"
    else:
        command["server_id"] = OTHER
    response = browser.post(BASE + "/description", headers=headers, json=command)
    assert response.status_code == status and integrated["writes"] == []
    assert not integrated["actions_file"].exists()
    assert_private(response)


@pytest.mark.parametrize("outcome,expected", [("timeout", "outcome_unknown"), ("http502", "outcome_unknown"),
                                             ("malformed", "outcome_unknown"), ("denied", "failed")])
def test_description_uncertain_outcomes_are_private_durable_and_never_retried(integrated, outcome, expected):
    browser, headers, command = open_description(integrated)
    integrated["description_response"] = {
        "timeout": httpx.ReadTimeout(PRIVATE), "http502": httpx.Response(502, text=PRIVATE),
        "malformed": httpx.Response(200, json={"error": PRIVATE}), "denied": httpx.Response(403, text=PRIVATE),
    }[outcome]
    first = browser.post(BASE + "/description", headers=headers, json=command)
    assert first.status_code == 200 and first.json()["status"] == expected and first.json()["ok"] is False
    again = browser.post(BASE + "/description", headers=headers, json=command)
    assert again.json() == first.json() and len(integrated["writes"]) == 1
    different = browser.post(BASE + "/description", headers=headers, json={**command, "description": "different"})
    assert different.status_code == 409 and different.json() == {"detail": "idempotency_conflict"}
    assert_private(first)
