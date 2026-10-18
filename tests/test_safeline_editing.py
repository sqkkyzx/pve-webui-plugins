"""Resource editing uses real routes with a stateful, contract-checked WAF transport."""

import copy
import json
import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app
from plugins.safeline.client import SafeLineClient
from plugins.safeline.connection import Connection, ConnectionStore, SafeLineError
from test_safeline import BASE, TOKEN, URL, configure
from test_site_routes import COOKIE, PVE, pve_transport


CRT = "-----BEGIN CERTIFICATE-----\nYWJj\n-----END CERTIFICATE-----"
NEW_CRT = "-----BEGIN CERTIFICATE-----\nZGVm\n-----END CERTIFICATE-----"


def waf_fixture():
    state = {"requests": [], "waf": [], "writes": [], "failure": None}
    state["site"] = {"id": 12, "server_names": ["app.example"], "ports": ["443_ssl"], "comment": "app", "mode": 0,
        "upstreams": ["https://one:443", "https://two:443"], "is_enabled": True, "cert_id": 5, "health_check": True,
        "health_check_interval": 30, "email": "owner@example.com", "group_id": 6, "index": "index.html", "type": 0,
        "stat_enabled": True, "static_default": 0, "redirect_status_code": 302, "updated_at": "version-1", "req_value": 100,
        "load_balance": {"balance_type": 2, "upstreams": [{"address": "https://one:443", "weight": 7, "role": "primary"}, {"address": "https://two:443", "weight": 3, "role": "backup"}]}}
    state["policy"] = {"id": 2, "action": 1, "name": "custom", "is_enabled": True, "builtin": False, "expire": 123456789,
        "log": True, "level": 0, "pattern": [[{"k": "src_ip", "op": "in", "sub_k": "", "v": ["10.0.0.1"]}]],
        "order": 5, "negate": True, "updated_at": "version-1", "req_count": 100}
    state["group"] = {"id": 7, "comment": "office", "ips": ["10.0.0.0/24"], "total": 1, "builtin": False, "reference": "in-use", "updated_at": "version-1"}
    state["proxy"] = {"gzip": {"global": True, "value": True}, "host": {"global": False, "value": "internal.example"},
        "ntlm": {"global": False, "value": True}, "http_headers": {"global": False, "value": [{"key": "Authorization", "val": "private-header", "target": "request", "op": "set"}]}, "global": {"gzip": False}}
    state["certificate"] = {"type": 2, "manual": {"crt": CRT, "key": "private-certificate-key"}, "acme": {"domains": ["app.example"], "email": ""}}

    def handler(request):
        state["waf"].append(request)
        assert request.headers["x-slce-api-token"] == TOKEN
        assert not {"cookie", "authorization", "x-pve-plugins-csrf", "csrfpreventiontoken"} & set(request.headers)
        path, method = request.url.path, request.method
        if method != "GET":
            payload = json.loads(request.content)
            state["writes"].append((method, path, payload))
            if state["failure"] == "timeout":
                raise httpx.ReadTimeout("private-upstream-body " + TOKEN)
            if state["failure"] == "business":
                return httpx.Response(200, json={"data": None, "err": "private-failure", "msg": TOKEN})
            if state["failure"] != "mismatch":
                if path == "/api/open/site": state["site"].update(payload)
                elif path == "/api/open/site/mode":
                    assert set(payload) == {"ids", "mode"} and payload["ids"] == [12]
                    state["site"]["mode"] = payload["mode"]
                elif path == "/api/open/site/12/proxy": state["proxy"].update(payload)
                elif path == "/api/open/policy": state["policy"].update(payload)
                elif path == "/api/open/policy/switch":
                    assert set(payload) == {"id", "is_enabled"} and payload["id"] == 2
                    state["policy"]["is_enabled"] = payload["is_enabled"]
                elif path == "/api/open/ipgroup":
                    state["group"].update(payload); state["group"]["total"] = len(state["group"]["ips"])
                elif path == "/api/open/cert":
                    assert method == "POST" and payload["id"] == 5
                    state["certificate"].update(payload)
                else: pytest.fail("Unexpected mutation: " + path)
            return httpx.Response(200, json={"data": None, "err": "", "msg": "ok"})
        if path == "/api/open/site/12": value = state["site"]
        elif path == "/api/open/system/edition": value = {"effective_edition": 2}
        elif path == "/api/open/site/12/proxy": value = state["proxy"]
        elif path == "/api/open/site": value = {"data": [state["site"]], "total": 1}
        elif path == "/api/open/policy/detail": value = state["policy"]
        elif path == "/api/open/ipgroup/detail": value = {"data": state["group"]}
        elif path == "/api/open/cert/5": value = state["certificate"]
        elif path == "/api/open/cert": value = {"nodes": [{"id": 5, "type": state["certificate"]["type"], "domains": ["app.example"]}], "total": 1}
        else: pytest.fail("Unexpected read: " + path)
        return httpx.Response(200, json={"data": value, "err": "", "msg": "ok"})

    return state, httpx.MockTransport(handler)


