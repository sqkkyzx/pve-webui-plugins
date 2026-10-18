import asyncio
from datetime import datetime
import json
import math

import httpx
import pytest

from plugins.komodo.alerts import ALERT_TYPES, RESOURCE_TYPES
from plugins.komodo.komodo import KomodoClient, KomodoDashboardError


ORIGIN = "http://core:9120"
KEY, SECRET = "alerts-only-api-key", "alerts-only-api-secret"
ALERT, RESOURCE, OTHER, HOST = "a" * 24, "b" * 24, "c" * 24, "d" * 24
TS, END = 1792256400123, 1792256460456


def alert(variant="ServerCpu", payload=None, *, resource_type="Server", resource_id=RESOURCE):
    return {
        "_id": {"$oid": ALERT}, "ts": TS, "resolved_ts": END, "resolved": True,
        "level": "WARNING", "target": {"type": resource_type, "id": resource_id},
        "data": {"type": variant, "data": payload if payload is not None else {
            "id": RESOURCE, "name": "historical-server", "region": "test-region", "percentage": 92.5,
        }},
    }


def client(handler, *, secret=SECRET):
    return KomodoClient(ORIGIN, KEY, secret, transport=httpx.MockTransport(handler))


def run(handler, **filters):
    return asyncio.run(client(handler).alerts(**filters))


def handler_for(rows=None, *, next_page=None, expected_page=0, expected_query=None, response=None, seen=None):
    def upstream(request):
        if seen is not None:
            seen.append(request)
        assert request.headers["x-api-key"] == KEY
        assert request.headers["x-api-secret"] == SECRET
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        if request.url.path == "/user":
            assert request.method == "GET"
            return httpx.Response(200, json={"username": "integration", "enabled": True}, headers={
                "set-cookie": "PVEAuthCookie=must-not-replay; Path=/",
            })
        assert str(request.url) == ORIGIN + "/read"
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "type": "ListAlerts", "params": {"page": expected_page, "query": expected_query},
        }
        if isinstance(response, Exception):
            raise response
        return response if response is not None else httpx.Response(200, json={
            "alerts": rows if rows is not None else [alert()], "next_page": next_page,
        })

    return upstream


def assert_safe(result):
    text = json.dumps(result, allow_nan=False)
    for forbidden in (KEY, SECRET, "private.invalid", "PVEAuthCookie", "traceback-private"):
        assert forbidden not in text


def test_list_alerts_fixed_read_returns_safe_details_and_original_millisecond_timestamps():
    seen = []
    result = run(handler_for(seen=seen))
    assert [request.url.path for request in seen] == ["/user", "/read"]
    assert result["page"] == 0 and result["next_page"] is None
    assert datetime.fromisoformat(result["updated_at"]).utcoffset().total_seconds() == 0
    assert result["alerts"] == [{
        "id": ALERT, "ts": TS, "resolved_ts": END, "resolved": True, "level": "WARNING",
        "resource_type": "Server", "resource_id": RESOURCE, "resource_name": "historical-server",
        "alert_type": "ServerCpu", "details": {"id": RESOURCE, "name": "historical-server", "region": "test-region", "percentage": 92.5},
        "details_omitted": False,
    }]
    assert_safe(result)


def test_only_three_allowlisted_mongo_keys_can_be_constructed_from_typed_filters():
    query = {"target.type": "Server", "data.type": "ServerDisk", "resolved": False}
    result = run(handler_for(expected_page=4, expected_query=query), page=4, resource_type="Server", alert_type="ServerDisk", unresolved_only=True)
    assert result["page"] == 4


@pytest.mark.parametrize("resource_type", RESOURCE_TYPES)
def test_all_source_resource_enums_can_filter_without_arbitrary_query(resource_type):
    run(handler_for(expected_query={"target.type": resource_type}), resource_type=resource_type)


@pytest.mark.parametrize("alert_type", ALERT_TYPES)
def test_all_source_alert_enums_can_filter(alert_type):
    run(handler_for(expected_query={"data.type": alert_type}), alert_type=alert_type)


