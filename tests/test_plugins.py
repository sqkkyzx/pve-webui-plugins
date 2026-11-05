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
from scripts.smoke_image import configuration as image_configuration
from test_app import DESKTOP


ROOT = Path(__file__).resolve().parent.parent
IDS = ("komodo", "temperatures", "sites", "safeline")
OPTIONAL_IDS = ("komodo", "temperatures", "safeline")
COMBINATIONS = [values for count in range(len(OPTIONAL_IDS) + 1) for values in combinations(OPTIONAL_IDS, count)]
PATHS = {
    "komodo": ("/pve-plugins/api/komodo/connection", "/pve-plugins/assets/komodo/extension.js"),
    "temperatures": ("/pve-plugins/api/temperatures/nodes/pve-a", "/pve-plugins/assets/temperatures/temperatures.js"),
    "sites": ("/pve-plugins/api/sites", "/pve-plugins/assets/sites/sites.js"),
    "safeline": ("/pve-plugins/api/safeline/connection", "/pve-plugins/assets/safeline/safeline.js"),
}


@pytest.mark.parametrize("enabled", COMBINATIONS)
def test_all_plugin_combinations_control_html_api_and_assets(enabled, tmp_path):
    def upstream(request):
        assert request.url.path == "/"
        return httpx.Response(200, content=DESKTOP, headers={"content-type": "text/html"})

    snapshot = image_configuration(enabled)
    expected = [plugin.id for plugin in resolve_plugins(discover_plugins(), tuple(snapshot["enabled_plugins"]))]
    config_path = tmp_path / "plugins.json"
    config_path.write_text(json.dumps(snapshot))
    application = create_app(Settings(
        "https://pve.example:8006", tls_verify=False, config_state_file=str(config_path),
        runtime_dir=str(tmp_path / "runtime"), site_state_file=str(tmp_path / "sites.sqlite3"),
    ), transport=httpx.MockTransport(upstream))
    with TestClient(application) as client:
        health = client.get("/pve-plugins/health")
        assert health.status_code == 200 and health.json()["plugins"] == expected
        body = client.get("/").content
        assert b"UserName: 'alice@pve', CSRFPreventionToken: 'csrf-value'" in body
        assert b"pve-webui-plugins:begin" in body
        assert b"/assets/core/manager.js" in body
        for plugin_id, (api, asset) in PATHS.items():
            active = plugin_id in expected
            assert client.get(api).status_code == (401 if active else 404)
            assert client.get(asset).status_code == (200 if active else 404)
            assert client.head(asset).status_code == (200 if active else 404)
            assert (asset.encode() in body) == active
        application_asset = "/pve-plugins/assets/safeline/application-pages.js"
        assert client.get(application_asset).status_code == (200 if "safeline" in enabled else 404)
        assert (application_asset.encode() in body) == ("safeline" in enabled)
        attack_asset = "/pve-plugins/assets/safeline/attack-pages.js"
        assert client.get(attack_asset).status_code == (200 if "safeline" in enabled else 404)
        assert (attack_asset.encode() in body) == ("safeline" in enabled)
        security_asset = "/pve-plugins/assets/safeline/security-pages.js"
        assert client.get(security_asset).status_code == (200 if "safeline" in enabled else 404)
        assert client.head(security_asset).status_code == (200 if "safeline" in enabled else 404)
        assert (security_asset.encode() in body) == ("safeline" in enabled)
        if "safeline" in enabled:
            assert body.index(application_asset.encode()) < body.index(PATHS["safeline"][1].encode())
            assert body.index(attack_asset.encode()) < body.index(PATHS["safeline"][1].encode())
            assert body.index(security_asset.encode()) < body.index(PATHS["safeline"][1].encode())
        assert client.get("/pve-plugins/api/safeline/applications/12/basic").status_code == (401 if "safeline" in enabled else 404)
        for path in ("challenges", "configuration/challenge", "cc/history/rate", "cc/history/waiting", "cc/configuration", "cc/relieve", "policy-management/create", "policy-management/edit?id=2", "policy-management/order?action=1", "policy-management/subscribe", "policy-management/summary?id=2", "policy-management/switch?id=2", "policy-management/delete?id=2"):
            assert client.get("/pve-plugins/api/safeline/" + path).status_code == (401 if "safeline" in enabled else 404)
        assert client.get("/pve-plugins/api/plugins").status_code == 401
        assert client.get("/?pve-plugins=off").content == DESKTOP
    generated = json.loads((tmp_path / "runtime/plugins.json").read_text())
    assert generated["enabled"] == expected
    head = (tmp_path / "runtime/desktop-head.html").read_bytes()
    assert head in body
    assert not (tmp_path / "sites.sqlite3").exists(), "anonymous requests must not open plugin storage"


