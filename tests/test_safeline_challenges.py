"""Human verification history and revisioned global challenge service settings."""

import copy
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app
from test_safeline import BASE, COOKIE, TOKEN, configure
from test_safeline_editing import draft, waf_fixture
from test_site_routes import PVE, pve_transport


@pytest.fixture
def challenges(tmp_path, monkeypatch):
    state, original = waf_fixture()
    state.update(edition={"version": 2, "effective_edition": 2}, challenge={"server": "cloud", "future": "preserved"}, listing=None)
    state["history"] = [
        {"id": 4, "event_id": "challenge-4", "ip": "2001:db8::1", "site_id": 12, "rule_id": 32,
         "site_comment": "Admin", "site_title": "Application", "site_server_names": ["app.example"],
         "country": "CN", "province": "福建省", "city": "廈門市", "trigger_count": 18, "pass_count": 0,
         "started_at": 1790146412232, "ended_at": 1790146413232, "dur_sec": 1,
         "site_icon": "private-image", "unknown": TOKEN, "req_header": "Cookie: private-cookie"},
        {"id": 3, "event_id": "challenge-3", "ip": "192.0.2.2", "site_id": 0, "site_server_names": None,
         "trigger_count": 0, "pass_count": 0, "started_at": 1790146000000, "ended_at": 0, "dur_sec": 80},
    ]

    def handler(request):
        path = request.url.path
        if path not in {"/api/open/records/challenge", "/api/open/challenge/config", "/api/open/system/edition"}:
            return original.handle_request(request)
        state["waf"].append(request)
        assert request.headers["x-slce-api-token"] == TOKEN
        assert not {"cookie", "authorization", "x-pve-plugins-csrf"} & set(request.headers)
        if request.method == "POST":
            assert path == "/api/open/challenge/config"
            payload = json.loads(request.content); state["writes"].append((request.method, path, payload))
            if state["failure"] == "timeout":
                raise httpx.ReadTimeout(TOKEN)
            if state["failure"] == "business":
                return httpx.Response(200, json={"data": None, "err": "business", "msg": TOKEN})
            if state["failure"] != "mismatch":
                state["challenge"].update(payload)
            value = state["challenge"]
        elif path == "/api/open/system/edition":
            value = state["edition"]
        elif path == "/api/open/challenge/config":
            value = state["challenge"]
        elif state["listing"] is not None:
            value = state["listing"]
        else:
            items = copy.deepcopy(state["history"]); params = request.url.params
            if "ip" in params: items = [row for row in items if row["ip"] == params["ip"]]
            if "site" in params: items = [row for row in items if params["site"] in row.get("site_comment", "")]
            if "started_at_begin" in params: items = [row for row in items if row["started_at"] >= int(params["started_at_begin"])]
            if "started_at_end" in params: items = [row for row in items if row["started_at"] <= int(params["started_at_end"])]
            offset = (int(params["page"]) - 1) * int(params["page_size"])
            value = {"data": items[offset:offset + int(params["page_size"])], "total": len(items)}
        return httpx.Response(200, json={"data": copy.deepcopy(value), "err": ""})

    monkeypatch.setenv("SAFELINE_STATE_FILE", str(tmp_path / "challenges.sqlite3"))
    app = create_app(Settings(PVE, enabled_plugins=("safeline",)), pve_auth_transport=pve_transport(state),
                     plugin_transports={"safeline": httpx.MockTransport(handler)})
    with TestClient(app) as client:
        yield client, state


def test_challenge_history_uses_one_bounded_read_and_omits_unneeded_or_private_fields(challenges):
    client, state = challenges; headers = configure(client)
    result = client.get(BASE + "/challenges?page=1&page_size=1", headers=headers)
    assert result.status_code == 200
    data = result.json(); row = data["items"][0]
    assert data["total"] == 2 and len(data["items"]) == 1
    assert row["site_name"] == "Admin" and row["domains"] == ["app.example"]
    assert row["started_at"] == 1790146412232 and row["dur_sec"] == 1 and row["pass_count"] == 0
    assert len(state["waf"]) == 1 and state["waf"][0].url.path == "/api/open/records/challenge"
    assert all(value not in result.text for value in (TOKEN, "private-cookie", "private-image", "site_icon", "req_header"))
    next_page = client.get(BASE + "/challenges?page=2&page_size=1", headers=headers).json()["items"][0]
    assert next_page["site_name"] == "—" and next_page["domains"] == [] and next_page["ended_at"] == 0
    assert next_page["trigger_count"] == 0 and next_page["pass_count"] == 0


def test_challenge_filters_use_application_text_and_millisecond_start_boundaries(challenges):
    client, state = challenges; headers = configure(client)
    result = client.get(BASE + "/challenges", params={"ip": "2001:db8::1", "site": "Admin", "start": 1790146412232, "end": 1790146412232}, headers=headers)
    assert result.status_code == 200 and result.json()["total"] == 1
    assert dict(state["waf"][-1].url.params) == {"page": "1", "page_size": "20", "ip": "2001:db8::1", "site": "Admin",
                                                "started_at_begin": "1790146412232", "started_at_end": "1790146412232"}
    assert client.get(BASE + "/challenges?site=missing", headers=headers).json()["total"] == 0
    assert not state["writes"]


