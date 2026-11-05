"""Host settings apply atomically, survive restarts and do not expose secrets."""

import asyncio
from dataclasses import replace
import json
import os

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app
from test_app import DESKTOP


@pytest.fixture
def settings(tmp_path):
    return Settings("https://pve.example:8006", tls_verify=False, enabled_plugins=(),
                    config_state_file=str(tmp_path / "plugins.json"))


@pytest.fixture
def authorized(monkeypatch):
    async def authenticate(self, request):
        return "root@pam"
    monkeypatch.setattr("app.main.PveAuthenticator.authenticate", authenticate)


def client_for(settings):
    return TestClient(create_app(settings, transport=httpx.MockTransport(lambda request: httpx.Response(
        200, content=DESKTOP, headers={"content-type": "text/html"}))),
        cookies={"PVEAuthCookie": "ticket-for-test"})


def save(client, data, enabled, modules=None):
    value = {"expected_revision": data["revision"], "enabled_plugins": enabled}
    if modules is not None:
        value["modules"] = modules
    return client.put("/pve-plugins/api/config", json=value,
                      headers={"x-pve-plugins-csrf": data["csrf_token"]})


def test_host_manager_available_with_every_plugin_disabled(settings):
    with client_for(settings) as client:
        assert client.get("/pve-plugins/api/config").status_code == 401
        response = client.get("/")
        assert b"/pve-plugins/assets/core/manager.js" in response.content
        assert b"/assets/sites/" in response.content
        assert client.get("/pve-plugins/assets/core/manager.js").status_code == 200
    assert not os.path.exists(settings.config_state_file)


def test_config_requires_csrf_and_same_origin(settings, authorized):
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        body = {"expected_revision": 0, "enabled_plugins": ["sites"]}
        assert client.put("/pve-plugins/api/config", json=body).status_code == 403
        assert client.put("/pve-plugins/api/config", json=body, headers={
            "x-pve-plugins-csrf": data["csrf_token"], "origin": "https://evil.invalid"}).status_code == 403
        assert save(client, data, ["sites"]).status_code == 200


def test_enable_disable_optional_routes_and_reboot_keep_builtin_sites(settings, authorized):
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        assert data["enabled_plugins"] == ["sites"]
        assert client.get("/pve-plugins/api/komodo/connection").status_code == 404
        response = save(client, data, ["komodo"])
        assert response.status_code == 200
        data = response.json()
        assert data["reload_required"] is True
        assert client.get("/pve-plugins/api/komodo/connection").status_code == 200
        assert client.get("/pve-plugins/assets/sites/sites.js").status_code == 200
        assert client.get("/pve-plugins/health").json()["plugins"] == ["komodo", "sites"]
        response = save(client, data, [])
        assert response.status_code == 200 and response.json()["enabled_plugins"] == ["sites"]
        assert client.get("/pve-plugins/api/komodo/connection").status_code == 404
        assert client.get("/pve-plugins/api/sites").status_code == 200
        assert client.get("/pve-plugins/assets/sites/sites.js").status_code == 200
        assert b"/assets/sites/" in client.get("/").content
        assert b"/assets/core/manager.js" in client.get("/").content
    with client_for(replace(settings, enabled_plugins=("komodo", "sites"))) as client:
        assert client.get("/pve-plugins/health").json()["plugins"] == ["sites"]
    assert os.stat(settings.config_state_file).st_mode & 0o777 == 0o600


def test_secret_masking_preservation_and_explicit_clear_survives_restart(settings, authorized):
    injected = replace(settings, komodo_url="http://core:9120", komodo_api_key="private-key", komodo_api_secret="private-secret")
    with client_for(injected) as client:
        data = client.get("/pve-plugins/api/config").json()
        assert "private-key" not in json.dumps(data) and "private-secret" not in json.dumps(data)
        module = {"url": "http://core:9120", "api_key": "", "api_secret": "", "tls_verify": True}
        response = save(client, data, [], {"komodo": module})
        assert response.json()["modules"]["komodo"]["api_key_configured"] is True
        data = response.json()
        changed = {**module, "url": "http://new-core:9120"}
        assert save(client, data, [], {"komodo": changed}).status_code == 422
        module["clear_credentials"] = True
        response = save(client, data, [], {"komodo": module})
        assert response.json()["modules"]["komodo"]["api_key_configured"] is False
    with client_for(injected) as client:
        data = client.get("/pve-plugins/api/config").json()
        assert not data["modules"]["komodo"]["api_key_configured"]
        assert not data["modules"]["komodo"]["api_secret_configured"]


