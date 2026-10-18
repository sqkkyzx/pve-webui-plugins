"""Collector configuration, truthful readings, bounded fetches and caching."""

import asyncio
import gzip
import json
import ssl

import certifi
import httpx
import pytest

import plugins.temperatures.temperatures as module
from plugins.temperatures.temperatures import TemperatureService, load_temperature_sources, validate_temperatures


NOW = 1_792_300_000
TOKEN = "collector-fixture-token-" + "x" * 32


def sensor(**changes):
    return {"id": "cpu-0", "chip": "coretemp", "label": "Package id 0", "category": "cpu", "celsius": 42.5,
            "max_celsius": 90, "critical_celsius": 100, **changes}


def payload(**changes):
    return {"version": 1, "node": "pve-a", "sampled_at": NOW, "sensors": [sensor()], **changes}


def sources(tmp_path, data=None):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(data if data is not None else {"nodes": {"pve-a": {"url": "https://collector.example:9469", "token": TOKEN}}}))
    return load_temperature_sources(path)


def get(service, node="pve-a"):
    return asyncio.run(service.get(node))


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr(module, "_now", lambda: NOW)


def test_missing_file_setting_is_explicitly_unconfigured():
    assert load_temperature_sources(None) == {}
    assert get(TemperatureService({})) == {"version": 1, "node": "pve-a", "sampled_at": None,
                                         "sensors": [], "status": "not_configured"}


def test_origin_token_and_tls_configuration_are_validated_once(tmp_path):
    configured = sources(tmp_path)
    entry = configured["pve-a"]
    assert entry.origin == "https://collector.example:9469" and entry.token == TOKEN
    assert entry.verify.verify_mode == ssl.CERT_REQUIRED and entry.verify.check_hostname
    assert TOKEN not in repr(entry)


def test_node_exporter_override_has_no_required_collector_token(tmp_path):
    configured = sources(tmp_path, {"nodes": {"pve-a": {
        "type": "node_exporter", "url": "https://metrics.example:19100",
    }}})
    entry = configured["pve-a"]
    assert entry.kind == "node_exporter" and entry.token is None
    assert entry.verify.verify_mode == ssl.CERT_REQUIRED and entry.verify.check_hostname


@pytest.mark.parametrize("kind", ["prometheus", "", None, True, [], {}])
def test_unknown_source_format_cannot_bypass_configuration_validation(tmp_path, kind):
    with pytest.raises(ValueError, match="Invalid PVE_TEMPERATURES_SOURCES_FILE configuration"):
        sources(tmp_path, {"nodes": {"pve-a": {"type": kind, "url": "https://metrics.example"}}})


@pytest.mark.parametrize("origin,allow_http", [("http://127.0.0.1:9469", False), ("http://[::1]:9469", False),
    ("http://localhost:9469/", False), ("http://collector.example:9469", True)])
def test_plain_http_requires_loopback_or_explicit_opt_in(tmp_path, origin, allow_http):
    assert sources(tmp_path, {"nodes": {"pve-a": {"url": origin, "token": TOKEN, "allow_http": allow_http}}})


@pytest.mark.parametrize("change", [
    {"url": "http://collector.example:9469"}, {"url": "http://127.1:9469"}, {"url": "file:///etc/passwd"},
    {"url": "https://user:password@collector.example"}, {"url": "https://collector.example/path"},
    {"url": "https://collector.example?key=private"}, {"url": "https://collector.example#private"},
    {"url": "https://collector.example?"}, {"url": "https://collector.example#"},
    {"url": "https://collector.example:0"}, {"url": "https://collector.example:70000"},
    {"url": "https://collector.example:"}, {"url": "https://collector.example\\private"},
    {"url": "https://collector.example\n"}, {"token": "short"}, {"token": "t" * 257},
    {"token": TOKEN + " "}, {"token": TOKEN + "\n"}, {"token": True}, {"allow_http": "true"},
    {"allow_http": 1}, {"verify": False}, {"ca_file": False}, {"ca_file": "missing-private-ca.pem"},
])
def test_bad_source_config_is_startup_value_error_without_value_disclosure(tmp_path, change):
    with pytest.raises(ValueError) as error:
        sources(tmp_path, {"nodes": {"pve-a": {"url": "https://collector.example", "token": TOKEN, **change}}})
    assert str(error.value) == "Invalid PVE_TEMPERATURES_SOURCES_FILE configuration"
    assert TOKEN not in str(error.value) and "password" not in str(error.value)


