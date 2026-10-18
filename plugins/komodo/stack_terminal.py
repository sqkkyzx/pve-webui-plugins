"""Temporary, identity-pinned terminals behind PVE session auth.

HTTP only issues a short-lived, single-use grant. The authenticated WebSocket
creates one uniquely named PTY, never reconnects automatically, and deletes only
that PTY when forwarding ends. Komodo credentials stay in this process.
"""

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import logging
import re
import secrets
import ssl
from time import monotonic
from typing import Literal
from urllib.parse import urlencode
import uuid

import anyio
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, UUID4, ValidationError, model_validator

from plugins.komodo.action_store import ActionStoreError
from plugins.komodo.actions import _object_id
from plugins.komodo.actions import KomodoActionError
from app.csrf import verify_write_origin
from plugins.komodo.dashboard import _text
from plugins.komodo.guest_links import GuestLinkError, PveGuestClient
from plugins.komodo.server_access import DIRECT_SERVER, ServerAccess, access_identifier, valid_server_target


_ID = re.compile(r"[0-9a-f]{24}\Z")
_CONTAINER = re.compile(r"[0-9a-f]{64}\Z")
_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_GRANT = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_GRANT_SECONDS = 60
_LIFETIME_SECONDS = 1800
_IDLE_SECONDS = 600
_REVALIDATE_SECONDS = 60
_TICK_SECONDS = 5
_MAX_INPUT = 16_384
LOGGER = logging.getLogger("pve_komodo")


class StackTerminalError(Exception):
    def __init__(self, code, status_code=502):
        self.code, self.status_code = code, status_code
        super().__init__(code)


class TerminalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: StrictStr = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    server_id: StrictStr = Field(pattern=r"^[0-9a-f]{24}$")
    container_id: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    shell: Literal["sh", "bash"]
    request_id: UUID4


class ContainerTerminalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shell: Literal["sh", "bash", "attach", "custom"]
    command: StrictStr | None = Field(default=None, max_length=2048)
    request_id: UUID4

    @model_validator(mode="after")
    def validate_command(self):
        if self.shell == "custom":
            if (not isinstance(self.command, str) or not self.command.strip()
                    or any(ord(char) < 32 or ord(char) == 127 for char in self.command)):
                raise ValueError("A custom terminal requires one nonempty command")
        elif self.command is not None:
            raise ValueError("Only custom terminals accept a command")
        return self


class ServerTerminalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_id: StrictStr = Field(pattern=r"^[0-9a-f]{24}$")
    binding_revision: StrictInt = Field(ge=0, le=9_007_199_254_740_991)
    shell: Literal["default", "sh", "bash", "custom"]
    command: StrictStr | None = Field(default=None, max_length=2048)
    request_id: UUID4

    @model_validator(mode="after")
    def validate_command(self):
        if self.shell == "custom":
            if (not isinstance(self.command, str) or not self.command.strip()
                    or any(ord(char) < 32 or ord(char) == 127 for char in self.command)):
                raise ValueError("A custom terminal requires one nonempty command")
        elif self.command is not None:
            raise ValueError("Only custom terminals accept a command")
        return self


@dataclass(repr=False)
class _Grant:
    user: str
    binding: str
    stack_id: str | None
    command: TerminalRequest | ContainerTerminalRequest | ServerTerminalRequest
    expires: float
    server_id: str
    container_id: str | None
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(repr=False)
class _ServerGrant(_Grant):
    guest_type: str = ""
    vmid: int | str = 0
    request: object = None


def _binding(request):
    return hashlib.sha256(request.cookies.get("PVEAuthCookie", "").encode("ascii")).hexdigest()


def _permission(value):
    return (isinstance(value, dict) and value.get("level") in ("Read", "Execute", "Write")
            and isinstance(value.get("specific"), list) and "Terminal" in value["specific"])


async def _default_connect(url, verify):
    # Imported lazily so isolated HTTP/mock tests need no network WS runtime.
    from websockets.asyncio.client import connect

    class FixedOriginConnect(connect):
        def process_redirect(self, error):
            # websockets follows redirects by default, including other hosts.
            # API-key login must never be sent beyond the configured origin.
            return error

    options = dict(proxy=None, compression=None, open_timeout=10, close_timeout=3,
                   max_size=1_048_576, max_queue=16)
    if url.startswith("wss:"):
        options["ssl"] = verify if isinstance(verify, ssl.SSLContext) else (
            ssl.create_default_context() if verify else ssl._create_unverified_context())
    return await FixedOriginConnect(url, **options)


