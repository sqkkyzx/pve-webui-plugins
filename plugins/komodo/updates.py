"""Read-only operation history and stage summaries without command output."""

import asyncio
from datetime import datetime, timezone
import re
import ssl

import httpx

from plugins.komodo.actions import KomodoActionError, _json, _object_id, _preflight_status
from plugins.komodo.alerts import RESOURCE_TYPES, _timestamp, _version
from plugins.komodo.containers import _identity
from plugins.komodo.dashboard import _text


# Operation in client/core/rs/src/entities/mod.rs (also the v2.3.3 schema).
OPERATIONS = (
    "None",
    "CreateSwarm", "UpdateSwarm", "RenameSwarm", "DeleteSwarm", "RemoveSwarmNodes", "UpdateSwarmNode",
    "RemoveSwarmStacks", "RemoveSwarmServices", "CreateSwarmConfig", "RotateSwarmConfig", "RemoveSwarmConfigs",
    "CreateSwarmSecret", "RotateSwarmSecret", "RemoveSwarmSecrets",
    "CreateServer", "UpdateServer", "UpdateServerKey", "DeleteServer", "RenameServer",
    "StartContainer", "RestartContainer", "PauseContainer", "UnpauseContainer", "StopContainer", "DestroyContainer",
    "StartAllContainers", "RestartAllContainers", "PauseAllContainers", "UnpauseAllContainers", "StopAllContainers",
    "PruneContainers", "CreateNetwork", "DeleteNetwork", "PruneNetworks", "DeleteImage", "PruneImages",
    "DeleteVolume", "PruneVolumes", "PruneDockerBuilders", "PruneBuildx", "PruneSystem",
    "CreateStack", "UpdateStack", "RenameStack", "DeleteStack", "WriteStackContents", "RefreshStackCache",
    "PullStack", "DeployStack", "StartStack", "RestartStack", "PauseStack", "UnpauseStack", "StopStack",
    "DestroyStack", "RunStackService", "CheckStackForUpdate",
    "DeployStackService", "PullStackService", "StartStackService", "RestartStackService", "PauseStackService",
    "UnpauseStackService", "StopStackService", "DestroyStackService",
    "CreateDeployment", "UpdateDeployment", "RenameDeployment", "DeleteDeployment", "Deploy", "PullDeployment",
    "StartDeployment", "RestartDeployment", "PauseDeployment", "UnpauseDeployment", "StopDeployment",
    "DestroyDeployment", "CheckDeploymentForUpdate",
    "CreateBuild", "UpdateBuild", "RenameBuild", "DeleteBuild", "RunBuild", "CancelBuild", "WriteDockerfile",
    "CreateRepo", "UpdateRepo", "RenameRepo", "DeleteRepo", "CloneRepo", "PullRepo", "BuildRepo", "CancelRepoBuild",
    "CreateProcedure", "UpdateProcedure", "RenameProcedure", "DeleteProcedure", "RunProcedure", "CancelProcedure",
    "CreateAction", "UpdateAction", "RenameAction", "DeleteAction", "RunAction", "CancelAction",
    "CreateResourceSync", "UpdateResourceSync", "RenameResourceSync", "DeleteResourceSync", "WriteSyncContents",
    "CommitSync", "RunSync", "CreateBuilder", "UpdateBuilder", "RenameBuilder", "DeleteBuilder",
    "CreateAlerter", "UpdateAlerter", "RenameAlerter", "DeleteAlerter", "TestAlerter", "SendAlert",
    "ClearRepoCache", "BackupCoreDatabase", "GlobalAutoUpdate", "RotateAllServerKeys", "RotateCoreKeys",
    "CreateVariable", "UpdateVariableValue", "DeleteVariable",
    "CreateGitProviderAccount", "UpdateGitProviderAccount", "DeleteGitProviderAccount",
    "CreateDockerRegistryAccount", "UpdateDockerRegistryAccount", "DeleteDockerRegistryAccount",
)
_RESOURCE_LISTS = {
    "Swarm": "ListSwarms", "Server": "ListServers", "Stack": "ListStacks", "Deployment": "ListDeployments",
    "Build": "ListBuilds", "Repo": "ListRepos", "Procedure": "ListProcedures", "Action": "ListActions",
    "Builder": "ListBuilders", "Alerter": "ListAlerters", "ResourceSync": "ListResourceSyncs",
}
_SERVICE_OPERATORS = (
    "System", "Procedure", "Action", "Git Webhook", "Auto Redeploy", "Resource Sync",
    "Stack Wizard", "Build Manager", "Repo Manager",
)
_REQUEST_SECONDS = 10.0
_ENRICH_SECONDS = 8.0
_MAX_PAGE = 1_000_000
_HASH = re.compile(r"[0-9a-fA-F]{7,64}")


