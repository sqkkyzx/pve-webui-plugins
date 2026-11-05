"""Policy/CC contracts through authenticated routes; no real WAF mutations."""

import copy
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app
from test_safeline import BASE, COOKIE, TOKEN, configure
from test_safeline_editing import draft, waf_fixture
from test_site_routes import PVE, pve_transport


def condition(k="src_ip", op="eq", values=None, sub_k=""):
    return {"k": k, "op": op, "sub_k": sub_k, "v": values if values is not None else ["192.0.2.1"]}


@pytest.fixture
def security(tmp_path, monkeypatch):
    state, fallback = waf_fixture()
    state.update(edition=2, catalog=[{"id": 80, "policy_name": "Mixed library", "org_name": "Publisher", "black_rule": 2,
        "white_rule": 1, "compatible": 3, "added": False, "updated_at": 1790000000, "private": TOKEN}], listing=None)
    state["rules"] = [dict(copy.deepcopy(state["policy"]), id=i, action=action, order=order, expire=0, cloud_id=0,
                            pattern=[[condition()]], name="Rule " + str(i)) for i, action, order in [(2, 1, 1), (3, 1, 2), (4, 0, 1)]]
    state["rules"].extend([dict(copy.deepcopy(state["rules"][0]), id=1, builtin=True, order=0),
        dict(copy.deepcopy(state["rules"][0]), id=9, cloud_id=71, action=999, black_rule=3, white_rule=1)])
    state["cc"] = [{"id": i, "name": "CC " + str(i), "type": typ, "enabled": True, "period": 60, "count": 50,
        "block_min": 30, "action": "challenge_v1", "custom_status_codes": [], "built_in": True, "use_global": True,
        "conditions": [{"condition": {"field": "Authorization", "value": "private-condition"}}], "future": {"retained": True}}
        for i, typ in [(1, "req"), (2, "attack"), (3, "status")]]
    state["rate"] = [{"id": i, "ip": "192.0.2." + str(i), "status": status, "site_id": 12, "site_comment": "Admin",
        "site_title": "App", "site_server_names": ["app.example"], "country": "CN", "period": 60, "count": 50,
        "denied_count": 200, "block_min": 30, "reason": "attack", "result": "challenge_v1", "created_at": "2026-09-23T14:00:00+08:00",
        "started": 1790146413, "site_icon": "private-image", "private": TOKEN} for i, status in [(18, 0), (19, 1), (20, 2)]]
    state["waiting"] = [{"site_id": 12, "site_comment": "Admin", "site_server_names": ["app.example"], "rule_id": 1,
        "max_concurrent": 5, "total_waiting": 9, "top_waiting": 7, "avg_wait_sec": 22, "bounce_rate": .44,
        "dur_sec": 60, "started_at": 1790146412, "ended_at": 1790146472, "site_icon": "private-image", "private": TOKEN}]

    def handler(request):
        path, method = request.url.path, request.method
        paths = {"/api/open/policy", "/api/open/policy/detail", "/api/open/policy/orders", "/api/open/policy/switch", "/api/open/cloud/policies",
                 "/api/open/cloud/policies/subscribe", "/api/open/records/acl", "/api/open/records/waiting", "/api/open/system/edition",
                 "/api/open/site/0/acl", "/api/acl/relieve", "/api/open/ipgroup", "/api/open/ja4"}
        if path not in paths:
            return fallback.handle_request(request)
        state["waf"].append(request)
        assert request.headers["x-slce-api-token"] == TOKEN
        assert not {"cookie", "authorization", "x-pve-plugins-csrf"} & set(request.headers)
        if method != "GET":
            payload = json.loads(request.content); state["writes"].append((method, path, payload)); result = None
            if state["failure"] == "timeout" or state["failure"] == "order-timeout" and path.endswith("/orders"): raise httpx.ReadTimeout(TOKEN)
            if state["failure"] == "business": return httpx.Response(200, json={"data": None, "err": "failure", "msg": TOKEN})
            if state["failure"] != "mismatch":
                if path == "/api/open/policy" and method == "POST":
                    result = 100; state["rules"].append(dict(copy.deepcopy(payload), id=result, order=3, builtin=False, cloud_id=0))
                elif path == "/api/open/policy" and method == "PUT":
                    next(r for r in state["rules"] if r["id"] == payload["id"]).update(payload)
                elif path == "/api/open/policy" and method == "DELETE":
                    assert set(payload) == {"id"}; state["rules"] = [r for r in state["rules"] if r["id"] != payload["id"]]
                elif path == "/api/open/policy/switch":
                    assert method == "PUT" and set(payload) == {"id", "is_enabled"}
                    next(r for r in state["rules"] if r["id"] == payload["id"])["is_enabled"] = payload["is_enabled"]
                elif path == "/api/open/policy/orders":
                    assert method == "PUT" and set(payload) == {"orders"}
                    for row in payload["orders"]: next(r for r in state["rules"] if r["id"] == row["id"])["order"] = row["order"]
                elif path == "/api/open/cloud/policies/subscribe":
                    assert method == "POST" and set(payload) == {"id"}
                    next(r for r in state["catalog"] if r["id"] == payload["id"])["added"] = True
                elif path == "/api/open/site/0/acl":
                    assert method == "PUT" and set(payload) == {"rules"}; state["cc"] = copy.deepcopy(payload["rules"])
                elif path == "/api/acl/relieve":
                    assert method == "PUT" and set(payload) == {"id"} and payload["id"], "never send the unbounded all sentinel"
                    for row in state["rate"]:
                        if row["id"] in payload["id"]: row["status"] = 0
                else: pytest.fail("Unexpected mutation " + path)
            return httpx.Response(200, json={"data": result, "err": ""})
        params = request.url.params
        if path == "/api/open/system/edition": result = {"version": 2, "effective_edition": state["edition"]}
        elif path == "/api/open/policy":
            assert dict(params) == {"action": "-1", "page": "1", "page_size": "100"}
            result = {"data": state["rules"], "total": state.get("policy_total", len(state["rules"]))}
        elif path == "/api/open/policy/detail": result = next(r for r in state["rules"] if r["id"] == int(params["id"]))
        elif path == "/api/open/cloud/policies": result = state["catalog"]
        elif path == "/api/open/site/0/acl": result = state["cc"]
        elif path == "/api/open/ipgroup": result = {"nodes": [state["group"]], "total": 1}
        elif path == "/api/open/ja4": result = {"id": 81, "name": "JA4 intelligence", "count": 130, "pro": False}
        else:
            kind = "rate" if path.endswith("/acl") else "waiting"
            values = copy.deepcopy(state[kind])
            if "ip" in params: values = [r for r in values if r.get("ip") == params["ip"]]
            if "site" in params: values = [r for r in values if params["site"] in r["site_comment"]]
            if kind == "rate":
                if "begin" in params: values = [r for r in values if r["started"] >= int(params["begin"])]
                if "end" in params: values = [r for r in values if r["started"] <= int(params["end"])]
            else:
                if "started_at_begin" in params: values = [r for r in values if r["started_at"] >= int(params["started_at_begin"])]
                if "started_at_end" in params: values = [r for r in values if r["started_at"] <= int(params["started_at_end"])]
            size = int(params.get("page_size", 20)); offset = (int(params.get("page", 1)) - 1) * size
            result = state["listing"] if state["listing"] is not None else {"data": values[offset:offset + size], "total": len(values)}
        return httpx.Response(200, json={"data": copy.deepcopy(result), "err": ""})

    app = create_app(Settings(PVE, enabled_plugins=("safeline",), safeline_state_file=str(tmp_path / "security.sqlite3")), pve_auth_transport=pve_transport(state),
                     plugin_transports={"safeline": httpx.MockTransport(handler)})
    with TestClient(app) as client: yield client, state


