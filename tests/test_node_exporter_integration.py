"""Exercise the default PVE-discovery -> node_exporter path through create_app."""

from urllib.parse import parse_qs, quote

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app
import plugins.temperatures.temperatures as temperatures


PVE = "https://pve.example:8006"
TICKET = "PVE:root@pam:1234ABCD::integration-signature="
COOKIE = "PVEAuthCookie=" + quote(TICKET, safe="")
PATH = "/pve-komodo/api/nodes/pve-a/temperatures"
NOW = 1_792_300_000
METRICS = """# HELP node_hwmon_temp_celsius Hardware temperature
# TYPE node_hwmon_temp_celsius gauge
node_hwmon_temp_celsius{chip="platform_coretemp_0",sensor="temp1"} 0
node_hwmon_temp_max_celsius{chip="platform_coretemp_0",sensor="temp1"} 80
node_hwmon_temp_crit_celsius{chip="platform_coretemp_0",sensor="temp1"} 100
node_hwmon_sensor_label{chip="platform_coretemp_0",sensor="temp1",label="Package id 0"} 1
node_hwmon_chip_names{chip="platform_coretemp_0",chip_name="coretemp"} 1
node_scrape_collector_success{collector="hwmon"} 1
"""


def node(name="pve-a", ip="10.20.30.40", online=1):
    return {"type": "node", "name": name, "id": "node/" + name, "ip": ip, "online": online}


@pytest.fixture
def integration(monkeypatch):
    for key in ("PVE_TEMPERATURE_SOURCES_FILE", "KOMODO_URL", "KOMODO_API_KEY", "KOMODO_API_SECRET"):
        monkeypatch.delenv(key, raising=False)
    state = {"events": [], "pve_requests": [], "metrics_requests": [], "nodes": [node()], "clock": 10.0,
             "auth_status": 200, "metrics_response": None, "discovery_response": None}
    monkeypatch.setattr(temperatures, "_now", lambda: NOW)
    monkeypatch.setattr(temperatures, "_monotonic", lambda: state["clock"])

    def pve(request):
        assert request.url.scheme == "https" and request.url.host == "pve.example" and request.url.port == 8006
        assert request.headers["cookie"] == COOKIE
        assert "authorization" not in request.headers and "x-api-key" not in request.headers
        state["pve_requests"].append(request)
        state["events"].append(request.url.path)
        if request.url.path == "/api2/json/access/permissions":
            assert request.method == "GET"
            return httpx.Response(state["auth_status"], json={"data": {"/": {"Sys.Audit": 1}}},
                                  headers={"set-cookie": "must-not-cross-origin=pve-only"})
        if request.url.path == "/api2/json/access/ticket":
            assert request.method == "POST"
            assert parse_qs(request.content.decode()) == {"username": ["root@pam"], "password": [TICKET],
                                                         "path": ["/"], "privs": ["Sys.Audit"]}
            return httpx.Response(200, json={"data": {"username": "root@pam"}})
        assert request.url.path == "/api2/json/cluster/status" and request.method == "GET"
        assert not request.url.query
        response = state["discovery_response"]
        return response if response is not None else httpx.Response(200, json={"data": state["nodes"]})

    def exporter(request):
        assert request.method == "GET" and request.url.scheme == "http" and request.url.port == 9100
        assert request.url.path == "/metrics" and dict(request.url.params) == {"collect[]": "hwmon"}
        for header in ("cookie", "authorization", "x-api-key", "x-api-secret", "x-pve-komodo-csrf", "csrfpreventiontoken"):
            assert header not in request.headers
        assert request.headers["accept-encoding"] == "identity"
        assert state["events"][-1] == "/api2/json/cluster/status", "discovery must be checked before every exporter read"
        state["metrics_requests"].append(request)
        state["events"].append("metrics:" + request.url.host)
        response = state["metrics_response"]
        if callable(response):
            return response(request)
        return response if response is not None else httpx.Response(200, text=METRICS)

    def forbidden(request):
        pytest.fail("Temperature request must not contact Komodo or the HTML upstream")

    settings = Settings(PVE, action_state_file=":memory:", guest_links_file=":memory:")
    assert settings.temperature_sources_file is None and settings.komodo_url is None
    app = create_app(settings, transport=httpx.MockTransport(forbidden), pve_auth_transport=httpx.MockTransport(pve),
                     komodo_transport=httpx.MockTransport(forbidden), temperature_transport=httpx.MockTransport(exporter))
    with TestClient(app) as client:
        state["get"] = lambda path=PATH, **kwargs: client.get(path, headers={"cookie": COOKIE, **kwargs.pop("headers", {})}, **kwargs)
        yield client, state


