import asyncio
import json
import sqlite3
import uuid

from fastapi import Request
import httpx
import pytest

from plugins.komodo.guest_links import GuestLinkError, GuestLinkStore, PveGuestClient, assess_link


SERVER = "a" * 24
OTHER = "b" * 24
PVE = "https://pve.example:8006"
KOMODO = "http://core:9120"
VM_UUID = "ce11010c-26ad-4d1b-bb2c-f5f51eb52263"
IDENTITY = {"kind": "vm_uuid", "values": [VM_UUID], "strength": "strong"}
CT_IDENTITY = {"kind": "ct_mac", "values": ["02:00:00:00:00:01"], "strength": "weak"}
GUEST = {"id": "qemu/101", "guest_type": "qemu", "vmid": 101, "node": "pve-a", "name": "first", "status": "running", "template": False}
CT = {**GUEST, "id": "lxc/102", "guest_type": "lxc", "vmid": 102}


def store(path=":memory:", *, pve=PVE, origin=KOMODO):
    return GuestLinkStore(str(path), komodo_origin=origin, pve_origin=pve)


def browser(cookie="original-private-session"):
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [
        (b"cookie", f"PVEAuthCookie={cookie}; other=private-other-cookie".encode()),
        (b"authorization", b"Bearer private-authorization"),
    ]})


def client(handler):
    return PveGuestClient(PVE, transport=httpx.MockTransport(handler))


def fails(code, operation, status=503):
    with pytest.raises(GuestLinkError) as error:
        operation()
    assert error.value.code == code
    assert error.value.status_code == status
    assert str(error.value) == code
    return error.value


def test_namespace_links_and_revisions_survive_restart(tmp_path):
    path = tmp_path / "guest-links.sqlite3"
    first = store(path)
    namespace = first.cluster_id()
    assert uuid.UUID(namespace).version == 4
    link = first.save(SERVER, GUEST, IDENTITY, expected_revision=0)
    assert link["cluster_id"] == namespace
    assert link["revision"] == 1
    first.close()
    reopened = store(path)
    assert reopened.cluster_id() == namespace
    assert reopened.get(SERVER) == link
    assert reopened.list() == [link]


def test_pve_origin_is_pinned_and_database_is_not_adopted_by_another_cluster(tmp_path):
    path = tmp_path / "links.sqlite3"
    initial = store(path)
    link = initial.save(SERVER, GUEST, IDENTITY, expected_revision=0)
    initial.close()
    for method in ("get", "list", "cluster_id", "revision", "unlink", "save"):
        other = store(path, pve="https://other-pve.example:8006")
        operation = {
            "get": lambda: other.get(SERVER), "list": other.list,
            "cluster_id": other.cluster_id, "revision": lambda: other.revision(SERVER),
            "unlink": lambda: other.unlink(SERVER, expected_revision=1),
            "save": lambda: other.save(SERVER, GUEST, IDENTITY, expected_revision=1),
        }[method]
        fails("pve_instance_mismatch", operation)
    assert store(path).get(SERVER) == link


def test_canonical_origins_do_not_create_false_pin_or_scope_conflicts(tmp_path):
    path = tmp_path / "links.sqlite3"
    first = store(path, pve="https://PVE.EXAMPLE/", origin="https://CORE.EXAMPLE:443/")
    link = first.save(SERVER, GUEST, IDENTITY, expected_revision=0)
    first.close()
    assert store(path, pve="https://pve.example:443", origin="https://core.example").get(SERVER) == link


def test_fixed_komodo_origin_namespaces_server_ids(tmp_path):
    path = tmp_path / "links.sqlite3"
    original = store(path)
    original.save(SERVER, GUEST, IDENTITY, expected_revision=0)
    another = store(path, origin="http://another-core:9120")
    assert another.get(SERVER) is None
    assert another.cluster_id() == original.cluster_id()
    assert another.save(SERVER, GUEST, IDENTITY, expected_revision=0)["revision"] == 1


