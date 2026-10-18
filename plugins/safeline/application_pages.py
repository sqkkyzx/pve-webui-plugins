"""Application pages and bounded, verified configuration transactions."""

import asyncio
import copy
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from plugins.safeline import advanced
from plugins.safeline.connection import SafeLineError
from plugins.safeline.editing import EditCommand, Pattern, _SITE_FIELDS, fingerprint, validated
from plugins.safeline.service import _SECRET, application, now, policy, safe, take


SECTIONS = {"basic", "advanced", "nginx", "cc", "bot", "auth", "attack", "forwarding", "routes", "collection", "tamper", "statistics", "log-access", "log-error"}
MODULES = {"m_sqli": "SQL 注入檢測", "m_xss": "XSS 檢測", "m_file_upload": "檔案上傳檢測", "m_file_include": "檔案包含檢測",
           "m_cmd_injection": "命令注入檢測", "m_java": "JAVA 代碼注入檢測", "m_java_unserialize": "JAVA 反序列化檢測",
           "m_php_unserialize": "PHP 反序列化檢測", "m_php_code_injection": "PHP 代碼注入檢測", "m_asp_code_injection": "ASP 代碼注入檢測",
           "m_ssti": "模板注入檢測", "m_csrf": "CSRF 檢測", "m_ssrf": "SSRF 檢測", "m_http": "畸形 HTTP 協定檢測"}
CHAOS = ("is_enabled", "html_encryption", "html_fast_decryption", "js_encryption", "js_path", "img_encryption", "img_watermark", "img_text")
RULE_FIELDS = ("id", "name", "type", "priority", "enabled", "period", "count", "block_min", "action", "conditions", "custom_status_codes", "built_in", "use_global")
POLICY_FIELDS = ("id", "level", "expire", "replay", "negate", "pattern", "review", "auth_source_ids", "auth_callback", "tfa_enabled")
SOURCE_TYPES = {0: "帳號密碼", 1: "微信", 2: "GitHub", 3: "CAS", 4: "OIDC", 5: "釘釘", 6: "企業微信", 7: "LDAP"}


def invalid(code="invalid_safeline_application"):
    raise SafeLineError(code, 422)


def require(condition):
    if not condition:
        invalid()


def premium(raw):
    if raw["edition"] not in {1, 2}:
        raise SafeLineError("safeline_feature_unavailable", 409)


def commercial(raw):
    if raw["edition"] != 2:
        raise SafeLineError("safeline_business_required", 409)


def integer(value, low=0, high=2_147_483_647):
    require(type(value) is int and low <= value <= high)
    return value


def boolean(value):
    require(type(value) is bool)
    return value


def string(value, limit=4096):
    require(type(value) is str and len(value) <= limit and not any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in value))
    return value


def strings(value, limit=256):
    require(isinstance(value, list) and len(value) <= limit)
    return [string(v) for v in value]


def upstreams(values):
    values = strings(values)
    require(bool(values))
    for value in values:
        try:
            url = urlsplit(value)
            require(url.scheme in {"http", "https"} and url.hostname and url.username is None and url.password is None and
                    not url.query and not url.fragment and (url.port is None or 1 <= url.port <= 65535) and not any(ord(c) <= 32 for c in value))
        except ValueError:
            invalid("invalid_safeline_upstreams")
    return values


def patterns(value):
    require(isinstance(value, list) and len(value) <= 64)
    result = []
    for group in value:
        require(isinstance(group, list) and 1 <= len(group) <= 64)
        result.append([validated(Pattern, row) for row in group])
    return result


def bounded_rows(value, key=None):
    values = value.get(key) if key and isinstance(value, dict) else value
    if not isinstance(values, list) or len(values) > 10000 or any(not isinstance(v, dict) for v in values):
        raise SafeLineError("safeline_unexpected_response")
    return values


def caps(raw):
    level = raw["edition"]
    return {"edition": level, "premium": level in {1, 2}, "business": level == 2,
            "forwarding_limit": {0: 3, 1: 10, 2: 100}.get(level, 3), "module_names": MODULES}


