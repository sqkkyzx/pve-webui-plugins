"""Non-invasive PVE desktop host with independently selected WebUI plugins."""

from contextlib import asynccontextmanager
import asyncio
from copy import deepcopy
from pathlib import Path
import logging
import re
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
import httpx

from app.csrf import CsrfProtector
from app.plugins import discover_plugins, render_runtime, resolve_plugins
from app.configuration import ConfigurationStore, MAX_CONFIG_BYTES, decode
from app.plugin_runtime import GenerationBuildError, PluginGateway, build_generation
from app.pve_auth import PveAuthenticator
from app.pve_upstream import PveFailoverTransport, PveUpstreamState
from app.settings import Settings


LOGGER = logging.getLogger("pve_webui_plugins")
VERSION = "1.0.0"


# Reject unknown desktop shapes instead of injecting into login providers,
# consoles, mobile UIs, or an HTML error page.
MANAGER_SCRIPT = re.compile(
    rb"<script\b[^>]*\bsrc=[\"']/pve2/js/pvemanagerlib\.js\?ver=(9\.[^\"']*)[\"'][^>]*>\s*</script>",
    re.IGNORECASE,
)
WORKSPACE_SCRIPT = re.compile(
    rb"<script\b[^>]*>\s*(?:if\s*\(typeof\(PVE\).*?;\s*)?"
    rb"Ext\.History\.fieldid\s*=.*?Ext\.onReady\(function\(\)\s*\{\s*"
    rb"Ext\.create\('PVE\.StdWorkspace'\);\s*\}\);\s*</script>",
    re.DOTALL,
)



