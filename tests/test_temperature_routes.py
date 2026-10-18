"""Temperature endpoint keeps PVE authorization before every source/cache read."""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, quote

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from app.pve_auth import PveAuthenticator
from plugins.temperatures.temperature_routes import register_temperature_routes
import plugins.temperatures.temperatures as module


PVE = "https://pve.example:8006"
TICKET = "PVE:root@pam:1234ABCD::signature+value="
COOKIE = "PVEAuthCookie=" + quote(TICKET, safe="")
TOKEN = "temperature-fixture-token-" + "x" * 32
PATH = "/pve-komodo/api/nodes/pve-a/temperatures"
NOW = 1_792_300_000


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    state = {"events": [], "allow": True, "responses": {}, "configured": True}
    monkeypatch.setattr(module, "_now", lambda: NOW)
    path = tmp_path / "temperature-sources.json"
    path.write_text(json.dumps({"nodes": {"pve-a": {"url": "https://collector.example:9469", "token": TOKEN}}}))

    def pve(request):
        assert str(request.url).startswith(PVE + "/")
        assert request.headers["cookie"] == COOKIE
        assert "authorization" not in request.headers and "x-api-key" not in request.headers
        state["events"].append(request.url.path)
        if request.url.path == "/api2/json/access/permissions":
            assert request.method == "GET"
            return httpx.Response(200, json={"data": {"/": {"Sys.Audit": 1}} if state["allow"] else {}})
        if request.url.path == "/api2/json/cluster/status":
            return httpx.Response(200, json={"data": [{"type": "node", "name": "pve-a", "ip": "10.0.0.21", "online": 1}]})
        assert request.url.path == "/api2/json/access/ticket" and request.method == "POST"
        assert parse_qs(request.content.decode()) == {"username": ["root@pam"], "password": [TICKET], "path": ["/"], "privs": ["Sys.Audit"]}
        if not state["allow"]:
            return httpx.Response(403, json={"data": {}})
        return httpx.Response(200, json={"data": {"username": "root@pam"}})

    def collector(request):
        if request.url.host == "10.0.0.21":
            assert request.url.port == 9100 and request.url.path == "/metrics"
            assert dict(request.url.params) == {"collect[]": "hwmon"}
            assert "authorization" not in request.headers and "cookie" not in request.headers
            assert state["events"][-1] == "/api2/json/cluster/status"
            state["events"].append("node_exporter")
            return httpx.Response(200, headers={"content-type": "text/plain; version=0.0.4"}, text='''
node_hwmon_chip_names{chip="cpu0",chip_name="coretemp"} 1
node_hwmon_sensor_label{chip="cpu0",sensor="temp1",label="Package id 0"} 1
node_hwmon_temp_celsius{chip="cpu0",sensor="temp1"} 42
node_scrape_collector_success{collector="hwmon"} 1
''')
        assert str(request.url) == "https://collector.example:9469/v1/temperatures"
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert "cookie" not in request.headers and "x-pve-komodo-csrf" not in request.headers
        assert request.headers["accept-encoding"] == "identity"
        assert state["events"][-1] == "/api2/json/access/ticket", "PVE session must be fully authenticated first"
        state["events"].append("collector")
        response = state["responses"].get("collector")
        if isinstance(response, Exception):
            raise response
        return response if response is not None else httpx.Response(200, json={"version": 1, "node": "pve-a", "sampled_at": NOW,
            "sensors": [{"id": "cpu-0", "chip": "coretemp", "label": "Package id 0", "category": "cpu", "celsius": 42,
                         "max_celsius": None, "critical_celsius": 100}]})

    def make(configured=True):
        app = FastAPI()
        auth = PveAuthenticator(PVE, transport=httpx.MockTransport(pve))
        register_temperature_routes(app, SimpleNamespace(temperature_sources_file=str(path) if configured else None), auth,
                                    transport=httpx.MockTransport(collector))
        return TestClient(app)
    state["make"] = make
    with make() as browser:
        yield browser, state


