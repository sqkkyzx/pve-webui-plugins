"""Direct access must be explicitly routed and continuously unlinked."""

import asyncio

from fastapi import Request
import pytest

from plugins.komodo.guest_links import GuestLinkError, GuestLinkStore
from plugins.komodo.server_access import DIRECT_SERVER, ServerAccess, valid_server_target


SERVER, OTHER = "a" * 24, "b" * 24


def request(*, direct=True, identifier=SERVER):
    return Request({"type": "http", "method": "GET", "path": "/",
        "path_params": {"server_id": identifier} if direct else {"guest_type": "server", "vmid": identifier}, "headers": []})


class NoPve:
    async def list_guests(self, request):
        pytest.fail("direct server context must never query guest discovery")


def run(service, **kwargs):
    return asyncio.run(service.resolve(request(), DIRECT_SERVER, SERVER, **kwargs))


def test_direct_access_checks_active_binding_and_allows_explicitly_unlinked_history():
    store = GuestLinkStore(":memory:", komodo_origin="http://core:9120", pve_origin="https://pve.example:8006")
    service = ServerAccess(store, NoPve())
    assert run(service) == {"server_id": SERVER, "binding_revision": 0, "guest_type": "server", "vmid": SERVER}
    link = store.save(SERVER, {"type": "qemu", "vmid": 119},
        {"kind": "vm_uuid", "strength": "strong", "values": ["f39d4086-245a-471b-9d67-d4cbdc12139b"]}, expected_revision=0)
    with pytest.raises(GuestLinkError, match="server_link_requires_guest") as error:
        run(service)
    assert error.value.status_code == 409
    store.unlink(SERVER, expected_revision=link["revision"])
    assert run(service, expected_server_id=SERVER, expected_revision=0)["binding_revision"] == 0
    store.close()


@pytest.mark.parametrize("kwargs", [{"expected_server_id": OTHER}, {"expected_revision": 1}, {"expected_revision": True}])
def test_direct_context_does_not_accept_foreign_server_or_guest_revision(kwargs):
    with pytest.raises(GuestLinkError, match="server_access_context_changed"):
        run(ServerAccess(None, NoPve()), **kwargs)


def test_missing_store_fails_closed_instead_of_assuming_unlinked():
    with pytest.raises(GuestLinkError, match="guest_link_store_not_configured") as error:
        run(ServerAccess(None, NoPve()))
    assert error.value.status_code == 503


def test_unavailable_store_error_is_preserved():
    class Store:
        def get(self, server):
            raise GuestLinkError("guest_link_store_unavailable", 503)
    with pytest.raises(GuestLinkError, match="guest_link_store_unavailable"):
        run(ServerAccess(Store(), NoPve()))


def test_generic_guest_route_cannot_supply_direct_server_marker():
    service = ServerAccess(None, NoPve())
    assert not valid_server_target(request(direct=False), DIRECT_SERVER, SERVER)
    with pytest.raises(GuestLinkError, match="invalid_server_id"):
        asyncio.run(service.resolve(request(direct=False), DIRECT_SERVER, SERVER))


@pytest.mark.parametrize("revision", [0, False])
def test_zero_revision_never_bypasses_guest_contract(revision):
    with pytest.raises(GuestLinkError, match="invalid_guest_link_revision") as error:
        asyncio.run(ServerAccess(None, NoPve()).resolve(request(), "qemu", 119, expected_revision=revision))
    assert error.value.status_code == 422
