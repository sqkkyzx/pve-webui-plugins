"""Discover packaged plugins, resolve dependencies, and render startup templates.

Manifests are trusted application files, never browser input. Disabled plugins
are not imported, do not register routes, and do not have their assets read.
"""

from dataclasses import dataclass, field
import hashlib
import html
import importlib
import json
import os
from pathlib import Path
import re
from string import Template
import tempfile
from typing import Any, Awaitable, Callable


ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = ROOT / "plugins"
TEMPLATES_DIR = ROOT / "templates"
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
ASSET_URL = re.compile(r"/(?:pve-komodo|pve-plugins)/assets/[a-zA-Z0-9_./-]+\.js\Z")


@dataclass(frozen=True)
class Asset:
    path: Path
    url: str


@dataclass(frozen=True)
class Plugin:
    id: str
    name: str
    version: str
    entrypoint: str
    requires: tuple[str, ...]
    assets: tuple[Asset, ...]
    after: tuple[str, ...] = ()

    def public(self):
        return {"id": self.id, "name": self.name, "version": self.version,
                "requires": list(self.requires), "after": list(self.after),
                "assets": [asset.url for asset in self.assets]}


@dataclass
class PluginContext:
    application: Any
    settings: Any
    authenticator: Any
    csrf: Any
    transports: dict = field(default_factory=dict)


@dataclass
class PluginHandle:
    shutdown: Callable[[], Awaitable[None]] | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Runtime:
    plugins: tuple[Plugin, ...]
    head: bytes
    manifest: dict


def discover_plugins(root: Path = PLUGINS_DIR) -> dict[str, Plugin]:
    catalog = {}
    reserved_urls = set()
    for manifest in sorted(root.glob("*/plugin.json")):
        try:
            value = json.loads(manifest.read_text())
            if (not isinstance(value, dict) or set(value) - {"after"} != {
                    "id", "name", "version", "api_version", "entrypoint", "requires", "assets"}
                    or type(value["api_version"]) is not int or value["api_version"] != 1):
                raise ValueError
            plugin_id = value["id"]
            if (not isinstance(plugin_id, str) or IDENTIFIER.fullmatch(plugin_id) is None
                    or plugin_id != manifest.parent.name or plugin_id in catalog):
                raise ValueError
            if (not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 80
                    or any(ord(char) < 32 for char in value["name"])
                    or not isinstance(value["version"], str)
                    or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value["version"]) is None
                    or value["entrypoint"] != f"plugins.{plugin_id}.plugin:setup"):
                raise ValueError
            requires, after = value["requires"], value.get("after", [])
            for dependencies in (requires, after):
                if (not isinstance(dependencies, list) or any(not isinstance(item, str)
                        or IDENTIFIER.fullmatch(item) is None for item in dependencies)
                        or len(set(dependencies)) != len(dependencies) or plugin_id in dependencies):
                    raise ValueError
            if not isinstance(value["assets"], list):
                raise ValueError
            assets = []
            for asset in value["assets"]:
                if (not isinstance(asset, dict) or set(asset) != {"source", "url"}
                        or not isinstance(asset["source"], str) or not asset["source"]
                        or Path(asset["source"]).is_absolute()
                        or ".." in Path(asset["source"]).parts
                        or not isinstance(asset["url"], str) or ASSET_URL.fullmatch(asset["url"]) is None
                        or any(part in {"", ".", ".."} for part in asset["url"].split("/")[1:])
                        or asset["url"] in reserved_urls):
                    raise ValueError
                path = (manifest.parent / asset["source"]).resolve()
                if not path.is_relative_to(manifest.parent.resolve()):
                    raise ValueError
                reserved_urls.add(asset["url"])
                assets.append(Asset(path, asset["url"]))
            catalog[plugin_id] = Plugin(plugin_id, value["name"], value["version"],
                                        value["entrypoint"], tuple(requires), tuple(assets), tuple(after))
        except (ValueError, TypeError, KeyError, OSError):
            raise ValueError(f"Invalid plugin manifest: {manifest.parent.name}") from None
    return catalog


def resolve_plugins(catalog: dict[str, Plugin], enabled: tuple[str, ...]) -> tuple[Plugin, ...]:
    if any(not isinstance(item, str) or IDENTIFIER.fullmatch(item) is None for item in enabled):
        raise ValueError("PVE_PLUGINS must contain comma-separated plugin IDs")
    if len(set(enabled)) != len(enabled):
        raise ValueError("PVE_PLUGINS contains duplicate IDs")
    if set(enabled) - set(catalog):
        raise ValueError("PVE_PLUGINS contains unknown IDs")
    visited, visiting, ordered = set(), set(), []

    def visit(plugin_id):
        if plugin_id in visiting:
            raise ValueError("Plugin dependency cycle")
        if plugin_id in visited:
            return
        visiting.add(plugin_id)
        for dependency in catalog[plugin_id].requires:
            if dependency not in enabled:
                raise ValueError(f"Plugin {plugin_id} requires enabled plugin {dependency}")
            visit(dependency)
        # Shared PVE overrides can require ordering without making another
        # plugin mandatory. An unselected optional predecessor stays disabled.
        for predecessor in catalog[plugin_id].after:
            if predecessor in enabled:
                visit(predecessor)
        visiting.remove(plugin_id)
        visited.add(plugin_id)
        ordered.append(catalog[plugin_id])

    for plugin_id in enabled:
        visit(plugin_id)
    return tuple(ordered)


def _write_generated(folder: Path, name: str, body: bytes):
    folder.mkdir(parents=True, exist_ok=True)
    # Readers never see a partly written file. Generated files contain no
    # session data or credentials and are replaced on every application start.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".render-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(body)
        os.replace(temporary, folder / name)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def render_runtime(plugins: tuple[Plugin, ...], *, output_dir: str | None = None,
                   templates: Path = TEMPLATES_DIR) -> Runtime:
    scripts = []
    for plugin in plugins:
        for asset in plugin.assets:
            digest = hashlib.sha256(asset.path.read_bytes()).hexdigest()[:16]
            scripts.append(f'<script src="{html.escape(asset.url, quote=True)}?v={digest}"></script>')
    head = Template((templates / "desktop-head.html.tmpl").read_text()).substitute(
        plugin_scripts="\n".join(scripts),
    ).encode()
    manifest = {"schema_version": 1, "enabled": [plugin.id for plugin in plugins],
                "plugins": [plugin.public() for plugin in plugins]}
    public_json = Template((templates / "plugins.json.tmpl").read_text()).substitute(
        enabled=json.dumps(manifest["enabled"]), plugins=json.dumps(manifest["plugins"], ensure_ascii=True),
    ).encode()
    if json.loads(public_json) != manifest:
        raise ValueError("Generated plugin manifest does not match selected plugins")
    if output_dir is not None:
        folder = Path(output_dir)
        _write_generated(folder, "desktop-head.html", head)
        _write_generated(folder, "plugins.json", public_json)
    return Runtime(plugins, head, manifest)


def load_plugin(plugin: Plugin, context: PluginContext) -> PluginHandle:
    module, function = plugin.entrypoint.split(":", 1)
    handle = getattr(importlib.import_module(module), function)(context)
    if not isinstance(handle, PluginHandle):
        raise TypeError(f"Plugin {plugin.id} must return a PluginHandle")
    return handle
