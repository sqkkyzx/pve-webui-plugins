"""Atomically replace plugin routes while in-flight requests finish safely."""

import asyncio
from dataclasses import dataclass, field
import logging

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.routing import BaseRoute, Match

from app.plugins import PluginContext, load_plugin, render_runtime, resolve_plugins


LOGGER = logging.getLogger("pve_webui_plugins")


@dataclass
class Generation:
    application: FastAPI
    runtime: object
    handles: dict
    active: int = 0
    sockets: dict = field(default_factory=dict)
    idle: asyncio.Event = field(default_factory=asyncio.Event)

    async def close(self):
        errors = []
        for plugin_id, handle in reversed(tuple(self.handles.items())):
            if handle.shutdown:
                try:
                    await handle.shutdown()
                except Exception as error:
                    LOGGER.error("Plugin shutdown failed: %s", plugin_id)
                    errors.append(error)
        if errors:
            raise ExceptionGroup("Plugin shutdown failed", errors)


class GenerationBuildError(ValueError):
    def __init__(self, generation):
        super().__init__("Plugin initialization failed")
        self.generation = generation


def build_generation(settings, catalog, authenticator, csrf, transports):
    selected = resolve_plugins(catalog, settings.enabled_plugins)
    runtime = render_runtime(selected)
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    context = PluginContext(application, settings, authenticator, csrf, transports)
    handles = {}
    generation = Generation(application, runtime, handles)
    for plugin in selected:
        try:
            handles[plugin.id] = load_plugin(plugin, context)
        except Exception:
            raise GenerationBuildError(generation) from None
        for asset in plugin.assets:
            def endpoint(asset):
                async def serve():
                    return FileResponse(asset.path, media_type="text/javascript; charset=utf-8",
                                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})
                return serve
            application.add_api_route(asset.url, endpoint(asset), methods=["GET", "HEAD"])
    return generation


class PluginGateway(BaseRoute):
    path = "/pve-plugins/{plugin_path:path}"

    def __init__(self, generation):
        self.generation = generation
        self.retired = set()

    def matches(self, scope):
        path = scope.get("path", "")
        # Let the host router retain method errors for its own endpoints.
        core = {"/pve-plugins/api/session", "/pve-plugins/api/plugins", "/pve-plugins/api/config", "/pve-plugins/api/navigation",
                "/pve-plugins/health", "/pve-plugins/assets/core/manager.js",
                "/pve-plugins/assets/core/navigation.js"}
        if scope["type"] in {"http", "websocket"} and path.startswith("/pve-plugins/") and path not in core:
            return Match.FULL, {}
        return Match.NONE, {}

    async def handle(self, scope, receive, send):
        generation = self.generation
        generation.active += 1
        generation.idle.clear()
        socket_task = asyncio.current_task() if scope["type"] == "websocket" else None
        if socket_task:
            generation.sockets[socket_task] = send
        try:
            await generation.application(scope, receive, send)
        finally:
            if socket_task:
                generation.sockets.pop(socket_task, None)
            generation.active -= 1
            if not generation.active:
                generation.idle.set()

    def url_path_for(self, name, /, **path_params):
        from starlette.routing import NoMatchFound
        raise NoMatchFound(name, path_params)

    def replace(self, generation):
        previous, self.generation = self.generation, generation
        async def retire():
            # Revocation must close terminal sessions as soon as a module is
            # disabled/reconfigured. Accepted HTTP actions still finish below.
            async def revoke(socket_task, send):
                try:
                    async with asyncio.timeout(1):
                        await send({"type": "websocket.close", "code": 1012,
                                    "reason": "plugin_configuration_changed"})
                except Exception:
                    pass
                finally:
                    socket_task.cancel()
            await asyncio.gather(*(revoke(socket_task, send)
                                   for socket_task, send in tuple(previous.sockets.items())))
            if previous.active:
                await previous.idle.wait()
            await previous.close()
        task = asyncio.create_task(retire())
        self.retired.add(task)
        def done(task):
            self.retired.discard(task)
            if not task.cancelled() and task.exception():
                LOGGER.error("Retired plugin shutdown failed")
        task.add_done_callback(done)

    async def close(self):
        errors = []
        if self.retired:
            results = await asyncio.gather(*tuple(self.retired), return_exceptions=True)
            errors.extend(error for error in results if isinstance(error, Exception))
        try:
            await self.generation.close()
        except Exception as error:
            errors.append(error)
        if errors:
            raise ExceptionGroup("Plugin shutdown failed", errors)
