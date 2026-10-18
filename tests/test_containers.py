import asyncio
from copy import deepcopy
from datetime import datetime
import json

import httpx
import pytest

from plugins.komodo.komodo import KomodoActionError, KomodoClient, KomodoDashboardError


ORIGIN = "http://core:9120"
KEY, SECRET = "container-api-key", "container-api-secret"
SERVER, OTHER_SERVER, TEMPLATE = "a" * 24, "b" * 24, "e" * 24
CONTAINER, OTHER_CONTAINER, TEMPLATE_CONTAINER, OPERATION = "c" * 64, "d" * 64, "f" * 64, "9" * 24
FIRST, SECOND = SERVER + ":" + CONTAINER, OTHER_SERVER + ":" + OTHER_CONTAINER
STACK, OTHER_STACK = "1" * 24, "2" * 24


def data():
    return {
        "ListServers": [
            {"id": SERVER, "name": "docker-vm", "type": "Server", "template": False, "tags": ["tag-one"], "info": {"address": SECRET}},
            {"id": OTHER_SERVER, "name": "docker-ct", "type": "Server", "template": False, "tags": [], "config": {"environment": SECRET}},
            {"id": TEMPLATE, "name": "template", "type": "Server", "template": True, "tags": []},
        ],
        "ListAllContainers": [
            {"id": CONTAINER, "server_id": SERVER, "server_name": "ignored-untrusted-name", "name": "same-name", "state": "running",
             "image": "registry.example:5000/project/app:latest", "networks": ["project_default"],
             "ports": [{"IP": "192.0.2.123", "PublicPort": 18080, "PrivatePort": 8080, "Type": "tcp"},
                       {"IP": "2001:db8::123", "PublicPort": 18080, "PrivatePort": 8080, "Type": "tcp"},
                       {"PrivatePort": 53, "Type": "udp"}],
             "volumes": ["app-data", "/private/bind/" + SECRET], "labels": {"key": SECRET}, "env": [SECRET], "stats": {"private": SECRET}},
            {"id": OTHER_CONTAINER, "server_id": OTHER_SERVER, "name": "same-name", "state": "paused", "image": "alpine:latest",
             "networks": [], "network_mode": "host", "ports": [], "volumes": []},
            {"id": TEMPLATE_CONTAINER, "server_id": TEMPLATE, "name": "template-container", "state": "running", "image": "alpine"},
        ],
        "ListTags": [{"_id": {"$oid": "tag-one"}, "name": "production", "owner": SECRET}],
        "ListStacks": [], "ListAllStackServices": [],
    }


def client(handler, *, secret=SECRET):
    return KomodoClient(ORIGIN, KEY, secret, transport=httpx.MockTransport(handler))


def run_list(handler):
    return asyncio.run(client(handler).containers())


def run_action(handler, action="restart", ids=None):
    return asyncio.run(client(handler).perform_action("containers", action, ids if ids is not None else [FIRST]))


def handler_for(raw=None, *, overrides=None, submit=None, seen=None):
    raw = raw or data()
    overrides = overrides or {}

    def handler(request):
        if seen is not None:
            seen.append(request)
        assert request.headers["x-api-key"] == KEY
        assert request.headers["x-api-secret"] == SECRET
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        assert str(request.url).startswith(ORIGIN)
        if request.url.path == "/user":
            assert request.method == "GET"
            return overrides.get("user") or httpx.Response(200, json={"username": "integration", "enabled": True}, headers={
                "set-cookie": "PVEAuthCookie=must-not-replay; Path=/",
            })
        assert request.method == "POST"
        payload = json.loads(request.content)
        if request.url.path == "/read":
            variant = payload["type"]
            if variant == "ListAllStackServices":
                assert set(payload["params"]) == {"stacks", "limit"}
                assert payload["params"]["limit"] == 0
                selection = payload["params"]["stacks"]
                assert selection and selection == sorted(set(selection))
                assert all(isinstance(value, str) and len(value) == 24
                    and set(value) <= set("0123456789abcdef") for value in selection)
            else:
                assert payload["params"] == {
                "ListServers": {"query": {"templates": "Include"}, "limit": 0},
                "ListAllContainers": {"limit": 0}, "ListTags": {},
                "ListStacks": {"query": {"templates": "Exclude"}, "limit": 0},
                }[variant]
            if variant in overrides:
                response = overrides[variant]
                if isinstance(response, Exception):
                    raise response
                return response
            return httpx.Response(200, json=raw[variant], headers={"set-cookie": "other-session=must-not-replay; Path=/"})
        assert request.url.path == "/execute"
        if submit is not None:
            return submit(request, payload)
        return httpx.Response(200, json={
            "_id": {"$oid": OPERATION}, "operation": payload["type"],
            "target": {"type": "Server", "id": payload["params"]["server"]},
            "status": "InProgress", "success": False, "logs": [{"stdout": SECRET}], "other_data": SECRET,
        })

    return handler


