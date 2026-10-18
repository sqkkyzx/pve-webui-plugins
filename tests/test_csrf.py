import base64
import hashlib
import hmac
import json

from fastapi import HTTPException, Request
import pytest

from app.csrf import CsrfProtector, verify_write_origin


SECRET = b"test-csrf-secret-must-be-32-bytes!"
COOKIE = "PVEAuthCookie=PVE%3Aroot%40pam%3A1234ABCD%3A%3Asigned-private-ticket"


def request(*, host="pve.example", scheme="https", cookie=COOKIE, origin=None, fetch_site=None, extra=()):
    headers = [("host", host)]
    if cookie is not None:
        headers.append(("cookie", cookie))
    if origin is not None:
        headers.append(("origin", origin))
    if fetch_site is not None:
        headers.append(("sec-fetch-site", fetch_site))
    headers.extend(extra)
    return Request({
        "type": "http", "scheme": scheme, "method": "POST", "path": "/pve-plugins/api/komodo/action",
        "query_string": b"", "headers": [(name.encode(), value.encode()) for name, value in headers],
        "server": ("pve.example", 443),
    })


def error403(action, code):
    with pytest.raises(HTTPException) as error:
        action()
    assert error.value.status_code == 403
    assert error.value.detail == code
    assert "private-ticket" not in str(error.value)


def resign(payload):
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    signature = hmac.new(SECRET, b"pve-plugins-csrf-token\0" + body, hashlib.sha256).digest()
    return "v1." + body.decode() + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def payload(token):
    encoded = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))


def test_valid_token_is_bound_to_user_and_cookie_without_disclosing_ticket():
    protector = CsrfProtector(SECRET)
    req = request(origin="https://pve.example", fetch_site="same-origin")
    token = protector.issue(req, "root@pam")
    protector.verify(req, "root@pam", token)
    data = payload(token)
    assert data["user"] == "root@pam"
    assert len(data["binding"]) == 64
    assert "PVEAuthCookie" not in json.dumps(data)
    assert "private-ticket" not in json.dumps(data)
    assert "private-ticket" not in token


def test_default_random_secret_does_not_accept_another_process_token():
    req = request()
    token = CsrfProtector().issue(req, "root@pam")
    error403(lambda: CsrfProtector().verify(req, "root@pam", token), "invalid_csrf_token")


def test_token_cannot_be_replayed_as_different_user_or_browser_session():
    protector = CsrfProtector(SECRET)
    token = protector.issue(request(), "root@pam")
    error403(lambda: protector.verify(request(), "alice@pve", token), "invalid_csrf_token")
    different_cookie = COOKIE.replace("signed-private-ticket", "another-private-ticket")
    error403(lambda: protector.verify(request(cookie=different_cookie), "root@pam", token), "invalid_csrf_token")
    error403(lambda: protector.verify(request(cookie=None), "root@pam", token), "invalid_csrf_token")


def test_cookie_is_bound_exactly_and_not_redecoded_into_other_session():
    protector = CsrfProtector(SECRET)
    token = protector.issue(request(), "root@pam")
    raw_cookie = "PVEAuthCookie=PVE:root@pam:1234ABCD::signed-private-ticket"
    error403(lambda: protector.verify(request(cookie=raw_cookie), "root@pam", token), "invalid_csrf_token")


def test_expiration_boundary_and_future_issued_token_are_rejected(monkeypatch):
    protector = CsrfProtector(SECRET, ttl_seconds=60)
    monkeypatch.setattr("app.csrf.time.time", lambda: 1000)
    req = request()
    token = protector.issue(req, "root@pam")
    monkeypatch.setattr("app.csrf.time.time", lambda: 1059)
    protector.verify(req, "root@pam", token)
    monkeypatch.setattr("app.csrf.time.time", lambda: 1060)
    error403(lambda: protector.verify(req, "root@pam", token), "invalid_csrf_token")
    monkeypatch.setattr("app.csrf.time.time", lambda: 999)
    error403(lambda: protector.verify(req, "root@pam", token), "invalid_csrf_token")


@pytest.mark.parametrize("token", [None, "", "not-a-token", "v2.body.signature", "x" * 2049, "v1.%FF.bad"])
def test_missing_malformed_or_overlong_token_has_fixed_failure(token):
    error403(lambda: CsrfProtector(SECRET).verify(request(), "root@pam", token), "invalid_csrf_token")


def test_payload_and_signature_tampering_cannot_authenticate():
    protector = CsrfProtector(SECRET)
    req = request()
    token = protector.issue(req, "root@pam")
    version, body, signature = token.split(".")
    changed_body = ("A" if body[0] != "A" else "B") + body[1:]
    changed_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    for forged in (".".join((version, changed_body, signature)), ".".join((version, body, changed_signature))):
        error403(lambda: protector.verify(req, "root@pam", forged), "invalid_csrf_token")


