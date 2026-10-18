import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from plugins.komodo.actions import KomodoActionError
from plugins.komodo.stack_details import (
    BOOL_FIELDS, CONFIG_FIELDS, LIST_FIELDS, STRING_FIELDS, get_stack_details, get_stack_logs,
    update_stack_details, validate_config_patch,
)


ORIGIN = "http://core:9120"
KEY, SECRET = "integration-api-key", "integration-api-secret"
PRIVATE = "private-key-or-registry-token"
STACK, SERVER, OTHER, UPDATE = "a" * 24, "b" * 24, "c" * 24, "d" * 24


def fixture():
    config = {field: False for field in BOOL_FIELDS}
    config.update({field: "" for field in STRING_FIELDS})
    config.update({field: [] for field in LIST_FIELDS})
    config.update(server_id=SERVER, repo="namespace/repository", branch="main", git_https=True,
        environment="APP_SECRET=authorized-resource-secret\n", webhook_secret="custom-webhook-secret",
        pre_deploy={"path": "./sub", "command": "echo before\n", "shell_mode": True},
        post_deploy={"path": "", "command": "", "shell_mode": False},
        additional_env_files=[{"path": ".env.extra", "track": False}],
        config_files=[{"path": "config.json", "services": ["web"], "requires": "Restart"}],
        private_key=PRIVATE, registry_token=PRIVATE, api_key=KEY)
    return {
        "GetStack": {"_id": {"$oid": STACK}, "name": "application-stack", "template": False,
            "description": "Original description\nsecond line", "config": config, "updated_at": 123,
            "info": {"remote_contents": [{"path": "compose.yaml", "contents": "services:\n  web:\n    image: nginx\n", "private_key": PRIVATE}],
                     "remote_errors": None, "deployed_config": "services:\n  web:\n    environment:\n      SECRET: '[REDACTED]'\n",
                     "deployed_contents": [{"path": "compose.yaml", "contents": "services: {}\n"}],
                     "private_key": PRIVATE, "environment": PRIVATE}},
        "GetPermission": {"level": "Write", "specific": ["Logs", "Terminal"]},
        "GetCoreInfo": {"ui_write_disabled": False, "api_key": KEY, "secret": SECRET},
        "ListStacks": [{"id": STACK, "name": "application-stack", "template": False, "info": {"state": "running"}}],
        "ListStackServices": [{"stack_id": STACK, "service": "web", "container": {"env": PRIVATE}},
                              {"stack_id": STACK, "service": "worker"}],
        "ListServers": [{"id": SERVER, "name": "docker-host", "template": False, "config": {"passkey": PRIVATE}}],
        "ListSwarms": [{"id": OTHER, "name": "swarm", "template": False}],
        "ListRepos": [{"id": OTHER, "name": "source-repo", "template": False, "token": PRIVATE}],
        "GetStackLog": {"stage": PRIVATE, "command": PRIVATE, "stdout": "\x1b[31mred\x1b[0m\n" + KEY + "\n" + SECRET,
                        "stderr": "warn\n", "success": True, "start_ts": 0, "end_ts": 1700000000123},
        "SearchStackLog": {"stdout": "filtered\n", "stderr": "", "success": True, "start_ts": 1, "end_ts": 2},
    }


