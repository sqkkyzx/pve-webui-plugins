import asyncio
from datetime import datetime
import json
from pathlib import Path
import re

import httpx
import pytest

from plugins.komodo.komodo import KomodoClient, KomodoDashboardError
from plugins.komodo.updates import OPERATIONS, RESOURCE_TYPES


ORIGIN = "http://core:9120"
KEY, SECRET = "updates-only-api-key", "updates-only-api-secret"
UPDATE, RESOURCE, OTHER, OPERATOR = "a" * 24, "b" * 24, "c" * 24, "d" * 24
START, END = 1792256400123, 1792256460456
LISTS = {
    "Swarm": "ListSwarms", "Server": "ListServers", "Stack": "ListStacks", "Deployment": "ListDeployments",
    "Build": "ListBuilds", "Repo": "ListRepos", "Procedure": "ListProcedures", "Action": "ListActions",
    "Builder": "ListBuilders", "Alerter": "ListAlerters", "ResourceSync": "ListResourceSyncs",
}


def item(*, detail=False, resource_type="Server", resource_id=RESOURCE, status="Complete", success=True):
    result = {
        "operation": "RestartAllContainers", "start_ts": START, "status": status, "success": success,
        "operator": OPERATOR, "target": {"type": resource_type, "id": resource_id},
        "other_data": SECRET,
    }
    if detail:
        result.update({
            "_id": {"$oid": UPDATE}, "end_ts": END,
            "version": {"major": 2, "minor": 3, "patch": 3}, "commit_hash": "abcdef0123456789",
            "prev_toml": SECRET, "current_toml": KEY, "config": {"environment": SECRET},
            "logs": [{"stage": "Restart containers", "success": True, "start_ts": START, "end_ts": END,
                      "command": "secret-command " + SECRET, "stdout": KEY, "stderr": "https://private.invalid/" + SECRET}],
        })
    else:
        result.update(id=UPDATE, username="integration-user")
    return result


def client(handler, *, secret=SECRET):
    return KomodoClient(ORIGIN, KEY, secret, transport=httpx.MockTransport(handler))


def list_updates(handler, **filters):
    return asyncio.run(client(handler).updates(**filters))


def details(handler, identifier=UPDATE):
    return asyncio.run(client(handler).update_details(identifier))


def handler_for(rows=None, *, full=None, next_page=None, page=0, query=None, overrides=None, seen=None):
    overrides = overrides or {}

    def upstream(request):
        if seen is not None:
            seen.append(request)
        assert request.headers["x-api-key"] == KEY
        assert request.headers["x-api-secret"] == SECRET
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if request.url.path == "/user":
            assert request.method == "GET"
            return overrides.get("user") or httpx.Response(200, json={"username": "integration", "enabled": True}, headers={
                "set-cookie": "PVEAuthCookie=must-not-replay; Path=/",
            })
        assert str(request.url) == ORIGIN + "/read"
        assert request.method == "POST"
        payload = json.loads(request.content)
        variant = payload["type"]
        if variant == "ListUpdates":
            assert payload["params"] == {"page": page, "query": query}
        elif variant == "GetUpdate":
            assert payload["params"] == {"id": UPDATE}
        else:
            assert variant in LISTS.values()
            assert payload["params"]["limit"] == 0
            assert payload["params"]["query"]["templates"] == "Include"
            assert payload["params"]["query"]["names"]
            assert all(re.fullmatch(r"[a-f0-9]{24}", identifier) for identifier in payload["params"]["query"]["names"])
        if variant in overrides:
            response = overrides[variant]
            if isinstance(response, Exception):
                raise response
            return response
        if variant == "ListUpdates":
            return httpx.Response(200, json={"updates": rows if rows is not None else [item()], "next_page": next_page})
        if variant == "GetUpdate":
            return httpx.Response(200, json=full if full is not None else item(detail=True))
        resource_type = next(key for key, value in LISTS.items() if value == variant)
        return httpx.Response(200, json=[{
            "id": identifier, "name": "current-" + resource_type, "type": resource_type,
            "info": {"address": SECRET}, "config": {"environment": SECRET},
        } for identifier in payload["params"]["query"]["names"]], headers={"set-cookie": "other=must-not-replay; Path=/"})

    return upstream


def assert_safe(result):
    text = json.dumps(result, allow_nan=False)
    for forbidden in (KEY, SECRET, "private.invalid", "secret-command", "PVEAuthCookie", "environment"):
        assert forbidden not in text


def test_operation_constant_matches_source_schema_instead_of_a_partial_toolbar_list():
    source = Path(__file__).parent / "fixtures/komodo-operation.rs"
    body = source.read_text().split("pub enum Operation {", 1)[1].split("\n}", 1)[0]
    expected = tuple(re.findall(r"^  ([A-Za-z][A-Za-z0-9]*),", body, flags=re.M))
    assert OPERATIONS == expected
    assert len(OPERATIONS) == 137


