"""Global application defaults, with typed choices and preserved private headers."""

import asyncio
import copy
import ipaddress
import json
import re

from plugins.safeline.connection import SafeLineError
from plugins.safeline.service import _SECRET, safe


BOOLS = ("ipv6", "http_1.0", "http2", "force_https", "hsts", "hsts_sub", "hsts_preload",
         "gzip", "br", "sse", "ntlm", "default_server", "reset_xff")
STRINGS = ("ip_source", "ip_value", "host", "xfh", "xfp", "hsts_max_age", "ssl_protocols", "ssl_ciphers")
SOURCES = {"socket_ip", "xff_first", "xff_second", "xff_third", "xff_dynamic", "http_header", "proxy_protocol"}
PROTOCOLS = {"TLSv1", "TLSv1.1", "TLSv1.2", "TLSv1.3", "SSLv2", "SSLv3"}
PREMIUM = {"ssl_protocols", "ssl_ciphers", "http_headers"}
HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}\Z")


def capabilities(raw):
    return {"edition": raw["edition"], "premium": raw["edition"] in {1, 2}}


async def read(client, section):
    path = {"proxy": "/api/open/global/proxy", "groups": "/api/open/site/group/switch"}.get(section)
    if not path:
        raise SafeLineError("invalid_safeline_resource", 422)
    value, edition = await asyncio.gather(client.get(path), client.get("/api/open/system/edition"), return_exceptions=True)
    if isinstance(value, Exception):
        raise value
    level = edition.get("effective_edition", edition.get("version")) if isinstance(edition, dict) else None
    level = level if type(level) is int and level in {0, 1, 2} else None
    if section == "proxy" and not isinstance(value, dict) or section == "groups" and type(value) is not bool:
        raise SafeLineError("safeline_unexpected_response")
    return {section: value, "edition": level}


def headers(values, token):
    if not isinstance(values, list) or len(values) > 128:
        raise ValueError
    result = []
    for index, row in enumerate(values):
        if (not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in ("target", "op", "key", "val")) or
                not HEADER.fullmatch(row["key"]) or row["target"] not in {"req", "res"} or row["op"] not in {"set", "del"} or
                len(row["val"]) > 4096 or token in row["key"]):
            raise ValueError
        item = {k: row[k] for k in ("target", "op", "key", "val")}
        if _SECRET.search(row["key"]) or safe(row["val"], token) != row["val"]:
            item.update(val="", keep=index)
        result.append(item)
    return result


def values(raw, section, token):
    if section == "groups":
        return {"enable": raw["groups"]}, [] if capabilities(raw)["premium"] else ["enable"]
    result, locked = {}, []
    for key in (*BOOLS, *STRINGS, "http_headers"):
        item = raw["proxy"].get(key)
        if not isinstance(item, dict) or type(item.get("global")) is not bool:
            locked.append(key)
            continue
        value = item.get("value")
        if key == "http_headers":
            try:
                value = headers(value, token)
            except ValueError:
                locked.append(key)
                continue
        elif type(value) is not (bool if key in BOOLS else str) or isinstance(value, str) and len(value) > 4096:
            locked.append(key)
            continue
        elif safe(value, token) != value:
            locked.append(key)
            value = safe(value, token)
        result[key] = {"global": item["global"], "value": value}
        if key in PREMIUM and not capabilities(raw)["premium"]:
            locked.append(key)
    return result, sorted(set(locked))


def invalid(code="invalid_safeline_advanced"):
    raise SafeLineError(code, 422)


def proxy_addresses(value):
    for address in value.split(",") if value else []:
        try:
            if "-" in address:
                first, last = (ipaddress.ip_address(v) for v in address.split("-"))
                if first.version != last.version or int(first) > int(last):
                    raise ValueError
            else:
                ipaddress.ip_network(address, strict=False)
        except ValueError:
            invalid("invalid_safeline_source_ip")