def assert_safe(value):
    text = json.dumps(value)
    for forbidden in (KEY, SECRET, "192.0.2.123", "2001:db8::123", "PVEAuthCookie", "ignored-untrusted-name"):
        assert forbidden not in text


def test_container_list_uses_exact_fixed_reads_and_allowlisted_display_fields():
    seen = []
    result = run_list(handler_for(seen=seen))
    assert [request.url.path for request in seen] == ["/user", "/read", "/read", "/read", "/read"]
    assert sorted(json.loads(request.content)["type"] for request in seen[1:]) == ["ListAllContainers", "ListServers", "ListStacks", "ListTags"]
    assert result["containers"] == [
        {"id": FIRST, "container_id": CONTAINER, "server_id": SERVER, "server_name": "docker-vm", "name": "same-name",
         "state": "running", "image": "registry.example:5000/project/app:latest", "networks": ["project_default"],
         "ports": ["18080→8080/tcp", "53/udp"], "volumes": ["app-data"], "tags": ["production"],
         "stack_id": None, "stack_association": "unassigned"},
        {"id": SECOND, "container_id": OTHER_CONTAINER, "server_id": OTHER_SERVER, "server_name": "docker-ct", "name": "same-name",
         "state": "paused", "image": "alpine:latest", "networks": ["host"], "ports": [], "volumes": [], "tags": [],
         "stack_id": None, "stack_association": "unassigned"},
    ]
    assert result["servers"] == [{"id": SERVER, "name": "docker-vm"}, {"id": OTHER_SERVER, "name": "docker-ct"}]
    assert result["stacks"] == []
    assert result["errors"] == []
    assert datetime.fromisoformat(result["updated_at"]).utcoffset().total_seconds() == 0
    assert_safe(result)


@pytest.mark.parametrize("variant,response,code", [
    ("ListAllContainers", httpx.Response(403, text=SECRET), "containers_permission_denied"),
    ("ListAllContainers", httpx.ReadTimeout(SECRET), "containers_upstream_unavailable"),
    ("ListAllContainers", httpx.Response(200, json={"error": SECRET}), "containers_invalid_response"),
    ("ListAllContainers", httpx.Response(302, headers={"location": "https://private.invalid/" + SECRET}), "containers_upstream_unavailable"),
    ("ListTags", httpx.Response(403, text=SECRET), "tags_permission_denied"),
    ("ListServers", httpx.Response(403, text=SECRET), "servers_permission_denied"),
])
def test_partial_read_failures_keep_safe_available_data_without_following_redirects(variant, response, code):
    seen = []
    result = run_list(handler_for(overrides={variant: response}, seen=seen))
    assert code in result["errors"]
    assert len(seen) == 5
    if variant in {"ListAllContainers", "ListServers"}:
        assert result["containers"] == []
    else:
        assert len(result["containers"]) == 2
        assert result["containers"][0]["tags"] == []
    assert_safe(result)


@pytest.mark.parametrize("mode", ["list", "action"])
def test_disabled_account_is_rejected_before_resource_reads_or_writes(mode):
    seen = []
    handler = handler_for(overrides={"user": httpx.Response(200, json={"username": "service", "enabled": False})}, seen=seen)
    with pytest.raises(KomodoDashboardError if mode == "list" else KomodoActionError) as error:
        run_list(handler) if mode == "list" else run_action(handler)
    assert error.value.code == "invalid_credentials"
    assert [request.url.path for request in seen] == ["/user"]


def test_missing_configuration_does_not_contact_upstream_for_read_or_action():
    def unexpected(request):
        pytest.fail("unconfigured container client must not connect")

    with pytest.raises(KomodoDashboardError) as read_error:
        asyncio.run(client(unexpected, secret=None).containers())
    assert read_error.value.code == "not_configured"
    with pytest.raises(KomodoActionError) as action_error:
        asyncio.run(client(unexpected, secret=None).perform_action("containers", "start", [FIRST]))
    assert action_error.value.code == "not_configured"


