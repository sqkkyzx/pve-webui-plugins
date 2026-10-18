import asyncio
import json
from urllib.parse import parse_qs, quote

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app


TICKET = "PVE:root@pam:1234ABCD::signature+value="
COOKIE = "PVEAuthCookie=" + quote(TICKET, safe="")


def client_for(*, pve=None, komodo=None, configured=True, allowed=("root@pam",)):
    def no_call(request):
        pytest.fail("An unauthorized or unconfigured request contacted an upstream")

    return TestClient(create_app(
        Settings(
            "https://pve.example:8006", admin_users=allowed,
            komodo_url="http://komodo.example:9120" if configured else None,
            komodo_api_key="test-key" if configured else None,
            komodo_api_secret="test-secret" if configured else None,
            action_state_file=":memory:",
        ),
        transport=httpx.MockTransport(no_call),
        pve_auth_transport=httpx.MockTransport(pve or no_call),
        komodo_transport=httpx.MockTransport(komodo or no_call),
    ))


def pve_success(request):
    assert request.url.host == "pve.example"
    assert request.headers["cookie"] == COOKIE
    assert "x-api-key" not in request.headers and "x-api-secret" not in request.headers
    if request.method == "GET":
        assert request.url.path == "/api2/json/access/permissions"
        assert not request.url.query
        return httpx.Response(200, json={"data": {"/": {"Sys.Audit": 1}}})
    assert request.method == "POST"
    assert request.url.path == "/api2/json/access/ticket"
    assert parse_qs(request.content.decode()) == {
        "username": ["root@pam"], "password": [TICKET], "path": ["/"], "privs": ["Sys.Audit"],
    }
    return httpx.Response(200, json={"data": {"username": "root@pam"}})


@pytest.mark.parametrize("route", ["session", "connection", "dashboard", "containers", "alerts", "updates", "updates/" + "a" * 24])
def test_anonymous_request_is_denied_before_either_upstream(route):
    response = client_for().get("/pve-komodo/api/" + route)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"
    assert "test-secret" not in response.text


def test_unlisted_user_denied_without_using_service_credentials():
    response = client_for(allowed=()).get("/pve-komodo/api/connection", headers={"cookie": COOKIE})
    assert response.status_code == 403
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("code,expected", [(401, 401), (403, 403), (500, 503), (302, 503)])
def test_failed_pve_verification_never_calls_komodo(code, expected):
    response = client_for(pve=lambda request: httpx.Response(code, text="sensitive-upstream-body")).get(
        "/pve-komodo/api/connection", headers={"cookie": COOKIE},
    )
    assert response.status_code == expected
    assert "sensitive-upstream-body" not in response.text
    assert "no-store" in response.headers["cache-control"]


def test_session_returns_verified_identity_without_connecting_komodo():
    response = client_for(pve=pve_success).get("/pve-komodo/api/session", headers={"cookie": COOKIE})
    assert response.status_code == 200
    assert response.json() == {"user": "root@pam", "komodo_configured": True}


def test_unconfigured_status_is_available_only_after_pve_verification():
    response = client_for(pve=pve_success, configured=False).get(
        "/pve-komodo/api/connection", headers={"cookie": COOKIE},
    )
    assert response.json() == {
        "user": "root@pam", "komodo": {"configured": False, "connected": False},
    }


def test_successful_connection_keeps_credentials_and_sessions_on_their_own_origins(caplog):
    def komodo(request):
        assert request.url.host == "komodo.example"
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        if request.url.path == "/user":
            assert request.headers["x-api-key"] == "test-key"
            assert request.headers["x-api-secret"] == "test-secret"
            return httpx.Response(200, json={
                "username": "pve-integration", "enabled": True, "config": {"private": "hidden"},
            }, headers={"set-cookie": "should-not-be-forwarded=private"})
        assert request.url.path == "/version"
        assert "x-api-key" not in request.headers and "x-api-secret" not in request.headers
        return httpx.Response(200, text="2.3.3")

    response = client_for(pve=pve_success, komodo=komodo).get(
        "/pve-komodo/api/connection", headers={"cookie": COOKIE},
    )
    assert response.status_code == 200
    assert response.json() == {"user": "root@pam", "komodo": {
        "configured": True, "connected": True, "username": "pve-integration", "version": "2.3.3",
    }}
    for private in ("test-key", "test-secret", "signature+value", "hidden", "komodo.example"):
        assert private not in response.text
        assert private not in caplog.text
    assert "set-cookie" not in response.headers
    assert "no-store" in response.headers["cache-control"]