def harness(raw=None, *, overrides=None, submit=None):
    raw, overrides, seen = raw or fixture(), overrides or {}, []
    async def handler(request):
        seen.append(request)
        assert str(request.url).startswith(ORIGIN + "/")
        assert request.headers["x-api-key"] == KEY and request.headers["x-api-secret"] == SECRET
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if request.url.path == "/user":
            assert request.method == "GET"
            return overrides.get("user") or httpx.Response(200, json={"username": "integration", "enabled": True},
                headers={"set-cookie": "PVEAuthCookie=must-not-cross-requests; Path=/"})
        assert request.method == "POST"
        payload = json.loads(request.content)
        variant = payload["type"]
        if request.url.path == "/write":
            assert variant in {"UpdateStack", "WriteStackFileContents"}
            if submit:
                return submit(payload)
            if variant == "UpdateStack":
                return httpx.Response(200, json={"_id": {"$oid": STACK}, "config": {"secret": PRIVATE}})
            return httpx.Response(200, json={"_id": {"$oid": UPDATE}, "operation": "WriteStackContents",
                "target": {"type": "Stack", "id": STACK}, "success": True, "status": "Complete",
                "logs": [{"stdout": PRIVATE}], "other_data": PRIVATE})
        assert request.url.path == "/read" and variant in raw
        expected = {
            "GetStack": {"stack": STACK}, "GetPermission": {"target": {"type": "Stack", "id": STACK}},
            "GetCoreInfo": {}, "ListStacks": {"query": {"names": [STACK], "templates": "Include"}, "limit": 0},
            "ListStackServices": {"stack": STACK},
        }
        if variant in {"ListServers", "ListSwarms", "ListRepos"}:
            assert payload["params"] == {"query": {"templates": "Exclude"}, "limit": 0}
        elif variant not in {"GetStackLog", "SearchStackLog"}:
            assert payload["params"] == expected[variant]
        if variant in overrides:
            response = overrides[variant]
            if isinstance(response, Exception):
                raise response
            return response
        return httpx.Response(200, json=deepcopy(raw[variant]), headers={"set-cookie": "upstream=must-not-replay; Path=/"})
    options = {"origin": ORIGIN, "key": KEY, "secret": SECRET, "verify": False, "transport": httpx.MockTransport(handler)}
    return raw, seen, options


def read(options):
    return asyncio.run(get_stack_details(STACK, **options))


def writes(seen):
    return [json.loads(request.content) for request in seen if request.url.path == "/write"]


def assert_safe(value):
    encoded = json.dumps(value)
    for forbidden in (KEY, SECRET, PRIVATE, "PVEAuthCookie", "registry_token", "private_key"):
        assert forbidden not in encoded


def save(options, version, *, action="config", **values):
    return asyncio.run(update_stack_details(STACK, action=action, base_version=version,
        expected_server_id=SERVER, **values, **options))


def test_details_cover_every_real_stack_config_field_with_typed_files_permissions_and_selectors():
    raw, seen, options = harness()
    result = read(options)
    assert len(CONFIG_FIELDS) == 42 and set(result["config"]) == set(CONFIG_FIELDS)
    assert result["stack_id"] == STACK and result["server_id"] == SERVER and result["source_mode"] == "git"
    assert result["can_write"] and result["can_execute"] and result["can_logs"]
    assert result["config"]["environment"] == "APP_SECRET=authorized-resource-secret\n"
    assert result["config"]["webhook_secret"] == "custom-webhook-secret"
    assert result["files"][0]["contents"] == raw["GetStack"]["info"]["remote_contents"][0]["contents"]
    assert result["files"][0]["editable"] and result["files"][0]["redacted"] is False
    assert result["deployed_config"] == raw["GetStack"]["info"]["deployed_config"]
    assert result["deployed_files"] == [{"path": "compose.yaml", "contents": "services: {}\n"}]
    assert result["selectors"] == {"servers": [{"id": SERVER, "name": "docker-host"}],
        "swarms": [{"id": OTHER, "name": "swarm"}], "repos": [{"id": OTHER, "name": "source-repo"}]}
    assert result["services"] == ["web", "worker"] and result["errors"] == []
    assert result["redacted_fields"] == [] and "git_file_save_commits_and_pushes" in result["save_effects"]
    assert all(request.url.path in {"/user", "/read"} for request in seen)
    assert_safe(result)


@pytest.mark.parametrize("mode,expected", [("host", "host"), ("git", "git"), ("ui", "ui"), ("none", "none")])
def test_source_modes_follow_the_same_priority_as_upstream(mode, expected):
    raw, seen, options = harness()
    config = raw["GetStack"]["config"]
    config.update(files_on_host=mode == "host", repo="source/repo" if mode in {"host", "git"} else "",
        file_contents="services: {}\n" if mode != "none" else "")
    result = read(options)
    assert result["source_mode"] == expected
    assert result["files"][0]["editable"] is (mode in {"host", "git"})


