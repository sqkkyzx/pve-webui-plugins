"""PVE guest-bound server options with authenticated, deduplicated writes."""

import asyncio
import hashlib
import logging
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, UUID4, ValidationError, field_validator

from plugins.komodo.action_store import ActionStoreError
from plugins.komodo.server_access import DIRECT_SERVER, ServerAccess, access_identifier, valid_server_target
from plugins.komodo.actions import KomodoActionError
from plugins.komodo.guest_links import GuestLinkError, PveGuestClient
from plugins.komodo.komodo import KomodoDashboardError
from plugins.komodo.server_options import validate_patch, validate_public_key


LOGGER = logging.getLogger("pve_komodo")


class OptionsCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["save", "public_key", "rotate_keys"]
    server_id: StrictStr = Field(pattern=r"^[0-9a-fA-F]{24}$")
    binding_revision: StrictInt = Field(ge=0, le=9_007_199_254_740_991)
    base_version: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: UUID4
    config: dict | None = None
    public_key: StrictStr | None = None

    @field_validator("server_id")
    @classmethod
    def normalized_id(cls, value: str) -> str:
        return value.lower()


def register_server_option_routes(application, config, authenticator, komodo, csrf, guest_store,
                                  action_store, action_tasks, *, transport=None):
    service = ServerAccess(guest_store, PveGuestClient(
        config.upstream, verify=config.tls_context(), transport=transport,
    ))

    async def authorize(request, guest_type, vmid, *, write=False):
        user = await authenticator.authenticate(request)
        if request.query_params:
            raise HTTPException(422, "invalid_server_options_query")
        if not valid_server_target(request, guest_type, vmid):
            raise HTTPException(422, "invalid_server_options_guest")
        if write:
            headers = request.headers.getlist("x-pve-plugins-csrf")
            csrf.verify(request, user, headers[0] if len(headers) == 1 else "")
        return user

    @application.get("/pve-plugins/api/komodo/guests/{guest_type}/{vmid}/options")
    async def server_options(request: Request, guest_type: str, vmid: str):
        try:
            async with asyncio.timeout(45):
                user = await authorize(request, guest_type, vmid)
                binding = await service.resolve(request, guest_type, access_identifier(guest_type, vmid))
                data = await komodo.server_options(binding["server_id"])
                # A mapping can be changed in another request during upstream reads.
                await service.resolve(request, guest_type, access_identifier(guest_type, vmid),
                                      expected_server_id=binding["server_id"],
                                      expected_revision=binding["binding_revision"])
                return {**data, "binding_revision": binding["binding_revision"],
                        "csrf_token": csrf.issue(request, user)}
        except (KomodoActionError, KomodoDashboardError, GuestLinkError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "server_options_timeout") from None

    @application.post("/pve-plugins/api/komodo/guests/{guest_type}/{vmid}/options")
    async def save_server_options(request: Request, guest_type: str, vmid: str):
        try:
            async with asyncio.timeout(20):
                user = await authorize(request, guest_type, vmid, write=True)
        except TimeoutError:
            raise HTTPException(503, "pve_verification_timeout") from None
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "json_required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, "server_options_request_too_large")
        try:
            command = OptionsCommand.model_validate_json(bytes(body))
            supplied = command.model_fields_set
            if command.action == "save":
                if "config" not in supplied or "public_key" in supplied:
                    raise ValueError
                patch = validate_patch(command.config)
                public_key = None
            elif command.action == "public_key":
                if "public_key" not in supplied or "config" in supplied:
                    raise ValueError
                public_key = validate_public_key(command.public_key)
                patch = None
            else:
                if "config" in supplied or "public_key" in supplied:
                    raise ValueError
                patch = public_key = None
        except (ValidationError, ValueError, TypeError, KomodoActionError):
            raise HTTPException(422, "invalid_server_options_request") from None
        if action_store is None:
            raise HTTPException(503, "action_state_not_configured")
        try:
            async with asyncio.timeout(20):
                binding = await service.resolve(
                    request, guest_type, access_identifier(guest_type, vmid), expected_server_id=command.server_id,
                    expected_revision=command.binding_revision,
                )
        except (KomodoActionError, GuestLinkError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "server_options_timeout") from None
        # Same durable ledger as Docker actions; the payload separates this RPC
        # family without ever storing config values or public keys in plaintext.
        payload = {"kind": "guest_server_options", "guest_type": guest_type, "vmid": access_identifier(guest_type, vmid),
                   **command.model_dump(mode="json", exclude={"request_id", "config", "public_key"}),
                   "config": patch, "public_key": public_key}
        scope = hashlib.sha256((user + "\0" + config.upstream + "\0" + (config.komodo_url or "")).encode()).hexdigest()
        request_id = str(command.request_id)
        try:
            cached = action_store.reserve(scope, request_id, payload)
        except ActionStoreError as error:
            raise HTTPException(error.status_code, error.code) from None
        if cached is not None:
            return JSONResponse(cached[1], status_code=cached[0])

        async def guard():
            await service.resolve(request, guest_type, access_identifier(guest_type, vmid),
                                  expected_server_id=binding["server_id"],
                                  expected_revision=binding["binding_revision"])

        async def dispatch():
            try:
                async with asyncio.timeout(70):
                    result = await komodo.update_server_options(
                        binding["server_id"], action=command.action, base_version=command.base_version,
                        config=patch, public_key=public_key, before_write=guard,
                    )
                status = 200
            except (KomodoActionError, KomodoDashboardError, GuestLinkError) as error:
                status, result = error.status_code, {"detail": error.code}
            except Exception:
                status, result = 200, {"ok": False, "status": "outcome_unknown",
                                      "operation_id": None, "error_code": "outcome_unknown"}
            try:
                action_store.finish(scope, request_id, status, result)
            except ActionStoreError:
                LOGGER.warning("Server options receipt unavailable user=%s request_id=%s", user, request_id)
            LOGGER.info("Server options user=%s request_id=%s action=%s", user, request_id, command.action)
            return status, result

        task = asyncio.create_task(dispatch())
        action_tasks.add(task)
        task.add_done_callback(action_tasks.discard)
        status, result = await asyncio.shield(task)
        return JSONResponse(result, status_code=status)


    @application.get("/pve-plugins/api/komodo/servers/{server_id}/options")
    async def direct_options(request: Request, server_id: str):
        return await server_options(request, DIRECT_SERVER, server_id)

    @application.post("/pve-plugins/api/komodo/servers/{server_id}/options")
    async def direct_save_options(request: Request, server_id: str):
        return await save_server_options(request, DIRECT_SERVER, server_id)
