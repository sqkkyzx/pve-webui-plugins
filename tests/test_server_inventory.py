"""Selected inventory contracts, target isolation and non-retrying mutations."""

import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from plugins.komodo.actions import KomodoActionError
from plugins.komodo.server_inventory import get_server_inventory, perform_inventory_action


SERVER, OTHER, STACK, UPDATE = "a" * 24, "b" * 24, "c" * 24, "e" * 24
CONTAINER, NETWORK, IMAGE = "d" * 64, "f" * 64, "sha256:" + "1" * 64
ORIGIN, KEY, SECRET = "http://core:9120", "inventory-private-key", "inventory-private-secret"


def responses():
    return {
        "GetServer": {"_id": SERVER, "name": "host", "template": False, "config": {"enabled": True, "passkey": SECRET}},
        "ListServers": [{"id": SERVER, "name": "host", "template": False, "info": {"state": "Ok"}}],
        "GetPermission": {"level": "Write", "specific": ["Attach"]},
        "GetCoreInfo": {"ui_write_disabled": False, "disable_non_admin_create": False},
        "ListContainers": [{"id": CONTAINER, "name": "app", "server_id": SERVER, "state": "running", "status": "up 2 hours",
                            "image": "nginx:latest", "network_mode": "host", "networks": ["host"], "env": [SECRET]}],
        "ListNetworks": [{"id": NETWORK, "name": "app-network", "driver": "bridge", "scope": "local", "attachable": False,
                          "ipam_driver": "default", "created": "2026-10-18T00:00:00Z", "in_use": False, "gateway": SECRET}],
        "ListVolumes": [{"name": "app-data", "driver": "local", "scope": "local", "created": "2026-10-18T00:00:00Z",
                         "in_use": False, "mountpoint": "/private/" + SECRET}],
        "ListImages": [{"id": IMAGE, "name": "nginx:latest", "created": 123, "size": 0, "in_use": False, "tags": [SECRET]}],
        "ListStacks": [{"id": STACK, "name": "app-stack", "template": False, "tags": ["production"],
                        "info": {"server_id": SERVER, "swarm_id": "", "state": "running", "files_on_host": True, "private": SECRET}}],
        "ListDeployments": [{"id": "2" * 24, "name": "app-deployment", "template": False,
                             "info": {"server_id": SERVER, "swarm_id": "", "image": "nginx:latest", "state": "running"}}],
        "ListRepos": [{"id": "3" * 24, "name": "app-repo", "template": False,
                       "info": {"server_id": SERVER, "repo": "org/project", "branch": "main", "state": "Ok", "token": SECRET}}],
        "GetStack": {"_id": STACK, "name": "app-stack", "template": False, "config": {"server_id": SERVER, "swarm_id": "", "environment": SECRET}},
    }