def source_ip(source, value):
    if type(source) is not str or type(value) is not str or source not in SOURCES:
        invalid("invalid_safeline_source_ip")
    if source == "http_header":
        if not HEADER.fullmatch(value):
            invalid("invalid_safeline_source_ip")
    elif source == "xff_dynamic":
        def pairs(items):
            result = {}
            for k, v in items:
                if k in result:
                    raise ValueError
                result[k] = v
            return result
        try:
            settings = json.loads(value, object_pairs_hook=pairs)
            if (not isinstance(settings, dict) or set(settings) != {"position", "index"} or
                    settings["position"] not in {"from_start", "from_end"} or type(settings["index"]) is not int or
                    not 1 <= settings["index"] <= 100):
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            invalid("invalid_safeline_source_ip")
    elif source == "proxy_protocol":
        proxy_addresses(value)
    elif value:
        invalid("invalid_safeline_source_ip")


def header_patch(items, original, token):
    if not isinstance(items, list) or len(items) > 128:
        invalid("invalid_safeline_headers")
    result, used = [], set()
    for row in items:
        if not isinstance(row, dict) or set(row) - {"target", "op", "key", "val", "keep"} or any(type(row.get(k)) is not str for k in ("target", "op", "key", "val")):
            invalid("invalid_safeline_headers")
        if row["target"] not in {"req", "res"} or row["op"] not in {"set", "del"} or not HEADER.fullmatch(row["key"]):
            invalid("invalid_safeline_headers")
        item = {k: row[k] for k in ("target", "op", "key", "val")}
        if "keep" in row:
            index = row["keep"]
            if (type(index) is not int or not 0 <= index < len(original) or index in used or row["val"] or
                    any(original[index].get(k) != row[k] for k in ("target", "op", "key")) or
                    not (_SECRET.search(row["key"]) or safe(original[index]["val"], token) != original[index]["val"])):
                invalid("invalid_safeline_headers")
            item["val"] = original[index]["val"]
            used.add(index)
        if len(item["val"]) > 4096 or any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in item["val"]) or item["op"] == "set" and not item["val"]:
            invalid("invalid_safeline_headers")
        if item["op"] == "del":
            item["val"] = ""
        result.append(item)
    return result


def build(raw, section, changes, token):
    current, locked = values(raw, section, token)
    if not changes or not set(changes) <= set(current):
        invalid()
    if section == "groups":
        if not capabilities(raw)["premium"]:
            raise SafeLineError("safeline_feature_unavailable", 409)
        if set(changes) != {"enable"} or type(changes["enable"]) is not bool:
            invalid()
        return "PUT", "/api/commercial/site/group/switch", changes, changes
    patch = {}
    for key, item in changes.items():
        if key in PREMIUM and not capabilities(raw)["premium"]:
            raise SafeLineError("safeline_feature_unavailable", 409)
        if key in locked:
            raise SafeLineError("safeline_edit_unsupported", 409)
        if not isinstance(item, dict) or set(item) != {"global", "value"} or type(item["global"]) is not bool:
            invalid()
        value = item["value"]
        if key == "http_headers":
            value = header_patch(value, raw["proxy"][key]["value"], token)
        elif type(value) is not (bool if key in BOOLS else str) or isinstance(value, str) and len(value) > 4096:
            invalid()
        if isinstance(value, str) and any(ord(c) < 32 or ord(c) == 127 for c in value):
            invalid()
        if key == "host" and value and not re.fullmatch(r"[A-Za-z0-9._:$\[\]-]+", value):
            invalid()
        if key in {"xfh", "xfp"} and value not in {"", "$http_host" if key == "xfh" else "$scheme"}:
            invalid()
        if key == "hsts_max_age" and (not re.fullmatch(r"[0-9]{1,10}", value) or int(value) > 2147483647):
            invalid()
        if key == "ssl_protocols" and (not value.split() or len(value.split()) != len(set(value.split())) or not set(value.split()) <= PROTOCOLS):
            invalid()
        if key == "ssl_ciphers" and not re.fullmatch(r"[A-Za-z0-9_:+!@=. -]*", value):
            invalid()
        patch[key] = {"global": item["global"], "value": value}
    merged = {**raw["proxy"], **patch}
    if {"ip_source", "ip_value"} & set(patch):
        if any(not isinstance(merged.get(k), dict) or "value" not in merged[k] for k in ("ip_source", "ip_value")):
            invalid("invalid_safeline_source_ip")
        source_ip(merged["ip_source"]["value"], merged["ip_value"]["value"])
    return "PUT", "/api/open/global/proxy", copy.deepcopy(patch), patch