class StackTerminals:
    def __init__(self, komodo, action_store, *, ws_connect=None):
        self.origin, self.key, self.secret = komodo._origin, komodo._key, komodo._secret
        self.verify, self.transport = komodo._verify, komodo._transport
        self.action_store = action_store
        self.connect = ws_connect or _default_connect
        self.grants = {}
        self.active = {}
        self.cleanup_tasks = set()

    def __repr__(self):
        return "StackTerminals()"

    async def _request(self, variant=None, params=None, *, write=False):
        if not self.origin or not self.key or not self.secret:
            raise StackTerminalError("not_configured", 503)
        headers = {"x-api-key": self.key, "x-api-secret": self.secret}
        try:
            async with httpx.AsyncClient(verify=self.verify, transport=self.transport,
                    trust_env=False, follow_redirects=False, timeout=httpx.Timeout(10, connect=5)) as client:
                async with asyncio.timeout(10):
                    response = await (client.get(self.origin + "/user", headers=headers) if variant is None else
                        client.post(self.origin + ("/write" if write else "/read"), headers=headers,
                                    json={"type": variant, "params": params}))
        except (httpx.HTTPError, TimeoutError):
            raise StackTerminalError("terminal_upstream_unavailable") from None
        if response.status_code in (401, 403):
            raise StackTerminalError("terminal_permission_denied", 403)
        if response.status_code != 200:
            raise StackTerminalError("terminal_upstream_unavailable")
        try:
            return response.json()
        except ValueError:
            raise StackTerminalError("terminal_invalid_response") from None

    async def snapshot(self, stack_id):
        identity = await self._request()
        if (not isinstance(identity, dict) or identity.get("enabled") is not True
                or _text(identity.get("username")) is None):
            raise StackTerminalError("terminal_permission_denied", 403)
        stack = await self._request("GetStack", {"stack": stack_id})
        config = stack.get("config") if isinstance(stack, dict) else None
        if (not isinstance(stack, dict) or _object_id(stack.get("_id")) != stack_id
                or stack.get("template") is not False or not isinstance(config, dict)
                or config.get("swarm_id", "") != "" or _object_id(config.get("server_id")) is None):
            raise StackTerminalError("terminal_stack_unavailable", 409)
        server_id = _object_id(config["server_id"])
        stack_permission, server_permission, server, listing, services, containers = await asyncio.gather(
            self._request("GetPermission", {"target": {"type": "Stack", "id": stack_id}}),
            self._request("GetPermission", {"target": {"type": "Server", "id": server_id}}),
            self._request("GetServer", {"server": server_id}),
            self._request("ListServers", {"query": {"names": [server_id], "templates": "Include"}, "limit": 0}),
            self._request("ListStackServices", {"stack": stack_id}),
            self._request("ListContainers", {"server": server_id}),
        )
        if not _permission(stack_permission) or not _permission(server_permission):
            raise StackTerminalError("terminal_permission_denied", 403)
        if (not isinstance(server, dict) or _object_id(server.get("_id")) != server_id
                or server.get("template") is not False or not isinstance(server.get("config"), dict)):
            raise StackTerminalError("terminal_server_unavailable", 409)
        matches = [row for row in listing if isinstance(row, dict) and _object_id(row.get("id")) == server_id] if isinstance(listing, list) else []
        info = matches[0].get("info") if len(matches) == 1 else None
        available = (server["config"].get("enabled") is True and isinstance(info, dict)
                     and info.get("state") == "Ok" and info.get("container_terminals_disabled") is False)
        if not isinstance(services, list) or not isinstance(containers, list) or len(services) > 1000:
            raise StackTerminalError("terminal_invalid_response")
        present = {}
        for row in containers:
            if not isinstance(row, dict):
                continue
            identifier = row.get("id")
            if isinstance(identifier, str) and _CONTAINER.fullmatch(identifier):
                present.setdefault(identifier, []).append(row)
        result, names = [], set()
        for row in services:
            if (not isinstance(row, dict) or _object_id(row.get("stack_id")) != stack_id
                    or not isinstance(row.get("service"), str) or not _SERVICE.fullmatch(row["service"])
                    or row["service"] in names):
                raise StackTerminalError("terminal_invalid_response")
            names.add(row["service"])
            container = row.get("container")
            identifier = container.get("id") if isinstance(container, dict) else None
            current = present.get(identifier, []) if isinstance(identifier, str) else []
            exact = (isinstance(identifier, str) and _CONTAINER.fullmatch(identifier) is not None
                     and _object_id(container.get("server_id")) == server_id and len(current) == 1
                     and _object_id(current[0].get("server_id")) == server_id and row.get("swarm_service") is None)
            state = current[0].get("state") if exact else "unknown"
            state = state if state in ("running", "paused", "exited", "created", "restarting", "dead", "stopping", "removing") else "unknown"
            result.append({"service": row["service"], "server_id": server_id,
                           "container_id": identifier if exact else None,
                           "name": _text(current[0].get("name")) if exact else None,
                           "state": state, "can_open": bool(available and exact and state == "running")})
        return {"stack_id": stack_id, "server_id": server_id, "services": result, "shells": ["sh", "bash"]}

    @staticmethod
    def chosen(snapshot, command):
        matches = [row for row in snapshot["services"] if row["service"] == command.service]
        if (snapshot["server_id"] != command.server_id or len(matches) != 1
                or matches[0]["container_id"] != command.container_id or not matches[0]["can_open"]):
            raise StackTerminalError("terminal_service_changed", 409)

    async def container_snapshot(self, server_id, container_id):
        """Re-resolve one full container ID without relying on stack membership."""
        identity = await self._request()
        if (not isinstance(identity, dict) or identity.get("enabled") is not True
                or _text(identity.get("username")) is None):
            raise StackTerminalError("terminal_permission_denied", 403)
        permission, server, listing, containers = await asyncio.gather(
            self._request("GetPermission", {"target": {"type": "Server", "id": server_id}}),
            self._request("GetServer", {"server": server_id}),
            self._request("ListServers", {"query": {"names": [server_id], "templates": "Include"}, "limit": 0}),
            self._request("ListContainers", {"server": server_id}),
        )
        if not _permission(permission):
            raise StackTerminalError("terminal_permission_denied", 403)
        if (not isinstance(server, dict) or _object_id(server.get("_id")) != server_id
                or server.get("template") is not False or not isinstance(server.get("config"), dict)):
            raise StackTerminalError("terminal_server_unavailable", 409)
        if not isinstance(containers, list) or not isinstance(listing, list):
            raise StackTerminalError("terminal_invalid_response")
        matches = [row for row in listing if isinstance(row, dict) and _object_id(row.get("id")) == server_id]
        info = matches[0].get("info") if len(matches) == 1 else None
        current = [row for row in containers if isinstance(row, dict) and row.get("id") == container_id]
        if len(current) != 1 or _object_id(current[0].get("server_id")) != server_id:
            raise StackTerminalError("terminal_container_unavailable", 409)
        state = current[0].get("state")
        state = state if state in ("running", "paused", "exited", "created", "restarting", "dead", "stopping", "removing") else "unknown"
        available = bool(server["config"].get("enabled") is True and isinstance(info, dict)
                         and info.get("state") == "Ok" and info.get("container_terminals_disabled") is False
                         and state == "running")
        # Docker attach requires Read + Terminal in Core too. Specific Attach
        # permission controls linking resources and is unrelated to terminal IO.
        options = {mode: available for mode in ("sh", "bash", "attach", "custom")}
        return {"server_id": server_id, "container_id": container_id, "name": _text(current[0].get("name")),
                "state": state, "can_open": any(options.values()), "shells": list(options), "options": options}

    @staticmethod
    def chosen_container(snapshot, command, server_id, container_id):
        if (snapshot["server_id"] != server_id or snapshot["container_id"] != container_id
                or snapshot.get("options", {}).get(command.shell) is not True):
            raise StackTerminalError("terminal_container_unavailable", 409)

    async def validate_grant_target(self, grant):
        if grant.stack_id is not None:
            self.chosen(await self.snapshot(grant.stack_id), grant.command)
        else:
            self.chosen_container(await self.container_snapshot(grant.server_id, grant.container_id),
                                  grant.command, grant.server_id, grant.container_id)

    def _issue(self, request, user, stack_id, server_id, container_id, command):
        now = monotonic()
        self.grants = {token: grant for token, grant in self.grants.items() if grant.expires > now}
        if len(self.grants) >= 128 or sum(grant.user == user for grant in self.grants.values()) >= 8:
            raise StackTerminalError("terminal_limit_reached", 429)
        token = secrets.token_urlsafe(32)
        grant = _Grant(user, _binding(request), stack_id, command, now + _GRANT_SECONDS, server_id, container_id)
        self.grants[token] = grant
        return {"session_id": grant.session_id, "grant": token, "expires_in": _GRANT_SECONDS}

    def issue(self, request, user, stack_id, command):
        return self._issue(request, user, stack_id, command.server_id, command.container_id, command)

    def issue_container(self, request, user, server_id, container_id, command):
        return self._issue(request, user, None, server_id, container_id, command)

    def _consume(self, request, user, stack_id, token, *, server_id=None, container_id=None):
        grant = self.grants.get(token) if isinstance(token, str) and _GRANT.fullmatch(token) else None
        if (grant is None or grant.expires <= monotonic() or grant.user != user or grant.stack_id != stack_id
                or (stack_id is None and (grant.server_id != server_id or grant.container_id != container_id))
                or not hmac.compare_digest(grant.binding, _binding(request))):
            raise StackTerminalError("terminal_grant_invalid", 403)
        del self.grants[token]
        return grant

    def consume(self, request, user, stack_id, token):
        return self._consume(request, user, stack_id, token)

    def consume_container(self, request, user, server_id, container_id, token):
        return self._consume(request, user, None, token, server_id=server_id, container_id=container_id)

    async def delete_owned(self, target, terminal):
        # Both values were constructed here, never supplied as arbitrary RPCs.
        with suppress(StackTerminalError, TimeoutError):
            async with asyncio.timeout(10):
                await self._request("DeleteTerminal", {"target": target, "terminal": terminal}, write=True)

    @staticmethod
    def terminal_target(grant):
        return {"type": "Container", "params": {"server": grant.server_id, "container": grant.container_id}}

    @staticmethod
    def session_payload(grant):
        payload = {"kind": "stack_terminal_session" if grant.stack_id is not None else "container_terminal_session",
                   "server_id": grant.server_id, "container_id": grant.container_id,
                   **grant.command.model_dump(mode="json", exclude={"request_id"})}
        if grant.stack_id is not None:
            payload["stack_id"] = grant.stack_id
        return payload

    async def forward(self, socket, request, user, grant, authenticator, scope):
        if self.action_store is None:
            raise StackTerminalError("action_state_not_configured", 503)
        if len(self.active) >= 16 or sum(value == user for value in self.active.values()) >= 4:
            raise StackTerminalError("terminal_limit_reached", 429)
        async with asyncio.timeout(25):
            await self.validate_grant_target(grant)
        # Preflight yields: another connection can acquire a slot meanwhile.
        if len(self.active) >= 16 or sum(value == user for value in self.active.values()) >= 4:
            raise StackTerminalError("terminal_limit_reached", 429)
        payload = self.session_payload(grant)
        request_id = str(grant.command.request_id)
        try:
            cached = self.action_store.reserve(scope, request_id, payload)
            if cached is not None:
                raise StackTerminalError("terminal_already_requested", 409)
        except ActionStoreError as error:
            raise StackTerminalError("terminal_already_requested" if error.code == "action_already_requested" else error.code,
                                     error.status_code) from None
        self.active[grant.session_id] = user
        LOGGER.info("Stack terminal requested user=%s stack_id=%s server_id=%s session_id=%s request_id=%s",
                    user, grant.stack_id, grant.server_id, grant.session_id, request_id)
        terminal = "pve-komodo-" + uuid.UUID(grant.session_id).hex
        target = self.terminal_target(grant)
        # Core's serde_qs parser needs literal brackets for nested query keys;
        # encoded brackets are interpreted as part of an ordinary field name.
        query_params = {"target[type]": target["type"], "terminal": terminal,
                        **{"target[params][" + key + "]": value for key, value in target["params"].items()}}
        query = urlencode(query_params, safe="[]")
        url = ("wss" if self.origin.startswith("https:") else "ws") + self.origin[self.origin.index(":"):] + "/ws/terminal?" + query
        upstream, cleanup, tasks, creation = None, False, [], None
        try:
            # Core sends LOGGED_IN before its optional WS initialization has
            # completed. Explicit creation gives an acknowledgement before
            # exposing a ready socket, avoiding Delete racing a normal create.
            cleanup = True
            mode = "attach" if grant.command.shell == "attach" else "exec"
            command = (None if mode == "attach" or grant.command.shell == "default" else grant.command.command if grant.command.shell == "custom"
                       else grant.command.shell)
            creation = asyncio.create_task(self._request("CreateTerminal", {
                "name": terminal, "target": target, "command": command,
                "mode": mode, "recreate": "Never",
            }, write=True))
            with anyio.CancelScope(shield=True):
                created = await asyncio.shield(creation)
            if not isinstance(created, dict) or created.get("name") != terminal or created.get("target") != target:
                raise StackTerminalError("terminal_creation_unconfirmed")
            async with asyncio.timeout(15):
                upstream = await self.connect(url, self.verify)
                await upstream.send(json.dumps({"type": "ApiKeys", "params": {"key": self.key, "secret": self.secret}}))
                if await upstream.recv() != "LOGGED_IN":
                    raise StackTerminalError("terminal_connection_failed")
            await socket.send_json({"type": "ready", "session_id": grant.session_id})
            started = last_auth = monotonic()
            activity = [started]

            async def browser_input():
                while True:
                    raw = await socket.receive()
                    if raw["type"] == "websocket.disconnect":
                        return
                    data = raw.get("text")
                    if not isinstance(data, str) or len(data) > _MAX_INPUT * 2:
                        raise StackTerminalError("invalid_terminal_message", 400)
                    try:
                        message = json.loads(data)
                        if not isinstance(message, dict):
                            raise ValueError()
                        if message == {"type": "close"}:
                            return
                        if set(message) == {"type", "data"} and message["type"] == "data" and isinstance(message["data"], str):
                            content = message["data"].encode("utf-8")
                            if not content or len(content) > _MAX_INPUT:
                                raise ValueError()
                            frame = content + b"\x01"
                        elif set(message) == {"type", "rows", "cols"} and message["type"] == "resize":
                            if type(message["rows"]) is not int or type(message["cols"]) is not int or not 1 <= message["rows"] <= 500 or not 1 <= message["cols"] <= 1000:
                                raise ValueError()
                            frame = json.dumps({"rows": message["rows"], "cols": message["cols"]}, separators=(",", ":")).encode() + b"\xff"
                        else:
                            raise ValueError()
                    except (ValueError, UnicodeError):
                        raise StackTerminalError("invalid_terminal_message", 400) from None
                    async with asyncio.timeout(10):
                        await upstream.send(frame)
                    activity[0] = monotonic()

            async def terminal_output():
                while True:
                    chunk = await upstream.recv()
                    if not isinstance(chunk, bytes):
                        # Core error text includes infrastructure details. PTY
                        # output is binary; only fixed errors cross this bridge.
                        raise StackTerminalError("terminal_stream_closed")
                    if len(chunk) > 1_048_576:
                        raise StackTerminalError("terminal_output_too_large")
                    async with asyncio.timeout(10):
                        await socket.send_bytes(chunk)
                    activity[0] = monotonic()

            async def validate_session():
                nonlocal last_auth
                while True:
                    await asyncio.sleep(_TICK_SECONDS)
                    now = monotonic()
                    if now - started >= _LIFETIME_SECONDS:
                        raise StackTerminalError("terminal_session_expired", 403)
                    if now - activity[0] >= _IDLE_SECONDS:
                        raise StackTerminalError("terminal_session_idle", 403)
                    if now - last_auth >= _REVALIDATE_SECONDS:
                        async with asyncio.timeout(25):
                            if await authenticator.authenticate(request) != user:
                                raise StackTerminalError("terminal_session_expired", 403)
                            await self.validate_grant_target(grant)
                        last_auth = monotonic()

            tasks = [asyncio.create_task(call()) for call in (browser_input, terminal_output, validate_session)]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            # ASGI disconnect/reload may cancel an AnyIO request scope. A bare
            # asyncio.shield only protects a child task, not these cleanup
            # awaits or the durable receipt, so shield the bounded finalizer.
            with anyio.CancelScope(shield=True):
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    if creation is not None:
                        # Await the same bounded request, never issue a retry,
                        # even if the ASGI task was cancelled while creating.
                        with suppress(Exception):
                            await asyncio.shield(creation)
                    if upstream is not None:
                        with suppress(Exception):
                            async with asyncio.timeout(4):
                                await upstream.close()
                    if cleanup:
                        task = asyncio.create_task(self.delete_owned(target, terminal))
                        self.cleanup_tasks.add(task)
                        task.add_done_callback(self.cleanup_tasks.discard)
                        with suppress(Exception):
                            await asyncio.shield(task)
                finally:
                    self.active.pop(grant.session_id, None)
                    with suppress(ActionStoreError):
                        self.action_store.finish(scope, request_id, 200, {"detail": "terminal_session_requested"})
                    LOGGER.info("Stack terminal forwarding closed user=%s stack_id=%s session_id=%s",
                                user, grant.stack_id, grant.session_id)


