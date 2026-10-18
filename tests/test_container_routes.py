import json
import sqlite3
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app.main import Settings, create_app
from app.pve_auth import PveAuthenticator
from plugins.komodo.actions import KomodoActionError
import plugins.komodo.container_routes as routes

SERVER, CONTAINER, VERSION = "a" * 24, "e" * 64, "c" * 64
PATH = f"/pve-plugins/api/komodo/servers/{SERVER}/containers/{CONTAINER}"
COOKIE = {"cookie": "PVEAuthCookie=test-ticket"}


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    state = {"reads": [], "inspect": [], "logs": [], "writes": [], "actions": [], "auth": [], "error": None, "unknown": False}
    ledger = tmp_path / "actions.sqlite3"

    async def auth(self, request):
        state["auth"].append(request.cookies.get("PVEAuthCookie"))
        if request.cookies.get("PVEAuthCookie") != "test-ticket":
            raise HTTPException(401, "pve_auth_required")
        return "root@pam"

    async def read(server_id, container_id, **kwargs):
        state["reads"].append((server_id, container_id))
        return {"server_id": server_id, "container_id": container_id, "version": VERSION, "description_version": VERSION, "can_execute": True}

    async def inspect(server_id, container_id, **kwargs):
        state["inspect"].append((server_id, container_id))
        return {"server_id": server_id, "container_id": container_id, "inspect": {"Id": container_id}}

    async def logs(server_id, container_id, **kwargs):
        state["logs"].append((server_id, container_id, kwargs))
        return {"server_id": server_id, "container_id": container_id, "stdout": "safe", "stderr": ""}

    async def submit(server_id, container_id, **kwargs):
        await kwargs["before_write"]()
        with sqlite3.connect(ledger) as connection:
            assert connection.execute("SELECT COUNT(*) FROM actions WHERE status IS NULL").fetchone()[0] == 1
        target = state["actions"] if "action" in kwargs else state["writes"]
        target.append((server_id, container_id, kwargs))
        if state["unknown"]:
            raise RuntimeError("PRIVATE_UPSTREAM_SECRET")
        if state["error"]:
            raise state["error"]
        if "action" in kwargs:
            return {"section": "containers", "action": kwargs["action"], "results": [{"id": server_id + ":" + container_id, "ok": True, "status": "submitted"}]}
        return {"ok": True, "status": "completed", "operation_id": None, "error_code": None}

    monkeypatch.setattr(PveAuthenticator, "authenticate", auth)
    monkeypatch.setattr(routes, "get_container_details", read)
    monkeypatch.setattr(routes, "get_container_inspect", inspect)
    monkeypatch.setattr(routes, "get_container_logs", logs)
    monkeypatch.setattr(routes, "update_container_description", submit)
    monkeypatch.setattr(routes, "perform_container_detail_action", submit)
    client = TestClient(create_app(Settings("https://pve.example:8006", komodo_url="http://core:9120",
        komodo_api_key="private-key", komodo_api_secret="private-secret", action_state_file=str(ledger))))
    response = client.get(PATH, headers=COOKIE)
    assert response.status_code == 200
    state["headers"] = {**COOKIE, "origin": "http://testserver", "x-pve-plugins-csrf": response.json()["csrf_token"]}
    state["ledger"] = ledger
    return client, state


def command(**changes):
    return {"action": "description", "base_version": VERSION, "description": "NEW-USER-DESCRIPTION", "request_id": str(uuid.uuid4()), **changes}


def test_container_reads_inspect_logs_actions_require_existing_pve_session(boundary):
    client, state = boundary
    count = len(state["reads"])
    for method, suffix in (("GET", ""), ("GET", "/inspect"), ("GET", "/logs"), ("POST", ""), ("POST", "/actions")):
        response = client.request(method, PATH + suffix)
        assert response.status_code == 401 and "no-store" in response.headers["cache-control"]
    assert len(state["reads"]) == count and not state["inspect"] and not state["logs"] and not state["writes"]


@pytest.mark.parametrize("suffix", ["?_dc=1", "?server_id=" + SERVER, "/inspect?foo=1", "/logs?tail=0", "/logs?tail=5001",
    "/logs?tail=1&tail=2", "/logs?timestamps=1", "/logs?invert=TRUE", "/logs?stream=error", "/logs?search=" + "x" * 1001])
