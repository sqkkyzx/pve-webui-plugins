import asyncio
import sqlite3
from urllib.parse import parse_qs, quote

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import create_app
from app.pve_upstream import (
    FAILURE_COOLDOWN_SECONDS, PveFailoverTransport, PveUpstreamState,
    VERIFY_TICKET_EXTENSION, parse_upstream_nodes, parse_upstream_server_names,
)
from app.settings import Settings


PRIMARY = "https://192.0.2.10:8006"
BACKUP = "https://192.0.2.11:8006"
NODES = (PRIMARY, BACKUP)
TICKET = "PVE:root@pam:1234ABCD::signature+value="
COOKIE = "PVEAuthCookie=" + quote(TICKET, safe="")
DESKTOP = b"""<html><head><script src="/pve2/js/pvemanagerlib.js?ver=9.2.1"></script>
<script>Ext.History.fieldid = 'x-history-field';
Ext.onReady(function() { Ext.create('PVE.StdWorkspace');});</script></head></html>"""


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("value,expected", [
    ("192.0.2.10", (PRIMARY,)),
    ("192.0.2.10:8443, 192.0.2.11", ("https://192.0.2.10:8443", BACKUP)),
    ("2001:db8::1", ("https://[2001:db8::1]:8006",)),
    ("[2001:0db8:0:0::1]:8443", ("https://[2001:db8::1]:8443",)),
    ("[::1],127.0.0.1:1", ("https://[::1]:8006", "https://127.0.0.1:1")),
])
def test_parse_ip_nodes(value, expected):
    assert parse_upstream_nodes(value) == expected


@pytest.mark.parametrize("value", [
    None, "", " ", ",", "192.0.2.10,", ",192.0.2.10", "192.0.2.10,,192.0.2.11",
    "https://192.0.2.10:8006", "http://192.0.2.10", "pve.example", "user@192.0.2.10",
    "192.0.2.10/", "192.0.2.10/path", "192.0.2.10?x=1", "192.0.2.10#x", "192.0.2.10\\foo",
    "192.0.02.10", "192.0.2.10:0", "192.0.2.10:65536", "192.0.2.10:-1", "192.0.2.10:",
    "192.0.2.10:+8006", "192.0.2.10:８００６", "192.0.2.10\n", "192.0. 2.10", "192.0.2.10\x00",
    "[192.0.2.10]:8006", "[2001:db8::1", "[2001:db8::1]:", "[2001:db8::1]:0",
    "[fe80::1%eth0]", "fe80::1%eth0", "[2001:db8::1]/", "192.0.2.10,192.0.2.10:8006",
    "[2001:db8::1],2001:0db8::1", ",".join("192.0.2." + str(index) for index in range(1, 18)),
])
def test_reject_invalid_environment_nodes(value):
    with pytest.raises(ValueError):
        parse_upstream_nodes(value)


@pytest.mark.parametrize("value,count,expected", [
    (None, 2, ()), ("", 2, ()), ("PVE-A.example, pve-b.example", 2, ("pve-a.example", "pve-b.example")),
    ("pve", 1, ("pve",)),
])
def test_parse_optional_positional_server_names(value, count, expected):
    assert parse_upstream_server_names(value, count) == expected


@pytest.mark.parametrize("value,count", [
    ("pve-a.example", 2), ("pve-a.example,", 2),
    ("192.0.2.10", 1), ("::1", 1), ("https://pve.example", 1), ("pve.example:8006", 1),
    ("*.example", 1), ("pve_example", 1), ("-pve.example", 1), ("pve-.example", 1),
    ("pve.example.", 1), ("pve..example", 1), ("pve.example\n", 1), ("pve.example;bad", 1),
    ("pve.example/path", 1), ("pve.example\x00", 1), ("pvé.example", 1),
    ("x" * 64 + ".example", 1), (".".join(["x" * 63] * 4), 1),
])
def test_reject_unsafe_or_misaligned_server_names(value, count):
    with pytest.raises(ValueError):
        parse_upstream_server_names(value, count)


