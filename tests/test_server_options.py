import asyncio
import json

from fastapi import Request
import httpx
import pytest

from plugins.komodo.guest_links import GuestLinkStore
from plugins.komodo.komodo import KomodoActionError, KomodoClient
from plugins.komodo.server_options import (
    BOOL_FIELDS, CONFIG_FIELDS, GuestBoundOptionsService, NUMBER_FIELDS,
    validate_patch, validate_public_key,
)


ORIGIN, PVE = "http://core:9120", "https://pve.example:8006"
KEY, SECRET, PASSKEY, PRIVATE_KEY = "options-api-key", "options-api-secret", "private-passkey", "private-key-material"
SERVER, OTHER, OPERATION = "a" * 24, "b" * 24, "c" * 24
UUID = "ce11010c-26ad-4d1b-bb2c-f5f51eb52263"
IDENTITY = {"kind": "vm_uuid", "values": [UUID], "strength": "strong"}
GUEST = {"id": "qemu/101", "guest_type": "qemu", "vmid": 101, "node": "pve-a", "name": "vm", "status": "running", "template": False}


def window():
    return {
        "name": "Backup", "description": "Daily backup", "schedule_type": "Daily",
        "day_of_week": "", "date": "", "hour": 5, "minute": 30, "duration_minutes": 60,
        "timezone": "Asia/Shanghai", "enabled": True,
    }


def resource():
    config = {field: True for field in BOOL_FIELDS}
    config.update({field: 99.5 for field in NUMBER_FIELDS})
    config.update(address="wss://periphery.example:8120", external_address="docker.example",
                  region="east", ignore_mounts=["/dev/loop0"], maintenance_windows=[window()],
                  passkey=PASSKEY, links=["https://private.invalid/" + SECRET])
    return {"_id": {"$oid": SERVER}, "name": "docker-host", "template": False, "updated_at": 1792256400123,
            "config": config, "info": {"public_key": "public-spki-base64", "private_key": PRIVATE_KEY}}


def update_response():
    return httpx.Response(200, json={
        "_id": {"$oid": OPERATION}, "operation": "UpdateServerKey", "status": "Complete", "success": True,
        "target": {"type": "Server", "id": SERVER},
        "logs": [{"command": SECRET, "stdout": PRIVATE_KEY}], "other_data": PASSKEY,
    })


def handler_for(raw=None, *, level="Write", ui_disabled=False, state="Ok", submit=None, overrides=None, seen=None):
    raw = raw if raw is not None else resource()
    overrides = overrides or {}

    def upstream(request):
        if seen is not None:
            seen.append(request)
        assert request.headers["x-api-key"] == KEY and request.headers["x-api-secret"] == SECRET
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if request.url.path == "/user":
            assert request.method == "GET"
            return overrides.get("user") or httpx.Response(200, json={"username": "integration", "enabled": True}, headers={
                "set-cookie": "PVEAuthCookie=must-not-replay; Path=/",
            })
        assert request.method == "POST"
        payload = json.loads(request.content)
        variant = payload["type"]
        if request.url.path == "/write":
            if submit:
                return submit(request, payload)
            return httpx.Response(200, json=raw) if variant == "UpdateServer" else update_response()
        assert str(request.url) == ORIGIN + "/read"
        if variant in overrides:
            response = overrides[variant]
            if isinstance(response, Exception):
                raise response
            return response
        if variant == "GetServer":
            assert payload["params"] == {"server": SERVER}
            response = raw
        elif variant == "GetCoreInfo":
            assert payload["params"] == {}
            response = {"ui_write_disabled": ui_disabled, "webhook_base_url": SECRET}
        elif variant == "GetPermission":
            assert payload["params"] == {"target": {"type": "Server", "id": SERVER}}
            response = {"level": level, "specific": []}
        elif variant == "ListServers":
            assert payload["params"] == {"query": {"names": [SERVER], "templates": "Include"}, "limit": 0}
            response = [{"id": SERVER, "name": "docker-host", "template": False, "info": {"state": state, "public_key": "online-key-must-not-be-used"}}]
        else:
            pytest.fail("unapproved RPC")
        return httpx.Response(200, json=response, headers={"set-cookie": "other=must-not-replay; Path=/"})

    return upstream


def client(handler, *, secret=SECRET):
    return KomodoClient(ORIGIN, KEY, secret, transport=httpx.MockTransport(handler))


def snapshot(handler):
    return asyncio.run(client(handler).server_options(SERVER))


def save(handler, *, action="save", version=None, config=None, public_key=None, guard=None):
    if version is None:
        version = snapshot(handler)["version"]
    return asyncio.run(client(handler).update_server_options(SERVER, action=action, base_version=version,
                                                           config=config, public_key=public_key, before_write=guard))