def test_stale_revision_and_invalid_module_leave_runtime_unchanged(settings, authorized):
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        assert save(client, data, ["sites"]).status_code == 200
        assert save(client, data, []).status_code == 409
        current = client.get("/pve-plugins/api/config").json()
        invalid = {"temperatures": {"nodes": {"pve-a": {"url": "http://pve-a:9100", "type": "node_exporter"}}}}
        assert save(client, current, ["temperatures"], invalid).status_code == 422
        assert client.get("/pve-plugins/health").json()["plugins"] == ["sites"]
        assert client.get("/pve-plugins/api/config").json()["revision"] == 1
        assert save(client, current, ["temperatures"], {"temperatures": {"nodes": {"pve-a": {
            "url": "http://pve-a:9100", "type": "node_exporter", "allow_http": True}}}}).status_code == 200


def test_duplicate_keys_rejected_without_echoing_secrets(settings, authorized):
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        response = client.put("/pve-plugins/api/config", content='{"enabled_plugins":[],"enabled_plugins":["sites"],"expected_revision":0,"secret":"private"}',
                              headers={"content-type": "application/json", "x-pve-plugins-csrf": data["csrf_token"]})
        assert response.status_code == 422 and "private" not in response.text


def test_retired_runtime_finishes_active_request_before_shutdown():
    from fastapi import FastAPI
    from app.plugin_runtime import Generation, PluginGateway
    from app.plugins import PluginHandle
    events = []
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        old = FastAPI()
        @old.get("/pve-plugins/api/test")
        async def long_request():
            started.set()
            await finish.wait()
            events.append("finished")
            return {"ok": True}
        async def shutdown():
            events.append("closed")
        gateway = PluginGateway(Generation(old, None, {"sample": PluginHandle(shutdown)}))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://host") as client:
            request = asyncio.create_task(client.get("/pve-plugins/api/test"))
            await started.wait()
            gateway.replace(Generation(FastAPI(), None, {}))
            await asyncio.sleep(0)
            assert events == []
            assert (await client.get("/pve-plugins/api/test")).status_code == 404
            finish.set()
            assert (await request).status_code == 200
            await gateway.close()
        assert events == ["finished", "closed"]
    asyncio.run(scenario())


def test_failed_generation_closes_already_initialized_handles(settings, authorized, monkeypatch):
    from app.plugins import PluginHandle
    events = []
    def setup(spec, context):
        if spec.id == "temperatures":
            raise ValueError("private-invalid-configuration")
        async def close():
            events.append(spec.id)
        return PluginHandle(shutdown=close)
    monkeypatch.setattr("app.plugin_runtime.load_plugin", setup)
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        response = save(client, data, ["sites", "temperatures"])
        assert response.status_code == 422 and "private-invalid" not in response.text
        assert events == ["sites"]
        assert client.get("/pve-plugins/health").json()["plugins"] == ["sites"]
        assert not os.path.exists(settings.config_state_file)


def test_configuration_change_revokes_active_websocket_and_runs_cleanup():
    from fastapi import FastAPI, WebSocket
    from app.plugin_runtime import Generation, PluginGateway
    async def scenario():
        started = asyncio.Event()
        frames, events = [], []
        old = FastAPI()
        @old.websocket("/pve-plugins/api/test")
        async def terminal(socket: WebSocket):
            await socket.accept()
            started.set()
            try:
                await socket.receive_text()
            finally:
                events.append("terminal-cleanup")
        gateway = PluginGateway(Generation(old, None, {}))
        incoming = asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        async def receive():
            return await incoming.get()
        async def send(frame):
            frames.append(frame)
        task = asyncio.create_task(gateway({"type": "websocket", "path": "/pve-plugins/api/test",
                                            "scheme": "ws", "query_string": b"", "headers": [],
                                            "root_path": "", "client": ("host", 1), "server": ("host", 80)}, receive, send))
        await started.wait()
        gateway.replace(Generation(FastAPI(), None, {}))
        with pytest.raises(asyncio.CancelledError):
            await task
        await gateway.close()
        assert any(frame["type"] == "websocket.close" and frame["code"] == 1012 for frame in frames)
        assert events == ["terminal-cleanup"]
    asyncio.run(scenario())