def handler(raw=None, *, seen=None, writes=None, overrides=None, change=None):
    raw = raw or responses()
    overrides = overrides or {}
    seen = seen if seen is not None else []
    writes = writes if writes is not None else []

    def upstream(request):
        assert request.headers["x-api-key"] == KEY and request.headers["x-api-secret"] == SECRET
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert str(request.url).startswith(ORIGIN + "/")
        if request.url.path == "/user":
            return overrides.get("user") or httpx.Response(200, json={"enabled": True, "username": "integration"},
                headers={"set-cookie": "private-cookie=must-not-replay; Path=/"})
        body = json.loads(request.content)
        seen.append(body)
        variant = body["type"]
        if request.url.path == "/read":
            if variant in {"ListContainers", "ListNetworks", "ListImages", "ListVolumes", "GetServer"}:
                assert body["params"] == {"server": SERVER}
            if variant in {"ListStacks", "ListDeployments", "ListRepos"}:
                assert body["params"] == {"query": {"templates": "Exclude", "specific": {"server_ids": [SERVER]}}, "limit": 0}
            if change:
                change(raw, seen, writes, body)
            if variant in overrides:
                value = overrides[variant]
                if isinstance(value, Exception):
                    raise value
                return value
            return httpx.Response(200, json=raw[variant], headers={"set-cookie": "another=must-not-replay; Path=/"})
        assert request.url.path in {"/execute", "/write"}
        writes.append(body)
        if "write" in overrides:
            value = overrides["write"]
            if isinstance(value, Exception):
                raise value
            return value
        if variant == "CreateStack":
            return httpx.Response(200, json={"_id": STACK, "name": body["params"]["name"], "config": {"server_id": SERVER}, "env": SECRET})
        if variant == "DeleteStack":
            return httpx.Response(200, json={"_id": STACK, "config": {"env": SECRET}})
        if variant == "CheckStackForUpdate":
            return httpx.Response(200, json={"stack": STACK, "services": [], "secret": SECRET})
        return httpx.Response(200, json={"_id": UPDATE, "operation": variant, "target": {
            "type": "Stack" if variant.endswith("Stack") else "Server", "id": STACK if variant.endswith("Stack") else SERVER},
            "success": True, "status": "Queued", "logs": [SECRET]})
    return upstream


def run(upstream, *, kind="docker", section=None, action=None, targets=None, name=None, guard=None):
    options = {"origin": ORIGIN, "key": KEY, "secret": SECRET, "verify": True, "transport": httpx.MockTransport(upstream)}
    if action:
        return asyncio.run(perform_inventory_action(SERVER, section=section, action=action, targets=targets or [], name=name, before_write=guard, **options))
    return asyncio.run(get_server_inventory(SERVER, kind=kind, **options))


def target(upstream, section, *, kind="docker"):
    row = run(upstream, kind=kind)[section][0]
    return {"id": row["id"], "version": row["version"]}


def test_inventory_is_four_tables_and_discards_config_credentials_mountpoints_and_tags():
    result = run(handler())
    assert result["server_id"] == SERVER and result["can_execute"] and result["can_write"]
    assert result["containers"][0]["allowed_actions"] == ["restart", "pause", "stop", "destroy"]
    assert result["networks"][0]["allowed_actions"] == result["volumes"][0]["allowed_actions"] == result["images"][0]["allowed_actions"] == ["delete"]
    assert result["images"][0]["size"] == 0
    encoded = json.dumps(result)
    assert SECRET not in encoded and KEY not in encoded and "mountpoint" not in encoded and "passkey" not in encoded


def test_resource_lists_are_server_filtered_and_only_return_requested_safe_metadata():
    raw = responses()
    other = deepcopy(raw["ListStacks"][0]); other["id"] = OTHER; other["info"]["server_id"] = OTHER
    template = deepcopy(raw["ListStacks"][0]); template["id"] = "4" * 24; template["template"] = True
    swarm = deepcopy(raw["ListStacks"][0]); swarm["id"] = "5" * 24; swarm["info"]["swarm_id"] = OTHER
    raw["ListStacks"] += [other, template, swarm]
    result = run(handler(raw), kind="resources")
    assert [row["id"] for row in result["stacks"]] == [STACK]
    assert result["stacks"][0]["source"] == "host" and result["can_create_stack"]
    assert result["stacks"][0]["allowed_actions"] == ["check_update", "pull", "deploy", "restart", "stop", "destroy", "delete"]
    assert result["deployments"][0]["allowed_actions"] == result["repos"][0]["allowed_actions"] == []
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("name", ["bridge", "none", "host"])
def test_builtin_networks_cannot_be_deleted_even_when_unused(name):
    raw = responses(); raw["ListNetworks"][0]["name"] = name
    upstream = handler(raw)
    row = run(upstream)["networks"][0]
    assert row["system"] is row["builtin"] is True and row["allowed_actions"] == []
    with pytest.raises(KomodoActionError, match="inventory_action_forbidden"):
        run(upstream, section="networks", action="delete", targets=[{"id": row["id"], "version": row["version"]}])