def test_default_discovery_without_komodo_or_temperature_configuration_preserves_zero_and_metadata(integration):
    _, state = integration
    response = state["get"]()
    assert response.status_code == 200 and response.json()["status"] == "ok"
    data = response.json()
    assert data["node"] == "pve-a" and data["sampled_at"] == NOW
    assert len(data["sensors"]) == 1
    sensor = data["sensors"][0]
    assert sensor["category"] == "cpu" and sensor["label"] == "Package id 0" and sensor["celsius"] == 0
    assert sensor["max_celsius"] == 80 and sensor["critical_celsius"] == 100
    assert "coretemp" in sensor["chip"]
    assert state["metrics_requests"][0].url.host == "10.20.30.40"
    assert state["events"] == ["/api2/json/access/permissions", "/api2/json/access/ticket", "/api2/json/cluster/status", "metrics:10.20.30.40"]
    assert response.headers["cache-control"] == "private, no-store"


def test_browser_credentials_and_pve_cookie_never_reach_node_exporter(integration):
    _, state = integration
    response = state["get"](headers={"authorization": "Bearer browser-only", "x-api-key": "browser-key",
                                      "x-pve-komodo-csrf": "browser-csrf", "csrfpreventiontoken": "pve-csrf"})
    assert response.json()["status"] == "ok"
    assert len(state["pve_requests"]) == 3 and len(state["metrics_requests"]) == 1
    for private in (COOKIE, TICKET, "browser-only", "browser-key", "browser-csrf", "must-not-cross-origin", "10.20.30.40"):
        assert private not in response.text


def test_anonymous_and_revoked_sessions_cannot_discover_or_receive_cached_metrics(integration):
    client, state = integration
    assert client.get(PATH).status_code == 401 and state["events"] == []
    assert state["get"]().json()["status"] == "ok"
    state["auth_status"] = 403
    assert state["get"]().status_code == 403
    assert len(state["metrics_requests"]) == 1
    assert state["events"].count("/api2/json/cluster/status") == 1


def test_ipv6_inventory_address_is_bracketed_and_keeps_the_fixed_endpoint(integration):
    _, state = integration
    state["nodes"] = [node(ip="fd12:3456:789a::10")]
    assert state["get"]().json()["status"] == "ok"
    assert str(state["metrics_requests"][0].url) == "http://[fd12:3456:789a::10]:9100/metrics?collect%5B%5D=hwmon"


def test_browser_cannot_supply_a_url_ip_or_alternate_metrics_query(integration):
    _, state = integration
    for path in (PATH + "?url=http://192.0.2.10:9100", PATH + "?ip=10.20.30.99", PATH + "?collect[]=cpu",
                 PATH.replace("pve-a", "127.0.0.1")):
        assert state["get"](path).status_code == 422
    assert state["metrics_requests"] == []
    assert "/api2/json/cluster/status" not in state["events"]


def test_offline_and_unlisted_nodes_do_not_trigger_a_scrape(integration):
    _, state = integration
    state["nodes"] = [node(online=0)]
    assert state["get"]().json()["status"] == "unavailable"
    state["nodes"] = [node("pve-b")]
    assert state["get"]().json()["status"] == "unavailable"
    assert state["metrics_requests"] == []


def test_conflicting_inventory_addresses_do_not_choose_an_arbitrary_host(integration):
    _, state = integration
    state["nodes"] = [node(), node("pve-b")]
    assert state["get"]().json()["status"] == "unavailable"
    assert state["metrics_requests"] == []


def test_relocated_node_does_not_reuse_metrics_from_its_previous_address(integration):
    _, state = integration
    state["metrics_response"] = lambda request: httpx.Response(200, text=METRICS.replace(
        'sensor="temp1"} 0', 'sensor="temp1"} ' + ("0" if request.url.host.endswith(".40") else "63")))
    assert state["get"]().json()["sensors"][0]["celsius"] == 0
    state["nodes"] = [node(ip="10.20.30.41")]
    assert state["get"]().json()["sensors"][0]["celsius"] == 63
    assert [request.url.host for request in state["metrics_requests"]] == ["10.20.30.40", "10.20.30.41"]