def assert_safe(value):
    serialized = json.dumps(value, allow_nan=False)
    for secret in (KEY, SECRET, PASSKEY, PRIVATE_KEY, "PVEAuthCookie", "private.invalid"):
        assert secret not in serialized


def test_options_read_returns_all_21_real_fields_expected_public_key_and_no_secrets():
    seen = []
    result = snapshot(handler_for(seen=seen))
    assert set(result["config"]) == set(CONFIG_FIELDS)
    assert len(CONFIG_FIELDS) == 21
    assert result["config"]["maintenance_windows"] == [window()]
    assert result["public_key"] == "public-spki-base64"
    assert result["can_write"] is result["can_rotate"] is True
    assert result["server_state"] == "Ok"
    assert len(result["version"]) == 64
    assert len(seen) == 5
    assert all(request.url.path not in {"/write", "/execute"} for request in seen)
    assert_safe(result)


def test_version_changes_for_config_public_key_updated_at_and_server_id():
    original = snapshot(handler_for())["version"]
    for mutate in (
        lambda row: row["config"].update(region="west"),
        lambda row: row["info"].update(public_key="changed-public-key"),
        lambda row: row.update(updated_at=1792256400456),
    ):
        raw = resource()
        mutate(raw)
        assert snapshot(handler_for(raw))["version"] != original
    # A different Server cannot adopt another target's version by returning a
    # byte-for-byte copy of the visible config under the wrong identity.
    raw = resource()
    raw["_id"] = {"$oid": OTHER}
    with pytest.raises(KomodoActionError) as error:
        snapshot(handler_for(raw))
    assert error.value.code == "komodo_server_unavailable"


@pytest.mark.parametrize("level,disabled,state,can_write,can_rotate", [
    ("Read", False, "Ok", False, False), ("Execute", False, "Ok", False, False),
    ("Write", True, "Ok", False, False), ("Write", False, "Disabled", True, False),
    ("Write", False, "NotOk", True, False), ("Write", False, "Ok", True, True),
])
def test_actual_service_permission_and_global_ui_policy_gate_editing_and_rotation(level, disabled, state, can_write, can_rotate):
    result = snapshot(handler_for(level=level, ui_disabled=disabled, state=state))
    assert result["can_write"] is can_write
    assert result["can_rotate"] is can_rotate


def test_missing_stored_field_is_null_instead_of_an_invented_default():
    raw = resource()
    del raw["config"]["cpu_warning"]
    result = snapshot(handler_for(raw))
    assert result["config"]["cpu_warning"] is None


@pytest.mark.parametrize("value", [101.25, -1.0, 0, 1_000_000])
def test_source_threshold_semantics_are_not_restricted_to_percent_ranges(value):
    raw = resource()
    raw["config"].update(cpu_warning=value, cpu_critical=-5)
    result = snapshot(handler_for(raw))
    assert result["config"]["cpu_warning"] == value
    assert result["config"]["cpu_critical"] == -5
    assert validate_patch({"cpu_warning": value}) == {"cpu_warning": value}


@pytest.mark.parametrize("patch", [
    {}, {"passkey": SECRET}, {"private_key": PRIVATE_KEY}, {"links": ["https://private.invalid"]},
    {"enabled": 1}, {"insecure_tls": "true"}, {"cpu_warning": True}, {"cpu_warning": float("nan")},
    {"mem_critical": float("inf")}, {"region": "bad\nregion"}, {"region": "x" * 257},
    {"ignore_mounts": "/dev"}, {"ignore_mounts": [True]}, {"ignore_mounts": ["x"] * 257},
    {"address": "https://user:secret@private.invalid"}, {"external_address": "bad\nhost"},
])
def test_invalid_partial_fields_types_and_hidden_credentials_are_rejected_before_network(patch):
    def unexpected(request):
        pytest.fail("invalid options must not connect")

    with pytest.raises(KomodoActionError) as error:
        save(unexpected, version="1" * 64, config=patch)
    assert error.value.code == "invalid_server_options"


def test_maintenance_windows_roundtrip_all_fields_and_allow_partial_config_arrays():
    weekly = {**window(), "schedule_type": "Weekly", "day_of_week": "Monday"}
    once = {**window(), "schedule_type": "OneTime", "date": "2026-10-18", "timezone": ""}
    assert validate_patch({"maintenance_windows": [weekly, once]}) == {"maintenance_windows": [weekly, once]}
    assert validate_patch({"ignore_mounts": []}) == {"ignore_mounts": []}


@pytest.mark.parametrize("changes", [
    {"name": ""}, {"hour": 24}, {"minute": True}, {"duration_minutes": 0}, {"duration_minutes": 2**32},
    {"enabled": 1}, {"schedule_type": "Other"}, {"schedule_type": []},
    {"schedule_type": "Weekly", "day_of_week": ""}, {"day_of_week": "Friday<script>"},
    {"schedule_type": "OneTime", "date": "2026-02-30"}, {"timezone": "not/a-zone"}, {"secret": SECRET},
])
def test_invalid_maintenance_window_fields_are_rejected(changes):
    with pytest.raises(KomodoActionError):
        validate_patch({"maintenance_windows": [{**window(), **changes}]})