def public_rules(rules, token):
    result, locked = copy.deepcopy(rules), []
    for row in result:
        conditions = row.get("conditions") or []
        if (safe(conditions, token) != conditions or any(
                _SECRET.search(str(c.get("condition", {}).get("field", "")) + " " + str(c.get("strategy", ""))) for c in conditions if isinstance(c, dict))):
            row.pop("conditions", None); locked.append("conditions:" + str(row["id"]))
        else:
            row["conditions"] = conditions
        row["custom_status_codes"] = row.get("custom_status_codes") or []
    return result, locked


def site_fields(section):
    return {"bot": ("id", "challenge_id", "challenge_is_enabled", "chaos_id", "chaos_is_enabled"),
            "auth": ("id", "auth_defense_id", "portal_redirect", "auth_challenge_timeout", "auth_challenge_timeout_unit")}.get(section, ("id",))


async def read(client, identifier, section):
    if section not in SECTIONS:
        invalid()
    site = await client.get("/api/open/site/" + identifier)
    if not isinstance(site, dict) or str(site.get("id")) != identifier:
        raise SafeLineError("safeline_resource_not_found", 404)
    try:
        edition = await client.get("/api/open/system/edition")
        level = edition.get("effective_edition", edition.get("version"))
        level = level if type(level) is int and level in {0, 1, 2} else None
    except (SafeLineError, AttributeError):
        level = None
    raw = {"edition": level, "site": take(site, site_fields(section))}
    path = "/api/open/site/" + identifier
    if section == "basic":
        raw["site"] = application(site)
        raw["site"]["access_type"] = site.get("type")
        raw["site"].update(take(site, ("access_log_limit", "error_log_limit")))
        missing = [kind for kind in ("access", "error") if type(raw["site"].get(kind + "_log_limit")) is not int]
        limits = await asyncio.gather(*(client.get(path + "/log/limit", {"type": kind}) for kind in missing), return_exceptions=True)
        for kind, limit in zip(missing, limits):
            raw["site"][kind + "_log_limit"] = limit if type(limit) is int and limit >= 0 else None
    elif section == "statistics":
        raw["site"] = take(site, (*_SITE_FIELDS, "sp_enabled"))
    elif section == "advanced":
        proxy = await client.get(path + "/proxy")
        if not isinstance(proxy, dict):
            raise SafeLineError("safeline_unexpected_response")
        raw["proxy"] = {k: v for k, v in proxy.items() if k not in {"id", "site_id", "created_at", "updated_at", "global"}}
        global_proxy = proxy.get("global")
        if not isinstance(global_proxy, dict):
            global_proxy = await client.get("/api/open/global/proxy")
        raw["global_proxy"] = {k: v if isinstance(v, dict) else {"global": True, "value": v} for k, v in global_proxy.items()}
    elif section in {"nginx", "collection"}:
        data = await client.get(path + ("/nginx_config" if section == "nginx" else "/excludes"))
        if not isinstance(data, dict):
            raise SafeLineError("safeline_unexpected_response")
        raw[section] = take(data, ("custom_location",) if section == "nginx" else ("prefixes", "content_types"))
    elif section == "cc":
        rules, waiting, global_rules = await asyncio.gather(client.get(path + "/acl"), client.get(path + "/waiting"), client.get("/api/open/site/0/acl"))
        raw.update(rules=[take(r, RULE_FIELDS) for r in bounded_rows(rules)], waiting=take(waiting, ("is_enabled", "max_concurrent", "session_timeout")),
                   global_rules=[take(r, RULE_FIELDS) for r in bounded_rows(global_rules)])
    elif section in {"bot", "auth"}:
        key = "challenge_id" if section == "bot" else "auth_defense_id"
        ref = site.get(key, 0)
        data = await client.get("/api/open/policy/detail", {"id": ref}) if type(ref) is int and ref > 0 else {}
        if data and (not isinstance(data, dict) or data.get("id") != ref):
            raise SafeLineError("safeline_unexpected_response")
        raw["policy"] = take(data, POLICY_FIELDS)
        if section == "bot":
            raw["chaos"] = take(await client.get(path + "/chaos") or {}, CHAOS)
        else:
            sources, portal = await asyncio.gather(client.get("/api/open/auth_defense/source"), client.get("/api/open/portal"))
            raw["sources"] = [{"id": 0, "type": 0}, *[take(s, ("id", "type")) for s in bounded_rows(sources, "data")]]
            raw["portal"] = {**take(portal, ("enable", "tfa_enabled")),
                             "auth_sources": [take(source, ("id", "type")) for source in bounded_rows(portal.get("auth_sources") or [])]}
    elif section == "attack":
        local, shared = await asyncio.gather(client.get(path + "/semantics"), client.get("/api/open/global/mode"))
        if local is not None and not isinstance(local, dict) or not isinstance(shared, dict):
            raise SafeLineError("safeline_unexpected_response")
        raw.update(modules=local.get("semantics") if local else None, global_modules=shared.get("semantics", {}))
    elif section == "forwarding":
        raw["rules"] = [take(r, ("id", "path_prefix", "upstreams", "comment", "is_enabled", "updated_at"))
                        for r in bounded_rows(await client.get(path + "/forwarding_rules"))]
    elif section == "routes":
        result = await client.get(path + "/resources")
        raw["resources"] = [take(r, ("id", "path", "method", "content_type", "created_at", "updated_at", "req_today", "req_month"))
                            for r in bounded_rows(result, "nodes")]
        raw["total"] = result.get("total", len(raw["resources"]))
    elif section == "tamper":
        raw["tamper"] = {"data": [], "refresh_time": None, "refresh_state": ""}
        raw["resources"] = []
        if level == 2:
            data, resources = await asyncio.gather(client.get("/api/business/site/" + identifier + "/anti_tamper"), client.get(path + "/resources"))
            raw["tamper"] = {**take(data, ("refresh_time", "refresh_state")), "data": [take(r, ("id", "path", "changed", "cache_time", "site_id")) for r in bounded_rows(data, "data")]}
            raw["resources"] = [take(r, ("id", "path", "method", "content_type")) for r in bounded_rows(resources, "nodes")]
    elif section.startswith("log-"):
        kind = section[4:]
        files, limit = await asyncio.gather(client.get(path + "/log", {"type": kind}), client.get(path + "/log/limit", {"type": kind}))
        raw.update(files=[take(r, ("filename", "size")) for r in bounded_rows(files)], limit=limit)
    return raw