def register_stack_terminal_routes(application, config, authenticator, komodo, csrf, action_store, *, ws_connect=None):
    terminals = StackTerminals(komodo, action_store, ws_connect=ws_connect)

    def validate_target(request, stack_id):
        if _ID.fullmatch(stack_id) is None or request.query_params:
            raise HTTPException(422, "invalid_terminal_target")

    async def authorize(request, stack_id, *, write=False):
        user = await authenticator.authenticate(request)
        validate_target(request, stack_id)
        if write:
            headers = request.headers.getlist("x-pve-komodo-csrf")
            csrf.verify(request, user, headers[0] if len(headers) == 1 else "")
        return user

    async def authorize_container(request, server_id, container_id, *, write=False):
        user = await authenticator.authenticate(request)
        if _ID.fullmatch(server_id) is None or _CONTAINER.fullmatch(container_id) is None or request.query_params:
            raise HTTPException(422, "invalid_terminal_target")
        if write:
            headers = request.headers.getlist("x-pve-komodo-csrf")
            csrf.verify(request, user, headers[0] if len(headers) == 1 else "")
        return user

    @application.get("/pve-komodo/api/stacks/{stack_id}/terminal-services")
    async def services(request: Request, stack_id: str):
        try:
            async with asyncio.timeout(45):
                user = await authorize(request, stack_id)
                result = await terminals.snapshot(stack_id)
                return {**result, "csrf_token": csrf.issue(request, user)}
        except StackTerminalError as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "terminal_preflight_timeout") from None

    @application.post("/pve-komodo/api/stacks/{stack_id}/terminals")
    async def issue(request: Request, stack_id: str):
        try:
            async with asyncio.timeout(45):
                user = await authorize(request, stack_id, write=True)
                if action_store is None:
                    raise HTTPException(503, "action_state_not_configured")
                if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise HTTPException(415, "json_required")
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 4096:
                        raise HTTPException(413, "terminal_request_too_large")
                try:
                    command = TerminalRequest.model_validate_json(bytes(body))
                except ValidationError:
                    raise HTTPException(422, "invalid_terminal_request") from None
                terminals.chosen(await terminals.snapshot(stack_id), command)
                return terminals.issue(request, user, stack_id, command)
        except StackTerminalError as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "terminal_preflight_timeout") from None

    container_base = "/pve-komodo/api/servers/{server_id}/containers/{container_id}"

    @application.get(container_base + "/terminal-services")
    async def container_services(request: Request, server_id: str, container_id: str):
        try:
            async with asyncio.timeout(45):
                user = await authorize_container(request, server_id, container_id)
                result = await terminals.container_snapshot(server_id, container_id)
                return {**result, "csrf_token": csrf.issue(request, user)}
        except StackTerminalError as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "terminal_preflight_timeout") from None

    @application.post(container_base + "/terminals")
    async def issue_container(request: Request, server_id: str, container_id: str):
        try:
            async with asyncio.timeout(45):
                user = await authorize_container(request, server_id, container_id, write=True)
                if action_store is None:
                    raise HTTPException(503, "action_state_not_configured")
                if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise HTTPException(415, "json_required")
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 12_288:
                        raise HTTPException(413, "terminal_request_too_large")
                try:
                    command = ContainerTerminalRequest.model_validate_json(bytes(body))
                except ValidationError:
                    raise HTTPException(422, "invalid_terminal_request") from None
                terminals.chosen_container(await terminals.container_snapshot(server_id, container_id),
                                          command, server_id, container_id)
                return terminals.issue_container(request, user, server_id, container_id, command)
        except StackTerminalError as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "terminal_preflight_timeout") from None

    async def terminal_socket(socket, *, stack_id=None, server_id=None, container_id=None):
        accepted, close_code = False, 1000
        request = Request({**socket.scope, "type": "http", "scheme": "https" if socket.scope.get("scheme") == "wss" else "http"})
        try:
            if len(request.headers.getlist("origin")) != 1:
                raise HTTPException(403, "invalid_write_origin")
            verify_write_origin(request)
            async with asyncio.timeout(20):
                user = (await authorize(request, stack_id) if stack_id is not None
                        else await authorize_container(request, server_id, container_id))
            await socket.accept()
            accepted = True
            async with asyncio.timeout(5):
                first = await socket.receive_text()
            if len(first) > 8192:
                raise StackTerminalError("invalid_terminal_message", 400)
            try:
                message = json.loads(first)
            except ValueError:
                raise StackTerminalError("invalid_terminal_message", 400) from None
            if not isinstance(message, dict) or set(message) != {"type", "grant", "csrf_token"} or message["type"] != "open":
                raise StackTerminalError("invalid_terminal_message", 400)
            csrf.verify(request, user, message["csrf_token"])
            grant = (terminals.consume(request, user, stack_id, message["grant"]) if stack_id is not None
                     else terminals.consume_container(request, user, server_id, container_id, message["grant"]))
            scope = hashlib.sha256((user + "\0" + config.upstream + "\0" + (config.komodo_url or "")).encode()).hexdigest()
            await terminals.forward(socket, request, user, grant, authenticator, scope)
        except (StackTerminalError, HTTPException) as error:
            close_code = 1008
            LOGGER.warning("Terminal rejected stack_id=%s server_id=%s code=%s", stack_id, server_id,
                           error.code if isinstance(error, StackTerminalError) else "terminal_session_rejected")
            if accepted:
                with suppress(Exception):
                    async with asyncio.timeout(3):
                        await socket.send_json({"type": "error", "code": error.code if isinstance(error, StackTerminalError) else "terminal_session_rejected"})
        except WebSocketDisconnect:
            pass
        except Exception as error:
            close_code = 1011
            LOGGER.warning("Terminal failed stack_id=%s server_id=%s exception_type=%s", stack_id, server_id, type(error).__name__)
            if accepted:
                with suppress(Exception):
                    async with asyncio.timeout(3):
                        await socket.send_json({"type": "error", "code": "terminal_connection_failed"})
        finally:
            with suppress(Exception):
                async with asyncio.timeout(3):
                    await socket.close(code=close_code)

    @application.websocket("/pve-komodo/api/stacks/{stack_id}/terminals/ws")
    async def terminal(socket: WebSocket, stack_id: str):
        await terminal_socket(socket, stack_id=stack_id)

    @application.websocket(container_base + "/terminals/ws")
    async def container_terminal(socket: WebSocket, server_id: str, container_id: str):
        await terminal_socket(socket, server_id=server_id, container_id=container_id)

    return terminals


