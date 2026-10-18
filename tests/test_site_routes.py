"""Site CRUD uses the real PVE session and same-origin CSRF boundaries."""

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from app.csrf import CsrfProtector
from app.main import Settings, create_app
from app.pve_auth import PveAuthenticator
from plugins.sites.site_routes import register_site_routes


PVE = "https://pve.example:8006"
PATH = "/pve-plugins/api/sites"
TICKET = "PVE:root@pam:1234ABCD::fixture-signature="
COOKIE = "PVEAuthCookie=" + quote(TICKET, safe="")
URL = "https://grafana.example/dashboard?orgId=1#panel"


def pve_transport(state):
    def pve(request):
        assert str(request.url).startswith(PVE + "/")
        assert request.headers["cookie"] == state.get("cookie", COOKIE)
        assert "authorization" not in request.headers and "x-api-key" not in request.headers
        state["requests"].append((request.method, request.url.path))
        if not state.get("allowed", True):
            return httpx.Response(401, text="private-upstream-response")
        if request.url.path == "/api2/json/access/permissions":
            assert request.method == "GET"
            return httpx.Response(200, json={"data": {"/": {"Sys.Audit": 1}}})
        assert request.method == "POST" and request.url.path == "/api2/json/access/ticket"
        body = parse_qs(request.content.decode())
        assert body == {"username": ["root@pam"], "password": [state.get("ticket", TICKET)], "path": ["/"], "privs": ["Sys.Audit"]}
        return httpx.Response(200, json={"data": {"username": "root@pam"}})
    return httpx.MockTransport(pve)


@pytest.fixture
def integrated(tmp_path):
    state = {"requests": []}
    path = tmp_path / "new-parent" / "sites.sqlite3"
    settings = Settings(PVE, site_state_file=str(path))
    application = create_app(settings, pve_auth_transport=pve_transport(state),
        komodo_transport=httpx.MockTransport(lambda _: pytest.fail("Sites must not contact Komodo or a saved site URL")))
    with TestClient(application) as client:
        yield client, state, path


def credentials(client):
    response = client.get(PATH, headers={"cookie": COOKIE})
    assert response.status_code == 200
    return {"cookie": COOKIE, "origin": "http://testserver", "x-pve-plugins-csrf": response.json()["csrf_token"]}


def test_real_app_crud_and_private_headers_work_without_komodo(integrated):
    client, state, path = integrated
    assert not path.exists()
    headers = credentials(client)
    response = client.post(PATH, json={"name": "  監控  ", "url": URL}, headers=headers)
    assert response.status_code == 200
    site = response.json()["site"]
    assert site["name"] == "監控" and site["url"] == URL and site["revision"] == 1
    listing = client.get(PATH, headers={"cookie": COOKIE})
    assert listing.json()["user"] == "root@pam" and listing.json()["sites"] == [site]
    assert listing.headers["cache-control"] == "private, no-store"
    assert listing.headers["x-content-type-options"] == "nosniff"
    updated = client.patch(PATH + "/" + site["id"], json={"name": "更新", "url": URL, "expected_revision": 1}, headers=headers)
    assert updated.status_code == 200 and updated.json()["site"]["revision"] == 2
    stale = client.patch(PATH + "/" + site["id"], json={"name": "stale", "url": URL, "expected_revision": 1}, headers=headers)
    assert stale.status_code == 409 and stale.json()["detail"] == "site_revision_conflict"
    assert client.request("DELETE", PATH + "/" + site["id"], json={"expected_revision": 1}, headers=headers).status_code == 409
    removed = client.request("DELETE", PATH + "/" + site["id"], json={"expected_revision": 2}, headers=headers)
    assert removed.json() == {"deleted": True, "id": site["id"]}
    assert client.get(PATH, headers={"cookie": COOKIE}).json()["sites"] == []
    assert len(state["requests"]) == 16  # Every one of eight requests re-authenticates.


@pytest.mark.parametrize("method,path,payload", [
    ("GET", PATH, None), ("POST", PATH, {"name": "monitor", "url": URL}),
    ("PATCH", PATH + "/" + "a" * 32, {"name": "monitor", "url": URL, "expected_revision": 1}),
    ("DELETE", PATH + "/" + "a" * 32, {"expected_revision": 1}),
])
def test_anonymous_requests_are_401_before_pve_network_or_lazy_database(integrated, method, path, payload):
    client, state, database = integrated
    response = client.request(method, path, json=payload)
    assert response.status_code == 401 and response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert state["requests"] == [] and not database.exists()


