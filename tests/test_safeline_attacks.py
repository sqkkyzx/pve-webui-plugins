"""Attack queries/details and IP membership use their real route/auth/write contracts."""

import copy
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app
from plugins.safeline.attacks import packet_body
from test_safeline import BASE, TOKEN, configure
from test_site_routes import COOKIE, PVE, pve_transport


@pytest.fixture
def attacks(tmp_path, monkeypatch):
    for name in ("SAFELINE_URL", "SAFELINE_API_TOKEN", "SAFELINE_TLS_SERVER_NAME", "SAFELINE_CA_FILE"):
        monkeypatch.delenv(name, raising=False)
    state = {"requests": [], "waf": [], "writes": [], "failure": None}
    state["groups"] = {i: {"id": i, "comment": "group-" + str(i), "ips": ["10.0.0.1"], "total": 1,
                            "builtin": i == 2, "reference": "https://sync.example/list" if i == 3 else ""} for i in range(1, 5)}
    state["record"] = {"event_id": "log-uuid", "timestamp": 1792185349, "action": 0, "attack_type": 8,
                       "src_ip": "192.0.2.3", "host": "app.example", "url_path": "/login", "module": "m_php_code_injection",
                       "country": "CN", "province": "福建省", "city": "廈門市", "ja4_fingerprint": "t13_fingerprint",
                       "req_header": "POST /login HTTP/1.1\r\nHost: app.example\r\nContent-Type: application/json\r\nAuthorization: Bearer private-auth\r\n folded-auth\r\nCookie: private-cookie",
                       "req_body": json.dumps({"query": "normal request", "password": "private-password", "nested": {"token": TOKEN}}),
                       "rsp_header": "HTTP/1.1 200 OK\r\nSet-Cookie: private-set-cookie", "rsp_body": "<html>response</html>",
                       "location": "headers", "decode_path": "unquote", "payload": "<script>payload</script>"}

    def handler(request):
        state["waf"].append(request)
        assert request.headers["x-slce-api-token"] == TOKEN
        assert not {"cookie", "authorization", "x-pve-plugins-csrf"} & set(request.headers)
        path = request.url.path
        if request.method != "GET":
            body = json.loads(request.content); state["writes"].append((path, body))
            if state["failure"] == "timeout":
                raise httpx.ReadTimeout("private-error " + TOKEN)
            if state["failure"] == "business":
                return httpx.Response(200, json={"data": None, "err": "no", "msg": TOKEN})
            if path == "/api/open/ipgroup/append":
                assert set(body) == {"ip_group_ids", "ips"}
                for i in body["ip_group_ids"]:
                    if state["failure"] != "partial" or i == 1:
                        state["groups"][i]["ips"].extend(v for v in body["ips"] if v not in state["groups"][i]["ips"])
                value = None
            elif path == "/api/open/ipgroup":
                assert set(body) == {"comment", "ips"}
                value = 5
                state["groups"][5] = {"id": 5, **body, "builtin": False, "reference": ""}
            else:
                pytest.fail("Unexpected write: " + path)
        elif path == "/api/open/ipgroup":
            value = {"nodes": list(state["groups"].values()), "total": len(state["groups"])}
        elif path == "/api/open/ipgroup/detail":
            group = state["groups"][int(request.url.params["id"])]
            value = {"data": {**group, "total": len(group["ips"])}}
        elif path.startswith("/api/open/record/"):
            value = state["record"]
        elif path.startswith("/api/open/records"):
            value = {"data": [state["record"]], "total": 1}
        else:
            pytest.fail("Unexpected read: " + path)
        return httpx.Response(200, json={"data": copy.deepcopy(value), "err": "", "msg": "ok"})

    app = create_app(Settings(PVE, enabled_plugins=("safeline",), safeline_state_file=str(tmp_path / "private/connection.sqlite3")), pve_auth_transport=pve_transport(state),
                     plugin_transports={"safeline": httpx.MockTransport(handler)})
    with TestClient(app) as client:
        yield client, state