def test_failed_persistence_preserves_live_runtime(settings, authorized, tmp_path):
    parent = tmp_path / "unwritable-parent"
    parent.write_text("file prevents directory creation")
    with client_for(replace(settings, config_state_file=str(parent / "plugins.json"))) as client:
        data = client.get("/pve-plugins/api/config").json()
        response = save(client, data, ["sites"])
        assert response.status_code == 503
        assert client.get("/pve-plugins/health").json()["plugins"] == ["sites"]
        assert client.get("/pve-plugins/api/config").json()["revision"] == 0


def save_navigation(client, data, navigation, *, modules=None):
    body = {"expected_revision": data["revision"], "enabled_plugins": data["enabled_plugins"], "navigation": navigation}
    if modules is not None:
        body["modules"] = modules
    return client.put("/pve-plugins/api/config", json=body,
                      headers={"x-pve-plugins-csrf": data["csrf_token"]})


def test_navigation_endpoint_is_authenticated_and_contains_only_preferences(settings, authorized):
    from app.configuration import default_navigation
    with client_for(settings) as client:
        response = client.get("/pve-plugins/api/navigation")
        assert response.status_code == 200
        assert response.json() == {"revision": 0, "navigation": default_navigation()}
        assert response.headers["cache-control"] == "private, no-store"
        assert client.get("/pve-plugins/api/navigation?node=pve-a").status_code == 422
        assert client.post("/pve-plugins/api/navigation").status_code == 405


def test_navigation_endpoint_requires_pve_session_with_all_modules_disabled(settings):
    with client_for(settings) as client:
        assert client.get("/pve-plugins/api/navigation").status_code == 401


@pytest.mark.parametrize("navigation", [
    None, [], {}, {"module_order": ["komodo", "temperatures", "safeline", "sites"]},
    {"module_order": ["komodo", "temperatures", "safeline", "sites"], "view_order": [], "extra": True},
    {"module_order": ["komodo", "temperatures", "sites", "sites"], "view_order": []},
    {"module_order": ["komodo", "temperatures", "safeline", True], "view_order": []},
    {"module_order": ["komodo", "temperatures", "safeline", []], "view_order": []},
    {"module_order": ["komodo", "temperatures", "safeline", "unknown"], "view_order": []},
    *[{"module_order": ["komodo", "temperatures", "safeline", "sites"], "view_order": views} for views in [
        None, "pve-plugin-manager", [True], [[]], ["server"], ["pve-extra-sites"], ["__proto__"],
        ["pve-extra-site-" + "A" * 32], ["pve-extra-site-" + "a" * 31],
        ["pve-plugin-manager", "pve-plugin-manager"],
        ["pve-extra-site-" + f"{i:032x}" for i in range(104)],
    ]],
])
def test_navigation_validation_rejects_unknown_native_and_duplicate_ids(settings, authorized, navigation):
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        response = save_navigation(client, data, navigation)
        assert response.status_code == 422
        assert client.get("/pve-plugins/api/navigation").json()["revision"] == 0
        assert not os.path.exists(settings.config_state_file)


def test_navigation_accepts_all_site_slots_without_initializing_site_storage(settings, authorized):
    from app.configuration import default_navigation
    navigation = default_navigation()
    navigation["entry_order"].extend("pve-extra-site-" + f"{i:032x}" for i in range(100))
    with client_for(settings) as client:
        response = save_navigation(client, client.get("/pve-plugins/api/config").json(), navigation)
        assert response.status_code == 200
        assert len(response.json()["navigation"]["entry_order"]) == 103
        assert client.get("/pve-plugins/health").json()["plugins"] == ["sites"]
    assert not os.path.exists(os.path.join(os.path.dirname(settings.config_state_file), "sites.sqlite3"))