def test_rebind_requires_explicit_confirmation_and_current_revision():
    links = store()
    original = links.save(SERVER, GUEST, IDENTITY, expected_revision=0)
    changed = {**GUEST, "vmid": 202}
    fails("guest_link_confirmation_required", lambda: links.save(SERVER, changed, IDENTITY, expected_revision=1), 409)
    fails("guest_link_revision_conflict", lambda: links.save(SERVER, changed, IDENTITY, expected_revision=0, confirmed=True), 409)
    assert links.get(SERVER) == original
    assert links.save(SERVER, changed, IDENTITY, expected_revision=1, confirmed=True)["vmid"] == 202


def test_same_guest_with_changed_identity_also_requires_confirmation():
    links = store()
    links.save(SERVER, GUEST, IDENTITY, expected_revision=0)
    replacement = {**IDENTITY, "values": [str(uuid.uuid4())]}
    fails("guest_link_confirmation_required", lambda: links.save(SERVER, GUEST, replacement, expected_revision=1), 409)
    assert links.save(SERVER, GUEST, replacement, expected_revision=1, confirmed=True)["identity"] == replacement


def test_target_owned_by_another_server_cannot_be_transferred_even_when_confirmed():
    links = store()
    links.save(SERVER, GUEST, IDENTITY, expected_revision=0)
    fails("pve_guest_already_linked", lambda: links.save(OTHER, GUEST, IDENTITY, expected_revision=0, confirmed=True), 409)
    assert links.get(OTHER) is None
    assert links.get(SERVER)["revision"] == 1


def test_unlink_keeps_revision_tombstone_and_prevents_stale_confirmation_aba(tmp_path):
    path = tmp_path / "links.sqlite3"
    links = store(path)
    links.save(SERVER, GUEST, IDENTITY, expected_revision=0)
    removed = links.unlink(SERVER, expected_revision=1)
    assert removed == {"server_id": SERVER, "cluster_id": links.cluster_id(), "revision": 2, "active": False}
    assert links.get(SERVER) is None
    assert links.list() == []
    assert links.get(SERVER, include_inactive=True) == removed
    links.close()
    reopened = store(path)
    assert reopened.revision(SERVER) == 2
    fails("guest_link_revision_conflict", lambda: reopened.save(SERVER, GUEST, IDENTITY, expected_revision=0, confirmed=True), 409)
    assert reopened.save(SERVER, GUEST, IDENTITY, expected_revision=2)["revision"] == 3
    fails("guest_link_revision_conflict", lambda: reopened.unlink(SERVER, expected_revision=1), 409)


def test_unlink_frees_target_without_removing_other_associations():
    links = store()
    links.save(SERVER, GUEST, IDENTITY, expected_revision=0)
    links.unlink(SERVER, expected_revision=1)
    assert links.save(OTHER, GUEST, IDENTITY, expected_revision=0)["server_id"] == OTHER
    assert links.unlink(SERVER, expected_revision=2)["revision"] == 2


@pytest.mark.parametrize("revision", [-1, True, None, "0", 1.0])
def test_revision_is_strict_nonnegative_integer(revision):
    links = store()
    fails("invalid_guest_link_revision", lambda: links.save(SERVER, GUEST, IDENTITY, expected_revision=revision), 422)
    fails("invalid_guest_link_revision", lambda: links.unlink(SERVER, expected_revision=revision), 422)


def test_unavailable_persistent_store_never_falls_back_to_memory(tmp_path):
    links = store(tmp_path / "missing" / "links.sqlite3")
    for operation in (links.cluster_id, links.list, lambda: links.get(SERVER), lambda: links.save(SERVER, GUEST, IDENTITY, expected_revision=0)):
        error = fails("guest_link_store_unavailable", operation)
        assert str(tmp_path) not in repr(error)
    assert links._connection is None


@pytest.mark.parametrize("path", ["", "  "])
def test_empty_sqlite_path_cannot_silently_create_an_ephemeral_database(path):
    fails("guest_link_store_unavailable", store(path).cluster_id)