@pytest.mark.parametrize("data", [{}, {"nodes": []}, {"nodes": {}, "debug": True}, {"nodes": {"../bad": {}}},
    {"nodes": {"pve-a": {}}}, {"nodes": {str(index): {"url": "https://example.test", "token": TOKEN} for index in range(65)}}])
def test_configuration_schema_and_node_limit_fail_closed(tmp_path, data):
    with pytest.raises(ValueError, match="Invalid PVE_TEMPERATURES_SOURCES_FILE configuration"):
        sources(tmp_path, data)


def test_duplicate_json_keys_and_oversized_config_are_rejected(tmp_path):
    path = tmp_path / "sources.json"
    for content in ('{"nodes":{},"nodes":{}}', '{"nodes":{"pve-a":{},"pve-a":{}}}', " " * 262145):
        path.write_text(content)
        with pytest.raises(ValueError, match="Invalid PVE_TEMPERATURES_SOURCES_FILE configuration"):
            load_temperature_sources(path)


def test_custom_ca_is_verified_and_relative_to_configuration_file(tmp_path):
    (tmp_path / "collector-ca.pem").write_bytes(__import__('pathlib').Path(certifi.where()).read_bytes())
    configured = sources(tmp_path, {"nodes": {"pve-a": {"url": "https://collector.example", "token": TOKEN, "ca_file": "collector-ca.pem"}}})
    assert configured["pve-a"].verify.verify_mode == ssl.CERT_REQUIRED and configured["pve-a"].verify.check_hostname


def test_reading_preserves_real_zero_negative_values_and_null_limits():
    data = payload(sensors=[sensor(celsius=0, max_celsius=None, critical_celsius=None), sensor(id="cpu-1", celsius=-5)])
    result = validate_temperatures(data, "pve-a", NOW)
    assert result["sensors"][0]["celsius"] == 0 and result["sensors"][0]["critical_celsius"] is None
    assert result["sensors"][1]["celsius"] == -5


@pytest.mark.parametrize("change", [
    {"version": True}, {"version": 2}, {"node": "pve-b"}, {"node": None}, {"sampled_at": True},
    {"sampled_at": float("nan")}, {"sampled_at": float("inf")}, {"sampled_at": 10 ** 1000},
    {"sampled_at": NOW + 11}, {"sampled_at": -1}, {"sensors": {}}, {"sensors": [sensor()] * 257},
    {"sensors": [sensor(), sensor()]}, {"secret": "unexpected"},
])
def test_invalid_payload_is_rejected_as_a_whole(change):
    with pytest.raises(ValueError, match="invalid_temperature_response"):
        validate_temperatures(payload(**change), "pve-a", NOW)


@pytest.mark.parametrize("change", [
    {"celsius": True}, {"celsius": "42"}, {"celsius": None}, {"celsius": float("nan")},
    {"celsius": float("inf")}, {"celsius": -101}, {"celsius": 251}, {"celsius": 10 ** 1000},
    {"max_celsius": False}, {"critical_celsius": "100"}, {"critical_celsius": float("nan")},
    {"id": "cpu / 0"}, {"id": "x" * 129}, {"chip": ""}, {"chip": "x" * 129},
    {"label": "label\nprivate"}, {"label": "invalid-\ud800"}, {"category": []}, {"category": "gpu"}, {"extra": "private"},
])
def test_bad_sensor_invalidates_entire_collection_instead_of_showing_partial_data(change):
    with pytest.raises(ValueError, match="invalid_temperature_response"):
        validate_temperatures(payload(sensors=[sensor(id="good"), sensor(**change)]), "pve-a", NOW)