@pytest.mark.parametrize("deployed,expected", [
    ([{"service_name": "deployed-service"}], ["deployed-service"]),
    (None, ["latest-service"]),
    ([], []),
])
def test_config_service_choices_use_native_deployed_or_latest_names_without_changing_runtime_services(deployed, expected):
    raw, seen, options = harness()
    raw["GetStack"]["info"].update(deployed_services=deployed,
        latest_services=[{"service_name": "latest-service"}])
    result = read(options)
    assert result["service_choices"] == expected
    assert result["services"] == ["web", "worker"]
    assert len([request for request in seen if request.url.path == "/read"]) == 8


def test_config_service_choices_project_only_valid_unique_names_and_redact_integration_credentials():
    raw, seen, options = harness()
    raw["GetStack"]["info"]["latest_services"] = [
        {"service_name": "worker", "container_name": PRIVATE},
        {"service_name": "web"}, {"service_name": "worker"},
        {"service_name": KEY}, {"service_name": SECRET},
        {}, None, "bare-name", {"service_name": None}, {"service_name": 12},
        {"service_name": ""}, {"service_name": "invalid service"},
    ]
    result = read(options)
    assert result["service_choices"] == ["worker", "web", "[REDACTED_SECRET]", "[REDACTED_SECRET]"]
    assert result["services"] == ["web", "worker"]
    assert_safe(result)


@pytest.mark.parametrize("deployed,latest", [(None, None), (None, {}), ({}, [{"service_name": "latest-service"}])])
def test_malformed_config_service_metadata_does_not_replace_or_break_other_stack_details(deployed, latest):
    raw, seen, options = harness()
    raw["GetStack"]["info"].update(deployed_services=deployed, latest_services=latest)
    result = read(options)
    assert result["service_choices"] == []
    assert result["services"] == ["web", "worker"]
    assert result["config"]["repo"] == "namespace/repository"
    assert result["errors"] == []


def test_missing_values_stay_null_and_legacy_structs_are_not_silently_cleared():
    raw, seen, options = harness()
    config = raw["GetStack"]["config"]
    config.pop("auto_update")
    config["additional_env_files"] = [".env.extra"]
    config["config_files"] = ["config.json", {"path": "second.json", "requires": "restart"}]
    result = read(options)
    assert result["config"]["auto_update"] is None
    assert result["config"]["additional_env_files"] == [{"path": ".env.extra", "track": True}]
    assert result["config"]["config_files"] == [{"path": "config.json", "services": [], "requires": "None"},
        {"path": "second.json", "services": [], "requires": "Restart"}]


@pytest.mark.parametrize("state", ["deploying", "running", "paused", "stopped", "created", "restarting", "dead", "removing", "unhealthy", "down", "unknown"])
def test_all_real_stack_states_preserve_their_meaning(state):
    raw, seen, options = harness()
    raw["ListStacks"][0]["info"]["state"] = state
    assert read(options)["state"] == state


@pytest.mark.parametrize("level,disabled,specific,write,execute,logs", [
    ("Read", False, [], False, False, False), ("Read", False, ["Logs"], False, False, True),
    ("Execute", False, ["Logs"], False, True, True), ("Write", True, ["Logs"], False, True, True),
])
def test_real_permission_and_global_config_policy_are_rechecked(level, disabled, specific, write, execute, logs):
    raw, seen, options = harness()
    raw["GetPermission"] = {"level": level, "specific": specific}
    raw["GetCoreInfo"]["ui_write_disabled"] = disabled
    result = read(options)
    assert (result["can_write"], result["can_execute"], result["can_logs"]) == (write, execute, logs)
    assert result["files"][0]["editable"] is write