def test_malformed_list_rows_short_ids_and_hidden_server_rows_are_not_exposed():
    raw = data()
    raw["ListAllContainers"].extend([
        None, {"id": CONTAINER[:12], "server_id": SERVER, "name": "short"},
        {"id": "8" * 64, "server_id": "7" * 24, "name": SECRET},
    ])
    raw["ListAllContainers"][1].update(image="https://user:" + SECRET + "@private.invalid", state={"private": SECRET})
    result = run_list(handler_for(raw))
    assert len(result["containers"]) == 2
    assert result["containers"][1]["image"] is None
    assert result["containers"][1]["state"] == "unknown"
    assert result["errors"] == ["containers_invalid_response"]
    assert_safe(result)


def stack_data():
    raw = data()
    raw["ListStacks"] = [
        {"id": STACK, "name": "stack-one", "template": False,
         "info": {"server_id": SERVER, "swarm_id": "", "environment": SECRET}, "config": {"secret": SECRET}},
        {"id": OTHER_STACK, "name": "stack-two", "template": False,
         "info": {"server_id": OTHER_SERVER, "swarm_id": ""}},
    ]
    raw["ListAllStackServices"] = [
        {"stack_id": STACK, "stack_name": SECRET, "service": SECRET, "image_digests": [SECRET],
         "container": {"id": CONTAINER, "server_id": SERVER, "name": "not-the-current-name", "env": [SECRET]},
         "swarm_service": None},
        {"stack_id": OTHER_STACK, "container": {"id": OTHER_CONTAINER, "server_id": OTHER_SERVER}},
    ]
    return raw


def test_tree_membership_uses_exact_ids_and_selected_resource_metadata_not_names():
    raw = stack_data()
    raw["ListAllStackServices"].append(deepcopy(raw["ListAllStackServices"][0]))
    raw["ListAllStackServices"].append({"stack_id": STACK, "container": None, "service": "not-deployed"})
    # Normalization is safe for the IDs; all names come from the current lists.
    raw["ListAllStackServices"][0]["container"]["id"] = CONTAINER.upper()
    seen = []
    result = run_list(handler_for(raw, seen=seen))
    assert result["stacks"] == [{"id": STACK, "name": "stack-one", "server_id": SERVER},
                                {"id": OTHER_STACK, "name": "stack-two", "server_id": OTHER_SERVER}]
    assert [(row["stack_id"], row["stack_association"]) for row in result["containers"]] == [
        (STACK, "matched"), (OTHER_STACK, "matched")]
    services_request = next(json.loads(request.content) for request in seen[1:]
                            if json.loads(request.content)["type"] == "ListAllStackServices")
    assert services_request == {"type": "ListAllStackServices", "params": {"stacks": [STACK, OTHER_STACK], "limit": 0}}
    assert result["errors"] == []
    assert_safe(result)
    assert "not-the-current-name" not in json.dumps(result)
    assert all(set(row) == {"id", "name", "server_id"} for row in result["stacks"])


@pytest.mark.parametrize("change", ["cross_server", "short_id", "missing_server", "malformed_container", "swarm_payload", "unknown_stack"])
def test_bad_stack_membership_never_hides_or_misassigns_current_containers(change):
    raw = stack_data()
    service = raw["ListAllStackServices"][0]
    if change == "cross_server": service["container"]["server_id"] = OTHER_SERVER
    if change == "short_id": service["container"]["id"] = CONTAINER[:12]
    if change == "missing_server": service["container"].pop("server_id")
    if change == "malformed_container": service["container"] = []
    if change == "swarm_payload": service["swarm_service"] = {"private": SECRET}
    if change == "unknown_stack": service["stack_id"] = "8" * 24
    result = run_list(handler_for(raw))
    assert len(result["containers"]) == 2
    assert result["containers"][0]["stack_id"] is None
    assert result["containers"][0]["stack_association"] == "unknown"
    assert result["containers"][1]["stack_id"] == OTHER_STACK
    assert result["containers"][1]["stack_association"] == "matched"
    assert "stack_services_invalid_response" in result["errors"]
    assert_safe(result)


