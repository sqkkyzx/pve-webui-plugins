"""Only the current authenticated PVE inventory may choose an exporter host."""

import asyncio
from dataclasses import FrozenInstanceError
import ssl
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from plugins.temperatures.node_temperature_discovery import DiscoveryError, DiscoveryTarget, NodeTemperatureDiscovery
import plugins.temperatures.node_temperature_discovery as module


PVE = "https://pve.example:8006"
COOKIE = "PVE%3Aroot%40pam%3A1234ABCD%3A%3Afixture-signature%3D"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def request(cookie=COOKIE, **headers):
    values = {"host": "browser-untrusted.example", "authorization": "Bearer browser-secret",
              "csrfpreventiontoken": "browser-csrf", "x-forwarded-host": "127.0.0.1:1234",
              "cookie": "other=not-forwarded; PVEAuthCookie=" + cookie, **headers}
    return Request({"type": "http", "method": "GET", "scheme": "https", "path": "/api/nodes/pve-a/temperatures",
                    "query_string": b"url=http://127.0.0.1:1234&ip=127.0.0.1", "headers": [
                        (name.encode(), value.encode()) for name, value in values.items()]})


def node(name="pve-a", ip="192.168.100.1", online=1, **extra):
    return {"type": "node", "name": name, "ip": ip, "online": online, **extra}


def discovery(handler, **auth_fields):
    return NodeTemperatureDiscovery(SimpleNamespace(upstream=PVE, verify=True, **auth_fields),
                                    transport=httpx.MockTransport(handler))


def response(rows):
    return httpx.Response(200, json={"data": rows})


@pytest.mark.anyio
async def test_current_cookie_only_reaches_fixed_pve_origin_and_standalone_maps_to_exporter():
    calls = []

    def pve(upstream_request):
        calls.append(upstream_request)
        assert str(upstream_request.url) == PVE + "/api2/json/cluster/status"
        assert upstream_request.method == "GET"
        assert upstream_request.headers["cookie"] == "PVEAuthCookie=" + COOKIE
        assert upstream_request.headers["accept"] == "application/json"
        assert upstream_request.headers["accept-encoding"] == "identity"
        for header in ("authorization", "csrfpreventiontoken", "x-forwarded-host"):
            assert header not in upstream_request.headers
        # This is also the shape PVE returns when no cluster is configured.
        return response([node(local=1, nodeid=0, id="node/pve-a")])

    target = await discovery(pve).resolve(request(), "pve-a")
    assert target == DiscoveryTarget("pve-a", "http://192.168.100.1:9100", "192.168.100.1")
    assert len(calls) == 1  # Discovery never probes the exporter or other interfaces.
    assert "192.168.100.1" not in repr(target)
    with pytest.raises(FrozenInstanceError):
        target.origin = "http://127.0.0.1:1234"


@pytest.mark.anyio
@pytest.mark.parametrize("address,origin", [
    ("10.20.30.40", "http://10.20.30.40:9100"),
    ("172.16.1.10", "http://172.16.1.10:9100"),
    ("192.168.1.10", "http://192.168.1.10:9100"),
    ("100.90.80.10", "http://100.90.80.10:9100"),
    ("8.8.8.8", "http://8.8.8.8:9100"),
    ("fd00:0:0:1::23", "http://[fd00:0:0:1::23]:9100"),
    ("2001:4860:4860:0000:0000:0000:0000:8888", "http://[2001:4860:4860::8888]:9100"),
])
async def test_verified_private_cgnat_public_and_ipv6_addresses(address, origin):
    target = await discovery(lambda _: response([node(ip=address)])).resolve(request(), "pve-a")
    assert target.origin == origin
    assert target.address == origin.removeprefix("http://").removesuffix(":9100").strip("[]")


@pytest.mark.anyio
@pytest.mark.parametrize("address", [
    None, True, 3232235777, "", "localhost", "pve-a.example", "https://192.168.1.10", "192.168.1.10:9100",
    "192.168.01.10", "192.168.1.10/24", " 192.168.1.10", "192.168.1.10\n", "192.168.1.10@host",
    "127.0.0.1", "127.9.8.7", "0.0.0.0", "169.254.169.254", "224.0.0.1", "239.0.0.1",
    "240.1.2.3", "255.255.255.255", "::", "::1", "fe80::1", "fe80::1%eth0", "fd00::1%eth0",
    "ff02::1", "100::1", "::ffff:127.0.0.1", "::ffff:192.168.1.10", "[fd00::1]",
])
async def test_no_dns_url_metadata_loopback_scoped_or_reserved_targets(address):
    calls = []

    def pve(upstream_request):
        calls.append(str(upstream_request.url))
        return response([node(ip=address)])

    with pytest.raises(DiscoveryError, match="^node_temperature_discovery_failed$"):
        await discovery(pve).resolve(request(), "pve-a")
    assert calls == [PVE + "/api2/json/cluster/status"]


