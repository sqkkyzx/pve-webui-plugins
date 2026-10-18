"""Create one Komodo Server, optionally bind it to a freshly verified PVE guest.

The CreateServer write is never retried or compensated by deletion. If creation
succeeds and association fails, its immutable server ID remains in the durable
receipt so the caller can recover the association separately.
"""

import asyncio
import hashlib
import logging
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, UUID4, ValidationError, field_validator, model_validator

from plugins.komodo.action_store import ActionStoreError
from plugins.komodo.actions import KomodoActionError
from plugins.komodo.guest_links import GuestLinkError, PveGuestClient, _identity, assess_link
from plugins.komodo.komodo import KomodoDashboardError
from plugins.komodo.server_create import create_result, create_server, get_server_create_capabilities, validate_server_name


LOGGER = logging.getLogger("pve_komodo")


class CreateServerCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr = Field(min_length=1, max_length=512)
    guest_type: Literal["qemu", "lxc"] | None = None
    vmid: StrictInt | None = Field(default=None, ge=100, le=999_999_999)
    request_id: UUID4

    @field_validator("name")
    @classmethod
    def exact_name(cls, value):
        try:
            return validate_server_name(value)
        except KomodoActionError:
            raise ValueError("invalid_server_name") from None

    @model_validator(mode="after")
    def paired_guest(self):
        if (self.guest_type is None) != (self.vmid is None):
            raise ValueError("invalid_guest_selection")
        return self


def register_server_create_routes(application, config, authenticator, komodo, csrf, guest_store,
                                  action_store, action_tasks, *, transport=None):
    pve = PveGuestClient(config.upstream, verify=config.tls_context(), transport=transport)
    client_options = {"origin": komodo._origin, "key": komodo._key, "secret": komodo._secret,
                      "verify": komodo._verify, "transport": komodo._transport}

    async def authorize(request, *, write=False):
        user = await authenticator.authenticate(request)
        if request.query_params:
            raise HTTPException(422, "invalid_server_create_query")
        if write:
            headers = request.headers.getlist("x-pve-plugins-csrf")
            csrf.verify(request, user, headers[0] if len(headers) == 1 else "")
        return user

    def unlinked_guests(guests):
        occupied = {(row["guest_type"], row["vmid"]) for row in guest_store.list()}
        return [guest for guest in guests if not guest["template"] and (guest["guest_type"], guest["vmid"]) not in occupied]

    async def selected_guest(request, command):
        if guest_store is None:
            raise GuestLinkError("guest_link_store_not_configured")
        guest_store.cluster_id()
        guests = await pve.list_guests(request)
        guest = next((row for row in unlinked_guests(guests) if row["guest_type"] == command.guest_type and row["vmid"] == command.vmid), None)
        if guest is None:
            raise GuestLinkError("pve_guest_unavailable_or_linked", 409)
        identity = _identity(await pve.fingerprint(request, guest), command.guest_type)
        if not identity["values"]:
            raise GuestLinkError("guest_identity_unavailable", 409)
        return guest, identity, guests

    @application.get("/pve-plugins/api/komodo/server-create-options")
    async def options(request: Request):
        try:
            async with asyncio.timeout(45):
                user = await authorize(request)
                capabilities = await get_server_create_capabilities(**client_options)
                guests, errors = [], []
                if guest_store is not None:
                    try:
                        guests = unlinked_guests(await pve.list_guests(request))
                    except GuestLinkError:
                        errors.append("server_create_guests_unavailable")
                return {**capabilities, "guests": guests, "can_link": guest_store is not None,
                        "errors": errors, "csrf_token": csrf.issue(request, user)}
        except (KomodoActionError, KomodoDashboardError, GuestLinkError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "server_create_options_timeout") from None

    @application.post("/pve-plugins/api/komodo/servers/create")
    async def create(request: Request):
        try:
            async with asyncio.timeout(20):
                user = await authorize(request, write=True)
        except TimeoutError:
            raise HTTPException(503, "pve_verification_timeout") from None
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "json_required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                raise HTTPException(413, "server_create_request_too_large")
        try:
            command = CreateServerCommand.model_validate_json(bytes(body))
        except ValidationError:
            raise HTTPException(422, "invalid_server_create_request") from None
        if action_store is None:
            raise HTTPException(503, "action_state_not_configured")
        if command.guest_type is not None and guest_store is None:
            raise HTTPException(503, "guest_link_store_not_configured")
        payload = {"kind": "server_create", **command.model_dump(mode="json", exclude={"request_id"})}
        scope = hashlib.sha256((user + "\0" + config.upstream + "\0" + (config.komodo_url or "")).encode()).hexdigest()
        request_id = str(command.request_id)
        try:
            cached = action_store.reserve(scope, request_id, payload)
        except ActionStoreError as error:
            raise HTTPException(error.status_code, error.code) from None
        if cached is not None:
            return JSONResponse(cached[1], status_code=cached[0])

        async def dispatch():
            result, baseline, submitted = None, None, False
            try:
                async with asyncio.timeout(120):
                    if command.guest_type is not None:
                        _, baseline, _ = await selected_guest(request, command)

                    async def guard():
                        nonlocal submitted
                        if baseline is not None:
                            _, identity, _ = await selected_guest(request, command)
                            if identity != baseline:
                                raise GuestLinkError("guest_identity_changed", 409)
                        submitted = True

                    result = await create_server(command.name, before_write=guard, **client_options)
                    if result["created"] is True and baseline is not None:
                        try:
                            guest, identity, guests = await selected_guest(request, command)
                            if identity != baseline:
                                raise GuestLinkError("guest_identity_changed", 409)
                            saved = guest_store.save(result["server_id"], guest, identity, expected_revision=0, confirmed=True)
                            link = assess_link(saved, guests, identity)
                            link["guest_name"] = link.pop("name", None)
                            link["server_name"] = result["server_name"]
                            result["link"] = link
                        except GuestLinkError as error:
                            result.update(ok=False, status="created_unlinked", link_error=error.code)
                status = 200
            except (KomodoActionError, KomodoDashboardError, GuestLinkError) as error:
                if result is not None and result.get("created") is True:
                    status = 200
                    result.update(ok=False, status="created_unlinked", link_error=error.code)
                else:
                    status, result = error.status_code, {"detail": error.code}
            except Exception:
                if result is not None and result.get("created") is True:
                    status = 200
                    result.update(ok=False, status="created_unlinked", link_error="guest_link_unavailable")
                elif submitted:
                    status, result = 200, create_result(command.name, status="outcome_unknown", error_code="outcome_unknown")
                else:
                    status, result = 503, {"detail": "server_create_preflight_unavailable"}
            try:
                action_store.finish(scope, request_id, status, result)
            except ActionStoreError:
                LOGGER.warning("Server create receipt unavailable user=%s request_id=%s", user, request_id)
            LOGGER.info("Server create user=%s request_id=%s status=%s", user, request_id, result.get("status", "rejected"))
            return status, result

        task = asyncio.create_task(dispatch())
        action_tasks.add(task)
        task.add_done_callback(action_tasks.discard)
        status, result = await asyncio.shield(task)
        return JSONResponse(result, status_code=status)