def test_list_uses_fixed_rpc_and_one_name_lookup_per_type_without_per_row_get_update():
    seen = []
    result = list_updates(handler_for(rows=[item(), item(resource_id=OTHER)], seen=seen))
    assert [json.loads(request.content)["type"] for request in seen[1:]] == ["ListUpdates", "ListServers"]
    assert json.loads(seen[-1].content)["params"] == {"query": {"names": [RESOURCE, OTHER], "templates": "Include"}, "limit": 0}
    assert result["updates"][0] == {
        "id": UPDATE, "operation": "RestartAllContainers", "start_ts": START, "status": "Complete", "success": True,
        "username": "integration-user", "resource_type": "Server", "resource_id": RESOURCE, "resource_name": "current-Server",
    }
    assert "end_ts" not in result["updates"][0]
    assert result["errors"] == []
    assert result["page"] == 0 and result["next_page"] is None
    assert datetime.fromisoformat(result["updated_at"]).utcoffset().total_seconds() == 0
    assert_safe(result)


def test_only_target_type_and_operation_are_generated_as_query_keys():
    query = {"target.type": "Server", "operation": "RestartAllContainers"}
    result = list_updates(handler_for(page=3, query=query), page=3, resource_type="Server", operation="RestartAllContainers")
    assert result["page"] == 3


@pytest.mark.parametrize("operation", OPERATIONS)
def test_every_source_operation_is_accepted_as_a_filter(operation):
    list_updates(handler_for(rows=[], query={"operation": operation}), operation=operation)


@pytest.mark.parametrize("resource_type", RESOURCE_TYPES)
def test_every_resource_type_can_filter(resource_type):
    list_updates(handler_for(rows=[], query={"target.type": resource_type}), resource_type=resource_type)


@pytest.mark.parametrize("filters", [
    {"page": True}, {"page": -1}, {"page": "0"}, {"page": 0.0}, {"page": 1_000_001},
    {"resource_type": {"$ne": "System"}}, {"resource_type": "Other"}, {"operation": "RunArbitraryCommand"},
    {"operation": {"$regex": ".*"}}, {"operation": None},
])
def test_invalid_filters_never_connect(filters):
    def unexpected(request):
        pytest.fail("invalid update filters must not connect")

    with pytest.raises(KomodoDashboardError) as error:
        list_updates(unexpected, **filters)
    assert error.value.code == "invalid_request" and error.value.status_code == 400


@pytest.mark.parametrize("identifier", ["name", "*", UPDATE[:12], None, True, {"$oid": UPDATE}, " " + UPDATE])
def test_invalid_detail_id_never_connects(identifier):
    def unexpected(request):
        pytest.fail("invalid detail ids must not connect")

    with pytest.raises(KomodoDashboardError) as error:
        details(unexpected, identifier)
    assert error.value.code == "invalid_request"


@pytest.mark.parametrize("mode", ["list", "details"])
def test_missing_configuration_and_disabled_users_prevent_reads(mode):
    def unexpected(request):
        pytest.fail("unconfigured history client must not connect")

    coroutine = client(unexpected, secret=None).updates() if mode == "list" else client(unexpected, secret=None).update_details(UPDATE)
    with pytest.raises(KomodoDashboardError) as error:
        asyncio.run(coroutine)
    assert error.value.code == "not_configured"
    seen = []
    handler = handler_for(overrides={"user": httpx.Response(200, json={"username": "service", "enabled": False})}, seen=seen)
    with pytest.raises(KomodoDashboardError) as error:
        list_updates(handler) if mode == "list" else details(handler)
    assert error.value.code == "invalid_credentials"
    assert [request.url.path for request in seen] == ["/user"]


@pytest.mark.parametrize("response,code", [
    (httpx.Response(401, text=SECRET), "invalid_credentials"), (httpx.Response(403, text=SECRET), "permission_denied"),
    (httpx.Response(404, text=SECRET), "resource_not_found"), (httpx.Response(503, text=SECRET), "upstream_unavailable"),
    (httpx.Response(200, text=SECRET), "invalid_response"),
    (httpx.Response(302, headers={"location": "https://private.invalid/" + SECRET}), "upstream_unavailable"),
    (httpx.ReadTimeout(SECRET), "upstream_unavailable"),
])
@pytest.mark.parametrize("mode", ["list", "details"])
def test_primary_read_errors_are_sanitized_and_redirects_not_followed(response, code, mode, caplog):
    seen = []
    handler = handler_for(overrides={"ListUpdates" if mode == "list" else "GetUpdate": response}, seen=seen)
    with pytest.raises(KomodoDashboardError) as error:
        list_updates(handler) if mode == "list" else details(handler)
    assert error.value.code == code
    assert len(seen) == 2
    assert SECRET not in repr(error.value) and SECRET not in caplog.text