def test_existing_pve_session_and_sys_audit_are_required_before_collector_lookup(boundary):
    browser, state = boundary
    assert browser.get(PATH).status_code == 401 and state["events"] == []
    state["allow"] = False
    assert browser.get(PATH, headers={"cookie": COOKIE}).status_code == 403
    assert "collector" not in state["events"]


def test_authorized_read_uses_only_fixed_source_and_safe_dto(boundary):
    browser, state = boundary
    response = browser.get(PATH, headers={"cookie": COOKIE, "authorization": "Bearer browser-supplied-value", "x-pve-komodo-csrf": "browser-secret"})
    assert response.status_code == 200 and response.json()["status"] == "ok"
    assert response.json()["node"] == "pve-a" and response.json()["sensors"][0]["celsius"] == 42
    assert state["events"] == ["/api2/json/access/permissions", "/api2/json/access/ticket", "collector"]
    for private in (TOKEN, COOKIE, TICKET, "collector.example", "browser-secret", "browser-supplied-value"):
        assert private not in response.text


def test_authentication_is_repeated_before_returning_cached_data(boundary):
    browser, state = boundary
    assert browser.get(PATH, headers={"cookie": COOKIE}).json()["status"] == "ok"
    assert browser.get(PATH, headers={"cookie": COOKIE}).json()["status"] == "ok"
    assert state["events"].count("collector") == 1
    assert state["events"].count("/api2/json/access/ticket") == 2
    state["allow"] = False
    assert browser.get(PATH, headers={"cookie": COOKIE}).status_code == 403
    assert state["events"].count("collector") == 1


def test_unconfigured_nodes_still_authenticate_and_never_select_arbitrary_origin(boundary):
    browser, state = boundary
    response = browser.get(PATH.replace("pve-a", "other-node"), headers={"cookie": COOKIE})
    assert response.status_code == 200 and response.json() == {"version": 1, "node": "other-node", "sampled_at": None,
                                                           "sensors": [], "status": "unavailable"}
    assert "collector" not in state["events"]
    assert browser.get(PATH.replace("pve-a", "other-node")).status_code == 401


def test_feature_without_sources_file_automatically_uses_pve_node_ip(boundary):
    _, state = boundary
    with state["make"](False) as browser:
        response = browser.get(PATH, headers={"cookie": COOKIE})
        assert response.status_code == 200 and response.json()["status"] == "ok"
        assert response.json()["sensors"][0]["celsius"] == 42
        assert state["events"][-2:] == ["/api2/json/cluster/status", "node_exporter"]
        assert "collector" not in state["events"]


@pytest.mark.parametrize("path", [PATH + "?_dc=1", PATH + "?url=https://private.example", PATH + "?token=private",
    PATH.replace("pve-a", "node_test"), PATH.replace("pve-a", "node:9469"), PATH.replace("pve-a", "a" * 65)])
def test_request_cannot_override_source_credentials_or_target_path(boundary, path):
    browser, state = boundary
    assert browser.get(path).status_code == 401
    assert browser.get(path, headers={"cookie": COOKIE}).status_code == 422
    assert "collector" not in state["events"]


@pytest.mark.parametrize("response", [httpx.Response(503, text=TOKEN + " https://private.example"), httpx.ReadTimeout(TOKEN),
    httpx.Response(200, json={"version": 1, "node": "other-host", "sampled_at": NOW, "sensors": []})])
def test_upstream_failures_do_not_leak_source_or_secret(boundary, response):
    browser, state = boundary
    state["responses"]["collector"] = response
    result = browser.get(PATH, headers={"cookie": COOKIE})
    assert result.status_code == 200 and result.json()["status"] == "unavailable" and result.json()["sensors"] == []
    assert TOKEN not in result.text and "private.example" not in result.text and "other-host" not in result.text