@pytest.fixture
def editing(tmp_path, monkeypatch):
    path = tmp_path / "private" / "connection.sqlite3"
    for name in ("SAFELINE_URL", "SAFELINE_API_TOKEN", "SAFELINE_TLS_SERVER_NAME", "SAFELINE_CA_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SAFELINE_STATE_FILE", str(path))
    state, transport = waf_fixture()
    app = create_app(Settings(PVE, enabled_plugins=("safeline",)), pve_auth_transport=pve_transport(state), plugin_transports={"safeline": transport})
    with TestClient(app) as client:
        yield client, state, path


def draft(client, path, changes, headers):
    loaded = client.get(BASE + path, headers=headers)
    assert loaded.status_code == 200, loaded.text
    value = loaded.json()
    return {"expected_revision": value["connection_revision"], "fingerprint": value["fingerprint"], "operation_id": str(uuid4()), "changes": changes}


@pytest.mark.parametrize("path,changes,target", [
    ("/resources/applications/12/edit", {"comment": "new", "server_names": ["new.example"], "ports": ["80", "443_ssl"], "cert_id": 0}, "/api/open/site"),
    ("/resources/applications/12/edit?section=mode", {"mode": 2}, "/api/open/site/mode"),
    ("/resources/applications/12/edit?section=proxy", {"gzip": {"global": False, "value": False}}, "/api/open/site/12/proxy"),
    ("/resources/deny/2/edit", {"name": "new", "log": False}, "/api/open/policy"),
    ("/resources/deny/2/edit?section=switch", {"is_enabled": False}, "/api/open/policy/switch"),
    ("/resources/ip-groups/7/edit", {"comment": "new", "ips": ["10.0.0.2", "2001:db8::/64", "10.0.1.1-10.0.1.9"]}, "/api/open/ipgroup"),
    ("/resources/certificates/5/edit", {"crt": NEW_CRT, "key": ""}, "/api/open/cert"),
])
def test_edits_preserve_untouched_fields_read_back_and_do_not_replay(editing, path, changes, target):
    client, state, database = editing
    headers = configure(client)
    before = copy.deepcopy(state)
    command = draft(client, path, changes, headers)
    result = client.put(BASE + path, json=command, headers=headers)
    assert result.status_code == 200 and result.json()["outcome"] == "confirmed", result.text
    assert result.json()["replayed"] is False
    assert len(state["writes"]) == 1 and state["writes"][0][1] == target
    payload = state["writes"][0][2]
    if target == "/api/open/site":
        for name in ("email", "group_id", "load_balance", "health_check", "health_check_interval", "static_default", "redirect_status_code", "type", "index", "stat_enabled", "upstreams"):
            assert payload[name] == before["site"][name]
    elif target == "/api/open/policy":
        for name in ("pattern", "level", "expire", "is_enabled", "action"):
            assert payload[name] == before["policy"][name]
        assert state["policy"]["negate"] is True and state["policy"]["order"] == 5
    elif target.endswith("/proxy"):
        assert payload["http_headers"] == before["proxy"]["http_headers"] and payload["ntlm"] == before["proxy"]["ntlm"]
    replayed = client.put(BASE + path, json=command, headers=headers)
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True and len(state["writes"]) == 1
    with sqlite3.connect(database) as db:
        receipts = str(db.execute("SELECT * FROM edits").fetchall())
    for secret in (TOKEN, "private-certificate-key", "private-header", NEW_CRT):
        assert secret not in result.text and secret not in receipts


