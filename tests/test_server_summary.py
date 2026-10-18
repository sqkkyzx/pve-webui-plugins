import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from plugins.komodo.komodo import KomodoDashboardError
from plugins.komodo.server_summary import HISTORY_INTERVALS, get_server_history, get_server_summary, get_server_summary_capabilities


ORIGIN, KEY, SECRET = "http://core:9120", "summary-api-key", "summary-api-secret"
SERVER, OTHER, UPDATE, CONTAINER = "a" * 24, "b" * 24, "c" * 24, "d" * 64
TS = 1792256400123


def responses():
    return {
        "GetServer": {"_id": {"$oid": SERVER}, "name": "docker-vm", "template": False,
                      "description": "Docker workloads\nManaged by operations", "config": {"enabled": True, "passkey": SECRET},
                      "info": {"private_key": SECRET}},
        "ListServers": [{"id": SERVER, "name": "docker-vm", "template": False,
                         "info": {"state": "Ok", "version": "2.3.3", "core_count": 2, "logical_core_count": 4,
                                  "address": "https://private.invalid/" + SECRET, "public_key": SECRET}}],
        "GetCoreInfo": {"ui_write_disabled": False, "webhook_base_url": SECRET},
        "GetPermission": {"level": "Write", "specific": ["Processes"]},
        "GetSystemStats": {"cpu_perc": 25.5, "load_average": {"one": 1.1, "five": 2.2, "fifteen": 3.3},
                           "mem_used_gb": 2, "mem_total_gb": 8, "mem_buff_cache_gb": 1, "mem_zfs_arc_gb": 0.5,
                           "disks": [{"mount": "/", "file_system": "ext4", "used_gb": 4, "total_gb": 10},
                                     {"mount": "/data", "file_system": "xfs", "used_gb": 2, "total_gb": 10}],
                           "network_ingress_bytes": 8366, "network_egress_bytes": 276, "polling_rate": "5-sec", "refresh_ts": TS},
        "GetSystemInformation": {"core_count": 4, "logical_core_count": 8, "host_name": "private-host", "kernel": "private-kernel"},
        "ListContainers": [{"id": CONTAINER, "name": "app", "server_id": SERVER, "state": "running",
                            "env": [SECRET], "labels": {"password": SECRET}, "stats": {
                                "cpu_perc": "2.50%", "mem_perc": "3.25%", "mem_usage": "20MiB / 1GiB",
                                "net_io": "2kB / 3kB", "block_io": "0B / 2MB", "pids": "7", "private": SECRET}}],
        "ListSystemProcesses": [{"pid": 42, "name": "python", "exe": "/usr/bin/python3", "cmd": ["python", "--password", SECRET],
                                 "cpu_perc": 250.5, "mem_mb": 128.25, "disk_read_kb": 99, "disk_write_kb": 12}],
        "ListUpdates": {"updates": [{"id": UPDATE, "operation": "RestartAllContainers", "start_ts": TS,
                                     "status": "Complete", "success": True, "username": "operator", "operator": OTHER,
                                     "target": {"type": "Server", "id": SERVER}, "other_data": SECRET}], "next_page": None},
        "GetHistoricalServerStats": {"stats": [{"sid": SERVER, "ts": TS, "cpu_perc": 25.5,
                                                "load_average": {"one": 1, "five": 2, "fifteen": 3},
                                                "mem_used_gb": 2, "mem_total_gb": 8, "mem_buff_cache_gb": 1, "mem_zfs_arc_gb": 0.5,
                                                "disk_used_gb": 6, "disk_total_gb": 20,
                                                "network_ingress_bytes": 8366, "network_egress_bytes": 276}], "next_page": None},
    }


def handler_for(raw=None, *, overrides=None, seen=None, interval="1-day"):
    raw = raw if raw is not None else responses()
    overrides = overrides or {}

    def upstream(request):
        if seen is not None:
            seen.append(request)
        assert request.headers["x-api-key"] == KEY and request.headers["x-api-secret"] == SECRET
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert str(request.url).startswith(ORIGIN + "/")
        if request.url.path == "/user":
            assert request.method == "GET"
            return overrides.get("user") or httpx.Response(200, json={"username": "integration", "enabled": True}, headers={
                "set-cookie": "PVEAuthCookie=must-not-replay; Path=/",
            })
        assert request.method == "POST" and request.url.path == "/read", "summary must never mutate an upstream"
        payload = json.loads(request.content)
        variant, params = payload["type"], payload["params"]
        if variant == "ListServers":
            assert params == {"query": {"names": [SERVER], "templates": "Include"}, "limit": 0}
        elif variant == "GetPermission":
            assert params == {"target": {"type": "Server", "id": SERVER}}
        elif variant == "GetCoreInfo":
            assert params == {}
        elif variant == "ListUpdates":
            assert params == {"page": 0, "query": {"target.type": "Server", "target.id": SERVER}}
        elif variant == "GetHistoricalServerStats":
            assert params == {"server": SERVER, "granularity": interval, "page": 0}
        else:
            assert variant in {"GetServer", "GetSystemStats", "GetSystemInformation", "ListContainers", "ListSystemProcesses"}
            assert params == {"server": SERVER}
        if variant in overrides:
            response = overrides[variant]
            if isinstance(response, Exception):
                raise response
            return response
        return httpx.Response(200, json=raw[variant], headers={"set-cookie": "other-private=must-not-replay; Path=/"})

    return upstream