def test_partial_selectors_services_and_file_errors_keep_configuration_and_omit_raw_errors():
    raw, seen, options = harness(overrides={"ListServers": httpx.Response(403, text=PRIVATE),
        "ListStackServices": httpx.ReadTimeout(PRIVATE)})
    raw["GetStack"]["info"]["remote_errors"] = [{"path": "missing.yaml", "contents": PRIVATE}]
    result = read(options)
    assert result["config"]["repo"] == "namespace/repository" and result["services"] == []
    assert result["selectors"]["servers"] == []
    assert result["remote_errors"] == [{"path": "missing.yaml", "error_code": "source_file_unavailable"}]
    assert result["files"][1]["path"] == "missing.yaml" and result["files"][1]["contents"] is None
    assert result["files"][1]["editable"]
    assert set(result["errors"]) == {"source_file_unavailable", "servers_unavailable", "services_unavailable"}
    assert_safe(result)


def test_integration_credentials_are_redacted_and_masked_config_or_source_files_cannot_be_written():
    raw, seen, options = harness()
    raw["GetStack"]["config"]["environment"] = "KEY=" + KEY + "\nSECRET=" + SECRET
    raw["GetStack"]["info"]["remote_contents"][0]["contents"] = "secret: " + SECRET
    raw["GetStack"]["info"]["deployed_config"] = "secret: " + KEY
    result = read(options)
    assert result["redacted_fields"] == ["environment"]
    assert "[REDACTED_SECRET]" in result["config"]["environment"]
    assert result["files"][0]["redacted"] and not result["files"][0]["editable"]
    assert_safe(result)
    for values, code in [({"config": {"environment": "replacement"}}, "redacted_field_read_only"),
        ({"action": "file", "file_path": "compose.yaml", "contents": "new"}, "source_file_read_only")]:
        with pytest.raises(KomodoActionError) as error:
            save(options, result["version"], **values)
        assert error.value.code == code
    assert writes(seen) == []


def test_config_save_is_one_fixed_partial_write_after_fresh_read_and_guard():
    raw, seen, options = harness()
    result = read(options)
    observed = []
    async def guard():
        assert writes(seen) == []
        observed.append("guard")
    output = save(options, result["version"], config={"auto_update": True, "environment": "VALUE=edited\n"}, before_write=guard)
    assert observed == ["guard"] and output == {"ok": True, "status": "completed", "operation_id": None, "error_code": None}
    assert writes(seen) == [{"type": "UpdateStack", "params": {"id": STACK, "config": {"auto_update": True, "environment": "VALUE=edited\n"}}}]
    assert_safe(output)


def test_file_save_uses_only_a_current_visible_file_and_returns_only_safe_operation_metadata():
    raw, seen, options = harness()
    result = read(options)
    output = save(options, result["version"], action="file", file_path="compose.yaml", contents="services: {}\n")
    assert output == {"ok": True, "status": "completed", "operation_id": UPDATE, "error_code": None}
    assert writes(seen) == [{"type": "WriteStackFileContents", "params": {"stack": STACK,
        "file_path": "compose.yaml", "contents": "services: {}\n"}}]
    assert_safe(output)


@pytest.mark.parametrize("change,code", [("config", "stack_version_conflict"), ("file", "stack_version_conflict"),
    ("target", "stack_version_conflict"), ("permission", "stack_read_only"), ("disabled", "stack_read_only")])
def test_preflight_conflicts_and_policy_changes_never_write(change, code):
    raw, seen, options = harness()
    version = read(options)["version"]
    if change == "config": raw["GetStack"]["config"]["branch"] = "external-update"
    if change == "file": raw["GetStack"]["info"]["remote_contents"][0]["contents"] = "external edit"
    if change == "target": raw["GetStack"]["config"]["server_id"] = OTHER
    if change == "permission": raw["GetPermission"]["level"] = "Read"
    if change == "disabled": raw["GetCoreInfo"]["ui_write_disabled"] = True
    with pytest.raises(KomodoActionError) as error:
        save(options, version, config={"branch": "new"})
    assert error.value.code == code and writes(seen) == []