@pytest.mark.parametrize("section", ["networks", "volumes", "images"])
@pytest.mark.parametrize("in_use", [True, None, "false"])
def test_used_or_unknown_usage_never_enables_delete(section, in_use):
    raw = responses(); raw[{"networks": "ListNetworks", "volumes": "ListVolumes", "images": "ListImages"}[section]][0]["in_use"] = in_use
    assert run(handler(raw))[section][0]["allowed_actions"] == []


@pytest.mark.parametrize("section,variant,identifier", [("networks", "DeleteNetwork", NETWORK), ("volumes", "DeleteVolume", "app-data"), ("images", "DeleteImage", IMAGE)])
def test_delete_exact_identity_and_sanitize_update_receipt(section, variant, identifier):
    writes = []; upstream = handler(writes=writes)
    result = run(upstream, section=section, action="delete", targets=[target(upstream, section)])
    assert writes == [{"type": variant, "params": {"server": SERVER, "name": identifier}}]
    assert result["results"][0]["status"] == "submitted" and result["results"][0]["id"] == identifier
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("state,action,variant", [("running", "restart", "RestartContainer"), ("running", "pause", "PauseContainer"),
    ("running", "stop", "StopContainer"), ("paused", "unpause", "UnpauseContainer"), ("exited", "start", "StartContainer"), ("created", "destroy", "DestroyContainer")])
def test_container_actions_use_full_id_and_current_state(state, action, variant):
    raw = responses(); raw["ListContainers"][0]["state"] = state
    writes = []; upstream = handler(raw, writes=writes)
    result = run(upstream, section="containers", action=action, targets=[target(upstream, "containers")])
    assert writes == [{"type": variant, "params": {"server": SERVER, "container": CONTAINER}}]
    assert result["results"][0]["ok"] and result["results"][0]["id"] == CONTAINER


@pytest.mark.parametrize("change", ["state", "id", "name", "offline", "permission"])
def test_pending_container_target_invalidated_before_dispatch(change):
    raw = responses(); writes = []; upstream = handler(raw, writes=writes)
    selected = target(upstream, "containers")
    if change == "offline": raw["ListServers"][0]["info"]["state"] = "NotOk"
    elif change == "permission": raw["GetPermission"]["level"] = "Read"
    else: raw["ListContainers"][0][change] = {"state": "exited", "id": "2" * 64, "name": "replacement"}[change]
    with pytest.raises(KomodoActionError):
        run(upstream, section="containers", action="restart", targets=[selected])
    assert writes == []


@pytest.mark.parametrize("action,variant", [("check_update", "CheckStackForUpdate"), ("pull", "PullStack"), ("deploy", "DeployStack"),
    ("restart", "RestartStack"), ("stop", "StopStack"), ("destroy", "DestroyStack"), ("delete", "DeleteStack")])
def test_stack_actions_current_binding_and_fixed_rpc(action, variant):
    writes = []; upstream = handler(writes=writes)
    result = run(upstream, section="stacks", action=action, targets=[target(upstream, "stacks", kind="resources")])
    assert writes[0]["type"] == variant
    assert writes[0]["params"].get("stack", writes[0]["params"].get("id")) == STACK
    assert result["results"][0]["ok"]


def test_stale_stack_list_cache_cannot_authorize_stack_moved_to_other_server():
    raw = responses()
    writes = []; upstream = handler(raw, writes=writes)
    selected = target(upstream, "stacks", kind="resources")
    raw["GetStack"]["config"]["server_id"] = OTHER
    with pytest.raises(KomodoActionError, match="inventory_resource_changed"):
        run(upstream, section="stacks", action="deploy", targets=[selected])
    assert not writes


@pytest.mark.parametrize("field,value", [("project_name", "different-project"), ("run_directory", "/different/path"),
    ("file_paths", ["different.yaml"]), ("file_contents", "services: {}")])