def test_fixed_fetch_is_authenticated_and_never_sends_browser_or_other_source_credentials(tmp_path):
    configured = sources(tmp_path, {"nodes": {
        "pve-a": {"url": "https://collector-a.example", "token": TOKEN},
        "pve-b": {"url": "https://collector-b.example", "token": "other-fixture-token-" + "y" * 32}}})
    seen = []
    def handler(request):
        seen.append(request)
        node = "pve-a" if request.url.host == "collector-a.example" else "pve-b"
        assert request.method == "GET" and request.url.path == "/v1/temperatures" and request.url.query == b""
        assert request.headers["authorization"] == "Bearer " + configured[node].token
        assert "cookie" not in request.headers and "x-pve-plugins-csrf" not in request.headers and "x-api-key" not in request.headers
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, json=payload(node=node), headers={"set-cookie": "private-cookie=do-not-replay; Path=/"})
    service = TemperatureService(configured, transport=httpx.MockTransport(handler))
    assert get(service)["status"] == "ok" and get(service, "pve-b")["status"] == "ok"
    assert len(seen) == 2


@pytest.mark.parametrize("response", [httpx.Response(302, headers={"location": "https://other.example/private"}),
    httpx.Response(503, text=TOKEN), httpx.Response(200, text="invalid-json"),
    httpx.Response(200, content=b"{" + b" " * 262144), httpx.Response(200, content=gzip.compress(b"{}"), headers={"content-encoding": "gzip"})])
def test_fetch_errors_redirects_and_oversized_responses_are_unavailable(tmp_path, response):
    seen = []
    def handler(request):
        seen.append(request)
        return response
    result = get(TemperatureService(sources(tmp_path), transport=httpx.MockTransport(handler)))
    assert result["status"] == "unavailable" and result["sensors"] == [] and result["sampled_at"] is None
    assert TOKEN not in json.dumps(result) and "collector.example" not in json.dumps(result) and len(seen) == 1


def test_malformed_json_numbers_duplicates_and_partial_sensor_data_are_unavailable(tmp_path):
    configured = sources(tmp_path)
    raw = json.dumps(payload()).replace('"celsius": 42.5', '"celsius": NaN')
    for value in (raw, '{"version":1,"version":1}', json.dumps(payload(sensors=[sensor(), sensor(id="bad", celsius=True)]))):
        result = get(TemperatureService(configured, transport=httpx.MockTransport(lambda request: httpx.Response(200, content=value))))
        assert result["status"] == "unavailable" and result["sensors"] == []


def test_streaming_limit_stops_before_consuming_entire_unbounded_body(tmp_path):
    class Stream(httpx.AsyncByteStream):
        count, closed = 0, False
        async def __aiter__(self):
            for _ in range(20):
                self.count += 1
                yield b" " * 65536
        async def aclose(self):
            self.closed = True
    stream = Stream()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    assert get(TemperatureService(sources(tmp_path), transport=transport))["status"] == "unavailable"
    assert stream.count == 5 and stream.closed


@pytest.mark.parametrize("age,status", [(0, "ok"), (30, "ok"), (31, "stale"), (-10, "ok"), (-11, "unavailable")])
def test_sample_age_and_future_tolerance(tmp_path, age, status):
    result = get(TemperatureService(sources(tmp_path), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload(sampled_at=NOW - age)))))
    assert result["status"] == status
    assert bool(result["sensors"]) is (status == "ok")


def test_empty_valid_sample_is_distinct_from_unavailable(tmp_path):
    result = get(TemperatureService(sources(tmp_path), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload(sensors=[])))))
    assert result["status"] == "no_sensors" and result["sampled_at"] == NOW