def run(handler, *, mode="summary", server_id=SERVER, interval="1-day", secret=SECRET):
    options = dict(origin=ORIGIN, key=KEY, secret=secret, verify=True, transport=httpx.MockTransport(handler))
    if mode == "history":
        return asyncio.run(get_server_history(server_id, interval=interval, **options))
    if mode == "capabilities":
        return asyncio.run(get_server_summary_capabilities(server_id, **options))
    return asyncio.run(get_server_summary(server_id, **options))


def assert_private(result):
    serialized = json.dumps(result, allow_nan=False)
    for value in (KEY, SECRET, "private.invalid", "private-host", "private-kernel", "PVEAuthCookie"):
        assert value not in serialized


def test_summary_combines_current_tables_description_and_history_without_exposing_config_or_command_args():
    seen = []
    result = run(handler_for(seen=seen))
    assert len(seen) == 10
    assert result["server_name"] == "docker-vm" and result["server_state"] == "Ok"
    assert result["description"] == "Docker workloads\nManaged by operations"
    assert result["can_write"] is result["can_execute"] is result["enabled"] is True
    assert result["current"] == {
        "cpu_percent": 25.5, "load_average": [1.1, 2.2, 3.3],
        "memory_used_gb": 2.0, "memory_total_gb": 8.0, "memory_cache_gb": 1.0, "memory_arc_gb": 0.5, "memory_percent": 25.0,
        "disk_used_gb": 6.0, "disk_total_gb": 20.0, "disk_percent": 30.0,
        "network_ingress_bytes_per_second": 1673.2, "network_egress_bytes_per_second": 55.2,
        "core_count": 4, "logical_core_count": 8, "version": "2.3.3", "refresh_ts": TS, "uptime_seconds": None,
    }
    assert result["containers"] == [{"id": CONTAINER, "name": "app", "state": "running", "cpu_percent": "2.50%",
                                      "memory_percent": "3.25%", "memory_usage": "20MiB / 1GiB", "network_io": "2kB / 3kB",
                                      "block_io": "0B / 2MB", "pids": "7"}]
    assert result["processes"] == [{"pid": 42, "name": "python", "exe": "/usr/bin/python3", "cpu_percent": 250.5, "memory_mb": 128.25}]
    assert result["disks"] == [{"mount": "/", "file_system": "ext4", "used_gb": 4.0, "total_gb": 10.0, "percent": 40.0},
                                {"mount": "/data", "file_system": "xfs", "used_gb": 2.0, "total_gb": 10.0, "percent": 20.0}]
    assert result["updates"][0]["resource_name"] == "docker-vm"
    assert result["errors"] == []
    assert_private(result)


def test_capability_preflight_avoids_every_metrics_process_and_update_read():
    seen = []
    result = run(handler_for(seen=seen), mode="capabilities")
    assert len(seen) == 5
    assert {json.loads(request.content)["type"] for request in seen[1:]} == {"GetServer", "ListServers", "GetPermission", "GetCoreInfo"}
    assert result["can_execute"] is True
    assert "current" not in result and "description" not in result
    assert_private(result)


@pytest.mark.parametrize("level,disabled,enabled,state,execute,write", [
    ("Read", False, True, "Ok", False, False), ("Execute", False, True, "Ok", True, False),
    ("Write", False, True, "Ok", True, True), ("Write", True, True, "Ok", True, False),
    ("Write", False, False, "Ok", False, True), ("Write", False, True, "Disabled", False, True),
    ("Write", False, True, "NotOk", False, True),
])
def test_capabilities_match_permission_global_policy_enabled_and_live_state(level, disabled, enabled, state, execute, write):
    raw = responses()
    raw["GetPermission"]["level"] = level
    raw["GetCoreInfo"]["ui_write_disabled"] = disabled
    raw["GetServer"]["config"]["enabled"] = enabled
    raw["ListServers"][0]["info"]["state"] = state
    result = run(handler_for(raw), mode="capabilities")
    assert result["can_execute"] is execute and result["can_write"] is write


