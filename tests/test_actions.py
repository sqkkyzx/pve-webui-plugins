import asyncio
import json

import httpx
import pytest

from plugins.komodo.komodo import KomodoActionError, KomodoClient


ORIGIN = "http://komodo-core-1:9120"
KEY = "action-only-api-key"
SECRET = "action-only-api-secret"
FIRST, SECOND, THIRD, OPERATION = "a" * 24, "b" * 24, "d" * 24, "c" * 24
TARGETS = {"servers": "Server", "stacks": "Stack", "procedures": "Procedure"}
LISTS = {"servers": "ListServers", "stacks": "ListStacks", "procedures": "ListProcedures"}


def client_for(handler, *, url=ORIGIN, key=KEY, secret=SECRET):
    return KomodoClient(url, key, secret, transport=httpx.MockTransport(handler))


def run_action(handler, section="servers", action="stop_all_containers", ids=None):
    return asyncio.run(client_for(handler).perform_action(section, action, ids or [FIRST]))


def update_response(section, variant, identifier, *, status="InProgress", success=False):
    return httpx.Response(200, json={
        "_id": {"$oid": OPERATION}, "operation": variant,
        "target": {"type": TARGETS[section], "id": identifier},
        "status": status, "success": success,
        "logs": [{"stdout": SECRET, "stderr": KEY, "command": "private-command"}],
        "other_data": SECRET, "current_toml": SECRET,
    }, headers={"set-cookie": "untrusted-session=must-not-replay; Path=/"})


def handler_for(section="servers", *, ids=None, rows=None, submit=None, seen=None):
    visible = ids or [FIRST, SECOND, THIRD]

    def upstream(request):
        if seen is not None:
            seen.append(request)
        assert request.headers["x-api-key"] == KEY
        assert request.headers["x-api-secret"] == SECRET
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        assert str(request.url).startswith(ORIGIN)
        if request.url.path == "/user":
            assert request.method == "GET"
            return httpx.Response(200, json={"username": "integration", "enabled": True}, headers={
                "set-cookie": "PVEAuthCookie=must-not-replay; Path=/",
            })
        payload = json.loads(request.content)
        assert request.method == "POST"
        if request.url.path == "/read":
            assert payload == {"type": LISTS[section], "params": {"query": {"templates": "Include"}, "limit": 0}}
            return httpx.Response(200, json=rows if rows is not None else [
                {"id": identifier, "name": "visible", "type": TARGETS[section], "template": False,
                 "info": {"state": "running" if section == "stacks" else "Ok"}}
                for identifier in visible
            ], headers={"set-cookie": "private-session=must-not-replay; Path=/"})
        assert request.url.path in {"/execute", "/write"}
        assert not payload["type"].startswith("Batch")
        if submit is not None:
            return submit(request, payload)
        identifier = payload["params"].get("id") or payload["params"].get({
            "servers": "server", "stacks": "stack", "procedures": "procedure",
        }[section])
        if payload["type"].startswith("Delete"):
            return httpx.Response(200, json={
                "_id": {"$oid": identifier}, "name": SECRET,
                "config": {"environment": SECRET, "private_address": KEY},
            })
        if payload["type"] == "CheckStackForUpdate":
            return httpx.Response(200, json={
                "stack": identifier, "services": [{"image": "https://user:" + SECRET + "@private.invalid"}],
            })
        return update_response(section, payload["type"], identifier)

    return upstream


def assert_safe(value):
    text = json.dumps(value)
    for forbidden in (KEY, SECRET, "private-command", "private_address", "PVEAuthCookie"):
        assert forbidden not in text


@pytest.mark.parametrize("section,action,path,variant,params", [
    ("servers", "prune_containers", "/execute", "PruneContainers", {"server": FIRST}),
    ("servers", "prune_networks", "/execute", "PruneNetworks", {"server": FIRST}),
    ("servers", "prune_volumes", "/execute", "PruneVolumes", {"server": FIRST}),
    ("servers", "prune_images", "/execute", "PruneImages", {"server": FIRST}),
    ("servers", "prune_system", "/execute", "PruneSystem", {"server": FIRST}),
    ("servers", "restart_all_containers", "/execute", "RestartAllContainers", {"server": FIRST}),
    ("servers", "stop_all_containers", "/execute", "StopAllContainers", {"server": FIRST}),
    ("servers", "delete", "/write", "DeleteServer", {"id": FIRST}),
    ("stacks", "check_update", "/write", "CheckStackForUpdate", {
        "stack": FIRST, "skip_auto_update": True, "wait_for_auto_update": False, "skip_cache_refresh": False,
    }),
    ("stacks", "pull", "/execute", "PullStack", {"stack": FIRST, "services": []}),
    ("stacks", "deploy", "/execute", "DeployStack", {"stack": FIRST, "services": [], "stop_time": None}),
    ("stacks", "restart", "/execute", "RestartStack", {"stack": FIRST, "services": []}),
    ("stacks", "pause", "/execute", "PauseStack", {"stack": FIRST, "services": []}),
    ("stacks", "stop", "/execute", "StopStack", {"stack": FIRST, "services": [], "stop_time": None}),
    ("stacks", "destroy", "/execute", "DestroyStack", {
        "stack": FIRST, "services": [], "stop_time": None, "remove_orphans": False,
    }),
    ("stacks", "delete", "/write", "DeleteStack", {"id": FIRST}),
    ("procedures", "run", "/execute", "RunProcedure", {"procedure": FIRST}),
    ("procedures", "delete", "/write", "DeleteProcedure", {"id": FIRST}),
])
def test_every_action_uses_fixed_single_resource_rpc_and_safe_defaults(section, action, path, variant, params):
    seen = []
    result = run_action(handler_for(section, seen=seen), section, action)
    assert [request.url.path for request in seen] == ["/user", "/read", path]
    assert json.loads(seen[-1].content) == {"type": variant, "params": params}
    assert result == {
        "section": section, "action": action, "results": [{
            "id": FIRST, "ok": True, "status": "completed" if path == "/write" else "submitted",
            "operation_id": None if path == "/write" else OPERATION, "error_code": None,
        }],
    }
    assert_safe(result)


