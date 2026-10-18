"""PVE authenticated full-ID container pages and durable write receipts."""

import asyncio
import hashlib
import logging
import re
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, UUID4, ValidationError, field_validator

from plugins.komodo.action_store import ActionStoreError
from plugins.komodo.actions import KomodoActionError
from plugins.komodo.komodo import KomodoDashboardError
from plugins.komodo.container_details import (get_container_details, get_container_inspect, get_container_logs,
                                    perform_container_detail_action, update_container_description)

LOGGER = logging.getLogger("pve_komodo")
_PATH = "/pve-komodo/api/servers/{server_id}/containers/{container_id}"


class ContainerDescriptionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["description"]
    base_version: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    description: StrictStr = Field(max_length=16384)
    request_id: UUID4

    @field_validator("description")
    @classmethod
    def plain(cls, value):
        if any(ord(c) < 32 and c not in "\r\n\t" or ord(c) == 127 for c in value):
            raise ValueError("invalid_description")
        return value


class ContainerActionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["restart", "pause", "unpause", "start", "stop", "destroy"]
    base_version: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: UUID4


def register_container_routes(application, config, authenticator, komodo, csrf, action_store, action_tasks):
    def credentials():
        return dict(origin=komodo._origin, key=komodo._key, secret=komodo._secret,
                    verify=komodo._verify, transport=komodo._transport)

    def response(data, status=200):
        return JSONResponse(data, status_code=status, headers={"cache-control": "no-store", "pragma": "no-cache"})

    async def authorize(request, server_id, container_id, *, write=False, logs=False):
        user = await authenticator.authenticate(request)
        if re.fullmatch(r"[0-9a-fA-F]{24}", server_id) is None or re.fullmatch(r"[0-9a-fA-F]{64}", container_id) is None:
            raise HTTPException(422, "invalid_container_identity")
        allowed = {"tail", "timestamps", "search", "invert", "stream"} if logs else set()
        if any(key not in allowed or len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise HTTPException(422, "invalid_container_query")
        if write:
            headers = request.headers.getlist("x-pve-komodo-csrf")
            csrf.verify(request, user, headers[0] if len(headers) == 1 else "")
        return user

    @application.get(_PATH)
    async def details(request: Request, server_id: str, container_id: str):
        try:
            async with asyncio.timeout(45):
                user = await authorize(request, server_id, container_id)
                data = await get_container_details(server_id.lower(), container_id.lower(), **credentials())
            return response({**data, "csrf_token": csrf.issue(request, user)})
        except (KomodoActionError, KomodoDashboardError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "container_details_timeout") from None

    @application.get(_PATH + "/inspect")
    async def inspect(request: Request, server_id: str, container_id: str):
        try:
            async with asyncio.timeout(45):
                await authorize(request, server_id, container_id)
                data = await get_container_inspect(server_id.lower(), container_id.lower(), **credentials())
            return response(data)
        except (KomodoActionError, KomodoDashboardError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "container_inspect_timeout") from None

    @application.get(_PATH + "/logs")
    async def logs(request: Request, server_id: str, container_id: str):
        try:
            async with asyncio.timeout(45):
                await authorize(request, server_id, container_id, logs=True)
                query = request.query_params
                tail = query.get("tail", "100")
                if (re.fullmatch(r"[1-9][0-9]{0,3}", tail) is None or int(tail) > 5000
                        or query.get("timestamps", "false") not in {"true", "false"}
                        or query.get("invert", "false") not in {"true", "false"}
                        or query.get("stream", "both") not in {"both", "stdout", "stderr"}
                        or len(query.get("search", "")) > 1000):
                    raise HTTPException(422, "invalid_container_log_query")
                data = await get_container_logs(server_id.lower(), container_id.lower(), tail=int(tail),
                    timestamps=query.get("timestamps", "false") == "true", search=query.get("search", ""),
                    invert=query.get("invert", "false") == "true", stream=query.get("stream", "both"), **credentials())
            return response(data)
        except (KomodoActionError, KomodoDashboardError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "container_logs_timeout") from None

    async def write(request, server_id, container_id, *, execution):
        try:
            async with asyncio.timeout(20):
                user = await authorize(request, server_id, container_id, write=True)
        except TimeoutError:
            raise HTTPException(503, "pve_verification_timeout") from None
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "json_required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > (16384 if execution else 262144):
                raise HTTPException(413, "container_request_too_large")
        try:
            command = (ContainerActionCommand if execution else ContainerDescriptionCommand).model_validate_json(bytes(body))
        except ValidationError:
            raise HTTPException(422, "invalid_container_request") from None
        if action_store is None:
            raise HTTPException(503, "action_state_not_configured")
        server_id, container_id = server_id.lower(), container_id.lower()
        payload = {"kind": "container_detail_action" if execution else "container_description", "server_id": server_id,
                   "container_id": container_id, **command.model_dump(mode="json", exclude={"request_id"})}
        scope = hashlib.sha256((user + "\0" + config.upstream + "\0" + (config.komodo_url or "")).encode()).hexdigest()
        request_id = str(command.request_id)
        try:
            cached = action_store.reserve(scope, request_id, payload)
        except ActionStoreError as error:
            raise HTTPException(error.status_code, error.code) from None
        if cached is not None:
            return response(cached[1], cached[0])

        async def guard():
            async with asyncio.timeout(20):
                await authenticator.authenticate(request)

        async def dispatch():
            try:
                async with asyncio.timeout(80):
                    if execution:
                        result = await perform_container_detail_action(server_id, container_id, action=command.action,
                            base_version=command.base_version, before_write=guard, **credentials())
                    else:
                        result = await update_container_description(server_id, container_id, description=command.description,
                            base_version=command.base_version, before_write=guard, **credentials())
                status = 200
            except (KomodoActionError, KomodoDashboardError) as error:
                status, result = error.status_code, {"detail": error.code}
            except HTTPException as error:
                status, result = error.status_code, {"detail": error.detail}
            except Exception:
                unknown = {"ok": False, "status": "outcome_unknown", "operation_id": None, "error_code": "outcome_unknown"}
                status, result = 200, ({"section": "containers", "action": command.action,
                    "results": [{"id": f"{server_id}:{container_id}", **unknown}]} if execution else unknown)
            try:
                action_store.finish(scope, request_id, status, result)
            except ActionStoreError:
                LOGGER.warning("Container receipt unavailable user=%s request_id=%s", user, request_id)
            LOGGER.info("Container write user=%s request_id=%s server=%s container=%s action=%s",
                        user, request_id, server_id, container_id, command.action)
            return status, result

        task = asyncio.create_task(dispatch())
        action_tasks.add(task)
        task.add_done_callback(action_tasks.discard)
        status, result = await asyncio.shield(task)
        return response(result, status)

    @application.post(_PATH)
    async def description(request: Request, server_id: str, container_id: str):
        return await write(request, server_id, container_id, execution=False)

    @application.post(_PATH + "/actions")
    async def action(request: Request, server_id: str, container_id: str):
        return await write(request, server_id, container_id, execution=True)