def create_changes(pattern=None, **values):
    return dict(action=1, name="New rule", is_enabled=True, log=False, insert_position="first",
                pattern=pattern or [[condition()]], **values)


@pytest.mark.parametrize("key,editable,removable", [(2, True, True), (1, False, False), (9, False, True)])
def test_summary_capabilities_preserve_native_builtin_and_online_rule_boundaries(security, key, editable, removable):
    client, state = security; headers = configure(client)
    result = client.get(BASE + f"/policy-management/summary?id={key}", headers=headers)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["capabilities"]["can_edit"] is editable and data["capabilities"]["can_edit_pattern"] is editable
    assert data["capabilities"]["can_delete"] is removable and data["capabilities"]["can_switch"] is True
    assert data["values"]["order"] == next(r for r in state["rules"] if r["id"] == key)["order"]
    assert data["preview"] == [[condition()]] and data["options"]["groups"]
    assert not state["writes"] and TOKEN not in result.text


@pytest.mark.parametrize("pattern", [[[condition("req_header", "future_op", [" keep spaces ", ""], "X-A")]],
    [[condition("req_header", "eq", ["Bearer private-credential"], "Authorization")]]])
def test_summary_masks_sensitive_preview_and_locks_unknown_patterns_without_blocking_metadata(security, pattern):
    client, state = security; headers = configure(client); original = state["rules"][0]; original["pattern"] = pattern
    path = "/policy-management/summary?id=2"; response = client.get(BASE + path, headers=headers)
    data = response.json(); assert "pattern" in data["locked"] and "pattern" not in data["values"]
    assert data["capabilities"]["can_edit"] is True and data["capabilities"]["can_edit_pattern"] is False
    assert "private-credential" not in response.text
    body = draft(client, path, {"name": "Renamed"}, headers)
    assert client.post(BASE + path, json=body, headers=headers).json()["outcome"] == "confirmed"
    assert state["writes"][0][2]["pattern"] == pattern


