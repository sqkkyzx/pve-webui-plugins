"""SafeLine configuration, read API boundary and independent lifecycle."""

import asyncio
import json
from pathlib import Path
import stat

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app
from plugins.safeline.client import SafeLineClient
from plugins.safeline.connection import Connection, ConnectionStore, SafeLineError
from plugins.safeline.service import application, certificate, ip_group, policy
from test_site_routes import COOKIE, PVE, pve_transport


BASE = "/pve-plugins/api/safeline"
TOKEN = "private-safeline-api-token"
URL = "https://waf.example:9443"


def waf_handler(state):
    def handler(request):
        state["waf"].append(request)
        assert request.method == "GET", "Read routes must never mutate SafeLine"
        assert request.headers["x-slce-api-token"] == state.get("token", TOKEN)
        assert not {"cookie", "authorization", "x-pve-plugins-csrf", "csrfpreventiontoken"} & set(request.headers)
        path = request.url.path
        site = {"id": 12, "title": "<script>app</script>", "server_names": ["app.example"], "ports": ["443"],
                "upstreams": ["https://backend:8443"], "is_enabled": True, "mode": 0, "cert_id": 5,
                "health_check": True, "health_state": {"https://backend:8443": {"state": 2, "error": ""}}}
        policies = [{"id": i+1, "action": i, "name": "rule-"+str(i), "is_enabled": i != 1, "expire": 0,
                     "pattern": [[{"k": "header", "sub_k": "Authorization", "op": "eq", "v": ["private-header"]}]],
                     "private_key": "never-return-this"} for i in range(3)]
        cert = {"id": 5, "domains": ["app.example"], "issuer": "CA", "valid_before": "2099-01-01T00:00:00Z",
                "expired": False, "revoked": False, "manual": {"key": "private-certificate-key"}}
        group = {"id": 7, "comment": "office", "ips": ["10.0.0.0/24"], "total": 1, "reference": "", "builtin": False}
        if path == "/api/open/system":
            value = {"version": "9.4.2", "license": {"secret": TOKEN}, "machine_id": "hidden"}
        elif path == "/api/open/system/edition": value = {"version": 0, "secret": TOKEN}
        elif path == "/api/open/site": value = {"data": [site], "total": 1}
        elif path == "/api/open/site/12": value = site
        elif path.endswith("/proxy"): value = {"gzip": {"value": True}, "http_headers": {"Authorization": "private-header"}}
        elif path.endswith("/semantics"): value = {"semantics": {"enabled": True}}
        elif path.endswith("/acl"): value = []
        elif path == "/api/open/policy": value = {"data": policies, "total": 3}
        elif path == "/api/open/policy/detail": value = policies[int(request.url.params["id"])-1]
        elif path == "/api/open/cert": value = {"nodes": [cert], "total": 1}
        elif path == "/api/open/ipgroup": value = {"nodes": [group], "total": 1}
        elif path == "/api/open/ipgroup/detail": value = {"data": group}
        elif path.startswith("/api/open/events"):
            value = {"nodes": [{"id": 9, "ip": "10.0.0.1", "host": "app.example", "start_at": 1792185285000}], "total": 1}
        elif path.startswith("/api/open/records"):
            value = {"data": [{"timestamp": 1792185349, "src_ip": "10.0.0.1", "event_id": "log-uuid", "reason": TOKEN,
                                "req_header": "GET / HTTP/1.1\r\nAuthorization: Bearer private-auth\r\nCookie: private-cookie\r\nHost: app.example",
                                "rsp_header": "HTTP/1.1 200 OK\r\nSet-Cookie: private-set-cookie", "req_body": "private-body"}], "total": 1}
        elif path == "/api/stat/qps": value = {"nodes": [{"time": "12:00:00", "0.0.0.0:0": 0}]}
        elif path == "/api/stat/advance/access": value = {"access": 0, "ip": 0, "session": 0}
        elif path == "/api/stat/advance/attack": value = {"attack_ip": 0, "intercept": {"block": 0}}
        elif path.startswith("/api/stat/advance/trend/"): value = [{"time": 1792185349, "count": 0}]
        elif path == "/api/stat/advance/error_status_code": value = {"error_4xx": 0, "error_5xx": 0}
        elif path == "/api/stat/advance/location": value = [{"country": "CN", "province": "福建省", "count": 5}]
        elif path == "/api/open/security_posture/statistics": value = {"attack_allow": 0, "attack_deny": 0, "anti_tamper": []}
        elif path == "/api/open/security_posture/trends": value = {"item": [{"1792185349": "0"}], "pass": [{"1792185349": "0"}], "top": []}
        elif path == "/api/open/security_posture/realtime": value = {"event": []}
        else: pytest.fail("Unexpected SafeLine endpoint: " + path)
        return httpx.Response(200, json={"data": value, "err": "", "msg": "ok"}, headers={"set-cookie": "upstream-private=do-not-replay"})
    return handler