@pytest.mark.parametrize("section,action,ids", [
    ("other", "delete", [FIRST]), ("servers", "DeleteStack", [FIRST]),
    ("servers", "run", [FIRST]), ("stacks", "check_updates", [FIRST]),
    ([], "delete", [FIRST]), ("servers", [], [FIRST]),
    ("servers", "delete", []), ("servers", "delete", [FIRST] * 51),
    ("servers", "delete", "*"), ("servers", "delete", ["*"]),
    ("servers", "delete", ["server-name"]), ("servers", "delete", [FIRST, "bad"]),
    ("servers", "delete", [{"$oid": FIRST}]), ("servers", "delete", [True]),
])
def test_invalid_inputs_fail_before_any_network_or_write(section, action, ids):
    def unexpected(request):
        pytest.fail("invalid action must not perform any network request")

    with pytest.raises(KomodoActionError) as error:
        asyncio.run(client_for(unexpected).perform_action(section, action, ids))
    assert error.value.code == "invalid_request"
    assert error.value.status_code == 400


def test_duplicate_and_uppercase_ids_are_canonicalized_before_exactly_one_submission():
    seen = []
    result = run_action(handler_for(seen=seen), ids=[FIRST.upper(), FIRST, SECOND])
    assert [entry["id"] for entry in result["results"]] == [FIRST, SECOND]
    assert [json.loads(request.content)["params"]["server"] for request in seen if request.url.path == "/execute"] == [FIRST, SECOND]


def test_missing_credentials_never_contact_the_network():
    def unexpected(request):
        pytest.fail("missing credentials must not produce a request")

    with pytest.raises(KomodoActionError) as error:
        asyncio.run(client_for(unexpected, secret=None).perform_action("servers", "delete", [FIRST]))
    assert error.value.code == "not_configured"
    assert error.value.status_code == 503


@pytest.mark.parametrize("response,code", [
    (httpx.Response(401, text=SECRET), "invalid_credentials"),
    (httpx.Response(403, text=SECRET), "invalid_credentials"),
    (httpx.Response(200, json={"username": "service", "enabled": False}), "invalid_credentials"),
    (httpx.Response(200, json={"username": "service", "enabled": 1}), "invalid_response"),
    (httpx.Response(200, text=SECRET), "invalid_response"),
    (httpx.Response(302, headers={"location": "https://private.invalid/" + SECRET}), "upstream_unavailable"),
])
def test_disabled_or_invalid_service_account_never_reads_or_submits(response, code):
    seen = []

    def upstream(request):
        seen.append(request)
        assert request.url.path == "/user"
        return response

    with pytest.raises(KomodoActionError) as error:
        run_action(upstream)
    assert len(seen) == 1
    assert error.value.code == code
    assert SECRET not in repr(error.value)


@pytest.mark.parametrize("rows,code", [
    ([{"id": FIRST, "template": False}], "resource_not_found"),
    ([{"id": FIRST, "template": False}, {"id": SECOND, "template": True}], "template_forbidden"),
    ([{"id": FIRST, "template": False}, {"id": SECOND}], "invalid_response"),
    ([{"id": FIRST, "template": False}, {"id": SECOND, "template": False, "type": "Stack"}], "invalid_response"),
    ([{"id": FIRST, "template": False}, {"id": SECOND, "template": False}, {"id": SECOND, "template": False}], "invalid_response"),
])
def test_all_selected_resources_are_preflighted_before_the_first_write(rows, code):
    seen = []
    with pytest.raises(KomodoActionError) as error:
        run_action(handler_for(rows=rows, seen=seen), ids=[FIRST, SECOND])
    assert error.value.code == code
    assert [request.url.path for request in seen] == ["/user", "/read"]


