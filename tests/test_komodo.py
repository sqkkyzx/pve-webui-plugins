import asyncio
import json
import ssl

import httpx
import pytest

from plugins.komodo.komodo import KomodoClient


ORIGIN = "http://komodo-core-1:9120"
API_KEY = "server-only-api-key"
API_SECRET = "server-only-api-secret"


def check(handler, *, url=ORIGIN, key=API_KEY, secret=API_SECRET):
    client = KomodoClient(url, key, secret, transport=httpx.MockTransport(handler))
    return asyncio.run(client.connection_status())


def assert_private(result):
    serialized = json.dumps(result)
    assert API_KEY not in serialized
    assert API_SECRET not in serialized
    assert ORIGIN not in serialized
    assert "PVEAuthCookie" not in serialized


def test_fixed_endpoints_credentials_and_allowlisted_result():
    seen = []

    def upstream(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.query == b""
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert request.extensions["timeout"] == {
            "connect": 5.0, "read": 10.0, "write": 10.0, "pool": 10.0,
        }
        if request.url.path == "/user":
            assert str(request.url) == ORIGIN + "/user"
            assert request.headers["x-api-key"] == API_KEY
            assert request.headers["x-api-secret"] == API_SECRET
            return httpx.Response(200, json={
                "username": "pve-service", "enabled": True,
                "config": {"private": API_SECRET}, "totp": {"secret": API_SECRET},
            }, headers={"set-cookie": "other-session=private; Path=/"})
        assert str(request.url) == ORIGIN + "/version"
        assert "x-api-key" not in request.headers
        assert "x-api-secret" not in request.headers
        return httpx.Response(200, text="2.3.3", headers={"content-type": "text/plain"})

    result = check(upstream)
    assert [request.url.path for request in seen] == ["/user", "/version"]
    assert result == {"configured": True, "connected": True, "username": "pve-service", "version": "2.3.3"}
    assert_private(result)


@pytest.mark.parametrize("url,key,secret", [
    (None, None, None), (None, API_KEY, API_SECRET), ("", API_KEY, API_SECRET),
    (ORIGIN, None, API_SECRET), (ORIGIN, API_KEY, None),
    (ORIGIN, "", API_SECRET), (ORIGIN, API_KEY, "  "),
])
def test_incomplete_configuration_never_contacts_an_upstream(url, key, secret):
    def unexpected(request):
        pytest.fail("incomplete Komodo credentials must not produce a request")

    assert check(unexpected, url=url, key=key, secret=secret) == {"configured": False, "connected": False}


@pytest.mark.parametrize("url", [
    "ftp://komodo-core-1", "//komodo-core-1", "https:///",
    "https://user:server-only-api-secret@komodo.example",
    "https://komodo.example/read", "https://komodo.example/?key=server-only-api-key",
    "https://komodo.example/#server-only-api-secret", "https://komodo.example:invalid",
    "https://komodo.example?", "https://komodo.example#",
    "https://komodo.example\\other", "https://komodo.example\n", "https://bad host",
])
def test_only_fixed_http_origins_are_allowed_and_errors_are_sanitized(url):
    with pytest.raises(ValueError) as error:
        KomodoClient(url, API_KEY, API_SECRET)
    assert str(error.value) == "PVE_KOMODO_URL must be a fixed HTTP(S) origin"
    assert API_KEY not in repr(error.value)
    assert API_SECRET not in repr(error.value)


def test_client_repr_never_contains_configuration_or_credentials():
    client = KomodoClient(ORIGIN, API_KEY, API_SECRET)
    assert repr(client) == "KomodoClient(configured=True)"
    assert_private({"representation": repr(client)})


@pytest.mark.parametrize("status,code", [
    (401, "invalid_credentials"), (403, "invalid_credentials"),
    (404, "upstream_unavailable"), (429, "upstream_unavailable"), (500, "upstream_unavailable"),
])
def test_failed_user_responses_do_not_echo_upstream_body(status, code):
    result = check(lambda request: httpx.Response(status, text=API_SECRET + API_KEY + ORIGIN))
    assert result == {"configured": True, "connected": False, "error_code": code}
    assert_private(result)


@pytest.mark.parametrize("user,code", [
    ({"username": "service", "enabled": False}, "invalid_credentials"),
    ({"username": "service"}, "invalid_response"),
    ({"username": "service", "enabled": "true"}, "invalid_response"),
    ({"username": "service", "enabled": 1}, "invalid_response"),
    ({"username": "", "enabled": True}, "invalid_response"),
    ({"username": None, "enabled": True}, "invalid_response"),
    ({"username": "service\nprivate", "enabled": True}, "invalid_response"),
    ([], "invalid_response"), (None, "invalid_response"),
])
def test_user_must_be_enabled_and_well_formed_before_version_request(user, code):
    def upstream(request):
        assert request.url.path == "/user"
        return httpx.Response(200, json=user)

    result = check(upstream)
    assert result == {"configured": True, "connected": False, "error_code": code}
    assert_private(result)


def test_invalid_json_is_sanitized():
    result = check(lambda request: httpx.Response(200, text=API_SECRET))
    assert result["error_code"] == "invalid_response"
    assert_private(result)


@pytest.mark.parametrize("body", [API_SECRET, '{"version":"2.3.3"}', "<html>2.3.3</html>", "2.3.3\nprivate", ""])
def test_version_must_be_plain_version_text(body):
    def upstream(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"username": "service", "enabled": True})
        return httpx.Response(200, text=body)

    result = check(upstream)
    assert result["error_code"] == "invalid_response"
    assert_private(result)