def test_save_sends_only_the_partial_patch_and_discards_secret_full_resource_response():
    seen = []
    result = save(handler_for(seen=seen), config={"region": "west"})
    writes = [request for request in seen if request.url.path == "/write"]
    assert len(writes) == 1
    assert json.loads(writes[0].content) == {"type": "UpdateServer", "params": {"id": SERVER, "config": {"region": "west"}}}
    assert result == {"ok": True, "status": "completed", "operation_id": None, "error_code": None}
    assert_safe(result)


@pytest.mark.parametrize("action,public_key,variant,params", [
    ("public_key", "", "UpdateServerPublicKey", {"server": SERVER, "public_key": ""}),
    ("public_key", "custom-public-key", "UpdateServerPublicKey", {"server": SERVER, "public_key": "custom-public-key"}),
    ("rotate_keys", None, "RotateServerKeys", {"server": SERVER}),
])
def test_public_key_saving_and_rotation_use_native_write_rpc_and_update_server_key_operation(action, public_key, variant, params):
    seen = []
    result = save(handler_for(seen=seen), action=action, public_key=public_key)
    writes = [request for request in seen if request.url.path == "/write"]
    assert len(writes) == 1
    assert json.loads(writes[0].content) == {"type": variant, "params": params}
    assert result == {"ok": True, "status": "completed", "operation_id": OPERATION, "error_code": None}
    assert_safe(result)


@pytest.mark.parametrize("action,config,public_key", [
    ("save", {"region": "west"}, "key"), ("public_key", {"region": "west"}, "key"),
    ("rotate_keys", {"region": "west"}, None), ("rotate_keys", None, "key"),
    ("arbitrary", None, None), ([], None, None),
])
def test_mixed_or_unknown_options_actions_never_connect(action, config, public_key):
    def unexpected(request):
        pytest.fail("invalid action must not connect")

    with pytest.raises(KomodoActionError):
        save(unexpected, action=action, version="1" * 64, config=config, public_key=public_key)


def test_stale_hash_policy_change_or_offline_rotation_prevents_any_write():
    original = snapshot(handler_for())["version"]
    cases = [
        (handler_for(), "0" * 64, "save", {"region": "west"}, "server_options_conflict"),
        (handler_for(level="Read"), original, "save", {"region": "west"}, "server_options_read_only"),
        (handler_for(ui_disabled=True), original, "save", {"region": "west"}, "server_options_read_only"),
        (handler_for(state="Disabled"), original, "rotate_keys", None, "komodo_server_unavailable"),
    ]
    for handler, version, action, patch, code in cases:
        writes = []
        def upstream(request):
            if request.url.path == "/write":
                writes.append(request)
            return handler(request)
        with pytest.raises(KomodoActionError) as error:
            save(upstream, action=action, version=version, config=patch)
        assert error.value.code == code and writes == []


def test_before_write_guard_is_last_and_can_block_a_rebound_guest_after_read_preflight():
    seen = []
    version = snapshot(handler_for())["version"]
    async def reject_rebind():
        assert len(seen) == 5
        raise KomodoActionError("guest_link_revision_conflict", 409)
    with pytest.raises(KomodoActionError) as error:
        save(handler_for(seen=seen), version=version, config={"region": "west"}, guard=reject_rebind)
    assert error.value.code == "guest_link_revision_conflict"
    assert all(request.url.path != "/write" for request in seen)


@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout(SECRET), httpx.Response(408, text=SECRET), httpx.Response(500, text=SECRET),
    httpx.Response(302, headers={"location": "https://private.invalid/" + SECRET}),
    httpx.Response(200, json={"_id": {"$oid": OTHER}, "config": {"passkey": SECRET}}),
])
@pytest.mark.parametrize("action", ["save", "public_key", "rotate_keys"])
def test_uncertain_options_write_is_never_retried_and_does_not_leak_response(failure, action):
    writes = []
    def submit(request, payload):
        writes.append(request)
        if isinstance(failure, Exception):
            raise failure
        return failure
    result = save(handler_for(submit=submit), action=action,
                  config={"region": "west"} if action == "save" else None,
                  public_key="new-key" if action == "public_key" else None)
    assert len(writes) == 1
    assert result == {"ok": False, "status": "outcome_unknown", "operation_id": None, "error_code": "outcome_unknown"}
    assert_safe(result)