@pytest.mark.parametrize("response,code", [
    (httpx.Response(401, text=SECRET), "invalid_credentials"),
    (httpx.Response(403, text=SECRET), "permission_denied"),
    (httpx.Response(503, text=SECRET), "upstream_unavailable"),
    (httpx.Response(200, json={"error": SECRET}), "invalid_response"),
])
def test_preflight_permission_and_protocol_failures_do_not_submit(response, code):
    normal = handler_for()
    seen = []

    def upstream(request):
        seen.append(request)
        if request.url.path == "/read":
            return response
        return normal(request)

    with pytest.raises(KomodoActionError) as error:
        run_action(upstream)
    assert error.value.code == code
    assert [request.url.path for request in seen] == ["/user", "/read"]
    assert SECRET not in repr(error.value)


def test_partial_failure_retains_accepted_update_and_stops_remaining_ids():
    seen = []

    def submit(request, payload):
        identifier = payload["params"]["server"]
        if identifier == SECOND:
            return httpx.Response(403, text=SECRET + KEY)
        return update_response("servers", payload["type"], identifier)

    result = run_action(handler_for(submit=submit, seen=seen), ids=[FIRST, SECOND, THIRD])
    assert [entry["status"] for entry in result["results"]] == ["submitted", "failed", "not_submitted"]
    assert result["results"][0]["operation_id"] == OPERATION
    assert result["results"][1]["error_code"] == "permission_denied"
    assert result["results"][2]["error_code"] == "not_submitted"
    assert len([request for request in seen if request.url.path == "/execute"]) == 2
    assert_safe(result)


@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout(SECRET), httpx.WriteError(SECRET), httpx.ConnectError(SECRET),
    httpx.Response(408, text=SECRET), httpx.Response(500, text=SECRET),
    httpx.Response(302, headers={"location": "https://private.invalid/" + SECRET}),
    httpx.Response(200, text=SECRET), httpx.Response(200, json={"_id": {"$oid": OPERATION}, "config": SECRET}),
])
def test_uncertain_submission_never_retries_or_follows_redirect_and_stops_rest(failure, caplog):
    seen = []

    def submit(request, payload):
        if isinstance(failure, Exception):
            raise failure
        return failure

    result = run_action(handler_for(submit=submit, seen=seen), ids=[FIRST, SECOND])
    assert [entry["status"] for entry in result["results"]] == ["outcome_unknown", "not_submitted"]
    assert len([request for request in seen if request.url.path == "/execute"]) == 1
    assert_safe(result)
    assert SECRET not in caplog.text


@pytest.mark.parametrize("status,success,expected", [
    ("Queued", False, "submitted"), ("InProgress", False, "submitted"),
    ("Complete", True, "completed"), ("Complete", False, "failed"),
])
def test_update_status_is_not_confused_with_completion(status, success, expected):
    def submit(request, payload):
        return update_response("servers", payload["type"], FIRST, status=status, success=success)

    result = run_action(handler_for(submit=submit))
    assert result["results"][0]["status"] == expected
    assert result["results"][0]["ok"] is (expected in {"submitted", "completed"})
    assert_safe(result)


@pytest.mark.parametrize("field,value", [
    ("target", {"type": "Server", "id": SECOND}), ("operation", "DestroyStack"),
    ("status", [SECRET]), ("success", "true"), ("_id", {"$oid": SECRET}),
])
def test_unexpected_update_identity_or_schema_is_uncertain_not_reported_as_success(field, value):
    def submit(request, payload):
        data = update_response("servers", payload["type"], FIRST).json()
        data[field] = value
        return httpx.Response(200, json=data)

    result = run_action(handler_for(submit=submit))
    assert result["results"][0]["status"] == "outcome_unknown"
    assert_safe(result)


def test_dispatch_budget_stops_unsubmitted_resources_without_cancelling_previous_update(monkeypatch):
    readings = iter([100.0, 100.0, 145.0, 146.0])
    monkeypatch.setattr("plugins.komodo.actions.monotonic", lambda: next(readings))
    seen = []
    result = run_action(handler_for(seen=seen), ids=[FIRST, SECOND, THIRD])
    assert [entry["status"] for entry in result["results"]] == ["submitted", "not_submitted", "not_submitted"]
    assert len([request for request in seen if request.url.path == "/execute"]) == 1


def test_total_submission_timeout_is_uncertain_and_never_retries(monkeypatch):
    monkeypatch.setattr("plugins.komodo.actions._REQUEST_SECONDS", 0.01)
    normal = handler_for()
    writes = []

    async def upstream(request):
        if request.url.path == "/execute":
            writes.append(request)
            await asyncio.sleep(1)
        return normal(request)

    result = run_action(upstream, ids=[FIRST, SECOND])
    assert [entry["status"] for entry in result["results"]] == ["outcome_unknown", "not_submitted"]
    assert len(writes) == 1


def test_non_template_running_stack_delete_uses_actual_native_write_not_destroy_rpc():
    seen = []
    result = run_action(handler_for("stacks", seen=seen), "stacks", "delete")
    # Native DeleteStack may take down a running stack in its pre_delete hook;
    # the UI must confirm that effect. The integration adds no separate destroy.
    assert [request.url.path for request in seen] == ["/user", "/read", "/write"]
    assert json.loads(seen[-1].content) == {"type": "DeleteStack", "params": {"id": FIRST}}
    assert result["results"][0]["status"] == "completed"
    assert_safe(result)
