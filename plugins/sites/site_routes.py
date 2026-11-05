"""PVE-authenticated shared site links and bounded registered-site icon reads."""

import json
import re
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from plugins.sites.site_store import SiteError, SitesStore
from plugins.sites.icon_discovery import SiteIconDiscovery


class SiteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr
    url: StrictStr
    enabled: StrictBool = True
    alias: StrictStr = ""
    icon: dict | None = None


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: StrictInt = Field(ge=1, le=9_007_199_254_740_991)


class UpdateSiteRequest(RevisionRequest):
    name: StrictStr = ""
    url: StrictStr = ""
    enabled: StrictBool = True
    alias: StrictStr = ""
    icon: dict | None = None


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


def register_site_routes(application, settings, authenticator, csrf, *, icon_discovery=None):
    path = settings.site_state_file
    if path is None:
        actions = settings.action_state_file
        path = (":memory:" if actions == ":memory:" else str(Path(actions).with_name("sites.sqlite3"))) if actions else str(
            Path.cwd() / "data" / "sites.sqlite3")
    store = SitesStore(path, pve_origin=settings.upstream)
    icons = icon_discovery if icon_discovery is not None else SiteIconDiscovery()

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

    def stored_site(site_id):
        if re.fullmatch(r"[0-9a-f]{32}", site_id) is None:
            raise SiteError("invalid_site_id", 422)
        site = next((item for item in store.list() if item["id"] == site_id), None)
        if site is None:
            raise SiteError("site_not_found", 404)
        return site

    @application.get("/pve-plugins/api/sites/{site_id}/icon")
    async def get_site_icon(request: Request, site_id: str):
        await authorize(request)
        try:
            site = stored_site(site_id)
            if not site["enabled"]:
                raise SiteError("site_disabled", 404)
            if site["icon"] is not None:
                raise SiteError("site_icon_is_manual", 409)
            icon = await icons.discover(site["url"])
            current = stored_site(site_id)
            if current != site:
                raise SiteError("site_revision_conflict", 409)
            return {"site_id": site_id, "revision": site["revision"], "icon_url": icon}
        except SiteError as error:
            raise HTTPException(error.status_code, error.code) from None

    @application.post("/pve-plugins/api/sites")
    async def create_site(request: Request):
        await authorize(request, write=True)
        value = await _command(request, SiteRequest)
        try:
            return {"site": store.create(**value.model_dump())}
        except SiteError as error:
            raise HTTPException(error.status_code, error.code) from None

    @application.patch("/pve-plugins/api/sites/{site_id}")
    async def update_site(request: Request, site_id: str):
        await authorize(request, write=True)
        value = await _command(request, UpdateSiteRequest)
        try:
            return {"site": store.update(site_id, **value.model_dump(exclude_unset=True))}
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
