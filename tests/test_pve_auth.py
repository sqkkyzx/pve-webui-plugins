from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import httpx
import pytest

from app.pve_auth import PveAuthenticator


ROOT_TICKET = "PVE:root@pam:6A000001::signed+ticket/value="
ROOT_COOKIE = quote(ROOT_TICKET, safe="")
BOB_TICKET = "PVE:bob@pve:6A000002::another+ticket/value="
BOB_COOKIE = quote(BOB_TICKET, safe="")


def client_for(handler, *, allowed_users=("root@pam",)):
    authenticator = PveAuthenticator(
        "https://pve.example:8006",
        allowed_users=allowed_users,
        transport=httpx.MockTransport(handler),
    )
    app = FastAPI()

    @app.get("/protected")
    async def protected(request: Request):
        return {"username": await authenticator.authenticate(request)}

    return TestClient(app)


def success(request):
    return httpx.Response(200, json={"data": (
        {"/": {"Sys.Audit": 1}} if request.method == "GET" else {"username": "root@pam"}
    )})


def test_existing_session_is_checked_then_identity_verified_without_renewal():
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.url.host == "pve.example"
        assert request.url.port == 8006
        assert request.url.query == b""
        assert request.headers["cookie"] == f"PVEAuthCookie={ROOT_COOKIE}"
        for name in ("authorization", "x-api-key", "x-api-secret", "x-user", "csrfpreventiontoken"):
            assert name not in request.headers
        if request.method == "GET":
            assert request.url.path == "/api2/json/access/permissions"
        else:
            assert request.method == "POST"
            assert request.url.path == "/api2/json/access/ticket"
            assert parse_qs(request.content.decode()) == {
                "username": ["root@pam"],
                "password": [ROOT_TICKET],
                "path": ["/"],
                "privs": ["Sys.Audit"],
            }
        return success(request)

    response = client_for(upstream).get("/protected", headers={
        "Cookie": f"other=private; PVEAuthCookie={ROOT_COOKIE}",
        "Authorization": "Bearer do-not-forward",
        "X-User": "different@pve",
        "X-Api-Key": "do-not-forward",
        "X-Api-Secret": "do-not-forward",
        "CSRFPreventionToken": "not-a-csrf-verification",
    })
    assert response.status_code == 200
    assert response.json() == {"username": "root@pam"}
    assert [request.method for request in calls] == ["GET", "POST"]
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("cookie", [
    None, "", "not-a-pve-ticket", "PVE:root@pam:invalid::signature",
    quote(ROOT_COOKIE, safe=""), quote(ROOT_TICKET + "\n", safe=""),
    "PVEAuthCookie=" + ROOT_COOKIE, "x" * 4097, "%FF", ROOT_COOKIE + "%FF",
    quote("PVE:!tfa!incomplete:6A000001::signature", safe=""),
])
def test_missing_or_malformed_browser_cookie_never_contacts_upstream(cookie):
    def unexpected(request):
        pytest.fail("invalid cookies must not reach PVE")

    headers = {"X-User": "root@pam", "Authorization": "PVEAPIToken=root@pam!integration=invalid"}
    if cookie is not None:
        headers["Cookie"] = f"PVEAuthCookie={cookie}"
    response = client_for(unexpected).get("/protected", headers=headers)
    assert response.status_code == 401


def test_user_outside_allowlist_is_forbidden_before_upstream_access():
    def unexpected(request):
        pytest.fail("users outside the explicit allowlist must not reach PVE")

    response = client_for(unexpected).get("/protected", headers={"Cookie": f"PVEAuthCookie={BOB_COOKIE}"})
    assert response.status_code == 403