def test_settings_environment_preserves_primary_identity_and_defaults_encrypted_unverified(monkeypatch):
    monkeypatch.setenv("PVE_UPSTREAM", "192.0.2.10,[2001:db8::2]:8443")
    monkeypatch.setenv("PVE_TLS_SERVER_NAMES", "pve-a.example,pve-b.example")
    monkeypatch.delenv("PVE_TLS_VERIFY", raising=False)
    monkeypatch.delenv("PVE_CA_FILE", raising=False)
    settings = Settings.from_env()
    assert settings.upstream == PRIMARY
    assert settings.upstreams == (PRIMARY, "https://[2001:db8::2]:8443")
    assert settings.upstream_server_names == ("pve-a.example", "pve-b.example")
    assert settings.tls_verify is False
    assert Settings("https://pve.invalid:8006").upstreams == ("https://pve.invalid:8006",)
    assert Settings("https://pve.invalid:8006").tls_verify is True
    monkeypatch.setenv("PVE_TLS_VERIFY", "true")
    assert Settings.from_env().tls_verify is True


@pytest.mark.parametrize("changes", [
    {"upstreams": (BACKUP, PRIMARY)}, {"upstreams": (PRIMARY, PRIMARY)},
    {"upstreams": [PRIMARY]}, {"upstream_server_names": ("pve.example",)},
    {"upstream_server_names": ["pve.example", "backup.example"]},
    {"upstream_server_names": ("pve.example", None)},
])
def test_settings_reject_invalid_explicit_node_mapping(changes):
    options = {"upstreams": NODES, **changes}
    with pytest.raises(ValueError):
        Settings(PRIMARY, **options)


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("failure", [502, 503, 504, httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError])
async def test_reads_fail_over_preserve_target_and_close_rejected_response(method, failure):
    calls, closed = [], []

    class RejectedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"unused"

        async def aclose(self):
            closed.append(True)

    def upstream(request):
        calls.append(request)
        assert request.url.raw_path == b"/api2/json/cluster/resources?type=vm&x=%2F"
        if request.url.host == "192.0.2.10":
            if isinstance(failure, int):
                return httpx.Response(failure, stream=RejectedBody())
            raise failure("private", request=request)
        assert request.headers["host"] == "192.0.2.11:8006"
        assert request.headers["cookie"] == COOKIE
        return httpx.Response(200, json={"data": []})

    pool = PveFailoverTransport(PveUpstreamState(NODES), transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=pool) as client:
        response = await client.request(method, PRIMARY + "/api2/json/cluster/resources?type=vm&x=%2F", headers={"cookie": COOKIE})
    assert response.status_code == 200
    assert [request.url.host for request in calls] == ["192.0.2.10", "192.0.2.11"]
    assert closed == ([True] if isinstance(failure, int) else [])


@pytest.mark.anyio
@pytest.mark.parametrize("status", [200, 302, 400, 401, 403, 404, 500])
async def test_application_responses_are_not_retried(status):
    calls = []

    def upstream(request):
        calls.append(request.url.host)
        return httpx.Response(status, headers={"location": "https://outside.invalid/"})

    pool = PveFailoverTransport(PveUpstreamState(NODES), transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=pool, follow_redirects=False) as client:
        assert (await client.get(PRIMARY)).status_code == status
    assert calls == ["192.0.2.10"]


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("failure", [503, httpx.ConnectError, httpx.ReadTimeout, httpx.WriteError])
async def test_general_writes_never_replay_but_failed_node_is_avoided_by_next_request(method, failure):
    calls = []

    def upstream(request):
        calls.append((request.method, request.url.host, request.content))
        if request.url.host == "192.0.2.10":
            if isinstance(failure, int):
                return httpx.Response(failure)
            raise failure("private", request=request)
        return httpx.Response(200)

    pool = PveFailoverTransport(PveUpstreamState(NODES), transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=pool) as client:
        if isinstance(failure, int):
            assert (await client.request(method, PRIMARY + "/write", content=b"write-once")).status_code == failure
        else:
            with pytest.raises(failure):
                await client.request(method, PRIMARY + "/write", content=b"write-once")
        assert calls == [(method, "192.0.2.10", b"write-once")]
        assert (await client.request(method, PRIMARY + "/write", content=b"new-write")).status_code == 200
    assert calls[-1] == (method, "192.0.2.11", b"new-write")


