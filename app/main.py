"""Non-invasive PVE desktop host with independently selected WebUI plugins."""

from contextlib import asynccontextmanager
import logging
import re
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
import httpx

from app.csrf import CsrfProtector
from app.plugins import PluginContext, discover_plugins, load_plugin, render_runtime, resolve_plugins
from app.pve_auth import PveAuthenticator
from app.settings import Settings


LOGGER = logging.getLogger("pve_webui_plugins")
VERSION = "0.3.0"
# Retained for deployments that already route /pve-komodo/ to this service.
EXTENSION_PATH = "/pve-komodo/assets/extension.js"
TEMPERATURE_EXTENSION_PATH = "/pve-komodo/assets/temperatures.js"
SITE_EXTENSION_PATH = "/pve-komodo/assets/sites.js"


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
    if not enabled or params.get("pve-plugins") == ["off"] or params.get("pve-komodo") == ["off"]:
        return body, "disabled"
    if any(key in params for key in ("console", "novnc", "xtermjs", "mobile")):
        return body, "console-or-mobile"
    if b"<!-- pve-webui-plugins:begin -->" in body or EXTENSION_PATH.encode() in body:
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
) -> FastAPI:
    config = settings or Settings.from_env()
    tls = config.tls_context()
    catalog = discover_plugins()
    selected = resolve_plugins(catalog, config.enabled_plugins)
    runtime = render_runtime(selected, output_dir=config.runtime_dir)
    authenticator = PveAuthenticator(
        config.upstream, verify=tls, allowed_users=config.admin_users, transport=pve_auth_transport,
    )
    handles = {}

    @asynccontextmanager
    async def lifespan(application):
        try:
            yield
        finally:
            errors = []
            for plugin_id, handle in reversed(tuple(handles.items())):
                if handle.shutdown is not None:
                    try:
                        await handle.shutdown()
                    except Exception as error:
                        LOGGER.error("Plugin shutdown failed: %s", plugin_id)
                        errors.append(error)
            if errors:
                raise ExceptionGroup("Plugin shutdown failed", errors)

    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    context = PluginContext(application, config, authenticator, CsrfProtector(),
                            {"pve_auth": pve_auth_transport, "komodo": komodo_transport,
                             "temperature": temperature_transport})
    for plugin in selected:
        handles[plugin.id] = load_plugin(plugin, context)
    application.state.plugin_runtime = runtime
    application.state.plugin_handles = handles

    @application.middleware("http")
    async def private_api_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/pve-komodo/api/", "/pve-plugins/api/")):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @application.get("/pve-plugins/api/session")
    @application.get("/pve-komodo/api/session")
    async def session(request: Request) -> dict:
        user = await authenticator.authenticate(request)
        komodo = handles.get("komodo")
        return {"user": user, "komodo_configured": bool(komodo and komodo.metadata.get("configured"))}

    @application.get("/pve-plugins/api/plugins")
    async def plugins(request: Request) -> dict:
        await authenticator.authenticate(request)
        return {**runtime.manifest, "available": [plugin.public() for plugin in catalog.values()]}

    @application.get("/pve-plugins/health")
    @application.get("/pve-komodo/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": VERSION,
                             "extension_enabled": config.extension_enabled,
                             "plugins": runtime.manifest["enabled"]})

    def asset_endpoint(asset):
        async def serve() -> FileResponse:
            return FileResponse(asset.path, media_type="text/javascript; charset=utf-8",
                                headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})
        return serve

    for plugin in selected:
        for asset in plugin.assets:
            application.add_api_route(asset.url, asset_endpoint(asset), methods=["GET", "HEAD"])

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
                transport=transport,
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
            body, state = inject_extension(body, query, config.extension_enabled and bool(runtime.plugins), runtime.head)

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
        response.headers["X-PVE-KOMODO-Extension"] = state
        response.headers["X-PVE-Plugins-Extension"] = state
        return response

    return application