def test_upstream_changes_retain_load_balance_weights_and_roles(editing):
    client, state, _ = editing
    headers = configure(client)
    path = "/resources/applications/12/edit"
    command = draft(client, path, {"upstreams": ["https://two:443", "https://new:443"]}, headers)
    assert client.put(BASE + path, json=command, headers=headers).json()["outcome"] == "confirmed"
    assert state["site"]["load_balance"] == {"balance_type": 2, "upstreams": [{"address": "https://two:443", "weight": 3, "role": "backup"}, {"address": "https://new:443", "weight": 1, "role": "primary"}]}


def test_sensitive_conditions_cannot_be_exported_or_accidentally_replaced(editing):
    client, state, _ = editing
    headers = configure(client)
    state["policy"]["pattern"] = [[{"k": "req_header", "op": "eq", "sub_k": "Authorization", "v": ["secret-header-value"]}]]
    path = "/resources/deny/2/edit"
    response = client.get(BASE + path, headers=headers)
    assert "secret-header-value" not in response.text and "pattern" not in response.json()["values"]
    command = draft(client, path, {"name": "new"}, headers)
    assert client.put(BASE + path, json=command, headers=headers).json()["outcome"] == "confirmed"
    assert state["policy"]["pattern"][0][0]["v"] == ["secret-header-value"]
    command = draft(client, path, {"pattern": [[{"k": "src_ip", "op": "eq", "v": ["10.0.0.1"]}]]}, headers)
    assert client.put(BASE + path, json=command, headers=headers).status_code == 409
    assert len(state["writes"]) == 1


@pytest.mark.parametrize("path,state_key,changes", [
    ("/resources/applications/12/edit", "site", {"comment": "new"}),
    ("/resources/deny/2/edit", "policy", {"name": "new"}),
    ("/resources/ip-groups/7/edit", "group", {"comment": "new"}),
])
def test_other_writers_and_connection_revisions_reject_stale_forms(editing, path, state_key, changes):
    client, state, _ = editing
    headers = configure(client)
    command = draft(client, path, changes, headers)
    state[state_key]["comment" if state_key != "policy" else "name"] = "concurrent"
    result = client.put(BASE + path, json=command, headers=headers)
    assert result.status_code == 409 and result.json()["detail"] == "safeline_resource_changed"
    command = draft(client, path, changes, headers)
    command["expected_revision"] = 0
    assert client.put(BASE + path, json=command, headers=headers).status_code == 409
    assert not state["writes"]


@pytest.mark.parametrize("failure,outcome", [("timeout", "unknown"), ("mismatch", "unknown"), ("business", "failed")])
def test_uncertain_writes_and_rejections_have_durable_non_replayable_receipts(editing, failure, outcome):
    client, state, database = editing
    headers = configure(client)
    path = "/resources/applications/12/edit?section=mode"
    command = draft(client, path, {"mode": 2}, headers)
    state["failure"] = failure
    response = client.put(BASE + path, json=command, headers=headers)
    assert response.status_code == 200 and response.json()["outcome"] == outcome, response.text
    state["failure"] = None
    assert client.put(BASE + path, json=command, headers=headers).json()["outcome"] == outcome
    assert len(state["writes"]) == 1 and TOKEN not in response.text
    command["changes"] = {"mode": 1}
    assert client.put(BASE + path, json=command, headers=headers).status_code == 409


@pytest.mark.parametrize("method,headers,status", [("GET", {}, 401), ("PUT", {}, 401), ("PUT", {"cookie": COOKIE}, 403),
    ("PUT", {"cookie": COOKIE, "origin": "https://evil.example", "x-pve-plugins-csrf": "invalid"}, 403)])