def inject_extension(body: bytes, query: str, enabled: bool, head: bytes) -> tuple[bytes, str]:
    params = parse_qs(query, keep_blank_values=True)
    if not enabled or params.get("pve-plugins") == ["off"]:
        return body, "disabled"
    if any(key in params for key in ("console", "novnc", "xtermjs", "mobile")):
        return body, "console-or-mobile"
    if b"<!-- pve-webui-plugins:begin -->" in body:
        return body, "already-present"
    managers = list(MANAGER_SCRIPT.finditer(body))
    workspaces = list(WORKSPACE_SCRIPT.finditer(body))
    if len(managers) != 1 or len(workspaces) != 1:
        return body, "unsupported-page"
    insertion = workspaces[0].start()
    if managers[0].end() > insertion:
        return body, "unsupported-page"
    return body[:insertion] + head + body[insertion:], "enabled"


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    pve_auth_transport: httpx.AsyncBaseTransport | None = None,
    komodo_transport: httpx.AsyncBaseTransport | None = None,
    temperature_transport: httpx.AsyncBaseTransport | None = None,
    plugin_transports: dict[str, httpx.AsyncBaseTransport] | None = None,
) -> FastAPI:
    config = settings or Settings.from_env()
    tls = config.tls_context()
    catalog = discover_plugins()
    store = ConfigurationStore(config, catalog)
    upstream_state = PveUpstreamState(config.upstreams)
    desktop_transport = PveFailoverTransport(upstream_state, verify=tls,
        server_names=config.upstream_server_names, transport=transport)
    auth_transport = desktop_transport if transport is pve_auth_transport else PveFailoverTransport(
        upstream_state, verify=tls, server_names=config.upstream_server_names, transport=pve_auth_transport)
    authenticator = PveAuthenticator(
        config.upstream, verify=tls, allowed_users=config.admin_users, transport=auth_transport,
    )
    csrf = CsrfProtector()
    transports = {**(plugin_transports or {}), "pve_auth": auth_transport, "komodo": komodo_transport,
                  "temperature": temperature_transport}
    try:
        generation = build_generation(store.effective_settings(), catalog, authenticator, csrf, transports)
    except GenerationBuildError as error:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(error.generation.close())
        else:
            loop.create_task(error.generation.close())
        raise ValueError("Plugin initialization failed") from None
    gateway = PluginGateway(generation)
    if config.runtime_dir:
        render_runtime(generation.runtime.plugins, output_dir=config.runtime_dir)
    update_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(application):
        try:
            yield
        finally:
            try:
                await gateway.close()
            finally:
                try:
                    await desktop_transport.close_pool()
                finally:
                    if auth_transport is not desktop_transport:
                        await auth_transport.close_pool()

    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    published_keys = set()

    def publish_state(generation):
        application.state.plugin_runtime = generation.runtime
        application.state.plugin_handles = generation.handles
        # Keep plugin diagnostic state accessible to local integrations/tests.
        for key in published_keys:
            if hasattr(application.state, key):
                delattr(application.state, key)
        published_keys.clear()
        for key, value in generation.application.state._state.items():
            setattr(application.state, key, value)
            published_keys.add(key)

    publish_state(generation)

    @application.middleware("http")
    async def private_api_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/pve-plugins/api/"):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @application.get("/pve-plugins/api/session")
    async def session(request: Request) -> dict:
        user = await authenticator.authenticate(request)
        return {"user": user}

    @application.get("/pve-plugins/api/plugins")
    async def plugins(request: Request) -> dict:
        await authenticator.authenticate(request)
        return {**gateway.generation.runtime.manifest, "available": [plugin.public() for plugin in catalog.values()]}

    @application.get("/pve-plugins/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": VERSION,
                             "injection_enabled": config.injection_enabled,
                             "plugins": gateway.generation.runtime.manifest["enabled"]})

    def configuration_response(request, user):
        return {**store.public(), "available": [plugin.public() for plugin in catalog.values()],
                "csrf_token": csrf.issue(request, user)}

    @application.get("/pve-plugins/api/config")
    async def get_configuration(request: Request):
        user = await authenticator.authenticate(request)
        if request.query_params:
            raise HTTPException(422, "invalid_plugin_configuration_query")
        return configuration_response(request, user)

    @application.get("/pve-plugins/api/navigation")
    async def get_navigation(request: Request):
        await authenticator.authenticate(request)
        if request.query_params:
            raise HTTPException(422, "invalid_plugin_navigation_query")
        return {"revision": store.value["revision"], "navigation": deepcopy(store.value["navigation"])}

    @application.put("/pve-plugins/api/config")
    async def put_configuration(request: Request):
        user = await authenticator.authenticate(request)
        headers = request.headers.getlist("x-pve-plugins-csrf")
        csrf.verify(request, user, headers[0] if len(headers) == 1 else "")
        if request.query_params:
            raise HTTPException(422, "invalid_plugin_configuration_query")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "json_required")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_CONFIG_BYTES:
                raise HTTPException(413, "plugin_configuration_too_large")
            body.extend(chunk)
        try:
            command = decode(bytes(body))
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(422, "invalid_plugin_configuration") from None
        async with update_lock:
            value = store.prepare(command)
            try:
                selected = resolve_plugins(catalog, tuple(value["enabled_plugins"]))
            except ValueError:
                raise HTTPException(422, "invalid_plugin_configuration") from None
            if (selected == gateway.generation.runtime.plugins
                    and value["modules"] == store.value["modules"]):
                # Navigation preferences do not replace clients or interrupt
                # accepted requests and open terminal sessions.
                store.save(value)
                return {**configuration_response(request, user), "reload_required": False}
            try:
                candidate = build_generation(store.effective_settings(value), catalog, authenticator, csrf, transports)
            except GenerationBuildError as error:
                await error.generation.close()
                raise HTTPException(422, "invalid_plugin_configuration") from None
            except (ValueError, OSError):
                raise HTTPException(422, "invalid_plugin_configuration") from None
            try:
                store.save(value)
            except Exception:
                await candidate.close()
                raise
            gateway.replace(candidate)
            publish_state(candidate)
            if config.runtime_dir:
                try:
                    render_runtime(candidate.runtime.plugins, output_dir=config.runtime_dir)
                except OSError:
                    LOGGER.warning("Generated plugin metadata could not be written")
            return {**configuration_response(request, user), "reload_required": True}

    @application.api_route("/pve-plugins/assets/core/manager.js", methods=["GET", "HEAD"])
    async def manager_asset():
        return FileResponse(Path(__file__).parent / "static" / "manager.js",
                            media_type="text/javascript; charset=utf-8",
                            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    @application.api_route("/pve-plugins/assets/core/navigation.js", methods=["GET", "HEAD"])
    async def navigation_asset():
        return FileResponse(Path(__file__).parent / "static" / "navigation.js",
                            media_type="text/javascript; charset=utf-8",
                            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    @application.api_route("/", methods=["GET", "HEAD"])
    async def desktop(request: Request) -> Response:
        query = request.url.query
        upstream_url = config.upstream.rstrip("/") + "/" + ("?" + query if query else "")
        headers = {
            name: request.headers[name]
            for name in ("cookie", "user-agent", "accept-language", "accept")
            if name in request.headers
        }
        headers["accept-encoding"] = "identity"
        # Never share HTTPX's cookie jar between users. The desktop is fetched
        # infrequently; an isolated client also prevents upstream Set-Cookie
        # responses from being replayed into another user's request.
        try:
            async with httpx.AsyncClient(
                verify=tls,
                timeout=httpx.Timeout(20.0, connect=5.0),
                follow_redirects=False,
                trust_env=False,
                transport=desktop_transport,
            ) as client:
                upstream = await client.get(upstream_url, headers=headers)
        except httpx.HTTPError:
            # Do not log URLs, cookies, or response bodies containing identity.
            LOGGER.warning("PVE desktop upstream request failed")
            return Response(
                "PVE upstream unavailable. Use the original PVE management address.",
                status_code=502,
                media_type="text/plain",
                headers={"Cache-Control": "no-store"},
            )

        body = upstream.content
        state = "upstream-response"
        if upstream.status_code == 200 and upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "text/html":
            body, state = inject_extension(body, query, config.injection_enabled, gateway.generation.runtime.head)

        # HTTPX has decoded Content-Encoding. Remove body validators as well as
        # hop-by-hop headers, and preserve duplicate Set-Cookie headers.
        excluded = {
            "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
            "te", "trailer", "transfer-encoding", "upgrade", "content-length",
            "content-encoding", "etag", "last-modified", "content-md5", "digest",
            "content-digest", "cache-control", "expires", "pragma",
        }
        excluded.update(part.strip().lower() for part in upstream.headers.get("connection", "").split(","))
        response = Response(content=b"" if request.method == "HEAD" else body, status_code=upstream.status_code)
        response.raw_headers = [
            (name, value) for name, value in upstream.headers.raw
            if name.decode("ascii").lower() not in excluded
        ]
        response.headers["Content-Length"] = str(len(body))
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-PVE-Plugins-Extension"] = state
        return response

    application.router.routes.append(gateway)
    return application