def _filters(page, resource_type, operation) -> dict | None:
    if (
        not isinstance(page, int) or isinstance(page, bool) or not 0 <= page <= _MAX_PAGE
        or not isinstance(resource_type, str) or resource_type not in ("", *RESOURCE_TYPES)
        or not isinstance(operation, str) or operation not in ("", *OPERATIONS)
    ):
        raise KomodoActionError("invalid_request", 400)
    result = {}
    if resource_type:
        result["target.type"] = resource_type
    if operation:
        result["operation"] = operation
    return result or None


def _target(item: dict) -> tuple[str, str | None]:
    target = item.get("target")
    target = target if isinstance(target, dict) else {}
    resource_type = target.get("type")
    resource_type = resource_type if isinstance(resource_type, str) and resource_type in RESOURCE_TYPES else "unknown"
    identifier = target.get("id")
    if identifier == "" or resource_type == "System" and identifier == "system":
        return resource_type, identifier
    return resource_type, _object_id(identifier)


def _service_name(operator) -> str | None:
    if not isinstance(operator, str):
        return None
    if operator in (*_SERVICE_OPERATORS, "Github"):
        return operator
    for index, name in enumerate(_SERVICE_OPERATORS):
        if operator == f"{index:024x}":
            return name
    return None


def _row(item, *, detail: bool = False) -> dict:
    if not isinstance(item, dict):
        raise KomodoActionError("invalid_response")
    status = item.get("status")
    status = status if isinstance(status, str) and status in {"Queued", "InProgress", "Complete"} else "unknown"
    operation = item.get("operation")
    resource_type, resource_id = _target(item)
    result = {
        "id": _object_id(item.get("_id")) or _object_id(item.get("id")),
        "operation": operation if isinstance(operation, str) and operation in OPERATIONS else "unknown",
        "start_ts": _timestamp(item.get("start_ts")), "status": status,
        # The upstream initializes success=false before the operation finishes.
        # Pending or unknown status therefore has no definitive success value.
        "success": item.get("success") if status == "Complete" and isinstance(item.get("success"), bool) else None,
        "username": _service_name(item.get("operator")) or (_text(item.get("username")) if not detail else None),
        "resource_type": resource_type, "resource_id": resource_id,
        "resource_name": "系统" if resource_type == "System" else None,
    }
    if detail:
        operator = item.get("operator")
        result["operator_id"] = _object_id(operator) if result["username"] is None else None
    return result


async def _read(client: httpx.AsyncClient, origin: str, headers: dict, variant: str, params: dict):
    client.cookies.clear()
    async with asyncio.timeout(_REQUEST_SECONDS):
        response = await client.post(origin + "/read", headers=headers, json={"type": variant, "params": params})
    if response.status_code == 404:
        raise KomodoActionError("resource_not_found", 404)
    _preflight_status(response)
    return _json(response)


async def _name_lookup(resource_type: str, identifiers: list[str], *, origin: str, headers: dict,
                       verify: ssl.SSLContext | bool, transport: httpx.AsyncBaseTransport | None) -> tuple[dict, str | None]:
    # Each concurrent lookup has its own cookie jar; there is no race between
    # clearing one shared client and another response setting cookies.
    try:
        async with httpx.AsyncClient(
            verify=verify, transport=transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(_ENRICH_SECONDS, connect=5.0),
        ) as client:
            response = await client.post(origin + "/read", headers=headers, json={
                "type": _RESOURCE_LISTS[resource_type],
                # ResourceQuery.names splits valid ObjectIds into an _id $in
                # filter in Komodo, so unrelated resource names are not fetched.
                "params": {"query": {"names": identifiers, "templates": "Include"}, "limit": 0},
            })
    except (httpx.HTTPError, TimeoutError):
        return {}, "resource_names_unavailable"
    if response.status_code in {401, 403}:
        return {}, "resource_names_permission_denied"
    if response.status_code != 200:
        return {}, "resource_names_unavailable"
    payload = _json(response)
    if not isinstance(payload, list):
        return {}, "resource_names_invalid_response"
    names = {}
    invalid = False
    for item in payload:
        if not isinstance(item, dict):
            invalid = True
            continue
        identifier, name = _object_id(item.get("id")), _text(item.get("name"))
        if identifier not in identifiers:
            continue
        if name is None or (resource_type, identifier) in names or item.get("type", resource_type) != resource_type:
            invalid = True
            continue
        names[(resource_type, identifier)] = name
    return names, "resource_names_invalid_response" if invalid else None


async def _enrich(rows: list[dict], *, origin: str, headers: dict, verify: ssl.SSLContext | bool,
                  transport: httpx.AsyncBaseTransport | None) -> list[str]:
    groups = {}
    for row in rows:
        resource_type, identifier = row["resource_type"], row["resource_id"]
        if resource_type in _RESOURCE_LISTS and _object_id(identifier) is not None:
            groups.setdefault(resource_type, [])
            if identifier not in groups[resource_type]:
                groups[resource_type].append(identifier)
    if not groups:
        return []
    tasks = [asyncio.create_task(_name_lookup(resource_type, identifiers, origin=origin, headers=headers,
                                            verify=verify, transport=transport))
             for resource_type, identifiers in groups.items()]
    try:
        done, pending = await asyncio.wait(tasks, timeout=_ENRICH_SECONDS)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    names, errors = {}, set()
    if pending:
        errors.add("resource_names_unavailable")
    for task in done:
        lookup, error = task.result()
        names.update(lookup)
        if error:
            errors.add(error)
    for row in rows:
        if row["resource_type"] != "System":
            row["resource_name"] = names.get((row["resource_type"], row["resource_id"]))
    return sorted(errors)