@pytest.fixture
def integrated(tmp_path, monkeypatch):
    path = tmp_path / "private" / "connection.sqlite3"
    for name in ("SAFELINE_URL", "SAFELINE_API_TOKEN", "SAFELINE_TLS_SERVER_NAME", "SAFELINE_CA_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SAFELINE_STATE_FILE", str(path))
    state = {"requests": [], "waf": []}
    transport = httpx.MockTransport(waf_handler(state))
    app = create_app(Settings(PVE, enabled_plugins=("safeline",)), pve_auth_transport=pve_transport(state), plugin_transports={"safeline": transport})
    with TestClient(app) as client:
        yield client, state, path, app


def credentials(client):
    response = client.get(BASE + "/connection", headers={"cookie": COOKIE})
    assert response.status_code == 200
    return {"cookie": COOKIE, "origin": "http://testserver", "x-pve-plugins-csrf": response.json()["csrf_token"]}


def configure(client):
    headers = credentials(client)
    result = client.put(BASE + "/connection", json={"url": URL, "token": TOKEN, "expected_revision": 0}, headers=headers)
    assert result.status_code == 200
    assert TOKEN not in result.text
    return headers


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/connection", None), ("GET", "/status", None), ("GET", "/inventory", None), ("GET", "/overview", None),
    ("GET", "/events", None), ("GET", "/records", None), ("GET", "/resources/applications/12", None),
    ("PUT", "/connection", {"token": TOKEN}), ("POST", "/connection/test", {"token": TOKEN}), ("DELETE", "/connection", {"expected_revision": 0}),
])
def test_anonymous_requests_do_not_open_storage_or_external_connections(integrated, method, path, payload):
    client, state, path_on_disk, _ = integrated
    response = client.request(method, BASE + path, json=payload)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"
    assert not state["requests"] and not state["waf"] and not path_on_disk.exists()