@pytest.mark.parametrize("resource_type", LISTS)
def test_name_enrichment_uses_only_corresponding_fixed_list_and_requested_ids(resource_type):
    seen = []
    result = list_updates(handler_for(rows=[item(resource_type=resource_type)], seen=seen))
    assert len(seen) == 3
    assert json.loads(seen[-1].content)["type"] == LISTS[resource_type]
    assert result["updates"][0]["resource_name"] == "current-" + resource_type
    assert_safe(result)


def test_system_and_empty_targets_require_no_name_lookup():
    seen = []
    rows = [item(resource_type="System", resource_id="system"), item(resource_id=""), item(resource_id=None)]
    result = list_updates(handler_for(rows=rows, seen=seen))
    assert len(seen) == 2
    assert result["updates"][0]["resource_name"] == "系统"
    assert result["updates"][0]["resource_id"] == "system"
    assert result["updates"][1]["resource_name"] is None


@pytest.mark.parametrize("response,code", [
    (httpx.Response(403, text=SECRET), "resource_names_permission_denied"),
    (httpx.Response(500, text=SECRET), "resource_names_unavailable"),
    (httpx.ReadTimeout(SECRET), "resource_names_unavailable"),
    (httpx.Response(200, json={"error": SECRET}), "resource_names_invalid_response"),
    (httpx.Response(302, headers={"location": "https://private.invalid/" + SECRET}), "resource_names_unavailable"),
])
def test_name_lookup_failure_keeps_history_with_unknown_names_and_safe_error_code(response, code):
    result = list_updates(handler_for(overrides={"ListServers": response}))
    assert len(result["updates"]) == 1
    assert result["updates"][0]["resource_name"] is None
    assert result["errors"] == [code]
    assert_safe(result)


def test_deleted_resource_stays_unknown_without_guessing_name_from_sensitive_other_data():
    result = list_updates(handler_for(overrides={"ListServers": httpx.Response(200, json=[])}))
    assert result["updates"][0]["resource_name"] is None
    assert result["errors"] == []
    assert_safe(result)


def test_enrichment_groups_run_concurrently_with_one_total_budget_and_isolated_cookies(monkeypatch):
    monkeypatch.setattr("plugins.komodo.updates._ENRICH_SECONDS", 0.05)
    normal = handler_for(rows=[item(resource_type="Server"), item(resource_type="Stack"), item(resource_type="Procedure")])
    started, cancelled = set(), set()

    async def upstream(request):
        if request.url.path == "/read":
            variant = json.loads(request.content)["type"]
            if variant in {"ListServers", "ListStacks", "ListProcedures"}:
                started.add(variant)
                assert "cookie" not in request.headers
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    cancelled.add(variant)
                    raise
        return normal(request)

    result = list_updates(upstream)
    assert started == cancelled == {"ListServers", "ListStacks", "ListProcedures"}
    assert len(result["updates"]) == 3
    assert result["errors"] == ["resource_names_unavailable"]
    assert all(row["resource_name"] is None for row in result["updates"])


@pytest.mark.parametrize("status,success,expected", [
    ("Queued", False, None), ("InProgress", False, None), ("Complete", True, True),
    ("Complete", False, False), ("Complete", "false", None), ("future-state", False, None),
])
def test_pending_operations_are_not_mislabeled_failed(status, success, expected):
    result = list_updates(handler_for(rows=[item(status=status, success=success)]))
    assert result["updates"][0]["success"] is expected


@pytest.mark.parametrize("value", [True, -1, 1.0, "1792256400123", None, 8_640_000_000_000_001])
def test_invalid_timestamps_are_null_without_guessing_units(value):
    row = item()
    row["start_ts"] = value
    result = list_updates(handler_for(rows=[row]))
    assert result["updates"][0]["start_ts"] is None


def test_full_page_can_be_followed_by_empty_page_and_max_page_has_no_unrequestable_next():
    result = list_updates(handler_for(rows=[item(resource_type="System", resource_id="system")] * 100, next_page=1))
    assert result["next_page"] == 1
    assert list_updates(handler_for(rows=[], page=1), page=1)["updates"] == []
    result = list_updates(handler_for(rows=[], page=1_000_000, next_page=1_000_001), page=1_000_000)
    assert result["next_page"] is None