def _stages(item: dict) -> tuple[list, bool]:
    logs = item.get("logs")
    if not isinstance(logs, list):
        return [], logs is not None
    stages = []
    omitted = len(logs) > 500
    for log in logs[:500]:
        if not isinstance(log, dict):
            omitted = True
            continue
        name = _text(log.get("stage"))
        stages.append({
            "stage": name,
            "success": log.get("success") if isinstance(log.get("success"), bool) else None,
            "start_ts": _timestamp(log.get("start_ts")), "end_ts": _timestamp(log.get("end_ts")),
        })
        if name is None and log.get("stage") is not None:
            omitted = True
        if any(field not in {"stage", "success", "start_ts", "end_ts"} and value not in (None, "", [], {}) for field, value in log.items()):
            omitted = True
    return stages, omitted


async def list_updates(*, page: int = 0, resource_type: str = "", operation: str = "",
                       origin: str | None, key: str | None, secret: str | None,
                       verify: ssl.SSLContext | bool, transport: httpx.AsyncBaseTransport | None) -> dict:
    query = _filters(page, resource_type, operation)
    if not origin or not key or not secret:
        raise KomodoActionError("not_configured", 503)
    headers = {"x-api-key": key, "x-api-secret": secret}
    try:
        async with httpx.AsyncClient(
            verify=verify, transport=transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0),
        ) as client:
            await _identity(client, origin, headers)
            payload = await _read(client, origin, headers, "ListUpdates", {"page": page, "query": query})
    except (httpx.HTTPError, TimeoutError):
        raise KomodoActionError("upstream_unavailable") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("updates"), list) or "next_page" not in payload:
        raise KomodoActionError("invalid_response")
    next_page = payload["next_page"]
    if next_page is not None and (not isinstance(next_page, int) or isinstance(next_page, bool) or next_page != page + 1):
        raise KomodoActionError("invalid_response")
    if next_page is not None and next_page > _MAX_PAGE:
        next_page = None
    if len(payload["updates"]) > 100:
        raise KomodoActionError("invalid_response")
    rows = [_row(item) for item in payload["updates"]]
    errors = await _enrich(rows, origin=origin, headers=headers, verify=verify, transport=transport)
    return {"updates": rows, "page": page, "next_page": next_page, "errors": errors,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}


async def update_details(identifier: str, *, origin: str | None, key: str | None, secret: str | None,
                         verify: ssl.SSLContext | bool, transport: httpx.AsyncBaseTransport | None) -> dict:
    if not isinstance(identifier, str) or _object_id(identifier) is None:
        raise KomodoActionError("invalid_request", 400)
    identifier = identifier.lower()
    if not origin or not key or not secret:
        raise KomodoActionError("not_configured", 503)
    headers = {"x-api-key": key, "x-api-secret": secret}
    try:
        async with httpx.AsyncClient(
            verify=verify, transport=transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(_REQUEST_SECONDS, connect=5.0),
        ) as client:
            await _identity(client, origin, headers)
            payload = await _read(client, origin, headers, "GetUpdate", {"id": identifier})
    except (httpx.HTTPError, TimeoutError):
        raise KomodoActionError("upstream_unavailable") from None
    if not isinstance(payload, dict) or _object_id(payload.get("_id")) != identifier:
        raise KomodoActionError("invalid_response")
    row = _row(payload, detail=True)
    stages, omitted = _stages(payload)
    raw_version = payload.get("version")
    version = _version(raw_version)
    raw_hash = payload.get("commit_hash")
    commit_hash = raw_hash if isinstance(raw_hash, str) and _HASH.fullmatch(raw_hash) else None
    if (raw_version is not None and version is None) or (raw_hash not in (None, "") and commit_hash is None):
        omitted = True
    # All other_data/config/toml fields are explicitly omitted, regardless of
    # their alleged format; they can contain credentials or entire deployments.
    allowed = {"_id", "operation", "start_ts", "end_ts", "status", "success", "operator", "target", "version", "commit_hash", "logs"}
    if any(field not in allowed and value not in (None, "", [], {}) for field, value in payload.items()):
        omitted = True
    row.update(end_ts=_timestamp(payload.get("end_ts")), version=version, commit_hash=commit_hash,
               stages=stages, details_omitted=omitted)
    errors = await _enrich([row], origin=origin, headers=headers, verify=verify, transport=transport)
    return {"update": row, "errors": errors, "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
