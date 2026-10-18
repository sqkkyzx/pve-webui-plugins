"""Resolve a verified guest binding or explicitly unlinked Komodo server.

Direct server access is available only while the local guest-link store has no
active binding for that server. It must never bypass the existing guest identity
checks merely because a caller supplies a Komodo ID.
"""

import re

from plugins.komodo.guest_links import GuestLinkError
from plugins.komodo.server_options import GuestBoundOptionsService


_SERVER = re.compile(r"[0-9a-f]{24}\Z")
_GUEST = re.compile(r"[1-9][0-9]{2,8}\Z")
DIRECT_SERVER = "server"


def valid_server_target(request, kind, identifier):
    if not isinstance(identifier, str):
        return False
    if kind == DIRECT_SERVER:
        # Only the dedicated /servers/{server_id}/... wrapper may select direct
        # access. The generic /guests/{guest_type}/{vmid}/... route cannot use
        # a forged guest_type=server to obtain this context.
        return bool(_SERVER.fullmatch(identifier) and request.path_params.get("server_id") == identifier
                    and "guest_type" not in request.path_params)
    return bool(kind in {"qemu", "lxc"} and _GUEST.fullmatch(identifier))


def access_identifier(kind, identifier):
    return identifier if kind == DIRECT_SERVER else int(identifier)


class ServerAccess:
    def __init__(self, store, pve):
        self.store = store
        self.guest = GuestBoundOptionsService(store, pve)

    async def resolve(self, request, kind, identifier, *, expected_server_id=None, expected_revision=None):
        if kind != DIRECT_SERVER:
            # Models permit revision zero for direct routes, but it must never
            # weaken the original guest binding revision contract.
            if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
                raise GuestLinkError("invalid_guest_link_revision", 422)
            return await self.guest.resolve(request, kind, int(identifier),
                expected_server_id=expected_server_id, expected_revision=expected_revision)
        if not valid_server_target(request, kind, identifier):
            raise GuestLinkError("invalid_server_id", 422)
        if expected_server_id is not None and expected_server_id != identifier:
            raise GuestLinkError("server_access_context_changed", 409)
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != 0):
            raise GuestLinkError("server_access_context_changed", 409)
        if self.store is None:
            raise GuestLinkError("guest_link_store_not_configured", 503)
        if self.store.get(identifier) is not None:
            raise GuestLinkError("server_link_requires_guest", 409)
        return {"server_id": identifier, "binding_revision": 0, "guest_type": DIRECT_SERVER, "vmid": identifier}