@pytest.mark.parametrize("override", [
    {"x-pve-plugins-csrf": "wrong"}, {"origin": "https://evil.example"},
    {"origin": "http://evil.example", "x-forwarded-host": "evil.example"},
    {"sec-fetch-site": "cross-site"},
])
def test_token_and_browser_origin_fail_before_any_site_write(integrated, override):
    client, state, path = integrated
    headers = credentials(client)
    response = client.post(PATH, json={"name": "no", "url": URL}, headers={**headers, **override})
    assert response.status_code == 403
    assert client.get(PATH, headers={"cookie": COOKIE}).json()["sites"] == []


def test_duplicate_csrf_headers_are_rejected(integrated):
    client, _, _ = integrated
    headers = credentials(client)
    response = client.post(PATH, json={"name": "no", "url": URL}, headers=[
        ("cookie", COOKIE), ("origin", "http://testserver"),
        ("x-pve-plugins-csrf", headers["x-pve-plugins-csrf"]), ("x-pve-plugins-csrf", headers["x-pve-plugins-csrf"])])
    assert response.status_code == 403


def test_missing_csrf_does_not_initialize_database_even_with_valid_pve_session(integrated):
    client, state, database = integrated
    response = client.post(PATH, json={"name": "no", "url": URL}, headers={"cookie": COOKIE})
    assert response.status_code == 403 and not database.exists()
    assert len(state["requests"]) == 2


def test_csrf_from_previous_pve_ticket_cannot_write_in_new_session(integrated):
    client, state, _ = integrated
    old = credentials(client)
    state["ticket"] = "PVE:root@pam:1234ABCE::different-signature="
    state["cookie"] = "PVEAuthCookie=" + quote(state["ticket"], safe="")
    response = client.post(PATH, json={"name": "no", "url": URL}, headers={**old, "cookie": state["cookie"]})
    assert response.status_code == 403
    assert client.get(PATH, headers={"cookie": state["cookie"]}).json()["sites"] == []


def test_expired_pve_session_fails_before_saved_sites_are_read(integrated):
    client, state, path = integrated
    headers = credentials(client)
    assert client.post(PATH, json={"name": "saved", "url": URL}, headers=headers).status_code == 200
    state["allowed"] = False
    response = client.get(PATH, headers={"cookie": COOKIE})
    assert response.status_code == 401 and URL not in response.text


@pytest.mark.parametrize("method,path,payload", [
    ("POST", PATH, {"name": "ok", "url": URL, "unknown": True}),
    ("POST", PATH, {"name": 1, "url": URL}), ("POST", PATH, {"name": "ok", "url": True}),
    ("PATCH", PATH + "/" + "a" * 32, {"name": "ok", "url": URL}),
    ("PATCH", PATH + "/" + "a" * 32, {"name": "ok", "url": URL, "expected_revision": True}),
    ("DELETE", PATH + "/" + "a" * 32, {"expected_revision": "1"}),
    ("DELETE", PATH + "/" + "a" * 32, {"expected_revision": 0}),
    ("DELETE", PATH + "/" + "a" * 32, {"expected_revision": 1, "url": URL}),
])
def test_strict_json_schema_and_revisions(integrated, method, path, payload):
    client, _, _ = integrated
    response = client.request(method, path, json=payload, headers=credentials(client))
    assert response.status_code == 422 and response.json()["detail"] == "invalid_site_request"


@pytest.mark.parametrize("body", [
    "not-json", '{"name":"one","name":"two","url":"https://example.com"}',
    '{"name":"ok","url":"https://example.com","unknown":NaN}',
    "[" * 2000 + "]" * 2000,
])
def test_malformed_duplicate_and_deep_json_get_fixed_error(integrated, body):
    client, _, _ = integrated
    response = client.post(PATH, content=body, headers={**credentials(client), "content-type": "application/json"})
    assert response.status_code == 422 and response.json() == {"detail": "invalid_site_request"}