def test_strict_queries_fail_before_data_reads(boundary, suffix):
    client, state = boundary
    before = len(state["reads"])
    assert client.get(PATH + suffix, headers=COOKIE).status_code == 422
    assert len(state["reads"]) == before and not state["logs"] and not state["inspect"]


def test_valid_log_filters_and_exact_server_fullid_are_forwarded(boundary):
    client, state = boundary
    response = client.get(PATH + "/logs", params={"tail": "500", "timestamps": "true", "invert": "true", "search": "warning", "stream": "stderr"}, headers=COOKIE)
    assert response.status_code == 200 and "no-store" in response.headers["cache-control"]
    server, container, params = state["logs"][0]
    assert (server, container) == (SERVER, CONTAINER)
    assert params["tail"] == 500 and params["timestamps"] is True and params["invert"] is True
    assert params["search"] == "warning" and params["stream"] == "stderr"
    assert client.get(PATH + "/inspect", headers=COOKIE).json()["inspect"]["Id"] == CONTAINER


@pytest.mark.parametrize("headers", [COOKIE, {**COOKIE, "x-pve-plugins-csrf": "bad"}, {**COOKIE, "origin": "https://evil.example", "x-pve-plugins-csrf": "bad"}])
def test_csrf_and_cross_origin_rejects_writes(boundary, headers):
    client, state = boundary
    assert client.post(PATH, headers=headers, json=command()).status_code == 403
    assert not state["writes"]


@pytest.mark.parametrize("change", [{"action": "exec"}, {"description": 1}, {"description": "x\x00"}, {"description": "x" * 16385},
    {"base_version": "old"}, {"server_id": SERVER}, {"container_id": CONTAINER}, {"config": {}}, {"request_id": "bad"}])
def test_description_request_schema_disallows_extra_target_and_config_fields(boundary, change):
    client, state = boundary
    assert client.post(PATH, headers=state["headers"], json=command(**change)).status_code == 422
    assert not state["writes"]


def test_description_uuid_replay_receipts_persist_without_description_plaintext(boundary):
    client, state = boundary
    data = command()
    first = client.post(PATH, headers=state["headers"], json=data)
    assert first.status_code == 200 and first.json()["ok"]
    assert client.post(PATH, headers=state["headers"], json=data).json() == first.json()
    assert len(state["writes"]) == 1
    assert client.post(PATH, headers=state["headers"], json={**data, "description": "different"}).status_code == 409
    with sqlite3.connect(state["ledger"]) as connection:
        assert "NEW-USER-DESCRIPTION" not in "\n".join(connection.iterdump())
    assert len(state["auth"]) >= 5


@pytest.mark.parametrize("execution", [False, True])
def test_unknown_submission_results_stay_deduplicated_without_private_error_leak(boundary, execution):
    client, state = boundary
    state["unknown"] = True
    data = command()
    if execution:
        data.update(action="restart")
        del data["description"]
    path = PATH + ("/actions" if execution else "")
    first = client.post(path, headers=state["headers"], json=data)
    assert first.status_code == 200 and "outcome_unknown" in first.text and "PRIVATE_UPSTREAM" not in first.text
    assert client.post(path, headers=state["headers"], json=data).json() == first.json()
    assert len(state["actions"] if execution else state["writes"]) == 1


def test_action_failures_are_durable_sanitized_receipts(boundary):
    client, state = boundary
    state["error"] = KomodoActionError("container_version_conflict", 409)
    data = {"action": "restart", "base_version": VERSION, "request_id": str(uuid.uuid4())}
    response = client.post(PATH + "/actions", headers=state["headers"], json=data)
    assert response.status_code == 409 and response.json() == {"detail": "container_version_conflict"}
    assert client.post(PATH + "/actions", headers=state["headers"], json=data).json() == response.json()
    assert len(state["actions"]) == 1


@pytest.mark.parametrize("change", [{"action": "delete"}, {"force": True}, {"volumes": True}, {"base_version": "old"}, {"description": "extra"}])
def test_action_payload_never_accepts_force_volume_flags_or_other_fields(boundary, change):
    client, state = boundary
    data = {"action": "destroy", "base_version": VERSION, "request_id": str(uuid.uuid4()), **change}
    assert client.post(PATH + "/actions", headers=state["headers"], json=data).status_code == 422
    assert state["actions"] == []
