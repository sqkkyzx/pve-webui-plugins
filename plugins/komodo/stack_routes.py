"""Authenticated stack detail reads and deduplicated configuration/file writes."""
import asyncio
import hashlib
import logging
import re
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, UUID4, ValidationError, field_validator

from plugins.komodo.action_store import ActionStoreError
from plugins.komodo.actions import KomodoActionError
from plugins.komodo.komodo import KomodoDashboardError
from plugins.komodo.stack_details import get_stack_details, get_stack_logs, update_stack_details

LOGGER = logging.getLogger('pve_komodo')


class StackCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['config', 'file']
    base_version: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')
    server_id: StrictStr = Field(pattern=r'^(?:[0-9a-fA-F]{24})?$')
    request_id: UUID4
    config: dict | None = None
    file_path: StrictStr | None = None
    contents: StrictStr | None = None

    @field_validator('server_id')
    @classmethod
    def normalized_server(cls, value):
        return value.lower()


class StackActionCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['deploy', 'pull', 'restart', 'pause', 'stop', 'destroy']
    base_version: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')
    server_id: StrictStr = Field(pattern=r'^(?:[0-9a-fA-F]{24})?$')
    request_id: UUID4

    @field_validator('server_id')
    @classmethod
    def normalized_server(cls, value):
        return value.lower()