@pytest.mark.parametrize("enabled", COMBINATIONS)
def test_disabled_plugin_packages_are_not_imported_in_fresh_process(enabled, tmp_path):
    script = """
import sys
from app.main import Settings, create_app
ids = tuple(sys.argv[1].split(',')) if sys.argv[1] else ()
ids = tuple(dict.fromkeys((*ids, "sites")))
app = create_app(Settings('https://pve.example:8006', tls_verify=False, enabled_plugins=ids))
for name in ('komodo', 'temperatures', 'sites', 'safeline'):
    assert ('plugins.' + name + '.plugin' in sys.modules) == (name in ids)
    if name not in ids:
        assert not any(key.startswith('plugins.' + name + '.') for key in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script, ",".join(enabled)], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_environment_defaults_to_builtin_sites_and_ignores_module_configuration(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PVE_UPSTREAM", "192.0.2.1,192.0.2.2")
    monkeypatch.delenv("PVE_PLUGINS_DATA_DIR", raising=False)
    monkeypatch.setenv("PVE_PLUGINS", "komodo,temperatures,safeline")
    monkeypatch.setenv("PVE_KOMODO_URL", "invalid-origin")
    monkeypatch.setenv("PVE_KOMODO_TLS_VERIFY", "invalid-but-unused")
    monkeypatch.setenv("PVE_TEMPERATURES_SOURCES_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("SAFELINE_URL", "invalid-origin")
    monkeypatch.setenv("SAFELINE_API_TOKEN", "invalid\nheader")
    monkeypatch.setenv("SAFELINE_STATE_FILE", str(tmp_path / "must-not-be-opened" / "connection.sqlite3"))
    config = Settings.from_env()
    assert config.upstreams == ("https://192.0.2.1:8006", "https://192.0.2.2:8006")
    assert config.tls_verify is False
    assert config.enabled_plugins == ("sites",)
    assert config.komodo_url is config.komodo_api_key is config.komodo_api_secret is None
    with TestClient(create_app(config)) as client:
        assert client.get("/pve-plugins/health").json()["plugins"] == ["sites"]


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


def test_sites_wraps_container_tree_even_if_configuration_lists_sites_first():
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

    monkeypatch.setattr("app.plugin_runtime.load_plugin", load)
    app = create_app(Settings("https://pve.example:8006", tls_verify=False,
                              enabled_plugins=("komodo", "temperatures", "sites")))

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
    catalog = {**discover_plugins(), **discover_plugins(tmp_path)}
    monkeypatch.setattr("app.main.discover_plugins", lambda: catalog)
    app = create_app(Settings("https://pve.example:8006", enabled_plugins=("sample",), tls_verify=False))
    with TestClient(app) as client:
        assert client.get("/pve-plugins/health").json()["plugins"] == ["sample", "sites"]
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


def test_settings_environment_only_controls_infrastructure(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("PVE_PLUGINS_INJECTION_ENABLED", "PVE_PLUGINS_RUNTIME_DIR", "PVE_PLUGINS_DATA_DIR",
                 "PVE_ADMIN_USERS", "PVE_TLS_VERIFY", "PVE_CA_FILE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "PVE_UPSTREAM": "192.0.2.1", "PVE_PLUGINS": "komodo,temperatures,safeline",
        "PVE_KOMODO_URL": "invalid-origin", "PVE_KOMODO_API_KEY": "ignored-key",
        "PVE_KOMODO_API_SECRET": "ignored-secret", "PVE_KOMODO_TLS_VERIFY": "invalid",
        "PVE_PLUGINS_CONFIG_FILE": "/removed/config", "PVE_KOMODO_ACTION_STATE_FILE": "/removed/actions",
        "PVE_KOMODO_GUEST_LINKS_FILE": "/removed/links", "PVE_SITES_STATE_FILE": "/removed/sites",
        "PVE_TEMPERATURES_SOURCES_FILE": "/removed/temperatures", "SAFELINE_STATE_FILE": "/removed/safeline",
        "SAFELINE_URL": "invalid-origin", "SAFELINE_API_TOKEN": "ignored-token",
    }.items():
        monkeypatch.setenv(name,value)
    config = Settings.from_env()
    assert config.upstreams == ("https://192.0.2.1:8006",) and config.tls_verify is False
    assert config.injection_enabled is True and config.runtime_dir is None
    assert config.enabled_plugins == ("sites",)
    assert config.komodo_url is config.komodo_api_key is config.komodo_api_secret is None
    assert config.config_state_file == str(tmp_path / "data/plugins.json")
    assert config.action_state_file == str(tmp_path / "data/actions.sqlite3")
    assert config.guest_links_file == str(tmp_path / "data/guest-links.sqlite3")
    assert config.site_state_file == str(tmp_path / "data/sites.sqlite3")
    assert config.safeline_state_file == str(tmp_path / "data/safeline.sqlite3")
    assert config.temperature_sources_file is None
    directory = tmp_path / "custom-data"
    for name, value in {
        "PVE_PLUGINS_DATA_DIR": str(directory), "PVE_PLUGINS_INJECTION_ENABLED": "false",
        "PVE_PLUGINS_RUNTIME_DIR": "/internal/runtime", "PVE_ADMIN_USERS": "admin@pve, root@pam",
        "PVE_TLS_VERIFY": "false",
    }.items():
        monkeypatch.setenv(name,value)
    config = Settings.from_env()
    assert config.injection_enabled is False and config.runtime_dir == "/internal/runtime"
    assert config.admin_users == ("admin@pve", "root@pam") and config.tls_verify is False
    assert config.config_state_file == str(directory / "plugins.json")
    assert config.action_state_file == str(directory / "actions.sqlite3")
    assert config.guest_links_file == str(directory / "guest-links.sqlite3")
    assert config.site_state_file == str(directory / "sites.sqlite3")
    assert config.safeline_state_file == str(directory / "safeline.sqlite3")
    assert config.komodo_url is config.komodo_api_key is config.komodo_api_secret is None