@pytest.mark.anyio
@pytest.mark.parametrize("failure,retry", [
    (httpx.ConnectError, True), (httpx.ConnectTimeout, True), (httpx.ReadTimeout, False),
    (httpx.WriteError, False), (httpx.RemoteProtocolError, False), (503, False), (401, False), (403, False),
])
async def test_only_connect_failures_allow_explicit_ticket_verification_retry(failure, retry):
    calls = []

    def upstream(request):
        calls.append(request)
        assert parse_qs(request.content.decode()) == {"username": ["root@pam"], "password": [TICKET], "path": ["/"], "privs": ["Sys.Audit"]}
        assert VERIFY_TICKET_EXTENSION not in request.extensions
        if request.url.host == "192.0.2.10":
            if isinstance(failure, int):
                return httpx.Response(failure)
            raise failure("private", request=request)
        return httpx.Response(200)

    pool = PveFailoverTransport(PveUpstreamState(NODES), transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=pool) as client:
        call = client.post(PRIMARY + "/api2/json/access/ticket", data={"username": "root@pam", "password": TICKET, "path": "/", "privs": "Sys.Audit"}, extensions={VERIFY_TICKET_EXTENSION: True})
        if not isinstance(failure, int) and not retry:
            with pytest.raises(failure):
                await call
        else:
            assert (await call).status_code == (200 if retry else failure)
    assert len(calls) == (2 if retry else 1)


@pytest.mark.anyio
async def test_health_is_shared_between_clients_and_primary_recovers_for_new_requests():
    now, calls = [100.0], []
    state = PveUpstreamState(NODES, clock=lambda: now[0])

    def upstream(request):
        calls.append(request.url.host)
        if len(calls) == 1:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200)

    first = PveFailoverTransport(state, transport=httpx.MockTransport(upstream))
    second = PveFailoverTransport(state, transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=first) as client:
        await client.get(PRIMARY)
    async with httpx.AsyncClient(transport=second) as client:
        await client.get(PRIMARY)
        now[0] += FAILURE_COOLDOWN_SECONDS
        await client.get(PRIMARY)
    assert calls == ["192.0.2.10", "192.0.2.11", "192.0.2.11", "192.0.2.10"]
    assert state.origins == NODES


@pytest.mark.anyio
async def test_aligned_sni_changes_with_target_without_leaking_marker_or_browser_override():
    calls = []

    def upstream(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200)

    pool = PveFailoverTransport(PveUpstreamState(NODES), server_names=("primary.example", "backup.example"), transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=pool, timeout=7) as client:
        assert (await client.get(PRIMARY, extensions={"sni_hostname": "outside.invalid"})).status_code == 200
    assert [request.extensions["sni_hostname"] for request in calls] == ["primary.example", "backup.example"]
    assert all(request.extensions["timeout"]["connect"] <= 2 for request in calls)
    assert all(request.extensions["timeout"]["read"] == 7 for request in calls)


@pytest.mark.anyio
async def test_total_header_budget_is_bounded_and_reserves_time_for_backup(monkeypatch):
    monkeypatch.setattr("app.pve_upstream.REQUEST_BUDGET_SECONDS", 0.05)
    calls = []

    async def upstream(request):
        calls.append(request.url.host)
        if len(calls) == 1:
            await asyncio.sleep(0.2)
        return httpx.Response(200)

    pool = PveFailoverTransport(PveUpstreamState(NODES), transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=pool) as client:
        async with asyncio.timeout(0.15):
            assert (await client.get(PRIMARY)).status_code == 200
    assert calls == ["192.0.2.10", "192.0.2.11"]


@pytest.mark.anyio
async def test_exhausted_budget_raises_sanitized_httpx_timeout_without_replaying_write(monkeypatch):
    monkeypatch.setattr("app.pve_upstream.REQUEST_BUDGET_SECONDS", 0.02)
    calls = []

    async def upstream(request):
        calls.append(request.url.host)
        await asyncio.sleep(1)

    pool = PveFailoverTransport(PveUpstreamState(NODES), transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=pool) as client:
        with pytest.raises(httpx.ReadTimeout, match="^PVE upstream .*timed out$"):
            await client.post(PRIMARY + "/write", content=b"private")
    assert calls == ["192.0.2.10"]