def test_configuration_is_private_revisioned_and_survives_restart(integrated):
    client, state, path, app = integrated
    headers = configure(client)
    assert not state["waf"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    store = ConnectionStore(path, PVE)
    config, revision, source = store.load()
    assert (config.url, config.token, revision, source) == (URL, TOKEN, 1, "saved")
    assert TOKEN not in repr(config)
    store.close()
    conflict = client.put(BASE + "/connection", json={"url": URL, "token": "new-token", "expected_revision": 0}, headers=headers)
    assert conflict.status_code == 409
    tested = client.post(BASE + "/connection/test", json={"url": URL, "expected_revision": 1}, headers=headers)
    assert tested.status_code == 200 and tested.json()["version"] == "9.4.2"
    assert app.state.safeline_service.store.load()[1] == 1
    changed = client.put(BASE + "/connection", json={"url": "https://other.example", "expected_revision": 1}, headers=headers)
    assert changed.status_code == 422 and changed.json()["detail"] == "safeline_token_required"


def test_clear_connection_does_not_reactivate_environment_credentials(integrated, monkeypatch):
    client, state, _, _ = integrated
    monkeypatch.setenv("SAFELINE_URL", URL)
    monkeypatch.setenv("SAFELINE_API_TOKEN", TOKEN)
    response = client.get(BASE + "/connection", headers={"cookie": COOKIE})
    assert response.json()["source"] == "environment" and TOKEN not in response.text
    headers = configure(client)
    deleted = client.request("DELETE", BASE + "/connection", json={"expected_revision": 1}, headers=headers)
    assert deleted.status_code == 200 and deleted.json()["configured"] is False
    assert client.get(BASE + "/status", headers={"cookie": COOKIE}).json()["configured"] is False
    assert not state["waf"]


@pytest.mark.parametrize("path", ["/inventory", "/status", "/overview?hours=24", "/events", "/records", "/resources/applications/12?section=proxy",
                                 "/resources/applications/12?section=protection", "/resources/deny/2", "/resources/allow/1", "/resources/certificates/5", "/resources/ip-groups/7"])
def test_real_routes_only_read_safe_dtos_and_reauthenticate(integrated, path):
    client, state, _, _ = integrated
    configure(client)
    before = len(state["requests"])
    response = client.get(BASE + path, headers={"cookie": COOKIE, "authorization": "Bearer browser-secret"})
    assert response.status_code == 200, response.text
    assert len(state["requests"]) == before + 2
    for secret in (TOKEN, "private-certificate-key", "never-return-this", "private-auth", "private-cookie", "private-set-cookie", "private-body", "private-header", "browser-secret"):
        assert secret not in response.text
    assert response.json()["user"] == "root@pam"
    assert all(request.method == "GET" for request in state["waf"])


def test_inventory_classification_cert_references_and_unknown_metrics(integrated):
    client, _, _, _ = integrated
    configure(client)
    inv = client.get(BASE + "/inventory", headers={"cookie": COOKIE}).json()
    assert {k: len(v) for k, v in inv["resources"].items()} == {"applications": 1, "deny": 1, "allow": 1, "certificates": 1, "ip-groups": 1}
    assert inv["resources"]["applications"][0]["status"]["code"] == "running"
    assert inv["resources"]["applications"][0]["cert_id"] == 5
    assert inv["resources"]["ip-groups"][0]["status"]["code"] == "normal"
    assert inv["policies"][0]["action"] == 2
    stats = client.get(BASE + "/overview", headers={"cookie": COOKIE}).json()
    assert stats["sections"]["access"]["access"] == 0
    assert stats["sections"]["access"]["pv"] is None
    assert stats["sections"]["attack"]["intercept"]["challenge"] is None


def test_overview_uses_verified_seconds_and_console_preset(integrated):
    client, state, _, _ = integrated
    configure(client)
    assert client.get(BASE + "/overview?hours=24", headers={"cookie": COOKIE}).status_code == 200
    for request in state["waf"]:
        if request.url.path not in {"/api/stat/qps", "/api/open/system/edition", "/api/open/security_posture/realtime"}:
            assert request.url.params["time_preset"] == "last1Day"
            assert int(request.url.params["end_time"]) - int(request.url.params["begin_time"]) == 86400
    state["waf"].clear()
    for hours in (1, 168):
        assert client.get(BASE + "/overview?hours=" + str(hours), headers={"cookie": COOKIE}).status_code == 422
    assert not state["waf"]


@pytest.mark.parametrize("path", ["/inventory?url=https://evil.example", "/events?page_size=101", "/events?page=1&page=2", "/overview?hours=2",
                                 "/records?policy_rule_id=1", "/resources/unknown/1", "/resources/applications/0", "/records?start=20&end=10"])
def test_query_allowlists_prevent_arbitrary_upstream_requests(integrated, path):
    client, state, _, _ = integrated
    configure(client)
    response = client.get(BASE + path, headers={"cookie": COOKIE})
    assert response.status_code == 422 and not state["waf"]


def test_configuration_writes_require_same_origin_and_csrf_before_payload_or_store(integrated):
    client, _, path, _ = integrated
    value = {"url": URL, "token": TOKEN, "expected_revision": 0}
    assert client.put(BASE + "/connection", json=value, headers={"cookie": COOKIE}).status_code == 403
    assert not path.exists()
    headers = credentials(client)
    assert client.put(BASE + "/connection", json=value, headers={**headers, "origin": "https://evil.example"}).status_code == 403
    assert client.put(BASE + "/connection", json={**value, "unknown": TOKEN}, headers=headers).json() == {"detail": "invalid_safeline_request"}
    duplicate = client.put(BASE + "/connection", content='{"token":"'+TOKEN+'","token":"again"}', headers={**headers,"content-type":"application/json"})
    assert duplicate.status_code == 422 and TOKEN not in duplicate.text


@pytest.mark.parametrize("url", ["http://waf.example", "https://user:password@waf.example", "https://waf.example/api", "https://waf.example?token=private", "https://127.0.0.1:9443", "https://169.254.169.254", "https://[::1]"])
def test_connection_rejects_invalid_or_local_metadata_targets(url):
    with pytest.raises(SafeLineError) as error: Connection(url, TOKEN)
    assert TOKEN not in str(error.value) and url not in str(error.value)


@pytest.mark.parametrize("status,body,code", [
    (401, {}, "safeline_invalid_credentials"), (403, {}, "safeline_permission_denied"), (404, {}, "safeline_endpoint_unavailable"),
    (467, {}, "safeline_sso_required"), (302, {}, "safeline_sso_required"), (500, {}, "safeline_upstream_error"),
    (200, {"data": {}, "err": TOKEN}, "safeline_business_error"), (200, [], "safeline_unexpected_response"),
])
def test_client_distinguishes_errors_without_returning_upstream_bodies(status, body, code):
    client = SafeLineClient(Connection(URL, TOKEN), transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)))
    with pytest.raises(SafeLineError) as error: asyncio.run(client.get("/api/open/system"))
    assert error.value.code == code and TOKEN not in repr(error.value)


