"""Private, revisioned host configuration; plugin packages stay lazy imports."""

from copy import deepcopy
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from urllib.parse import urlsplit

from fastapi import HTTPException

from app.icons import validate_module_icons


MAX_CONFIG_BYTES = 262144
ENTRY_MODULES = ("komodo", "temperatures", "safeline")
SCHEMA_VERSION = 2


def default_navigation():
    return {"entry_order": list(ENTRY_MODULES), "module_aliases": {}, "module_icons": {}}


def object_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def invalid_constant(value):
    raise ValueError


def decode(body):
    return json.loads(body, object_pairs_hook=object_pairs, parse_constant=invalid_constant)


def _text(value, maximum=4096):
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError
    return value


def _origin(value):
    _text(value, 512)
    if not value:
        return ""
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or any(char in value for char in "\\?#")
            or any(ord(char) <= 32 or ord(char) >= 127 for char in value)
            or parsed.netloc.endswith(":")):
        raise ValueError
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError
    return value.rstrip("/")


def _secret(value):
    _text(value)
    if any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        raise ValueError
    return value


class ConfigurationStore:
    def __init__(self, settings, catalog):
        self.path = Path(settings.config_state_file) if settings.config_state_file else None
        self.catalog = catalog
        self.settings = settings
        self.has_saved = False
        if self.path and self.path.exists():
            try:
                if self.path.stat().st_size > MAX_CONFIG_BYTES:
                    raise ValueError
                self.value = self._snapshot(decode(self.path.read_bytes()))
                self.has_saved = True
            except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                raise ValueError("Invalid schema 2 plugin configuration") from None
        else:
            try:
                if settings.temperature_sources_file is not None:
                    raise ValueError
                initial = {
                    "schema_version": SCHEMA_VERSION, "revision": 0,
                    "enabled_plugins": self._enabled(settings.enabled_plugins),
                    "modules": {
                        "komodo": {"url": settings.komodo_url or "", "api_key": settings.komodo_api_key or "",
                                   "api_secret": settings.komodo_api_secret or "", "tls_verify": settings.komodo_tls_verify},
                        "temperatures": deepcopy(settings.temperature_sources) if settings.temperature_sources is not None else {"nodes": {}},
                    },
                    "navigation": default_navigation(),
                }
                self.value = self._snapshot(initial, persisted=False)
                self._encode(self.value)
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, HTTPException):
                raise ValueError("Invalid initial plugin settings") from None

    def _snapshot(self, value, *, persisted=True):
        if (not isinstance(value, dict)
                or set(value) != {"schema_version", "revision", "enabled_plugins", "modules", "navigation"}
                or type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION
                or type(value["revision"]) is not int or value["revision"] < (1 if persisted else 0)
                or not isinstance(value["enabled_plugins"], list)
                or not isinstance(value["modules"], dict) or set(value["modules"]) != {"komodo", "temperatures"}):
            raise ValueError
        komodo = value["modules"]["komodo"]
        if (not isinstance(komodo, dict) or set(komodo) != {"url", "api_key", "api_secret", "tls_verify"}
                or type(komodo["tls_verify"]) is not bool):
            raise ValueError
        normalized = {
            "schema_version": SCHEMA_VERSION, "revision": value["revision"],
            "enabled_plugins": self._enabled(value["enabled_plugins"]),
            "modules": {
                "komodo": {"url": _origin(komodo["url"]), "api_key": _secret(komodo["api_key"]),
                           "api_secret": _secret(komodo["api_secret"]), "tls_verify": komodo["tls_verify"]},
                "temperatures": self._temperatures(value["modules"]["temperatures"], {}),
            },
            "navigation": self._navigation(value["navigation"]),
        }
        if persisted and normalized != value:
            raise ValueError
        return normalized

    @staticmethod
    def _encode(value):
        body = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if len(body) > MAX_CONFIG_BYTES:
            raise HTTPException(413, "plugin_configuration_too_large")
        return body

    def public(self):
        result = deepcopy(self.value)
        result.pop("schema_version")
        komodo = result["modules"]["komodo"]
        for key in ("api_key", "api_secret"):
            komodo[key + "_configured"] = bool(komodo.pop(key))
        for source in result["modules"]["temperatures"]["nodes"].values():
            source["token_configured"] = bool(source.pop("token", None))
        return result

    def prepare(self, command):
        try:
            if (not isinstance(command, dict) or set(command) - {"expected_revision", "enabled_plugins", "modules", "navigation"}
                    or not {"expected_revision", "enabled_plugins"} <= set(command)
                    or type(command["expected_revision"]) is not int or command["expected_revision"] < 0):
                raise ValueError
            if command["expected_revision"] != self.value["revision"]:
                raise HTTPException(409, "plugin_configuration_changed")
            enabled = command["enabled_plugins"]
            if (not isinstance(enabled, list) or any(not isinstance(item, str) or item not in self.catalog for item in enabled)
                    or len(set(enabled)) != len(enabled)):
                raise ValueError
            modules = command.get("modules", {})
            if not isinstance(modules, dict) or set(modules) - {"komodo", "temperatures"}:
                raise ValueError
            result = deepcopy(self.value)
            result["revision"] += 1
            result["enabled_plugins"] = self._enabled(enabled)
            if "navigation" in command:
                result["navigation"] = self._navigation(command["navigation"])
            if "komodo" in modules:
                current = result["modules"]["komodo"]
                value = modules["komodo"]
                if (not isinstance(value, dict) or set(value) - {"url", "api_key", "api_secret", "tls_verify", "clear_credentials"}
                        or not {"url", "tls_verify"} <= set(value) or type(value["tls_verify"]) is not bool
                        or type(value.get("clear_credentials", False)) is not bool):
                    raise ValueError
                url = _origin(value["url"])
                clear = value.get("clear_credentials", False)
                credentials = {}
                for key in ("api_key", "api_secret"):
                    secret = _secret(value.get(key, ""))
                    if clear and secret:
                        raise ValueError
                    if not secret and not clear and url == current["url"]:
                        secret = current[key]
                    elif not secret and not clear and current[key]:
                        raise HTTPException(422, "plugin_credentials_required_for_new_url")
                    credentials[key] = secret
                result["modules"]["komodo"] = {"url": url, "tls_verify": value["tls_verify"], **credentials}
            if "temperatures" in modules:
                result["modules"]["temperatures"] = self._temperatures(modules["temperatures"], result["modules"]["temperatures"]["nodes"])
            self._encode(result)
            return result
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            raise HTTPException(422, "invalid_plugin_configuration") from None

    def _enabled(self, enabled):
        if (not isinstance(enabled, (list, tuple))
                or any(not isinstance(item, str) or item not in self.catalog for item in enabled)
                or len(set(enabled)) != len(enabled) or "sites" not in self.catalog):
            raise ValueError
        values = list(enabled)
        if "sites" not in values:
            values.append("sites")
        return values

    @staticmethod
    def _navigation(value):
        if (not isinstance(value, dict) or set(value) != {"entry_order", "module_aliases", "module_icons"}):
            raise ValueError
        entries = value["entry_order"]
        if (not isinstance(entries, list) or not 3 <= len(entries) <= 103
                or any(not isinstance(item, str) or (item not in ENTRY_MODULES
                       and re.fullmatch(r"pve-extra-site-[0-9a-f]{32}", item) is None) for item in entries)
                or len(set(entries)) != len(entries) or not set(ENTRY_MODULES) <= set(entries)):
            raise ValueError
        aliases = value["module_aliases"]
        if not isinstance(aliases, dict) or set(aliases) - set(ENTRY_MODULES):
            raise ValueError
        normalized_aliases = {}
        for module_id, alias in aliases.items():
            if not isinstance(alias, str) or any(unicodedata.category(char) in {"Cc", "Cs"} for char in alias):
                raise ValueError
            alias = alias.strip()
            if len(alias) > 100:
                raise ValueError
            if alias:
                normalized_aliases[module_id] = alias
        return {"entry_order": list(entries), "module_aliases": normalized_aliases,
                "module_icons": validate_module_icons(value["module_icons"])}

    @staticmethod
    def _temperatures(value, current):
        if not isinstance(value, dict) or set(value) != {"nodes"} or not isinstance(value["nodes"], dict) or len(value["nodes"]) > 64:
            raise ValueError
        result = {}
        for node, source in value["nodes"].items():
            if (not isinstance(node, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", node) is None
                    or not isinstance(source, dict) or set(source) - {"url", "type", "allow_http", "token", "ca_file", "clear_token"}
                    or "url" not in source or source.get("type", "collector") not in {"collector", "node_exporter"}
                    or type(source.get("allow_http", False)) is not bool or type(source.get("clear_token", False)) is not bool):
                raise ValueError
            item = {"url": _origin(source["url"]), "type": source.get("type", "collector"), "allow_http": source.get("allow_http", False)}
            if not item["url"]:
                raise ValueError
            previous = current.get(node, {})
            token = _secret(source.get("token", ""))
            if not token and not source.get("clear_token", False) and previous.get("url") == item["url"]:
                token = previous.get("token", "")
            if token:
                if not 32 <= len(token) <= 256 or source.get("clear_token", False):
                    raise ValueError
                item["token"] = token
            if item["type"] == "collector" and not token:
                raise ValueError
            if source.get("ca_file"):
                ca_file = _text(source["ca_file"], 4096)
                if not Path(ca_file).is_absolute():
                    raise ValueError
                item["ca_file"] = ca_file
            result[node] = item
        return {"nodes": result}

    def effective_settings(self, value=None):
        value = value or self.value
        komodo = value["modules"]["komodo"]
        settings = replace(self.settings, enabled_plugins=tuple(value["enabled_plugins"]),
                           komodo_url=komodo["url"] or None, komodo_api_key=komodo["api_key"] or None,
                           komodo_api_secret=komodo["api_secret"] or None, komodo_tls_verify=komodo["tls_verify"],
                           temperature_sources=value["modules"]["temperatures"], temperature_sources_file=None)
        if self.path:
            settings = replace(settings,
                               action_state_file=settings.action_state_file or str(self.path.with_name("actions.sqlite3")),
                               guest_links_file=settings.guest_links_file or str(self.path.with_name("guest-links.sqlite3")),
                               site_state_file=settings.site_state_file or str(self.path.with_name("sites.sqlite3")),
                               safeline_state_file=settings.safeline_state_file or str(self.path.with_name("safeline.sqlite3")))
        return settings

    def save(self, value):
        try:
            value = self._snapshot(value)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise HTTPException(422, "invalid_plugin_configuration") from None
        body = self._encode(value)
        if self.path:
            temporary = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                lock_path = self.path.with_suffix(self.path.suffix + ".lock")
                descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                with os.fdopen(descriptor, "w") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    revision = decode(self.path.read_bytes())["revision"] if self.path.exists() else 0
                    if revision != self.value["revision"]:
                        raise HTTPException(409, "plugin_configuration_changed")
                    with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".plugins-", delete=False) as handle:
                        temporary = Path(handle.name)
                        handle.write(body)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, self.path)
            except (OSError, ValueError, TypeError, KeyError):
                raise HTTPException(503, "plugin_configuration_write_failed") from None
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        self.value = value
        self.has_saved = True