@pytest.mark.anyio
@pytest.mark.parametrize("target", ["https://outside.invalid/", "http://192.0.2.10:8006/", "https://user:secret@192.0.2.10:8006/"])
async def test_unconfigured_origin_never_receives_cookies(target):
    def unexpected(request):
        pytest.fail("unconfigured origin was contacted")

    pool = PveFailoverTransport(PveUpstreamState(NODES), transport=httpx.MockTransport(unexpected))
    async with httpx.AsyncClient(transport=pool) as client:
        with pytest.raises(httpx.UnsupportedProtocol):
            await client.get(target, headers={"cookie": COOKIE})


@pytest.mark.anyio
async def test_callers_keep_streaming_limits_and_body_timeout_after_headers():
    reads = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append(True)
            yield b"one"
            yield b"two"

    def upstream(request):
        assert request.extensions["timeout"]["read"] == 17
        return httpx.Response(200, stream=Body())

    pool = PveFailoverTransport(PveUpstreamState(NODES), transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=pool, timeout=17) as client:
        async with client.stream("GET", PRIMARY) as response:
            assert reads == []
            assert await response.aread() == b"onetwo"


@pytest.mark.anyio
async def test_partial_response_failure_is_not_replayed_and_next_request_uses_backup():
    calls = []

    class InterruptedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("connection interrupted")

    def upstream(request):
        calls.append(request.url.host)
        if request.url.host == "192.0.2.10":
            return httpx.Response(200, stream=InterruptedBody())
        return httpx.Response(200, content=b"complete")

    pool = PveFailoverTransport(PveUpstreamState(NODES), transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=pool) as client:
        with pytest.raises(httpx.ReadError):
            await client.get(PRIMARY)
        assert calls == ["192.0.2.10"]
        assert (await client.get(PRIMARY)).content == b"complete"
    assert calls == ["192.0.2.10", "192.0.2.11"]


