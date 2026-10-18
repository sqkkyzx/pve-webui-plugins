import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from plugins.komodo.actions import KomodoActionError
from plugins.komodo.container_details import (get_container_details, get_container_inspect, get_container_logs,
                                    perform_container_detail_action, update_container_description)

SERVER, OTHER, STACK, UPDATE = "a" * 24, "b" * 24, "c" * 24, "d" * 24
CONTAINER, REPLACEMENT = "e" * 64, "f" * 64
KEY, SECRET = "integration-key-credential", "integration-secret-credential"


def fixture():
    return {
        "GetServer": {"_id": {"$oid": SERVER}, "name": "docker-server", "template": False,
            "description": "Server shared description", "config": {"enabled": True, "private_key": "must-not-project"}},
        "ListServers": [{"id": SERVER, "template": False, "info": {"state": "Ok", "container_terminals_disabled": False}}],
        "GetPermission": {"level": "Write", "specific": ["Inspect", "Logs", "Terminal"]},
        "GetCoreInfo": {"ui_write_disabled": False, "secret": SECRET},
        "ListContainers": [{"server_id": SERVER, "id": CONTAINER, "name": "web", "state": "running", "status": "Up 2 hours",
            "image": "nginx:latest", "image_id": "sha256:123", "network_mode": "bridge", "networks": ["web-network"],
            "ports": [{"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"}], "volumes": ["web-data"],
            "created": 1700000000, "stats": {"cpu_perc": "0.00%", "mem_perc": "10.00%", "mem_usage": "1MiB / 10MiB", "net_io": "0B / 1B", "block_io": "0B / 0B", "pids": "2"}}],
        "InspectContainer": {"Id": CONTAINER, "Name": "/web", "Created": "2026-10-17T00:00:00Z", "RestartCount": 0,
            "Config": {"Hostname": "web", "User": "", "WorkingDir": "/app", "Cmd": ["nginx", "-g", "daemon off;"],
                "Entrypoint": ["docker-entrypoint.sh"], "Labels": {"project": "web"}, "Env": ["KEY=" + KEY, "USER_SECRET=application-value"]},
            "State": {"Status": "running", "StartedAt": "2026-10-18T00:00:00Z", "OOMKilled": False, "ExitCode": 0, "Pid": 123,
                "Health": {"Status": "healthy"}}, "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}, "Driver": "overlayfs", "Platform": "linux"},
        "ListUpdates": {"updates": [{"_id": {"$oid": UPDATE}, "operation": "RestartContainer", "target": {"type": "Server", "id": SERVER},
            "status": "Complete", "success": True, "start_ts": 1700000000123, "username": "operator", "logs": [{"stdout": "do-not-project"}]}]},
        "GetResourceMatchingContainer": {"resource": {"type": "Stack", "id": STACK}},
        "GetStack": {"_id": {"$oid": STACK}, "template": False, "name": "web-stack", "config": {"server_id": SERVER, "swarm_id": ""}},
        "ListStackServices": [{"stack_id": STACK, "service": "web", "container": {"server_id": SERVER, "id": CONTAINER}}],
        "GetContainerLog": {"stdout": "\x1b[31mhello\x1b[0m\n" + KEY + "\x07", "stderr": "warn\n" + SECRET, "success": True, "start_ts": 0, "end_ts": 1700000000123},
        "SearchContainerLog": {"stdout": "matched\n", "stderr": "", "success": True, "start_ts": 1, "end_ts": 2},
    }


def harness(raw=None, *, override=None, write_response=None):
    raw, seen, counts = raw or fixture(), [], {}

    async def handler(request):
        seen.append(request)
        assert str(request.url).startswith("http://core:9120/")
        assert request.headers["x-api-key"] == KEY and request.headers["x-api-secret"] == SECRET
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if request.url.path == "/user":
            return httpx.Response(200, json={"username": "integration", "enabled": True}, headers={"set-cookie": "bad=replayed; Path=/"})
        data = json.loads(request.content)
        variant = data["type"]
        counts[variant] = counts.get(variant, 0) + 1
        if override:
            result = override(variant, data["params"], counts[variant])
            if isinstance(result, Exception):
                raise result
            if result is not None:
                return result
        if request.url.path == "/write":
            return write_response or httpx.Response(200, json={})
        if request.url.path == "/execute":
            return write_response or httpx.Response(200, json={"_id": {"$oid": UPDATE}, "operation": variant,
                "target": {"type": "Server", "id": SERVER}, "status": "InProgress", "success": False, "logs": [{"stdout": "do-not-project"}]})
        assert request.url.path == "/read" and variant in raw
        return httpx.Response(200, json=deepcopy(raw[variant]), headers={"set-cookie": "bad=replayed; Path=/"})

    return raw, seen, dict(origin="http://core:9120", key=KEY, secret=SECRET, verify=False, transport=httpx.MockTransport(handler))


def details(options):
    return asyncio.run(get_container_details(SERVER, CONTAINER, **options))


def writes(seen):
    return [json.loads(request.content) for request in seen if request.url.path in {"/write", "/execute"}]


def test_native_information_stats_real_zero_unknown_server_description_and_update_timestamps():
    raw, seen, options = harness()
    data = details(options)
    assert data["container_id"] == CONTAINER and data["server_id"] == SERVER
    assert data["description"] == raw["GetServer"]["description"] and data["description_scope"] == data["updates_scope"] == "server"
    assert data["information"]["stack_id"] == STACK and data["information"]["stack_name"] == "web-stack"
    assert data["information"]["ports"] == ["8080→80/tcp"] and data["information"]["restart_count"] == 0
    assert data["information"]["oom_killed"] is False and data["stats"]["cpu_percent"] == "0.00%"
    assert all(data[name] for name in ("can_execute", "can_logs", "can_inspect", "can_terminal", "can_attach", "can_edit_description"))
    assert data["updates"][0]["start_ts"] == 1700000000123 and data["updates"][0]["success"] is True and data["errors"] == []
    assert all(secret not in json.dumps(data) for secret in (KEY, SECRET, "must-not-project", "do-not-project"))
    raw["ListContainers"][0]["stats"] = None
    assert all(value is None for value in details(options)["stats"].values())


@pytest.mark.parametrize("mutation,code", [
    (lambda r: r["GetServer"].update(template=True), "container_server_unavailable"),
    (lambda r: r["GetServer"].update(_id=OTHER), "container_server_unavailable"),
    (lambda r: r["ListContainers"][0].update(server_id=OTHER), "container_unavailable"),
    (lambda r: r["ListContainers"][0].update(id=REPLACEMENT), "container_unavailable"),
    (lambda r: r["ListContainers"].append(deepcopy(r["ListContainers"][0])), "container_unavailable"),
    (lambda r: r["InspectContainer"].update(Id=REPLACEMENT), "container_identity_changed"),
])
def test_full_id_server_identity_and_duplicate_checks_reject_same_name_replacements(mutation, code):
    raw, seen, options = harness()
    mutation(raw)
    with pytest.raises(KomodoActionError, match=code):
        details(options)
    assert writes(seen) == []


def test_enrichment_permissions_are_independent_and_sensitive_fields_never_projected():
    raw, seen, options = harness()
    raw["GetPermission"] = {"level": "Read", "specific": []}
    data = details(options)
    assert not any(data[key] for key in ("can_execute", "can_logs", "can_inspect", "can_terminal", "can_edit_description"))
    assert data["information"]["command"] is None
    assert all(json.loads(request.content)["type"] != "InspectContainer" for request in seen if request.url.path == "/read")
    for callback in (get_container_inspect, get_container_logs):
        with pytest.raises(KomodoActionError) as exc:
            asyncio.run(callback(SERVER, CONTAINER, **options))
        assert exc.value.status_code == 403


def test_association_requires_exact_current_container_id_and_correct_stack_server():
    raw, seen, options = harness()
    raw["ListStackServices"][0]["container"]["id"] = REPLACEMENT
    assert details(options)["information"]["stack_name"] is None
    raw["GetStack"]["config"]["server_id"] = OTHER
    data = details(options)
    assert data["information"]["stack_name"] is None and "stack_association_unavailable" in data["errors"]


def test_read_errors_are_sanitized_partial_enrichment_and_inspect_redacts_known_creds():
    raw, _, options = harness(override=lambda variant, params, count: httpx.Response(500, text="PRIVATE-UPSTREAM") if variant == "ListUpdates" else None)
    data = details(options)
    assert data["updates"] == [] and data["errors"] == ["updates_unavailable"]
    inspected = asyncio.run(get_container_inspect(SERVER, CONTAINER, **options))["inspect"]
    assert inspected["Config"]["Env"] == ["KEY=[REDACTED_SECRET]", "USER_SECRET=application-value"]
    assert "PRIVATE-UPSTREAM" not in json.dumps(data)


def test_inspect_rechecks_presence_after_raw_read():
    _, _, options = harness(override=lambda variant, params, count: httpx.Response(200, json=[]) if variant == "ListContainers" and count >= 2 else None)
    with pytest.raises(KomodoActionError, match="container_unavailable"):
        asyncio.run(get_container_inspect(SERVER, CONTAINER, **options))


def test_overview_checks_presence_after_all_enrichment_without_an_extra_mid_request_read():
    _, seen, options = harness()
    details(options)
    reads = [json.loads(request.content)["type"] for request in seen if request.url.path == "/read"]
    assert reads.count("ListContainers") == 2
    assert reads[-1] == "ListContainers", "the final check still follows Inspect and stack association"
    _, _, options = harness(override=lambda variant, params, count:
                          httpx.Response(200, json=[]) if variant == "ListContainers" and count == 2 else None)
    with pytest.raises(KomodoActionError, match="container_unavailable"):
        details(options)


def test_logs_exact_full_id_split_search_timestamp_and_no_ansi_credentials():
    _, seen, options = harness()
    data = asyncio.run(get_container_logs(SERVER, CONTAINER, tail=200, timestamps=True, **options))
    assert data["stdout"] == "hello\n[REDACTED_SECRET]" and data["stderr"] == "warn\n[REDACTED_SECRET]"
    requests = [json.loads(request.content) for request in seen if request.url.path == "/read"]
    assert {"type": "GetContainerLog", "params": {"server": SERVER, "container": CONTAINER, "tail": 200, "timestamps": True}} in requests
    data = asyncio.run(get_container_logs(SERVER, CONTAINER, search="error", invert=True, stream="stdout", **options))
    assert data["tail"] == 5000 and data["search_scope"] == "last_5000" and data["stderr"] == ""
    assert json.loads(seen[-2].content)["params"] == {"server": SERVER, "container": CONTAINER, "timestamps": False, "terms": ["error"], "combinator": "And", "invert": True}


def test_search_merges_native_streams_and_never_hides_matches_for_selected_stderr():
    _, _, options = harness()
    data = asyncio.run(get_container_logs(SERVER, CONTAINER, search="error", stream="stderr", **options))
    assert data["stdout"] == "matched\n" and data["stderr"] == ""
    assert data["stream"] == "stdout" and data["search_streams_merged"] is True


@pytest.mark.parametrize("change", [{"tail": True}, {"tail": 0}, {"tail": 5001}, {"timestamps": "true"}, {"invert": 1}, {"search": "x\n"}, {"stream": "invalid"}])
def test_invalid_log_shapes_fail_before_network(change):
    _, seen, options = harness()
    with pytest.raises(KomodoActionError, match="invalid_container_log_query"):
        asyncio.run(get_container_logs(SERVER, CONTAINER, **change, **options))
    assert seen == []


def test_description_uses_server_metadata_only_preserves_config_and_checks_version():
    raw, seen, options = harness()
    data = details(options)
    called = []
    async def guard():
        called.append(True)
    result = asyncio.run(update_container_description(SERVER, CONTAINER, description="New shared description", base_version=data["description_version"], before_write=guard, **options))
    assert result["ok"] and called == [True]
    assert writes(seen) == [{"type": "UpdateResourceMeta", "params": {"target": {"type": "Server", "id": SERVER}, "description": "New shared description"}}]
    raw["GetServer"]["description"] = "Concurrently changed"
    with pytest.raises(KomodoActionError, match="container_description_conflict"):
        asyncio.run(update_container_description(SERVER, CONTAINER, description="Wrong overwrite", base_version=data["description_version"], **options))
    assert len(writes(seen)) == 1


@pytest.mark.parametrize("action,state", [("restart", "running"), ("pause", "running"), ("unpause", "paused"), ("start", "exited"), ("stop", "running"), ("destroy", "running")])
def test_actions_full_id_default_flags_submitted_receipts(action, state):
    raw, seen, options = harness()
    raw["ListContainers"][0]["state"] = state
    data = details(options)
    result = asyncio.run(perform_container_detail_action(SERVER, CONTAINER, action=action, base_version=data["version"], **options))
    assert result["results"][0]["id"] == SERVER + ":" + CONTAINER and result["results"][0]["status"] == "submitted"
    assert writes(seen)[0]["params"] == {"server": SERVER, "container": CONTAINER}
    assert "do-not-project" not in json.dumps(result)


def test_state_or_description_permission_changes_fail_before_any_submission():
    raw, seen, options = harness()
    data = details(options)
    raw["ListContainers"][0]["state"] = "paused"
    with pytest.raises(KomodoActionError, match="container_version_conflict"):
        asyncio.run(perform_container_detail_action(SERVER, CONTAINER, action="restart", base_version=data["version"], **options))
    raw["GetCoreInfo"]["ui_write_disabled"] = True
    with pytest.raises(KomodoActionError, match="container_description_read_only"):
        asyncio.run(update_container_description(SERVER, CONTAINER, description="No", base_version=data["description_version"], **options))
    assert not writes(seen)


def test_redacted_description_readonly_and_ambiguous_write_outcome_never_leaks_upstream():
    raw, seen, options = harness(write_response=httpx.Response(500, text=SECRET))
    data = details(options)
    result = asyncio.run(update_container_description(SERVER, CONTAINER, description="new", base_version=data["description_version"], **options))
    assert result["status"] == "outcome_unknown" and SECRET not in json.dumps(result)
    raw["GetServer"]["description"] = "Contains " + KEY
    data = details(options)
    assert data["description"] == "Contains [REDACTED_SECRET]" and not data["can_edit_description"]
