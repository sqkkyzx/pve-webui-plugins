"""PVE-authenticated CRUD for shared local site links; never fetch site URLs."""

import json
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from plugins.sites.site_store import SiteError, SitesStore


class SiteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr
    url: StrictStr


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: StrictInt = Field(ge=1, le=9_007_199_254_740_991)


class UpdateSiteRequest(SiteRequest, RevisionRequest):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("invalid_json_number")


async def _command(request, model):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "json_required")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 32768:
            raise HTTPException(413, "site_request_too_large")
        body.extend(chunk)
    try:
        payload = json.loads(bytes(body), object_pairs_hook=_object, parse_constant=_constant)
        return model.model_validate(payload)
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
        raise HTTPException(422, "invalid_site_request") from None


def register_site_routes(application, settings, authenticator, csrf):
    path = settings.site_state_file
    if path is None:
        actions = settings.action_state_file
        path = (":memory:" if actions == ":memory:" else str(Path(actions).with_name("sites.sqlite3"))) if actions else str(
            Path.cwd() / "data" / "sites.sqlite3")
    store = SitesStore(path, pve_origin=settings.upstream)

    async def authorize(request, *, write=False):
        user = await authenticator.authenticate(request)
        if request.query_params:
            raise HTTPException(422, "invalid_site_query")
        if write:
            headers = request.headers.getlist("x-pve-plugins-csrf")
            csrf.verify(request, user, headers[0] if len(headers) == 1 else "")
        return user

    @application.get("/pve-plugins/api/sites")
    async def list_sites(request: Request):
        user = await authorize(request)
        try:
            return {"user": user, "sites": store.list(), "csrf_token": csrf.issue(request, user)}
        except SiteError as error:
            raise HTTPException(error.status_code, error.code) from None

    @application.post("/pve-plugins/api/sites")
    async def create_site(request: Request):
        await authorize(request, write=True)
        value = await _command(request, SiteRequest)
        try:
            return {"site": store.create(value.name, value.url)}
        except SiteError as error:
            raise HTTPException(error.status_code, error.code) from None

    @application.patch("/pve-plugins/api/sites/{site_id}")
    async def update_site(request: Request, site_id: str):
        await authorize(request, write=True)
        value = await _command(request, UpdateSiteRequest)
        try:
            return {"site": store.update(site_id, value.name, value.url, expected_revision=value.expected_revision)}
        except SiteError as error:
            raise HTTPException(error.status_code, error.code) from None

    @application.delete("/pve-plugins/api/sites/{site_id}")
    async def delete_site(request: Request, site_id: str):
        await authorize(request, write=True)
        value = await _command(request, RevisionRequest)
        try:
            return store.delete(site_id, expected_revision=value.expected_revision)
        except SiteError as error:
            raise HTTPException(error.status_code, error.code) from None

    return store
