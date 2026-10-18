"""PVE-authenticated SafeLine pages and explicit, CSRF-protected resource edits."""

import json
import re

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from plugins.safeline.client import SafeLineClient
from plugins.safeline.connection import Connection, SafeLineError
from plugins.safeline.editing import EditCommand, ResourceEditor
from plugins.safeline.service import KINDS, now


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: StrictInt = Field(ge=0, le=9_007_199_254_740_991)


class ConnectionCommand(Revision):
    url: StrictStr = Field(max_length=512)
    token: StrictStr = Field(default="", max_length=4096)
    tls_verify: StrictBool = True
    tls_server_name: StrictStr = Field(default="", max_length=253)
    ca_pem: StrictStr = Field(default="", max_length=65536)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(value):
    raise ValueError


async def command(request, model):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "json_required")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 100_000:
            raise HTTPException(413, "safeline_request_too_large")
        body.extend(chunk)
    try:
        return model.model_validate(json.loads(body, object_pairs_hook=_pairs, parse_constant=_constant))
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
        # Pydantic's default validation error includes submitted passwords.
        raise HTTPException(422, "invalid_safeline_request") from None


def query(request, allowed):
    values = dict(request.query_params)
    if not set(values) <= allowed or len(values) != len(request.query_params.multi_items()):
        raise HTTPException(422, "invalid_safeline_query")
    return values


def integer(value, minimum, maximum):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,16}", value) or not minimum <= int(value) <= maximum:
        raise HTTPException(422, "invalid_safeline_query")
    return int(value)