def test_node_mismatch_never_shows_another_hosts_measurements(tmp_path):
    result = get(TemperatureService(sources(tmp_path), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload(node="pve-b")))))
    assert result["status"] == "unavailable" and result["sensors"] == []


def test_concurrent_requests_share_one_fetch_and_cache_expires_after_five_seconds(tmp_path, monkeypatch):
    clock = {"monotonic": 0.0, "calls": 0}
    monkeypatch.setattr(module, "_monotonic", lambda: clock["monotonic"])
    async def handler(request):
        clock["calls"] += 1
        await asyncio.sleep(0)
        return httpx.Response(200, json=payload())
    service = TemperatureService(sources(tmp_path), transport=httpx.MockTransport(handler))
    async def exercise():
        values = await asyncio.gather(*(service.get("pve-a") for _ in range(25)))
        assert all(value["status"] == "ok" for value in values) and clock["calls"] == 1
        clock["monotonic"] = 4.9
        await service.get("pve-a")
        assert clock["calls"] == 1
        clock["monotonic"] = 5.0
        await service.get("pve-a")
        assert clock["calls"] == 2
    asyncio.run(exercise())


def test_cached_sample_is_marked_stale_as_it_ages_even_before_cache_expires(tmp_path, monkeypatch):
    clock = {"now": NOW}
    monkeypatch.setattr(module, "_now", lambda: clock["now"])
    monkeypatch.setattr(module, "_monotonic", lambda: 0)
    service = TemperatureService(sources(tmp_path), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload(sampled_at=NOW - 29))))
    assert get(service)["status"] == "ok"
    clock["now"] += 2
    result = get(service)
    assert result["status"] == "stale" and result["sensors"] == []


def test_timeouts_and_transport_errors_are_cached_safe_unavailable(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(module, "_monotonic", lambda: 0)
    def handler(request):
        seen.append(request)
        raise httpx.ReadTimeout(TOKEN + " https://private.example")
    service = TemperatureService(sources(tmp_path), transport=httpx.MockTransport(handler))
    first, second = get(service), get(service)
    assert first == second and first["status"] == "unavailable" and len(seen) == 1
    assert TOKEN not in json.dumps(first) and "private.example" not in json.dumps(first)


@pytest.mark.parametrize("label", [TOKEN, "https://collector.example:9469"])
def test_reflected_configuration_secrets_are_rejected_even_inside_allowed_metadata(tmp_path, label):
    service = TemperatureService(sources(tmp_path), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload(sensors=[sensor(label=label)]))))
    result = get(service)
    assert result["status"] == "unavailable" and result["sensors"] == []
    assert label not in json.dumps(result)


def test_client_disables_environment_proxy_and_redirects_with_verified_tls(tmp_path, monkeypatch):
    configured = sources(tmp_path)
    original, options = httpx.AsyncClient, []
    def client(**kwargs):
        options.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(module.httpx, "AsyncClient", client)
    def handler(request):
        assert request.extensions["timeout"] == {"connect": 4.0, "read": 4.0, "write": 4.0, "pool": 4.0}
        return httpx.Response(200, json=payload())
    assert get(TemperatureService(configured, transport=httpx.MockTransport(handler)))["status"] == "ok"
    assert len(options) == 1 and options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert options[0]["verify"].verify_mode == ssl.CERT_REQUIRED and options[0]["verify"].check_hostname


def test_total_deadline_includes_response_streaming(tmp_path, monkeypatch):
    original = asyncio.timeout
    def short_timeout(seconds):
        assert seconds == 4
        return original(0.01)
    monkeypatch.setattr(module.asyncio, "timeout", short_timeout)
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{"
            await asyncio.sleep(1)
            yield b"}"
    service = TemperatureService(sources(tmp_path), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=SlowStream())))
    assert get(service)["status"] == "unavailable"