def test_invalid_komodo_credentials_return_safe_status_after_pve_verification():
    response = client_for(
        pve=pve_success, komodo=lambda request: httpx.Response(401, text="test-secret"),
    ).get("/pve-komodo/api/connection", headers={"cookie": COOKIE})
    assert response.status_code == 200
    assert response.json()["komodo"] == {
        "configured": True, "connected": False, "error_code": "invalid_credentials",
    }
    assert "test-secret" not in response.text


@pytest.mark.parametrize("route", ["session", "connection", "dashboard", "containers", "alerts", "updates", "updates/" + "a" * 24])
def test_no_write_endpoints(route):
    response = client_for().post("/pve-komodo/api/" + route)
    assert response.status_code == 405
    assert "no-store" in response.headers["cache-control"]


def test_settings_load_new_environment_without_showing_credentials(monkeypatch):
    for name, value in {
        "PVE_UPSTREAM": "https://pve.example:8006", "PVE_ADMIN_USERS": "root@pam, alice@pve",
        "KOMODO_URL": "http://core:9120", "KOMODO_API_KEY": "test-key",
        "KOMODO_API_SECRET": "test-secret", "KOMODO_TLS_VERIFY": "false",
    }.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.admin_users == ("root@pam", "alice@pve")
    assert settings.komodo_url == "http://core:9120"
    assert settings.komodo_api_key == "test-key"
    assert settings.komodo_api_secret == "test-secret"
    assert settings.komodo_tls_verify is False
    assert "test-key" not in repr(settings) and "test-secret" not in repr(settings)
    monkeypatch.setenv("PVE_ADMIN_USERS", "")
    assert Settings.from_env().admin_users == ()


