"""Advanced defaults use the same authenticated, revisioned write protocol."""

import copy
import json
import sqlite3

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app
from plugins.safeline.advanced import BOOLS
from test_safeline import BASE, TOKEN, configure
from test_safeline_editing import draft, waf_fixture
from test_site_routes import PVE, pve_transport


@pytest.fixture
def advanced(tmp_path, monkeypatch):
    state, original = waf_fixture()
    state.update(edition={"version": 2, "effective_edition": 2}, groups=False)
    state["global_proxy"] = {k: {"value": False, "global": True} for k in BOOLS}
    state["global_proxy"].update({
        "ip_source": {"value": "xff_dynamic", "global": False},
        "ip_value": {"value": '{"position":"from_start","index":1}', "global": False},
        "host": {"value": "$http_host", "global": False}, "xfp": {"value": "$scheme", "global": True},
        "xfh": {"value": "$http_host", "global": True}, "hsts_max_age": {"value": "15768000", "global": False},
        "ssl_protocols": {"value": "TLSv1.2 TLSv1.3", "global": False}, "ssl_ciphers": {"value": "", "global": False},
        "http_headers": {"value": [{"target": "req", "op": "set", "key": "Authorization", "val": "private-header"}], "global": False},
        "http3": {"value": True, "global": False}, "future_option": {"value": "untouched"}, "updated_at": "first"})

    def handler(request):
        path = request.url.path
        if path not in {"/api/open/global/proxy", "/api/open/system/edition", "/api/open/site/group/switch", "/api/commercial/site/group/switch"}:
            return original.handle_request(request)
        state["waf"].append(request)
        assert request.headers["x-slce-api-token"] == TOKEN
        if request.method == "PUT":
            payload = json.loads(request.content)
            state["writes"].append((request.method, path, payload))
            if state["failure"] == "timeout":
                raise httpx.ReadTimeout(TOKEN)
            if state["failure"] != "mismatch":
                if path == "/api/open/global/proxy": state["global_proxy"].update(payload)
                elif path == "/api/commercial/site/group/switch": state["groups"] = payload["enable"]
                else: pytest.fail(path)
            return httpx.Response(200, json={"data": None, "err": ""})
        value = {"/api/open/global/proxy": state["global_proxy"], "/api/open/system/edition": state["edition"], "/api/open/site/group/switch": state["groups"]}[path]
        return httpx.Response(200, json={"data": value, "err": ""})

    db = tmp_path / "advanced.sqlite3"
    app = create_app(Settings(PVE, enabled_plugins=("safeline",), safeline_state_file=str(db)), pve_auth_transport=pve_transport(state), plugin_transports={"safeline": httpx.MockTransport(handler)})
    with TestClient(app) as client:
        yield client, state, db


def test_advanced_has_pve_auth_csrf_and_no_credential_readback(advanced):
    client, state, _ = advanced
    assert client.get(BASE + "/configuration/proxy").status_code == 401
    headers = configure(client)
    response = client.get(BASE + "/configuration/proxy", headers=headers)
    assert response.status_code == 200 and response.json()["capabilities"]["premium"]
    assert "private-header" not in response.text and TOKEN not in response.text
    row = response.json()["values"]["http_headers"]["value"][0]
    assert row["val"] == "" and row["keep"] == 0
    command = draft(client, "/configuration/proxy", {"gzip": {"global": True, "value": True}}, headers)
    assert client.put(BASE + "/configuration/proxy", json=command, headers={k: v for k, v in headers.items() if k.lower() != "x-pve-plugins-csrf"}).status_code == 403
    assert not state["writes"]


@pytest.mark.parametrize("section,changes,target", [
    ("proxy", {"gzip": {"global": True, "value": True}, "host": {"global": True, "value": "$host"}}, "/api/open/global/proxy"),
    ("proxy", {"ip_source": {"global": False, "value": "proxy_protocol"}, "ip_value": {"global": False, "value": "10.0.0.0/24,2001:db8::1"}}, "/api/open/global/proxy"),
    ("proxy", {"ssl_protocols": {"global": False, "value": "TLSv1.2 TLSv1.3"}}, "/api/open/global/proxy"),
    ("groups", {"enable": True}, "/api/commercial/site/group/switch"),
])
def test_advanced_writes_only_changed_fields_and_deduplicates(advanced, section, changes, target):
    client, state, database = advanced
    headers = configure(client)
    before = copy.deepcopy(state["global_proxy"])
    path = "/configuration/" + section
    command = draft(client, path, changes, headers)
    response = client.put(BASE + path, json=command, headers=headers)
    assert response.status_code == 200 and response.json()["outcome"] == "confirmed", response.text
    assert state["writes"] == [("PUT", target, changes)]
    for key in before.keys() - changes.keys(): assert state["global_proxy"][key] == before[key]
    assert client.put(BASE + path, json=command, headers=headers).json()["replayed"]
    assert len(state["writes"]) == 1
    with sqlite3.connect(database) as db:
        assert "private-header" not in str(db.execute("SELECT * FROM edits").fetchall())