@pytest.mark.parametrize("query", ["page=0", "page_size=101", "page_size=0", "page=1&page=2", "source=general", "host=x",
                                  "site=", "site=a&site=b", "ip=%7F", "site=%0A", "start=-1", "start=2&end=1", "start=1.5", "url=https://other.example"])
def test_invalid_challenge_filters_never_reach_waf(challenges, query):
    client, state = challenges; headers = configure(client)
    assert client.get(BASE + "/challenges?" + query, headers=headers).status_code == 422
    assert not state["waf"]


@pytest.mark.parametrize("listing", [{"data": [], "total": True}, {"data": [], "total": 1.5}, {"data": [], "total": -1},
                                    {"data": [{}], "total": 0}, {"data": [1], "total": 1}, {"nodes": [], "total": 0}])
def test_malformed_history_does_not_become_a_successful_empty_page(challenges, listing):
    client, state = challenges; headers = configure(client); state["listing"] = listing
    assert client.get(BASE + "/challenges", headers=headers).status_code == 502


def test_history_marks_invalid_counts_unknown_and_redacts_strings(challenges):
    client, state = challenges; headers = configure(client)
    state["history"][0].update(trigger_count=True, pass_count=-1, dur_sec="5", site_comment=TOKEN)
    row = client.get(BASE + "/challenges", headers=headers).json()["items"][0]
    assert row["trigger_count"] is None and row["pass_count"] is None and row["dur_sec"] is None
    assert row["site_name"] == "[REDACTED]"


@pytest.mark.parametrize("method,path", [("GET", "/challenges"), ("GET", "/configuration/challenge"), ("PUT", "/configuration/challenge")])
def test_challenge_routes_authorize_before_storage_and_upstream(challenges, method, path):
    client, state = challenges
    assert client.request(method, BASE + path, json={}).status_code == 401
    assert not state["waf"]


def test_global_challenge_service_is_csrf_protected_and_verifies_a_single_write(challenges):
    client, state = challenges; headers = configure(client)
    body = draft(client, "/configuration/challenge", {"server": "local"}, headers)
    assert client.put(BASE + "/configuration/challenge", json=body, headers={"cookie": COOKIE}).status_code == 403
    assert not state["writes"]
    result = client.put(BASE + "/configuration/challenge", json=body, headers=headers)
    assert result.status_code == 200 and result.json()["outcome"] == "confirmed"
    assert state["writes"] == [("POST", "/api/open/challenge/config", {"server": "local"})]
    assert state["challenge"] == {"server": "local", "future": "preserved"}
    count = len(state["waf"])
    assert client.put(BASE + "/configuration/challenge", json=body, headers=headers).json()["replayed"] is True
    assert len(state["waf"]) == count


@pytest.mark.parametrize("edition", [0, None, True, 5])
def test_challenge_service_change_respects_effective_edition(challenges, edition):
    client, state = challenges; headers = configure(client); state["edition"]["effective_edition"] = edition
    data = client.get(BASE + "/configuration/challenge", headers=headers).json()
    assert data["values"] == {"server": "cloud"} and data["locked"] == ["server"]
    body = {"expected_revision": data["connection_revision"], "fingerprint": data["fingerprint"], "operation_id": str(uuid4()), "changes": {"server": "local"}}
    assert client.put(BASE + "/configuration/challenge", json=body, headers=headers).status_code == 409
    assert not state["writes"]


@pytest.mark.parametrize("changes", [{"server": "external"}, {"server": []}, {"server": True}, {"server": "local", "url": "https://other.example"}, {}])
def test_only_documented_challenge_service_choices_can_be_written(challenges, changes):
    client, state = challenges; headers = configure(client)
    body = draft(client, "/configuration/challenge", changes, headers)
    assert client.put(BASE + "/configuration/challenge", json=body, headers=headers).status_code in {409, 422}
    assert not state["writes"]


def test_stale_configuration_or_connection_prevents_challenge_service_changes(challenges):
    client, state = challenges; headers = configure(client)
    body = draft(client, "/configuration/challenge", {"server": "local"}, headers)
    state["challenge"]["future"] = "changed elsewhere"
    assert client.put(BASE + "/configuration/challenge", json=body, headers=headers).status_code == 409
    body = draft(client, "/configuration/challenge", {"server": "local"}, headers); body["expected_revision"] += 1
    assert client.put(BASE + "/configuration/challenge", json=body, headers=headers).status_code == 409
    assert not state["writes"]


@pytest.mark.parametrize("failure,outcome", [("timeout", "unknown"), ("mismatch", "unknown"), ("business", "failed")])
def test_uncertain_challenge_writes_are_not_retried_or_reported_as_saved(challenges, failure, outcome):
    client, state = challenges; headers = configure(client); state["failure"] = failure
    body = draft(client, "/configuration/challenge", {"server": "local"}, headers)
    response = client.put(BASE + "/configuration/challenge", json=body, headers=headers)
    assert response.status_code == 200 and response.json()["outcome"] == outcome and TOKEN not in response.text
    assert client.put(BASE + "/configuration/challenge", json=body, headers=headers).json()["replayed"] is True
    assert len(state["writes"]) == 1
