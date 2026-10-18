"""Traffic and posture share a requested scope without hiding upstream limits."""

import asyncio
import math
import time

from plugins.safeline.connection import SafeLineError
from plugins.safeline.service import now, number, rows, safe, take, text


PRESETS = ("last1Day", "today", "last7Day", "last30Day")
_DAYS = {"last1Day": 1, "last7Day": 7, "last30Day": 30}
_INTERCEPT = ("block", "blacklist", "challenge", "rate_limit", "auth_defense", "offline")
_POSTURE = ("attack_deny", "attack_allow", "black_hit", "white_hit", "acl_hit", "waiting_hit",
            "challenge_deny", "challenge_allow", "auth_deny", "auth_allow")


def count(value):
    result = number(value)
    if result is None or result < 0:
        return None
    return result


def capability(edition):
    # Target 9.4.2 CN capability catalog, also verified against its CE responses.
    # effective_edition matters when a paid license is blocked or has expired.
    value = edition.get("effective_edition", edition.get("version")) if isinstance(edition, dict) else None
    value = value if type(value) is int and value in {0, 1, 2} else None
    traffic = list(PRESETS) if value in {1, 2} else ["last1Day"]
    security = list(PRESETS) if value in {1, 2} else ["last1Day", "last7Day"] if value == 0 else ["last1Day"]
    return {"edition": value, "traffic": {"presets": traffic, "applications": value in {1, 2}},
            "security": {"presets": security, "applications": value == 2},
            "presets": [p for p in PRESETS if p in traffic or p in security]}


def sequence(value):
    if not isinstance(value, list) or len(value) > 3000 or any(not isinstance(v, dict) for v in value):
        raise SafeLineError("safeline_unexpected_response")
    return value


def trend(value):
    result = []
    for row in sequence(value):
        timestamp = number(row.get("time"))
        if timestamp is None:
            raise SafeLineError("safeline_unexpected_response")
        result.append({"time": timestamp, "count": count(row.get("count"))})
    return sorted(result, key=lambda row: row["time"])


def keyed_counts(value, *, timestamps=False):
    result = []
    for row in sequence(value):
        if len(row) != 1:
            raise SafeLineError("safeline_unexpected_response")
        key, raw = next(iter(row.items()))
        try:
            parsed = float(raw) if isinstance(raw, str) else number(raw)
            if parsed is None or not math.isfinite(parsed) or parsed < 0:
                raise ValueError
            if timestamps:
                key = int(key)
                if key <= 0:
                    raise ValueError
            elif not isinstance(key, str) or len(key) > 128:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise SafeLineError("safeline_unexpected_response") from None
        result.append({"time" if timestamps else "name": key, "count": parsed})
    return sorted(result, key=lambda row: row["time"] if timestamps else -row["count"])


def decode(name, value, site_id):
    if name == "access":
        if not isinstance(value, dict):
            raise SafeLineError("safeline_unexpected_response")
        return {k: count(value.get(k)) for k in ("access", "pv", "session", "ip")}
    if name == "attack":
        if not isinstance(value, dict) or not isinstance(value.get("intercept"), dict):
            raise SafeLineError("safeline_unexpected_response")
        return {"attack_ip": count(value.get("attack_ip")), "intercept": {k: count(value["intercept"].get(k)) for k in _INTERCEPT}}
    if name in {"error_upstream", "error_waf"}:
        if not isinstance(value, dict):
            raise SafeLineError("safeline_unexpected_response")
        return {k: count(value.get(k)) for k in ("error_4xx", "error_5xx")}
    if name in {"access_trend", "intercept_trend"}:
        return trend(value)
    if name.startswith("geo_"):
        return sorted([{"country": text(v.get("country"), 128), "province": text(v.get("province"), 128), "count": count(v.get("count"))}
                       for v in sequence(value)], key=lambda row: -(row["count"] or 0))
    if name == "qps":
        result = []
        for row in rows(value, "nodes")[-35:]:
            values = [count(v) for k, v in row.items() if k != "time"]
            # The console's source buckets are five-second request counts.
            current = math.ceil(sum(values) / 5) if values and all(v is not None for v in values) else None
            result.append({"time": text(row.get("time"), 100), "count": current})
        return result
    if name == "posture":
        if not isinstance(value, dict):
            raise SafeLineError("safeline_unexpected_response")
        result = {k: count(value.get(k)) for k in _POSTURE}
        tamper = value.get("anti_tamper")
        result["anti_tamper"] = keyed_counts(tamper) if isinstance(tamper, list) else None
        return result
    if name == "posture_trend":
        if not isinstance(value, dict):
            raise SafeLineError("safeline_unexpected_response")
        return {"denied": keyed_counts(value.get("item"), timestamps=True), "allowed": keyed_counts(value.get("pass"), timestamps=True),
                "sources": keyed_counts(value.get("top"))}
    if name == "realtime":
        result = []
        for row in rows(value, "event")[:7]:
            if site_id is not None and row.get("site_id") != site_id:
                raise SafeLineError("safeline_statistics_scope_unavailable")
            event = row.get("event")
            result.append({"type": row.get("event_type") if type(row.get("event_type")) is int else 0,
                           "site_id": number(row.get("site_id")), "site_name": text(row.get("site_name"), 300),
                           "time": number(row.get("time_stamp")), "attack_type": text(row.get("attack_type"), 100),
                           "event": take(event, ("ip", "port", "host", "start", "end")) if isinstance(event, dict) else None})
        return result
    raise SafeLineError("safeline_unexpected_response")