@pytest.mark.parametrize("path", ["/user", "/version"])
def test_redirects_are_not_followed_even_when_the_destination_is_a_different_origin(path):
    seen = []

    def upstream(request):
        seen.append(str(request.url))
        if request.url.path == path:
            return httpx.Response(302, headers={"location": "https://untrusted.example/" + API_SECRET})
        assert request.url.path == "/user"
        return httpx.Response(200, json={"username": "service", "enabled": True})

    result = check(upstream)
    assert result["error_code"] == "upstream_unavailable"
    assert all(value.startswith(ORIGIN) for value in seen)
    assert len(seen) == (1 if path == "/user" else 2)
    assert_private(result)


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_network_failures_never_expose_exception_details_or_credentials(error_type, caplog):
    def upstream(request):
        raise error_type(API_SECRET + API_KEY + ORIGIN, request=request)

    result = check(upstream)
    assert result["error_code"] == "upstream_unavailable"
    assert_private(result)
    assert API_KEY not in caplog.text
    assert API_SECRET not in caplog.text


def test_pve_session_and_komodo_credentials_do_not_cross_upstreams():
    seen = []

    def upstream(request):
        seen.append(request)
        if request.url.host == "pve.example":
            assert request.headers["cookie"] == "PVEAuthCookie=pve-ticket"
            assert "x-api-key" not in request.headers
            assert "x-api-secret" not in request.headers
            return httpx.Response(200, json={"data": []})
        assert request.url.host == "komodo-core-1"
        assert "cookie" not in request.headers
        if request.url.path == "/user":
            return httpx.Response(200, json={"username": "service", "enabled": True})
        return httpx.Response(200, text="2.3.3")

    async def scenario():
        transport = httpx.MockTransport(upstream)
        async with httpx.AsyncClient(transport=transport, cookies={"PVEAuthCookie": "pve-ticket"}) as pve:
            await pve.get("https://pve.example:8006/api2/json/access/permissions")
            komodo = KomodoClient(ORIGIN, API_KEY, API_SECRET, transport=transport)
            assert (await komodo.connection_status())["connected"] is True
            assert (await komodo.connection_status())["connected"] is True
            await pve.get("https://pve.example:8006/api2/json/access/permissions")

    asyncio.run(scenario())
    assert len(seen) == 6


def test_client_ignores_environment_proxies_and_uses_explicit_tls_context(monkeypatch):
    original_client = httpx.AsyncClient
    captured = []
    context = ssl.create_default_context()

    def client_factory(**kwargs):
        captured.append(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr("plugins.komodo.komodo.httpx.AsyncClient", client_factory)

    def upstream(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"username": "service", "enabled": True})
        return httpx.Response(200, text="2.3.3")

    client = KomodoClient(ORIGIN, API_KEY, API_SECRET, verify=context, transport=httpx.MockTransport(upstream))
    assert asyncio.run(client.connection_status())["connected"] is True
    assert captured[0]["verify"] is context
    assert captured[0]["trust_env"] is False
    assert captured[0]["follow_redirects"] is False
