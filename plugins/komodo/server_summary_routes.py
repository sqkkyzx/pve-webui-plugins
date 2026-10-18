"""Guest-bound overview, sampled history and explicitly confirmed server actions."""

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
from plugins.komodo.server_summary import HISTORY_INTERVALS


LOGGER = logging.getLogger("pve_komodo")


class ServerExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["restart_all_containers", "pause_all_containers", "stop_all_containers", "prune_buildx", "prune_system"]
    server_id: StrictStr = Field(pattern=r"^[0-9a-fA-F]{24}$")
    binding_revision: StrictInt = Field(ge=0, le=9_007_199_254_740_991)
    request_id: UUID4

    @field_validator("server_id")
    @classmethod
    def normalized_id(cls, value):
        return value.lower()


class ServerDescriptionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_id: StrictStr = Field(pattern=r"^[0-9a-fA-F]{24}$")
    binding_revision: StrictInt = Field(ge=0, le=9_007_199_254_740_991)
    request_id: UUID4
    base_version: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    description: StrictStr = Field(max_length=16_384)

    @field_validator("server_id")
    @classmethod
    def normalized_id(cls, value):
        return value.lower()

    @field_validator("description")
    @classmethod
    def plain_text(cls, value):
        if any(ord(char) < 32 and char not in "\n\r\t" or ord(char) == 127 for char in value):
            raise ValueError("invalid_description")
        return value


