"""Komodo plugin registration and lifecycle; loaded only when selected."""

import asyncio
import hashlib
import logging
import re
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, UUID4, ValidationError, ValidationInfo, field_validator

from plugins.komodo.action_store import ActionStore, ActionStoreError
from plugins.komodo.alerts import ALERT_TYPES, RESOURCE_TYPES
from plugins.komodo.container_routes import register_container_routes
from plugins.komodo.guest_link_routes import register_guest_link_routes
from plugins.komodo.komodo import KomodoActionError, KomodoClient, KomodoDashboardError
from plugins.komodo.rpc_transport import KomodoTransport
from plugins.komodo.server_option_routes import register_server_option_routes
from plugins.komodo.server_inventory_routes import register_server_inventory_routes
from plugins.komodo.server_create_routes import register_server_create_routes
from plugins.komodo.server_summary_routes import register_server_summary_routes
from plugins.komodo.stack_routes import register_stack_routes
from plugins.komodo.stack_terminal import register_stack_terminal_routes, register_server_terminal_routes
from plugins.komodo.updates import OPERATIONS

from app.plugins import PluginHandle

LOGGER = logging.getLogger("pve_komodo")
DASHBOARD_TIMEOUT_SECONDS = 45
ACTION_AUTH_TIMEOUT_SECONDS = 20
ACTION_DISPATCH_TIMEOUT_SECONDS = 80


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: Literal["servers", "stacks", "procedures", "containers"]
    action: StrictStr = Field(min_length=1, max_length=40)
    ids: list[StrictStr] = Field(min_length=1, max_length=50)
    request_id: UUID4

    @field_validator("ids")
    @classmethod
    def resource_ids(cls, values: list[str], info: ValidationInfo) -> list[str]:
        # Container names repeat across hosts and can be reused after deletion.
        # Bind a selection to both its server and immutable full Docker ID.
        pattern = r"[0-9a-fA-F]{24}:[0-9a-fA-F]{64}" if info.data.get("section") == "containers" else r"[0-9a-fA-F]{24}"
        if any(re.fullmatch(pattern, value) is None for value in values):
            raise ValueError("Invalid resource ID")
        return list(dict.fromkeys(value.lower() for value in values))

