"""Read-only alert history with fixed filters and safe, typed detail fields.

Komodo alert payloads can contain exception traces or arbitrary custom text.
This boundary never exposes those fields, URLs, or complete upstream objects.
"""

import asyncio
from datetime import datetime, timezone
import re
import ssl

import httpx

from plugins.komodo.actions import KomodoActionError, _json, _object_id, _preflight_status
from plugins.komodo.containers import _identity
from plugins.komodo.dashboard import _number, _text


RESOURCE_TYPES = (
    "System", "Swarm", "Server", "Stack", "Deployment", "Build", "Repo",
    "Procedure", "Action", "Builder", "Alerter", "ResourceSync",
)
ALERT_TYPES = (
    "None", "Test", "SwarmUnhealthy", "ServerUnreachable", "ServerCpu", "ServerMem",
    "ServerDisk", "ServerVersionMismatch", "ContainerStateChange",
    "DeploymentImageUpdateAvailable", "DeploymentAutoUpdated", "StackStateChange",
    "StackImageUpdateAvailable", "StackAutoUpdated", "AwsBuilderTerminationFailed",
    "ResourceSyncPendingUpdates", "BuildFailed", "RepoBuildFailed", "ProcedureFailed",
    "ActionFailed", "ScheduleRun", "Custom",
)
_RESOURCE_FOR_ALERT = {
    "Test": "Alerter", "SwarmUnhealthy": "Swarm",
    "ServerUnreachable": "Server", "ServerCpu": "Server", "ServerMem": "Server",
    "ServerDisk": "Server", "ServerVersionMismatch": "Server",
    "ContainerStateChange": "Deployment", "DeploymentImageUpdateAvailable": "Deployment",
    "DeploymentAutoUpdated": "Deployment", "StackStateChange": "Stack",
    "StackImageUpdateAvailable": "Stack", "StackAutoUpdated": "Stack",
    "ResourceSyncPendingUpdates": "ResourceSync", "BuildFailed": "Build",
    "RepoBuildFailed": "Repo", "ProcedureFailed": "Procedure", "ActionFailed": "Action",
}
_SERVER_ALERTS = {"ServerUnreachable", "ServerCpu", "ServerMem", "ServerDisk", "ServerVersionMismatch"}
_HOSTED_ALERTS = {
    "ContainerStateChange", "DeploymentImageUpdateAvailable", "DeploymentAutoUpdated",
    "StackStateChange", "StackImageUpdateAvailable", "StackAutoUpdated",
}
_STACK_STATES = {
    "deploying", "running", "paused", "stopped", "created", "restarting", "dead",
    "removing", "unhealthy", "down", "unknown",
}
_DEPLOYMENT_STATES = {
    "deploying", "running", "created", "restarting", "stopping", "removing", "paused",
    "exited", "dead", "unhealthy", "not_deployed", "unknown",
}
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
_INSTANCE_ID = re.compile(r"i-[0-9a-f]{8,17}")
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,1023}")
_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}")
_MAX_PAGE = 1_000_000
_REQUEST_SECONDS = 10.0


def _filters(page, resource_type, alert_type, unresolved_only) -> dict | None:
    if (
        not isinstance(page, int) or isinstance(page, bool) or not 0 <= page <= _MAX_PAGE
        or not isinstance(resource_type, str) or resource_type not in ("", *RESOURCE_TYPES)
        or not isinstance(alert_type, str) or alert_type not in ("", *ALERT_TYPES)
        or not isinstance(unresolved_only, bool)
    ):
        raise KomodoActionError("invalid_request", 400)
    query = {}
    if resource_type:
        query["target.type"] = resource_type
    if alert_type:
        query["data.type"] = alert_type
    if unresolved_only:
        query["resolved"] = False
    return query or None


def _timestamp(value) -> int | None:
    # I64 timestamps are milliseconds already. Do not guess seconds, coerce
    # strings/bools, substitute zero for missing data, or overflow JS Date.
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 8_640_000_000_000_000 else None


def _image(value) -> str | None:
    if not isinstance(value, str) or _IMAGE.fullmatch(value) is None or "://" in value:
        return None
    # Docker image digests use @sha256:..., not username/password userinfo.
    if "@" in value and re.fullmatch(r"[^@]+@sha256:[0-9a-fA-F]{64}", value) is None:
        return None
    return value


def _version(value):
    if isinstance(value, str):
        return value if len(value) <= 128 and _VERSION.fullmatch(value) else None
    if isinstance(value, dict) and set(value) == {"major", "minor", "patch"}:
        if all(isinstance(part, int) and not isinstance(part, bool) and 0 <= part <= 2_147_483_647 for part in value.values()):
            return {part: value[part] for part in ("major", "minor", "patch")}
    return None


