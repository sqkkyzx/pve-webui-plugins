"""Display DTOs for five independent resource types and read-only WAF pages."""

import asyncio
from datetime import datetime, timezone
import hashlib
import math
import re

from plugins.safeline.client import SafeLineClient
from plugins.safeline.connection import SafeLineError


KINDS = ("applications", "deny", "allow", "certificates", "ip-groups")
_PROXY = ("br", "force_https", "gzip", "host", "hsts", "hsts_max_age", "hsts_preload", "hsts_sub",
          "http2", "http3", "http_1.0", "ipv6", "ssl_ciphers", "ssl_protocols", "sse", "reset_xff")
_SECRET = re.compile(r"authorization|cookie|password|secret|token|api[_-]?key|private[_-]?key", re.I)


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, limit=8192):
    return value[:limit] if isinstance(value, str) else ""


def number(value):
    return value if type(value) in {int, float} and math.isfinite(value) else None


def rows(data, key):
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise SafeLineError("safeline_unexpected_response")
    if any(not isinstance(row, dict) for row in data[key]):
        raise SafeLineError("safeline_unexpected_response")
    return data[key]


def safe(value, token, depth=0):
    if depth > 8:
        return "[omitted]"
    if isinstance(value, str):
        value = value.replace(token, "[REDACTED]")[:32768]
        value = re.sub(r"(https?://)[^/@\s]+@", r"\1[REDACTED]@", value)
        return re.sub(r"(?i)([?&](?:token|password|secret|api[_-]?key|access_token)=)[^&#\s]*", r"\1[REDACTED]", value)
    if isinstance(value, list):
        return [safe(v, token, depth+1) for v in value[:10000]]
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if _SECRET.search(str(k)) else safe(v, token, depth+1)
                for k, v in list(value.items())[:300]}
    if type(value) is float and not math.isfinite(value):
        return None
    return value if value is None or type(value) in {bool, int, float} else None


def take(value, names):
    return {k: value[k] for k in names if k in value}


def state(code, label):
    return {"code": code, "label": label}


def application(value):
    enabled, mode = value.get("is_enabled"), value.get("mode")
    status = state("unknown", "狀態未知")
    if enabled is False:
        status = state("stopped", "已停用")
    elif enabled is True:
        status = {0: state("running", "防護模式"), 1: state("stopped", "已離線"),
                  2: state("paused", "觀察模式")}.get(mode, status)
    health = value.get("health_state")
    health_rows = [{"target": host, "state": item.get("state"), "error": text(item.get("error"))}
                   for host, item in health.items() if isinstance(item, dict)] if isinstance(health, dict) else []
    errors = [item for item in health_rows if item["error"]]
    if enabled is True and mode == 0 and value.get("health_check") is True and errors:
        status = state("error", "回源不可用") if len(errors) == len(health_rows) else state("pending", "部分回源異常")
    listeners = []
    for port in value.get("ports") or []:
        match = re.fullmatch(r"([1-9][0-9]{0,4})(_ssl)?", str(port))
        if match and int(match[1]) <= 65535:
            listeners.append({"port": int(match[1]), "tls": bool(match[2])})
    result = take(value, ("id", "comment", "is_enabled", "mode", "cert_id", "created_at", "updated_at", "health_check",
                          "semantics", "challenge_is_enabled", "chaos_is_enabled", "acl_enabled", "stat_enabled", "sp_enabled"))
    # Match the console's four protection badges. Existing policy IDs do not
    # imply BOT protection is active: disabled policies retain their IDs.
    challenge, chaos = value.get("challenge_is_enabled"), value.get("chaos_is_enabled")
    auth = value.get("auth_defense_id")
    result.update({"cc_enabled": value.get("cc_bot") if type(value.get("cc_bot")) is bool else None,
                   "bot_enabled": True if challenge is True or chaos is True else False if challenge is False and chaos is False else None,
                   "auth_enabled": auth > 0 if type(auth) is int and auth >= 0 else None,
                   "attack_enabled": value.get("semantics") if type(value.get("semantics")) is bool else None})
    for key in ("req_value", "denied_value"):
        count = number(value.get(key))
        result[key] = count if count is not None and count >= 0 else None
    result.update({"id": str(value["id"]), "kind": "applications", "status": status,
                   "name": text(value.get("title")) or ", ".join(value.get("server_names") or []) or "應用 #" + str(value["id"]),
                   "domains": value.get("server_names") or [], "ports": value.get("ports") or [],
                   "upstreams": value.get("upstreams") or [], "health": health_rows, "listeners": listeners})
    return result