@pytest.mark.anyio
@pytest.mark.parametrize("rows", [
    [], [node(name="pve-b")], [node(online=0)], [node(online=False)],
    [{"type": "node", "name": "pve-a", "online": 0}],
    [{"type": "cluster", "name": "pve-a", "ip": "192.168.100.1", "online": 1}],
    [node(name="PVE-A")],
])
async def test_exact_node_type_and_name_required_and_absent_or_offline_returns_none(rows):
    assert await discovery(lambda _: response(rows)).resolve(request(), "pve-a") is None


@pytest.mark.anyio
@pytest.mark.parametrize("rows", [
    [node(), node()], [node(), node(ip="192.168.100.2")],
    [node(), node(online=0)], [node(), node(name="pve-b")],
    [node(ip="fd00::1"), node(name="pve-b", ip="fd00:0:0:0:0:0:0:1")],
    [node(online="1")], [node(online=1.0)], [node(online=None)], [node(online=2)],
    [{"type": "node", "name": "pve-a", "ip": "192.168.100.1"}],
    [node(name="bad/node")], [node(name=None)], [None],
])
async def test_duplicate_conflicting_or_malformed_inventory_is_an_error(rows):
    with pytest.raises(DiscoveryError):
        await discovery(lambda _: response(rows)).resolve(request(), "pve-a")


@pytest.mark.anyio
async def test_cluster_summary_is_not_a_duplicate_node_and_boolean_online_is_supported():
    rows = [{"type": "cluster", "name": "pve-a", "online": 0}, node(online=True),
            node(name="pve-b", online=0, ip=None)]
    assert (await discovery(lambda _: response(rows)).resolve(request(), "pve-a")).address == "192.168.100.1"


@pytest.mark.anyio
async def test_resolves_every_request_using_its_ticket_and_never_reuses_previous_node_ip():
    cookies, ips = [], iter(["10.0.0.1", "10.0.0.2", "10.0.0.3"])

    def pve(upstream_request):
        cookies.append(upstream_request.headers["cookie"])
        return response([node(ip=next(ips))])

    service = discovery(pve)
    first = await service.resolve(request(), "pve-a")
    second = await service.resolve(request("another-current-ticket"), "pve-a")
    third = await service.resolve(request(), "pve-a")
    assert [first.origin, second.origin, third.origin] == ["http://10.0.0.1:9100", "http://10.0.0.2:9100", "http://10.0.0.3:9100"]
    assert cookies == ["PVEAuthCookie=" + COOKIE, "PVEAuthCookie=another-current-ticket", "PVEAuthCookie=" + COOKIE]


@pytest.mark.anyio
async def test_concurrent_users_and_nodes_do_not_share_a_discovered_mapping():
    async def pve(upstream_request):
        await asyncio.sleep(0)
        if upstream_request.headers["cookie"].endswith("alice"):
            return response([node(ip="10.0.0.1"), node(name="pve-b", ip="10.0.0.2")])
        return response([node(ip="10.0.1.1"), node(name="pve-b", ip="10.0.1.2")])

    service = discovery(pve)
    targets = await asyncio.gather(service.resolve(request("alice"), "pve-a"),
                                   service.resolve(request("bob"), "pve-a"),
                                   service.resolve(request("bob"), "pve-b"))
    assert [target.address for target in targets] == ["10.0.0.1", "10.0.1.1", "10.0.1.2"]


@pytest.mark.anyio
@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 404, 500])
async def test_rejects_errors_and_never_follows_redirect_or_carries_ticket_to_other_origin(status):
    calls = []

    def pve(upstream_request):
        calls.append(upstream_request)
        return httpx.Response(status, headers={"location": "http://127.0.0.1:9100/metrics"}, text="private-response")

    with pytest.raises(DiscoveryError) as error:
        await discovery(pve).resolve(request(), "pve-a")
    assert len(calls) == 1
    assert str(error.value) == "node_temperature_discovery_failed"
    assert error.value.code == "node_temperature_discovery_failed"


@pytest.mark.anyio
@pytest.mark.parametrize("payload", [
    {}, {"data": {}}, {"data": None}, {"data": [], "errors": {}},
    {"data": [], "success": False}, {"data": [], "success": 1},
    {"data": [], "NeedTFA": True}, [], None,
])
async def test_invalid_pve_envelope_is_not_silently_reported_as_no_node(payload):
    with pytest.raises(DiscoveryError):
        await discovery(lambda _: httpx.Response(200, json=payload)).resolve(request(), "pve-a")