def values(raw, section, token):
    locked = []
    if section == "advanced":
        result, locked = advanced.values(raw, "proxy", token)
        # All per-application custom proxy settings require the native advanced
        # configuration capability. Returning to inheritance remains available.
        if raw["edition"] not in {1, 2}:
            locked = sorted(set(locked) | set(result))
        return result, locked
    if section == "basic":
        return {"resource": safe(raw["site"], token)}, ["resource"]
    if section in {"nginx", "collection"}:
        result = copy.deepcopy(raw[section])
        if section == "nginx" and raw["edition"] not in {1, 2}:
            locked = list(result)
    elif section == "cc":
        rules, locked = public_rules(raw["rules"], token)
        result = {"waiting": raw["waiting"], "use_global": bool(raw["rules"] and raw["rules"][0].get("use_global")), "rules": rules}
        if raw["edition"] not in {1, 2}:
            locked.extend(("max_concurrent", "session_timeout", "new_rules"))
    elif section in {"bot", "auth"}:
        p = raw["policy"]
        result = {"enable": bool(raw["site"].get("challenge_is_enabled") if section == "bot" else raw["site"].get("auth_defense_id")),
                  "pattern": p.get("pattern") or [], "negate": p.get("negate", False)}
        if result["pattern"] != policy({"id": 0, "pattern": result["pattern"]})["pattern"] or safe(result["pattern"], token) != result["pattern"]:
            result.pop("pattern"); locked.append("pattern")
        if section == "bot":
            result.update(level=p.get("level", 1), expire=p.get("expire", 3600) or 3600, replay=p.get("replay", False),
                          chaos={**{k: False for k in CHAOS[:4]}, "js_path": [], "img_encryption": False, "img_watermark": "", "img_text": "", **raw["chaos"]})
            # Existing watermark images can be large; keep them on the backend.
            result["chaos"].pop("img_watermark", None)
            for key, value in list(result["chaos"].items()):
                if safe(value, token) != value:
                    result["chaos"].pop(key); locked.append("chaos." + key)
            if raw["edition"] not in {1, 2}:
                locked.extend(("level", "expire", "replay", "html_fast_decryption", "img_text", "img_watermark"))
        else:
            result.update(auth_source_ids=p.get("auth_source_ids") or [], review=p.get("review", 0), auth_callback=p.get("auth_callback", ""),
                          tfa_enabled=p.get("tfa_enabled", False), portal_redirect=raw["site"].get("portal_redirect", ""),
                          auth_challenge_timeout=raw["site"].get("auth_challenge_timeout", 1) or 1,
                          auth_challenge_timeout_unit=raw["site"].get("auth_challenge_timeout_unit", "hour"))
    elif section == "attack":
        result = {"use_global": raw["modules"] is None, "modules": copy.deepcopy(raw["global_modules"] if raw["modules"] is None else raw["modules"])}
    elif section == "forwarding":
        result = {"rules": raw["rules"]}
    elif section == "routes":
        result = {"resources": raw["resources"], "total": raw["total"]}
    elif section == "tamper":
        result = {**raw["tamper"], "resources": raw["resources"]}
        if raw["edition"] != 2:
            locked = ["create", "delete", "refresh", "accept"]
    elif section == "statistics":
        result = take(raw["site"], ("stat_enabled", "sp_enabled"))
        if raw["edition"] not in {1, 2}:
            locked.append("stat_enabled")
        if raw["edition"] != 2:
            locked.append("sp_enabled")
    else:
        result = {"files": raw["files"], "limit": raw["limit"]}
    for key, value in list(result.items()):
        if safe(value, token) != value:
            result.pop(key); locked.append(key)
    return result, sorted(set(locked))


