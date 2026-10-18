import asyncio
from copy import deepcopy
from datetime import datetime
import json
import math

import httpx
import pytest

from plugins.komodo.dashboard import assemble_dashboard
from plugins.komodo.komodo import KomodoClient, KomodoDashboardError


ORIGIN = "http://komodo-core-1:9120"
KEY = "dashboard-only-key"
SECRET = "dashboard-only-secret"
FORBIDDEN = (KEY, SECRET, "private-address.invalid", "private-env-value", "PVEAuthCookie")


def resources():
    return {
        "ListServers": [{
            "id": "server-one", "name": "docker-vm", "template": False, "tags": ["tag-one"],
            "config": {"environment": "private-env-value"},
            "info": {
                "state": "Ok", "core_count": 2, "logical_core_count": 4, "version": "2.3.3",
                "address": "https://private-address.invalid", "public_key": SECRET,
                "err": {"message": SECRET},
                "stats": {
                    "cpu_perc": 5.410822,
                    "load_average": {"one": 3.97, "five": 3.33, "fifteen": 3.13},
                    "mem_used_gb": 0.003235340118408203, "mem_total_gb": 2,
                    "disk_used_gb": 4.713409423828125, "disk_total_gb": 7.838832855224609,
                    "network_ingress_bytes": 8366, "network_egress_bytes": 276,
                    "polling_rate": "5-sec", "refresh_ts": 1792202055001,
                },
            },
        }],
        "ListStacks": [{
            "id": "stack-one", "name": "sample-stack", "template": False, "tags": ["tag-one"],
            "info": {
                "state": "running", "server_id": "server-one", "server_name": "docker-vm",
                "swarm_id": "", "swarm_name": "", "files_on_host": False, "linked_repo": "",
                "repo": "https://user:" + SECRET + "@private-address.invalid/project",
                "repo_link": "https://user:" + KEY + "@private-address.invalid/project",
                "status": SECRET, "file_contents": "private-env-value",
                "services": [{"service": "web"}, {"service": "worker"}],
                "auto_update_all_services": True,
            },
            "config": {"environment": "private-env-value", "file_contents": SECRET},
        }],
        "ListFullStacks": [{
            "_id": {"$oid": "stack-one"}, "name": "sample-stack",
            "config": {"auto_update": False, "auto_update_all_services": True,
                       "poll_for_updates": True, "environment": "private-env-value", "file_contents": SECRET},
            "info": {"deployed_contents": SECRET},
        }],
        "ListProcedures": [{
            "id": "procedure-one", "name": "sample-procedure", "template": False, "tags": ["tag-one"],
            "info": {"state": "Ok", "next_scheduled_run": 1792256400000, "schedule_error": None},
            "config": {"stages": [{"execution": {"secret": SECRET}}]},
        }],
        "ListTags": [{"_id": {"$oid": "tag-one"}, "name": "production", "owner": SECRET}],
        "ListAllContainers": [
            {"id": "container-one", "server_id": "server-one", "state": "running", "image": SECRET},
            {"id": "container-two", "server_id": "server-one", "state": "exited", "environment": SECRET},
        ],
        "ListDeployments": [{"id": "deployment-one", "template": True,
                             "info": {"server_id": "server-one"}, "config": {"environment": SECRET}}],
        "ListRepos": [{"id": "repo-one", "info": {"server_id": "server-one", "repo": SECRET}}],
    }


def run_dashboard(handler, *, url=ORIGIN, key=KEY, secret=SECRET):
    client = KomodoClient(url, key, secret, transport=httpx.MockTransport(handler))
    return asyncio.run(client.dashboard())


def handler_for(data=None, overrides=None, seen=None):
    data = data or resources()
    overrides = overrides or {}

    def handler(request):
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
        assert request.method == "POST"
        assert str(request.url) == ORIGIN + "/read"
        payload = json.loads(request.content)
        variant = payload["type"]
        expected = {} if variant == "ListTags" else {"limit": 0} if variant == "ListAllContainers" else {
            "query": {"templates": "Include"}, "limit": 0,
        }
        assert payload["params"] == expected
        if variant in overrides:
            result = overrides[variant]
            if isinstance(result, Exception):
                raise result
            return result
        return httpx.Response(200, json=data[variant], headers={"set-cookie": "other-session=must-not-replay; Path=/"})

    return handler