@pytest.mark.parametrize("change", [
    {"user": "alice@pve"}, {"user": ["root@pam"]}, {"binding": "bad"},
    {"issued_at": True}, {"issued_at": "1000"}, {"issued_at": -1}, {"extra": "unsupported"},
])
def test_signed_but_invalid_payload_is_still_rejected(change):
    protector = CsrfProtector(SECRET)
    req = request()
    data = payload(protector.issue(req, "root@pam"))
    data.update(change)
    error403(lambda: protector.verify(req, "root@pam", resign(data)), "invalid_csrf_token")


@pytest.mark.parametrize("fetch_site", ["cross-site", "same-site", "", "unknown"])
def test_cross_site_and_same_site_writes_are_denied_even_with_valid_token(fetch_site):
    protector = CsrfProtector(SECRET)
    token = protector.issue(request(), "root@pam")
    req = request(origin="https://pve.example", fetch_site=fetch_site)
    error403(lambda: protector.verify(req, "root@pam", token), "invalid_write_origin")


def test_same_origin_behind_https_nginx_works_with_no_proxy_headers_uvicorn():
    protector = CsrfProtector(SECRET)
    req = request(scheme="http", origin="https://pve.example", fetch_site="same-origin",
                  extra=[("x-forwarded-proto", "https")])
    token = protector.issue(req, "root@pam")
    protector.verify(req, "root@pam", token)


def test_forwarded_scheme_never_replaces_host_or_allows_http_origin_on_https_site():
    error403(lambda: verify_write_origin(request(
        scheme="http", origin="https://untrusted.example", fetch_site="same-origin",
        extra=[("x-forwarded-proto", "https"), ("x-forwarded-host", "untrusted.example")],
    )), "invalid_write_origin")
    error403(lambda: verify_write_origin(request(
        scheme="http", origin="http://pve.example", extra=[("x-forwarded-proto", "https")],
    )), "invalid_write_origin")


@pytest.mark.parametrize("host,origin", [
    ("pve.example", "https://pve.example"),
    ("pve.example:443", "https://pve.example"),
    ("pve.example", "https://pve.example:443"),
    ("pve.example:8443", "https://pve.example:8443"),
    ("PVE.EXAMPLE:443", "https://pve.example"),
    ("[2001:db8::1]:8443", "https://[2001:db8::1]:8443"),
])
def test_same_origin_authority_normalizes_default_ports_and_host_case(host, origin):
    verify_write_origin(request(host=host, origin=origin))


@pytest.mark.parametrize("origin", [
    "null", "https://untrusted.example", "http://pve.example", "https://pve.example:8443",
    "https://user:secret@pve.example", "https://pve.example/", "https://pve.example/path",
    "https://pve.example?", "https://pve.example#", "https://pve.example\\@untrusted.example",
    "https://pve.example https://untrusted.example", "https://pve.example:0",
])
def test_foreign_or_non_origin_urls_are_rejected(origin):
    error403(lambda: verify_write_origin(request(origin=origin)), "invalid_write_origin")


@pytest.mark.parametrize("host", [
    "user@pve.example", "pve.example/path", "pve.example?x=1", "pve.example#x", "pve.example:",
    "pve.example:65536", "pve.example,untrusted.example", "pve.example\nX-Test:injected", "%70ve.example",
])
def test_malformed_host_is_rejected_even_without_browser_headers(host):
    error403(lambda: verify_write_origin(request(host=host)), "invalid_write_origin")


def test_duplicate_host_origin_and_forwarded_scheme_headers_fail_closed():
    for headers in (
        [("host", "untrusted.example")], [("origin", "https://untrusted.example")],
        [("x-forwarded-proto", "https"), ("x-forwarded-proto", "http")],
        [("x-forwarded-proto", "https,http")],
    ):
        error403(lambda: verify_write_origin(request(origin="https://pve.example", extra=headers)), "invalid_write_origin")


@pytest.mark.parametrize("fetch_site", [None, "same-origin", "none"])
def test_no_origin_cli_and_same_origin_browser_require_the_valid_token(fetch_site):
    protector = CsrfProtector(SECRET)
    req = request(fetch_site=fetch_site)
    protector.verify(req, "root@pam", protector.issue(req, "root@pam"))
    error403(lambda: protector.verify(req, "root@pam", "not-a-token"), "invalid_csrf_token")


def test_forwarded_proto_alone_cannot_bypass_invalid_token():
    req = request(scheme="http", origin="https://pve.example", extra=[("x-forwarded-proto", "https")])
    error403(lambda: CsrfProtector(SECRET).verify(req, "root@pam", "forged"), "invalid_csrf_token")


@pytest.mark.parametrize("secret,ttl", [(b"short", 3600), (SECRET, 0), (SECRET, -1), (SECRET, True)])
def test_insecure_configuration_is_rejected(secret, ttl):
    with pytest.raises(ValueError):
        CsrfProtector(secret, ttl_seconds=ttl)