class ServerTerminals(StackTerminals):
    """Guest-identity-pinned host terminals using the shared PTY bridge."""

    def __init__(self, komodo, action_store, binding_service, *, ws_connect=None):
        super().__init__(komodo, action_store, ws_connect=ws_connect)
        self.binding_service = binding_service

    async def server_snapshot(self, server_id):
        identity = await self._request()
        if (not isinstance(identity, dict) or identity.get("enabled") is not True
                or _text(identity.get("username")) is None):
            raise StackTerminalError("terminal_permission_denied", 403)
        permission, server, listing = await asyncio.gather(
            self._request("GetPermission", {"target": {"type": "Server", "id": server_id}}),
            self._request("GetServer", {"server": server_id}),
            self._request("ListServers", {"query": {"names": [server_id], "templates": "Include"}, "limit": 0}),
        )
        if not _permission(permission):
            raise StackTerminalError("terminal_permission_denied", 403)
        if (not isinstance(server, dict) or _object_id(server.get("_id")) != server_id
                or server.get("template") is not False or not isinstance(server.get("config"), dict)
                or _text(server.get("name")) is None):
            raise StackTerminalError("terminal_server_unavailable", 409)
        if not isinstance(listing, list):
            raise StackTerminalError("terminal_invalid_response")
        matches = [row for row in listing if isinstance(row, dict) and _object_id(row.get("id")) == server_id]
        info = matches[0].get("info") if len(matches) == 1 else None
        available = bool(server["config"].get("enabled") is True and isinstance(info, dict)
                         and info.get("state") == "Ok" and info.get("terminals_disabled") is False)
        options = {mode: available for mode in ("default", "sh", "bash", "custom")}
        return {"server_id": server_id, "server_name": _text(server["name"]),
                "state": info.get("state") if isinstance(info, dict) and info.get("state") in ("Ok", "NotOk", "Disabled", "Unknown") else "Unknown",
                "can_open": available, "shells": list(options), "options": options}

    async def bound_snapshot(self, request, guest_type, vmid, *, command=None):
        try:
            binding = await self.binding_service.resolve(request, guest_type, vmid,
                expected_server_id=command.server_id if command is not None else None,
                expected_revision=command.binding_revision if command is not None else None)
            result = await self.server_snapshot(binding["server_id"])
            await self.binding_service.resolve(request, guest_type, vmid,
                expected_server_id=binding["server_id"], expected_revision=binding["binding_revision"])
        except (GuestLinkError, KomodoActionError) as error:
            raise StackTerminalError(error.code, error.status_code) from None
        return {**result, "binding_revision": binding["binding_revision"]}

    async def validate_grant_target(self, grant):
        snapshot = await self.bound_snapshot(grant.request, grant.guest_type, grant.vmid, command=grant.command)
        self.chosen_server(snapshot, grant.command)

    @staticmethod
    def chosen_server(snapshot, command):
        if (snapshot["server_id"] != command.server_id or snapshot["binding_revision"] != command.binding_revision
                or snapshot.get("options", {}).get(command.shell) is not True):
            raise StackTerminalError("terminal_server_unavailable", 409)

    def issue_server(self, request, user, guest_type, vmid, command):
        result = self._issue(request, user, None, command.server_id, None, command)
        grant = self.grants[result["grant"]]
        self.grants[result["grant"]] = _ServerGrant(**grant.__dict__, guest_type=guest_type, vmid=vmid, request=request)
        return result

    def consume_server(self, request, user, guest_type, vmid, token):
        grant = self.grants.get(token) if isinstance(token, str) and _GRANT.fullmatch(token) else None
        if not isinstance(grant, _ServerGrant) or grant.guest_type != guest_type or grant.vmid != vmid:
            raise StackTerminalError("terminal_grant_invalid", 403)
        grant = self._consume(request, user, None, token, server_id=grant.server_id, container_id=None)
        grant.request = request
        return grant

    @staticmethod
    def terminal_target(grant):
        return {"type": "Server", "params": {"server": grant.server_id}}

    @staticmethod
    def session_payload(grant):
        return {"kind": "guest_server_terminal_session", "guest_type": grant.guest_type, "vmid": grant.vmid,
                **grant.command.model_dump(mode="json", exclude={"request_id"})}


