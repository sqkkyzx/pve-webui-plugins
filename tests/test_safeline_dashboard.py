"""Dashboard contracts, shared filters, edition clamps and independent failures."""

from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app
from plugins.safeline import dashboard
from test_safeline import BASE, TOKEN, configure, waf_handler
from test_site_routes import COOKIE, PVE, pve_transport


@pytest.fixture
def dashboard_app(tmp_path, monkeypatch):
    for key in ("SAFELINE_URL", "SAFELINE_API_TOKEN", "SAFELINE_TLS_SERVER_NAME", "SAFELINE_CA_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(dashboard, "time", SimpleNamespace(time=lambda: 1792324800))
    state = {"requests": [], "waf": [], "edition": {"version": 2, "effective_edition": 2}, "failure": None}
    base = waf_handler(state)

    def handler(request):
        path = request.url.path
        custom = {"/api/open/system/edition", "/api/stat/qps", "/api/stat/advance/location", "/api/stat/advance/error_status_code",
                  "/api/open/security_posture/statistics", "/api/open/security_posture/trends", "/api/open/security_posture/realtime"}
        if path not in custom:
            return base(request)
        state["waf"].append(request)
        assert request.method == "GET" and request.headers["x-slce-api-token"] == TOKEN
        assert not {"cookie", "authorization", "x-pve-plugins-csrf"} & set(request.headers)
        if state["failure"] == path:
            return httpx.Response(200, json={"data": None, "err": "unavailable", "msg": TOKEN})
        if path == "/api/open/system/edition": value = state["edition"]
        elif path == "/api/stat/qps": value = {"nodes": [{"time": "12:00:00", "0.0.0.0:0": 10, "127.0.0.1:443": 5}]}
        elif path.endswith("/error_status_code"): value = {"error_4xx": 10 if request.url.params["upstream"] == "true" else 4, "error_5xx": 2}
        elif path.endswith("/location"):
            value = [{"country": "CN", "province": "福建省" if request.url.params["global"] == "false" else "", "count": 11 if request.url.params["action"] == "-1" else 3}]
        elif path.endswith("/statistics"):
            value = {"attack_deny": 2, "attack_allow": 3, "black_hit": 4, "white_hit": 5, "acl_hit": 6, "waiting_hit": 7,
                     "challenge_deny": 8, "challenge_allow": 9, "auth_deny": 10, "auth_allow": 11, "anti_tamper": [{"app.example": "12"}]}
        elif path.endswith("/trends"):
            value = {"item": [{"1792321200": "2"}, {"1792317600": "1"}], "pass": [{"1792321200": "3"}], "top": [{"10.0.0.1": "5"}]}
        else:
            value = {"event": [{"site_id": 12 if not state.get("wrong_event_scope") else 999, "site_name": "<script>app</script>",
                "event_type": 1, "time_stamp": 1792300000, "attack_type": "SQL", "secret": "never-forward-this",
                "event": {"ip": "10.0.0.1", "port": "443", "host": "app.example", "start": 1792300000, "end": 1792300300, "password": "never-forward-this"}}]}
        return httpx.Response(200, json={"data": value, "err": "", "msg": "ok"})

    app = create_app(Settings(PVE, enabled_plugins=("safeline",), safeline_state_file=str(tmp_path / "safeline" / "connection.sqlite3")), pve_auth_transport=pve_transport(state), plugin_transports={"safeline": httpx.MockTransport(handler)})
    with TestClient(app) as client:
        yield client, state


def test_all_panels_share_the_scope_but_realtime_series_remain_current(dashboard_app):
    client, state = dashboard_app
    headers = configure(client)
    response = client.get(BASE + "/overview?preset=last7Day&site_id=12&utc_offset=-480", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["errors"] == {} and data["filters"] == {"preset": "last7Day", "site_id": 12, "utc_offset": -480}
    assert data["end_time"] - data["begin_time"] == 7 * 86400
    calls = [r for r in state["waf"] if r.url.path != "/api/open/system/edition"]
    assert len(calls) == 14
    for request in calls:
        assert request.url.params["site_id"] == "12"
        if request.url.path not in {"/api/stat/qps", "/api/open/security_posture/realtime"}:
            assert request.url.params["begin_time"] == str(data["begin_time"])
            assert request.url.params["end_time"] == str(data["end_time"])
            assert request.url.params["time_preset"] == "last7Day"
        else:
            assert "time_preset" not in request.url.params and "begin_time" not in request.url.params
    assert data["sections"]["qps"][0]["count"] == 3, "five-second source bins are converted to QPS"
    assert data["sections"]["posture_trend"]["denied"] == [{"time": 1792317600, "count": 1}, {"time": 1792321200, "count": 2}]
    assert data["sections"]["posture_trend"]["sources"] == [{"name": "10.0.0.1", "count": 5}]
    assert "never-forward-this" not in response.text and TOKEN not in response.text


def test_four_geography_combinations_use_the_console_filters_without_a_globe(dashboard_app):
    client, state = dashboard_app
    headers = configure(client)
    data = client.get(BASE + "/overview", headers=headers).json()
    assert not data["errors"]
    assert data["sections"]["geo_world_access"] == [{"country": "CN", "province": "", "count": 11}]
    assert data["sections"]["geo_china_intercept"] == [{"country": "CN", "province": "福建省", "count": 3}]
    requests = [r for r in state["waf"] if r.url.path.endswith("/location")]
    assert {(r.url.params["global"], r.url.params["action"]) for r in requests} == {("true", "-1"), ("true", "1"), ("false", "-1"), ("false", "1")}
    assert all(r.url.params["size"] == "-1" for r in requests)
    assert all("site_id" not in r.url.params for r in requests), "all applications omits site_id, never sends the console's -1 sentinel"


def test_ce_seven_day_query_keeps_posture_and_never_presents_clamped_traffic(dashboard_app):
    client, state = dashboard_app
    state["edition"] = {"version": 0, "effective_edition": 0}
    headers = configure(client)
    data = client.get(BASE + "/overview?preset=last7Day", headers=headers).json()
    assert data["capabilities"]["presets"] == ["last1Day", "last7Day"]
    assert data["capabilities"]["traffic"]["applications"] is False
    assert "posture" in data["sections"] and "qps" in data["sections"] and "realtime" in data["sections"]
    assert "access" not in data["sections"] and data["errors"]["access"] == "safeline_statistics_range_unavailable"
    assert not any("/stat/advance/" in r.url.path for r in state["waf"])
    assert all(r.url.params["time_preset"] == "last7Day" for r in state["waf"] if r.url.path.endswith(("/statistics", "/trends")))


def test_effective_ce_license_never_passes_an_ignored_application_filter(dashboard_app):
    client, state = dashboard_app
    state["edition"] = {"version": 2, "licensed_edition": 2, "effective_edition": 0}
    headers = configure(client)
    data = client.get(BASE + "/overview?site_id=12", headers=headers).json()
    assert data["sections"] == {}
    assert set(data["errors"].values()) == {"safeline_statistics_scope_unavailable"}
    assert len(state["waf"]) == 1 and state["waf"][0].url.path == "/api/open/system/edition"


def test_cn_pro_application_query_does_not_impersonate_security_app_statistics(dashboard_app):
    client, state = dashboard_app
    state["edition"] = {"version": 1}
    headers = configure(client)
    data = client.get(BASE + "/overview?site_id=12", headers=headers).json()
    assert "access" in data["sections"] and "posture" not in data["sections"]
    assert data["errors"]["posture"] == "safeline_statistics_scope_unavailable"


def test_local_today_is_calculated_with_the_browser_offset(dashboard_app):
    client, _ = dashboard_app
    headers = configure(client)
    data = client.get(BASE + "/overview?preset=today&utc_offset=-480", headers=headers).json()
    assert not data["errors"]
    assert (data["begin_time"] + 8 * 3600) % 86400 == 0
    assert 0 <= data["end_time"] - data["begin_time"] < 86400


@pytest.mark.parametrize("failure", ["/api/stat/advance/location", "/api/open/security_posture/statistics", "/api/open/security_posture/realtime", "/api/stat/advance/error_status_code"])
def test_a_failed_panel_does_not_replace_other_data_with_zero(dashboard_app, failure):
    client, state = dashboard_app
    headers = configure(client)
    state["failure"] = failure
    response = client.get(BASE + "/overview", headers=headers)
    data = response.json()
    assert response.status_code == 200 and data["sections"]["access"]["access"] == 0
    assert data["errors"] and TOKEN not in response.text
    if failure.endswith("location"):
        assert len(data["errors"]) == 4 and "geo_world_access" not in data["sections"]
    elif failure.endswith("error_status_code"):
        assert "error_upstream" not in data["sections"] and "error_waf" not in data["sections"]


def test_realtime_response_must_match_the_selected_application(dashboard_app):
    client, state = dashboard_app
    headers = configure(client)
    state["wrong_event_scope"] = True
    data = client.get(BASE + "/overview?site_id=12", headers=headers).json()
    assert data["errors"]["realtime"] == "safeline_statistics_scope_unavailable" and "realtime" not in data["sections"]


@pytest.mark.parametrize("query", ["preset=unknown", "site_id=-1", "site_id=0", "site_id=true", "utc_offset=-999", "utc_offset=1.5", "utc_offset=+8",
    "preset=last1Day&preset=last7Day", "hours=24&preset=last7Day", "url=https://evil.example", "global=true", "count=1000"])
def test_dashboard_query_boundary_rejects_arbitrary_upstream_parameters(dashboard_app, query):
    client, state = dashboard_app
    headers = configure(client)
    response = client.get(BASE + "/overview?" + query, headers=headers)
    assert response.status_code == 422 and not state["waf"]


@pytest.mark.parametrize("value", [None, {}, {"version": True}, {"version": "2"}, {"version": 999}, {"effective_edition": None, "version": 2}])
def test_unknown_edition_fails_closed_on_extended_ranges_and_application_scope(value):
    caps = dashboard.capability(value)
    assert caps["traffic"]["presets"] == ["last1Day"] and not caps["traffic"]["applications"]
    assert caps["security"]["presets"] == ["last1Day"] and not caps["security"]["applications"]