def test_summary_reorders_the_full_target_list_after_a_type_change_and_retains_unedited_fields(security):
    client, state = security; headers = configure(client); original = copy.deepcopy(state["rules"][0])
    path = "/policy-management/summary?id=2"
    body = draft(client, path, {"name": "Moved", "action": 0, "order": 2}, headers)
    result = client.post(BASE + path, json=body, headers=headers)
    assert result.status_code == 200 and result.json()["outcome"] == "confirmed", result.text
    assert [(m, p) for m, p, _ in state["writes"]] == [("PUT", "/api/open/policy"), ("PUT", "/api/open/policy/orders")]
    assert state["writes"][1][2] == {"orders": [{"id": 4, "order": 1}, {"id": 2, "order": 2}]}
    payload = state["writes"][0][2]
    for key in ("pattern", "expire", "level", "log", "is_enabled"):
        assert payload[key] == original[key]
    assert state["rules"][0]["negate"] == original["negate"]
    assert state["rules"][1]["action"] == 1 and state["rules"][1]["order"] == 2


@pytest.mark.parametrize("changes", [{"order": 0}, {"order": True}, {"order": 3}, {"order": "2"}, {"action": 999},
    {"name": ""}, {"is_enabled": False}, {"pattern": []}])
def test_invalid_summary_changes_never_write(security, changes):
    client, state = security; headers = configure(client); path = "/policy-management/summary?id=2"
    result = client.post(BASE + path, json=draft(client, path, changes, headers), headers=headers)
    assert result.status_code == 422, result.text
    assert not state["writes"]


def test_partial_metadata_and_order_write_is_unknown_and_cannot_be_replayed(security):
    client, state = security; headers = configure(client); path = "/policy-management/summary?id=2"
    body = draft(client, path, {"name": "Applied", "order": 2}, headers); state["failure"] = "order-timeout"
    result = client.post(BASE + path, json=body, headers=headers)
    assert result.status_code == 200 and result.json()["outcome"] == "unknown", result.text
    assert state["rules"][0]["name"] == "Applied" and len(state["writes"]) == 2
    assert client.post(BASE + path, json=body, headers=headers).json()["outcome"] == "unknown"
    assert len(state["writes"]) == 2


@pytest.mark.parametrize("key", [1, 2, 9])
def test_summary_switch_is_a_narrow_native_write_and_verified_readback(security, key):
    client, state = security; headers = configure(client); path = f"/policy-management/switch?id={key}"
    original = next(r for r in state["rules"] if r["id"] == key); before = copy.deepcopy(original)
    result = client.post(BASE + path, json=draft(client, path, {"is_enabled": False}, headers), headers=headers)
    assert result.json()["outcome"] == "confirmed", result.text
    assert state["writes"] == [("PUT", "/api/open/policy/switch", {"id": key, "is_enabled": False})]
    assert original == {**before, "is_enabled": False}