def draft(client, headers, changes):
    response = client.get(BASE + "/ip-groups/append", headers=headers)
    assert response.status_code == 200
    value = response.json()
    return {"expected_revision": value["connection_revision"], "fingerprint": value["fingerprint"],
            "operation_id": str(uuid4()), "changes": changes}


@pytest.mark.parametrize("source", ["general", "rule"])
def test_detail_uses_log_string_id_and_bounds_redacts_packets(attacks, source):
    client, state = attacks; configure(client)
    result = client.get(BASE + "/records/log-uuid?source=" + source, headers={"cookie": COOKIE})
    assert result.status_code == 200
    record = result.json()["record"]
    assert record["module"] == "m_php_code_injection" and record["ja4_fingerprint"] == "t13_fingerprint"
    assert "normal request" in record["req_body"] and "response" in record["rsp_body"]
    assert "Host: app.example" in record["req_header"] and record["payload"] == "<script>payload</script>"
    assert all(secret not in result.text for secret in (TOKEN, "private-auth", "folded-auth", "private-cookie", "private-password", "private-set-cookie"))
    assert state["waf"][-1].url.path == "/api/open/record/" + ("rule/" if source == "rule" else "") + "log-uuid"
    state["record"]["req_body"] = "x" * 40000
    value = client.get(BASE + "/records/log-uuid", headers={"cookie": COOKIE}).json()["record"]
    assert len(value["req_body"]) == 32768 and value["content_truncated"] is True


def test_detail_identity_mismatch_does_not_return_another_request(attacks):
    client, state = attacks; configure(client)
    assert client.get(BASE + "/records/different-id", headers={"cookie": COOKIE}).status_code == 404


def test_ja4_and_multiple_attack_type_filters_are_forwarded_without_row_detail_calls(attacks):
    client, state = attacks; configure(client)
    response = client.get(BASE + "/records?ja4_fingerprint=t13_fingerprint&attack_type=0,8&action=0&start=1000&end=2000", headers={"cookie": COOKIE})
    assert response.status_code == 200
    assert len(state["waf"]) == 1 and state["waf"][0].url.params["attack_type"] == "0,8"
    assert state["waf"][0].url.params["ja4_fingerprint"] == "t13_fingerprint"
    assert response.json()["module_in_list"] is True
    assert "req_body" not in response.json()["items"][0]


@pytest.mark.parametrize("method,path", [("GET", "/records/log-uuid"), ("GET", "/ip-groups/append"), ("POST", "/ip-groups/append"), ("POST", "/ip-groups")])
def test_new_attack_routes_require_pve_auth_before_storage_or_waf(attacks, method, path):
    client, state = attacks
    assert client.request(method, BASE + path, json={}).status_code == 401
    assert not state["waf"]


@pytest.mark.parametrize("path", ["/records/log-uuid?source=bad", "/records/log%20id", "/records/log-uuid?source=general&source=rule", "/events?ja4_fingerprint=abc", "/ip-groups/append?url=https://other.example"])
def test_attack_query_allowlists_reject_unverified_fields_and_paths(attacks, path):
    client, state = attacks; configure(client)
    assert client.get(BASE + path, headers={"cookie": COOKIE}).status_code == 422
    assert not state["waf"]


def test_append_options_exclude_builtin_and_synchronized_groups(attacks):
    client, state = attacks; headers = configure(client)
    response = client.get(BASE + "/ip-groups/append", headers=headers)
    assert response.json()["groups"] == [{"id": 1, "name": "group-1"}, {"id": 4, "name": "group-4"}]
    assert "ips" not in response.text and not state["writes"]