def test_tls_name_is_explicit_and_injected_transport_is_not_closed():
    class Borrowed(httpx.AsyncBaseTransport):
        closed = 0
        async def handle_async_request(self, request):
            assert request.extensions["sni_hostname"] == "waf.example"
            assert request.headers["host"] == "waf.example:9443"
            return httpx.Response(200, json={"data": {"version": "9.4.2"}, "err": ""})
        async def aclose(self): self.closed += 1
    transport = Borrowed()
    client = SafeLineClient(Connection("https://10.101.100.101:9443", TOKEN, True, "waf.example"), transport=transport)
    asyncio.run(client.get("/api/open/system"))
    assert transport.closed == 0


def test_resource_states_do_not_treat_unknown_health_or_reference_as_failure():
    site = {"id": 1, "is_enabled": True, "mode": 0, "health_check": True, "health_state": {"upstream": {"state": 999, "error": ""}}}
    assert application(site)["status"]["code"] == "running"
    site["health_state"]["upstream"]["error"] = "connection refused"
    assert application(site)["status"]["code"] == "error"
    site["mode"] = 2
    assert application(site)["status"]["code"] == "paused"
    assert policy({"id": 1, "is_enabled": False, "expire": 0})["status"]["code"] == "paused"
    assert policy({"id": 1, "is_enabled": True, "expire": 123})["status"]["code"] == "unknown"
    assert certificate({"id": 1, "expired": True})["status"]["code"] == "error"
    assert ip_group({"id": 1, "ips": [], "reference": ""})["status"]["code"] == "normal"


def test_tls_listener_suffix_is_not_forwarded_as_a_log_port():
    site = application({"id": 1, "ports": ["443_ssl", "80", "unknown", "99999_ssl"]})
    assert site["listeners"] == [{"port": 443, "tls": True}, {"port": 80, "tls": False}]


def test_status_redacts_credentials_before_truncating_display_strings():
    def handler(request):
        data = {"version": "v" * 90 + TOKEN} if request.url.path.endswith("system") else {"version": 0}
        return httpx.Response(200, json={"data": data, "err": ""})
    result = asyncio.run(SafeLineClient(Connection(URL, TOKEN), transport=httpx.MockTransport(handler)).status())
    assert TOKEN[:8] not in json.dumps(result)