def assert_safe(result):
    serialized = json.dumps(result, allow_nan=False)
    for forbidden in FORBIDDEN:
        assert forbidden not in serialized


def test_real_list_item_shape_units_tag_names_and_field_allowlist():
    seen = []
    dashboard = run_dashboard(handler_for(seen=seen))
    assert [str(request.url) for request in seen] == [ORIGIN + "/user"] + [ORIGIN + "/read"] * 8
    assert [json.loads(request.content)["type"] for request in seen[1:]] == [
        "ListServers", "ListStacks", "ListProcedures", "ListTags", "ListFullStacks",
        "ListAllContainers", "ListDeployments", "ListRepos",
    ]
    assert dashboard["errors"] == []
    assert datetime.fromisoformat(dashboard["updated_at"]).utcoffset().total_seconds() == 0
    server = dashboard["servers"][0]
    assert server["cpu_percent"] == 5.410822
    assert server["memory_percent"] == pytest.approx(0.16176700592041016)
    assert server["disk_percent"] == pytest.approx(4.713409423828125 / 7.838832855224609 * 100)
    assert (server["memory_used_bytes"], server["memory_total_bytes"]) == (3473920, 2147483648)
    assert (server["disk_used_bytes"], server["disk_total_bytes"]) == (5060984832, 8416882688)
    assert server["network_bytes_per_second"] == pytest.approx(1728.4)
    assert server["load_average"] == [3.97, 3.33, 3.13]
    assert (server["core_count"], server["logical_core_count"], server["version"]) == (2, 4, "2.3.3")
    assert server["tags"] == ["production"]
    assert (server["container_count"], server["containers_running"], server["containers_stopped"],
            server["containers_other"], server["resource_count"]) == (2, 1, 1, 0, 3)
    assert dashboard["stacks"][0] == {
        "id": "stack-one", "name": "sample-stack", "template": False, "tags": ["production"],
        "state": "running", "source": "Git 仓库", "server_id": "server-one", "server_name": "docker-vm",
        "swarm_id": None, "swarm_name": None,
        "service_count": 2, "auto_update": False,
    }
    assert dashboard["procedures"][0] == {
        "id": "procedure-one", "name": "sample-procedure", "template": False, "tags": ["production"],
        "state": "Ok", "next_run_at": 1792256400000, "schedule_error": False,
    }
    assert_safe(dashboard)


def test_server_container_counts_classify_states_and_do_not_mix_equal_names_or_other_servers():
    data = resources()
    data["ListAllContainers"] = [
        {"id": str(index), "server_id": "server-one", "name": "same", "state": state}
        for index, state in enumerate(["running", "created", "paused", "exited", "restarting", "dead", "", {"secret": SECRET}])
    ] + [{"id": "0", "server_id": "other-server", "name": "same", "state": "running"}]
    server = run_dashboard(handler_for(data))["servers"][0]
    assert (server["container_count"], server["containers_running"], server["containers_stopped"],
            server["containers_other"]) == (8, 1, 3, 4)
    assert_safe(server)


def test_server_counts_distinguish_real_zero_and_offline_cache_and_config_resources():
    data = resources()
    data["ListAllContainers"] = []
    server = run_dashboard(handler_for(data))["servers"][0]
    assert server["container_count"] == server["containers_running"] == server["containers_stopped"] == 0
    data["ListServers"][0]["info"]["state"] = "NotOk"
    data["ListAllContainers"] = resources()["ListAllContainers"]
    server = run_dashboard(handler_for(data))["servers"][0]
    assert all(server[key] is None for key in ["container_count", "containers_running", "containers_stopped", "containers_other"])
    assert server["resource_count"] == 3
    data["ListStacks"] = data["ListDeployments"] = data["ListRepos"] = []
    assert run_dashboard(handler_for(data))["servers"][0]["resource_count"] == 0