@pytest.mark.anyio
@pytest.mark.parametrize("body", [
    b"not-json", b'{"data":[],"data":[]}', b'{"data":[{"type":"node","name":"pve-a","name":"pve-b"}]}',
    b'{"data":[],"number":NaN}', b'{"data":[],"number":Infinity}', b"[" * 2000 + b"]" * 2000,
])
async def test_malformed_duplicate_nonfinite_or_deep_json_is_a_fixed_error(body):
    with pytest.raises(DiscoveryError):
        await discovery(lambda _: httpx.Response(200, content=body)).resolve(request(), "pve-a")


class Chunks(httpx.AsyncByteStream):
    def __init__(self, values, *, delay=0):
        self.values, self.delay, self.closed, self.reads = values, delay, False, 0

    async def __aiter__(self):
        for value in self.values:
            self.reads += 1
            await asyncio.sleep(self.delay)
            yield value

    async def aclose(self):
        self.closed = True


@pytest.mark.anyio
async def test_streamed_body_is_bounded_and_closed_without_reading_remaining_chunks():
    stream = Chunks([b"x" * 131_072, b"x" * 131_073, b"must-not-read"])
    with pytest.raises(DiscoveryError):
        await discovery(lambda _: httpx.Response(200, stream=stream)).resolve(request(), "pve-a")
    assert stream.closed and stream.reads == 2


@pytest.mark.anyio
@pytest.mark.parametrize("headers", [
    {"content-length": "262145"}, {"content-length": "-1"}, {"content-length": "bad"},
    {"content-length": "9" * 5000}, {"content-encoding": "gzip"}, {"content-encoding": "br"},
])
async def test_rejects_bad_length_or_compression_before_stream_read(headers):
    stream = Chunks([b"do-not-read"])
    with pytest.raises(DiscoveryError):
        await discovery(lambda _: httpx.Response(200, headers=headers, stream=stream)).resolve(request(), "pve-a")
    assert stream.closed and stream.reads == 0


@pytest.mark.anyio
async def test_total_timeout_bounds_a_slow_stream_and_closes_it(monkeypatch):
    monkeypatch.setattr(module, "_TIMEOUT_SECONDS", 0.02)
    stream = Chunks([b'{"data":', b"[]}"], delay=0.015)
    with pytest.raises(DiscoveryError):
        await discovery(lambda _: httpx.Response(200, stream=stream)).resolve(request(), "pve-a")
    assert stream.closed


@pytest.mark.anyio
async def test_transport_failure_never_exposes_ticket_or_private_response():
    def pve(upstream_request):
        raise httpx.ConnectError("private-response " + COOKIE, request=upstream_request)

    with pytest.raises(DiscoveryError) as error:
        await discovery(pve).resolve(request(), "pve-a")
    assert str(error.value) == "node_temperature_discovery_failed"
    assert COOKIE not in repr(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__


@pytest.mark.anyio
async def test_pve_tls_transport_and_environment_proxy_policy_are_preserved(monkeypatch):
    real_client, seen = httpx.AsyncClient, []
    context = ssl.create_default_context()

    def client(**kwargs):
        seen.append(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", client)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    transport = httpx.MockTransport(lambda _: response([node()]))
    service = NodeTemperatureDiscovery(SimpleNamespace(upstream=PVE + "/", verify=context, transport=transport))
    await service.resolve(request(), "pve-a")
    assert seen[0]["verify"] is context and seen[0]["transport"] is transport
    assert seen[0]["trust_env"] is False and seen[0]["follow_redirects"] is False
    assert seen[0]["timeout"].connect == 5 and seen[0]["timeout"].read == 5


@pytest.mark.anyio
@pytest.mark.parametrize("name,cookie", [
    ("pve-a/../../other", COOKIE), ("", COOKIE), (None, COOKIE), ("a" * 65, COOKIE),
    ("pve-a", ""), ("pve-a", "x" * 4097), ("pve-a", "bad;cookie"),
])
async def test_invalid_node_or_missing_cookie_fails_before_contacting_pve(name, cookie):
    def unexpected(_):
        pytest.fail("invalid request must not contact PVE")

    with pytest.raises(DiscoveryError):
        await discovery(unexpected).resolve(SimpleNamespace(cookies={"PVEAuthCookie": cookie}), name)


@pytest.mark.parametrize("origin", [
    "http://pve.example:8006", "https://user:password@pve.example", "https://pve.example/path",
    "https://pve.example?target=other", "https://pve.example#fragment",
])
def test_constructor_only_accepts_fixed_https_pve_origin(origin):
    with pytest.raises(ValueError):
        NodeTemperatureDiscovery(SimpleNamespace(upstream=origin, verify=True))