def test_navigation_only_save_keeps_generation_and_existing_websocket(settings, authorized, monkeypatch):
    from fastapi import WebSocket
    from app.configuration import default_navigation
    with client_for(settings) as client:
        generation = client.app.state.plugin_runtime
        gateway = client.app.router.routes[-1]
        @gateway.generation.application.websocket("/pve-plugins/api/preference-test")
        async def terminal(socket: WebSocket):
            await socket.accept()
            try:
                while True:
                    await socket.send_text(await socket.receive_text())
            except Exception:
                pass
        def unexpected_build(*args, **kwargs):
            pytest.fail("A preference-only save must not rebuild plugin clients")
        monkeypatch.setattr("app.main.build_generation", unexpected_build)
        data = client.get("/pve-plugins/api/config").json()
        navigation = default_navigation()
        navigation["entry_order"].reverse()
        navigation["module_icons"] = {"komodo": {"type": "pve", "value": "fa fa-server"}}
        with client.websocket_connect("/pve-plugins/api/preference-test") as terminal_client:
            response = save_navigation(client, data, navigation, modules={"komodo": {
                "url": "", "api_key": "", "api_secret": "", "tls_verify": True}})
            assert response.status_code == 200
            assert response.json()["reload_required"] is False
            assert client.app.state.plugin_runtime is generation
            terminal_client.send_text("still-connected")
            assert terminal_client.receive_text() == "still-connected"


def test_module_aliases_persist_trim_and_reset_without_replacing_clients(settings, authorized, monkeypatch):
    from app.configuration import default_navigation
    import app.main as main_module
    original_builder = main_module.build_generation
    with client_for(settings) as client:
        runtime = client.app.state.plugin_runtime
        data = client.get("/pve-plugins/api/config").json()
        navigation = default_navigation()
        navigation["module_aliases"] = {"komodo": "  生产容器  ", "temperatures": "温度", "safeline": "防火墙"}
        def unexpected_build(*args, **kwargs):
            pytest.fail("Saving module aliases must preserve running plugin clients")
        monkeypatch.setattr("app.main.build_generation", unexpected_build)
        response = save_navigation(client, data, navigation)
        assert response.status_code == 200
        assert response.json()["reload_required"] is False
        assert client.app.state.plugin_runtime is runtime
        aliases = response.json()["navigation"]["module_aliases"]
        assert aliases == {"komodo": "生产容器", "temperatures": "温度", "safeline": "防火墙"}
        assert client.get("/pve-plugins/api/navigation").json()["navigation"]["module_aliases"] == aliases
    monkeypatch.setattr("app.main.build_generation", original_builder)
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        assert data["navigation"]["module_aliases"] == aliases
        reset = {**data["navigation"], "module_aliases": {**aliases, "komodo": " "}}
        response = save_navigation(client, data, reset)
        assert response.status_code == 200
        assert response.json()["navigation"]["module_aliases"] == {"temperatures": "温度", "safeline": "防火墙"}
        response = save_navigation(client, response.json(), {**reset, "module_aliases": {}})
        assert response.status_code == 200
        assert response.json()["navigation"]["module_aliases"] == {}


@pytest.mark.parametrize("aliases", [
    None, [], "Komodo", {"unknown": "名称"}, {"__proto__": "名称"},
    {"komodo": None}, {"komodo": True}, {"komodo": 42}, {"komodo": []},
    {"komodo": "x" * 101}, {"sites": "站点\n管理"}, {"safeline": "WAF\t配置"},
    {"temperatures": "\x00温度"}, {"komodo": "名称\x7f"}, {"komodo": "名称\x85"},
])
def test_invalid_module_aliases_are_rejected_without_changing_saved_preferences(settings, authorized, aliases):
    from app.configuration import default_navigation
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        response = save_navigation(client, data, {**default_navigation(), "module_aliases": aliases})
        assert response.status_code == 422
        assert client.get("/pve-plugins/api/navigation").json() == {"revision": 0, "navigation": default_navigation()}
        assert not os.path.exists(settings.config_state_file)