def test_content_type_bounded_stream_and_query_whitelist(integrated):
    client, _, _ = integrated
    headers = credentials(client)
    assert client.post(PATH, content='{"name":"ok"}', headers=headers).status_code == 415
    oversized = client.post(PATH, content="x" * 32769, headers={**headers, "content-type": "application/json"})
    assert oversized.status_code == 413 and oversized.json()["detail"] == "site_request_too_large"
    for method, path, payload in (("GET", PATH, None), ("POST", PATH, {"name": "ok", "url": URL}),
                                   ("DELETE", PATH + "/" + "a" * 32, {"expected_revision": 1})):
        response = client.request(method, path + "?upstream=http://evil.example", json=payload, headers=headers)
        assert response.status_code == 422 and response.json()["detail"] == "invalid_site_query"


@pytest.mark.parametrize("payload,code", [
    ({"name": " ", "url": URL}, "invalid_site_name"),
    ({"name": "ok", "url": "javascript:alert(1)"}, "invalid_site_url"),
    ({"name": "ok", "url": "https://user:private-secret@example.com"}, "invalid_site_url"),
])
def test_invalid_link_values_are_not_reflected_or_persisted(integrated, payload, code):
    client, _, _ = integrated
    response = client.post(PATH, json=payload, headers=credentials(client))
    assert response.status_code == 422 and response.json() == {"detail": code}
    assert "private-secret" not in response.text


def test_storage_unavailability_is_fixed_and_never_exposes_file_path(tmp_path):
    state = {"requests": []}
    application = create_app(Settings(PVE, site_state_file=str(tmp_path)), pve_auth_transport=pve_transport(state))
    with TestClient(application) as client:
        response = client.get(PATH, headers={"cookie": COOKIE})
        assert response.status_code == 503 and response.json() == {"detail": "site_store_unavailable"}
        assert str(tmp_path) not in response.text


def test_different_pve_origin_cannot_read_existing_sites(tmp_path):
    from plugins.sites.site_store import SitesStore

    path = tmp_path / "sites.sqlite3"
    initial = SitesStore(path, pve_origin="https://different.example")
    initial.create("private site", "https://private.example")
    initial.close()
    state = {"requests": []}
    application = create_app(Settings(PVE, site_state_file=str(path)), pve_auth_transport=pve_transport(state))
    with TestClient(application) as client:
        response = client.get(PATH, headers={"cookie": COOKIE})
        assert response.status_code == 503 and response.json() == {"detail": "pve_instance_mismatch"}
        assert "private.example" not in response.text


def test_persisted_site_can_be_read_after_main_app_restart(tmp_path):
    path = tmp_path / "sites.sqlite3"
    settings, state = Settings(PVE, site_state_file=str(path)), {"requests": []}
    with TestClient(create_app(settings, pve_auth_transport=pve_transport(state))) as client:
        saved = client.post(PATH, json={"name": "persistent", "url": URL}, headers=credentials(client)).json()["site"]
    with TestClient(create_app(settings, pve_auth_transport=pve_transport(state))) as restarted:
        listing = restarted.get(PATH, headers={"cookie": COOKIE})
        assert listing.status_code == 200 and listing.json()["sites"] == [saved]


@pytest.mark.parametrize("actions,expected", [(None, "data/sites.sqlite3"), (":memory:", ":memory:"), ("state/actions.sqlite3", "state/sites.sqlite3")])
def test_default_path_derivation_is_lazy_and_explicit(monkeypatch, tmp_path, actions, expected):
    monkeypatch.chdir(tmp_path)
    settings = SimpleNamespace(site_state_file=None, action_state_file=actions, upstream=PVE)
    app = FastAPI()
    database = register_site_routes(app, settings, None, CsrfProtector())
    assert str(database.path) == (str(tmp_path / expected) if actions is None else expected)
    assert database._connection is None and list(tmp_path.iterdir()) == []


def test_old_csrf_header_cannot_authorize_a_write(integrated):
    client,_,_ = integrated
    headers = credentials(client)
    headers['x-pve-komodo-csrf'] = headers.pop('x-pve-plugins-csrf')
    response = client.post(PATH,json={"name": "no", "url": URL},headers=headers)
    assert response.status_code == 403
    assert client.get(PATH,headers={"cookie": COOKIE}).json()['sites'] == []