@pytest.mark.parametrize("variant,section", [
    ("ListStacks", "stacks"), ("ListDeployments", "deployments"), ("ListRepos", "repos"),
])
def test_resource_category_failure_never_displays_a_partial_total_as_complete(variant, section):
    dashboard = run_dashboard(handler_for(overrides={variant: httpx.Response(403, text=SECRET)}))
    assert dashboard["servers"][0]["resource_count"] is None
    assert dashboard["servers"][0]["container_count"] == 2
    assert section + "_permission_denied" in dashboard["errors"]
    assert_safe(dashboard)


def test_failed_or_duplicate_container_rows_never_report_false_zero_counts():
    failed = run_dashboard(handler_for(overrides={"ListAllContainers": httpx.Response(503, text=SECRET)}))
    assert failed["servers"][0]["container_count"] is None
    assert failed["servers"][0]["resource_count"] == 3
    data = resources()
    data["ListAllContainers"].append(deepcopy(data["ListAllContainers"][0]))
    invalid = run_dashboard(handler_for(data))
    assert invalid["servers"][0]["container_count"] is None
    assert invalid["errors"] == ["containers_invalid_response"]
    assert_safe(invalid)


def test_resource_count_joins_by_server_id_includes_templates_and_ignores_unassigned_resources():
    data = resources()
    data["ListRepos"] += [{"id": "unassigned", "info": {"server_id": ""}},
                          {"id": "other", "info": {"server_id": "other-server"}}]
    server = run_dashboard(handler_for(data))["servers"][0]
    assert server["resource_count"] == 3
    data["ListRepos"].append({"id": "malformed", "info": {"server_id": None}})
    invalid = run_dashboard(handler_for(data))
    assert invalid["servers"][0]["resource_count"] is None
    assert invalid["errors"] == ["server_resources_invalid_response"]


@pytest.mark.parametrize("services,count", [([], 0), ([{}, {}, {}], 3), (None, None), ("secret", None), ({}, None)])
def test_stack_service_count_preserves_zero_and_unknown(services, count):
    data = resources()
    data["ListStacks"][0]["info"].update(services=services, state="stopped")
    dashboard = run_dashboard(handler_for(data))
    assert dashboard["stacks"][0]["service_count"] == count
    assert_safe(dashboard)


def test_stack_auto_update_joins_by_bson_id_without_exposing_full_config():
    data = resources()
    other = deepcopy(data["ListStacks"][0])
    other.update(id="stack-two", name="sample-stack")
    data["ListStacks"].append(other)
    data["ListFullStacks"].insert(0, {
        "_id": {"$oid": "stack-two"}, "name": "sample-stack",
        "config": {"auto_update": True, "auto_update_all_services": False, "environment": SECRET},
    })
    dashboard = run_dashboard(handler_for(data))
    assert {row["id"]: row["auto_update"] for row in dashboard["stacks"]} == {
        "stack-one": False, "stack-two": True,
    }
    assert_safe(dashboard)


@pytest.mark.parametrize("enabled", [None, "true", "false", 0, 1, {}, []])
def test_invalid_auto_update_values_are_unknown_not_coerced(enabled):
    data = resources()
    data["ListFullStacks"][0]["config"]["auto_update"] = enabled
    dashboard = run_dashboard(handler_for(data))
    assert dashboard["stacks"][0]["auto_update"] is None
    assert dashboard["errors"] == ["stack_settings_invalid_response"]
    assert_safe(dashboard)


def test_missing_stack_setting_never_falls_back_to_name_or_update_scope():
    data = resources()
    data["ListFullStacks"][0]["_id"] = {"$oid": "other-stack"}
    dashboard = run_dashboard(handler_for(data))
    assert dashboard["stacks"][0]["auto_update"] is None
    assert dashboard["stacks"][0]["service_count"] == 2


