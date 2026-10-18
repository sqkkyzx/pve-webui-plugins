"""Exercise plugin selection through the real application factory and templates."""

import asyncio
from itertools import combinations
import json
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app
from app.plugins import Asset, Plugin, PluginHandle, discover_plugins, render_runtime, resolve_plugins
from test_app import DESKTOP


ROOT = Path(__file__).resolve().parent.parent
IDS = ("komodo", "temperatures", "sites")
COMBINATIONS = [values for count in range(4) for values in combinations(IDS, count)]
PATHS = {
    "komodo": ("/pve-plugins/api/komodo/connection", "/pve-plugins/assets/komodo/extension.js"),
    "temperatures": ("/pve-plugins/api/temperatures/nodes/pve-a", "/pve-plugins/assets/temperatures/temperatures.js"),
    "sites": ("/pve-plugins/api/sites", "/pve-plugins/assets/sites/sites.js"),
}


@pytest.mark.parametrize("enabled", COMBINATIONS)
def test_all_plugin_combinations_control_html_api_and_assets(enabled, tmp_path):
    def upstream(request):
        assert request.url.path == "/"
        return httpx.Response(200, content=DESKTOP, headers={"content-type": "text/html"})

    application = create_app(Settings(
        "https://pve.example:8006", tls_verify=False, enabled_plugins=enabled,
        runtime_dir=str(tmp_path / "runtime"), site_state_file=str(tmp_path / "sites.sqlite3"),
    ), transport=httpx.MockTransport(upstream))
    with TestClient(application) as client:
        health = client.get("/pve-plugins/health")
        assert health.status_code == 200 and health.json()["plugins"] == list(enabled)
        body = client.get("/").content
        assert b"UserName: 'alice@pve', CSRFPreventionToken: 'csrf-value'" in body
        assert (b"pve-webui-plugins:begin" in body) == bool(enabled)
        for plugin_id, (api, asset) in PATHS.items():
            active = plugin_id in enabled
            assert client.get(api).status_code == (401 if active else 404)
            assert client.get(asset).status_code == (200 if active else 404)
            assert client.head(asset).status_code == (200 if active else 404)
            assert (asset.encode() in body) == active
        assert client.get("/pve-plugins/api/plugins").status_code == 401
        assert client.get("/?pve-plugins=off").content == DESKTOP
    generated = json.loads((tmp_path / "runtime/plugins.json").read_text())
    assert generated["enabled"] == list(enabled)
    head = (tmp_path / "runtime/desktop-head.html").read_bytes()
    if enabled:
        assert head in body
    assert not (tmp_path / "sites.sqlite3").exists(), "anonymous requests must not open plugin storage"