def test_append_is_atomic_endpoint_not_a_membership_replacement_and_replay_is_read_only(attacks):
    client, state = attacks; headers = configure(client)
    body = draft(client, headers, {"ip": "2001:db8:0:0::1", "ip_group_ids": [1, 4]})
    response = client.post(BASE + "/ip-groups/append", json=body, headers=headers)
    assert response.status_code == 200 and response.json()["outcome"] == "confirmed"
    assert state["writes"] == [("/api/open/ipgroup/append", {"ips": ["2001:db8::1"], "ip_group_ids": [1, 4]})]
    assert all("10.0.0.1" in state["groups"][i]["ips"] and "2001:db8::1" in state["groups"][i]["ips"] for i in (1, 4))
    count = len(state["waf"])
    assert client.post(BASE + "/ip-groups/append", json=body, headers=headers).json()["replayed"] is True
    assert len(state["waf"]) == count


def test_create_group_with_address_is_one_explicit_write_and_verified(attacks):
    client, state = attacks; headers = configure(client)
    body = draft(client, headers, {"ip": "192.0.2.17/24", "comment": "new group"})
    response = client.post(BASE + "/ip-groups", json=body, headers=headers)
    assert response.status_code == 200 and response.json()["outcome"] == "confirmed"
    assert state["writes"] == [("/api/open/ipgroup", {"ips": ["192.0.2.0/24"], "comment": "new group"})]


@pytest.mark.parametrize("changes", [{"ip": "bad", "ip_group_ids": [1]}, {"ip": "192.0.2.1", "ip_group_ids": []},
                                     {"ip": "192.0.2.1", "ip_group_ids": [1, 1]}, {"ip": "192.0.2.1", "ip_group_ids": [True]},
                                     {"ip": "192.0.2.1", "ip_group_ids": [2]}, {"ip": "192.0.2.1", "ip_group_ids": [3]},
                                     {"ip": "192.0.2.1", "ip_group_ids": [99]}, {"ip": "192.0.2.1", "ip_group_ids": [1], "url": "https://evil"}])
def test_invalid_membership_cannot_write_or_edit_builtin_groups(attacks, changes):
    client, state = attacks; headers = configure(client)
    body = draft(client, headers, changes)
    assert client.post(BASE + "/ip-groups/append", json=body, headers=headers).status_code in {409, 422}
    assert not state["writes"]


def test_membership_requires_csrf_revision_and_current_eligible_groups(attacks):
    client, state = attacks; headers = configure(client)
    body = draft(client, headers, {"ip": "192.0.2.1", "ip_group_ids": [1]})
    assert client.post(BASE + "/ip-groups/append", json=body, headers={"cookie": COOKIE}).status_code == 403
    state["groups"][1]["builtin"] = True
    assert client.post(BASE + "/ip-groups/append", json=body, headers=headers).status_code == 409
    body["expected_revision"] = 0
    assert client.post(BASE + "/ip-groups/append", json=body, headers=headers).status_code == 409
    assert not state["writes"]


@pytest.mark.parametrize("failure,outcome", [("timeout", "unknown"), ("partial", "unknown"), ("business", "failed")])
def test_unconfirmed_membership_is_receipted_and_cannot_be_repeated(attacks, failure, outcome):
    client, state = attacks; headers = configure(client)
    body = draft(client, headers, {"ip": "192.0.2.1", "ip_group_ids": [1, 4]}); state["failure"] = failure
    response = client.post(BASE + "/ip-groups/append", json=body, headers=headers)
    assert response.status_code == 200 and response.json()["outcome"] == outcome
    assert client.post(BASE + "/ip-groups/append", json=body, headers=headers).json()["replayed"] is True
    assert len(state["writes"]) == 1


def test_form_packets_redact_secrets_and_bound_field_count():
    value = packet_body("foo=ok&password=hidden", "content-type: application/x-www-form-urlencoded", TOKEN)
    assert "hidden" not in value and "foo=ok" in value
    assert "omitted" in packet_body("a&" * 12000, "application/x-www-form-urlencoded", TOKEN)
