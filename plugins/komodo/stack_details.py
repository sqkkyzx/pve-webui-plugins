"""Fixed Stack detail/config/file/log APIs. Routes own PVE auth and the ledger.

Only original Stack configuration and explicitly selected file/log text cross
this boundary. API credentials, provider tokens, complete resources and Update
logs never do. Saving retains Komodo's native auto-update and Git semantics.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import re
import ssl

import httpx

from plugins.komodo.actions import KomodoActionError, _json, _object_id, _result, _submitted_result
from plugins.komodo.containers import _identity
from plugins.komodo.komodo import _credential, _origin


BOOL_FIELDS = (
    "auto_pull", "run_build", "poll_for_updates", "auto_update", "auto_update_all_services",
    "destroy_before_deploy", "skip_secret_interp", "git_https", "reclone", "webhook_enabled",
    "webhook_force_deploy", "files_on_host", "send_alerts",
)
STRING_FIELDS = (
    "swarm_id", "server_id", "project_name", "linked_repo", "git_provider", "git_account", "repo", "branch",
    "commit", "clone_path", "webhook_secret", "run_directory", "env_file_path", "registry_provider",
    "registry_account", "compose_cmd_wrapper", "file_contents", "environment",
)
LIST_FIELDS = ("links", "auto_update_skip_services", "file_paths", "extra_args", "build_extra_args",
               "compose_cmd_wrapper_include", "ignore_services")
CONFIG_FIELDS = (*BOOL_FIELDS, *STRING_FIELDS, *LIST_FIELDS, "additional_env_files", "config_files", "pre_deploy", "post_deploy")
MAX_TEXT = 1_048_576
MAX_CONFIG_BYTES = 2_097_152
_REQUEST_SECONDS = 10.0
_HASH = re.compile(r"[0-9a-f]{64}")
_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}")
_STATES = {"deploying", "running", "paused", "stopped", "created", "restarting", "dead", "removing", "unhealthy", "down", "unknown"}
_STREAM_LIMIT = 1_048_576
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")


def _invalid(code="invalid_stack_details", status=422):
    raise KomodoActionError(code, status)


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value, limit=8192, *, multiline=False):
    if (not isinstance(value, str) or len(value) > limit
            or any(ord(char) < 32 and not (multiline and char in "\r\n\t") or ord(char) == 127 for char in value)):
        _invalid()
    return value


def _strings(value):
    if not isinstance(value, list) or len(value) > 256:
        _invalid()
    return [_text(item, 2048) for item in value]


def _command(value):
    if not isinstance(value, dict) or set(value) - {"path", "command", "shell_mode"}:
        _invalid()
    result = {"path": "", "command": "", "shell_mode": False, **value}
    _text(result["path"], 2048)
    _text(result["command"], MAX_TEXT, multiline=True)
    if type(result["shell_mode"]) is not bool:
        _invalid()
    return result


def _additional_files(value, *, dependencies):
    if not isinstance(value, list) or len(value) > 256:
        _invalid()
    result = []
    for item in value:
        # Both DTOs deliberately support the original legacy string form.
        if isinstance(item, str):
            item = {"path": item}
        allowed = {"path", "services", "requires"} if dependencies else {"path", "track"}
        if not isinstance(item, dict) or "path" not in item or set(item) - allowed:
            _invalid()
        row = {"path": _text(item["path"], 2048)}
        if dependencies:
            row["services"] = _strings(item.get("services", []))
            requires = item.get("requires", "None")
            if not isinstance(requires, str) or requires.lower() not in {"none", "restart", "redeploy"}:
                _invalid()
            row["requires"] = {"none": "None", "restart": "Restart", "redeploy": "Redeploy"}[requires.lower()]
        else:
            row["track"] = item.get("track", True)
            if type(row["track"]) is not bool:
                _invalid()
        result.append(row)
    return result


def validate_config_patch(config):
    if not isinstance(config, dict) or not config or set(config) - set(CONFIG_FIELDS):
        _invalid("invalid_stack_config")
    result = {}
    for field, value in config.items():
        if field in BOOL_FIELDS:
            if type(value) is not bool:
                _invalid("invalid_stack_config")
            result[field] = value
        elif field in STRING_FIELDS:
            multiline = field in {"file_contents", "environment", "compose_cmd_wrapper"}
            result[field] = _text(value, MAX_TEXT if multiline else 8192, multiline=multiline)
        elif field in LIST_FIELDS:
            result[field] = _strings(value)
        elif field in {"pre_deploy", "post_deploy"}:
            result[field] = _command(value)
        else:
            result[field] = _additional_files(value, dependencies=field == "config_files")
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_CONFIG_BYTES:
        _invalid("stack_config_too_large", 413)
    return result


def _redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED_SECRET]")
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    return value


def _credentials(origin, key, secret):
    if not origin or not key or not secret:
        _invalid("not_configured", 503)
    try:
        origin = _origin(origin)
        key, secret = _credential(key), _credential(secret)
    except ValueError:
        _invalid("invalid_upstream_configuration", 503)
    if not origin or not key or not secret:
        _invalid("invalid_upstream_configuration", 503)
    return origin, {"x-api-key": key, "x-api-secret": secret}, sorted({key, secret}, key=len, reverse=True)


def _identifier(value):
    if not isinstance(value, str) or _object_id(value) is None:
        _invalid("invalid_stack_id")
    return value.lower()


async def _read(variant, params, *, origin, headers, verify, transport):
    try:
        async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0)) as client:
            async with asyncio.timeout(_REQUEST_SECONDS):
                response = await client.post(origin + "/read", headers=headers, json={"type": variant, "params": params})
    except (httpx.HTTPError, TimeoutError):
        _invalid("upstream_unavailable", 502)
    if response.status_code in {401, 403, 404}:
        _invalid({401: "invalid_credentials", 403: "permission_denied", 404: "stack_unavailable"}[response.status_code],
                 403 if response.status_code == 403 else 404 if response.status_code == 404 else 502)
    if response.status_code != 200:
        _invalid("upstream_unavailable", 502)
    data = _json(response)
    if data is None:
        _invalid("invalid_response", 502)
    return data


async def _snapshot(stack_id, *, origin, key, secret, verify, transport):
    stack_id = _identifier(stack_id)
    origin, headers, secrets = _credentials(origin, key, secret)
    try:
        async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0)) as client:
            await _identity(client, origin, headers)
    except (httpx.HTTPError, TimeoutError):
        _invalid("upstream_unavailable", 502)
    options = {"origin": origin, "headers": headers, "verify": verify, "transport": transport}
    resource, permission, core, rows = await asyncio.gather(*(
        _read(variant, params, **options) for variant, params in (
            ("GetStack", {"stack": stack_id}),
            ("GetPermission", {"target": {"type": "Stack", "id": stack_id}}),
            ("GetCoreInfo", {}),
            ("ListStacks", {"query": {"names": [stack_id], "templates": "Include"}, "limit": 0}),
        )
    ))
    if (not isinstance(resource, dict) or _object_id(resource.get("_id")) != stack_id
            or resource.get("template") is not False or not isinstance(resource.get("config"), dict)
            or not isinstance(resource.get("info"), dict)):
        _invalid("stack_unavailable", 409)
    try:
        name = _text(resource.get("name"), 512)
        description = _text(resource.get("description", ""), 16_384, multiline=True)
        config = {field: validate_config_patch({field: resource["config"][field]})[field]
                  if field in resource["config"] else None for field in CONFIG_FIELDS}
    except KomodoActionError:
        _invalid("unsupported_stack_config", 502)
    if (not isinstance(permission, dict) or not isinstance(permission.get("level"), str)
            or permission["level"] not in {"None", "Read", "Execute", "Write"}
            or not isinstance(permission.get("specific"), list) or not isinstance(core, dict)
            or type(core.get("ui_write_disabled")) is not bool or not isinstance(rows, list)):
        _invalid("invalid_response", 502)
    matches = [row for row in rows if isinstance(row, dict) and _object_id(row.get("id")) == stack_id]
    if len(matches) != 1 or matches[0].get("template") is not False or not isinstance(matches[0].get("info"), dict):
        _invalid("stack_unavailable", 409)
    server_id, swarm_id = config["server_id"], config["swarm_id"]
    if not isinstance(server_id, str) or not isinstance(swarm_id, str):
        _invalid("unsupported_stack_config", 502)
    source = "host" if config["files_on_host"] is True else "git" if config["repo"] or config["linked_repo"] else "ui" if config["file_contents"] else "none"
    raw_state = matches[0]["info"].get("state")
    state = raw_state if isinstance(raw_state, str) and raw_state in _STATES else "unknown"
    return {"stack_id": stack_id, "stack_name": name, "description": description, "server_id": server_id,
            "swarm_id": swarm_id, "state": state, "config": config, "source_mode": source,
            "can_write": permission["level"] == "Write" and not core["ui_write_disabled"],
            "can_execute": permission["level"] in {"Execute", "Write"},
            "can_logs": permission["level"] in {"Read", "Execute", "Write"} and "Logs" in permission["specific"],
            "_resource": resource, "_options": options, "_secrets": secrets}


def _files(snapshot):
    info, errors = snapshot["_resource"]["info"], []
    remote, deployed, missing = [], [], []
    for field, target in (("remote_contents", remote), ("deployed_contents", deployed)):
        rows = info.get(field)
        if rows is None:
            continue
        if not isinstance(rows, list) or len(rows) > 256:
            _invalid("unsupported_stack_files", 502)
        seen = set()
        for row in rows:
            try:
                if not isinstance(row, dict):
                    _invalid()
                path, contents = _text(row.get("path"), 2048), _text(row.get("contents"), MAX_TEXT, multiline=True)
                if not path or path in seen:
                    _invalid()
                seen.add(path)
                target.append({"path": path, "contents": contents})
            except KomodoActionError:
                _invalid("unsupported_stack_files", 502)
    rows = info.get("remote_errors")
    if rows is not None:
        if not isinstance(rows, list) or len(rows) > 256:
            _invalid("unsupported_stack_files", 502)
        for row in rows:
            try:
                path = _text(row.get("path"), 2048) if isinstance(row, dict) else ""
                if not path:
                    _invalid()
            except KomodoActionError:
                _invalid("unsupported_stack_files", 502)
            missing.append({"path": path, "error_code": "source_file_unavailable"})
            if path not in {item["path"] for item in remote}:
                remote.append({"path": path, "contents": None})
            if "source_file_unavailable" not in errors:
                errors.append("source_file_unavailable")
    deployed_config = info.get("deployed_config")
    if deployed_config is not None:
        try:
            _text(deployed_config, MAX_TEXT, multiline=True)
        except KomodoActionError:
            _invalid("unsupported_stack_files", 502)
    return remote, deployed, missing, deployed_config, errors


def _version(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


async def _services(snapshot):
    rows = await _read("ListStackServices", {"stack": snapshot["stack_id"]}, **snapshot["_options"])
    if not isinstance(rows, list):
        _invalid("invalid_response", 502)
    result = []
    for row in rows:
        service = row.get("service") if isinstance(row, dict) else None
        if (not isinstance(row, dict) or _object_id(row.get("stack_id")) != snapshot["stack_id"]
                or not isinstance(service, str) or _SERVICE.fullmatch(service) is None):
            _invalid("invalid_response", 502)
        if service not in result:
            result.append(service)
    return result


def _service_choices(snapshot):
    # Match the native config selector: an empty deployed list is authoritative.
    info = snapshot["_resource"]["info"]
    rows = info.get("deployed_services")
    if rows is None:
        rows = info.get("latest_services", [])
    if not isinstance(rows, list):
        return []
    result, seen = [], set()
    for row in rows:
        name = row.get("service_name") if isinstance(row, dict) else None
        if isinstance(name, str) and _SERVICE.fullmatch(name) is not None and name not in seen:
            result.append(name)
            seen.add(name)
    return result


async def get_stack_details(stack_id, *, origin, key, secret, verify: ssl.SSLContext | bool = True, transport=None):
    snapshot = await _snapshot(stack_id, origin=origin, key=key, secret=secret, verify=verify, transport=transport)
    files, deployed, missing, deployed_config, errors = _files(snapshot)
    version = _version({"stack_id": snapshot["stack_id"], "server_id": snapshot["server_id"], "swarm_id": snapshot["swarm_id"],
        "config": snapshot["config"], "updated_at": snapshot["_resource"].get("updated_at"), "files": files,
        "deployed": deployed, "deployed_config": deployed_config})
    selectors = {}
    async def selector(name, variant):
        try:
            rows = await _read(variant, {"query": {"templates": "Exclude"}, "limit": 0}, **snapshot["_options"])
            if not isinstance(rows, list):
                _invalid()
            result, seen = [], set()
            for row in rows:
                identifier = _object_id(row.get("id")) if isinstance(row, dict) else None
                label = _text(row.get("name"), 512) if isinstance(row, dict) else None
                if not identifier or label is None or identifier in seen or row.get("template") is not False:
                    _invalid()
                seen.add(identifier)
                result.append({"id": identifier, "name": label})
            return name, result, None
        except KomodoActionError:
            return name, [], name + "_unavailable"
    async def services():
        try:
            return await _services(snapshot), None
        except KomodoActionError:
            return [], "services_unavailable"
    results = await asyncio.gather(*(selector(name, variant) for name, variant in (
        ("servers", "ListServers"), ("swarms", "ListSwarms"), ("repos", "ListRepos"))), services())
    for name, rows, error in results[:3]:
        selectors[name] = rows
        if error:
            errors.append(error)
    service_names, error = results[3]
    if error:
        errors.append(error)
    output = {field: value for field, value in snapshot.items() if not field.startswith("_")}
    safe_config = _redact(snapshot["config"], snapshot["_secrets"])
    redacted_fields = [field for field in CONFIG_FIELDS if safe_config[field] != snapshot["config"][field]]
    safe_files = []
    for item in files:
        safe = _redact(item, snapshot["_secrets"])
        redacted = safe != item
        safe_files.append({**safe, "version": _version({"stack_id": snapshot["stack_id"], **item}),
            "editable": snapshot["can_write"] and snapshot["source_mode"] in {"host", "git"} and not redacted,
            "redacted": redacted})
    output.update(config=safe_config, redacted_fields=redacted_fields, version=version, files=safe_files,
        deployed_files=_redact(deployed, snapshot["_secrets"]), deployed_config=_redact(deployed_config, snapshot["_secrets"]),
        remote_errors=_redact(missing, snapshot["_secrets"]), selectors=_redact(selectors, snapshot["_secrets"]),
        services=_redact(service_names, snapshot["_secrets"]),
        service_choices=_redact(_service_choices(snapshot), snapshot["_secrets"]), errors=errors, updated_at=_now(),
        save_effects=["config_source_changes_may_auto_deploy", "source_file_changes_may_auto_deploy"] +
            (["git_file_save_commits_and_pushes"] if snapshot["source_mode"] == "git" else []))
    return _redact(output, snapshot["_secrets"])


async def update_stack_details(stack_id, *, action, base_version, expected_server_id, config=None,
                               file_path=None, contents=None, before_write=None, origin, key, secret,
                               verify: ssl.SSLContext | bool = True, transport=None):
    if (not isinstance(action, str) or action not in {"config", "file"} or not isinstance(base_version, str)
            or _HASH.fullmatch(base_version) is None or not isinstance(expected_server_id, str)):
        _invalid()
    if action == "config":
        if file_path is not None or contents is not None:
            _invalid()
        patch = validate_config_patch(config)
    else:
        if config is not None:
            _invalid()
        _text(file_path, 2048)
        _text(contents, MAX_TEXT, multiline=True)
    snapshot = await get_stack_details(stack_id, origin=origin, key=key, secret=secret, verify=verify, transport=transport)
    if not snapshot["can_write"]:
        _invalid("stack_read_only", 403)
    if snapshot["server_id"] != expected_server_id or snapshot["version"] != base_version:
        _invalid("stack_version_conflict", 409)
    if action == "config":
        if set(patch) & set(snapshot["redacted_fields"]):
            _invalid("redacted_field_read_only", 409)
        variant, params = "UpdateStack", {"id": snapshot["stack_id"], "config": patch}
    else:
        files = [item for item in snapshot["files"] if item["path"] == file_path and item["editable"]]
        if len(files) != 1:
            _invalid("source_file_read_only", 409)
        variant, params = "WriteStackFileContents", {"stack": snapshot["stack_id"], "file_path": file_path, "contents": contents}
    if before_write is not None:
        await before_write()
    origin, headers, _ = _credentials(origin, key, secret)
    try:
        async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0)) as client:
            async with asyncio.timeout(_REQUEST_SECONDS):
                response = await client.post(origin + "/write", headers=headers, json={"type": variant, "params": params})
    except (httpx.HTTPError, TimeoutError):
        result = _result(snapshot["stack_id"], "outcome_unknown", error_code="outcome_unknown")
    else:
        if action == "config" and 200 <= response.status_code < 300:
            raw = _json(response)
            result = _result(snapshot["stack_id"], "completed") if isinstance(raw, dict) and _object_id(raw.get("_id")) == snapshot["stack_id"] else _result(snapshot["stack_id"], "outcome_unknown", error_code="outcome_unknown")
        else:
            result = _submitted_result(response, "stacks", "file", snapshot["stack_id"], "WriteStackContents")
    return {field: result[field] for field in ("ok", "status", "operation_id", "error_code")}


def _log_text(value, secrets):
    if not isinstance(value, str):
        _invalid("invalid_response", 502)
    value = _redact(value, secrets)
    truncated = len(value) > _STREAM_LIMIT
    value = _ANSI.sub("", value[:_STREAM_LIMIT])
    # Text-only output: strip C0/C1 controls, keep ordinary line/tab formatting.
    value = "".join(char for char in value if char in "\r\n\t" or ord(char) >= 32 and not 127 <= ord(char) < 160)
    return value, truncated


async def get_stack_logs(stack_id, *, services=None, tail=100, timestamps=False, search="", invert=False,
                         origin, key, secret, verify: ssl.SSLContext | bool = True, transport=None):
    if (type(tail) is not int or not 1 <= tail <= 5000 or type(timestamps) is not bool or type(invert) is not bool
            or services is not None and not isinstance(services, list)):
        _invalid("invalid_stack_log_query")
    _text(search, 2048)
    selected = _strings(services or [])
    if len(selected) > 100 or any(_SERVICE.fullmatch(service) is None for service in selected):
        _invalid("invalid_stack_log_query")
    selected = list(dict.fromkeys(selected))
    snapshot = await _snapshot(stack_id, origin=origin, key=key, secret=secret, verify=verify, transport=transport)
    if not snapshot["can_logs"]:
        _invalid("stack_logs_forbidden", 403)
    allowed = await _services(snapshot)
    if any(service not in allowed for service in selected):
        _invalid("stack_service_unavailable", 409)
    if snapshot["swarm_id"]:
        selected = selected or allowed[:1]
        if len(selected) != 1:
            _invalid("stack_single_service_required", 422)
    params = {"stack": snapshot["stack_id"], "services": selected, "timestamps": timestamps}
    if search:
        variant = "SearchStackLog"
        params.update(terms=[search], combinator="And", invert=invert)
    else:
        variant = "GetStackLog"
        params["tail"] = tail
    raw = await _read(variant, params, **snapshot["_options"])
    if not isinstance(raw, dict) or type(raw.get("success")) is not bool:
        _invalid("invalid_response", 502)
    stdout, clipped_out = _log_text(raw.get("stdout"), snapshot["_secrets"])
    stderr, clipped_err = _log_text(raw.get("stderr"), snapshot["_secrets"])
    timestamp = lambda value: value if type(value) is int and 0 <= value <= 8_640_000_000_000_000 else None
    return {"stack_id": snapshot["stack_id"], "server_id": snapshot["server_id"], "stdout": stdout, "stderr": stderr, "success": raw["success"],
            "start_ts": timestamp(raw.get("start_ts")), "end_ts": timestamp(raw.get("end_ts")),
            "services": _redact(selected, snapshot["_secrets"]), "tail": 5000 if search else tail, "timestamps": timestamps,
            # Both Compose and Swarm search implementations grep the last
            # 5000 lines, independently of the ordinary log tail selector.
            "search_scope": "last_5000" if search else "tail", "truncated": clipped_out or clipped_err, "updated_at": _now()}