@pytest.mark.parametrize("key", [2, 9])
def test_summary_delete_names_the_exact_resource_and_checks_its_absence(security, key):
    client, state = security; headers = configure(client); path = f"/policy-management/delete?id={key}"
    name = next(r for r in state["rules"] if r["id"] == key)["name"]; before = {r["id"] for r in state["rules"]}
    body = draft(client, path, {"confirm_name": name}, headers)
    assert client.post(BASE + path, json=body, headers=headers).json()["outcome"] == "confirmed"
    assert state["writes"] == [("DELETE", "/api/open/policy", {"id": key})]
    assert {r["id"] for r in state["rules"]} == before - {key}


@pytest.mark.parametrize("key,changes", [(1, {"confirm_name": "Rule 2"}), (2, {"confirm_name": "Another"}),
    (2, {"confirm_name": "Rule 2", "id": 3})])
def test_deletion_cannot_remove_builtin_rules_or_a_different_target(security, key, changes):
    client, state = security; headers = configure(client); path = f"/policy-management/delete?id={key}"
    assert client.post(BASE + path, json=draft(client, path, changes, headers), headers=headers).status_code == 422
    assert not state["writes"]


@pytest.mark.parametrize("section,changes", [("delete", {"confirm_name": "Rule 2"}), ("switch", {"is_enabled": False})])
def test_unconfirmed_summary_actions_are_unknown_and_never_retried(security, section, changes):
    client, state = security; headers = configure(client); path = f"/policy-management/{section}?id=2"
    body = draft(client, path, changes, headers); state["failure"] = "mismatch"
    assert client.post(BASE + path, json=body, headers=headers).json()["outcome"] == "unknown"
    assert client.post(BASE + path, json=body, headers=headers).json()["outcome"] == "unknown"
    assert len(state["writes"]) == 1


@pytest.mark.parametrize("kind,expected", [("rate", {"begin": "1790146413", "end": "1790146413"}),
                                            ("waiting", {"started_at_begin": "1790146412", "started_at_end": "1790146413"})])
def test_history_units_and_bounded_public_fields(security, kind, expected):
    client, state = security; headers = configure(client)
    value = client.get(BASE + "/cc/history/" + kind, params={"site": "Admin", "start": 1790146412232, "end": 1790146412232}, headers=headers)
    assert value.status_code == 200, value.text
    assert value.json()["items"][0]["site_name"] == "Admin"
    assert dict(state["waf"][-1].url.params) == {"site": "Admin", "page": "1", "page_size": "20", **expected}
    assert len(state["waf"]) == 1
    assert all(x not in value.text for x in (TOKEN, "private-image", "site_icon", "private"))


@pytest.mark.parametrize("path", ["/cc/history/rate", "/cc/history/waiting", "/cc/configuration", "/cc/relieve",
                                  "/policy-management/create", "/policy-management/edit?id=2", "/policy-management/order?action=1", "/policy-management/subscribe",
                                  "/policy-management/summary?id=2", "/policy-management/switch?id=2", "/policy-management/delete?id=2"])
def test_new_reads_authorize_before_external_requests(security, path):
    client, state = security
    assert client.get(BASE + path).status_code == 401
    assert not state["waf"]


@pytest.mark.parametrize("path", ["/cc/configuration", "/cc/relieve", "/policy-management/create", "/policy-management/edit?id=2",
                                  "/policy-management/order?action=1", "/policy-management/subscribe", "/policy-management/summary?id=2",
                                  "/policy-management/switch?id=2", "/policy-management/delete?id=2"])
def test_every_new_write_requires_the_existing_pve_csrf_boundary(security, path):
    client, state = security; configure(client)
    assert client.post(BASE + path, json={}, headers={"cookie": COOKIE}).status_code == 403
    assert not state["waf"] and not state["writes"]


@pytest.mark.parametrize("path", ["/cc/history/rate?page=0", "/cc/history/rate?page_size=101", "/cc/history/waiting?ip=x",
    "/cc/history/rate?start=2&end=1", "/cc/history/rate?start=1.2", "/cc/history/rate?site=%0A", "/cc/relieve?host=x",
    "/policy-management/create?id=2", "/policy-management/order?action=999", "/policy-management/order?action=1&action=0",
    "/policy-management/edit?id=0", "/cc/configuration?site=12", "/policy-management/summary?id=2&id=3",
    "/policy-management/switch?id=2&action=1", "/policy-management/delete?id=-1"])