@pytest.mark.parametrize("enabled", COMBINATIONS)
def test_disabled_plugin_packages_are_not_imported_in_fresh_process(enabled, tmp_path):
    script = """
import sys
from app.main import Settings, create_app
ids = tuple(sys.argv[1].split(',')) if sys.argv[1] else ()
app = create_app(Settings('https://pve.example:8006', tls_verify=False, enabled_plugins=ids))
for name in ('komodo', 'temperatures', 'sites'):
    assert ('plugins.' + name + '.plugin' in sys.modules) == (name in ids)
    if name not in ids:
        assert not any(key.startswith('plugins.' + name + '.') for key in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script, ",".join(enabled)], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_environment_can_select_no_plugins_and_ignores_disabled_plugin_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("PVE_UPSTREAM", "https://pve.example:8006")
    monkeypatch.setenv("PVE_PLUGINS", "")
    monkeypatch.setenv("PVE_KOMODO_URL", "invalid-origin")
    monkeypatch.setenv("PVE_KOMODO_TLS_VERIFY", "invalid-but-unused")
    monkeypatch.setenv("PVE_TEMPERATURES_SOURCES_FILE", str(tmp_path / "missing.json"))
    config = Settings.from_env()
    assert config.enabled_plugins == ()
    with TestClient(create_app(config)) as client:
        assert client.get("/pve-plugins/health").json()["plugins"] == []


def test_plugin_metadata_requires_pve_auth_and_generated_templates_never_contain_credentials(monkeypatch, tmp_path):
    async def authenticate(self, request):
        return "root@pam"

    monkeypatch.setattr("app.main.PveAuthenticator.authenticate", authenticate)
    config = Settings("https://pve.example:8006", enabled_plugins=("sites",), tls_verify=False,
                      komodo_api_key="NEVER-PUBLISH-KEY", komodo_api_secret="NEVER-PUBLISH-SECRET",
                      runtime_dir=str(tmp_path), site_state_file=":memory:")
    with TestClient(create_app(config)) as client:
        data = client.get("/pve-plugins/api/plugins")
        assert data.json()["enabled"] == ["sites"]
        assert {item["id"] for item in data.json()["available"]} == set(IDS)
        assert data.headers["cache-control"] == "private, no-store"
        assert client.get("/pve-plugins/api/session").json() == {"user": "root@pam"}
    for path in tmp_path.iterdir():
        assert b"NEVER-PUBLISH" not in path.read_bytes()


def plugin(plugin_id, requires=(), after=()):
    return Plugin(plugin_id, plugin_id, "1.0.0", f"plugins.{plugin_id}.plugin:setup", tuple(requires), (), tuple(after))


def test_dependency_order_missing_dependencies_and_cycles():
    catalog = {"a": plugin("a", ("b",)), "b": plugin("b")}
    assert [item.id for item in resolve_plugins(catalog, ("a", "b"))] == ["b", "a"]
    with pytest.raises(ValueError, match="requires enabled"):
        resolve_plugins(catalog, ("a",))
    catalog["b"] = plugin("b", ("a",))
    with pytest.raises(ValueError, match="cycle"):
        resolve_plugins(catalog, ("a", "b"))


def test_optional_order_does_not_enable_a_dependency_but_still_detects_cycles():
    catalog = {"a": plugin("a", after=("b",)), "b": plugin("b")}
    assert [item.id for item in resolve_plugins(catalog, ("a",))] == ["a"]
    assert [item.id for item in resolve_plugins(catalog, ("a", "b"))] == ["b", "a"]
    catalog["b"] = plugin("b", after=("a",))
    with pytest.raises(ValueError, match="cycle"):
        resolve_plugins(catalog, ("a", "b"))


def test_sites_wraps_container_tree_even_if_environment_lists_sites_first():
    app = create_app(Settings("https://pve.example:8006", tls_verify=False, enabled_plugins=("sites", "komodo")),
                     transport=httpx.MockTransport(lambda request: httpx.Response(
                         200, content=DESKTOP, headers={"content-type": "text/html"})))
    with TestClient(app) as client:
        assert client.get("/pve-plugins/health").json()["plugins"] == ["komodo", "sites"]
        body = client.get("/").text
        assert body.index("/assets/komodo/extension.js") < body.index("/assets/sites/sites.js")
        assert client.get("/pve-plugins/api/sites").status_code == 401


@pytest.mark.parametrize("enabled", [("unknown",), ("sites", "sites"), ("../sites",)])
def test_invalid_selection_fails_at_startup(enabled):
    with pytest.raises(ValueError):
        create_app(Settings("https://pve.example:8006", enabled_plugins=enabled, tls_verify=False))


def test_disabled_assets_need_not_exist_and_restarting_replaces_generated_files(tmp_path):
    catalog = discover_plugins()
    all_runtime = render_runtime(resolve_plugins(catalog, IDS), output_dir=str(tmp_path))
    assert b"extension.js" in all_runtime.head
    selected = resolve_plugins(catalog, ("sites",))
    runtime = render_runtime(selected, output_dir=str(tmp_path))
    assert b"extension.js" not in runtime.head and b"temperatures.js" not in runtime.head
    assert (tmp_path / "desktop-head.html").read_bytes() == runtime.head
    assert json.loads((tmp_path / "plugins.json").read_text())["enabled"] == ["sites"]
    # An unselected plugin's assets are not required for the host to start.
    missing = Plugin("optional", "Optional", "1.0.0", "plugins.optional.plugin:setup", (),
                     (Asset(tmp_path / "missing.js", "/pve-plugins/assets/optional/missing.js"),))
    catalog[missing.id] = missing
    assert render_runtime(resolve_plugins(catalog, ("sites",))).head == runtime.head
    with pytest.raises(FileNotFoundError):
        render_runtime(resolve_plugins(catalog, ("optional",)))


@pytest.mark.parametrize("change", [
    {"api_version": 2}, {"id": "other"}, {"entrypoint": "os:system"},
    {"assets": [{"source": "../../outside.js", "url": "/pve-plugins/assets/test.js"}]},
    {"assets": [{"source": "static/test.js", "url": "/pve-plugins/assets/../test.js"}]},
    {"assets": [{"source": "static/test.js", "url": "/pve-komodo/assets/test.js"}]},
    {"assets": [{"source": "static/test.js", "url": "/pve-plugins/assets/other/test.js"}]},
])
def test_manifests_reject_unsupported_contracts_and_path_escape(tmp_path, change):
    folder = tmp_path / "sample"
    folder.mkdir()
    value = {"id": "sample", "name": "Sample", "version": "1.0.0", "api_version": 1,
             "entrypoint": "plugins.sample.plugin:setup", "requires": [], "assets": [], **change}
    (folder / "plugin.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Invalid plugin manifest"):
        discover_plugins(tmp_path)


def test_shutdown_runs_in_reverse_order_even_if_one_plugin_fails(monkeypatch):
    events = []

    def load(spec, context):
        async def shutdown():
            events.append(spec.id)
            if spec.id == "sites":
                raise RuntimeError("test shutdown failure")
        return PluginHandle(shutdown=shutdown)

    monkeypatch.setattr("app.main.load_plugin", load)
    app = create_app(Settings("https://pve.example:8006", tls_verify=False))

    async def scenario():
        with pytest.raises(ExceptionGroup):
            async with app.router.lifespan_context(app):
                pass

    asyncio.run(scenario())
    assert events == ["sites", "temperatures", "komodo"]


def test_new_plugin_is_discovered_and_loaded_without_editing_core(monkeypatch, tmp_path):
    import plugins

    folder = tmp_path / "sample"
    (folder / "static").mkdir(parents=True)
    (folder / "__init__.py").write_text("")
    (folder / "static/sample.js").write_text("window.samplePluginLoaded = true;\n")
    (folder / "plugin.json").write_text(json.dumps({
        "id": "sample", "name": "Sample", "version": "1.0.0", "api_version": 1,
        "entrypoint": "plugins.sample.plugin:setup", "requires": [],
        "assets": [{"source": "static/sample.js", "url": "/pve-plugins/assets/sample/sample.js"}],
    }))
    (folder / "plugin.py").write_text('''
from fastapi import Request
from app.plugins import PluginHandle

def setup(context):
    @context.application.get('/pve-plugins/api/sample/status')
    async def status(request: Request):
        await context.authenticator.authenticate(request)
        return {'status': 'ok'}
    return PluginHandle()
''')
    monkeypatch.setattr(plugins, "__path__", [*plugins.__path__, str(tmp_path)])
    catalog = discover_plugins(tmp_path)
    monkeypatch.setattr("app.main.discover_plugins", lambda: catalog)
    app = create_app(Settings("https://pve.example:8006", enabled_plugins=("sample",), tls_verify=False))
    with TestClient(app) as client:
        assert client.get("/pve-plugins/health").json()["plugins"] == ["sample"]
        assert client.get("/pve-plugins/api/sample/status").status_code == 401
        assert client.get("/pve-plugins/assets/sample/sample.js").text == "window.samplePluginLoaded = true;\n"


@pytest.mark.parametrize("path", [
    "/pve-komodo/health", "/pve-komodo/api/session", "/pve-komodo/api/dashboard",
    "/pve-komodo/api/sites", "/pve-komodo/assets/extension.js",
    "/pve-komodo/assets/sites.js", "/pve-plugins/api/dashboard",
    "/pve-plugins/assets/sites.js",
])
def test_removed_routes_are_not_redirected_or_aliased(path):
    app = create_app(Settings("https://pve.example:8006", tls_verify=False))
    with TestClient(app) as client:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 404 and "location" not in response.headers
    assert all(not route.path.startswith("/pve-komodo/") for route in app.routes)


def test_old_bypass_and_response_header_are_removed():
    app = create_app(Settings("https://pve.example:8006", tls_verify=False),
                     transport=httpx.MockTransport(lambda _: httpx.Response(
                         200, content=DESKTOP, headers={"content-type": "text/html"})))
    with TestClient(app) as client:
        response = client.get("/?pve-komodo=off")
        assert response.headers["x-pve-plugins-extension"] == "enabled"
        assert "x-pve-komodo-extension" not in response.headers
        assert b"/pve-plugins/assets/sites/sites.js" in response.content
        assert client.get("/?pve-plugins=off").content == DESKTOP


def test_settings_use_only_current_environment_names(monkeypatch):
    for name in ("PVE_KOMODO_URL", "PVE_KOMODO_API_KEY", "PVE_KOMODO_API_SECRET",
                 "PVE_KOMODO_TLS_VERIFY", "PVE_PLUGINS_INJECTION_ENABLED",
                 "PVE_PLUGINS_RUNTIME_DIR", "PVE_KOMODO_ACTION_STATE_FILE",
                 "PVE_KOMODO_GUEST_LINKS_FILE", "PVE_SITES_STATE_FILE",
                 "PVE_TEMPERATURES_SOURCES_FILE"):
        monkeypatch.delenv(name, raising=False)
    for name,value in {"PVE_UPSTREAM": "https://pve.example:8006", "PVE_PLUGINS": "sites",
                       "KOMODO_URL": "http://old.invalid:9120", "KOMODO_API_KEY": "old-key",
                       "KOMODO_API_SECRET": "old-secret", "KOMODO_TLS_VERIFY": "invalid",
                       "PVE_EXTENSION_ENABLED": "false", "PVE_RUNTIME_DIR": "/old/runtime",
                       "PVE_ACTION_STATE_FILE": "/old/actions", "PVE_GUEST_LINKS_FILE": "/old/links",
                       "PVE_SITES_FILE": "/old/sites", "PVE_TEMPERATURE_SOURCES_FILE": "/old/temperatures"}.items():
        monkeypatch.setenv(name,value)
    config = Settings.from_env()
    assert config.injection_enabled is True and config.runtime_dir is None
    assert config.komodo_url is config.komodo_api_key is config.komodo_api_secret is None
    assert config.action_state_file is config.guest_links_file is config.site_state_file is config.temperature_sources_file is None
    for name,value in {"PVE_KOMODO_URL": "http://core:9120", "PVE_KOMODO_API_KEY": "new-key",
                       "PVE_KOMODO_API_SECRET": "new-secret", "PVE_PLUGINS_INJECTION_ENABLED": "false",
                       "PVE_PLUGINS_RUNTIME_DIR": "/new/runtime", "PVE_KOMODO_ACTION_STATE_FILE": "/new/actions",
                       "PVE_KOMODO_GUEST_LINKS_FILE": "/new/links", "PVE_SITES_STATE_FILE": "/new/sites",
                       "PVE_TEMPERATURES_SOURCES_FILE": "/new/temperatures"}.items():
        monkeypatch.setenv(name,value)
    config = Settings.from_env()
    assert config.injection_enabled is False and config.runtime_dir == "/new/runtime"
    assert (config.komodo_url,config.komodo_api_key,config.komodo_api_secret) == ("http://core:9120","new-key","new-secret")
    assert (config.action_state_file,config.guest_links_file,config.site_state_file,config.temperature_sources_file) == ("/new/actions","/new/links","/new/sites","/new/temperatures")
