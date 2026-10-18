"""Allowlisted dashboard DTOs derived from Komodo resource list items.

Keep this boundary independent of HTTP: full resource/config objects, addresses,
repository URLs, and upstream error text must never reach the browser.
"""

from datetime import datetime, timezone
import math
import re


_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
_POLLING_RATE = re.compile(r"([1-9][0-9]*)-(sec|min|hr|day|wk)")
_SERVER_STATES = {"Ok", "NotOk", "Disabled"}
_STACK_STATES = {
    "deploying", "running", "paused", "stopped", "created", "restarting",
    "dead", "removing", "unhealthy", "down", "unknown",
}
_PROCEDURE_STATES = {"Running", "Ok", "Failed", "Unknown"}


def _text(value, *, limit: int = 512) -> str | None:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    return value


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _integer(value) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() and number > 0 else None


def _percentage(used, total) -> float | None:
    used, total = _number(used), _number(total)
    if used is None or total is None or total == 0:
        return None
    percentage = used / total * 100
    return percentage if math.isfinite(percentage) else None


def _gibibytes_to_bytes(value) -> int | None:
    # Periphery's *_gb fields use BYTES_PER_GB = 2**30 for both memory
    # and disks. Restore bytes here so the browser need not infer the unit.
    number = _number(value)
    if number is None:
        return None
    size = number * (1 << 30)
    return round(size) if math.isfinite(size) else None


def _network_rate(stats: dict) -> float | None:
    ingress = _number(stats.get("network_ingress_bytes"))
    egress = _number(stats.get("network_egress_bytes"))
    polling_rate = stats.get("polling_rate")
    match = _POLLING_RATE.fullmatch(polling_rate) if isinstance(polling_rate, str) and len(polling_rate) < 32 else None
    if ingress is None or egress is None or match is None:
        return None
    # Periphery sums sysinfo received()/transmitted(): these are byte deltas
    # since the last polling refresh, despite older DTO comments saying MB.
    seconds = int(match[1]) * {"sec": 1, "min": 60, "hr": 3600, "day": 86400, "wk": 604800}[match[2]]
    try:
        rate = (ingress + egress) / seconds
    except OverflowError:
        return None
    return rate if math.isfinite(rate) else None


def _load_average(stats: dict) -> list[float] | None:
    load = stats.get("load_average")
    if not isinstance(load, dict):
        return None
    values = [_number(load.get(key)) for key in ("one", "five", "fifteen")]
    return values if all(value is not None for value in values) else None


def _add_error(errors: list[str], code: str) -> None:
    if code not in errors:
        errors.append(code)


def _tags_index(tags: list, errors: list[str]) -> dict[str, str]:
    index = {}
    for tag in tags:
        if not isinstance(tag, dict):
            _add_error(errors, "tags_invalid_response")
            continue
        # Tag uses BSON ObjectId JSON; resource list items use plain id strings.
        identifier = tag.get("_id")
        if isinstance(identifier, dict):
            identifier = identifier.get("$oid")
        identifier = _text(identifier) or _text(tag.get("id"))
        name = _text(tag.get("name"))
        if identifier is None or name is None:
            _add_error(errors, "tags_invalid_response")
            continue
        index[identifier] = name
    return index


def _base(resource: dict, tag_names: dict[str, str]) -> dict | None:
    identifier, name = _text(resource.get("id")), _text(resource.get("name"))
    if identifier is None or name is None:
        return None
    raw_tags = resource.get("tags")
    tags = raw_tags if isinstance(raw_tags, list) else []
    names = [tag_names[tag] for tag in tags if isinstance(tag, str) and tag in tag_names]
    return {
        "id": identifier, "name": name, "template": resource.get("template") is True,
        "tags": list(dict.fromkeys(names)),
    }


def _rows(resources: list, section: str, tag_names: dict[str, str], errors: list[str]):
    for resource in resources:
        if not isinstance(resource, dict):
            _add_error(errors, section + "_invalid_response")
            continue
        row = _base(resource, tag_names)
        if row is None:
            _add_error(errors, section + "_invalid_response")
            continue
        info = resource.get("info")
        if not isinstance(info, dict):
            _add_error(errors, section + "_invalid_response")
            info = {}
        yield row, info


def stack_auto_updates(resources: list, errors: list[str]) -> dict[str, bool]:
    """Reduce full stack configs to IDs and the exact auto-update switch."""
    settings = {}
    for resource in resources:
        if not isinstance(resource, dict):
            _add_error(errors, "stack_settings_invalid_response")
            continue
        identifier = resource.get("_id")
        if isinstance(identifier, dict):
            identifier = identifier.get("$oid")
        identifier = _text(identifier)
        config = resource.get("config")
        enabled = config.get("auto_update") if isinstance(config, dict) else None
        if identifier is None or not isinstance(enabled, bool):
            _add_error(errors, "stack_settings_invalid_response")
            continue
        settings[identifier] = enabled
    return settings


def _available_list(resources: dict, section: str, errors: list[str]) -> bool:
    return isinstance(resources.get(section), list) and not any(
        code.startswith(section + "_") for code in errors
    )


def _server_resource_counts(resources: dict, errors: list[str]) -> dict[str, int] | None:
    # Match Komodo's server table: all linked stacks, deployments and repos,
    # including templates. A failed category must not look like a real zero.
    sections = ("stacks", "deployments", "repos")
    if not all(_available_list(resources, section, errors) for section in sections):
        return None
    counts = {}
    for section in sections:
        seen = set()
        for item in resources[section]:
            info = item.get("info") if isinstance(item, dict) else None
            identifier = _text(item.get("id")) if isinstance(item, dict) else None
            server_id = info.get("server_id") if isinstance(info, dict) else None
            if identifier is None or identifier in seen or not isinstance(server_id, str):
                _add_error(errors, "server_resources_invalid_response")
                return None
            seen.add(identifier)
            if server_id:
                counts[server_id] = counts.get(server_id, 0) + 1
    return counts