def test_replaced_container_with_the_same_name_never_inherits_a_stale_stack_id():
    raw = stack_data()
    raw["ListAllStackServices"][0]["container"].update(id="8" * 64, name="same-name")
    result = run_list(handler_for(raw))
    assert result["containers"][0]["stack_id"] is None
    assert result["containers"][0]["stack_association"] == "unknown"
    assert "stack_services_stale_response" in result["errors"]


def test_identical_full_container_ids_on_different_servers_keep_separate_memberships():
    raw = stack_data()
    raw["ListAllContainers"][1]["id"] = CONTAINER
    raw["ListAllStackServices"][1]["container"]["id"] = CONTAINER
    result = run_list(handler_for(raw))
    assert [(row["id"], row["stack_id"]) for row in result["containers"]] == [
        (SERVER + ":" + CONTAINER, STACK), (OTHER_SERVER + ":" + CONTAINER, OTHER_STACK)]
    assert all(row["stack_association"] == "matched" for row in result["containers"])
    assert result["errors"] == []


def test_two_stacks_claiming_the_same_exact_container_are_an_explicit_conflict():
    raw = stack_data()
    raw["ListStacks"][1]["info"]["server_id"] = SERVER
    raw["ListAllStackServices"][1]["container"] = deepcopy(raw["ListAllStackServices"][0]["container"])
    result = run_list(handler_for(raw))
    assert result["containers"][0]["stack_id"] is None
    assert result["containers"][0]["stack_association"] == "conflict"
    assert result["containers"][1]["stack_association"] == "unassigned"
    assert result["errors"] == ["stack_membership_conflict"]


@pytest.mark.parametrize("server_id", [[], {}, {"private": SECRET}, 2, True])
def test_malformed_stack_target_is_partial_not_a_total_container_list_failure(server_id):
    raw = stack_data()
    raw["ListStacks"][0]["info"]["server_id"] = server_id
    raw["ListAllStackServices"] = raw["ListAllStackServices"][1:]
    result = run_list(handler_for(raw))
    assert len(result["containers"]) == 2
    assert len(result["stacks"]) == 1
    assert result["containers"][0]["stack_association"] == "unknown"
    assert "stacks_invalid_response" in result["errors"]
    assert_safe(result)


def test_templates_swarm_precedence_and_inaccessible_servers_never_enter_the_tree():
    raw = stack_data()
    raw["ListStacks"].extend([
        {"id": "3" * 24, "name": SECRET, "template": True, "info": {"server_id": SERVER}},
        {"id": "4" * 24, "name": SECRET, "template": False, "info": {"server_id": SERVER, "swarm_id": "5" * 24}},
        {"id": "6" * 24, "name": SECRET, "template": False, "info": {"server_id": "7" * 24}},
        {"id": "8" * 24, "name": "not-targeted", "template": False, "info": {"server_id": ""}},
    ])
    raw["ListAllStackServices"].append({"stack_id": "6" * 24,
        "container": {"id": CONTAINER, "server_id": "7" * 24, "name": SECRET}})
    seen = []
    result = run_list(handler_for(raw, seen=seen))
    assert len(result["stacks"]) == 2 and len(result["containers"]) == 2
    assert result["errors"] == []
    selection = next(json.loads(request.content)["params"]["stacks"] for request in seen[1:]
                     if json.loads(request.content)["type"] == "ListAllStackServices")
    assert selection == [STACK, OTHER_STACK, "6" * 24]
    assert_safe(result)


@pytest.mark.parametrize("rows", [[], [None], [
    {"id": STACK, "name": "duplicate-one", "template": False, "info": {"server_id": SERVER}},
    {"id": STACK, "name": "duplicate-two", "template": False, "info": {"server_id": OTHER_SERVER}},
]])
def test_empty_or_ambiguous_stack_selection_never_expands_to_all_services(rows):
    raw = data()
    raw["ListStacks"] = rows
    seen = []
    result = run_list(handler_for(raw, seen=seen))
    assert result["stacks"] == [] and len(result["containers"]) == 2
    assert all(json.loads(request.content)["type"] != "ListAllStackServices" for request in seen[1:])
    assert result["containers"][0]["stack_association"] == ("unknown" if rows else "unassigned")