def test_before_write_guard_rejects_target_changes_without_any_submission():
    raw, seen, options = harness()
    version = read(options)["version"]
    async def guard():
        raise KomodoActionError("binding_changed", 409)
    with pytest.raises(KomodoActionError) as error:
        save(options, version, config={"auto_pull": True}, before_write=guard)
    assert error.value.code == "binding_changed" and writes(seen) == []


@pytest.mark.parametrize("path", ["../../etc/shadow", "/root/.ssh/id_rsa", "another-compose.yaml"])
def test_client_cannot_turn_file_save_into_an_arbitrary_file_write(path):
    raw, seen, options = harness()
    version = read(options)["version"]
    with pytest.raises(KomodoActionError) as error:
        save(options, version, action="file", file_path=path, contents="replacement")
    assert error.value.code == "source_file_read_only" and writes(seen) == []


@pytest.mark.parametrize("action", ["config", "file"])
@pytest.mark.parametrize("response", [httpx.Response(408, text=PRIVATE), httpx.Response(500, text=PRIVATE),
    httpx.Response(302, headers={"location": "https://private.invalid/" + SECRET}), httpx.Response(200, text="broken"),
    httpx.Response(200, json={"_id": {"$oid": OTHER}})])
def test_uncertain_write_response_is_not_retried_or_misreported(action, response):
    raw, seen, options = harness(submit=lambda payload: response)
    version = read(options)["version"]
    values = {"config": {"auto_pull": True}} if action == "config" else {"file_path": "compose.yaml", "contents": "new"}
    output = save(options, version, action=action, **values)
    assert output["status"] == "outcome_unknown" and output["ok"] is False
    assert len(writes(seen)) == 1
    assert_safe(output)


def test_submit_network_timeout_is_unknown_and_never_retried():
    def submit(payload):
        raise httpx.ReadTimeout(PRIVATE + SECRET)
    raw, seen, options = harness(submit=submit)
    version = read(options)["version"]
    output = save(options, version, config={"auto_pull": True})
    assert output["status"] == "outcome_unknown" and len(writes(seen)) == 1
    assert_safe(output)


@pytest.mark.parametrize("patch", [{}, {"private_key": "new"}, {"auto_pull": "false"}, {"environment": None},
    {"links": "https://example.invalid"}, {"config_files": [{"path": "x", "requires": "Delete"}]},
    {"additional_env_files": [{"path": "x", "track": 1}]}, {"pre_deploy": {"command": "ls", "unknown": True}}])
def test_invalid_partial_fields_are_rejected_before_any_read_or_write(patch):
    raw, seen, options = harness()
    with pytest.raises(KomodoActionError):
        save(options, "0" * 64, config=patch)
    assert seen == []


def test_log_read_uses_fixed_current_stack_and_service_membership_without_raw_metadata():
    raw, seen, options = harness()
    result = asyncio.run(get_stack_logs(STACK, services=["web", "web"], tail=5000, timestamps=True, **options))
    payload = json.loads(seen[-1].content)
    assert payload == {"type": "GetStackLog", "params": {"stack": STACK, "services": ["web"], "tail": 5000, "timestamps": True}}
    assert result["stdout"] == "red\n[REDACTED_SECRET]\n[REDACTED_SECRET]"
    assert result["stderr"] == "warn\n" and result["search_scope"] == "tail"
    assert result["stack_id"] == STACK and result["server_id"] == SERVER and result["start_ts"] == 0
    assert result["truncated"] is False and writes(seen) == []
    assert_safe(result)


def test_log_search_uses_native_search_contract_and_never_invents_a_tail_parameter():
    raw, seen, options = harness()
    result = asyncio.run(get_stack_logs(STACK, services=[], tail=100, timestamps=False, search="error words", invert=True, **options))
    assert json.loads(seen[-1].content) == {"type": "SearchStackLog", "params": {
        "stack": STACK, "services": [], "timestamps": False, "terms": ["error words"], "combinator": "And", "invert": True}}
    assert result["search_scope"] == "last_5000" and result["tail"] == 5000 and result["stdout"] == "filtered\n"