@pytest.mark.parametrize("filters", [
    {"page": -1}, {"page": True}, {"page": 1.0}, {"page": "1"}, {"page": 1_000_001},
    {"resource_type": "Server.$where"}, {"resource_type": {"$ne": "Server"}}, {"resource_type": None},
    {"alert_type": "servercpu"}, {"alert_type": {"$ne": "Custom"}}, {"alert_type": None},
    {"unresolved_only": "true"}, {"unresolved_only": 1}, {"unresolved_only": None},
])
def test_invalid_filters_fail_before_any_network(filters):
    def unexpected(request):
        pytest.fail("invalid filters must not connect")

    with pytest.raises(KomodoDashboardError) as error:
        run(unexpected, **filters)
    assert error.value.code == "invalid_request"
    assert error.value.status_code == 400


def test_unconfigured_client_never_contacts_komodo():
    def unexpected(request):
        pytest.fail("missing credentials must not connect")

    with pytest.raises(KomodoDashboardError) as error:
        asyncio.run(client(unexpected, secret=None).alerts())
    assert error.value.code == "not_configured"


@pytest.mark.parametrize("response,code", [
    (httpx.Response(401, text=SECRET), "invalid_credentials"),
    (httpx.Response(403, text=SECRET), "invalid_credentials"),
    (httpx.Response(200, json={"username": "service", "enabled": False}), "invalid_credentials"),
    (httpx.Response(200, json={"username": "service", "enabled": "true"}), "invalid_response"),
    (httpx.Response(200, text=SECRET), "invalid_response"),
    (httpx.Response(302, headers={"location": "https://private.invalid/" + SECRET}), "upstream_unavailable"),
])
def test_invalid_identity_never_reads_alerts(response, code):
    seen = []

    def upstream(request):
        seen.append(request)
        assert request.url.path == "/user"
        return response

    with pytest.raises(KomodoDashboardError) as error:
        run(upstream)
    assert error.value.code == code
    assert len(seen) == 1
    assert SECRET not in repr(error.value)


@pytest.mark.parametrize("response,code", [
    (httpx.Response(401, text=SECRET), "invalid_credentials"),
    (httpx.Response(403, text=SECRET), "permission_denied"),
    (httpx.Response(500, text=SECRET), "upstream_unavailable"),
    (httpx.Response(302, headers={"location": "https://private.invalid/" + SECRET}), "upstream_unavailable"),
    (httpx.Response(200, text=SECRET), "invalid_response"),
    (httpx.ReadTimeout(SECRET), "upstream_unavailable"),
])
def test_read_failures_do_not_follow_redirects_or_leak_error_details(response, code, caplog):
    seen = []
    with pytest.raises(KomodoDashboardError) as error:
        run(handler_for(response=response, seen=seen))
    assert error.value.code == code
    assert len(seen) == 2
    assert SECRET not in repr(error.value)
    assert SECRET not in caplog.text


@pytest.mark.parametrize("value", [True, -1, 1.5, "1792256400123", None, 8_640_000_000_000_001])
def test_invalid_timestamps_are_null_without_zero_substitution_or_unit_guessing(value):
    row = alert()
    row.update(ts=value, resolved_ts=value)
    result = run(handler_for(rows=[row]))
    assert result["alerts"][0]["ts"] is None
    assert result["alerts"][0]["resolved_ts"] is None


def test_true_epoch_zero_and_earlier_resolution_are_preserved_for_ui_duration_validation():
    rows = [alert(), alert(), alert()]
    rows[0].update(ts=0, resolved_ts=0)
    rows[1].update(ts=TS, resolved_ts=TS - 1)
    # CloseAlert marks resolved without necessarily setting resolved_ts.
    rows[2].update(resolved=True, resolved_ts=None)
    result = run(handler_for(rows=rows))
    assert result["alerts"][0]["ts"] == result["alerts"][0]["resolved_ts"] == 0
    assert result["alerts"][1]["resolved_ts"] == TS - 1
    assert result["alerts"][2]["resolved"] is True
    assert result["alerts"][2]["resolved_ts"] is None


@pytest.mark.parametrize("next_page", [0, -1, 2, True, "1", 1.0])
def test_next_page_must_be_exactly_one_forward_integer_or_null(next_page):
    with pytest.raises(KomodoDashboardError) as error:
        run(handler_for(next_page=next_page))
    assert error.value.code == "invalid_response"


def test_full_page_can_point_to_an_empty_following_page():
    result = run(handler_for(rows=[alert()] * 100, next_page=1))
    assert result["next_page"] == 1 and len(result["alerts"]) == 100
    following = run(handler_for(rows=[], expected_page=1), page=1)
    assert following["next_page"] is None and following["alerts"] == []