def test_dashboard_is_protected_and_returns_only_allowlisted_resource_fields(caplog):
    seen = []

    def komodo(request):
        assert request.url.host == "komodo.example"
        assert "cookie" not in request.headers
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["x-api-secret"] == "test-secret"
        seen.append(request.url.path)
        if request.url.path == "/user":
            return httpx.Response(200, json={"username": "integration", "enabled": True})
        assert request.url.path == "/read" and request.method == "POST"
        kind = json.loads(request.content)["type"]
        resources = {
            "ListServers": [{"id": "a" * 24, "name": "Server one", "tags": [], "template": False,
                             "info": {"state": "Ok", "version": "2.3.3", "stats": None,
                                      "address": "private-upstream-address", "err": "private-server-error"}}],
            "ListStacks": [{"id": "b" * 24, "name": "Stack one", "tags": [], "template": False,
                            "info": {"state": "running", "files_on_host": True,
                                     "server_id": "a" * 24, "server_name": "Server one",
                                     "repo_link": "https://hidden-credential@example.invalid"}}],
            "ListProcedures": [{"id": "c" * 24, "name": "Procedure one", "tags": [], "template": False,
                                "info": {"state": "Ok", "next_scheduled_run": 1792256400000,
                                         "schedule_error": "private-schedule-error"}}],
            "ListTags": [],
            "ListFullStacks": [{"_id": {"$oid": "b" * 24},
                                "config": {"auto_update": True, "environment": "private-stack-setting"}}],
            "ListAllContainers": [], "ListDeployments": [], "ListRepos": [],
        }
        assert kind in resources
        return httpx.Response(200, json=resources[kind])

    response = client_for(pve=pve_success, komodo=komodo).get(
        "/pve-komodo/api/dashboard", headers={"cookie": COOKIE},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["user"] == "root@pam"
    assert len(data["servers"]) == len(data["stacks"]) == len(data["procedures"]) == 1
    assert data["servers"][0]["cpu_percent"] is None
    assert data["procedures"][0]["next_run_at"] == 1792256400000
    assert data["stacks"][0]["auto_update"] is True
    assert data["servers"][0]["container_count"] == 0
    assert data["servers"][0]["resource_count"] == 1
    assert len(seen) == 9
    assert "no-store" in response.headers["cache-control"]
    for secret in ("test-key", "test-secret", "private-upstream-address", "private-server-error",
                   "hidden-credential", "private-schedule-error", "private-stack-setting", "signature+value"):
        assert secret not in response.text and secret not in caplog.text


@pytest.mark.parametrize("code,expected", [(401, 401), (403, 403), (500, 503)])
def test_dashboard_pve_failure_never_uses_service_account(code, expected):
    response = client_for(pve=lambda request: httpx.Response(code)).get(
        "/pve-komodo/api/dashboard", headers={"cookie": COOKIE},
    )
    assert response.status_code == expected


def test_dashboard_not_configured_is_a_safe_error_after_authentication():
    response = client_for(pve=pve_success, configured=False).get(
        "/pve-komodo/api/dashboard", headers={"cookie": COOKIE},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "not_configured"}
    assert "no-store" in response.headers["cache-control"]


def test_dashboard_total_deadline_cancels_slow_upstream_work(monkeypatch):
    async def slow_read(self):
        await asyncio.sleep(1)
        pytest.fail("The cancelled dashboard operation must not finish")

    monkeypatch.setattr("plugins.komodo.plugin.DASHBOARD_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("plugins.komodo.plugin.KomodoClient.dashboard", slow_read)
    response = client_for(pve=pve_success).get(
        "/pve-komodo/api/dashboard", headers={"cookie": COOKIE},
    )
    assert response.status_code == 504
    assert response.json() == {"detail": "dashboard_timeout"}
    assert "no-store" in response.headers["cache-control"]


def action_client(monkeypatch, *, perform=None):
    async def dashboard(self):
        return {"servers": [], "stacks": [], "procedures": [], "errors": [], "updated_at": "2026-10-17T00:00:00Z"}

    monkeypatch.setattr("plugins.komodo.plugin.KomodoClient.dashboard", dashboard)
    if perform is not None:
        monkeypatch.setattr("plugins.komodo.plugin.KomodoClient.perform_action", perform)
    client = client_for(pve=pve_success)
    response = client.get("/pve-komodo/api/dashboard", headers={"cookie": COOKIE})
    headers = {"cookie": COOKIE, "x-pve-komodo-csrf": response.json()["csrf_token"], "origin": "http://testserver"}
    payload = {"section": "servers", "action": "prune_images", "ids": ["a" * 24],
               "request_id": "11111111-1111-4111-8111-111111111111"}
    return client, headers, payload


def test_action_requires_authenticated_session_before_any_other_work():
    response = client_for().post("/pve-komodo/api/action", json={})
    assert response.status_code == 401
    assert "no-store" in response.headers["cache-control"]


def test_action_requires_bound_csrf_and_same_origin_before_komodo(monkeypatch):
    client, headers, payload = action_client(monkeypatch)
    assert client.post("/pve-komodo/api/action", headers={"cookie": COOKIE}, json=payload).status_code == 403
    response = client.post("/pve-komodo/api/action", headers={**headers, "origin": "https://evil.example"}, json=payload)
    assert response.status_code == 403
    assert response.json() == {"detail": "invalid_write_origin"}
    response = client.post("/pve-komodo/api/action", headers={**headers, "x-pve-komodo-csrf": "wrong"}, json=payload)
    assert response.json() == {"detail": "invalid_csrf_token"}


@pytest.mark.parametrize("change", [{"ids": []}, {"ids": ["*"]}, {"section": "other"},
                                    {"url": "https://evil.example"}, {"request_id": "not-a-uuid"}])
def test_action_rejects_arbitrary_targets_and_extra_rpc_parameters(monkeypatch, change):
    client, headers, payload = action_client(monkeypatch)
    response = client.post("/pve-komodo/api/action", headers=headers, json={**payload, **change})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_action_request"}


def test_action_executes_once_and_replays_its_safe_receipt(monkeypatch):
    called = []

    async def perform(self, section, action, ids):
        called.append((section, action, ids))
        return {"section": section, "action": action, "results": [
            {"id": ids[0], "ok": True, "status": "submitted", "operation_id": "b" * 24, "error_code": None},
        ]}

    client, headers, payload = action_client(monkeypatch, perform=perform)
    first = client.post("/pve-komodo/api/action", headers=headers, json=payload)
    second = client.post("/pve-komodo/api/action", headers=headers, json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(called) == 1
    assert "no-store" in first.headers["cache-control"]
    conflict = client.post("/pve-komodo/api/action", headers=headers, json={**payload, "ids": ["c" * 24]})
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "idempotency_conflict"}
    assert len(called) == 1


def test_action_unexpected_failure_is_unknown_and_not_retried(monkeypatch):
    called = []

    async def perform(self, *args):
        called.append(args)
        raise RuntimeError("secret-body-and-key")

    client, headers, payload = action_client(monkeypatch, perform=perform)
    first = client.post("/pve-komodo/api/action", headers=headers, json=payload)
    assert first.status_code == 200
    assert first.json()["results"][0]["status"] == "outcome_unknown"
    assert "secret-body-and-key" not in first.text
    client.post("/pve-komodo/api/action", headers=headers, json=payload)
    assert len(called) == 1


def test_action_origin_matches_https_nginx_even_with_http_upstream(monkeypatch):
    async def perform(self, section, action, ids):
        return {"section": section, "action": action, "results": []}

    client, headers, payload = action_client(monkeypatch, perform=perform)
    response = client.post("/pve-komodo/api/action", headers={
        **headers, "origin": "https://testserver", "x-forwarded-proto": "https", "sec-fetch-site": "same-origin",
    }, json=payload)
    assert response.status_code == 200


def test_action_deduplication_survives_pve_ticket_renewal(monkeypatch):
    called = []

    async def authenticate(self, request):
        return "root@pam"

    async def perform(self, section, action, ids):
        called.append(ids)
        return {"section": section, "action": action, "results": []}

    monkeypatch.setattr("app.main.PveAuthenticator.authenticate", authenticate)
    client, headers, payload = action_client(monkeypatch, perform=perform)
    assert client.post("/pve-komodo/api/action", headers=headers, json=payload).status_code == 200
    renewed_cookie = COOKIE.replace("signature", "renewed-signature")
    token = client.get("/pve-komodo/api/dashboard", headers={"cookie": renewed_cookie}).json()["csrf_token"]
    response = client.post("/pve-komodo/api/action", headers={
        **headers, "cookie": renewed_cookie, "x-pve-komodo-csrf": token,
    }, json=payload)
    assert response.status_code == 200
    assert len(called) == 1


def test_containers_read_requires_pve_verification_before_service_account():
    response = client_for(pve=lambda request: httpx.Response(401, text="private-rejection")).get(
        "/pve-komodo/api/containers", headers={"cookie": COOKIE},
    )
    assert response.status_code == 401
    assert "private-rejection" not in response.text
    assert "no-store" in response.headers["cache-control"]


def test_containers_total_deadline_cancels_slow_read(monkeypatch):
    async def slow_read(self):
        await asyncio.sleep(1)
        pytest.fail("Timed-out container work must be cancelled")

    monkeypatch.setattr("plugins.komodo.plugin.DASHBOARD_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("plugins.komodo.plugin.KomodoClient.containers", slow_read)
    response = client_for(pve=pve_success).get("/pve-komodo/api/containers", headers={"cookie": COOKIE})
    assert response.status_code == 504
    assert response.json() == {"detail": "containers_timeout"}
    assert "no-store" in response.headers["cache-control"]


def test_containers_page_issues_session_csrf_and_uses_persistent_action_deduplication(monkeypatch):
    identifier = "a" * 24 + ":" + "b" * 64
    called = []

    async def read(self):
        return {"containers": [{"id": identifier, "name": "app", "server_name": "host"}],
                "servers": [{"id": "a" * 24, "name": "host"}], "updated_at": "2026-10-17T00:00:00Z", "errors": []}

    async def perform(self, section, action, ids):
        called.append((section, action, ids))
        return {"section": section, "action": action, "results": [
            {"id": ids[0], "ok": True, "status": "submitted", "operation_id": "c" * 24, "error_code": None},
        ]}

    monkeypatch.setattr("plugins.komodo.plugin.KomodoClient.containers", read)
    monkeypatch.setattr("plugins.komodo.plugin.KomodoClient.perform_action", perform)
    client = client_for(pve=pve_success)
    response = client.get("/pve-komodo/api/containers", headers={"cookie": COOKIE})
    assert response.status_code == 200
    assert response.json()["user"] == "root@pam"
    assert response.json()["containers"][0]["id"] == identifier
    assert "no-store" in response.headers["cache-control"]
    headers = {"cookie": COOKIE, "x-pve-komodo-csrf": response.json()["csrf_token"], "origin": "http://testserver"}
    payload = {"section": "containers", "action": "start", "ids": [identifier.upper(), identifier],
               "request_id": "11111111-1111-4111-8111-111111111111"}
    first = client.post("/pve-komodo/api/action", headers=headers, json=payload)
    replay = client.post("/pve-komodo/api/action", headers=headers, json=payload)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert called == [("containers", "start", [identifier])]


@pytest.mark.parametrize("section,identifiers", [
    ("containers", ["a" * 24]),
    ("containers", ["app"]),
    ("containers", ["a" * 24 + ":" + "b" * 12]),
    ("containers", ["a" * 24 + ":../container"]),
    ("containers", ["a" * 24 + ":" + "g" * 64]),
    ("servers", ["a" * 24 + ":" + "b" * 64]),
])
def test_container_action_rejects_names_short_ids_and_cross_section_ids(monkeypatch, section, identifiers):
    client, headers, payload = action_client(monkeypatch)
    response = client.post("/pve-komodo/api/action", headers=headers, json={**payload, "section": section, "ids": identifiers})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_action_request"}


def test_alerts_route_forwards_only_validated_filters_after_authentication(monkeypatch):
    calls = []

    async def read(self, **kwargs):
        calls.append(kwargs)
        return {"alerts": [{"id": "a" * 24, "ts": 1792199100000, "resolved_ts": 1792199100123}],
                "page": kwargs["page"], "next_page": None, "updated_at": "2026-10-17T00:00:00Z"}

    monkeypatch.setattr("plugins.komodo.plugin.KomodoClient.alerts", read)
    client = client_for(pve=pve_success)
    response = client.get("/pve-komodo/api/alerts", params={
        "page": 2, "resource_type": "Stack", "alert_type": "StackStateChange", "unresolved_only": "true",
    }, headers={"cookie": COOKIE})
    assert response.status_code == 200
    assert calls == [{"page": 2, "resource_type": "Stack", "alert_type": "StackStateChange", "unresolved_only": True}]
    assert response.json()["user"] == "root@pam"
    assert response.json()["alerts"][0]["ts"] == 1792199100000
    assert response.json()["alerts"][0]["resolved_ts"] == 1792199100123
    assert "csrf_token" not in response.json()
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("query", [
    "query={\"$where\":\"private-test\"}", "resource_type=Other", "alert_type=Other",
    "page=-1", "page=1.5", "page=true", "page=1000001", "page=0001", "page=0&page=1",
    "unresolved_only=1", "unresolved_only=false&unresolved_only=true",
])
def test_alert_query_rejects_mongo_unknown_and_ambiguous_filters_before_komodo(query):
    response = client_for(pve=pve_success).get("/pve-komodo/api/alerts?" + query, headers={"cookie": COOKIE})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_alert_query"}
    assert "private-test" not in response.text
    assert "no-store" in response.headers["cache-control"]


def test_alert_query_is_not_processed_before_pve_authentication():
    response = client_for().get("/pve-komodo/api/alerts?query=arbitrary")
    assert response.status_code == 401


def test_alerts_route_total_deadline_cancels_slow_read(monkeypatch):
    async def slow_read(self, **kwargs):
        await asyncio.sleep(1)
        pytest.fail("Timed-out alert read must not finish")

    monkeypatch.setattr("plugins.komodo.plugin.DASHBOARD_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("plugins.komodo.plugin.KomodoClient.alerts", slow_read)
    response = client_for(pve=pve_success).get("/pve-komodo/api/alerts", headers={"cookie": COOKIE})
    assert response.status_code == 504
    assert response.json() == {"detail": "alerts_timeout"}
    assert "no-store" in response.headers["cache-control"]


def test_updates_route_accepts_only_fixed_filters_after_authentication(monkeypatch):
    calls = []

    async def read(self, **kwargs):
        calls.append(kwargs)
        return {"updates": [{"id": "a" * 24, "start_ts": 1792199100123}], "page": kwargs["page"],
                "next_page": None, "updated_at": "2026-10-17T00:00:00Z", "errors": []}

    monkeypatch.setattr("plugins.komodo.plugin.KomodoClient.updates", read)
    response = client_for(pve=pve_success).get("/pve-komodo/api/updates", params={
        "page": 2, "resource_type": "Server", "operation": "RestartAllContainers",
    }, headers={"cookie": COOKIE})
    assert response.status_code == 200
    assert calls == [{"page": 2, "resource_type": "Server", "operation": "RestartAllContainers"}]
    assert response.json()["updates"][0]["start_ts"] == 1792199100123
    assert response.json()["user"] == "root@pam"
    assert "csrf_token" not in response.json()
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("query", [
    "query={\"$where\":\"private-test\"}", "resource_type=Other", "operation=Other",
    "page=-1", "page=1.5", "page=true", "page=1000001", "page=0001", "page=0&page=1",
    "operation=RestartAllContainers&operation=StopAllContainers", "_dc=123",
])
def test_updates_route_rejects_unknown_and_ambiguous_queries_before_komodo(query):
    response = client_for(pve=pve_success).get("/pve-komodo/api/updates?" + query, headers={"cookie": COOKIE})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_update_query"}
    assert "private-test" not in response.text


def test_update_details_route_authorizes_before_validating_id_and_preserves_timestamps(monkeypatch):
    calls = []

    async def details(self, identifier):
        calls.append(identifier)
        return {"update": {"id": identifier, "start_ts": 1792199100000, "end_ts": 1792199100123},
                "updated_at": "2026-10-17T00:00:00Z", "errors": []}

    monkeypatch.setattr("plugins.komodo.plugin.KomodoClient.update_details", details)
    assert client_for().get("/pve-komodo/api/updates/not-an-id").status_code == 401
    response = client_for(pve=pve_success).get("/pve-komodo/api/updates/" + "A" * 24, headers={"cookie": COOKIE})
    assert response.status_code == 200
    assert response.json()["update"]["end_ts"] - response.json()["update"]["start_ts"] == 123
    assert calls == ["a" * 24]
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("path", ["not-an-id", "a" * 23, "g" * 24, "a" * 24 + "?url=https://other.invalid"])
def test_update_details_rejects_names_and_extra_parameters_before_komodo(path):
    response = client_for(pve=pve_success).get("/pve-komodo/api/updates/" + path, headers={"cookie": COOKIE})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_update_id"}


@pytest.mark.parametrize("kind,method,expected", [
    ("updates", "updates", "updates_timeout"),
    ("updates/" + "a" * 24, "update_details", "update_details_timeout"),
])
def test_update_reads_have_total_deadlines(monkeypatch, kind, method, expected):
    async def slow_read(self, *args, **kwargs):
        await asyncio.sleep(1)
        pytest.fail("Timed-out operation history work must be cancelled")

    monkeypatch.setattr("plugins.komodo.plugin.DASHBOARD_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("plugins.komodo.plugin.KomodoClient." + method, slow_read)
    response = client_for(pve=pve_success).get("/pve-komodo/api/" + kind, headers={"cookie": COOKIE})
    assert response.status_code == 504
    assert response.json() == {"detail": expected}


@pytest.mark.parametrize("method,route", [
    ("get", "server-create-options"), ("post", "servers/create"),
    *[("get", "servers/" + "a" * 24 + "/" + section) for section in ["summary", "history", "options", "docker", "resources", "terminal-services"]],
    *[("post", "servers/" + "a" * 24 + "/" + section) for section in ["description", "actions", "options", "inventory-actions", "terminals"]],
])
def test_create_app_registers_server_creation_and_unlinked_server_routes_with_auth(method, route):
    response = getattr(client_for(), method)("/pve-komodo/api/" + route)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"