@pytest.mark.parametrize("variant,prefix", [("ListStacks", "stacks"), ("ListAllStackServices", "stack_services")])
@pytest.mark.parametrize("response,suffix", [
    (httpx.Response(403, text=SECRET), "permission_denied"),
    (httpx.Response(401, text=SECRET), "permission_denied"),
    (httpx.Response(500, text=SECRET), "upstream_unavailable"),
    (httpx.Response(302, headers={"location": "http://private.invalid/" + SECRET}), "upstream_unavailable"),
    (httpx.ReadTimeout(SECRET), "upstream_unavailable"),
    (httpx.Response(200, json={"private": SECRET}), "invalid_response"),
    (httpx.Response(200, text="not-json-" + SECRET), "invalid_response"),
])
def test_optional_stack_failures_keep_container_rows_without_leaking_or_following_redirects(variant, prefix, response, suffix):
    seen = []
    result = run_list(handler_for(stack_data(), overrides={variant: response}, seen=seen))
    assert len(result["containers"]) == 2 and len(result["servers"]) == 2
    assert all(row["stack_id"] is None and row["stack_association"] == "unknown" for row in result["containers"])
    assert result["errors"] == [prefix + "_" + suffix]
    assert len(result["stacks"]) == (2 if variant == "ListAllStackServices" else 0)
    assert all(request.url.host == "core" and request.url.path in {"/user", "/read"} for request in seen)
    assert_safe(result)


def test_optional_stack_budget_cancels_slow_read_and_keeps_the_original_list(monkeypatch):
    monkeypatch.setattr("plugins.komodo.containers._STACK_SECONDS", 0.01)
    normal = handler_for(stack_data())
    seen = []
    async def handler(request):
        seen.append(request)
        if request.url.path == "/read" and json.loads(request.content)["type"] == "ListAllStackServices":
            await asyncio.sleep(0.1)
            pytest.fail("Optional stack enrichment must be cancelled at its own budget")
        return normal(request)
    result = run_list(handler)
    assert len(result["containers"]) == 2
    assert result["errors"] == ["stack_services_upstream_unavailable"]
    assert all(row["stack_association"] == "unknown" for row in result["containers"])
    assert_safe(result)


@pytest.mark.parametrize("action,variant", [
    ("start", "StartContainer"), ("restart", "RestartContainer"), ("pause", "PauseContainer"),
    ("unpause", "UnpauseContainer"), ("stop", "StopContainer"), ("destroy", "DestroyContainer"),
])
def test_six_actions_use_full_container_id_and_server_targeted_updates(action, variant):
    seen = []
    result = run_action(handler_for(seen=seen), action)
    assert [request.url.path for request in seen] == ["/user", "/read", "/read", "/execute"]
    assert json.loads(seen[-1].content) == {"type": variant, "params": {"server": SERVER, "container": CONTAINER}}
    assert result == {"section": "containers", "action": action, "results": [{
        "id": FIRST, "ok": True, "status": "submitted", "operation_id": OPERATION, "error_code": None,
    }]}
    assert_safe(result)


@pytest.mark.parametrize("action,ids", [
    ("delete", [FIRST]), ("DestroyContainer", [FIRST]), ("restart", []),
    ("start", [FIRST] * 51), ("start", [SERVER + ":" + CONTAINER[:12]]),
    ("start", [SERVER + ":same-name"]), ("start", ["same-name"]),
    ("start", [SERVER + ":sha256:" + CONTAINER]), ("start", [FIRST, "invalid"]),
    ("start", [{"server": SERVER, "container": CONTAINER}]), ("start", [True]),
])
def test_invalid_composite_selection_never_reads_or_submits(action, ids):
    def unexpected(request):
        pytest.fail("invalid container selection must not connect")

    with pytest.raises(KomodoActionError) as error:
        run_action(unexpected, action, ids)
    assert error.value.code == "invalid_request"


def test_same_names_on_two_hosts_use_distinct_composite_ids_and_deduplicate_casing():
    seen = []
    result = run_action(handler_for(seen=seen), ids=[FIRST.upper(), FIRST, SECOND])
    assert [entry["id"] for entry in result["results"]] == [FIRST, SECOND]
    writes = [json.loads(request.content) for request in seen if request.url.path == "/execute"]
    assert [payload["params"] for payload in writes] == [
        {"server": SERVER, "container": CONTAINER}, {"server": OTHER_SERVER, "container": OTHER_CONTAINER},
    ]