def register_stack_routes(application, config, authenticator, komodo, csrf, action_store, action_tasks):
    def credentials():
        return dict(origin=komodo._origin, key=komodo._key, secret=komodo._secret,
                    verify=komodo._verify, transport=komodo._transport)

    async def authorize(request, stack_id, *, write=False, logs=False):
        user = await authenticator.authenticate(request)
        if re.fullmatch(r'[0-9a-fA-F]{24}', stack_id) is None:
            raise HTTPException(422, 'invalid_stack_id')
        allowed = {'services', 'tail', 'timestamps', 'search', 'invert'} if logs else set()
        if any(key not in allowed or key != 'services' and len(request.query_params.getlist(key)) != 1
               for key in request.query_params):
            raise HTTPException(422, 'invalid_stack_query')
        if write:
            headers = request.headers.getlist('x-pve-komodo-csrf')
            csrf.verify(request, user, headers[0] if len(headers) == 1 else '')
        return user

    @application.get('/pve-komodo/api/stacks/{stack_id}')
    async def stack_details(request: Request, stack_id: str):
        try:
            async with asyncio.timeout(45):
                user = await authorize(request, stack_id)
                data = await get_stack_details(stack_id.lower(), **credentials())
            return {**data, 'csrf_token': csrf.issue(request, user)}
        except (KomodoActionError, KomodoDashboardError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, 'stack_details_timeout') from None

    @application.get('/pve-komodo/api/stacks/{stack_id}/logs')
    async def stack_logs(request: Request, stack_id: str):
        try:
            async with asyncio.timeout(45):
                await authorize(request, stack_id, logs=True)
                query = request.query_params
                if query.get('timestamps', 'false') not in {'true', 'false'} or query.get('invert', 'false') not in {'true', 'false'}:
                    raise HTTPException(422, 'invalid_stack_logs_query')
                tail = query.get('tail', '100')
                if re.fullmatch(r'[1-9][0-9]{0,3}', tail) is None or int(tail) > 5000:
                    raise HTTPException(422, 'invalid_stack_logs_query')
                services = query.getlist('services')
                if len(services) > 100 or len(query.get('search', '')) > 1000:
                    raise HTTPException(422, 'invalid_stack_logs_query')
                return await get_stack_logs(stack_id.lower(), services=services, tail=int(tail),
                    timestamps=query.get('timestamps', 'false') == 'true', search=query.get('search', ''),
                    invert=query.get('invert', 'false') == 'true', **credentials())
        except (KomodoActionError, KomodoDashboardError) as error:
            raise HTTPException(error.status_code, error.code) from None
        except TimeoutError:
            raise HTTPException(504, 'stack_logs_timeout') from None

    @application.post('/pve-komodo/api/stacks/{stack_id}')
    async def save_stack(request: Request, stack_id: str):
        try:
            async with asyncio.timeout(20):
                user = await authorize(request, stack_id, write=True)
        except TimeoutError:
            raise HTTPException(503, 'pve_verification_timeout') from None
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise HTTPException(415, 'json_required')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1_048_576:
                raise HTTPException(413, 'stack_request_too_large')
        try:
            command = StackCommand.model_validate_json(bytes(body))
            supplied = command.model_fields_set
            if command.action == 'config':
                if not isinstance(command.config, dict) or not command.config or 'file_path' in supplied or 'contents' in supplied:
                    raise ValueError
            elif 'config' in supplied or command.file_path is None or command.contents is None:
                raise ValueError
        except (ValidationError, ValueError):
            raise HTTPException(422, 'invalid_stack_request') from None
        if action_store is None:
            raise HTTPException(503, 'action_state_not_configured')
        payload = {'kind': 'stack_details', 'stack_id': stack_id.lower(),
                   **command.model_dump(mode='json', exclude={'request_id'})}
        scope = hashlib.sha256((user + '\0' + config.upstream + '\0' + (config.komodo_url or '')).encode()).hexdigest()
        request_id = str(command.request_id)
        try:
            cached = action_store.reserve(scope, request_id, payload)
        except ActionStoreError as error:
            raise HTTPException(error.status_code, error.code) from None
        if cached is not None:
            return JSONResponse(cached[1], status_code=cached[0])

        async def guard():
            async with asyncio.timeout(20):
                await authenticator.authenticate(request)

        async def dispatch():
            try:
                async with asyncio.timeout(80):
                    result = await update_stack_details(stack_id.lower(), action=command.action,
                        base_version=command.base_version, expected_server_id=command.server_id,
                        config=command.config, file_path=command.file_path, contents=command.contents,
                        before_write=guard, **credentials())
                status = 200
            except (KomodoActionError, KomodoDashboardError) as error:
                status, result = error.status_code, {'detail': error.code}
            except HTTPException as error:
                status, result = error.status_code, {'detail': error.detail}
            except Exception:
                status, result = 200, {'ok': False, 'status': 'outcome_unknown',
                                      'operation_id': None, 'error_code': 'outcome_unknown'}
            try:
                action_store.finish(scope, request_id, status, result)
            except ActionStoreError:
                LOGGER.warning('Stack save receipt unavailable user=%s request_id=%s', user, request_id)
            LOGGER.info('Stack save user=%s request_id=%s stack=%s action=%s', user, request_id, stack_id.lower(), command.action)
            return status, result

        task = asyncio.create_task(dispatch())
        action_tasks.add(task)
        task.add_done_callback(action_tasks.discard)
        status, result = await asyncio.shield(task)
        return JSONResponse(result, status_code=status)

    @application.post('/pve-komodo/api/stacks/{stack_id}/actions')
    async def stack_action(request: Request, stack_id: str):
        try:
            async with asyncio.timeout(20):
                user = await authorize(request, stack_id, write=True)
        except TimeoutError:
            raise HTTPException(503, 'pve_verification_timeout') from None
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise HTTPException(415, 'json_required')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                raise HTTPException(413, 'stack_request_too_large')
        try:
            command = StackActionCommand.model_validate_json(bytes(body))
        except ValidationError:
            raise HTTPException(422, 'invalid_stack_action') from None
        if action_store is None:
            raise HTTPException(503, 'action_state_not_configured')
        payload = {'kind': 'stack_detail_action', 'stack_id': stack_id.lower(),
                   **command.model_dump(mode='json', exclude={'request_id'})}
        scope = hashlib.sha256((user + '\0' + config.upstream + '\0' + (config.komodo_url or '')).encode()).hexdigest()
        request_id = str(command.request_id)
        try:
            cached = action_store.reserve(scope, request_id, payload)
        except ActionStoreError as error:
            raise HTTPException(error.status_code, error.code) from None
        if cached is not None:
            return JSONResponse(cached[1], status_code=cached[0])

        async def guard():
            async with asyncio.timeout(45):
                await authenticator.authenticate(request)
                current = await get_stack_details(stack_id.lower(), **credentials())
                if current.get('server_id') != command.server_id or current.get('version') != command.base_version:
                    raise KomodoActionError('stack_version_conflict', 409)
                if current.get('can_execute') is not True:
                    raise KomodoActionError('permission_denied', 403)

        async def dispatch():
            try:
                async with asyncio.timeout(80):
                    result = await komodo.perform_action('stacks', command.action, [stack_id.lower()], before_write=guard)
                status = 200
            except (KomodoActionError, KomodoDashboardError) as error:
                status, result = error.status_code, {'detail': error.code}
            except HTTPException as error:
                status, result = error.status_code, {'detail': error.detail}
            except Exception:
                status, result = 200, {'section': 'stacks', 'action': command.action,
                    'results': [{'id': stack_id.lower(), 'ok': False, 'status': 'outcome_unknown',
                                 'operation_id': None, 'error_code': 'outcome_unknown'}]}
            try:
                action_store.finish(scope, request_id, status, result)
            except ActionStoreError:
                LOGGER.warning('Stack action receipt unavailable user=%s request_id=%s', user, request_id)
            LOGGER.info('Stack action user=%s request_id=%s stack=%s action=%s', user, request_id, stack_id.lower(), command.action)
            return status, result

        task = asyncio.create_task(dispatch())
        action_tasks.add(task)
        task.add_done_callback(action_tasks.discard)
        status, result = await asyncio.shield(task)
        return JSONResponse(result, status_code=status)