@dataclass
class Plan:
    writes: list
    verify: object


def match_list(actual, desired, fields):
    remaining = list(actual)
    for want in desired:
        found = next((r for r in remaining if (not want.get("id") or r.get("id") == want["id"]) and all((r.get(k) or [] if k in {"conditions", "custom_status_codes"} else r.get(k)) == v for k, v in want.items() if k in fields)), None)
        if found is None:
            return False
        remaining.remove(found)
    return not remaining


def build(raw, identifier, section, changes, token):
    require(isinstance(changes, dict) and bool(changes))
    path = "/api/open/site/" + identifier
    current, locked = values(raw, section, token)
    writes = []
    if section == "advanced":
        require(set(changes) <= set(current))
        inherited, custom = {}, {}
        for key, item in changes.items():
            require(isinstance(item, dict) and set(item) == {"global", "value"} and type(item["global"]) is bool)
            if item["global"]:
                require(item["value"] == current[key]["value"])
                inherited[key] = {"global": True, "value": copy.deepcopy(raw["proxy"][key]["value"])}
            else:
                premium(raw); custom[key] = item
        patch = inherited
        if custom:
            prepared = copy.deepcopy(raw)
            for key in ("ip_source", "ip_value"):
                if key not in custom and prepared["proxy"].get(key, {}).get("global"):
                    prepared["proxy"][key] = copy.deepcopy(raw["global_proxy"].get(key, prepared["proxy"][key]))
            _, _, payload, _ = advanced.build(prepared, "proxy", custom, token)
            patch.update(payload)
        writes.append(("PUT", path + "/proxy", {**raw["proxy"], **patch}))
        return Plan(writes, lambda r: all(r["proxy"].get(k) == v for k, v in patch.items()))
    if section == "nginx":
        premium(raw); require(set(changes) == {"custom_location"} and "custom_location" in current)
        desired = strings(changes["custom_location"], 1000); require(sum(map(len, desired)) <= 65536)
        return Plan([("PUT", path + "/nginx_config", {"custom_location": desired})], lambda r: r["nginx"].get("custom_location") == desired)
    if section == "collection":
        require(set(changes) <= {"prefixes", "content_types"})
        if set(changes) & set(locked):
            raise SafeLineError("safeline_edit_unsupported", 409)
        patch = {k: strings(v) for k, v in changes.items()}
        require(all(v.startswith("/") and not any(c in v for c in "\r\n") for v in patch.get("prefixes", [])))
        desired = {**raw["collection"], **patch}
        return Plan([("POST", path + "/excludes", desired)], lambda r: all(r["collection"].get(k) == v for k, v in desired.items()))
    if section == "statistics":
        require(set(changes) <= {"stat_enabled", "sp_enabled"})
        for key, value in changes.items():
            boolean(value)
            if key == "stat_enabled":
                premium(raw); writes.append(("PUT", "/api/open/site", {**take(raw["site"], _SITE_FIELDS), key: value, "id": int(identifier)}))
            else:
                commercial(raw); writes.append(("POST", "/api/open/security_posture/site", {"site_id": int(identifier), "enabled": value}))
        return Plan(writes, lambda r: all(r["site"].get(k) == v for k, v in changes.items()))
    if section.startswith("log-"):
        require(set(changes) == {"limit"}); limit = integer(changes["limit"], 0, 65536)
        return Plan([("POST", path + "/log/limit", {"type": section[4:], "limit": limit})], lambda r: r["limit"] == limit)
    if section == "attack":
        require(set(changes) <= {"use_global", "modules"})
        use_global = boolean(changes.get("use_global", current["use_global"]))
        modules = changes.get("modules", {})
        require(isinstance(modules, dict) and set(modules) <= set(MODULES) and all(v in {"disable", "dry_run", "default", "strict"} for v in modules.values()))
        desired = {**current["modules"], **modules}
        return Plan([("PUT", path + "/semantics", {"use_global": use_global, "semantics": desired})],
                    lambda r: r["modules"] is None if use_global else isinstance(r["modules"], dict) and all(r["modules"].get(k) == v for k, v in desired.items()))
    if section in {"bot", "auth"}:
        allowed = ({"enable", "level", "expire", "replay", "negate", "pattern", "chaos"} if section == "bot" else
                   {"enable", "negate", "pattern", "auth_source_ids", "review", "auth_callback", "tfa_enabled", "portal_redirect", "auth_challenge_timeout", "auth_challenge_timeout_unit"})
        require(set(changes) <= allowed)
        if set(changes) & set(locked):
            if "pattern" in changes and "pattern" in locked:
                raise SafeLineError("safeline_edit_unsupported", 409)
            premium(raw)
        desired = {**current, **changes}
        for key in {"enable", "replay", "negate", "tfa_enabled"} & set(changes):
            boolean(changes[key])
        if "pattern" in changes:
            patterns(changes["pattern"])
        if section == "bot":
            if {"level", "expire", "replay"} & set(changes):
                premium(raw)
            integer(desired["level"], 1, 2); integer(desired["expire"], 1, 31536000)
            challenge_keys = {"enable", "level", "expire", "replay", "negate", "pattern"}
            if challenge_keys & set(changes):
                payload = {k: desired.get(k, raw["policy"].get(k)) for k in challenge_keys}
                if "pattern" not in desired:
                    payload["pattern"] = raw["policy"].get("pattern", [])
                writes.append(("PUT", "/api/open/site/challenge", {"id": int(identifier), **payload}))
            if "chaos" in changes:
                require(isinstance(changes["chaos"], dict) and set(changes["chaos"]) <= set(CHAOS))
                require(not any("chaos." + key in locked for key in changes["chaos"]))
                chaos = {**raw["chaos"], **current["chaos"], **changes["chaos"]}
                for key, value in changes["chaos"].items():
                    if key in {"html_fast_decryption", "img_text", "img_watermark"}:
                        premium(raw)
                    strings(value) if key == "js_path" else string(value, 65536) if key in {"img_text", "img_watermark"} else boolean(value)
                chaos["is_enabled"] = any(chaos.get(k) for k in ("html_encryption", "js_encryption", "img_encryption"))
                writes.append(("POST", path + "/chaos", chaos)); desired["chaos"] = chaos
        else:
            integer(desired["review"], 0, 1); integer(desired["auth_challenge_timeout"], 1, 36500)
            require(desired["auth_challenge_timeout_unit"] in {"day", "hour", "minute"})
            for key in ("auth_callback", "portal_redirect"):
                value = string(desired[key])
                if value:
                    url = urlsplit(value); require(url.scheme in {"http", "https"} and url.hostname and url.username is None and url.password is None and not url.fragment)
            require(isinstance(desired["auth_source_ids"], list) and len(desired["auth_source_ids"]) <= 32)
            source_ids = {r["id"] for r in raw["sources"]}
            require(all(type(v) is int and v in source_ids for v in desired["auth_source_ids"]))
            require(len(set(desired["auth_source_ids"])) == len(desired["auth_source_ids"]))
            source_types = {r["id"]: r.get("type") for r in raw["sources"]}
            require(len({source_types[i] for i in desired["auth_source_ids"]}) == len(desired["auth_source_ids"]))
            if desired["portal_redirect"] and not raw["portal"].get("enable"):
                raise SafeLineError("safeline_portal_disabled", 409)
            if desired["portal_redirect"]:
                require(set(desired["auth_source_ids"]) == {source["id"] for source in raw["portal"]["auth_sources"]} and
                        desired["tfa_enabled"] == bool(raw["portal"].get("tfa_enabled")))
            payload = {k: desired.get(k, raw["policy"].get(k)) for k in allowed}
            payload["pattern"] = desired.get("pattern", raw["policy"].get("pattern", []))
            writes.append(("PUT", "/api/open/site/defense", {"id": int(identifier), **payload}))
        def verify(r):
            updated, _ = values(r, section, token)
            for key in changes:
                if key == "chaos":
                    if any(r["chaos"].get(k) != v for k, v in desired["chaos"].items()):
                        return False
                elif updated.get(key) != desired.get(key):
                    return False
            return True
        return Plan(writes, verify)
    if section == "cc":
        require(set(changes) <= {"waiting", "rules", "use_global"})
        waiting = raw["waiting"]
        if "waiting" in changes:
            require(isinstance(changes["waiting"], dict) and set(changes["waiting"]) <= {"is_enabled", "max_concurrent", "session_timeout"})
            if {"max_concurrent", "session_timeout"} & set(changes["waiting"]):
                premium(raw)
            waiting = {**waiting, **changes["waiting"]}; boolean(waiting["is_enabled"])
            integer(waiting["max_concurrent"], 1, 5000); integer(waiting["session_timeout"], 1, 1440)
            if raw["edition"] not in {1, 2} and waiting["is_enabled"] and waiting["max_concurrent"] > 5:
                premium(raw)
            writes.append(("POST", path + "/waiting", waiting))
        use_global = boolean(changes.get("use_global", current["use_global"]))
        desired = copy.deepcopy(raw["rules"])
        if "rules" in changes:
            require(not use_global)
            proposed = bounded_rows(changes["rules"]); require(len(proposed) <= 128)
            originals = {r["id"]: r for r in raw["rules"]}; desired = []; seen = set()
            for row in proposed:
                require(set(row) <= set(RULE_FIELDS))
                key = integer(row.get("id", 0)); require(key == 0 or key in originals and key not in seen); seen.add(key)
                if not key:
                    premium(raw)
                original = originals.get(key, {})
                item = {**original, **row, "id": key, "use_global": False}
                require(item.get("type") in {"req", "attack", "error"})
                string(item.get("name", ""), 256); boolean(item["enabled"])
                for name in ("period", "count", "block_min"):
                    integer(item[name], 1 if item["enabled"] else 0, 1_000_000_000 if name == "count" else 86400)
                integer(item.get("priority", 0), 0, 100000)
                require(item["action"] in {"challenge_v1", "challenge_v2", "block"})
                if item["action"] == "challenge_v2" and item["action"] != original.get("action"):
                    premium(raw)
                if "conditions" in row and row["conditions"] != (original.get("conditions") or []):
                    require("conditions:" + str(key) not in locked)
                    require(isinstance(row["conditions"], list) and len(row["conditions"]) <= 64)
                    for condition in row["conditions"]:
                        require(isinstance(condition, dict) and set(condition) == {"strategy", "condition"})
                        string(condition["strategy"], 128); c = condition["condition"]
                        require(isinstance(c, dict) and set(c) == {"field", "operator", "value"})
                        string(c["field"], 256); string(c["operator"], 128); strings(c["value"])
                if "custom_status_codes" in row:
                    require(all(re.fullmatch(r"[1-5][0-9]{2}", v) for v in strings(row["custom_status_codes"])))
                if original.get("built_in"):
                    require(item.get("built_in") is True and item["type"] == original["type"])
                else:
                    require(item.get("built_in", False) is False)
                desired.append(item)
            require(all(not r.get("built_in") or r["id"] in seen for r in raw["rules"]))
            for row in raw["rules"]:
                if row["id"] not in seen:
                    premium(raw); writes.append(("DELETE", path + "/acl/" + str(row["id"]), None))
            # SetSiteACLRequest applies the existing rules and inheritance in one request.
            existing = [r for r in desired if r["id"]]
            writes.append(("PUT", path + "/acl", {"use_global": False, "rules": existing}))
            for row in desired:
                if row["id"] == 0:
                    writes.append(("POST", path + "/acl", {k: v for k, v in row.items() if k != "id"}))
        elif "use_global" in changes:
            writes.append(("PUT", path + "/acl", {"use_global": use_global, **({} if use_global else {"rules": [{**r, "use_global": False} for r in desired]})}))
        def verify(r):
            return (all(r["waiting"].get(k) == v for k, v in waiting.items()) and
                    (not ({"rules", "use_global"} & set(changes)) or
                     bool(r["rules"] and r["rules"][0].get("use_global")) == use_global and
                     (use_global or match_list(public_rules(r["rules"], token)[0], public_rules(desired, token)[0], set(RULE_FIELDS) - {"id", "use_global"}))))
        return Plan(writes, verify)
    if section in {"forwarding", "routes", "tamper"}:
        action = changes.get("action"); key = changes.get("id")
        require(action in ({"create", "edit", "switch", "delete"} if section == "forwarding" else {"delete"} if section == "routes" else {"create", "delete", "refresh", "accept"}))
        if section == "forwarding":
            before = {r["id"]: r for r in raw["rules"]}
            require(set(changes) <= {"action", "id", "values"})
            if action == "create":
                if len(before) >= caps(raw)["forwarding_limit"]:
                    raise SafeLineError("safeline_forwarding_limit", 409)
            else:
                integer(key, 1); require(key in before)
            if action in {"create", "edit"}:
                data = changes.get("values"); require(isinstance(data, dict) and set(data) <= {"path_prefix", "upstreams", "comment"})
                data = {**({} if action == "create" else take(before[key], ("path_prefix", "upstreams", "comment"))), **data}
                prefix = string(data.get("path_prefix", "")); require(prefix.startswith("/") and prefix != "/" and not any(ord(c) <= 32 for c in prefix))
                upstreams(data.get("upstreams")); string(data.get("comment", ""), 1024)
                if any(r["path_prefix"] == prefix and r["id"] != key for r in raw["rules"]):
                    invalid()
                writes.append(("POST" if action == "create" else "PUT", path + "/forwarding_rules" + ("" if action == "create" else "/" + str(key)), data))
                return Plan(writes, lambda r: any((v["id"] not in before if action == "create" else v["id"] == key) and all(v.get(k) == x for k, x in data.items()) for v in r["rules"]))
            if action == "switch":
                require(isinstance(changes.get("values"), dict) and set(changes["values"]) == {"is_enabled"})
                enabled = boolean(changes["values"]["is_enabled"])
                return Plan([("PUT", path + "/forwarding_rules/" + str(key) + "/switch", {"is_enabled": enabled})], lambda r: any(v["id"] == key and v.get("is_enabled") == enabled for v in r["rules"]))
            return Plan([("DELETE", path + "/forwarding_rules/" + str(key), None)], lambda r: all(v["id"] != key for v in r["rules"]))
        if section == "routes":
            require(set(changes) == {"action", "ids"})
            ids = changes.get("ids"); require(isinstance(ids, list) and 1 <= len(ids) <= 10000)
            require(all(type(i) is int and i in {r["id"] for r in raw["resources"]} for i in ids) and len(set(ids)) == len(ids))
            return Plan([("DELETE", path + "/resources", {"ids": ids})], lambda r: not set(ids) & {x["id"] for x in r["resources"]})
        commercial(raw)
        base = "/api/business/site/" + identifier + "/anti_tamper"
        if action == "create":
            require(set(changes) == {"action", "resource_ids"})
            ids = changes["resource_ids"]; require(isinstance(ids, list) and 1 <= len(ids) <= 500)
            resources = {r["id"]: r for r in raw["resources"]}; require(all(type(i) is int and i in resources for i in ids))
            wanted = {resources[i]["path"] for i in ids}
            return Plan([("POST", base, {"resource_ids": ids})], lambda r: wanted <= {x["path"] for x in r["tamper"]["data"]})
        if action == "refresh":
            require(set(changes) == {"action"})
            return Plan([("PUT", base, None)], lambda r: r["tamper"].get("refresh_state") == "running" or
                        (r["tamper"].get("refresh_time") or 0) > (raw["tamper"].get("refresh_time") or 0))
        require(set(changes) == {"action", "id"}); integer(key, 1)
        require(key in {r["id"] for r in raw["tamper"]["data"]})
        return Plan([("DELETE" if action == "delete" else "PUT", "/api/business/anti_tamper/" + str(key), None)],
                    lambda r: all(v["id"] != key for v in r["tamper"]["data"]) if action == "delete" else
                    any(v["id"] == key and v.get("changed") is False for v in r["tamper"]["data"]))
    invalid()