@pytest.mark.parametrize("response,code", [
    (httpx.Response(403, text=SECRET), "stack_settings_permission_denied"),
    (httpx.Response(503, text=SECRET), "stack_settings_upstream_unavailable"),
    (httpx.Response(200, json={"secret": SECRET}), "stack_settings_invalid_response"),
    (httpx.ReadTimeout(SECRET), "stack_settings_upstream_unavailable"),
])
def test_stack_settings_failure_preserves_service_counts_and_other_tables(response, code):
    dashboard = run_dashboard(handler_for(overrides={"ListFullStacks": response}))
    assert dashboard["stacks"][0]["auto_update"] is None
    assert dashboard["stacks"][0]["service_count"] == 2
    assert len(dashboard["servers"]) == len(dashboard["procedures"]) == 1
    assert dashboard["errors"] == [code]
    assert_safe(dashboard)


@pytest.mark.parametrize("url,key,secret", [
    (None, KEY, SECRET), (ORIGIN, None, SECRET), (ORIGIN, KEY, None), (ORIGIN, "", SECRET),
])
def test_unconfigured_dashboard_never_connects(url, key, secret):
    def unexpected(request):
        pytest.fail("unconfigured dashboard must not contact any upstream")

    with pytest.raises(KomodoDashboardError) as error:
        run_dashboard(unexpected, url=url, key=key, secret=secret)
    assert error.value.code == "not_configured"
    assert error.value.status_code == 503
    assert str(error.value) == "not_configured"


@pytest.mark.parametrize("response,code", [
    (httpx.Response(401, text=SECRET), "invalid_credentials"),
    (httpx.Response(403, text=SECRET), "invalid_credentials"),
    (httpx.Response(200, json={"username": "integration", "enabled": False}), "invalid_credentials"),
    (httpx.Response(200, json={"username": "integration", "enabled": "true"}), "invalid_response"),
    (httpx.Response(200, text=SECRET), "invalid_response"),
    (httpx.Response(502, text=SECRET), "upstream_unavailable"),
    (httpx.Response(302, headers={"location": "https://untrusted.invalid/" + SECRET}), "upstream_unavailable"),
])
def test_bad_service_account_fails_before_any_read_and_error_is_safe(response, code):
    seen = []

    def upstream(request):
        seen.append(request)
        assert request.url.path == "/user"
        return response

    with pytest.raises(KomodoDashboardError) as error:
        run_dashboard(upstream)
    assert len(seen) == 1
    assert error.value.code == code
    assert str(error.value) == code
    assert SECRET not in repr(error.value)


def test_identity_network_failure_uses_fixed_error_not_exception_text(caplog):
    def upstream(request):
        raise httpx.ConnectError(SECRET + ORIGIN, request=request)

    with pytest.raises(KomodoDashboardError) as error:
        run_dashboard(upstream)
    assert error.value.code == "upstream_unavailable"
    assert SECRET not in repr(error.value)
    assert SECRET not in caplog.text


@pytest.mark.parametrize("response,code", [
    (httpx.Response(403, text=SECRET), "servers_permission_denied"),
    (httpx.Response(503, text=SECRET), "servers_upstream_unavailable"),
    (httpx.Response(302, headers={"location": "https://untrusted.invalid/" + SECRET}), "servers_upstream_unavailable"),
    (httpx.Response(200, json={"error": SECRET}), "servers_invalid_response"),
    (httpx.Response(200, text=SECRET), "servers_invalid_response"),
    (httpx.ReadTimeout(SECRET), "servers_upstream_unavailable"),
])
def test_list_failures_preserve_other_tables_without_leaking_error_body(response, code):
    dashboard = run_dashboard(handler_for(overrides={"ListServers": response}))
    assert dashboard["servers"] == []
    assert len(dashboard["stacks"]) == len(dashboard["procedures"]) == 1
    assert dashboard["errors"] == [code]
    assert_safe(dashboard)


def test_key_revoked_during_read_fails_instead_of_returning_stale_success():
    with pytest.raises(KomodoDashboardError) as error:
        run_dashboard(handler_for(overrides={"ListStacks": httpx.Response(401, text=SECRET)}))
    assert error.value.code == "invalid_credentials"