def test_corrupt_persisted_identity_is_not_disclosed(tmp_path):
    path = tmp_path / "links.sqlite3"
    links = store(path)
    links.save(SERVER, GUEST, IDENTITY, expected_revision=0)
    links.close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE guest_links SET identity=?", ('{"password":"private-database-secret"}',))
    reopened = store(path)
    fails("guest_link_store_unavailable", lambda: reopened.get(SERVER))
    fails("guest_link_store_unavailable", reopened.list)


def test_missing_identity_cannot_be_newly_linked():
    fails("guest_identity_unavailable", lambda: store().save(SERVER, GUEST, {"kind": "vm_uuid", "values": [], "strength": "unavailable"}, expected_revision=0), 409)


@pytest.mark.parametrize("mac", ["00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "01:00:5e:01:02:03"])
def test_store_does_not_accept_unusable_ct_mac_even_from_a_trusted_caller(mac):
    identity = {**CT_IDENTITY, "values": [mac]}
    fails("invalid_guest_identity", lambda: store().save(SERVER, CT, identity, expected_revision=0), 422)


def test_store_does_not_accept_nil_vm_uuid_even_from_a_trusted_caller():
    identity = {**IDENTITY, "values": ["00000000-0000-0000-0000-000000000000"]}
    fails("invalid_guest_identity", lambda: store().save(SERVER, GUEST, identity, expected_revision=0), 422)


def test_migration_rename_and_power_state_changes_preserve_identity_and_current_target():
    link = store().save(SERVER, GUEST, IDENTITY, expected_revision=0)
    moved = {**GUEST, "node": "pve-b", "name": "renamed", "status": "stopped"}
    resolved = assess_link(link, [moved], IDENTITY)
    assert resolved["status"] == "linked"
    assert resolved["can_navigate"] is True
    assert resolved["node"] == "pve-b"
    assert resolved["name"] == "renamed"
    assert resolved["guest_status"] == "stopped"


def test_changed_vm_uuid_for_reused_vmid_requires_reconfirmation_without_rewriting_binding():
    links = store()
    link = links.save(SERVER, GUEST, IDENTITY, expected_revision=0)
    changed = {**IDENTITY, "values": [str(uuid.uuid4())]}
    result = assess_link(link, [GUEST], changed)
    assert result["status"] == "needs_confirmation"
    assert result["reason"] == "identity_changed"
    assert result["can_navigate"] is False
    assert links.get(SERVER) == link
    assert "values" not in json.dumps(result)


def test_ct_mac_set_is_sorted_weak_identity_and_changed_set_blocks_navigation():
    links = store()
    identity = {**CT_IDENTITY, "values": ["02:00:00:00:00:02", "02:00:00:00:00:01", "02:00:00:00:00:01"]}
    link = links.save(SERVER, CT, identity, expected_revision=0)
    assert link["identity"]["values"] == ["02:00:00:00:00:01", "02:00:00:00:00:02"]
    good = assess_link(link, [CT], identity)
    assert good["identity_strength"] == "weak"
    assert good["can_navigate"] is True
    assert assess_link(link, [CT], CT_IDENTITY)["status"] == "needs_confirmation"


def test_visibility_and_temporary_failure_do_not_delete_or_claim_object_deleted():
    links = store()
    link = links.save(SERVER, GUEST, IDENTITY, expected_revision=0)
    missing = assess_link(link, [], IDENTITY)
    assert missing["status"] == "missing"
    assert missing["reason"] == "missing_or_inaccessible"
    assert missing["node"] is None
    assert assess_link(link, None)["status"] == "unavailable"
    assert assess_link(link, [GUEST], unavailable=True)["status"] == "unavailable"
    assert assess_link(link, [GUEST])["status"] == "unavailable"
    assert links.get(SERVER) == link


def test_disappearing_identity_blocks_old_link_without_guessing():
    link = store().save(SERVER, GUEST, IDENTITY, expected_revision=0)
    result = assess_link(link, [GUEST], {"kind": "vm_uuid", "values": [], "strength": "unavailable"})
    assert result["status"] == "needs_confirmation"
    assert result["can_navigate"] is False


def test_discovery_only_uses_read_requests_and_allowlisted_dto_with_original_cookie():
    seen = []

    def upstream(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.headers["cookie"] == "PVEAuthCookie=original-private-session"
        assert "authorization" not in request.headers
        assert "x-api-key" not in request.headers
        assert str(request.url) == PVE + "/api2/json/cluster/resources?type=vm"
        return httpx.Response(200, json={"data": [{
            "type": "qemu", "vmid": 101, "node": "pve-a", "name": "first", "status": "running",
            "template": 0, "id": "not-trusted", "password": "private-secret", "diskread": 1234,
        }]}, headers={"set-cookie": "PVEAuthCookie=poisoned; Path=/"})

    result = asyncio.run(client(upstream).list_guests(browser()))
    assert result == [GUEST]
    assert "private" not in json.dumps(result)
    assert len(seen) == 1


def test_cookie_state_never_crosses_requests_or_visitors():
    seen = []

    def upstream(request):
        seen.append(request.headers["cookie"])
        return httpx.Response(200, json={"data": []}, headers={"set-cookie": "PVEAuthCookie=poisoned; Path=/"})

    async def read():
        session = client(upstream)
        await session.list_guests(browser("first-private-session"))
        await session.list_guests(browser("second-private-session"))
        await session.list_guests(browser("first-private-session"))

    asyncio.run(read())
    assert seen == ["PVEAuthCookie=first-private-session", "PVEAuthCookie=second-private-session", "PVEAuthCookie=first-private-session"]


def test_vm_config_does_not_disclose_secrets_or_unstable_digest():
    def upstream(request):
        assert request.method == "GET"
        assert str(request.url) == PVE + "/api2/json/nodes/pve-a/qemu/101/config"
        return httpx.Response(200, json={"data": {
            "smbios1": f"uuid={VM_UUID.upper()},manufacturer=private-manufacturer",
            "cipassword": "private-password", "sshkeys": "private-ssh-keys",
            "args": "private-extra-args", "digest": "private-digest", "meta": "ctime=1234",
        }})

    identity = asyncio.run(client(upstream).fingerprint(browser(), GUEST))
    assert identity == IDENTITY
    assert "private" not in json.dumps(identity)
    assert "ctime" not in json.dumps(identity)


def test_ct_config_extracts_only_valid_unicast_mac_set():
    data = {
        "net0": "name=eth0,bridge=vmbr0,hwaddr=02:AA:BB:CC:DD:01,ip=10.0.0.1/24",
        "net1": "hwaddr=02:aa:bb:cc:dd:01,name=eth1", "net2": "hwaddr=02:aa:bb:cc:dd:02",
        "net3": "hwaddr=00:00:00:00:00:00", "net4": "hwaddr=ff:ff:ff:ff:ff:ff",
        "net5": "hwaddr=01:00:5e:01:02:03", "net6": "hwaddr=malformed",
        "password": "private-password", "description": "hwaddr=02:00:00:00:00:99",
        "net7": "hwaddr=02:00:00:00:00:03,hwaddr=02:00:00:00:00:04",
    }
    identity = asyncio.run(client(lambda request: httpx.Response(200, json={"data": data})).fingerprint(browser(), CT))
    assert identity == {"kind": "ct_mac", "values": ["02:aa:bb:cc:dd:01", "02:aa:bb:cc:dd:02"], "strength": "weak"}
    assert "private" not in json.dumps(identity)


@pytest.mark.parametrize("smbios", [None, "", "uuid=not-valid", "uuid=00000000-0000-0000-0000-000000000000", f"uuid={VM_UUID},uuid={VM_UUID}", [VM_UUID]])
def test_absent_invalid_or_ambiguous_vm_uuid_is_unavailable(smbios):
    result = asyncio.run(client(lambda req: httpx.Response(200, json={"data": {"smbios1": smbios}})).fingerprint(browser(), GUEST))
    assert result == {"kind": "vm_uuid", "values": [], "strength": "unavailable"}


@pytest.mark.parametrize("node", ["../other", "pve-a/qemu/101/../../", "pve-a?x=1", "pve-a#fragment", "pve_a", "-pve-a", "pve-a\n", "a" * 65, None, []])
def test_config_path_node_is_validated_before_any_network_request(node):
    def unexpected(request):
        pytest.fail("invalid node must not cause a request")
    fails("invalid_pve_guest", lambda: asyncio.run(client(unexpected).fingerprint(browser(), {**GUEST, "node": node})), 422)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_permission_missing_or_session_errors_are_sanitized_and_not_guessed_as_deleted(status):
    error = fails("pve_guest_inaccessible", lambda: asyncio.run(client(lambda req: httpx.Response(status, text="private-password")).list_guests(browser())), 409)
    assert "private" not in repr(error)


@pytest.mark.parametrize("status", [301, 302, 307, 400, 429, 500, 503])
def test_unexpected_http_and_redirects_fail_without_following_or_disclosing_body(status):
    seen = []
    def upstream(request):
        seen.append(request)
        return httpx.Response(status, text="private-password", headers={"location": "https://attacker.example/steal"})
    fails("pve_guest_unavailable", lambda: asyncio.run(client(upstream).list_guests(browser())))
    assert len(seen) == 1


@pytest.mark.parametrize("payload", [None, {}, {"data": {}}, {"data": [], "errors": {"private": "password"}}, {"data": [], "success": False}, {"data": [None]}, {"data": [{"type": []}]}, {"data": [{"type": {}}]}])
def test_malformed_discovery_payloads_fail_safely(payload):
    fails("pve_guest_unavailable", lambda: asyncio.run(client(lambda req: httpx.Response(200, json=payload)).list_guests(browser())))


@pytest.mark.parametrize("patch", [{"vmid": True}, {"vmid": "101"}, {"vmid": 0}, {"vmid": -1}, {"vmid": 1_000_000_000}, {"node": "../../other"}, {"name": {}}, {"template": "false"}])
def test_malformed_guest_resource_fields_are_not_returned(patch):
    row = {**GUEST, "type": "qemu", **patch}
    fails("pve_guest_unavailable", lambda: asyncio.run(client(lambda req: httpx.Response(200, json={"data": [row]})).list_guests(browser())))


def test_unknown_runtime_status_is_normalized_without_hiding_other_guests():
    row = {**GUEST, "type": "qemu", "status": {"private": "unexpected"}}
    result = asyncio.run(client(lambda req: httpx.Response(200, json={"data": [row]})).list_guests(browser()))
    assert result == [{**GUEST, "status": "unknown"}]


def test_duplicate_guest_resource_identity_is_ambiguous_and_fails_closed():
    row = {**GUEST, "type": "qemu"}
    fails("pve_guest_unavailable", lambda: asyncio.run(client(lambda req: httpx.Response(200, json={"data": [row, {**row, "node": "pve-b"}]})).list_guests(browser())))


def test_http_failures_never_expose_cookie_or_request_details():
    def upstream(request):
        raise httpx.ReadTimeout("private ticket and private URL", request=request)
    fails("pve_guest_unavailable", lambda: asyncio.run(client(upstream).list_guests(browser())))


@pytest.mark.parametrize("url", ["http://pve.example", "https://user:private-password@pve.example", "https://pve.example/path", "https://pve.example?", "https://pve.example#", "https://pve.example\\other", "https://pve.example\n", "https://bad host", "https://pve.example:invalid", "https://pve.example:0", None])
def test_pve_origin_is_fixed_and_validation_never_echoes_credentials(url):
    with pytest.raises(ValueError) as error:
        PveGuestClient(url)
    assert str(error.value) == "Guest links require a fixed HTTP(S) origin"


def test_missing_session_cannot_contact_pve():
    req = Request({"type": "http", "headers": []})
    def unexpected(request):
        pytest.fail("missing session must not cause a request")
    fails("pve_guest_session_required", lambda: asyncio.run(client(unexpected).list_guests(req)), 401)