def test_resource_edits_fail_authentication_before_storage_or_waf_io(editing, method, headers, status):
    client, state, database = editing
    result = client.request(method, BASE + "/resources/applications/12/edit", headers=headers, json={"changes": {}})
    assert result.status_code == status and not state["waf"] and not database.exists()
    assert result.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("path,changes", [
    ("applications/12", {"id": 99}), ("applications/12", {"server_names": ["https://bad/path"]}),
    ("applications/12", {"ports": ["65536_ssl"]}), ("applications/12", {"cert_id": True}),
    ("applications/12", {"upstreams": ["https://user:secret@host:443"]}),
    ("applications/12?section=mode", {"mode": True}), ("applications/12?section=mode", {"mode": 9}),
    ("deny/2", {"action": 0}), ("deny/2", {"pattern": []}), ("deny/2", {"log": "false"}),
    ("ip-groups/7", {"ips": ["anything"]}), ("ip-groups/7", {"ips": ["10.0.0.9-10.0.0.1"]}),
    ("certificates/5", {"crt": "private-invalid-certificate"}),
])
def test_invalid_and_overbroad_changes_never_reach_a_mutation(editing, path, changes):
    client, state, _ = editing
    headers = configure(client)
    resource, _, query = path.partition("?")
    path = "/resources/" + resource + "/edit" + ("?" + query if query else "")
    command = draft(client, path, changes, headers)
    response = client.put(BASE + path, json=command, headers=headers)
    assert response.status_code in {409, 422}, response.text
    assert not state["writes"] and "private-invalid-certificate" not in response.text


def test_certificates_only_expose_empty_private_key_inputs_and_support_acme(editing):
    client, state, database = editing
    headers = configure(client)
    path = "/resources/certificates/5/edit"
    response = client.get(BASE + path, headers=headers)
    assert response.json()["values"] == {"crt": "", "key": ""} and "private-certificate-key" not in response.text
    state["certificate"]["type"] = 3
    command = draft(client, path, {"domains": ["new.example"], "email": "admin@example.com"}, headers)
    assert client.put(BASE + path, json=command, headers=headers).json()["outcome"] == "confirmed"
    assert state["writes"][0][2] == {"id": 5, "type": 3, "acme": {"domains": ["new.example"], "email": "admin@example.com"}}


def test_incomplete_and_builtin_resources_do_not_enable_unsafe_replacement(editing):
    client, state, _ = editing
    headers = configure(client)
    state["group"]["total"] = 10001
    assert client.get(BASE + "/resources/ip-groups/7/edit", headers=headers).status_code == 409
    state["policy"]["builtin"] = True
    assert client.get(BASE + "/resources/deny/2/edit", headers=headers).status_code == 409
    assert client.get(BASE + "/resources/deny/2/edit?section=switch", headers=headers).status_code == 200
    assert not state["writes"]


def test_pending_reservations_survive_restarts_and_do_not_store_plaintext(editing):
    client, state, path = editing
    headers = configure(client)
    store = ConnectionStore(path, PVE)
    operation = str(uuid4())
    store.reserve_edit(operation, "root@pam", "hash-only", "target-only")
    store.close()
    store = ConnectionStore(path, PVE)
    assert store.receipt(operation, "root@pam", "hash-only")["outcome"] == "unknown"
    with pytest.raises(SafeLineError): store.reserve_edit(str(uuid4()), "root@pam", "new-hash", "target-only")
    store.close()


@pytest.mark.parametrize("method,path", [("DELETE", "/api/open/site"), ("POST", "/api/open/site"), ("PUT", "/api/open/global/mode"), ("PUT", "/api/open/site/12/resources")])
def test_write_allowlist_is_narrow(method, path):
    async def invoke():
        await SafeLineClient(Connection(URL, TOKEN)).write(method, path, {})
    import asyncio
    with pytest.raises(SafeLineError, match="invalid_safeline_endpoint"):
        asyncio.run(invoke())