def policy(value):
    status = state("unknown", "狀態未知")
    expire = value.get("expire")
    # 0 is the instance's permanent sentinel. Positive units remain unverified.
    if expire not in {None, 0}:
        status = state("unknown", "有效期待核對")
    elif value.get("is_enabled") is True:
        status = state("running", "已啟用")
    elif value.get("is_enabled") is False:
        status = state("paused", "已停用")
    patterns = []
    for group in value.get("pattern") or []:
        if not isinstance(group, list):
            continue
        output = []
        for rule in group:
            if not isinstance(rule, dict):
                continue
            item = take(rule, ("k", "op", "sub_k", "v"))
            if _SECRET.search(str(item.get("sub_k", "")) + " " + str(item.get("k", ""))):
                item["v"] = ["[REDACTED]"]
            output.append(item)
        patterns.append(output)
    result = take(value, ("action", "is_enabled", "expire", "level", "order", "builtin", "log", "negate", "created_at", "updated_at", "req_count", "pass_count"))
    result.update({"id": str(value["id"]), "kind": {0: "allow", 1: "deny"}.get(value.get("action"), "policies"),
                   "name": text(value.get("name")) or "規則 #" + str(value["id"]), "status": status, "pattern": patterns})
    return result


def certificate(value):
    status = state("unknown", "有效期未知")
    if value.get("expired") is True or value.get("revoked") is True:
        status = state("error", "已撤銷" if value.get("revoked") else "已過期")
    elif isinstance(value.get("valid_before"), str):
        try:
            end = datetime.fromisoformat(value["valid_before"].replace("Z", "+00:00"))
            if end.tzinfo is None:
                raise ValueError
            remaining = (end - datetime.now(timezone.utc)).total_seconds()
            status = state("error", "已過期") if remaining <= 0 else state("pending", "即將到期") if remaining <= 30*86400 else state("online", "有效")
        except ValueError:
            pass
    result = take(value, ("domains", "issuer", "self_signature", "trusted", "revoked", "expired", "type", "valid_before", "related_sites", "acme_message"))
    result.update({"id": str(value["id"]), "kind": "certificates", "status": status,
                   "name": ", ".join(value.get("domains") or []) or "憑證 #" + str(value["id"])})
    return result


def ip_group(value):
    result = take(value, ("comment", "ips", "reference", "builtin", "updated_at", "total"))
    result.update({"id": str(value["id"]), "kind": "ip-groups", "status": state("normal", "已載入"),
                   "name": text(value.get("comment")) or "IP 組 #" + str(value["id"]),
                   "members_truncated": isinstance(value.get("ips"), list) and len(value["ips"]) > 10000})
    return result


def event(value):
    return take(value, ("id", "ip", "host", "dst_port", "start_at", "end_at", "updated_at", "deny_count", "pass_count", "finished", "country", "province", "city"))


def record(value):
    result = take(value, ("src_ip", "host", "url_path", "dst_port", "action", "attack_type", "created_at", "timestamp", "method",
                           "status_code", "rule_id", "policy_name", "event_id", "reason", "country", "province", "city", "module", "ja4_fingerprint", "protocol"))
    for name in ("req_header", "rsp_header"):
        result[name] = re.sub(r"(?im)^([^:\r\n]+):[^\r\n]*", lambda match:
            match[1] + ": [REDACTED]" if _SECRET.search(match[1]) else match[0], text(value.get(name), 32768))
    return result