def register_routes(context, store, service):
    application, authenticator, csrf = context.application, context.authenticator, context.csrf
    base = "/pve-plugins/api/safeline"
    editor = ResourceEditor(service)

    async def authorize(request, *, write=False):
        user = await authenticator.authenticate(request)
        if write:
            headers = request.headers.getlist("x-pve-plugins-csrf")
            csrf.verify(request, user, headers[0] if len(headers) == 1 else "")
        return user

    def connection_response(request, user):
        config, revision, source = store.load()
        return {"user": user, "configured": config is not None, "revision": revision, "source": source,
                "connection": config.public() if config else {"url": "", "token_configured": False, "tls_verify": True, "tls_server_name": "", "ca_pem": ""},
                "csrf_token": csrf.issue(request, user)}

    def draft(value):
        current, revision, source = store.load()
        if revision != value.expected_revision:
            raise SafeLineError("safeline_connection_changed", 409)
        token = value.token
        if not token:
            if not current or current.url != value.url.rstrip("/") or current.tls_server_name != value.tls_server_name:
                raise SafeLineError("safeline_token_required", 422)
            token = current.token
        return Connection(value.url, token, value.tls_verify, value.tls_server_name, value.ca_pem)

    @application.get(base + "/connection")
    async def get_connection(request: Request):
        user = await authorize(request)
        query(request, set())
        try:
            return connection_response(request, user)
        except SafeLineError as error:
            raise HTTPException(error.status, error.code) from None

    @application.put(base + "/connection")
    async def put_connection(request: Request):
        user = await authorize(request, write=True)
        query(request, set())
        value = await command(request, ConnectionCommand)
        try:
            async with editor.lock:
                store.save(draft(value), value.expected_revision)
            return connection_response(request, user)
        except SafeLineError as error:
            raise HTTPException(error.status, error.code) from None

    @application.delete(base + "/connection")
    async def delete_connection(request: Request):
        user = await authorize(request, write=True)
        query(request, set())
        value = await command(request, Revision)
        try:
            async with editor.lock:
                store.save(None, value.expected_revision)
            return connection_response(request, user)
        except SafeLineError as error:
            raise HTTPException(error.status, error.code) from None

    @application.post(base + "/connection/test")
    async def test_connection(request: Request):
        user = await authorize(request, write=True)
        query(request, set())
        value = await command(request, ConnectionCommand)
        try:
            result = await SafeLineClient(draft(value), transport=service.transport).status()
            return {"user": user, **result, "updated_at": now()}
        except SafeLineError as error:
            raise HTTPException(error.status, error.code) from None

    @application.get(base + "/status")
    async def status(request: Request):
        user = await authorize(request)
        query(request, set())
        try:
            client, revision = service.client()
            if client.connection is None:
                return {"user": user, "configured": False, "connected": False, "connection_revision": revision}
            try:
                result = await client.status()
            except SafeLineError as error:
                result = {"connected": False, "error": error.code}
            return {"user": user, "configured": True, **result, "connection_revision": revision, "updated_at": now()}
        except SafeLineError as error:
            raise HTTPException(error.status, error.code) from None

    @application.get(base + "/inventory")
    async def inventory(request: Request):
        user = await authorize(request)
        query(request, set())
        try:
            return {"user": user, **await service.inventory()}
        except SafeLineError as error:
            raise HTTPException(error.status, error.code) from None

    @application.get(base + "/resources/{kind}/{identifier}")
    async def detail(request: Request, kind: str, identifier: str):
        user = await authorize(request)
        values = query(request, {"section"})
        section = values.get("section", "summary")
        if kind not in {*KINDS, "policies"} or section not in {"summary", "proxy", "protection"}:
            raise HTTPException(422, "invalid_safeline_resource")
        integer(identifier, 1, 9_007_199_254_740_991)
        try:
            return {"user": user, **await service.detail(kind, identifier, section)}
        except SafeLineError as error:
            raise HTTPException(error.status, error.code) from None

    @application.get(base + "/overview")
    async def overview(request: Request):
        user = await authorize(request)
        values = query(request, {"hours", "preset", "site_id", "utc_offset"})
        hours = integer(values.get("hours", "24"), 1, 168)
        preset = values.get("preset", "last1Day")
        if hours != 24 or "hours" in values and "preset" in values or preset not in {"last1Day", "today", "last7Day", "last30Day"}:
            raise HTTPException(422, "invalid_safeline_query")
        site_id = integer(values["site_id"], 1, 9_007_199_254_740_991) if "site_id" in values else None
        offset = values.get("utc_offset", "0")
        if not re.fullmatch(r"-?[0-9]{1,3}", offset) or not -840 <= int(offset) <= 840:
            raise HTTPException(422, "invalid_safeline_query")
        try:
            return {"user": user, **await service.overview(hours, preset=preset, site_id=site_id, utc_offset=int(offset))}
        except SafeLineError as error:
            raise HTTPException(error.status, error.code) from None

    def edit_section(request, kind, identifier):
        section = query(request, {"section"}).get("section", "settings")
        if kind not in {*KINDS, "policies"} or section not in {"settings", "mode", "switch", "proxy"}:
            raise HTTPException(422, "invalid_safeline_resource")
        integer(identifier, 1, 9_007_199_254_740_991)
        return section

    @application.get(base + "/resources/{kind}/{identifier}/edit")
    async def edit_resource(request: Request, kind: str, identifier: str):
        user = await authorize(request)
        section = edit_section(request, kind, identifier)
        try:
            return {"user": user, **await editor.load(kind, identifier, section)}
        except SafeLineError as error:
            raise HTTPException(error.status, error.code) from None

    @application.put(base + "/resources/{kind}/{identifier}/edit")
    async def save_resource(request: Request, kind: str, identifier: str):
        user = await authorize(request, write=True)
        section = edit_section(request, kind, identifier)
        value = await command(request, EditCommand)
        try:
            return {"user": user, **await editor.save(kind, identifier, section, value, user)}
        except SafeLineError as error:
            raise HTTPException(error.status, error.code) from None

    async def logs(request, *, events):
        user = await authorize(request)
        allowed = {"page", "page_size", "source", "host", "ip", "port", "start", "end"}
        if not events:
            allowed |= {"action", "attack_type", "event_id", "policy_rule_id", "url"}
        values = query(request, allowed)
        source = values.pop("source", "general")
        if source not in {"general", "rule"}:
            raise HTTPException(422, "invalid_safeline_query")
        params = {"page": integer(values.pop("page", "1"), 1, 1_000_000),
                  "page_size": integer(values.pop("page_size", "20"), 1, 100)}
        for key, value in values.items():
            if key in {"start", "end", "policy_rule_id"}:
                params[key] = integer(value, 0, 9_007_199_254_740_991)
            elif not value or len(value) > 1024 or any(ord(c) < 32 for c in value):
                raise HTTPException(422, "invalid_safeline_query")
            else:
                params[key] = value
        if "policy_rule_id" in params and source != "rule":
            raise HTTPException(422, "invalid_safeline_query")
        if "start" in params and "end" in params and params["start"] > params["end"]:
            raise HTTPException(422, "invalid_safeline_query")
        try:
            return {"user": user, **await service.logs(events=events, rule=source == "rule", params=params)}
        except SafeLineError as error:
            raise HTTPException(error.status, error.code) from None

    @application.get(base + "/events")
    async def events(request: Request):
        return await logs(request, events=True)

    @application.get(base + "/records")
    async def records(request: Request):
        return await logs(request, events=False)
