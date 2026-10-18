"""Authenticated local association APIs. PVE and Komodo remain read-only."""

import asyncio
import logging
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError, field_validator

from plugins.komodo.guest_links import GuestLinkError, GuestLinkStore, PveGuestClient, assess_link
from plugins.komodo.komodo import KomodoDashboardError


LOGGER = logging.getLogger("pve_komodo")


class UnlinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_id: StrictStr = Field(pattern=r"^[0-9a-fA-F]{24}$")
    expected_revision: StrictInt = Field(ge=0, le=9_007_199_254_740_991)

    @field_validator("server_id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        return value.lower()


class BindRequest(UnlinkRequest):
    guest_type: Literal["qemu", "lxc"]
    vmid: StrictInt = Field(ge=100, le=999_999_999)


def register_guest_link_routes(application, config, authenticator, komodo, csrf, *, transport=None):
    path = config.guest_links_file
    if path is None and config.action_state_file:
        path = ":memory:" if config.action_state_file == ":memory:" else str(
            Path(config.action_state_file).with_name("guest-links.sqlite3")
        )
    store = GuestLinkStore(path, komodo_origin=config.komodo_url, pve_origin=config.upstream) if path and config.komodo_url else None
    pve = PveGuestClient(config.upstream, verify=config.tls_context(), transport=transport)

    def available_store():
        if store is None:
            raise HTTPException(503, "guest_link_store_not_configured")
        return store

    async def authorize(request, *, write=False):
        user = await authenticator.authenticate(request)
        if request.query_params:
            raise HTTPException(422, "invalid_guest_link_query")
        if write:
            headers = request.headers.getlist("x-pve-plugins-csrf")
            csrf.verify(request, user, headers[0] if len(headers) == 1 else "")
        return user

    async def command(request, model):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "json_required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, "guest_link_request_too_large")
        try:
            return model.model_validate_json(bytes(body))
        except ValidationError:
            raise HTTPException(422, "invalid_guest_link_request") from None

    async def assessed(request, saved, guests):
        guest = next((item for item in guests if item["guest_type"] == saved["guest_type"] and item["vmid"] == saved["vmid"]), None)
        if guest is None:
            return assess_link(saved, guests, None)
        try:
            identity = await pve.fingerprint(request, guest)
        except GuestLinkError:
            return assess_link(saved, guests, None, unavailable=True)
        result = assess_link(saved, guests, identity)
        if guest["template"]:
            result.update(status="needs_confirmation", can_navigate=False)
        return result

    @application.get("/pve-plugins/api/komodo/guest-links")
    async def guest_links(request: Request):
        try:
            async with asyncio.timeout(45):
                user = await authorize(request)
                database = available_store()
                # Fail on a moved PVE origin before querying either inventory.
                saved = database.list()
                servers, guests = await asyncio.gather(komodo.link_servers(), pve.list_guests(request))
                servers_by_id = {server["id"]: server for server in servers}
                semaphore = asyncio.Semaphore(8)
                async def limited(link):
                    async with semaphore:
                        return await assessed(request, link, guests)
                links = await asyncio.gather(*(limited(link) for link in saved))
                for link in links:
                    link["guest_name"] = link.pop("name", None)
                    server = servers_by_id.get(link["server_id"])
                    link["server_name"] = server["name"] if server else None
                    if server is None or server["template"]:
                        link.update(status="needs_confirmation", can_navigate=False, server_unavailable=True)
                for server in servers:
                    server["link_revision"] = database.revision(server["id"])
                return {"links": links, "guests": guests, "servers": servers, "csrf_token": csrf.issue(request, user)}
        except (GuestLinkError, KomodoDashboardError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "guest_links_timeout") from None

    @application.post("/pve-plugins/api/komodo/guest-links")
    async def bind_guest(request: Request):
        try:
            async with asyncio.timeout(45):
                user = await authorize(request, write=True)
                value = await command(request, BindRequest)
                database = available_store()
                database.cluster_id()
                servers, guests = await asyncio.gather(komodo.link_servers(), pve.list_guests(request))
                server = next((item for item in servers if item["id"] == value.server_id and not item["template"]), None)
                if server is None:
                    raise HTTPException(409, "komodo_server_unavailable")
                guest = next((item for item in guests if item["guest_type"] == value.guest_type and item["vmid"] == value.vmid and not item["template"]), None)
                if guest is None:
                    raise HTTPException(409, "pve_guest_inaccessible")
                identity = await pve.fingerprint(request, guest)
                saved = database.save(value.server_id, guest, identity, expected_revision=value.expected_revision, confirmed=True)
                link = assess_link(saved, guests, identity)
                link["guest_name"] = link.pop("name", None)
                link["server_name"] = server["name"]
                LOGGER.info("Guest link saved user=%s server_id=%s type=%s vmid=%s revision=%s", user,
                            value.server_id, value.guest_type, value.vmid, saved["revision"])
                return {"link": link}
        except (GuestLinkError, KomodoDashboardError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "guest_links_timeout") from None

    @application.post("/pve-plugins/api/komodo/guest-links/unlink")
    async def unlink_guest(request: Request):
        try:
            async with asyncio.timeout(20):
                user = await authorize(request, write=True)
                value = await command(request, UnlinkRequest)
                # Local cleanup remains possible after a Komodo server was
                # deleted or became inaccessible. Neither upstream is mutated.
                removed = available_store().unlink(value.server_id, expected_revision=value.expected_revision)
                LOGGER.info("Guest link removed user=%s server_id=%s revision=%s", user, value.server_id, removed["revision"])
                return {"removed": True, "revision": removed["revision"]}
        except GuestLinkError as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, "guest_links_timeout") from None

    return store