def test_invalid_scopes_never_reach_waf(security, path):
    client, state = security; headers = configure(client)
    assert client.get(BASE + path, headers=headers).status_code == 422
    assert not state["waf"]


@pytest.mark.parametrize("listing", [{"data": [], "total": True}, {"data": [{}], "total": 0}, {"data": [], "total": -1},
                                    {"data": [1], "total": 1}, {"nodes": [], "total": 0}])
def test_bad_cc_pages_are_not_rendered_as_empty_success(security, listing):
    client, state = security; headers = configure(client); state["listing"] = listing
    assert client.get(BASE + "/cc/history/rate", headers=headers).status_code == 502


def test_rule_choices_use_real_target_operator_pairs_and_single_ja4_library(security):
    client, state = security; headers = configure(client)
    data = client.get(BASE + "/policy-management/create", headers=headers).json()
    choices = {t["id"]: {op["id"] for op in t["operators"]} for t in data["options"]["targets"]}
    assert choices["method"] == {"eq", "not_eq"}
    assert choices["src_ip"] == {"eq", "not_eq", "match", "cidr", "not_cidr", "in", "not_in", "geo_eq", "geo_not_eq"}
    assert choices["req_header"] == {"eq", "not_eq", "has", "not_has", "re", "exist", "not_exist"}
    assert data["options"]["groups"] == [{"id": 7, "comment": "office"}]
    assert data["options"]["ja4"] == [{"id": 81, "name": "JA4 intelligence"}]
    assert {"CN", "CN-FJ", "US"} <= {g["id"] for g in data["options"]["geo"]}
    assert "10.0.0.0/24" not in json.dumps(data["options"]) and not state["writes"]


@pytest.mark.parametrize("pattern", [[[condition("src_ip", "in", ["7"]), condition("website", "eq", ["12"])]],
    [[condition("method", "eq", ["GET", "POST"])], [condition("ja4", "ja4_in", ["81"])]],
    [[condition("req_header", "exist", [], "User-Agent")]], [[condition("src_ip", "cidr", ["192.0.2.9/24"])]],
    [[condition("res_body", "has", ["error"])]]])
def test_create_posts_typed_and_or_conditions_and_verifies_new_id(security, pattern):
    client, state = security; headers = configure(client)
    changes = create_changes(pattern); body = draft(client, "/policy-management/create", changes, headers)
    result = client.post(BASE + "/policy-management/create", json=body, headers=headers)
    assert result.status_code == 200 and result.json()["outcome"] == "confirmed", result.text
    assert state["writes"][0][0:2] == ("POST", "/api/open/policy")
    assert state["writes"][0][2]["insert_position"] == "first"
    assert state["writes"][0][2]["pattern"][0][0]["v"] == (["192.0.2.0/24"] if pattern[0][0]["op"] == "cidr" else pattern[0][0]["v"])
    before = len(state["waf"])
    assert client.post(BASE + "/policy-management/create", json=body, headers=headers).json()["replayed"] is True
    assert len(state["waf"]) == before and len(state["writes"]) == 1


@pytest.mark.parametrize("pattern", [[[condition("method", "re", ["GET"])]], [[condition("src_ip", "eq", ["bad"])]],
    [[condition("src_ip", "in", ["999"])]], [[condition("website", "eq", ["99"])]], [[condition("ja4", "ja4_in", ["1"])]],
    [[condition("req_header", "exist", ["bad"], "Cookie")]], [[condition("get_param", "eq", ["x"])]],
    [[condition("src_ip", "geo_eq", ["ZZ"])]], [[condition("res_body", "has", ["x"]), condition("post_param", "eq", ["x"], "q")]],
    [[condition("uri", "has", ["x\ny"])]]])
def test_invalid_or_incompatible_new_conditions_never_write(security, pattern):
    client, state = security; headers = configure(client)
    body = draft(client, "/policy-management/create", create_changes(pattern), headers)
    assert client.post(BASE + "/policy-management/create", json=body, headers=headers).status_code == 422
    assert not state["writes"]