def test_five_second_metrics_cache_still_reauthenticates_and_rediscovers_each_time(integration):
    _, state = integration
    assert state["get"]().json()["status"] == "ok"
    state["clock"] = 14.9
    assert state["get"]().json()["status"] == "ok"
    assert len(state["metrics_requests"]) == 1
    state["clock"] = 15.1
    assert state["get"]().json()["status"] == "ok"
    assert len(state["metrics_requests"]) == 2
    for path in ("/api2/json/access/permissions", "/api2/json/access/ticket", "/api2/json/cluster/status"):
        assert state["events"].count(path) == 3


def test_html_response_is_unavailable_even_if_its_body_contains_valid_metrics(integration):
    _, state = integration
    state["metrics_response"] = httpx.Response(200, content=METRICS, headers={"content-type": "text/html"})
    response = state["get"]()
    assert response.status_code == 200 and response.json()["status"] == "unavailable"
    assert response.json()["sensors"] == [] and response.json()["sampled_at"] is None


def test_oversized_metrics_are_rejected_before_returning_sensor_data(integration):
    _, state = integration
    state["metrics_response"] = httpx.Response(200, content=METRICS, headers={"content-type": "text/plain",
        "content-length": str(temperatures._MAX_METRICS_BYTES + 1)})
    assert state["get"]().json()["status"] == "unavailable"


def test_exporter_redirect_is_not_followed_to_another_origin(integration):
    _, state = integration
    state["metrics_response"] = httpx.Response(302, headers={"location": "http://10.20.30.99:8080/private"})
    assert state["get"]().json()["status"] == "unavailable"
    assert len(state["metrics_requests"]) == 1
    assert state["metrics_requests"][0].url.host == "10.20.30.40"


def test_discovery_redirect_is_not_followed_or_sent_to_an_exporter(integration):
    _, state = integration
    state["discovery_response"] = httpx.Response(302, headers={"location": "https://another-pve.example/cluster/status"})
    assert state["get"]().json()["status"] == "unavailable"
    assert len(state["pve_requests"]) == 3 and state["metrics_requests"] == []


def test_identically_named_nvme_devices_remain_distinct_through_the_public_dto(integration):
    _, state = integration
    metrics = METRICS
    for chip, value in (("nvme_nvme0", 30), ("nvme_nvme1", 45)):
        metrics += f'node_hwmon_chip_names{{chip="{chip}",chip_name="nvme"}} 1\n'
        metrics += f'node_hwmon_sensor_label{{chip="{chip}",sensor="temp1",label="Composite"}} 1\n'
        metrics += f'node_hwmon_temp_celsius{{chip="{chip}",sensor="temp1"}} {value}\n'
    state["metrics_response"] = httpx.Response(200, text=metrics)
    sensors = state["get"]().json()["sensors"]
    disks = [sensor for sensor in sensors if sensor["category"] == "disk"]
    assert len(disks) == 2 and len({sensor["chip"] for sensor in disks}) == 2
    assert len({sensor["id"] for sensor in disks}) == 2
    assert sorted(sensor["celsius"] for sensor in disks) == [30, 45]


def test_successful_hwmon_scrape_without_readings_is_not_zero_degrees(integration):
    _, state = integration
    state["metrics_response"] = httpx.Response(200, text='node_scrape_collector_success{collector="hwmon"} 1\n')
    response = state["get"]()
    assert response.json()["status"] == "no_sensors"
    assert response.json()["sensors"] == [] and response.json()["sampled_at"] == NOW


def test_total_request_budget_stops_before_discovery_or_exporter_after_slow_auth(integration, monkeypatch):
    import asyncio
    import plugins.temperatures.temperature_routes as routes
    from app.pve_auth import PveAuthenticator

    async def slow_auth(self, request):
        await asyncio.sleep(0.1)

    monkeypatch.setattr(routes, "REQUEST_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(PveAuthenticator, "authenticate", slow_auth)
    _, state = integration
    response = state["get"]()
    assert response.status_code == 504
    assert response.json() == {"detail": "temperature_request_timeout"}
    assert response.headers["cache-control"] == "private, no-store"
    assert state["pve_requests"] == [] and state["metrics_requests"] == []
