"""PVE-authenticated, read-only physical node temperature endpoint."""

import asyncio

from fastapi import HTTPException, Request

from plugins.temperatures.temperatures import NODE, TemperatureService, load_temperature_sources
from plugins.temperatures.node_temperature_discovery import DiscoveryError, NodeTemperatureDiscovery


REQUEST_TIMEOUT_SECONDS = 14


def register_temperature_routes(application, settings, authenticator, *, transport=None):
    service = TemperatureService(load_temperature_sources(settings.temperature_sources_file), transport=transport)
    discovery = NodeTemperatureDiscovery(authenticator)

    @application.get("/pve-komodo/api/nodes/{node}/temperatures")
    async def temperatures(request: Request, node: str):
        try:
            # Keep authentication + discovery + scrape below the browser's
            # 15-second read timeout; a slow optional metric must not linger.
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                # Authenticate before lookup or cache access. PVE cookies never
                # leave the fixed PVE origin, including during discovery.
                await authenticator.authenticate(request)
                if NODE.fullmatch(node) is None or request.query_params:
                    raise HTTPException(422, "invalid_temperature_request")
                if node in service.sources:
                    return await service.get(node)
                try:
                    target = await discovery.resolve(request, node)
                except DiscoveryError:
                    target = None
                if target is None:
                    return {"version": 1, "node": node, "sampled_at": None, "sensors": [], "status": "unavailable"}
                return await service.get(node, service.discovered_source(target.origin))
        except TimeoutError:
            raise HTTPException(504, "temperature_request_timeout") from None

    return service