def _details(variant: str, payload, target_type: str, target_id: str | None) -> tuple[dict, bool, str | None]:
    if not isinstance(payload, dict) or variant not in ALERT_TYPES:
        return {}, True, None
    result = {}
    omitted = False
    handled = set()

    def pick(field, validator):
        nonlocal omitted
        handled.add(field)
        if field not in payload or payload[field] is None:
            return
        value = validator(payload[field])
        if value is None:
            omitted = True
        else:
            result[field] = value

    expected_type = _RESOURCE_FOR_ALERT.get(variant)
    if variant == "ScheduleRun":
        expected_type = payload.get("resource_type")
        pick("resource_type", lambda value: value if isinstance(value, str) and value in {"Procedure", "Action"} else None)

    resource_name = None
    if expected_type is not None:
        pick("id", _object_id)
        identifier = result.get("id")
        # Names are historical metadata, but only for the alert's actual target.
        # Never attach another resource's name based solely on an embedded field.
        identity_matches = identifier is not None and identifier == target_id and expected_type == target_type
        handled.add("name")
        if identity_matches:
            pick("name", _text)
            resource_name = result.get("name")
        elif payload.get("name") is not None:
            omitted = True

    if variant in _SERVER_ALERTS:
        pick("region", _text)
    if variant == "ServerCpu":
        pick("percentage", _number)
    if variant in {"ServerMem", "ServerDisk"}:
        pick("used_gb", _number)
        pick("total_gb", _number)
    if variant == "ServerDisk":
        pick("path", lambda value: _text(value, limit=1024))
    if variant == "ServerVersionMismatch":
        pick("server_version", _version)
        pick("core_version", _version)
    if variant in _HOSTED_ALERTS:
        for prefix in ("server", "swarm"):
            pick(prefix + "_id", _object_id)
            handled.add(prefix + "_name")
            if prefix + "_id" in result:
                pick(prefix + "_name", _text)
            elif payload.get(prefix + "_name") is not None:
                omitted = True
    if variant in {"ContainerStateChange", "StackStateChange"}:
        states = _STACK_STATES if variant == "StackStateChange" else _DEPLOYMENT_STATES
        for field in ("from", "to"):
            pick(field, lambda value: value if isinstance(value, str) and value in states else None)
    if variant in {"DeploymentImageUpdateAvailable", "DeploymentAutoUpdated", "StackImageUpdateAvailable"}:
        pick("image", _image)
    if variant == "StackImageUpdateAvailable":
        pick("service", lambda value: value if isinstance(value, str) and _SERVICE.fullmatch(value) else None)
    if variant == "StackAutoUpdated":
        def images(value):
            if not isinstance(value, list) or len(value) > 100:
                return None
            valid = [_image(image) for image in value]
            return valid if all(image is not None for image in valid) else None
        pick("images", images)
    if variant == "AwsBuilderTerminationFailed":
        pick("instance_id", lambda value: value if isinstance(value, str) and _INSTANCE_ID.fullmatch(value) else None)
    if variant == "BuildFailed":
        pick("version", _version)

    # err, AWS message, Custom message/details and all future unknown fields are
    # deliberately absent. They may embed arbitrary credentials or addresses.
    if any(field not in handled and value is not None for field, value in payload.items()):
        omitted = True
    return result, omitted, resource_name


def _alert(item) -> dict:
    if not isinstance(item, dict):
        raise KomodoActionError("invalid_response")
    target = item.get("target")
    target = target if isinstance(target, dict) else {}
    resource_type = target.get("type")
    resource_type = resource_type if isinstance(resource_type, str) and resource_type in RESOURCE_TYPES else "unknown"
    raw_target_id = target.get("id")
    if raw_target_id == "":
        resource_id = ""
    elif resource_type == "System" and raw_target_id == "system":
        resource_id = "system"
    else:
        resource_id = _object_id(raw_target_id)
    data = item.get("data")
    data = data if isinstance(data, dict) else {}
    variant = data.get("type")
    variant = variant if isinstance(variant, str) and variant in ALERT_TYPES else "unknown"
    details, omitted, resource_name = _details(variant, data.get("data"), resource_type, resource_id)
    level = item.get("level")
    return {
        "id": _object_id(item.get("_id")) or _object_id(item.get("id")),
        "ts": _timestamp(item.get("ts")), "resolved_ts": _timestamp(item.get("resolved_ts")),
        "resolved": item.get("resolved") if isinstance(item.get("resolved"), bool) else None,
        "level": level if isinstance(level, str) and level in {"OK", "WARNING", "CRITICAL"} else "unknown",
        "resource_type": resource_type, "resource_id": resource_id, "resource_name": resource_name,
        "alert_type": variant, "details": details, "details_omitted": omitted,
    }


async def list_alerts(*, page: int = 0, resource_type: str = "", alert_type: str = "",
                      unresolved_only: bool = False, origin: str | None, key: str | None,
                      secret: str | None, verify: ssl.SSLContext | bool,
                      transport: httpx.AsyncBaseTransport | None) -> dict:
    query = _filters(page, resource_type, alert_type, unresolved_only)
    if not origin or not key or not secret:
        raise KomodoActionError("not_configured", 503)
    headers = {"x-api-key": key, "x-api-secret": secret}
    try:
        async with httpx.AsyncClient(
            verify=verify, transport=transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0),
        ) as client:
            await _identity(client, origin, headers)
            client.cookies.clear()
            async with asyncio.timeout(_REQUEST_SECONDS):
                response = await client.post(origin + "/read", headers=headers, json={
                    "type": "ListAlerts", "params": {"page": page, "query": query},
                })
            _preflight_status(response)
            payload = _json(response)
    except (httpx.HTTPError, TimeoutError):
        raise KomodoActionError("upstream_unavailable") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("alerts"), list) or "next_page" not in payload:
        raise KomodoActionError("invalid_response")
    next_page = payload["next_page"]
    if next_page is not None and (
        not isinstance(next_page, int) or isinstance(next_page, bool) or next_page != page + 1
    ):
        raise KomodoActionError("invalid_response")
    if next_page is not None and next_page > _MAX_PAGE:
        next_page = None
    if len(payload["alerts"]) > 100:
        raise KomodoActionError("invalid_response")
    return {
        "alerts": [_alert(item) for item in payload["alerts"]], "page": page, "next_page": next_page,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
