"""Narrow, revisioned resource edits; requests and private keys are never stored."""

import asyncio
import copy
import hashlib
import ipaddress
import json
import re
import ssl
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from plugins.safeline.connection import SafeLineError
from plugins.safeline.service import _PROXY, now, policy, rows, safe, take
from plugins.safeline import advanced


Identifier = Annotated[StrictInt, Field(ge=0, le=9_007_199_254_740_991)]
Short = Annotated[StrictStr, Field(max_length=1024)]
Value = Annotated[StrictStr, Field(max_length=4096)]
Strings = Annotated[list[Short], Field(max_length=256)]
_SITE_FIELDS = ("cert_id", "comment", "email", "group_id", "health_check", "health_check_interval", "id", "index",
                "load_balance", "ports", "redirect_status_code", "server_names", "stat_enabled", "static_default", "type", "upstreams")
_POLICY_FIELDS = ("action", "expire", "id", "is_enabled", "level", "log", "name", "pattern")
_PROXY_FIELDS = (*_PROXY, "default_server", "http_headers", "ip_source", "ip_value", "ntlm", "xfh", "xfp")
_PROXY_STRING = {"host", "hsts_max_age", "ssl_ciphers", "ssl_protocols"}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EditCommand(Model):
    expected_revision: Identifier
    fingerprint: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    operation_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")]
    changes: dict


class ApplicationPatch(Model):
    comment: Short = ""
    server_names: Annotated[Strings, Field(min_length=1)] = []
    ports: Annotated[Strings, Field(min_length=1)] = []
    upstreams: Strings = []
    cert_id: Identifier = 0
    health_check: StrictBool = False
    stat_enabled: StrictBool = False


class ModePatch(Model):
    mode: Literal[0, 1, 2]


class Pattern(Model):
    k: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    op: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    sub_k: Short = ""
    v: Annotated[list[Value], Field(max_length=256)]


class PolicyPatch(Model):
    name: Annotated[StrictStr, Field(min_length=1, max_length=1024)] = ""
    log: StrictBool = True
    pattern: Annotated[list[Annotated[list[Pattern], Field(min_length=1, max_length=64)]], Field(min_length=1, max_length=64)] = []


class SwitchPatch(Model):
    is_enabled: StrictBool


class GroupPatch(Model):
    comment: Short = ""
    ips: Annotated[list[Annotated[StrictStr, Field(min_length=1, max_length=128)]], Field(max_length=10000)] = []


class CertificatePatch(Model):
    crt: Annotated[StrictStr, Field(min_length=1, max_length=65536)]
    key: Annotated[StrictStr, Field(max_length=16384)] = ""


class AcmePatch(Model):
    domains: Annotated[Strings, Field(min_length=1)]
    email: Annotated[StrictStr, Field(max_length=320)]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validated(model, changes):
    try:
        value = model.model_validate(changes)
        result = value.model_dump(exclude_unset=True)
        if not result:
            raise ValueError
        # Literal integers also match bool/float in Pydantic; modes must be integers.
        if model is ModePatch and type(changes.get("mode")) is not int:
            raise ValueError
        return result
    except (ValidationError, TypeError, ValueError):
        raise SafeLineError("invalid_safeline_request", 422) from None


def domains(values):
    for value in values:
        if value == "*":
            continue
        host = value[2:] if value.startswith("*.") else value
        if not host or len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise SafeLineError("invalid_safeline_domains", 422)