@pytest.mark.parametrize("case,code", [
    ("missing_server", "resource_not_found"), ("template", "template_forbidden"),
    ("missing_container", "resource_not_found"), ("replaced_same_name", "resource_not_found"),
    ("wrong_host", "resource_not_found"), ("duplicate_container", "invalid_response"),
])
def test_all_servers_and_exact_container_ids_are_preflighted_before_first_write(case, code):
    raw = data()
    if case == "missing_server":
        raw["ListServers"] = raw["ListServers"][:1]
    elif case == "template":
        raw["ListServers"][1]["template"] = True
    elif case == "missing_container":
        raw["ListAllContainers"] = raw["ListAllContainers"][:1]
    elif case == "replaced_same_name":
        raw["ListAllContainers"][1]["id"] = "8" * 64
    elif case == "wrong_host":
        raw["ListAllContainers"][1]["server_id"] = SERVER
    elif case == "duplicate_container":
        raw["ListAllContainers"].append(deepcopy(raw["ListAllContainers"][1]))
    seen = []
    with pytest.raises(KomodoActionError) as error:
        run_action(handler_for(raw, seen=seen), ids=[FIRST, SECOND])
    assert error.value.code == code
    assert all(request.url.path != "/execute" for request in seen)


@pytest.mark.parametrize("variant", ["ListServers", "ListAllContainers"])
def test_read_permission_failures_never_execute_a_container(variant):
    seen = []
    with pytest.raises(KomodoActionError) as error:
        run_action(handler_for(overrides={variant: httpx.Response(403, text=SECRET)}, seen=seen))
    assert error.value.code == "permission_denied"
    assert all(request.url.path != "/execute" for request in seen)
    assert SECRET not in repr(error.value)


@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout(SECRET), httpx.Response(408, text=SECRET), httpx.Response(502, text=SECRET),
    httpx.Response(200, text=SECRET), httpx.Response(302, headers={"location": "https://private.invalid/" + SECRET}),
])
def test_uncertain_container_action_never_retries_and_stops_remaining_ids(failure):
    seen = []

    def submit(request, payload):
        if isinstance(failure, Exception):
            raise failure
        return failure

    result = run_action(handler_for(submit=submit, seen=seen), ids=[FIRST, SECOND])
    assert [entry["status"] for entry in result["results"]] == ["outcome_unknown", "not_submitted"]
    assert len([request for request in seen if request.url.path == "/execute"]) == 1
    assert_safe(result)


def test_container_update_target_must_be_selected_server_not_a_container_or_different_server():
    def submit(request, payload):
        return httpx.Response(200, json={
            "_id": {"$oid": OPERATION}, "operation": payload["type"], "status": "InProgress", "success": False,
            "target": {"type": "Server", "id": OTHER_SERVER}, "logs": [{"stdout": SECRET}],
        })

    result = run_action(handler_for(submit=submit))
    assert result["results"][0]["status"] == "outcome_unknown"
    assert_safe(result)


def test_container_dispatch_budget_preserves_prior_submitted_update(monkeypatch):
    clock = iter([100.0, 100.0, 145.0])
    monkeypatch.setattr("plugins.komodo.containers.monotonic", lambda: next(clock))
    seen = []
    result = run_action(handler_for(seen=seen), ids=[FIRST, SECOND])
    assert [entry["status"] for entry in result["results"]] == ["submitted", "not_submitted"]
    assert len([request for request in seen if request.url.path == "/execute"]) == 1


def test_client_isolation_preserves_pve_cookie_and_keeps_it_away_from_komodo():
    normal = handler_for()

    def upstream(request):
        if request.url.host == "pve.example":
            assert request.headers["cookie"] == "PVEAuthCookie=private-ticket"
            assert "x-api-key" not in request.headers
            assert "x-api-secret" not in request.headers
            return httpx.Response(200, json={"data": []})
        return normal(request)

    async def scenario():
        transport = httpx.MockTransport(upstream)
        async with httpx.AsyncClient(transport=transport, cookies={"PVEAuthCookie": "private-ticket"}) as pve:
            await pve.get("https://pve.example:8006/api2/json/access/permissions")
            komodo = KomodoClient(ORIGIN, KEY, SECRET, transport=transport)
            assert (await komodo.containers())["containers"]
            assert (await komodo.perform_action("containers", "pause", [FIRST]))["results"][0]["ok"]
            await pve.get("https://pve.example:8006/api2/json/access/permissions")

    asyncio.run(scenario())