class ApplicationEditor:
    def __init__(self, editor):
        self.editor, self.service, self.store = editor, editor.service, editor.store

    async def load(self, identifier, section):
        client, revision = self.service.client()
        raw = await read(client, identifier, section)
        public, locked = values(raw, section, client.connection.token)
        extra = {}
        if section == "advanced":
            extra["global_values"] = advanced.values({"proxy": raw["global_proxy"], "edition": raw["edition"]}, "proxy", client.connection.token)[0]
        if section == "cc":
            extra["global_rules"] = safe(public_rules(raw["global_rules"], client.connection.token)[0], client.connection.token)
        if section == "auth":
            extra["sources"] = [{**r, "name": SOURCE_TYPES.get(r.get("type"), "認證來源") + " · #" + str(r["id"])} for r in raw["sources"]]
            extra["portal"] = raw["portal"]
        return {"values": public, "locked": locked, "capabilities": caps(raw), "fingerprint": self.editor.resource_fingerprint(client, revision, raw),
                "connection_revision": revision, "updated_at": now(), **extra}

    async def log_preview(self, identifier, kind, filename):
        client, revision = self.service.client()
        raw = await read(client, identifier, "log-" + kind)
        if filename not in {r.get("filename") for r in raw["files"]}:
            raise SafeLineError("safeline_resource_not_found", 404)
        lines, truncated = await client.log_lines(identifier, filename)
        redacted = [re.sub(r"(?i)(\b(?:authorization|cookie|set-cookie|password|secret|(?:access_|api_)?token)[\"']?\s*[:=]\s*)[^\r\n]*", r"\1[REDACTED]", safe(line, client.connection.token)) for line in lines]
        return {"lines": redacted, "truncated": truncated, "connection_revision": revision, "updated_at": now()}

    async def save(self, identifier, section, command: EditCommand, actor):
        async with self.editor.lock:
            digest = fingerprint({"kind": "application-pages", "id": identifier, "section": section, **command.model_dump()})
            previous = self.store.receipt(command.operation_id, actor, digest)
            if previous is not None:
                return {**previous, "operation_id": command.operation_id, "connection_revision": command.expected_revision, "replayed": True}
            client, revision = self.service.client()
            if client.connection is None or revision != command.expected_revision:
                raise SafeLineError("safeline_connection_changed", 409)
            raw = await read(client, identifier, section)
            if self.editor.resource_fingerprint(client, revision, raw) != command.fingerprint:
                raise SafeLineError("safeline_resource_changed", 409)
            try:
                plan = build(raw, identifier, section, command.changes, client.connection.token)
            except (TypeError, ValueError, KeyError, AttributeError):
                invalid()
            require(bool(plan.writes))
            if self.store.load()[1] != revision:
                raise SafeLineError("safeline_connection_changed", 409)
            target = fingerprint({"revision": revision, "kind": "applications", "id": identifier})
            self.store.reserve_edit(command.operation_id, actor, digest, target)
            result = {"outcome": "unknown", "code": "safeline_write_unknown"}
            accepted = False
            try:
                for method, path, payload in plan.writes:
                    await client.write(method, path, payload)
                    accepted = True
                updated = await read(client, identifier, section)
                if plan.verify(updated):
                    result = {"outcome": "confirmed", "code": "safeline_write_confirmed"}
            except SafeLineError as error:
                if not accepted and error.code in {"safeline_invalid_credentials", "safeline_permission_denied", "safeline_endpoint_unavailable", "safeline_business_error", "safeline_sso_required"}:
                    result = {"outcome": "failed", "code": error.code}
            finally:
                self.store.finish_edit(command.operation_id, result)
            return {**result, "operation_id": command.operation_id, "connection_revision": revision, "updated_at": now(), "replayed": False}
