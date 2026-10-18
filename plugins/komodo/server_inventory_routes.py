"""Authenticated guest-bound inventory reads and durable selected actions."""

import asyncio
import hashlib
import logging
import re
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, UUID4, ValidationError, field_validator, model_validator

from plugins.komodo.action_store import ActionStoreError
from plugins.komodo.server_access import DIRECT_SERVER, ServerAccess, access_identifier, valid_server_target
from plugins.komodo.actions import KomodoActionError
from plugins.komodo.guest_links import GuestLinkError, PveGuestClient
from plugins.komodo.komodo import KomodoDashboardError
from plugins.komodo.server_inventory import get_server_inventory, perform_inventory_action


LOGGER = logging.getLogger("pve_komodo")


class InventoryTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    version: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class InventoryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: Literal["containers", "networks", "volumes", "images", "stacks"]
    action: StrictStr
    server_id: StrictStr = Field(pattern=r"^[0-9a-fA-F]{24}$")
    binding_revision: StrictInt = Field(ge=0, le=9_007_199_254_740_991)
    request_id: UUID4
    targets: list[InventoryTarget] = Field(default_factory=list, max_length=50)
    name: StrictStr | None = Field(default=None, max_length=256)

    @field_validator("server_id")
    @classmethod
    def normalized_id(cls, value):
        return value.lower()

    @model_validator(mode="after")
    def fixed_action(self):
        allowed = {"containers": {"start", "restart", "pause", "unpause", "stop", "destroy"},
                   "networks": {"delete"}, "volumes": {"delete"}, "images": {"delete"},
                   "stacks": {"check_update", "pull", "deploy", "restart", "stop", "destroy", "delete", "create_stack"}}
        if self.action not in allowed[self.section]:
            raise ValueError("invalid_inventory_action")
        if self.action == "create_stack":
            if self.targets or not isinstance(self.name, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,255}", self.name) is None or re.fullmatch(r"[0-9a-fA-F]{24}", self.name):
                raise ValueError("invalid_inventory_action")
        elif self.name is not None or not self.targets or len({target.id for target in self.targets}) != len(self.targets):
            raise ValueError("invalid_inventory_action")
        return self


def register_server_inventory_routes(application, config, authenticator, komodo, csrf, guest_store,
                                     action_store, action_tasks, *, transport=None):
    service = ServerAccess(guest_store, PveGuestClient(config.upstream, verify=config.tls_context(), transport=transport))
    client_options = {"origin": komodo._origin, "key": komodo._key, "secret": komodo._secret,
                      "verify": komodo._verify, "transport": komodo._transport}

    async def authorize(request, guest_type, vmid, *, write=False):
        user = await authenticator.authenticate(request)
        if request.query_params:
            raise HTTPException(422, "invalid_inventory_query")
        if not valid_server_target(request, guest_type, vmid):
            raise HTTPException(422, "invalid_inventory_guest")
        if write:
            headers = request.headers.getlist("x-pve-plugins-csrf")
            csrf.verify(request, user, headers[0] if len(headers) == 1 else "")
        return user

    async def recheck(request, guest_type, vmid, binding):
        await service.resolve(request, guest_type, access_identifier(guest_type, vmid), expected_server_id=binding["server_id"],
                              expected_revision=binding["binding_revision"])

    async def read(request, guest_type, vmid, kind):
        try:
            async with asyncio.timeout(45):
                user = await authorize(request, guest_type, vmid)
                binding = await service.resolve(request, guest_type, access_identifier(guest_type, vmid))
                data = await get_server_inventory(binding["server_id"], kind=kind, **client_options)
                await recheck(request, guest_type, vmid, binding)
                return {**data, "binding_revision": binding["binding_revision"], "csrf_token": csrf.issue(request, user)}
        except (KomodoDashboardError, KomodoActionError, GuestLinkError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "inventory_timeout") from None

    @application.get("/pve-plugins/api/komodo/guests/{guest_type}/{vmid}/docker")
    async def docker(request: Request, guest_type: str, vmid: str):
        return await read(request, guest_type, vmid, "docker")

    @application.get("/pve-plugins/api/komodo/guests/{guest_type}/{vmid}/resources")
    async def resources(request: Request, guest_type: str, vmid: str):
        return await read(request, guest_type, vmid, "resources")

    @application.post("/pve-plugins/api/komodo/guests/{guest_type}/{vmid}/inventory-actions")
    async def execute(request: Request, guest_type: str, vmid: str):
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
            if len(body) > 32_768:
                raise HTTPException(413, "inventory_request_too_large")
        try:
            command = InventoryCommand.model_validate_json(bytes(body))
        except ValidationError:
            raise HTTPException(422, "invalid_inventory_action") from None
        if action_store is None:
            raise HTTPException(503, "action_state_not_configured")
        try:
            async with asyncio.timeout(20):
                binding = await service.resolve(request, guest_type, access_identifier(guest_type, vmid), expected_server_id=command.server_id,
                                                expected_revision=command.binding_revision)
        except (KomodoActionError, GuestLinkError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "inventory_preflight_timeout") from None
        payload = {"kind": "guest_server_inventory", "guest_type": guest_type, "vmid": access_identifier(guest_type, vmid),
                   **command.model_dump(mode="json", exclude={"request_id"})}
        scope = hashlib.sha256((user + "\0" + config.upstream + "\0" + (config.komodo_url or "")).encode()).hexdigest()
        request_id = str(command.request_id)
        try:
            cached = action_store.reserve(scope, request_id, payload)
        except ActionStoreError as error:
            raise HTTPException(error.status_code, error.code) from None
        if cached is not None:
            return JSONResponse(cached[1], status_code=cached[0])

        async def guard():
            await recheck(request, guest_type, vmid, binding)

        async def dispatch():
            try:
                async with asyncio.timeout(90):
                    result = await perform_inventory_action(binding["server_id"], section=command.section,
                        action=command.action, targets=[target.model_dump() for target in command.targets],
                        name=command.name, before_write=guard, **client_options)
                status = 200
            except (KomodoActionError, KomodoDashboardError, GuestLinkError) as error:
                status, result = error.status_code, {"detail": error.code}
            except Exception:
                status, result = 200, {"section": command.section, "action": command.action, "results": [
                    _unknown(target.id) for target in command.targets] or [_unknown(command.name)]}
            try:
                action_store.finish(scope, request_id, status, result)
            except ActionStoreError:
                LOGGER.warning("Inventory receipt unavailable user=%s request_id=%s", user, request_id)
            LOGGER.info("Guest inventory action user=%s request_id=%s section=%s action=%s", user, request_id, command.section, command.action)
            return status, result

        task = asyncio.create_task(dispatch())
        action_tasks.add(task)
        task.add_done_callback(action_tasks.discard)
        status, result = await asyncio.shield(task)
        return JSONResponse(result, status_code=status)


    @application.get("/pve-plugins/api/komodo/servers/{server_id}/docker")
    async def direct_docker(request: Request, server_id: str):
        return await read(request, DIRECT_SERVER, server_id, "docker")

    @application.get("/pve-plugins/api/komodo/servers/{server_id}/resources")
    async def direct_resources(request: Request, server_id: str):
        return await read(request, DIRECT_SERVER, server_id, "resources")

    @application.post("/pve-plugins/api/komodo/servers/{server_id}/inventory-actions")
    async def direct_execute(request: Request, server_id: str):
        return await execute(request, DIRECT_SERVER, server_id)


def _unknown(identifier):
    return {"id": identifier, "ok": False, "status": "outcome_unknown", "operation_id": None, "error_code": "outcome_unknown"}