def test_tags_permission_failure_preserves_resources_and_does_not_expose_ids_as_names():
    dashboard = run_dashboard(handler_for(overrides={"ListTags": httpx.Response(403, text=SECRET)}))
    assert [len(dashboard[section]) for section in ("servers", "stacks", "procedures")] == [1, 1, 1]
    assert all(row["tags"] == [] for section in ("servers", "stacks", "procedures") for row in dashboard[section])
    assert dashboard["errors"] == ["tags_permission_denied"]
    assert_safe(dashboard)


@pytest.mark.parametrize("state", ["NotOk", "Disabled"])
def test_disconnected_servers_never_display_stale_measurements_as_current(state):
    data = resources()
    data["ListServers"][0]["info"]["state"] = state
    dashboard = run_dashboard(handler_for(data))
    server = dashboard["servers"][0]
    assert server["state"] == state
    assert all(server[key] is None for key in (
        "cpu_percent", "memory_percent", "disk_percent", "network_bytes_per_second", "load_average",
        "memory_used_bytes", "memory_total_bytes", "disk_used_bytes", "disk_total_bytes",
    ))
    assert dashboard["errors"] == []


def test_missing_metrics_keep_server_and_other_tables_visible_with_nulls():
    data = resources()
    data["ListServers"][0]["info"]["stats"] = None
    dashboard = run_dashboard(handler_for(data))
    assert len(dashboard["servers"]) == 1
    assert dashboard["servers"][0]["cpu_percent"] is None
    assert dashboard["servers"][0]["network_bytes_per_second"] is None
    assert all(dashboard["servers"][0][key] is None for key in (
        "memory_used_bytes", "memory_total_bytes", "disk_used_bytes", "disk_total_bytes",
    ))
    assert dashboard["errors"] == ["server_metrics_unavailable"]


@pytest.mark.parametrize("polling_rate,rate", [
    ("1-sec", 8642), ("5-sec", 1728.4), ("1-min", 8642 / 60), ("2-hr", 8642 / 7200),
    ("1-day", 8642 / 86400), ("unknown", None), (None, None), ("0-sec", None),
])
def test_network_byte_delta_is_normalized_using_actual_polling_seconds(polling_rate, rate):
    data = resources()
    data["ListServers"][0]["info"]["stats"]["polling_rate"] = polling_rate
    dashboard = run_dashboard(handler_for(data))
    actual = dashboard["servers"][0]["network_bytes_per_second"]
    if rate is None:
        assert actual is None
    else:
        assert actual == pytest.approx(rate)


@pytest.mark.parametrize("used,total", [(None, 2), (1, 0), (-1, 2), (1, -2), (True, 2), (math.nan, 2), (math.inf, 2)])
def test_invalid_or_unknown_measurements_stay_null_and_are_json_safe(used, total):
    data = resources()
    data["ListServers"][0]["info"]["stats"]["mem_used_gb"] = used
    data["ListServers"][0]["info"]["stats"]["mem_total_gb"] = total
    dashboard = assemble_dashboard({"servers": data["ListServers"]}, [])
    assert dashboard["servers"][0]["memory_percent"] is None
    assert "server_metrics_invalid_response" in dashboard["errors"]
    assert_safe(dashboard)


@pytest.mark.parametrize("source_field,dto_field", [
    ("mem_used_gb", "memory_used_bytes"), ("mem_total_gb", "memory_total_bytes"),
    ("disk_used_gb", "disk_used_bytes"), ("disk_total_gb", "disk_total_bytes"),
])
@pytest.mark.parametrize("invalid", [None, -1, True, "1.5", math.nan, math.inf, 1e300, 10 ** 400])
def test_capacity_bytes_reject_invalid_values_and_conversion_overflow(source_field, dto_field, invalid):
    data = resources()
    data["ListServers"][0]["info"]["stats"][source_field] = invalid
    dashboard = assemble_dashboard({"servers": data["ListServers"]}, [])
    assert dashboard["servers"][0][dto_field] is None
    assert "server_metrics_invalid_response" in dashboard["errors"]
    assert_safe(dashboard)