def test_key_update_requires_matching_server_and_audit_operation_before_success():
    def submit(request, payload):
        response = update_response().json()
        response["operation"] = "RotateServerKeys"
        return httpx.Response(200, json=response)
    result = save(handler_for(submit=submit), action="rotate_keys")
    assert result["status"] == "outcome_unknown"


def test_disabled_service_identity_never_reads_or_writes():
    seen = []
    with pytest.raises(KomodoActionError) as error:
        snapshot(handler_for(overrides={"user": httpx.Response(200, json={"username": "integration", "enabled": False})}, seen=seen))
    assert error.value.code == "invalid_credentials"
    assert [request.url.path for request in seen] == ["/user"]


def browser():
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"cookie", b"PVEAuthCookie=private-session")]})


class Pve:
    def __init__(self, *, guests=None, identity=None, during_fingerprint=None):
        self.guests = [GUEST] if guests is None else guests
        self.identity = IDENTITY if identity is None else identity
        self.during_fingerprint = during_fingerprint

    async def list_guests(self, request):
        assert request.cookies["PVEAuthCookie"] == "private-session"
        return self.guests

    async def fingerprint(self, request, guest):
        if self.during_fingerprint:
            self.during_fingerprint()
        return self.identity


def bound_service(*, pve=None):
    store = GuestLinkStore(":memory:", komodo_origin=ORIGIN, pve_origin=PVE)
    store.save(SERVER, GUEST, IDENTITY, expected_revision=0, confirmed=True)
    return GuestBoundOptionsService(store, pve or Pve())


def resolve(service, **kwargs):
    return asyncio.run(service.resolve(browser(), "qemu", 101, **kwargs))


def test_guest_options_target_is_resolved_only_from_confirmed_binding_and_fresh_identity():
    service = bound_service()
    result = resolve(service, expected_server_id=SERVER, expected_revision=1)
    assert result == {"server_id": SERVER, "binding_revision": 1, "guest_type": "qemu", "vmid": 101}
    assert "identity" not in result


def test_migration_keeps_the_same_guest_identity_and_ct_uses_exact_saved_mac_fingerprint():
    service = bound_service(pve=Pve(guests=[{**GUEST, "node": "pve-b", "name": "renamed"}]))
    assert resolve(service)["server_id"] == SERVER
    ct = {**GUEST, "id": "lxc/102", "guest_type": "lxc", "vmid": 102}
    identity = {"kind": "ct_mac", "values": ["02:00:00:00:00:01"], "strength": "weak"}
    store = GuestLinkStore(":memory:", komodo_origin=ORIGIN, pve_origin=PVE)
    store.save(SERVER, ct, identity, expected_revision=0, confirmed=True)
    service = GuestBoundOptionsService(store, Pve(guests=[ct], identity=identity))
    assert asyncio.run(service.resolve(browser(), "lxc", 102))["server_id"] == SERVER
    service.pve.identity = {**identity, "values": ["02:00:00:00:00:02"]}
    with pytest.raises(KomodoActionError) as error:
        asyncio.run(service.resolve(browser(), "lxc", 102))
    assert error.value.code == "guest_identity_changed"


@pytest.mark.parametrize("kwargs", [{"expected_server_id": OTHER}, {"expected_revision": 0}, {"expected_revision": True}])
def test_expected_target_and_revision_cannot_select_or_bypass_the_bound_server(kwargs):
    with pytest.raises(KomodoActionError) as error:
        resolve(bound_service(), **kwargs)
    assert error.value.code == "guest_link_revision_conflict"


@pytest.mark.parametrize("pve,code", [
    (Pve(guests=[]), "pve_guest_inaccessible"), (Pve(guests=[{**GUEST, "template": True}]), "pve_guest_inaccessible"),
    (Pve(identity={"kind": "vm_uuid", "values": ["e6e4f50d-5c6f-4e40-8f5f-28c321803887"], "strength": "strong"}), "guest_identity_changed"),
    (Pve(identity={"kind": "vm_uuid", "values": [], "strength": "unavailable"}), "guest_identity_changed"),
])
def test_missing_recreated_and_template_guest_options_are_blocked(pve, code):
    with pytest.raises(KomodoActionError) as error:
        resolve(bound_service(pve=pve))
    assert error.value.code == code


def test_unlink_during_fingerprint_cannot_leave_an_old_write_guard_valid():
    service = bound_service()
    service.pve.during_fingerprint = lambda: service.store.unlink(SERVER, expected_revision=1)
    with pytest.raises(KomodoActionError) as error:
        resolve(service)
    assert error.value.code == "guest_link_revision_conflict"


def test_unbound_guest_never_falls_back_to_a_client_supplied_server_id():
    service = bound_service()
    service.store.unlink(SERVER, expected_revision=1)
    with pytest.raises(KomodoActionError) as error:
        resolve(service, expected_server_id=OTHER, expected_revision=1)
    assert error.value.code == "guest_not_linked"