def test_primary_down_desktop_session_guest_and_temperature_discovery_share_failover_and_stable_store_identity(tmp_path, monkeypatch):
    calls, exporter_calls = [], []
    server_id = "a" * 24

    def pve(request):
        calls.append((request.url.host, request.method, request.url.path))
        if request.url.host == "192.0.2.10":
            raise httpx.ConnectError("primary down", request=request)
        assert request.headers["host"] == "192.0.2.11:8006"
        if request.url.path == "/":
            return httpx.Response(200, content=DESKTOP, headers={"content-type": "text/html", "set-cookie": "unrelated=private"})
        assert request.headers["cookie"] == COOKIE
        assert "authorization" not in request.headers and "x-api-key" not in request.headers
        data = {
            "/api2/json/access/permissions": {"/": {"Sys.Audit": 1}},
            "/api2/json/access/ticket": {"username": "root@pam"},
            "/api2/json/cluster/resources": [{"type": "qemu", "vmid": 101, "node": "pve-a", "name": "guest", "status": "running"}],
            "/api2/json/nodes/pve-a/qemu/101/config": {"smbios1": "uuid=9f376f89-d6b7-4dc8-84a4-f4769dc9b9bb"},
            "/api2/json/cluster/status": [{"type": "node", "name": "pve-a", "online": 1, "ip": "10.20.30.40"}],
        }[request.url.path]
        return httpx.Response(200, json={"data": data}, headers={"set-cookie": "unrelated=private"})

    async def servers(self):
        return [{"id": server_id, "name": "server", "template": False}]

    def exporter(request):
        exporter_calls.append(request)
        assert str(request.url) == "http://10.20.30.40:9100/metrics?collect%5B%5D=hwmon"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(200, text='node_hwmon_temp_celsius{chip="chip",sensor="temp1"} 42\n')

    monkeypatch.setattr("plugins.komodo.komodo.KomodoClient.link_servers", servers)
    settings = Settings(PRIMARY, upstreams=NODES, enabled_plugins=("komodo", "temperatures", "sites"), tls_verify=False,
        config_state_file=str(tmp_path / "plugins.json"), komodo_url="http://komodo.invalid:9120",
        komodo_api_key="key", komodo_api_secret="secret")
    shared = httpx.MockTransport(pve)
    application = create_app(settings, transport=shared, pve_auth_transport=shared, temperature_transport=httpx.MockTransport(exporter))
    with TestClient(application) as client:
        assert client.get("/").headers["x-pve-plugins-extension"] == "enabled"
        client.cookies.clear()
        assert client.get("/pve-plugins/api/session", headers={"cookie": COOKIE}).json() == {"user": "root@pam"}
        initial = client.get("/pve-plugins/api/komodo/guest-links", headers={"cookie": COOKIE})
        assert initial.status_code == 200
        assert initial.json()["guests"][0]["vmid"] == 101
        response = client.post("/pve-plugins/api/komodo/guest-links", headers={"cookie": COOKIE, "origin": "http://testserver", "x-pve-plugins-csrf": initial.json()["csrf_token"]}, json={"server_id": server_id, "guest_type": "qemu", "vmid": 101, "expected_revision": 0})
        assert response.status_code == 200
        cluster_id = response.json()["link"]["cluster_id"]
        temperatures = client.get("/pve-plugins/api/temperatures/nodes/pve-a", headers={"cookie": COOKIE})
        assert temperatures.status_code == 200
        assert exporter_calls
        assert settings.upstream == PRIMARY and settings.upstreams == NODES
    assert sum(host == "192.0.2.10" for host, _, _ in calls) == 1
    assert all(host == "192.0.2.11" for host, _, path in calls if path.startswith("/api2/"))
    with sqlite3.connect(tmp_path / "guest-links.sqlite3") as database:
        values = database.execute("SELECT key, value FROM guest_link_meta").fetchall()
    assert ("pve_origin", PRIMARY) in values
    assert ("cluster_id", cluster_id) in values


def test_application_lifespan_closes_owned_shared_pool_once_and_never_shares_client_cookies(monkeypatch):
    pools, cookies = [], []

    class Pool(httpx.AsyncBaseTransport):
        def __init__(self, **options):
            assert options["verify"] is False
            assert options["trust_env"] is False and options["retries"] == 0
            self.close_count = 0
            pools.append(self)

        async def handle_async_request(self, request):
            cookies.append(request.headers.get("cookie"))
            if request.url.path == "/":
                return httpx.Response(200, content=DESKTOP, headers={"content-type": "text/html", "set-cookie": "private=hidden"})
            if request.method == "GET":
                return httpx.Response(200, json={"data": {"/": {"Sys.Audit": 1}}}, headers={"set-cookie": "private=hidden"})
            return httpx.Response(200, json={"data": {"username": "root@pam"}})

        async def aclose(self):
            self.close_count += 1

    monkeypatch.setattr("app.pve_upstream.httpx.AsyncHTTPTransport", Pool)
    application = create_app(Settings(PRIMARY, upstreams=NODES, tls_verify=False))
    assert pools == []
    with TestClient(application) as client:
        client.get("/")
        client.cookies.clear()
        client.get("/")
        client.get("/pve-plugins/api/session", headers={"cookie": COOKIE})
        assert len(pools) == 1 and pools[0].close_count == 0
    assert pools[0].close_count == 1
    assert cookies == [None, None, COOKIE, COOKIE]


def test_application_never_closes_injected_shared_transport():
    class ExternalTransport(httpx.AsyncBaseTransport):
        close_count = 0

        async def handle_async_request(self, request):
            return httpx.Response(200, content=DESKTOP, headers={"content-type": "text/html"})

        async def aclose(self):
            self.close_count += 1

    external = ExternalTransport()
    application = create_app(Settings(PRIMARY, upstreams=NODES, tls_verify=False), transport=external, pve_auth_transport=external)
    with TestClient(application) as client:
        assert client.get("/").status_code == 200
        assert client.get("/").status_code == 200
        assert external.close_count == 0
    assert external.close_count == 0