def register_server_summary_routes(application, config, authenticator, komodo, csrf, guest_store,
                                   action_store, action_tasks, *, transport=None):
    service = ServerAccess(guest_store, PveGuestClient(
        config.upstream, verify=config.tls_context(), transport=transport,
    ))

    async def authorize(request, guest_type, vmid, *, history=False, write=False):
        user = await authenticator.authenticate(request)
        allowed = {"interval"} if history else set()
        if any(key not in allowed or len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise HTTPException(422, "invalid_server_summary_query")
        if not valid_server_target(request, guest_type, vmid):
            raise HTTPException(422, "invalid_server_summary_guest")
        if write:
            headers = request.headers.getlist("x-pve-plugins-csrf")
            csrf.verify(request, user, headers[0] if len(headers) == 1 else "")
        return user

    async def recheck(request, guest_type, vmid, binding):
        await service.resolve(request, guest_type, access_identifier(guest_type, vmid), expected_server_id=binding["server_id"],
                              expected_revision=binding["binding_revision"])

    @application.get("/pve-plugins/api/komodo/guests/{guest_type}/{vmid}/summary")
    async def summary(request: Request, guest_type: str, vmid: str):
        try:
            async with asyncio.timeout(45):
                user = await authorize(request, guest_type, vmid)
                binding = await service.resolve(request, guest_type, access_identifier(guest_type, vmid))
                data = await komodo.server_summary(binding["server_id"])
                await recheck(request, guest_type, vmid, binding)
                return {**data, "binding_revision": binding["binding_revision"], "csrf_token": csrf.issue(request, user)}
        except (KomodoDashboardError, KomodoActionError, GuestLinkError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "server_summary_timeout") from None

    @application.get("/pve-plugins/api/komodo/guests/{guest_type}/{vmid}/history")
    async def history(request: Request, guest_type: str, vmid: str):
        try:
            async with asyncio.timeout(45):
                await authorize(request, guest_type, vmid, history=True)
                interval = request.query_params.get("interval", "1-day")
                if interval not in HISTORY_INTERVALS:
                    raise HTTPException(422, "invalid_server_history_interval")
                binding = await service.resolve(request, guest_type, access_identifier(guest_type, vmid))
                data = await komodo.server_history(binding["server_id"], interval=interval)
                await recheck(request, guest_type, vmid, binding)
                return {**data, "binding_revision": binding["binding_revision"]}
        except (KomodoDashboardError, KomodoActionError, GuestLinkError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "server_history_timeout") from None

    @application.post("/pve-plugins/api/komodo/guests/{guest_type}/{vmid}/description")
    async def description(request: Request, guest_type: str, vmid: str):
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
            if len(body) > 262_144:
                raise HTTPException(413, "server_description_request_too_large")
        try:
            command = ServerDescriptionEdit.model_validate_json(bytes(body))
        except ValidationError:
            raise HTTPException(422, "invalid_server_description") from None
        if action_store is None:
            raise HTTPException(503, "action_state_not_configured")
        try:
            async with asyncio.timeout(20):
                binding = await service.resolve(request, guest_type, access_identifier(guest_type, vmid),
                    expected_server_id=command.server_id, expected_revision=command.binding_revision)
        except (GuestLinkError, KomodoActionError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "server_description_preflight_timeout") from None
        payload = {"kind": "guest_server_description", "guest_type": guest_type, "vmid": access_identifier(guest_type, vmid),
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
                async with asyncio.timeout(60):
                    result = await komodo.update_server_description(binding["server_id"], description=command.description,
                        base_version=command.base_version, before_write=guard)
                status = 200
            except (KomodoActionError, KomodoDashboardError, GuestLinkError) as error:
                status, result = error.status_code, {"detail": error.code}
            except Exception:
                status, result = 200, {"ok": False, "status": "outcome_unknown",
                                      "operation_id": None, "error_code": "outcome_unknown"}
            try:
                action_store.finish(scope, request_id, status, result)
            except ActionStoreError:
                LOGGER.warning("Description receipt unavailable user=%s request_id=%s", user, request_id)
            LOGGER.info("Guest server description user=%s request_id=%s", user, request_id)
            return status, result

        task = asyncio.create_task(dispatch())
        action_tasks.add(task)
        task.add_done_callback(action_tasks.discard)
        status, result = await asyncio.shield(task)
        return JSONResponse(result, status_code=status)

    @application.post("/pve-plugins/api/komodo/guests/{guest_type}/{vmid}/actions")
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
            if len(body) > 4096:
                raise HTTPException(413, "server_execution_request_too_large")
        try:
            command = ServerExecution.model_validate_json(bytes(body))
        except ValidationError:
            raise HTTPException(422, "invalid_server_execution") from None
        if action_store is None:
            raise HTTPException(503, "action_state_not_configured")
        try:
            async with asyncio.timeout(20):
                binding = await service.resolve(request, guest_type, access_identifier(guest_type, vmid),
                                                expected_server_id=command.server_id,
                                                expected_revision=command.binding_revision)
        except (GuestLinkError, KomodoActionError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "server_execution_preflight_timeout") from None
        payload = {"kind": "guest_server_summary_action", "guest_type": guest_type, "vmid": access_identifier(guest_type, vmid),
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
            try:
                async with asyncio.timeout(30):
                    capabilities = await komodo.server_summary_capabilities(binding["server_id"])
                    if capabilities.get("can_execute") is not True:
                        raise KomodoActionError("server_execution_forbidden", 403)
                    await recheck(request, guest_type, vmid, binding)
            except TimeoutError:
                raise KomodoActionError("server_execution_preflight_timeout", 503) from None

        async def dispatch():
            try:
                async with asyncio.timeout(80):
                    result = await komodo.perform_action("servers", command.action, [binding["server_id"]], before_write=guard)
                status = 200
            except (KomodoActionError, KomodoDashboardError, GuestLinkError) as error:
                status, result = error.status_code, {"detail": error.code}
            except Exception:
                status, result = 200, {"section": "servers", "action": command.action, "results": [
                    {"id": binding["server_id"], "ok": False, "status": "outcome_unknown",
                     "operation_id": None, "error_code": "outcome_unknown"},
                ]}
            try:
                action_store.finish(scope, request_id, status, result)
            except ActionStoreError:
                LOGGER.warning("Server execution receipt unavailable user=%s request_id=%s", user, request_id)
            LOGGER.info("Guest server action user=%s request_id=%s action=%s", user, request_id, command.action)
            return status, result

        task = asyncio.create_task(dispatch())
        action_tasks.add(task)
        task.add_done_callback(action_tasks.discard)
        status, result = await asyncio.shield(task)
        return JSONResponse(result, status_code=status)


    @application.get("/pve-plugins/api/komodo/servers/{server_id}/summary")
    async def direct_summary(request: Request, server_id: str):
        return await summary(request, DIRECT_SERVER, server_id)

    @application.get("/pve-plugins/api/komodo/servers/{server_id}/history")
    async def direct_history(request: Request, server_id: str):
        return await history(request, DIRECT_SERVER, server_id)

    @application.post("/pve-plugins/api/komodo/servers/{server_id}/description")
    async def direct_description(request: Request, server_id: str):
        return await description(request, DIRECT_SERVER, server_id)

    @application.post("/pve-plugins/api/komodo/servers/{server_id}/actions")
    async def direct_execute(request: Request, server_id: str):
        return await execute(request, DIRECT_SERVER, server_id)