@pytest.mark.parametrize("pattern", [[[condition("src_ip", "geo_eq", ["CN-FJ"])]], [[condition("res_body", "re", ["secret"])]], [[condition("res_raw_header", "has", ["x"])]]])
def test_paid_condition_gate_uses_effective_edition(security, pattern):
    client, state = security; headers = configure(client); state["edition"] = 0
    body = draft(client, "/policy-management/create", create_changes(pattern), headers)
    assert client.post(BASE + "/policy-management/create", json=body, headers=headers).status_code == 409
    assert not state["writes"]


def test_edit_preserves_sensitive_pattern_and_untouched_settings_and_can_change_action(security):
    client, state = security; headers = configure(client); original = state["rules"][0]
    original["pattern"] = [[condition("req_header", "eq", ["private-credential"], "Authorization")]]
    path = "/policy-management/edit?id=2"; loaded = client.get(BASE + path, headers=headers)
    assert "pattern" in loaded.json()["locked"] and "private-credential" not in loaded.text
    body = draft(client, path, {"name": "Moved", "action": 0, "insert_position": "last"}, headers)
    original["req_count"] += 1
    result = client.post(BASE + path, json=body, headers=headers)
    assert result.json()["outcome"] == "confirmed"
    payload = state["writes"][0][2]
    assert payload["pattern"] == original["pattern"] and payload["expire"] == 0 and payload["level"] == original["level"]
    assert payload["action"] == 0 and original["negate"] is True
    assert "private-credential" not in result.text and TOKEN not in result.text


def test_configuration_changes_are_rejected_after_a_real_edit_but_not_a_new_hit(security):
    client, state = security; headers = configure(client); path = "/policy-management/order?action=1"
    body = draft(client, path, {"ids": [3, 2]}, headers)
    state["rules"][0]["req_count"] += 500
    assert client.post(BASE + path, json=body, headers=headers).json()["outcome"] == "confirmed"
    body = draft(client, path, {"ids": [2, 3]}, headers); state["rules"][0]["name"] = "Other editor"
    assert client.post(BASE + path, json=body, headers=headers).status_code == 409
    assert len(state["writes"]) == 1


def test_unknown_existing_patterns_are_preserved_when_editing_other_fields(security):
    client, state = security; headers = configure(client); state["rules"][0]["pattern"] = [[condition("future_target", "future_op")]]
    path = "/policy-management/edit?id=2"; loaded = client.get(BASE + path, headers=headers)
    assert "pattern" in loaded.json()["locked"]
    body = draft(client, path, {"name": "Renamed"}, headers)
    assert client.post(BASE + path, json=body, headers=headers).json()["outcome"] == "confirmed"
    assert state["writes"][0][2]["pattern"] == [[condition("future_target", "future_op")]]


def test_incomplete_policy_inventory_cannot_be_used_for_reordering_or_creation(security):
    client, state = security; headers = configure(client); state["policy_total"] = 4
    assert client.get(BASE + "/policy-management/order?action=1", headers=headers).status_code == 409
    assert client.get(BASE + "/policy-management/create", headers=headers).status_code == 409
    assert not state["writes"]


@pytest.mark.parametrize("ids", [[2], [2, 2], [2, 3, 1], [2, 3, 9], [4, 2], [True, 3]])
def test_order_requires_the_complete_custom_action_specific_permutation(security, ids):
    client, state = security; headers = configure(client); path = "/policy-management/order?action=1"
    body = draft(client, path, {"ids": ids}, headers)
    assert client.post(BASE + path, json=body, headers=headers).status_code == 422
    assert not state["writes"]


def test_order_applies_contiguous_positions_and_does_not_move_builtin_or_cloud(security):
    client, state = security; headers = configure(client); path = "/policy-management/order?action=1"
    body = draft(client, path, {"ids": [3, 2]}, headers)
    assert client.post(BASE + path, json=body, headers={"cookie": COOKIE}).status_code == 403
    assert not state["writes"]
    assert client.post(BASE + path, json=body, headers=headers).json()["outcome"] == "confirmed"
    assert state["writes"] == [("PUT", "/api/open/policy/orders", {"orders": [{"id": 3, "order": 1}, {"id": 2, "order": 2}]})]
    assert next(r for r in state["rules"] if r["id"] == 1)["order"] == 0