def test_module_alias_accepts_100_character_labels_and_retains_literal_markup(settings, authorized):
    from app.configuration import default_navigation
    aliases = {"komodo": "器" * 100, "safeline": '<img src=x onerror="alert(1)">'}
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        response = save_navigation(client, data, {**default_navigation(), "module_aliases": aliases})
        assert response.status_code == 200
        assert response.json()["navigation"]["module_aliases"] == aliases


def test_module_icons_persist_preserve_when_navigation_is_omitted_and_reset(settings, authorized):
    from app.configuration import default_navigation
    icons = {"komodo": {"type": "pve", "value": "fa fa-server"},
             "safeline": {"type": "url", "value": "https://assets.example/icons/tools.svg?theme=dark#logo"}}
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        assert data["navigation"]["module_icons"] == {}
        navigation = {**default_navigation(), "module_icons": icons}
        response = save_navigation(client, data, navigation)
        assert response.status_code == 200 and response.json()["reload_required"] is False
        data = response.json()
        response = save(client, data, data["enabled_plugins"])
        assert response.status_code == 200
        assert response.json()["navigation"]["module_icons"] == icons
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        assert data["navigation"]["module_icons"] == icons
        reset = {**data["navigation"], "module_icons": {}}
        response = save_navigation(client, data, reset)
        assert response.status_code == 200
        assert response.json()["navigation"]["module_icons"] == {}


@pytest.mark.parametrize("icons", [
    None, [], "fa fa-cubes", {"unknown": {"type": "pve", "value": "fa fa-cubes"}},
    {"__proto__": {"type": "pve", "value": "fa fa-cubes"}},
    *[{"komodo": specification} for specification in [
        None, "fa fa-cubes", {}, {"type": "pve"}, {"type": "pve", "value": True},
        {"type": "pve", "value": "fa fa-cubes", "extra": "x"},
        {"type": "svg", "value": "fa fa-cubes"},
        *[{"type": "pve", "value": classes} for classes in [
            "fa-cubes", "fa fa-Cubes", "fa fa-cubes fa-spin", "fa fa-cubes\n", "fl fa-cubes", "fa fa-cubes\" onclick=\"x", "fa  fa-cubes",
        ]],
        *[{"type": "url", "value": url} for url in [
            "", "//example.com/icon.png", "data:image/svg+xml,x", "javascript:alert(1)", "file:///etc/passwd",
            "https://user:pass@example.com/icon.png", "https://@example.com/icon.png", "https://example.com/icon.png\n",
            " https://example.com/icon.png", "https://example.com/icon name.png", "https://example.com/icon\x85.png",
            "https://example.com/\ud800", "https://example.com\\@evil.example/icon.png", "https://", "https://bad..host/icon.png",
            "https://example.com:0/icon.png", "https://example.com:65536/icon.png", "https://example.com:/icon.png",
            "https://example.com/" + "x" * 2048,
        ]],
    ]],
])
def test_invalid_module_icons_are_rejected_atomically(settings, authorized, icons):
    from app.configuration import default_navigation
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        body = {"expected_revision": data["revision"], "enabled_plugins": data["enabled_plugins"],
                "navigation": {**default_navigation(), "module_icons": icons}}
        response = client.put("/pve-plugins/api/config", content=json.dumps(body).encode("ascii"),
                              headers={"content-type": "application/json", "x-pve-plugins-csrf": data["csrf_token"]})
        assert response.status_code == 422
        assert client.get("/pve-plugins/api/navigation").json() == {"revision": 0, "navigation": default_navigation()}
        assert not os.path.exists(settings.config_state_file)


def test_valid_native_and_url_icons_preserve_literals(settings, authorized):
    from app.configuration import default_navigation
    icons = {"komodo": {"type": "pve", "value": "fa fa-cubes"},
             "temperatures": {"type": "pve", "value": "fl fl-proxmox"},
             "safeline": {"type": "url", "value": "http://[fd00::1]:3000/logo.svg"}}
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        response = save_navigation(client, data, {**default_navigation(), "module_icons": icons})
        assert response.status_code == 200
        assert response.json()["navigation"]["module_icons"] == icons