@pytest.mark.parametrize("status", [401, 403])
def test_expired_disabled_or_incomplete_session_fails_before_identity_check(status):
    methods = []

    def upstream(request):
        methods.append(request.method)
        return httpx.Response(status, json={"data": None, "errors": {"detail": ROOT_TICKET}})

    response = client_for(upstream).get("/protected", headers={"Cookie": f"PVEAuthCookie={ROOT_COOKIE}"})
    assert response.status_code == status
    assert methods == ["GET"]
    assert ROOT_TICKET not in response.text


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_need_tfa_data_cannot_authenticate(method):
    def upstream(request):
        if request.method == method:
            return httpx.Response(200, json={"data": {"username": "root@pam", "NeedTFA": 1}})
        return success(request)

    response = client_for(upstream).get("/protected", headers={"Cookie": f"PVEAuthCookie={ROOT_COOKIE}"})
    assert response.status_code == 401


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("kind", ["redirect", "html", "data-array", "errors", "failed"])
def test_malformed_or_redirected_pve_responses_fail_closed(method, kind):
    calls = []

    def upstream(request):
        calls.append(request.method)
        if request.method != method:
            return success(request)
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.invalid/steal"})
        if kind == "html":
            return httpx.Response(200, text=f"<html>{ROOT_TICKET}</html>")
        if kind == "data-array":
            return httpx.Response(200, json={"data": []})
        if kind == "errors":
            return httpx.Response(200, json={"data": {}, "errors": {"private": ROOT_TICKET}})
        return httpx.Response(200, json={"data": {}, "success": False})

    response = client_for(upstream).get("/protected", headers={"Cookie": f"PVEAuthCookie={ROOT_COOKIE}"})
    assert response.status_code == 503
    assert calls == (["GET"] if method == "GET" else ["GET", "POST"])
    assert ROOT_TICKET not in response.text


def test_forged_identity_hint_is_not_trusted_even_when_session_permissions_exist():
    def upstream(request):
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"username": "bob@pve"}})
        return success(request)

    response = client_for(upstream).get("/protected", headers={"Cookie": f"PVEAuthCookie={ROOT_COOKIE}"})
    assert response.status_code == 401


@pytest.mark.parametrize("status", [401, 403])
def test_failed_identity_or_audit_permission_check_cannot_reuse_get_success(status):
    def upstream(request):
        if request.method == "POST":
            return httpx.Response(status, json={"data": None})
        return success(request)

    response = client_for(upstream).get("/protected", headers={"Cookie": f"PVEAuthCookie={ROOT_COOKIE}"})
    assert response.status_code == status


def test_unexpected_ticket_renewal_response_is_not_accepted_or_returned():
    def upstream(request):
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"username": "root@pam", "ticket": ROOT_TICKET}})
        return success(request)

    response = client_for(upstream).get("/protected", headers={"Cookie": f"PVEAuthCookie={ROOT_COOKIE}"})
    assert response.status_code == 503
    assert ROOT_TICKET not in response.text


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_network_failure_is_unavailable_without_leaking_ticket(method, caplog):
    def upstream(request):
        if request.method == method:
            raise httpx.ConnectError(ROOT_TICKET, request=request)
        return success(request)

    response = client_for(upstream).get("/protected", headers={"Cookie": f"PVEAuthCookie={ROOT_COOKIE}"})
    assert response.status_code == 503
    assert ROOT_TICKET not in response.text
    assert ROOT_TICKET not in caplog.text


def test_upstream_cookie_jar_never_replaces_supplied_session_or_crosses_users():
    cookies = []

    def upstream(request):
        cookies.append(request.headers["cookie"])
        if request.method == "GET":
            return httpx.Response(200, json={"data": {}}, headers={
                "Set-Cookie": "PVEAuthCookie=another-user; Secure; Path=/",
            })
        username = parse_qs(request.content.decode())["username"][0]
        return httpx.Response(200, json={"data": {"username": username}})

    client = client_for(upstream, allowed_users=("root@pam", "bob@pve"))
    for value, username in ((ROOT_COOKIE, "root@pam"), (BOB_COOKIE, "bob@pve")):
        response = client.get("/protected", headers={"Cookie": f"PVEAuthCookie={value}"})
        assert response.status_code == 200
        assert response.json() == {"username": username}
        assert "set-cookie" not in response.headers
    assert cookies == [f"PVEAuthCookie={ROOT_COOKIE}"] * 2 + [f"PVEAuthCookie={BOB_COOKIE}"] * 2


@pytest.mark.parametrize("upstream", [
    "http://pve.example:8006", "https://user:secret@pve.example:8006",
    "https://pve.example:8006/api2", "https://pve.example?next=other", "https://pve.example#other",
])
def test_authentication_origin_is_fixed_https(upstream):
    with pytest.raises(ValueError):
        PveAuthenticator(upstream)