@pytest.mark.parametrize("used,total", [(0, 0), (0, 2), (2.5, 2)])
def test_capacity_bytes_preserve_zero_and_report_unclamped_usage(used, total):
    data = resources()
    stats = data["ListServers"][0]["info"]["stats"]
    for prefix in ("mem", "disk"):
        stats[prefix + "_used_gb"], stats[prefix + "_total_gb"] = used, total
    server = assemble_dashboard({"servers": data["ListServers"]}, [])["servers"][0]
    for prefix in ("memory", "disk"):
        assert server[prefix + "_used_bytes"] == used * (1 << 30)
        assert server[prefix + "_total_bytes"] == total * (1 << 30)
        assert server[prefix + "_percent"] == (used / total * 100 if total else None)
    assert_safe(server)


@pytest.mark.parametrize("flags,source", [
    ({"files_on_host": True, "linked_repo": "linked", "repo": "secret-url"}, "服务器上的文件"),
    ({"files_on_host": False, "linked_repo": "linked", "repo": "secret-url"}, "关联仓库"),
    ({"files_on_host": False, "linked_repo": "", "repo": "secret-url"}, "Git 仓库"),
    ({"files_on_host": False, "linked_repo": "", "repo": ""}, "在界面中定义"),
])
def test_stack_source_matches_upstream_priority_without_returning_repository_details(flags, source):
    data = resources()
    data["ListStacks"][0]["info"].update(flags)
    dashboard = run_dashboard(handler_for(data))
    assert dashboard["stacks"][0]["source"] == source
    assert "secret-url" not in json.dumps(dashboard)
    assert_safe(dashboard)


def test_templates_remain_available_for_client_filters_and_schedule_errors_are_boolean_only():
    data = resources()
    template = deepcopy(data["ListServers"][0])
    template.update(id="template", name="template", template=True)
    template["info"].update(stats=None, state="Disabled")
    data["ListServers"].append(template)
    data["ListProcedures"][0]["info"].update(next_scheduled_run=None, schedule_error=SECRET)
    dashboard = run_dashboard(handler_for(data))
    assert [row["template"] for row in dashboard["servers"]] == [False, True]
    assert dashboard["procedures"][0]["next_run_at"] is None
    assert dashboard["procedures"][0]["schedule_error"] is True
    assert_safe(dashboard)


def test_bad_rows_and_non_string_states_do_not_crash_or_echo_raw_payload():
    dashboard = assemble_dashboard({
        "servers": [None, {"id": "s", "name": "node", "info": {"state": {"secret": SECRET}}}],
        "stacks": [{"id": "x", "name": "app", "info": {"state": [SECRET]}}],
        "procedures": [{"id": "p", "name": "flow", "info": {"state": [SECRET]}}],
        "tags": [{"secret": SECRET}],
    }, [])
    assert dashboard["servers"][0]["state"] == "NotOk"
    assert dashboard["stacks"][0]["state"] == "unknown"
    assert dashboard["procedures"][0]["state"] == "Unknown"
    assert "servers_invalid_response" in dashboard["errors"]
    assert "tags_invalid_response" in dashboard["errors"]
    assert_safe(dashboard)


def test_each_dashboard_check_uses_new_cookie_jar_and_does_not_modify_pve_client():
    komodo_handler = handler_for()

    def upstream(request):
        if request.url.host == "pve.example":
            assert request.headers["cookie"] == "PVEAuthCookie=private-pve-ticket"
            assert "x-api-key" not in request.headers
            assert "x-api-secret" not in request.headers
            return httpx.Response(200, json={"data": []})
        return komodo_handler(request)

    async def scenario():
        transport = httpx.MockTransport(upstream)
        async with httpx.AsyncClient(transport=transport, cookies={"PVEAuthCookie": "private-pve-ticket"}) as pve:
            await pve.get("https://pve.example:8006/api2/json/access/permissions")
            komodo = KomodoClient(ORIGIN, KEY, SECRET, transport=transport)
            assert (await komodo.dashboard())["servers"]
            assert (await komodo.dashboard())["servers"]
            await pve.get("https://pve.example:8006/api2/json/access/permissions")

    asyncio.run(scenario())