def test_stack_private_config_change_invalidates_confirmation_without_exposing_config(field, value):
    raw = responses(); writes = []; upstream = handler(raw, writes=writes)
    opened = run(upstream, kind="resources")
    row = opened["stacks"][0]
    assert "config" not in row and SECRET not in json.dumps(opened)
    selected = {"id": row["id"], "version": row["version"]}
    raw["GetStack"]["config"][field] = value
    with pytest.raises(KomodoActionError, match="inventory_version_conflict"):
        run(upstream, section="stacks", action="destroy", targets=[selected])
    assert not writes


def test_all_selected_stacks_live_binding_checked_before_first_write_even_with_stale_list_cache():
    raw = responses()
    second = deepcopy(raw["ListStacks"][0]); second["id"] = OTHER; second["name"] = "second-stack"
    raw["ListStacks"].append(second)
    state = {"moved": False}
    def change(raw, seen, writes, body):
        if body["type"] == "GetStack":
            second_selected = body["params"]["stack"] == OTHER
            raw["GetStack"] = {"_id": OTHER if second_selected else STACK, "name": "second-stack" if second_selected else "app-stack",
                "template": False, "config": {"server_id": "9" * 24 if state["moved"] and second_selected else SERVER, "swarm_id": ""}}
    writes = []; upstream = handler(raw, writes=writes, change=change)
    rows = run(upstream, kind="resources")["stacks"]
    selected = [{"id": row["id"], "version": row["version"]} for row in rows]
    state["moved"] = True
    with pytest.raises(KomodoActionError, match="inventory_resource_changed"):
        run(upstream, section="stacks", action="destroy", targets=selected)
    assert not writes


def test_stack_config_is_checked_again_after_initial_whole_batch_preflight():
    raw = responses(); state = {"stack_reads": 0}
    def change(raw, seen, writes, body):
        if body["type"] == "GetStack":
            state["stack_reads"] += 1
            if state["stack_reads"] == 3:
                raw["GetStack"]["config"]["run_directory"] = "/changed-after-preflight"
    writes = []; upstream = handler(raw, writes=writes, change=change)
    selected = target(upstream, "stacks", kind="resources")
    with pytest.raises(KomodoActionError, match="inventory_version_conflict"):
        run(upstream, section="stacks", action="destroy", targets=[selected])
    assert state["stack_reads"] == 3 and not writes


@pytest.mark.parametrize("created", [None, "", "invalid-date", "2026-10-18T00:00:00", "0001-01-01T00:00:00Z"])
def test_volume_without_valid_creation_fingerprint_cannot_be_selected_for_delete(created):
    raw = responses(); raw["ListVolumes"][0]["created"] = created
    upstream = handler(raw)
    row = run(upstream)["volumes"][0]
    assert row["created"] is None and row["allowed_actions"] == []
    with pytest.raises(KomodoActionError, match="inventory_action_forbidden"):
        run(upstream, section="volumes", action="delete", targets=[{"id": row["id"], "version": row["version"]}])


def test_all_selected_versions_are_checked_before_first_write():
    writes = []; upstream = handler(writes=writes)
    selected = target(upstream, "containers")
    with pytest.raises(KomodoActionError, match="inventory_version_conflict"):
        run(upstream, section="containers", action="restart", targets=[selected, {"id": "9" * 64, "version": "a" * 64}])
    assert not writes


def test_ambiguous_submission_is_not_retried_or_followed_by_second_selected_write():
    raw = responses(); second = deepcopy(raw["ListContainers"][0]); second["id"] = "9" * 64; second["name"] = "second"
    raw["ListContainers"].append(second)
    writes = []; upstream = handler(raw, writes=writes, overrides={"write": httpx.ReadTimeout(SECRET)})
    rows = run(upstream)["containers"]
    result = run(upstream, section="containers", action="restart", targets=[{"id": row["id"], "version": row["version"]} for row in rows])
    assert len(writes) == 1 and [row["status"] for row in result["results"]] == ["outcome_unknown", "not_submitted"]
    assert SECRET not in json.dumps(result)