def test_last_supported_page_does_not_advertise_an_unrequestable_page():
    result = run(handler_for(next_page=1_000_001, expected_page=1_000_000), page=1_000_000)
    assert result["next_page"] is None


@pytest.mark.parametrize("value", [None, "false", 0])
def test_unknown_resolution_state_is_not_mislabeled_unresolved(value):
    row = alert()
    row["resolved"] = value
    result = run(handler_for(rows=[row]))
    assert result["alerts"][0]["resolved"] is None


@pytest.mark.parametrize("response", [
    {"alerts": [], "next_page": None, "error": SECRET},
    {"alerts": []}, {"alerts": {}, "next_page": None},
    {"alerts": [SECRET], "next_page": None}, {"alerts": [alert()] * 101, "next_page": 1},
])
def test_response_shape_is_checked_or_extra_top_level_fields_discarded(response):
    if "error" in response:
        result = run(handler_for(response=httpx.Response(200, json=response)))
        assert_safe(result)
        assert "error" not in result
    else:
        with pytest.raises(KomodoDashboardError) as error:
            run(handler_for(response=httpx.Response(200, json=response)))
        assert error.value.code == "invalid_response"


def test_unreachable_alert_omits_raw_error_while_preserving_historical_deleted_resource_name():
    row = alert("ServerUnreachable", {
        "id": RESOURCE, "name": "deleted-host", "region": "edge",
        "err": {"error": SECRET, "trace": ["https://user:" + KEY + "@private.invalid", "traceback-private"]},
    })
    result = run(handler_for(rows=[row]))
    assert result["alerts"][0]["resource_name"] == "deleted-host"
    assert result["alerts"][0]["details"] == {"id": RESOURCE, "name": "deleted-host", "region": "edge"}
    assert result["alerts"][0]["details_omitted"] is True
    assert_safe(result)


@pytest.mark.parametrize("target_type,target_id", [("Server", OTHER), ("Stack", RESOURCE), ("unknown", RESOURCE)])
def test_resource_name_is_never_attached_to_a_mismatched_target(target_type, target_id):
    row = alert(resource_type=target_type, resource_id=target_id)
    result = run(handler_for(rows=[row]))
    assert result["alerts"][0]["resource_name"] is None
    assert "name" not in result["alerts"][0]["details"]
    assert result["alerts"][0]["details_omitted"] is True


@pytest.mark.parametrize("target_id", ["system", "", None])
def test_system_targets_and_none_alerts_do_not_require_object_ids(target_id):
    row = alert("None", {}, resource_type="System", resource_id=target_id)
    result = run(handler_for(rows=[row]))
    assert result["alerts"][0]["resource_type"] == "System"
    assert result["alerts"][0]["resource_id"] == target_id
    assert result["alerts"][0]["details"] == {}
    assert result["alerts"][0]["details_omitted"] is False


@pytest.mark.parametrize("variant,payload", [
    ("Custom", {"message": SECRET, "details": "https://user:" + KEY + "@private.invalid"}),
    ("AwsBuilderTerminationFailed", {"instance_id": "i-0123456789abcdef0", "message": SECRET}),
    ("FuturePrivateType", {"name": SECRET, "err": KEY, "nested": {"message": "traceback-private"}}),
])
def test_freeform_custom_and_future_payloads_are_never_exposed(variant, payload):
    result = run(handler_for(rows=[alert(variant, payload, resource_type="System", resource_id="system")]))
    assert result["alerts"][0]["details_omitted"] is True
    assert result["alerts"][0]["details"] == ({"instance_id": "i-0123456789abcdef0"} if variant == "AwsBuilderTerminationFailed" else {})
    assert_safe(result)