@pytest.mark.parametrize("values", [{"tail": 0}, {"tail": True}, {"tail": 5001}, {"timestamps": 1},
    {"invert": "true"}, {"search": "bad\nline"}, {"services": ["--help"]}, {"services": ["web"] * 101}])
def test_invalid_log_filters_cannot_reach_upstream(values):
    raw, seen, options = harness()
    with pytest.raises(KomodoActionError):
        asyncio.run(get_stack_logs(STACK, **values, **options))
    assert seen == []


@pytest.mark.parametrize("case", ["no_logs_permission", "unknown_service", "disabled_account"])
def test_log_permission_and_identity_preflight_prevent_the_log_rpc(case):
    raw, seen, options = harness(overrides={"user": httpx.Response(200, json={"username": "x", "enabled": False})}
        if case == "disabled_account" else None)
    if case == "no_logs_permission": raw["GetPermission"]["specific"] = []
    with pytest.raises(KomodoActionError):
        asyncio.run(get_stack_logs(STACK, services=["missing"] if case == "unknown_service" else [], **options))
    assert all(request.url.path != "/read" or json.loads(request.content)["type"] not in {"GetStackLog", "SearchStackLog"} for request in seen)
    assert writes(seen) == []


def test_swarm_logs_require_one_known_service_and_default_to_the_first_real_service():
    raw, seen, options = harness()
    raw["GetStack"]["config"]["swarm_id"] = OTHER
    result = asyncio.run(get_stack_logs(STACK, **options))
    assert result["services"] == ["web"]
    with pytest.raises(KomodoActionError) as error:
        asyncio.run(get_stack_logs(STACK, services=["web", "worker"], **options))
    assert error.value.code == "stack_single_service_required"


def test_log_size_and_control_sequences_are_bounded_plain_text():
    raw, seen, options = harness()
    raw["GetStackLog"]["stdout"] = "\x1b]8;;https://private.invalid\x1b\\link\x1b]8;;\x1b\\\x00\x07" + "x" * 1_048_580
    raw["GetStackLog"]["start_ts"] = True
    result = asyncio.run(get_stack_logs(STACK, **options))
    assert result["truncated"] and len(result["stdout"]) <= 1_048_576
    assert result["start_ts"] is None and all(control not in result["stdout"] for control in ["\x1b", "\x00", "\x07", "https://private.invalid"])


@pytest.mark.parametrize("variant", ["GetStack", "GetPermission", "GetCoreInfo", "GetStackLog"])
def test_upstream_failure_and_redirect_never_reveal_sensitive_body_or_follow_location(variant):
    raw, seen, options = harness(overrides={variant: httpx.Response(302, headers={"location": "https://private.invalid/" + SECRET}, text=PRIVATE)})
    with pytest.raises(KomodoActionError) as error:
        asyncio.run(get_stack_logs(STACK, **options)) if variant == "GetStackLog" else read(options)
    assert str(error.value) == "upstream_unavailable" and all(request.url.host == "core" for request in seen)


def test_unconfigured_and_invalid_target_never_contact_upstream():
    raw, seen, options = harness()
    for values in [{**options, "secret": None}, options]:
        with pytest.raises(KomodoActionError):
            asyncio.run(get_stack_details(STACK if values["secret"] is None else "../bad", **values))
    assert seen == []


@pytest.mark.parametrize("level", [[], {}, True, None, "unexpected"])
def test_malformed_permission_level_returns_a_safe_error_not_an_unhashable_exception(level):
    raw, seen, options = harness()
    raw["GetPermission"]["level"] = level
    with pytest.raises(KomodoActionError) as error:
        read(options)
    assert error.value.code == "invalid_response" and writes(seen) == []


@pytest.mark.parametrize("values", [{"origin": " "}, {"origin": "http://user:private@core:9120"},
    {"key": " "}, {"secret": "invalid\nheader"}])
def test_invalid_server_configuration_is_rejected_safely_before_transport(values):
    raw, seen, options = harness()
    with pytest.raises(KomodoActionError) as error:
        read({**options, **values})
    assert error.value.code == "invalid_upstream_configuration" and seen == []