def pem(value):
    try:
        blocks = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", value, re.S)
        remaining = re.sub(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", "", value, flags=re.S)
        if not blocks or remaining.strip():
            raise ValueError
        for block in blocks:
            if not ssl.PEM_cert_to_DER_cert(block):
                raise ValueError
        return tuple(re.sub(r"\s", "", block) for block in blocks)
    except (ValueError, UnicodeError):
        raise SafeLineError("invalid_safeline_certificate", 422) from None


class ResourceEditor:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self.lock = asyncio.Lock()

    async def raw(self, client, kind, identifier, section):
        if kind == "configuration" and section == "settings":
            return await advanced.read(client, identifier)
        if kind == "applications" and section in {"settings", "mode", "proxy"}:
            value = await client.get("/api/open/site/" + identifier)
            if not isinstance(value, dict) or str(value.get("id")) != identifier:
                raise SafeLineError("safeline_resource_not_found", 404)
            if section == "proxy":
                value = await client.get("/api/open/site/" + identifier + "/proxy")
                if not isinstance(value, dict):
                    raise SafeLineError("safeline_edit_unsupported", 409)
                return take(value, _PROXY_FIELDS)
            result = take(value, (*_SITE_FIELDS, "mode", "is_enabled"))
            if section == "settings":
                try:
                    edition = await client.get("/api/open/system/edition")
                    result["_edition"] = edition.get("effective_edition", edition.get("version"))
                except (SafeLineError, AttributeError):
                    result["_edition"] = None
            return result
        if kind in {"deny", "allow", "policies"} and section in {"settings", "switch"}:
            value = await client.get("/api/open/policy/detail", {"id": identifier})
            if not isinstance(value, dict) or str(value.get("id")) != identifier:
                raise SafeLineError("safeline_resource_not_found", 404)
            actual = {0: "allow", 1: "deny"}.get(value.get("action"), "policies")
            if actual != kind:
                raise SafeLineError("safeline_resource_moved", 409)
            return take(value, (*_POLICY_FIELDS, "builtin", "negate", "order", "updated_at"))
        if kind == "ip-groups" and section == "settings":
            result = await client.get("/api/open/ipgroup/detail", {"id": identifier})
            value = result.get("data") if isinstance(result, dict) else None
            if not isinstance(value, dict) or str(value.get("id")) != identifier:
                raise SafeLineError("safeline_resource_not_found", 404)
            if not isinstance(value.get("ips"), list) or len(value["ips"]) > 10000 or value.get("total", len(value["ips"])) != len(value["ips"]):
                raise SafeLineError("safeline_edit_incomplete", 409)
            return take(value, ("id", "comment", "ips", "builtin", "reference", "total", "updated_at"))
        if kind == "certificates" and section == "settings":
            # Only this edit path reads cert detail; its manual.key never leaves the backend.
            items = rows(await client.get("/api/open/cert"), "nodes")
            if not any(str(item.get("id")) == identifier for item in items):
                raise SafeLineError("safeline_resource_not_found", 404)
            value = await client.get("/api/open/cert/" + identifier)
            if not isinstance(value, dict) or value.get("type") not in {2, 3}:
                raise SafeLineError("safeline_edit_unsupported", 409)
            return take(value, ("type", "manual", "acme"))
        raise SafeLineError("invalid_safeline_resource", 422)

    def values(self, raw, kind, section, token):
        if kind == "configuration":
            return advanced.values(raw, section, token)
        locked = []
        if kind == "applications":
            if section == "mode":
                values = take(raw, ("mode",))
            elif section == "proxy":
                values = {k: v for k, v in raw.items() if k in _PROXY and isinstance(v, dict) and
                          type(v.get("global")) is bool and type(v.get("value")) is (str if k in _PROXY_STRING else bool)}
            else:
                values = take(raw, ApplicationPatch.model_fields)
                if raw.get("_edition") not in {1, 2}:
                    locked.append("stat_enabled")
        elif kind in {"deny", "allow", "policies"}:
            if section == "switch":
                values = take(raw, ("is_enabled",))
            elif raw.get("builtin") or kind == "policies":
                raise SafeLineError("safeline_edit_unsupported", 409)
            else:
                values = take(raw, PolicyPatch.model_fields)
                if not isinstance(values.get("pattern"), list) or not values["pattern"]:
                    locked.append("pattern")
                elif policy(raw)["pattern"] != values["pattern"]:
                    locked.append("pattern")
        elif kind == "ip-groups":
            values = take(raw, ("ips",) if raw.get("builtin") else ("comment", "ips"))
        elif raw["type"] == 2:
            values = {"crt": "", "key": ""}
        else:
            values = take(raw.get("acme") or {}, ("domains", "email"))
        for key, value in list(values.items()):
            if key in locked:
                continue
            if safe(value, token) != value or key == "pattern" and any("\n" in v or "\r" in v for g in value for r in g for v in r.get("v", [])):
                locked.append(key)
        return {k: v for k, v in values.items() if k not in locked}, locked

    async def load(self, kind, identifier, section, expected_revision=None):
        client, revision = self.service.client()
        if client.connection is None:
            raise SafeLineError("safeline_not_configured", 409)
        if expected_revision is not None and expected_revision != revision:
            raise SafeLineError("safeline_connection_changed", 409)
        raw = await self.raw(client, kind, identifier, section)
        values, locked = self.values(raw, kind, identifier if kind == "configuration" else section, client.connection.token)
        return {"values": values, "locked": locked, "certificate_type": raw.get("type") if kind == "certificates" else None,
                "fingerprint": self.resource_fingerprint(client, revision, raw), "connection_revision": revision, "updated_at": now(),
                **({"capabilities": advanced.capabilities(raw)} if kind == "configuration" else {})}

    @staticmethod
    def resource_fingerprint(client, revision, raw):
        return fingerprint({"url": client.connection.url, "tls_server_name": client.connection.tls_server_name,
                            "revision": revision, "resource": raw})

    def build(self, raw, changes, kind, identifier, section):
        path, method = "/api/open/", "PUT"
        if kind == "applications" and section == "mode":
            patch = validated(ModePatch, changes)
            return method, path + "site/mode", {"ids": [int(identifier)], **patch}, patch
        if kind == "applications" and section == "proxy":
            patch = {}
            if not changes or not set(changes) <= set(_PROXY):
                raise SafeLineError("invalid_safeline_request", 422)
            for key, value in changes.items():
                if (not isinstance(value, dict) or set(value) != {"global", "value"} or type(value["global"]) is not bool or
                        type(value["value"]) is not (str if key in _PROXY_STRING else bool) or isinstance(value["value"], str) and len(value["value"]) > 4096):
                    raise SafeLineError("invalid_safeline_request", 422)
                patch[key] = value
            return method, path + "site/" + identifier + "/proxy", {**raw, **patch}, patch
        if kind == "applications":
            patch = validated(ApplicationPatch, changes)
            if "server_names" in patch:
                domains(patch["server_names"])
            for port in patch.get("ports", []):
                match = re.fullmatch(r"([1-9][0-9]{0,4})(_ssl)?", port)
                if not match or int(match[1]) > 65535:
                    raise SafeLineError("invalid_safeline_ports", 422)
            for upstream in patch.get("upstreams", []):
                try:
                    parsed = urlsplit(upstream)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {"", "/"} or parsed.port is not None and not 1 <= parsed.port <= 65535 or any(ord(c) <= 32 for c in upstream):
                        raise ValueError
                except ValueError:
                    raise SafeLineError("invalid_safeline_upstreams", 422) from None
            payload = {**take(raw, _SITE_FIELDS), **patch, "id": int(identifier)}
            if "upstreams" in patch and isinstance(payload.get("load_balance"), dict):
                old = {item["address"]: item for item in payload["load_balance"].get("upstreams") or [] if isinstance(item, dict) and "address" in item}
                payload["load_balance"] = {**payload["load_balance"], "upstreams": [old.get(address, {"address": address, "weight": 1, "role": "primary"}) for address in patch["upstreams"]]}
            return method, path + "site", payload, patch
        if kind in {"deny", "allow", "policies"}:
            if section == "switch":
                patch = validated(SwitchPatch, changes)
                return method, path + "policy/switch", {"id": int(identifier), **patch}, patch
            patch = validated(PolicyPatch, changes)
            return method, path + "policy", {**take(raw, _POLICY_FIELDS), **patch, "id": int(identifier)}, patch
        if kind == "ip-groups":
            patch = validated(GroupPatch, changes)
            for value in patch.get("ips", []):
                try:
                    if "-" in value:
                        first, last = (ipaddress.ip_address(x) for x in value.split("-"))
                        if first.version != last.version or int(first) > int(last):
                            raise ValueError
                    else:
                        ipaddress.ip_network(value, strict=False)
                except ValueError:
                    raise SafeLineError("invalid_safeline_ips", 422) from None
            return method, path + "ipgroup", {**raw, **patch, "id": int(identifier)}, patch
        if raw["type"] == 3:
            patch = validated(AcmePatch, changes)
            if "domains" in patch:
                domains(patch["domains"])
            if patch.get("email") and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", patch["email"]):
                raise SafeLineError("invalid_safeline_request", 422)
            return "POST", path + "cert", {"id": int(identifier), "type": 3, "acme": {**raw.get("acme", {}), **patch}}, patch
        patch = validated(CertificatePatch, changes)
        pem(patch["crt"])
        if patch.get("key") and not re.fullmatch(r"\s*-----BEGIN (?:RSA |EC )?PRIVATE KEY-----[\s\S]+-----END (?:RSA |EC )?PRIVATE KEY-----\s*", patch["key"]):
            raise SafeLineError("invalid_safeline_certificate", 422)
        return "POST", path + "cert", {"id": int(identifier), "type": 2, "manual": {"crt": patch["crt"], "key": patch.get("key", "")}}, patch

    async def save(self, kind, identifier, section, command, actor):
        async with self.lock:
            command_hash = fingerprint({"kind": kind, "id": identifier, "section": section, **command.model_dump()})
            previous = self.store.receipt(command.operation_id, actor, command_hash)
            if previous is not None:
                return {**previous, "operation_id": command.operation_id, "connection_revision": command.expected_revision, "replayed": True}
            client, revision = self.service.client()
            if client.connection is None or revision != command.expected_revision:
                raise SafeLineError("safeline_connection_changed", 409)
            raw = await self.raw(client, kind, identifier, section)
            if self.resource_fingerprint(client, revision, raw) != command.fingerprint:
                raise SafeLineError("safeline_resource_changed", 409)
            available, locked = self.values(raw, kind, identifier if kind == "configuration" else section, client.connection.token)
            if not set(command.changes) <= set(available):
                raise SafeLineError("safeline_edit_unsupported", 409)
            method, path, payload, patch = (advanced.build(raw, identifier, command.changes, client.connection.token) if kind == "configuration" else
                                          self.build(copy.deepcopy(raw), command.changes, kind, identifier, section))
            if self.store.load()[1] != revision:
                raise SafeLineError("safeline_connection_changed", 409)
            # IP membership append/create and full group edits share pending-write protection.
            target = fingerprint({"revision": revision, "kind": "ip-groups"} if kind == "ip-groups" else
                                 {"revision": revision, "kind": "policies" if kind in {"allow", "deny"} else kind, "id": identifier})
            self.store.reserve_edit(command.operation_id, actor, command_hash, target)
            result = {"outcome": "unknown", "code": "safeline_write_unknown"}
            accepted = False
            try:
                await client.write(method, path, payload)
                accepted = True
                updated = await self.raw(client, kind, identifier, section)
                verify = updated
                if kind == "configuration":
                    verify = {"enable": updated["groups"]} if identifier == "groups" else updated.get(identifier, {})
                verified = all(verify.get(k) == v for k, v in patch.items())
                if kind == "certificates":
                    verified = all(updated.get("acme", {}).get(k) == v for k, v in patch.items()) if raw["type"] == 3 else pem(updated.get("manual", {}).get("crt", "")) == pem(patch["crt"])
                if verified:
                    result = {"outcome": "confirmed", "code": "safeline_write_confirmed"}
            except SafeLineError as error:
                if not accepted and error.code in {"safeline_invalid_credentials", "safeline_permission_denied", "safeline_endpoint_unavailable", "safeline_business_error", "safeline_sso_required"}:
                    result = {"outcome": "failed", "code": error.code}
            finally:
                # A cancellation/crash leaves a durable pending reservation, never a retry.
                self.store.finish_edit(command.operation_id, result)
            return {**result, "operation_id": command.operation_id, "connection_revision": revision, "updated_at": now(), "replayed": False}