@pytest.mark.parametrize("order", [None, "komodo", [True], [[]], ["sites"], ["server"], ["pve-plugin-manager"],
    ["pve-komodo-containers"], ["komodo", "komodo"], ["pve-extra-site-" + "A" * 32],
    ["pve-extra-site-" + f"{i:032x}" for i in range(101)]])
def test_unified_entry_order_rejects_groups_native_views_duplicates_and_overflow(settings, authorized, order):
    from app.configuration import default_navigation
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        response = save_navigation(client, data, {**default_navigation(), "entry_order": order})
        assert response.status_code == 422
        assert client.get("/pve-plugins/api/navigation").json()["revision"] == 0


def test_unified_mixed_order_keeps_one_complete_global_sequence(settings, authorized):
    from app.configuration import default_navigation
    site = "pve-extra-site-" + "b" * 32
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        response = save_navigation(client, data, {**default_navigation(), "entry_order": [site, "safeline", "komodo", "temperatures"]})
        assert response.status_code == 200
        assert response.json()["navigation"]["entry_order"] == [site, "safeline", "komodo", "temperatures"]
        assert response.json()["reload_required"] is False


def test_builtin_sites_cannot_be_removed_and_resolved_order_keeps_existing_clients(settings, authorized, monkeypatch):
    settings = replace(settings, enabled_plugins=("sites", "komodo"))
    with client_for(settings) as client:
        runtime = client.app.state.plugin_runtime
        data = client.get("/pve-plugins/api/config").json()
        def fail(*args, **kwargs):
            pytest.fail("Equivalent plugin selections must preserve running clients")
        monkeypatch.setattr("app.main.build_generation", fail)
        response = save(client, data, ["komodo"])
        assert response.status_code == 200
        assert response.json()["enabled_plugins"] == ["komodo", "sites"]
        assert response.json()["reload_required"] is False
        assert client.app.state.plugin_runtime is runtime
        assert client.get("/pve-plugins/assets/sites/sites.js").status_code == 200


def test_schema_two_defaults_ignore_removed_module_environment_and_survive_save(monkeypatch, tmp_path, authorized):
    monkeypatch.setenv("PVE_UPSTREAM", "192.0.2.10")
    monkeypatch.setenv("PVE_PLUGINS_DATA_DIR", str(tmp_path))
    for key, value in {"PVE_PLUGINS": "komodo,unknown", "PVE_KOMODO_URL": "invalid-origin",
                       "PVE_KOMODO_API_KEY": "invalid\nheader", "PVE_KOMODO_TLS_VERIFY": "invalid",
                       "PVE_TEMPERATURES_SOURCES_FILE": str(tmp_path / "missing.json")}.items():
        monkeypatch.setenv(key, value)
    settings = Settings.from_env()
    assert settings.enabled_plugins == ("sites",)
    assert settings.komodo_url is settings.komodo_api_key is settings.temperature_sources_file is None
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        response = save_navigation(client, data, data["navigation"])
        assert response.status_code == 200
    from pathlib import Path
    persisted = json.loads(Path(settings.config_state_file).read_text())
    assert persisted["schema_version"] == 2
    assert persisted["modules"]["komodo"] == {"url": "", "api_key": "", "api_secret": "", "tls_verify": True}
    with client_for(Settings.from_env()) as client:
        assert client.get("/pve-plugins/health").json()["plugins"] == ["sites"]


@pytest.mark.parametrize("changes", [
    {"komodo_url": "invalid-origin"}, {"komodo_api_key": "bad\nheader"},
    {"komodo_api_secret": "bad secret"}, {"komodo_tls_verify": "false"},
    {"temperature_sources_file": "sources.json"},
])
def test_explicit_invalid_module_injection_fails_before_saving(settings, changes):
    with pytest.raises(ValueError, match="Invalid initial plugin settings"):
        create_app(replace(settings, **changes))
    assert not os.path.exists(settings.config_state_file)