def _server_container_counts(resources: dict, errors: list[str]) -> dict[str, dict] | None:
    if not _available_list(resources, "containers", errors):
        return None
    counts = {}
    seen = set()
    for item in resources["containers"]:
        server_id = _text(item.get("server_id")) if isinstance(item, dict) else None
        identifier = _text(item.get("id")) if isinstance(item, dict) else None
        if server_id is None or identifier is None or (server_id, identifier) in seen:
            _add_error(errors, "containers_invalid_response")
            return None
        seen.add((server_id, identifier))
        count = counts.setdefault(server_id, {"running": 0, "stopped": 0, "other": 0})
        state = item.get("state")
        # Same categories as Komodo's GetContainersSummary. Restarting/dead
        # and unknown states are kept separate instead of reported as stopped.
        category = "running" if state == "running" else "stopped" if isinstance(state, str) and state in {
            "created", "paused", "exited",
        } else "other"
        count[category] += 1
    return counts


def assemble_dashboard(
    resources: dict[str, list], initial_errors: list[str], *,
    stack_settings: dict[str, bool] | None = None,
) -> dict:
    errors = list(initial_errors)
    tag_names = _tags_index(resources.get("tags", []), errors)
    resource_counts = _server_resource_counts(resources, errors)
    container_counts = _server_container_counts(resources, errors)
    servers = []
    for row, info in _rows(resources.get("servers", []), "servers", tag_names, errors):
        raw_state = info.get("state")
        state = raw_state if isinstance(raw_state, str) and raw_state in _SERVER_STATES else "NotOk"
        # Disconnected/disabled hosts may retain stale stats. Do not display
        # these as current measurements or turn missing values into zeros.
        stats = info.get("stats") if state == "Ok" else None
        if not isinstance(stats, dict):
            if state == "Ok" and not row["template"]:
                _add_error(errors, "server_metrics_unavailable")
            stats = {}
        version = _text(info.get("version"), limit=128)
        containers = container_counts.get(row["id"], {"running": 0, "stopped": 0, "other": 0}) if (
            container_counts is not None and state == "Ok" and not row["template"]
        ) else None
        row.update({
            "state": state,
            "cpu_percent": _number(stats.get("cpu_perc")),
            "memory_percent": _percentage(stats.get("mem_used_gb"), stats.get("mem_total_gb")),
            "disk_percent": _percentage(stats.get("disk_used_gb"), stats.get("disk_total_gb")),
            "memory_used_bytes": _gibibytes_to_bytes(stats.get("mem_used_gb")),
            "memory_total_bytes": _gibibytes_to_bytes(stats.get("mem_total_gb")),
            "disk_used_bytes": _gibibytes_to_bytes(stats.get("disk_used_gb")),
            "disk_total_bytes": _gibibytes_to_bytes(stats.get("disk_total_gb")),
            "load_average": _load_average(stats),
            "core_count": _integer(info.get("core_count")),
            "logical_core_count": _integer(info.get("logical_core_count")),
            "network_bytes_per_second": _network_rate(stats),
            "version": version if version is not None and _VERSION.fullmatch(version) else None,
            "container_count": sum(containers.values()) if containers is not None else None,
            "containers_running": containers["running"] if containers is not None else None,
            "containers_stopped": containers["stopped"] if containers is not None else None,
            "containers_other": containers["other"] if containers is not None else None,
            "resource_count": resource_counts.get(row["id"], 0) if resource_counts is not None else None,
        })
        if state == "Ok" and isinstance(info.get("stats"), dict) and not row["template"] and any(
            row[key] is None for key in (
                "cpu_percent", "memory_percent", "disk_percent", "network_bytes_per_second",
                "memory_used_bytes", "memory_total_bytes", "disk_used_bytes", "disk_total_bytes",
            )
        ):
            _add_error(errors, "server_metrics_invalid_response")
        servers.append(row)

    server_names = {server["id"]: server["name"] for server in servers}
    stacks = []
    for row, info in _rows(resources.get("stacks", []), "stacks", tag_names, errors):
        if info.get("files_on_host") is True:
            source = "服务器上的文件"
        elif info.get("linked_repo"):
            source = "关联仓库"
        elif info.get("repo"):
            source = "Git 仓库"
        else:
            source = "在界面中定义"
        server_id = _text(info.get("server_id"))
        state = info.get("state")
        services = info.get("services")
        row.update({
            "source": source,
            "server_id": server_id,
            "server_name": _text(info.get("server_name")) or server_names.get(server_id),
            "swarm_id": _text(info.get("swarm_id")),
            "swarm_name": _text(info.get("swarm_name")),
            "state": state if isinstance(state, str) and state in _STACK_STATES else "unknown",
            "service_count": len(services) if isinstance(services, list) else None,
            "auto_update": (stack_settings or {}).get(row["id"]),
        })
        stacks.append(row)

    procedures = []
    for row, info in _rows(resources.get("procedures", []), "procedures", tag_names, errors):
        state = info.get("state")
        next_run_at = _integer(info.get("next_scheduled_run"))
        row.update({
            "state": state if isinstance(state, str) and state in _PROCEDURE_STATES else "Unknown",
            "next_run_at": next_run_at if next_run_at is not None and next_run_at <= 8_640_000_000_000_000 else None,
            "schedule_error": bool(info.get("schedule_error")),
        })
        procedures.append(row)

    return {
        "servers": servers, "stacks": stacks, "procedures": procedures,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "errors": errors,
    }