def test_binding_guard_runs_immediately_before_write_and_can_abort():
    writes = []; upstream = handler(writes=writes)
    async def guard():
        raise KomodoActionError("binding_changed", 409)
    with pytest.raises(KomodoActionError, match="binding_changed"):
        run(upstream, section="containers", action="restart", targets=[target(upstream, "containers")], guard=guard)
    assert not writes


def test_later_binding_change_preserves_earlier_submitted_batch_receipt():
    raw = responses(); second = deepcopy(raw["ListContainers"][0]); second["id"] = "9" * 64; second["name"] = "second"
    raw["ListContainers"].append(second)
    writes = []; upstream = handler(raw, writes=writes)
    rows = run(upstream)["containers"]
    async def guard():
        if writes:
            raise KomodoActionError("binding_changed", 409)
    result = run(upstream, section="containers", action="restart", targets=[{"id": row["id"], "version": row["version"]} for row in rows], guard=guard)
    assert len(writes) == 1
    assert [row["status"] for row in result["results"]] == ["submitted", "not_submitted"]
    assert result["results"][1]["error_code"] == "binding_changed"


def test_execute_permission_can_check_stack_update_without_configuration_write_permission():
    raw = responses(); raw["GetPermission"]["level"] = "Execute"
    upstream = handler(raw)
    row = run(upstream, kind="resources")["stacks"][0]
    assert "check_update" in row["allowed_actions"] and "delete" not in row["allowed_actions"]
    assert run(upstream, section="stacks", action="check_update", targets=[{"id": row["id"], "version": row["version"]}])["results"][0]["ok"]


def test_create_stack_sets_only_name_and_guest_server_and_discards_raw_config():
    writes = []; upstream = handler(writes=writes)
    result = run(upstream, section="stacks", action="create_stack", name="new-stack")
    assert writes == [{"type": "CreateStack", "params": {"name": "new-stack", "config": {"server_id": SERVER}}}]
    assert result["results"][0]["status"] == "completed" and result["results"][0]["id"] == STACK
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("field,value", [("specific", []), ("create_policy", True), ("ui_write_disabled", True)])
def test_create_stack_requires_attach_permission_and_global_creation_policy(field, value):
    raw = responses()
    if field == "specific": raw["GetPermission"][field] = value
    else: raw["GetCoreInfo"]["disable_non_admin_create" if field == "create_policy" else field] = value
    upstream = handler(raw)
    assert run(upstream, kind="resources")["can_create_stack"] is False
    with pytest.raises(KomodoActionError, match="inventory_action_forbidden"):
        run(upstream, section="stacks", action="create_stack", name="new-stack")


def test_one_failed_inventory_section_does_not_hide_other_tables():
    result = run(handler(overrides={"ListVolumes": httpx.Response(403, text=SECRET)}))
    assert result["volumes"] == [] and result["errors"] == ["volumes_permission_denied"]
    assert result["containers"] and result["networks"] and result["images"]


def test_invalid_credentials_fail_closed_without_any_write_or_private_output():
    with pytest.raises(KomodoActionError, match="invalid_credentials"):
        run(handler(overrides={"user": httpx.Response(401, text=SECRET)}))


def test_malformed_states_are_unknown_display_values_with_no_container_actions():
    raw = responses(); raw["ListContainers"][0]["state"] = {"nested": SECRET}
    result = run(handler(raw))
    assert result["containers"][0]["state"] == "unknown" and result["containers"][0]["allowed_actions"] == []
    raw["ListServers"][0]["info"]["state"] = {"nested": "private-other-value"}
    result = run(handler(raw))
    assert result["server_state"] == "unknown" and result["containers"] == []
    assert "private-other-value" not in json.dumps(result)