@pytest.mark.parametrize("state", ["NotOk", "Disabled"])
def test_offline_servers_keep_description_and_updates_but_never_show_cached_realtime_measurements(state):
    raw = responses()
    raw["ListServers"][0]["info"]["state"] = state
    seen = []
    result = run(handler_for(raw, seen=seen))
    assert len(seen) == 6
    assert all(result["current"][key] is None for key in ("cpu_percent", "load_average", "memory_percent", "network_ingress_bytes_per_second", "disk_percent"))
    assert result["containers"] == result["disks"] == result["processes"] == []
    assert result["updates"] and result["description"]
    assert result["errors"] == ["server_disconnected"]
    assert result["can_execute"] is False


@pytest.mark.parametrize("rpc,response,code", [
    ("GetSystemStats", httpx.Response(403, text=SECRET), "stats_permission_denied"),
    ("ListSystemProcesses", httpx.Response(403, text=SECRET), "processes_permission_denied"),
    ("ListContainers", httpx.ReadTimeout(SECRET), "containers_unavailable"),
    ("ListUpdates", httpx.Response(500, text=SECRET), "updates_unavailable"),
    ("GetSystemInformation", httpx.Response(302, headers={"location": "https://private.invalid/" + SECRET}), "information_unavailable"),
])
def test_one_section_failure_keeps_other_sections_and_returns_only_fixed_safe_codes(rpc, response, code):
    result = run(handler_for(overrides={rpc: response}))
    assert code in result["errors"]
    if rpc != "GetSystemStats":
        assert result["current"]["cpu_percent"] == 25.5
    if rpc != "ListContainers":
        assert result["containers"]
    if rpc != "ListUpdates":
        assert result["updates"]
    assert_private(result)


@pytest.mark.parametrize("rpc", ["GetCoreInfo", "GetPermission"])
def test_capability_query_failure_fails_closed_without_hiding_statistics(rpc):
    result = run(handler_for(overrides={rpc: httpx.Response(403, text=SECRET)}))
    assert result["can_write"] is False
    assert result["can_execute"] is (rpc == "GetCoreInfo")
    assert result["current"]["cpu_percent"] == 25.5
    assert_private(result)


def test_partial_or_invalid_numbers_remain_null_while_true_zero_is_preserved():
    raw = responses()
    raw["GetSystemStats"].update(cpu_perc=0, mem_used_gb=None, mem_total_gb=0, load_average=None,
                                 network_ingress_bytes=0, network_egress_bytes=None)
    raw["GetSystemStats"]["disks"] = []
    result = run(handler_for(raw))
    assert result["current"]["cpu_percent"] == 0
    assert result["current"]["network_ingress_bytes_per_second"] == 0
    assert result["current"]["network_egress_bytes_per_second"] is None
    assert result["current"]["memory_percent"] is None
    assert result["current"]["disk_used_gb"] is result["current"]["disk_total_gb"] is result["current"]["disk_percent"] is None


@pytest.mark.parametrize("polling_rate,ingress", [("1-sec", 8366), ("1-min", 8366 / 60), ("invalid", None), (None, None)])
def test_realtime_network_is_delta_bytes_divided_by_actual_periphery_polling_seconds(polling_rate, ingress):
    raw = responses()
    raw["GetSystemStats"]["polling_rate"] = polling_rate
    result = run(handler_for(raw))
    assert result["current"]["network_ingress_bytes_per_second"] == ingress


def test_formatted_container_metrics_do_not_allow_error_text_or_hidden_fields():
    raw = responses()
    raw["ListContainers"][0]["stats"].update(cpu_perc=SECRET, net_io="https://private.invalid/", pids=True)
    raw["ListContainers"].append({"id": "e" * 64, "name": "stopped", "state": "exited", "stats": None})
    result = run(handler_for(raw))
    assert len(result["containers"]) == 1
    assert result["containers"][0]["cpu_percent"] is result["containers"][0]["network_io"] is result["containers"][0]["pids"] is None
    assert_private(result)


def test_last_twenty_updates_are_bound_to_server_and_do_not_fetch_each_detail():
    raw = responses()
    original = raw["ListUpdates"]["updates"][0]
    raw["ListUpdates"]["updates"] = [{**deepcopy(original), "start_ts": TS + index} for index in range(25)]
    raw["ListUpdates"]["updates"].append({**deepcopy(original), "target": {"type": "Server", "id": OTHER}})
    raw["ListUpdates"]["updates"][0].update(status="InProgress", success=False)
    seen = []
    result = run(handler_for(raw, seen=seen))
    assert len(result["updates"]) == 20
    assert [row["start_ts"] for row in result["updates"]] == list(range(TS + 24, TS + 4, -1))
    assert "updates_invalid_response" in result["errors"]
    assert all(json.loads(request.content)["type"] != "GetUpdate" for request in seen[1:])