@pytest.mark.parametrize("case", ["schema1", "revision0", "missing_navigation", "missing_modules", "missing_komodo_secret",
    "missing_aliases", "missing_icons", "legacy_navigation", "missing_builtin", "legacy_sites_alias", "legacy_sites_icon"])
def test_saved_configuration_requires_complete_current_schema(settings, authorized, case):
    from pathlib import Path
    with client_for(settings) as client:
        assert save(client, client.get("/pve-plugins/api/config").json(), ["sites"]).status_code == 200
    path = Path(settings.config_state_file)
    value = json.loads(path.read_text())
    if case == "schema1":
        value["schema_version"] = 1
    elif case == "revision0":
        value["revision"] = 0
    elif case == "missing_navigation":
        del value["navigation"]
    elif case == "missing_modules":
        del value["modules"]
    elif case == "missing_komodo_secret":
        del value["modules"]["komodo"]["api_secret"]
    elif case == "missing_aliases":
        del value["navigation"]["module_aliases"]
    elif case == "missing_icons":
        del value["navigation"]["module_icons"]
    elif case == "legacy_navigation":
        value["navigation"] = {"module_order": ["komodo", "temperatures", "safeline", "sites"], "view_order": []}
    elif case == "missing_builtin":
        value["navigation"]["entry_order"] = ["komodo", "safeline"]
    elif case == "legacy_sites_alias":
        value["navigation"]["module_aliases"] = {"sites": "old group"}
    elif case == "legacy_sites_icon":
        value["navigation"]["module_icons"] = {"sites": {"type": "pve", "value": "fa fa-globe"}}
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Invalid schema 2 plugin configuration"):
        create_app(settings)
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["entry_order", "module_aliases", "module_icons"])
def test_navigation_put_rejects_missing_fields(settings, authorized, field):
    from app.configuration import default_navigation
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        navigation = default_navigation()
        del navigation[field]
        response = save_navigation(client, data, navigation)
        assert response.status_code == 422
        assert client.get("/pve-plugins/api/config").json()["revision"] == 0
        assert not os.path.exists(settings.config_state_file)


def test_complete_snapshot_limit_rejects_second_valid_request_without_changing_disk_or_runtime(settings, authorized, monkeypatch):
    from pathlib import Path
    from app.configuration import MAX_CONFIG_BYTES
    ca_path = "/" + "a/" * 1999 + "cert.pem"
    nodes = {f"node-{index}": {"url": "https://metrics.example", "type": "node_exporter", "ca_file": ca_path}
             for index in range(63)}
    with client_for(settings) as client:
        data = client.get("/pve-plugins/api/config").json()
        response = save(client, data, ["sites"], {"temperatures": {"nodes": nodes}})
        assert response.status_code == 200
        data = response.json()
        path = Path(settings.config_state_file)
        before = path.read_bytes()
        runtime = client.app.state.plugin_runtime
        import app.main as main_module
        original_builder = main_module.build_generation
        def unexpected_build(*args, **kwargs):
            pytest.fail("Oversized snapshots must fail before initializing replacement plugins")
        monkeypatch.setattr("app.main.build_generation", unexpected_build)
        prefix = "https://icons.example/"
        url = prefix + "a" * (2048 - len(prefix))
        navigation = data["navigation"]
        navigation["module_icons"] = {module: {"type": "url", "value": url} for module in ("komodo", "temperatures", "safeline")}
        body = {"expected_revision": data["revision"], "enabled_plugins": ["sites"], "navigation": navigation}
        assert len(json.dumps(body).encode()) < MAX_CONFIG_BYTES
        response = save_navigation(client, data, navigation)
        assert response.status_code == 413
        assert response.json()["detail"] == "plugin_configuration_too_large"
        assert path.read_bytes() == before
        assert client.app.state.plugin_runtime is runtime
        assert client.get("/pve-plugins/api/config").json()["revision"] == data["revision"]
    monkeypatch.setattr("app.main.build_generation", original_builder)
    with client_for(settings) as client:
        assert client.get("/pve-plugins/health").status_code == 200
        assert client.get("/pve-plugins/api/config").json()["revision"] == data["revision"]
