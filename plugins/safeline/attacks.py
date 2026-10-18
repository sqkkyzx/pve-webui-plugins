"""Bounded attack details and explicit IP-group membership operations."""

import ipaddress
import json
import re
from typing import Annotated
from urllib.parse import parse_qsl, urlencode

from pydantic import Field, StrictInt, StrictStr

from plugins.safeline.connection import SafeLineError
from plugins.safeline.editing import Model, fingerprint, validated
from plugins.safeline.service import _SECRET, now, record, rows, safe, take, text


def packet_header(value):
    return re.sub(r"(?im)^([^:\r\n]+):[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*", lambda m:
                  m[1] + ": [REDACTED]" if _SECRET.search(m[1]) else m[0], text(value, 32768))


def packet_body(value, header, token):
    value = text(value, 32768)
    try:
        parsed = json.loads(value)
        return json.dumps(safe(parsed, token), ensure_ascii=False, indent=2)
    except (ValueError, RecursionError):
        pass
    if "application/x-www-form-urlencoded" in header.lower():
        try:
            return urlencode([(k, "[REDACTED]" if _SECRET.search(k) else safe(v, token))
                              for k, v in parse_qsl(value, keep_blank_values=True, max_num_fields=10000)])
        except ValueError:
            return "[omitted: too many form fields]"
    # Also cover credentials in otherwise unstructured packet/payload text.
    return re.sub(r'''(?i)((?:authorization|cookie|password|secret|token|api[_-]?key)\s*[=:]\s*)([^\s&;,"'<>]+)''',
                  r"\1[REDACTED]", safe(value, token))


def detail(value, token):
    result = record(value)
    result.update(take(value, ("website", "query_string", "dst_ip", "src_port", "location", "decode_path", "short_rule_id")))
    result["content_truncated"] = any(isinstance(value.get(k), str) and len(value[k]) > 32768 for k in ("req_header", "rsp_header", "req_body", "rsp_body", "payload"))
    for prefix in ("req", "rsp"):
        header = packet_header(value.get(prefix + "_header"))
        result[prefix + "_header"] = header
        result[prefix + "_body"] = packet_body(value.get(prefix + "_body"), header, token)
    result["payload"] = packet_body(value.get("payload"), "", token)
    return result


GroupIds = Annotated[list[Annotated[StrictInt, Field(ge=1, le=9_007_199_254_740_991)]], Field(min_length=1, max_length=100)]


class AppendPatch(Model):
    ip: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    ip_group_ids: GroupIds


class CreatePatch(Model):
    ip: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    comment: Annotated[StrictStr, Field(min_length=1, max_length=1024)]


class IPGroupEditor:
    def __init__(self, editor):
        self.editor, self.service, self.store = editor, editor.service, editor.store

    async def snapshot(self, client, revision):
        data = await client.get("/api/open/ipgroup")
        values = rows(data, "nodes")
        if data.get("total") != len(values):
            raise SafeLineError("safeline_edit_incomplete", 409)
        groups = []
        for value in values:
            if (type(value.get("id")) is int and value["id"] > 0 and value.get("builtin") is False
                    and not value.get("reference") and safe(value.get("comment", ""), client.connection.token) == value.get("comment", "")):
                groups.append({"id": value["id"], "name": text(value.get("comment"), 1024) or "IP 組 #" + str(value["id"])})
        groups.sort(key=lambda v: v["id"])
        return {"groups": groups, "fingerprint": self.editor.resource_fingerprint(client, revision, groups),
                "connection_revision": revision, "updated_at": now()}

    async def load(self):
        client, revision = self.service.client()
        if client.connection is None:
            raise SafeLineError("safeline_not_configured", 409)
        return await self.snapshot(client, revision)

    async def save(self, action, command, actor):
        patch = validated(CreatePatch if action == "create" else AppendPatch, command.changes)
        try:
            address = ipaddress.ip_network(patch["ip"], strict=False) if "/" in patch["ip"] else ipaddress.ip_address(patch["ip"])
        except ValueError:
            raise SafeLineError("invalid_safeline_ips", 422) from None
        ip = str(address)
        if action == "append" and len(set(patch["ip_group_ids"])) != len(patch["ip_group_ids"]):
            raise SafeLineError("invalid_safeline_request", 422)
        if action == "create" and (not patch["comment"].strip() or any(ord(c) < 32 for c in patch["comment"])):
            raise SafeLineError("invalid_safeline_request", 422)
        async with self.editor.lock:
            digest = fingerprint({"action": "ip-group-" + action, **command.model_dump()})
            previous = self.store.receipt(command.operation_id, actor, digest)
            if previous is not None:
                return {**previous, "operation_id": command.operation_id, "connection_revision": command.expected_revision, "replayed": True}
            client, revision = self.service.client()
            if client.connection is None or revision != command.expected_revision:
                raise SafeLineError("safeline_connection_changed", 409)
            available = await self.snapshot(client, revision)
            if available["fingerprint"] != command.fingerprint:
                raise SafeLineError("safeline_resource_changed", 409)
            if action == "append" and not set(patch["ip_group_ids"]) <= {g["id"] for g in available["groups"]}:
                raise SafeLineError("safeline_edit_unsupported", 409)
            if action == "create" and safe(patch["comment"], client.connection.token) != patch["comment"]:
                raise SafeLineError("invalid_safeline_request", 422)
            if self.store.load()[1] != revision:
                raise SafeLineError("safeline_connection_changed", 409)
            self.store.reserve_edit(command.operation_id, actor, digest, fingerprint({"revision": revision, "kind": "ip-groups"}))
            result, accepted = {"outcome": "unknown", "code": "safeline_write_unknown"}, False
            try:
                if action == "create":
                    identifier = await client.write("POST", "/api/open/ipgroup", {"comment": patch["comment"], "ips": [ip]})
                    ids = [identifier] if type(identifier) is int and identifier > 0 else []
                else:
                    ids = patch["ip_group_ids"]
                    await client.write("POST", "/api/open/ipgroup/append", {"ip_group_ids": ids, "ips": [ip]})
                accepted = True
                verified = bool(ids)
                for identifier in ids:
                    data = await client.get("/api/open/ipgroup/detail", {"id": identifier})
                    group = data.get("data") if isinstance(data, dict) else None
                    verified = verified and isinstance(group, dict) and group.get("id") == identifier and isinstance(group.get("ips"), list) and ip in group["ips"]
                    if action == "create":
                        verified = verified and group.get("comment") == patch["comment"]
                if verified:
                    result = {"outcome": "confirmed", "code": "safeline_write_confirmed"}
            except SafeLineError as error:
                if not accepted and error.code in {"safeline_invalid_credentials", "safeline_permission_denied", "safeline_endpoint_unavailable", "safeline_business_error", "safeline_sso_required"}:
                    result = {"outcome": "failed", "code": error.code}
            finally:
                self.store.finish_edit(command.operation_id, result)
            return {**result, "operation_id": command.operation_id, "connection_revision": revision, "updated_at": now(), "replayed": False}