def setup(context):
    application, config = context.application, context.settings
    authenticator, csrf = context.authenticator, context.csrf
    pve_auth_transport = context.transports.get("pve_auth")
    komodo_transport = context.transports.get("komodo")
    komodo = KomodoClient(
        config.komodo_url, config.komodo_api_key, config.komodo_api_secret,
        verify=config.komodo_tls_verify, transport=komodo_transport,
    )
    pool = KomodoTransport(verify=komodo._verify) if komodo.configured and komodo_transport is None else None
    if pool is not None:
        komodo._transport = pool
    action_tasks: set[asyncio.Task] = set()

    action_store = ActionStore(config.action_state_file) if config.action_state_file else None
    guest_store = register_guest_link_routes(application, config, authenticator, komodo, csrf, transport=pve_auth_transport)
    register_server_option_routes(application, config, authenticator, komodo, csrf, guest_store,
                                  action_store, action_tasks, transport=pve_auth_transport)
    register_server_summary_routes(application, config, authenticator, komodo, csrf, guest_store,
                                   action_store, action_tasks, transport=pve_auth_transport)
    register_stack_routes(application, config, authenticator, komodo, csrf, action_store, action_tasks)
    register_container_routes(application, config, authenticator, komodo, csrf, action_store, action_tasks)
    register_stack_terminal_routes(application, config, authenticator, komodo, csrf, action_store)
    register_server_inventory_routes(application, config, authenticator, komodo, csrf, guest_store,
                                     action_store, action_tasks, transport=pve_auth_transport)
    register_server_terminal_routes(application, config, authenticator, komodo, csrf, guest_store,
                                    action_store, transport=pve_auth_transport)
    register_server_create_routes(application, config, authenticator, komodo, csrf, guest_store,
                                  action_store, action_tasks, transport=pve_auth_transport)

    @application.get("/pve-komodo/api/connection")
    async def connection(request: Request) -> dict:
        # Authorize the browser session before using the service account. These
        # routes are read-only; future write routes also need CSRF protection.
        user = await authenticator.authenticate(request)
        status = await komodo.connection_status()
        LOGGER.info("Komodo connection check user=%s connected=%s", user, status["connected"])
        return {"user": user, "komodo": status}

    @application.get("/pve-komodo/api/dashboard")
    async def dashboard(request: Request) -> dict:
        try:
            # Bound the whole operation, including authentication, below the
            # browser's 60-second timeout so slow upstreams cannot strand work.
            async with asyncio.timeout(DASHBOARD_TIMEOUT_SECONDS):
                user = await authenticator.authenticate(request)
                data = await komodo.dashboard()
        except KomodoDashboardError as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "dashboard_timeout") from None
        LOGGER.info("Komodo dashboard read user=%s", user)
        return {"user": user, **data, "csrf_token": csrf.issue(request, user)}

    @application.get("/pve-komodo/api/containers")
    async def containers(request: Request) -> dict:
        try:
            async with asyncio.timeout(DASHBOARD_TIMEOUT_SECONDS):
                user = await authenticator.authenticate(request)
                data = await komodo.containers()
        except KomodoDashboardError as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "containers_timeout") from None
        LOGGER.info("Komodo containers read user=%s", user)
        return {"user": user, **data, "csrf_token": csrf.issue(request, user)}

    @application.get("/pve-komodo/api/alerts")
    async def alerts(request: Request) -> dict:
        try:
            async with asyncio.timeout(DASHBOARD_TIMEOUT_SECONDS):
                user = await authenticator.authenticate(request)
                query = request.query_params
                allowed = {"page", "resource_type", "alert_type", "unresolved_only"}
                if any(key not in allowed or len(query.getlist(key)) != 1 for key in query):
                    raise HTTPException(422, "invalid_alert_query")
                page_text = query.get("page", "0")
                resource_type = query.get("resource_type", "")
                alert_type = query.get("alert_type", "")
                unresolved = query.get("unresolved_only", "false")
                if (
                    re.fullmatch(r"0|[1-9][0-9]{0,6}", page_text) is None
                    or int(page_text) > 1_000_000
                    or resource_type not in ("", *RESOURCE_TYPES)
                    or alert_type not in ("", *ALERT_TYPES)
                    or unresolved not in {"true", "false"}
                ):
                    raise HTTPException(422, "invalid_alert_query")
                data = await komodo.alerts(
                    page=int(page_text), resource_type=resource_type,
                    alert_type=alert_type, unresolved_only=unresolved == "true",
                )
        except KomodoDashboardError as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "alerts_timeout") from None
        LOGGER.info("Komodo alerts read user=%s page=%s", user, page_text)
        return {"user": user, **data}

    @application.get("/pve-komodo/api/updates")
    async def updates(request: Request) -> dict:
        try:
            async with asyncio.timeout(DASHBOARD_TIMEOUT_SECONDS):
                user = await authenticator.authenticate(request)
                query = request.query_params
                allowed = {"page", "resource_type", "operation"}
                if any(key not in allowed or len(query.getlist(key)) != 1 for key in query):
                    raise HTTPException(422, "invalid_update_query")
                page_text = query.get("page", "0")
                resource_type = query.get("resource_type", "")
                operation = query.get("operation", "")
                if (
                    re.fullmatch(r"0|[1-9][0-9]{0,6}", page_text) is None
                    or int(page_text) > 1_000_000
                    or resource_type not in ("", *RESOURCE_TYPES)
                    or operation not in ("", *OPERATIONS)
                ):
                    raise HTTPException(422, "invalid_update_query")
                data = await komodo.updates(
                    page=int(page_text), resource_type=resource_type, operation=operation,
                )
        except KomodoDashboardError as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "updates_timeout") from None
        LOGGER.info("Komodo updates read user=%s page=%s", user, page_text)
        return {"user": user, **data}

    @application.get("/pve-komodo/api/updates/{update_id}")
    async def update_details(request: Request, update_id: str) -> dict:
        try:
            async with asyncio.timeout(DASHBOARD_TIMEOUT_SECONDS):
                user = await authenticator.authenticate(request)
                if re.fullmatch(r"[0-9a-fA-F]{24}", update_id) is None or request.query_params:
                    raise HTTPException(422, "invalid_update_id")
                data = await komodo.update_details(update_id.lower())
        except KomodoDashboardError as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "update_details_timeout") from None
        LOGGER.info("Komodo update details read user=%s", user)
        return {"user": user, **data}

    @application.post("/pve-komodo/api/action")
    async def action(request: Request) -> JSONResponse:
        try:
            async with asyncio.timeout(ACTION_AUTH_TIMEOUT_SECONDS):
                user = await authenticator.authenticate(request)
        except TimeoutError:
            raise HTTPException(503, "pve_verification_timeout") from None
        csrf_headers = request.headers.getlist("x-pve-komodo-csrf")
        csrf.verify(request, user, csrf_headers[0] if len(csrf_headers) == 1 else "")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "json_required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                raise HTTPException(413, "action_request_too_large")
        try:
            command = ActionRequest.model_validate_json(bytes(body))
        except ValidationError:
            raise HTTPException(422, "invalid_action_request") from None
        payload = command.model_dump(mode="json", exclude={"request_id"})
        request_id = str(command.request_id)
        if action_store is None:
            raise HTTPException(503, "action_state_not_configured")
        # PVE renews its ticket in the background. Deduplication must outlive
        # that renewal while CSRF independently remains bound to the ticket.
        scope = hashlib.sha256((user + "\0" + config.upstream + "\0" + (config.komodo_url or "")).encode()).hexdigest()
        try:
            cached = action_store.reserve(scope, request_id, payload)
        except ActionStoreError as error:
            raise HTTPException(error.status_code, error.code) from None
        if cached is not None:
            return JSONResponse(cached[1], status_code=cached[0])

        async def dispatch() -> tuple[int, dict]:
            # Keep a confirmed submission alive if the client navigates away.
            # The durable reservation prevents repeats across process reloads.
            try:
                async with asyncio.timeout(ACTION_DISPATCH_TIMEOUT_SECONDS):
                    result = await komodo.perform_action(command.section, command.action, command.ids)
                status = 200
            except KomodoActionError as error:
                status, result = error.status_code, {"detail": error.code}
            except Exception:
                # An unexpected failure may occur after a write was accepted.
                # Never echo exception text or imply the request can be retried.
                status, result = 200, {
                    "section": command.section, "action": command.action,
                    "results": [{"id": identifier, "ok": False, "status": "outcome_unknown",
                                 "operation_id": None, "error_code": "outcome_unknown"}
                                for identifier in command.ids],
                }
            try:
                action_store.finish(scope, request_id, status, result)
            except ActionStoreError:
                LOGGER.warning("Komodo action receipt could not be persisted user=%s request_id=%s", user, request_id)
            LOGGER.info("Komodo action user=%s request_id=%s section=%s action=%s count=%s",
                        user, request_id, command.section, command.action, len(command.ids))
            return status, result

        task = asyncio.create_task(dispatch())
        action_tasks.add(task)
        task.add_done_callback(action_tasks.discard)
        status, result = await asyncio.shield(task)
        return JSONResponse(result, status_code=status)

    async def shutdown():
        try:
            if action_tasks:
                await asyncio.gather(*tuple(action_tasks), return_exceptions=True)
        finally:
            try:
                if guest_store is not None:
                    guest_store.close()
            finally:
                try:
                    if action_store is not None:
                        action_store.close()
                finally:
                    if pool is not None:
                        await pool.close_pool()

    return PluginHandle(shutdown=shutdown, metadata={"configured": komodo.configured})