async def overview(service, preset, site_id, utc_offset):
    client, revision = service.client()
    if client.connection is None:
        raise SafeLineError("safeline_not_configured", 409)
    end = int(time.time())
    begin = ((end - utc_offset * 60) // 86400) * 86400 + utc_offset * 60 if preset == "today" else end - _DAYS[preset] * 86400
    params = {"begin_time": begin, "end_time": end, "time_preset": preset}
    if site_id is not None:
        params["site_id"] = site_id
    errors = {}
    try:
        caps = capability(await client.get("/api/open/system/edition"))
    except SafeLineError as error:
        caps = capability(None)
        errors["capabilities"] = error.code

    jobs = []

    def add(name, path, query, family, *, realtime=False):
        if site_id is not None and not caps[family]["applications"]:
            errors[name] = "safeline_statistics_scope_unavailable"
        elif not realtime and preset not in caps[family]["presets"]:
            errors[name] = "safeline_statistics_range_unavailable"
        else:
            jobs.append((name, path, query))

    for name, path in (("access", "advance/access"), ("attack", "advance/attack"),
                       ("access_trend", "advance/trend/access"), ("intercept_trend", "advance/trend/intercept")):
        add(name, "/api/stat/" + path, params, "traffic")
    for name, upstream in (("error_upstream", "true"), ("error_waf", "false")):
        add(name, "/api/stat/advance/error_status_code", {**params, "upstream": upstream}, "traffic")
    for region, world in (("world", "true"), ("china", "false")):
        for mode, action in (("access", -1), ("intercept", 1)):
            add("geo_" + region + "_" + mode, "/api/stat/advance/location", {**params, "global": world, "action": action, "size": -1}, "traffic")
    scope = {"site_id": site_id} if site_id is not None else {}
    add("qps", "/api/stat/qps", {"count": 35, **scope}, "traffic", realtime=True)
    add("posture", "/api/open/security_posture/statistics", params, "security")
    add("posture_trend", "/api/open/security_posture/trends", {**params, "type": "attack", "top": 10}, "security")
    add("realtime", "/api/open/security_posture/realtime", {"top": 7, **scope}, "security", realtime=True)
    results = await asyncio.gather(*(client.get(path, query) for name, path, query in jobs), return_exceptions=True)
    sections = {}
    for (name, path, query), value in zip(jobs, results):
        try:
            if isinstance(value, BaseException):
                raise value
            sections[name] = decode(name, value, site_id)
        except (SafeLineError, ValueError, TypeError, KeyError) as error:
            errors[name] = error.code if isinstance(error, SafeLineError) else "safeline_unexpected_response"
    return safe({"sections": sections, "errors": errors, "capabilities": caps,
                 "filters": {"preset": preset, "site_id": site_id, "utc_offset": utc_offset},
                 "begin_time": begin, "end_time": end, "hours": (end - begin) / 3600,
                 "connection_revision": revision, "updated_at": now()}, client.connection.token)