def test_subscription_catalog_and_write_cover_the_entire_mixed_library(security):
    client, state = security; headers = configure(client); path = "/policy-management/subscribe"
    loaded = client.get(BASE + path, headers=headers)
    catalog = loaded.json()["catalog"]
    assert catalog[0]["black_rule"] == 2 and catalog[0]["white_rule"] == 1 and TOKEN not in loaded.text
    body = draft(client, path, {"id": 80}, headers)
    assert client.post(BASE + path, json=body, headers=headers).json()["outcome"] == "confirmed"
    assert state["writes"] == [("POST", "/api/open/cloud/policies/subscribe", {"id": 80})]
    body = draft(client, path, {"id": 80}, headers)
    assert client.post(BASE + path, json=body, headers=headers).status_code == 422
    assert len(state["writes"]) == 1


def test_cc_threshold_patch_preserves_all_rules_private_conditions_and_inheritance(security):
    client, state = security; headers = configure(client); path = "/cc/configuration"
    before = copy.deepcopy(state["cc"]); loaded = client.get(BASE + path, headers=headers)
    assert "private-condition" not in loaded.text and "conditions:1" in loaded.json()["locked"]
    body = draft(client, path, {"rules": [{"id": 2, "count": 80, "enabled": False}]}, headers)
    assert client.post(BASE + path, json=body, headers=headers).json()["outcome"] == "confirmed"
    payload = state["writes"][0][2]
    expected = before; expected[1].update(count=80, enabled=False)
    assert payload == {"rules": expected}
    assert all(r["use_global"] is True and r["future"] == {"retained": True} for r in state["cc"])


def test_relieve_reviews_matching_active_ids_and_never_sends_an_unbounded_empty_array(security):
    client, state = security; headers = configure(client); path = "/cc/relieve?site=Admin"
    assert client.get(BASE + path, headers=headers).json()["values"]["ids"] == [19, 20]
    body = draft(client, path, {"ids": [19]}, headers)
    assert client.post(BASE + path, json=body, headers=headers).json()["outcome"] == "confirmed"
    assert state["writes"] == [("PUT", "/api/acl/relieve", {"id": [19]})]
    body = draft(client, path, {"ids": []}, headers)
    assert client.post(BASE + path, json=body, headers=headers).status_code == 422
    body = draft(client, path, {"ids": [18]}, headers)
    assert client.post(BASE + path, json=body, headers=headers).status_code == 422
    assert len(state["writes"]) == 1


@pytest.mark.parametrize("values,status", [([{"id": 19, "status": 1}] * 2, 409), ([{"id": n + 1, "status": 1} for n in range(101)], 502)])
def test_relieve_requires_a_bounded_complete_snapshot_without_duplicate_ids(security, values, status):
    client, state = security; headers = configure(client); state["listing"] = {"data": values, "total": len(values)}
    assert client.get(BASE + "/cc/relieve", headers=headers).status_code == status
    assert not state["writes"]


@pytest.mark.parametrize("path,changes", [("/policy-management/create", create_changes()), ("/policy-management/order?action=1", {"ids": [3, 2]}),
    ("/policy-management/subscribe", {"id": 80}), ("/cc/configuration", {"rules": [{"id": 1, "count": 90}]}), ("/cc/relieve", {"ids": [19]})])
@pytest.mark.parametrize("failure,outcome", [("timeout", "unknown"), ("mismatch", "unknown"), ("business", "failed")])
def test_ambiguous_or_rejected_writes_are_recorded_and_never_automatically_replayed(security, path, changes, failure, outcome):
    client, state = security; headers = configure(client); body = draft(client, path, changes, headers); state["failure"] = failure
    response = client.post(BASE + path, json=body, headers=headers)
    assert response.status_code == 200 and response.json()["outcome"] == outcome, response.text
    assert TOKEN not in response.text
    before = len(state["waf"])
    assert client.post(BASE + path, json=body, headers=headers).json()["replayed"] is True
    assert len(state["waf"]) == before and len(state["writes"]) == 1