@pytest.mark.parametrize("edition", [0, None, True, 7])
@pytest.mark.parametrize("section,changes", [("proxy", {"ssl_ciphers": {"global": False, "value": "HIGH"}}), ("proxy", {"http_headers": {"global": False, "value": []}}), ("groups", {"enable": True})])
def test_personal_or_unknown_effective_edition_cannot_use_paid_settings(advanced, edition, section, changes):
    client, state, _ = advanced
    state["edition"] = {"version": 2, "effective_edition": edition}
    headers = configure(client)
    path = "/configuration/" + section
    command = draft(client, path, changes, headers)
    response = client.put(BASE + path, json=command, headers=headers)
    assert response.status_code == 409 and response.json()["detail"] == "safeline_feature_unavailable"
    assert not state["writes"]


@pytest.mark.parametrize("changes", [
    {"gzip": {"global": True, "value": 1}}, {"gzip": {"global": "false", "value": True}},
    {"gzip": {"global": True, "value": True, "extra": 1}}, {"http3": {"global": True, "value": False}},
    {"ip_source": {"global": False, "value": "unknown"}},
    {"ip_value": {"global": False, "value": '{"position":"from_start","index":0}'}},
    {"ip_value": {"global": False, "value": '{"position":"from_start","index":true}'}},
    {"ip_value": {"global": False, "value": '{"position":"from_start","index":1,"index":2}'}},
    {"ssl_protocols": {"global": False, "value": "TLSv9"}}, {"ssl_protocols": {"global": False, "value": ""}},
    {"hsts_max_age": {"global": False, "value": "-1"}}, {"hsts_max_age": {"global": False, "value": "9999999999"}},
    {"host": {"global": True, "value": "example.com; bad"}}, {"xfp": {"global": True, "value": "anything"}},
    {"ssl_ciphers": {"global": False, "value": "HIGH\ninvalid"}},
    {"http_headers": {"global": False, "value": [{"target": "req", "op": "set", "key": "X-Test", "val": "a\r\nb"}]}},
    {"http_headers": {"global": False, "value": [{"target": "req", "op": "add", "key": "X-Test", "val": "ok"}]}},
    {"http_headers": {"global": False, "value": [{"target": "res", "op": "set", "key": "Authorization", "val": "", "keep": 0}]}},
])
def test_advanced_invalid_choices_and_header_references_never_write(advanced, changes):
    client, state, _ = advanced
    headers = configure(client)
    command = draft(client, "/configuration/proxy", changes, headers)
    assert client.put(BASE + "/configuration/proxy", json=command, headers=headers).status_code in {409, 422}
    assert not state["writes"]


def test_private_headers_can_be_preserved_replaced_or_removed(advanced):
    client, state, database = advanced
    headers = configure(client)
    path = "/configuration/proxy"
    original = client.get(BASE + path, headers=headers).json()["values"]["http_headers"]
    original["value"].append({"target": "res", "op": "set", "key": "Cache-Control", "val": "no-store"})
    result = client.put(BASE + path, json=draft(client, path, {"http_headers": original}, headers), headers=headers)
    assert result.json()["outcome"] == "confirmed" and state["global_proxy"]["http_headers"]["value"][0]["val"] == "private-header"
    replacement = {"http_headers": {"global": False, "value": [{"target": "req", "op": "set", "key": "Authorization", "val": "new-private"}]}}
    assert client.put(BASE + path, json=draft(client, path, replacement, headers), headers=headers).json()["outcome"] == "confirmed"
    assert "new-private" not in client.get(BASE + path, headers=headers).text
    deletion = {"http_headers": {"global": False, "value": []}}
    assert client.put(BASE + path, json=draft(client, path, deletion, headers), headers=headers).json()["outcome"] == "confirmed"
    with sqlite3.connect(database) as db:
        receipts = str(db.execute("SELECT * FROM edits").fetchall())
    assert "new-private" not in receipts and "private-header" not in receipts


def test_concurrent_changes_and_license_downgrade_reject_old_forms(advanced):
    client, state, _ = advanced
    headers = configure(client)
    path = "/configuration/proxy"
    command = draft(client, path, {"gzip": {"global": True, "value": True}}, headers)
    state["global_proxy"]["updated_at"] = "another writer"
    assert client.put(BASE + path, json=command, headers=headers).json()["detail"] == "safeline_resource_changed"
    command = draft(client, path, {"ssl_ciphers": {"global": False, "value": "HIGH"}}, headers)
    state["edition"]["effective_edition"] = 0
    assert client.put(BASE + path, json=command, headers=headers).json()["detail"] == "safeline_resource_changed"
    assert not state["writes"]


@pytest.mark.parametrize("failure", ["timeout", "mismatch"])
def test_unconfirmed_global_writes_are_not_automatically_resent(advanced, failure):
    client, state, _ = advanced
    headers = configure(client)
    path = "/configuration/proxy"
    command = draft(client, path, {"gzip": {"global": True, "value": True}}, headers)
    state["failure"] = failure
    assert client.put(BASE + path, json=command, headers=headers).json()["outcome"] == "unknown"
    assert client.put(BASE + path, json=command, headers=headers).json()["replayed"]
    assert len(state["writes"]) == 1