def register_server_terminal_routes(application, config, authenticator, komodo, csrf, guest_store,
                                    action_store, *, transport=None, ws_connect=None):
    service = ServerAccess(guest_store, PveGuestClient(
        config.upstream, verify=config.tls_context(), transport=transport))
    terminals = ServerTerminals(komodo, action_store, service, ws_connect=ws_connect)
    base = "/pve-komodo/api/guests/{guest_type}/{vmid}"

    async def authorize(request, guest_type, vmid, *, write=False):
        user = await authenticator.authenticate(request)
        if request.query_params or not valid_server_target(request, guest_type, vmid):
            raise HTTPException(422, "invalid_terminal_target")
        if write:
            headers = request.headers.getlist("x-pve-komodo-csrf")
            csrf.verify(request, user, headers[0] if len(headers) == 1 else "")
        return user

    @application.get(base + "/terminal-services")
    async def server_services(request: Request, guest_type: str, vmid: str):
        try:
            async with asyncio.timeout(45):
                user = await authorize(request, guest_type, vmid)
                result = await terminals.bound_snapshot(request, guest_type, access_identifier(guest_type, vmid))
                return {**result, "csrf_token": csrf.issue(request, user)}
        except StackTerminalError as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "terminal_preflight_timeout") from None

    @application.post(base + "/terminals")
    async def issue_server(request: Request, guest_type: str, vmid: str):
        try:
            async with asyncio.timeout(45):
                user = await authorize(request, guest_type, vmid, write=True)
                if action_store is None:
                    raise HTTPException(503, "action_state_not_configured")
                if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise HTTPException(415, "json_required")
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 12_288:
                        raise HTTPException(413, "terminal_request_too_large")
                try:
                    command = ServerTerminalRequest.model_validate_json(bytes(body))
                except ValidationError:
                    raise HTTPException(422, "invalid_terminal_request") from None
                snapshot = await terminals.bound_snapshot(request, guest_type, access_identifier(guest_type, vmid), command=command)
                terminals.chosen_server(snapshot, command)
                return terminals.issue_server(request, user, guest_type, access_identifier(guest_type, vmid), command)
        except StackTerminalError as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "terminal_preflight_timeout") from None

    @application.websocket(base + "/terminals/ws")
    async def server_terminal(socket: WebSocket, guest_type: str, vmid: str):
        accepted, close_code = False, 1000
        request = Request({**socket.scope, "type": "http", "scheme": "https" if socket.scope.get("scheme") == "wss" else "http"})
        try:
            if len(request.headers.getlist("origin")) != 1:
                raise HTTPException(403, "invalid_write_origin")
            verify_write_origin(request)
            async with asyncio.timeout(20):
                user = await authorize(request, guest_type, vmid)
            await socket.accept()
            accepted = True
            async with asyncio.timeout(5):
                first = await socket.receive_text()
            if len(first) > 8192:
                raise StackTerminalError("invalid_terminal_message", 400)
            try:
                message = json.loads(first)
            except ValueError:
                raise StackTerminalError("invalid_terminal_message", 400) from None
            if not isinstance(message, dict) or set(message) != {"type", "grant", "csrf_token"} or message["type"] != "open":
                raise StackTerminalError("invalid_terminal_message", 400)
            csrf.verify(request, user, message["csrf_token"])
            grant = terminals.consume_server(request, user, guest_type, access_identifier(guest_type, vmid), message["grant"])
            scope = hashlib.sha256((user + "\0" + config.upstream + "\0" + (config.komodo_url or "")).encode()).hexdigest()
            await terminals.forward(socket, request, user, grant, authenticator, scope)
        except (StackTerminalError, HTTPException) as error:
            close_code = 1008
            LOGGER.warning("Server terminal rejected guest_type=%s vmid=%s code=%s", guest_type, vmid,
                           error.code if isinstance(error, StackTerminalError) else "terminal_session_rejected")
            if accepted:
                with suppress(Exception):
                    async with asyncio.timeout(3):
                        await socket.send_json({"type": "error", "code": error.code if isinstance(error, StackTerminalError) else "terminal_session_rejected"})
        except WebSocketDisconnect:
            pass
        except Exception as error:
            close_code = 1011
            LOGGER.warning("Server terminal failed guest_type=%s vmid=%s exception_type=%s", guest_type, vmid, type(error).__name__)
            if accepted:
                with suppress(Exception):
                    async with asyncio.timeout(3):
                        await socket.send_json({"type": "error", "code": "terminal_connection_failed"})
        finally:
            with suppress(Exception):
                async with asyncio.timeout(3):
                    await socket.close(code=close_code)

    @application.get("/pve-komodo/api/servers/{server_id}/terminal-services")
    async def direct_server_services(request: Request, server_id: str):
        return await server_services(request, DIRECT_SERVER, server_id)

    @application.post("/pve-komodo/api/servers/{server_id}/terminals")
    async def direct_issue_server(request: Request, server_id: str):
        return await issue_server(request, DIRECT_SERVER, server_id)

    @application.websocket("/pve-komodo/api/servers/{server_id}/terminals/ws")
    async def direct_server_terminal(socket: WebSocket, server_id: str):
        await server_terminal(socket, DIRECT_SERVER, server_id)

    return terminals