@pytest.mark.parametrize("next_page", [-1, True, 0, 2, "1", 1.0])
def test_non_contiguous_or_non_integer_next_page_is_rejected(next_page):
    with pytest.raises(KomodoDashboardError) as error:
        list_updates(handler_for(next_page=next_page))
    assert error.value.code == "invalid_response"


def test_detail_strips_all_output_and_configuration_and_returns_only_stage_metadata():
    seen = []
    result = details(handler_for(seen=seen))
    row = result["update"]
    assert row == {
        "id": UPDATE, "operation": "RestartAllContainers", "start_ts": START, "end_ts": END,
        "status": "Complete", "success": True, "username": None, "operator_id": OPERATOR,
        "resource_type": "Server", "resource_id": RESOURCE, "resource_name": "current-Server",
        "version": {"major": 2, "minor": 3, "patch": 3}, "commit_hash": "abcdef0123456789",
        "stages": [{"stage": "Restart containers", "success": True, "start_ts": START, "end_ts": END}],
        "details_omitted": True,
    }
    assert [json.loads(request.content)["type"] for request in seen[1:]] == ["GetUpdate", "ListServers"]
    assert_safe(result)


@pytest.mark.parametrize("operator,username,operator_id", [
    ("Procedure", "Procedure", None), ("Github", "Github", None), ("Auto Redeploy", "Auto Redeploy", None),
    ("000000000000000000000003", "Git Webhook", None), (OPERATOR, None, OPERATOR),
    (SECRET, None, None), ({"private": SECRET}, None, None),
])
def test_detail_service_names_are_allowlisted_and_user_ids_are_not_called_usernames(operator, username, operator_id):
    full = item(detail=True)
    full["operator"] = operator
    result = details(handler_for(full=full))
    assert result["update"]["username"] == username
    assert result["update"]["operator_id"] == operator_id
    assert_safe(result)


@pytest.mark.parametrize("index,name", list(enumerate([
    "System", "Procedure", "Action", "Git Webhook", "Auto Redeploy", "Resource Sync",
    "Stack Wizard", "Build Manager", "Repo Manager",
])))
def test_list_maps_real_reserved_username_and_operator_ids_to_source_service_names(index, name):
    row = item()
    row.update(operator=f"{index:024x}", username=f"{index:024x}")
    result = list_updates(handler_for(rows=[row]))
    assert result["updates"][0]["username"] == name


@pytest.mark.parametrize("operator,username,expected", [
    ("Procedure", "unknown", "Procedure"), ("Github", "unknown", "Github"),
    (OPERATOR, "human-user", "human-user"), (OPERATOR, OPERATOR, OPERATOR),
    ("000000000000000000000009", "another-user", "another-user"),
])
def test_list_preserves_non_service_usernames_and_only_maps_known_service_operators(operator, username, expected):
    row = item()
    row.update(operator=operator, username=username)
    result = list_updates(handler_for(rows=[row]))
    assert result["updates"][0]["username"] == expected


@pytest.mark.parametrize("identifier", [OTHER, {"$oid": OTHER}, None, SECRET])
def test_detail_response_must_have_matching_bson_update_id(identifier):
    full = item(detail=True)
    full["_id"] = identifier
    with pytest.raises(KomodoDashboardError) as error:
        details(handler_for(full=full))
    assert error.value.code == "invalid_response"


def test_detail_preserves_true_epoch_zero_and_does_not_expose_raw_invalid_stage_fallbacks():
    full = item(detail=True)
    full.update(start_ts=0, end_ts=0, logs=[
        {"stage": "", "command": SECRET, "success": True, "start_ts": 0, "end_ts": 0},
        {"stage": "bad\nlabel", "stdout": SECRET, "success": "false", "start_ts": True, "end_ts": -1},
    ])
    result = details(handler_for(full=full))
    assert result["update"]["start_ts"] == result["update"]["end_ts"] == 0
    assert result["update"]["stages"] == [
        {"stage": None, "success": True, "start_ts": 0, "end_ts": 0},
        {"stage": None, "success": None, "start_ts": None, "end_ts": None},
    ]
    assert_safe(result)


def test_stage_limit_prevents_unbounded_detail_payloads_and_reports_omission():
    full = item(detail=True)
    full["logs"] = [{"stage": "stage", "success": True, "start_ts": START, "end_ts": END}] * 501
    result = details(handler_for(full=full))
    assert len(result["update"]["stages"]) == 500
    assert result["update"]["details_omitted"] is True


def test_pve_cookie_jar_and_komodo_clients_remain_isolated_across_reads():
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
            assert (await komodo.updates())["updates"]
            assert (await komodo.update_details(UPDATE))["update"]
            await pve.get("https://pve.example:8006/api2/json/access/permissions")

    asyncio.run(scenario())