@pytest.mark.parametrize("interval", HISTORY_INTERVALS)
def test_history_uses_all_original_sampling_intervals_not_a_time_range(interval):
    result = run(handler_for(interval=interval), mode="history", interval=interval)
    assert result["interval"] == interval
    assert len(result["points"]) == 1


def test_history_preserves_sparse_millisecond_points_and_raw_network_bytes_without_inventing_rates():
    raw = responses()
    latest = raw["GetHistoricalServerStats"]["stats"][0]
    older = {**deepcopy(latest), "ts": TS - 3 * 86_400_000, "cpu_perc": 0, "network_ingress_bytes": 0}
    raw["GetHistoricalServerStats"]["stats"] = [latest, older]
    seen = []
    result = run(handler_for(raw, seen=seen), mode="history")
    assert len(seen) == 3  # identity + GetServer + one history request
    assert [point["ts"] for point in result["points"]] == [older["ts"], TS]
    assert len(result["points"]) == 2  # no invented missing daily point
    assert result["points"][0]["cpu_percent"] == result["points"][0]["network_ingress_bytes"] == 0
    assert result["points"][1]["network_ingress_bytes"] == 8366
    assert result["points"][1]["load_average"] == [1.0, 2.0, 3.0]
    assert result["points"][1]["disk_percent"] == 30
    assert result["network_unit"] == "bytes_per_sample"
    assert "network_ingress_bytes_per_second" not in result["points"][1]
    assert_private(result)


def test_empty_historical_page_is_valid_and_disconnected_server_history_remains_available():
    raw = responses()
    raw["GetServer"]["config"]["enabled"] = False
    raw["GetHistoricalServerStats"]["stats"] = []
    result = run(handler_for(raw), mode="history")
    assert result["points"] == [] and result["errors"] == []


@pytest.mark.parametrize("change", ["wrong_server", "bad_timestamp", "duplicate_timestamp", "non_object"])
def test_historical_points_must_match_server_and_valid_unique_timestamp(change):
    raw = responses()
    source = raw["GetHistoricalServerStats"]["stats"][0]
    if change == "wrong_server":
        source["sid"] = OTHER
    if change == "bad_timestamp":
        source["ts"] = True
    if change == "duplicate_timestamp":
        raw["GetHistoricalServerStats"]["stats"].append(deepcopy(source))
    if change == "non_object":
        raw["GetHistoricalServerStats"]["stats"] = [SECRET]
    result = run(handler_for(raw), mode="history")
    assert len(result["points"]) == (1 if change == "duplicate_timestamp" else 0)
    assert result["errors"] == ["history_invalid_response"]
    assert_private(result)


@pytest.mark.parametrize("response,code", [
    (httpx.Response(403, text=SECRET), "history_permission_denied"),
    (httpx.ReadTimeout(SECRET), "history_unavailable"), (httpx.Response(200, json={"stats": {}}), "history_invalid_response"),
])
def test_history_failures_are_safe_empty_sections(response, code):
    result = run(handler_for(overrides={"GetHistoricalServerStats": response}), mode="history")
    assert result["points"] == [] and result["errors"] == [code]
    assert_private(result)


@pytest.mark.parametrize("mode", ["summary", "capabilities", "history"])
def test_missing_credentials_disabled_identity_and_invalid_server_never_fetch_metrics(mode):
    def unexpected(request):
        pytest.fail("invalid summary configuration must not connect")
    with pytest.raises(KomodoDashboardError) as error:
        run(unexpected, mode=mode, server_id="server-name")
    assert error.value.code == "invalid_request"
    with pytest.raises(KomodoDashboardError) as error:
        run(unexpected, mode=mode, secret=None)
    assert error.value.code == "not_configured"
    seen = []
    with pytest.raises(KomodoDashboardError) as error:
        run(handler_for(overrides={"user": httpx.Response(200, json={"username": "integration", "enabled": False})}, seen=seen), mode=mode)
    assert error.value.code == "invalid_credentials"
    assert len(seen) == 1


@pytest.mark.parametrize("interval", ["1h", "24h", "", True, {"$ne": "1-day"}])
def test_unknown_history_interval_never_connects(interval):
    def unexpected(request):
        pytest.fail("invalid sampling interval must not connect")
    with pytest.raises(KomodoDashboardError) as error:
        run(unexpected, mode="history", interval=interval)
    assert error.value.code == "invalid_request"


def test_wrong_primary_resource_or_template_is_rejected_before_exposing_another_servers_data():
    for change in ({"template": True}, {"_id": {"$oid": OTHER}}):
        raw = responses()
        raw["GetServer"].update(change)
        with pytest.raises(KomodoDashboardError) as error:
            run(handler_for(raw))
        assert error.value.code == "komodo_server_unavailable"