@pytest.mark.parametrize("variant,resource_type,payload,expected", [
    ("ServerMem", "Server", {"used_gb": 2.5, "total_gb": 4}, {"used_gb": 2.5, "total_gb": 4.0}),
    ("ServerDisk", "Server", {"path": "/var/lib/docker", "used_gb": 4, "total_gb": 10}, {"path": "/var/lib/docker", "used_gb": 4.0, "total_gb": 10.0}),
    ("ServerVersionMismatch", "Server", {"server_version": "2.3.3", "core_version": "2.3.4"}, {"server_version": "2.3.3", "core_version": "2.3.4"}),
    ("ContainerStateChange", "Deployment", {"from": "running", "to": "not_deployed", "server_id": HOST, "server_name": "vm"}, {"from": "running", "to": "not_deployed", "server_id": HOST, "server_name": "vm"}),
    ("StackStateChange", "Stack", {"from": "running", "to": "down", "swarm_id": HOST, "swarm_name": "swarm"}, {"from": "running", "to": "down", "swarm_id": HOST, "swarm_name": "swarm"}),
    ("StackImageUpdateAvailable", "Stack", {"image": "registry.local/app:v2", "service": "app"}, {"image": "registry.local/app:v2", "service": "app"}),
    ("StackAutoUpdated", "Stack", {"images": ["app:v1", "app:v2"]}, {"images": ["app:v1", "app:v2"]}),
    ("BuildFailed", "Build", {"version": {"major": 2, "minor": 3, "patch": 4}}, {"version": {"major": 2, "minor": 3, "patch": 4}}),
    ("ScheduleRun", "Procedure", {"resource_type": "Procedure"}, {"resource_type": "Procedure"}),
])
def test_typed_variant_details_match_source_units_and_enums(variant, resource_type, payload, expected):
    payload = {"id": RESOURCE, "name": "historical-resource", **payload}
    result = run(handler_for(rows=[alert(variant, payload, resource_type=resource_type)]))
    assert result["alerts"][0]["details"] == {"id": RESOURCE, "name": "historical-resource", **expected}
    assert result["alerts"][0]["details_omitted"] is False


@pytest.mark.parametrize("variant,payload", [
    ("ServerCpu", {"percentage": True}), ("ServerCpu", {"percentage": -1}),
    ("ServerMem", {"used_gb": math.nan, "total_gb": math.inf}),
    ("ServerDisk", {"path": "bad\npath", "used_gb": "5"}),
    ("ServerVersionMismatch", {"server_version": SECRET}),
    ("StackImageUpdateAvailable", {"image": "https://user:" + SECRET + "@private.invalid", "service": "bad/service"}),
    ("StackAutoUpdated", {"images": ["app:v1", "https://user:" + SECRET + "@private.invalid"]}),
    ("BuildFailed", {"version": {"major": True, "minor": 0, "patch": 0}}),
])
def test_invalid_typed_details_are_omitted_not_coerced(variant, payload):
    # JSON cannot encode non-finite values: validate DTO directly for those.
    if variant == "ServerMem":
        from plugins.komodo.alerts import _alert

        result = {"alerts": [_alert(alert(variant, payload))]}
    else:
        result = run(handler_for(rows=[alert(variant, payload)]))
    assert result["alerts"][0]["details"] == {}
    assert result["alerts"][0]["details_omitted"] is True
    assert_safe(result)


def test_unknown_level_and_malformed_enums_do_not_echo_raw_values():
    row = alert()
    row.update(level={"key": SECRET}, target={"type": [SECRET], "id": SECRET}, data={"type": {"private": SECRET}, "data": {"err": SECRET}})
    result = run(handler_for(rows=[row]))
    assert result["alerts"][0]["level"] == "unknown"
    assert result["alerts"][0]["resource_type"] == "unknown"
    assert result["alerts"][0]["resource_id"] is None
    assert result["alerts"][0]["alert_type"] == "unknown"
    assert_safe(result)


def test_pve_and_other_callers_cookie_jars_remain_isolated():
    normal = handler_for()

    def upstream(request):
        if request.url.host == "pve.example":
            assert request.headers["cookie"] == "PVEAuthCookie=private-ticket"
            assert "x-api-key" not in request.headers and "x-api-secret" not in request.headers
            return httpx.Response(200, json={"data": []})
        return normal(request)

    async def scenario():
        transport = httpx.MockTransport(upstream)
        async with httpx.AsyncClient(transport=transport, cookies={"PVEAuthCookie": "private-ticket"}) as pve:
            await pve.get("https://pve.example:8006/api2/json/access/permissions")
            komodo = KomodoClient(ORIGIN, KEY, SECRET, transport=transport)
            assert (await komodo.alerts())["alerts"]
            assert (await komodo.alerts())["alerts"]
            await pve.get("https://pve.example:8006/api2/json/access/permissions")

    asyncio.run(scenario())
