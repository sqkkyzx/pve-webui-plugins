"""Read-only, server-side Komodo connectivity checks.

The caller must authenticate and authorize the PVE user before invoking this
client. Komodo API credentials never form part of the returned browser data.
"""

import re
import ssl
from urllib.parse import urlsplit

import certifi
import httpx

from plugins.komodo.dashboard import assemble_dashboard, stack_auto_updates
from plugins.komodo.actions import KomodoActionError, perform_action


VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")


class KomodoDashboardError(Exception):
    """A fixed, non-sensitive failure which the authenticated API may expose."""

    def __init__(self, code: str, status_code: int = 502) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _origin(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
            or "\\" in value
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise ValueError
        _ = parsed.port
        return str(httpx.URL(value)).rstrip("/")
    except (ValueError, httpx.InvalidURL):
        # Do not include the supplied URL: it might contain credentials.
        raise ValueError("KOMODO_URL must be a fixed HTTP(S) origin") from None


def _credential(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    if any(ord(character) < 32 or ord(character) >= 127 for character in value):
        raise ValueError("Komodo API credentials must contain valid ASCII header characters")
    return value


class KomodoClient:
    def __init__(
        self,
        url: str | None,
        key: str | None,
        secret: str | None,
        *,
        verify: ssl.SSLContext | bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._origin = _origin(url)
        self._key = _credential(key)
        self._secret = _credential(secret)
        # All upstream clients ignore environment TLS overrides. Match httpx's
        # verified default while loading the CA bundle once for this service.
        self._verify = (
            ssl.create_default_context(cafile=certifi.where())
            if verify is True and self.configured else verify
        )
        self._transport = transport

    def __repr__(self) -> str:
        return f"KomodoClient(configured={self.configured})"

    @property
    def configured(self) -> bool:
        return bool(self._origin and self._key and self._secret)

    @staticmethod
    def _failure(code: str) -> dict:
        return {"configured": True, "connected": False, "error_code": code}

    async def link_servers(self) -> list[dict]:
        """Minimal server identity list for local PVE associations."""
        from plugins.komodo.containers import _identity, _read
        from plugins.komodo.dashboard import _text

        if not self.configured:
            raise KomodoDashboardError("not_configured", 503)
        try:
            async with httpx.AsyncClient(
                verify=self._verify, transport=self._transport, trust_env=False,
                follow_redirects=False, timeout=httpx.Timeout(10.0, connect=5.0),
            ) as client:
                headers = {"x-api-key": self._key, "x-api-secret": self._secret}
                await _identity(client, self._origin, headers)
                response = await _read(client, self._origin, headers, "ListServers", {
                    "query": {"templates": "Include"}, "limit": 0,
                })
            if response.status_code in {401, 403}:
                raise KomodoDashboardError("invalid_credentials" if response.status_code == 401 else "servers_permission_denied")
            if response.status_code != 200:
                raise KomodoDashboardError("servers_upstream_unavailable")
            rows = response.json()
            if not isinstance(rows, list):
                raise KomodoDashboardError("servers_invalid_response")
            result, seen = [], set()
            for row in rows:
                identifier = row.get("id") if isinstance(row, dict) else None
                name = _text(row.get("name")) if isinstance(row, dict) else None
                if (not isinstance(identifier, str) or re.fullmatch(r"[0-9a-fA-F]{24}", identifier) is None
                        or name is None or not isinstance(row.get("template"), bool) or identifier.lower() in seen):
                    raise KomodoDashboardError("servers_invalid_response")
                seen.add(identifier.lower())
                result.append({"id": identifier.lower(), "name": name, "template": row["template"]})
            return result
        except KomodoActionError as error:
            raise KomodoDashboardError(error.code, error.status_code) from None
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise KomodoDashboardError("servers_upstream_unavailable") from None

    async def perform_action(self, section: str, action: str, ids: list[str], *, before_write=None) -> dict:
        if section == "containers":
            if before_write is not None:
                raise KomodoActionError("invalid_request", 400)
            from plugins.komodo.containers import perform_container_action

            return await perform_container_action(
                action, ids, origin=self._origin, key=self._key, secret=self._secret,
                verify=self._verify, transport=self._transport,
            )
        return await perform_action(
            section, action, ids, origin=self._origin, key=self._key, secret=self._secret,
            verify=self._verify, transport=self._transport, before_write=before_write,
        )

    async def server_summary(self, server_id: str) -> dict:
        from plugins.komodo.server_summary import get_server_summary

        return await get_server_summary(server_id, origin=self._origin, key=self._key, secret=self._secret,
                                        verify=self._verify, transport=self._transport)

    async def update_server_description(self, server_id: str, *, description: str, base_version: str, before_write=None) -> dict:
        from plugins.komodo.server_summary import update_server_description
        return await update_server_description(server_id, description=description, base_version=base_version,
            before_write=before_write, origin=self._origin, key=self._key, secret=self._secret,
            verify=self._verify, transport=self._transport)

    async def server_history(self, server_id: str, *, interval: str = "1-day") -> dict:
        from plugins.komodo.server_summary import get_server_history

        return await get_server_history(server_id, interval=interval, origin=self._origin, key=self._key,
                                        secret=self._secret, verify=self._verify, transport=self._transport)

    async def server_summary_capabilities(self, server_id: str) -> dict:
        from plugins.komodo.server_summary import get_server_summary_capabilities

        return await get_server_summary_capabilities(server_id, origin=self._origin, key=self._key,
                                                     secret=self._secret, verify=self._verify, transport=self._transport)

    async def containers(self) -> dict:
        from plugins.komodo.containers import list_containers

        try:
            return await list_containers(
                origin=self._origin, key=self._key, secret=self._secret,
                verify=self._verify, transport=self._transport,
            )
        except KomodoActionError as error:
            raise KomodoDashboardError(error.code, error.status_code) from None

    async def alerts(self, *, page: int = 0, resource_type: str = "", alert_type: str = "",
                     unresolved_only: bool = False) -> dict:
        from plugins.komodo.alerts import list_alerts

        try:
            return await list_alerts(
                page=page, resource_type=resource_type, alert_type=alert_type, unresolved_only=unresolved_only,
                origin=self._origin, key=self._key, secret=self._secret,
                verify=self._verify, transport=self._transport,
            )
        except KomodoActionError as error:
            raise KomodoDashboardError(error.code, error.status_code) from None

    async def updates(self, *, page: int = 0, resource_type: str = "", operation: str = "") -> dict:
        from plugins.komodo.updates import list_updates

        try:
            return await list_updates(
                page=page, resource_type=resource_type, operation=operation,
                origin=self._origin, key=self._key, secret=self._secret,
                verify=self._verify, transport=self._transport,
            )
        except KomodoActionError as error:
            raise KomodoDashboardError(error.code, error.status_code) from None

    async def update_details(self, identifier: str) -> dict:
        from plugins.komodo.updates import update_details

        try:
            return await update_details(
                identifier, origin=self._origin, key=self._key, secret=self._secret,
                verify=self._verify, transport=self._transport,
            )
        except KomodoActionError as error:
            raise KomodoDashboardError(error.code, error.status_code) from None

    async def server_options(self, server_id: str) -> dict:
        from plugins.komodo.server_options import get_server_options

        return await get_server_options(server_id, origin=self._origin, key=self._key, secret=self._secret,
                                        verify=self._verify, transport=self._transport)

    async def update_server_options(self, server_id: str, *, action: str, base_version: str,
                                    config=None, public_key=None, before_write=None) -> dict:
        from plugins.komodo.server_options import update_server_options

        return await update_server_options(server_id, action=action, base_version=base_version,
                                           config=config, public_key=public_key, before_write=before_write,
                                           origin=self._origin, key=self._key, secret=self._secret,
                                           verify=self._verify, transport=self._transport)

    async def connection_status(self) -> dict:
        if not self.configured:
            return {"configured": False, "connected": False}

        # Per-check clients isolate both PVE sessions and other callers. Ignore
        # environment proxies and never follow redirects carrying API keys.
        try:
            async with httpx.AsyncClient(
                verify=self._verify,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(10.0, connect=5.0),
            ) as client:
                user_response = await client.get(
                    self._origin + "/user",
                    headers={"x-api-key": self._key, "x-api-secret": self._secret},
                )
                if user_response.status_code in {401, 403}:
                    return self._failure("invalid_credentials")
                if user_response.status_code != 200:
                    return self._failure("upstream_unavailable")
                try:
                    user = user_response.json()
                except ValueError:
                    return self._failure("invalid_response")
                if not isinstance(user, dict):
                    return self._failure("invalid_response")
                if user.get("enabled") is False:
                    return self._failure("invalid_credentials")
                username = user.get("username")
                if (
                    user.get("enabled") is not True
                    or not isinstance(username, str)
                    or not username.strip()
                    or len(username) > 256
                    or any(ord(character) < 32 or ord(character) == 127 for character in username)
                ):
                    return self._failure("invalid_response")

                # /version is public. Neither API credentials nor cookies from
                # the preceding response need to be sent to it.
                client.cookies.clear()
                version_response = await client.get(self._origin + "/version")
                if version_response.status_code != 200:
                    return self._failure("upstream_unavailable")
                version = version_response.text.strip()
                if len(version) > 128 or VERSION.fullmatch(version) is None:
                    return self._failure("invalid_response")
        except httpx.HTTPError:
            # Responses and exceptions can echo headers or embed sensitive URLs.
            return self._failure("upstream_unavailable")

        return {
            "configured": True,
            "connected": True,
            "username": username,
            "version": version,
        }

    async def dashboard(self) -> dict:
        """Fetch only fixed read APIs after validating the service account.

        These POST requests resolve Komodo's read-only RPC variants. This is
        deliberately not a generic RPC method or a browser-supplied request.
        """
        if not self.configured:
            raise KomodoDashboardError("not_configured", 503)
        try:
            async with httpx.AsyncClient(
                verify=self._verify,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(10.0, connect=5.0),
            ) as client:
                headers = {"x-api-key": self._key, "x-api-secret": self._secret}
                response = await client.get(self._origin + "/user", headers=headers)
                if response.status_code in {401, 403}:
                    raise KomodoDashboardError("invalid_credentials")
                if response.status_code != 200:
                    raise KomodoDashboardError("upstream_unavailable")
                try:
                    user = response.json()
                except ValueError:
                    raise KomodoDashboardError("invalid_response") from None
                if not isinstance(user, dict):
                    raise KomodoDashboardError("invalid_response")
                if user.get("enabled") is False:
                    raise KomodoDashboardError("invalid_credentials")
                if user.get("enabled") is not True or not isinstance(user.get("username"), str) or not user["username"].strip():
                    raise KomodoDashboardError("invalid_response")

                resources = {}
                errors = []
                stack_settings = {}
                # Include template metadata so the browser can default to
                # excluding templates while offering the original UI's filter.
                requests = (
                    ("servers", "ListServers", {"query": {"templates": "Include"}, "limit": 0}),
                    ("stacks", "ListStacks", {"query": {"templates": "Include"}, "limit": 0}),
                    ("procedures", "ListProcedures", {"query": {"templates": "Include"}, "limit": 0}),
                    ("tags", "ListTags", {}),
                    ("stack_settings", "ListFullStacks", {"query": {"templates": "Include"}, "limit": 0}),
                    ("containers", "ListAllContainers", {"limit": 0}),
                    ("deployments", "ListDeployments", {"query": {"templates": "Include"}, "limit": 0}),
                    ("repos", "ListRepos", {"query": {"templates": "Include"}, "limit": 0}),
                )
                for section, variant, params in requests:
                    resources[section] = []
                    # Never replay Set-Cookie from /user or a preceding read.
                    client.cookies.clear()
                    try:
                        response = await client.post(
                            self._origin + "/read", headers=headers,
                            json={"type": variant, "params": params},
                        )
                    except httpx.HTTPError:
                        errors.append(section + "_upstream_unavailable")
                        continue
                    if response.status_code == 401:
                        # The service account may have been revoked between
                        # identity verification and fetching the resources.
                        raise KomodoDashboardError("invalid_credentials")
                    if response.status_code == 403:
                        errors.append(section + "_permission_denied")
                        continue
                    if response.status_code != 200:
                        errors.append(section + "_upstream_unavailable")
                        continue
                    try:
                        result = response.json()
                    except ValueError:
                        errors.append(section + "_invalid_response")
                        continue
                    if not isinstance(result, list):
                        errors.append(section + "_invalid_response")
                        continue
                    if section == "stack_settings":
                        # Full configs can contain secrets. Retain only the
                        # exact boolean by ID; never put configs in the DTO.
                        stack_settings = stack_auto_updates(result, errors)
                    else:
                        resources[section] = result
                return assemble_dashboard(resources, errors, stack_settings=stack_settings)
        except httpx.HTTPError:
            raise KomodoDashboardError("upstream_unavailable") from None