class SafeLineService:
    def __init__(self, store, transport=None):
        self.store, self.transport = store, transport

    def client(self):
        connection, revision, source = self.store.load()
        return SafeLineClient(connection, transport=self.transport), revision

    async def policies(self, client):
        result, total = [], 0
        for page in range(1, 51):
            data = await client.get("/api/open/policy", {"action": -1, "page": page, "page_size": 100})
            batch = rows(data, "data")
            total = number(data.get("total"))
            if total is None or total < 0:
                raise SafeLineError("safeline_unexpected_response")
            result.extend(batch)
            if len(result) >= total or not batch:
                return result, total, len(result) < total
        return result, total, True

    async def inventory(self):
        client, revision = self.client()
        if client.connection is None:
            raise SafeLineError("safeline_not_configured", 409)
        results = await asyncio.gather(client.get("/api/open/site"), self.policies(client), client.get("/api/open/cert"),
                                       client.get("/api/open/ipgroup"), return_exceptions=True)
        resources = {kind: [] for kind in KINDS}
        errors, totals, truncated = {}, {}, {}
        other = []
        for index, kinds in enumerate((("applications",), ("deny", "allow"), ("certificates",), ("ip-groups",))):
            try:
                data = results[index]
                if isinstance(data, BaseException):
                    raise data
                if index == 1:
                    raw, total, limited = data
                    mapped = [policy(row) for row in raw]
                    other = [row for row in mapped if row["kind"] == "policies"]
                    for kind in kinds:
                        resources[kind] = [row for row in mapped if row["kind"] == kind]
                        totals[kind] = len(resources[kind])
                        truncated[kind] = limited
                else:
                    kind = kinds[0]
                    raw = rows(data, "data" if index == 0 else "nodes")
                    mapper = (application, None, certificate, ip_group)[index]
                    resources[kind] = [mapper(row) for row in raw[:10000]]
                    totals[kind] = number(data.get("total"))
                    truncated[kind] = len(raw) > 10000 or totals[kind] is not None and len(resources[kind]) < totals[kind]
            except (SafeLineError, KeyError, TypeError, ValueError) as error:
                for kind in kinds:
                    errors[kind] = error.code if isinstance(error, SafeLineError) else "safeline_unexpected_response"
        identity = hashlib.sha256((client.connection.url + "\0" + client.connection.tls_server_name).encode()).hexdigest()[:24]
        return safe({"resources": resources, "policies": other, "errors": errors, "totals": totals, "truncated": truncated,
                     "origin_id": identity, "connection_revision": revision, "updated_at": now()}, client.connection.token)

    async def detail(self, kind, identifier, section):
        client, revision = self.client()
        if client.connection is None:
            raise SafeLineError("safeline_not_configured", 409)
        if kind == "applications":
            data = await client.get("/api/open/site/" + identifier)
            if not isinstance(data, dict):
                raise SafeLineError("safeline_unexpected_response")
            result = application(data)
            if section == "proxy":
                raw = await client.get("/api/open/site/" + identifier + "/proxy")
                if not isinstance(raw, dict):
                    raise SafeLineError("safeline_unexpected_response")
                result["proxy"] = take(raw, _PROXY)
                result["proxy_global"] = take(raw["global"], _PROXY) if isinstance(raw.get("global"), dict) else {}
            elif section == "protection":
                parts = await asyncio.gather(client.get("/api/open/site/" + identifier + "/semantics"),
                                             client.get("/api/open/site/" + identifier + "/acl"), return_exceptions=True)
                result["protection"] = {}
                for key, value in zip(("semantics", "acl"), parts):
                    result["protection"][key] = {"error": value.code} if isinstance(value, SafeLineError) else value if not isinstance(value, BaseException) else {"error": "safeline_upstream_error"}
        elif kind in {"deny", "allow", "policies"}:
            raw = await client.get("/api/open/policy/detail", {"id": identifier})
            if not isinstance(raw, dict):
                raise SafeLineError("safeline_unexpected_response")
            result = policy(raw)
            if result["kind"] != kind:
                raise SafeLineError("safeline_resource_moved", 409)
        elif kind == "ip-groups":
            raw = await client.get("/api/open/ipgroup/detail", {"id": identifier})
            if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
                raise SafeLineError("safeline_unexpected_response")
            result = ip_group(raw["data"])
        elif kind == "certificates":
            # Never call cert/{id}: it can return the private key.
            items = rows(await client.get("/api/open/cert"), "nodes")
            match = next((row for row in items if str(row.get("id")) == identifier), None)
            if match is None:
                raise SafeLineError("safeline_resource_not_found", 404)
            result = certificate(match)
        else:
            raise SafeLineError("invalid_safeline_resource", 422)
        return safe({"resource": result, "connection_revision": revision, "updated_at": now()}, client.connection.token)

    async def logs(self, *, events, rule, params):
        client, revision = self.client()
        path = "/api/open/" + ("events" if events else "records") + ("/rule" if rule else "")
        data = await client.get(path, params)
        values = rows(data, "nodes" if events else "data")
        if len(values) > params["page_size"]:
            raise SafeLineError("safeline_unexpected_response")
        total = number(data.get("total"))
        if total is None or total < 0:
            raise SafeLineError("safeline_unexpected_response")
        return safe({"items": [event(row) if events else record(row) for row in values], "total": total,
                     "module_in_list": not values or all("module" in row for row in values),
                     "connection_revision": revision, "updated_at": now()}, client.connection.token)

    async def record_detail(self, identifier, *, rule=False):
        from plugins.safeline.attacks import detail

        client, revision = self.client()
        value = await client.get("/api/open/record/" + ("rule/" if rule else "") + identifier)
        if not isinstance(value, dict) or value.get("event_id") != identifier:
            raise SafeLineError("safeline_resource_not_found", 404)
        return safe({"record": detail(value, client.connection.token), "connection_revision": revision,
                     "updated_at": now()}, client.connection.token)

    async def overview(self, hours=24, *, preset="last1Day", site_id=None, utc_offset=0):
        from plugins.safeline.dashboard import PRESETS, overview

        if hours != 24 or preset not in PRESETS:
            raise SafeLineError("invalid_safeline_query", 422)
        return await overview(self, preset, site_id, utc_offset)
