"""Read-only collector with fake sensor sources and isolated local HTTP."""

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
import shutil
import socket
import ssl
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from plugins.temperatures.collectors import temperature_collector as collector


TOKEN = b"temperature-test-token-" + b"a" * 32


def sensors(values=None, *, chip="coretemp-isa-0000", label="Package id 0"):
    return json.dumps({chip: {"Adapter": "ISA adapter", label: {"temp1_input": 0, "temp1_max": 90,
        "temp1_crit": 105, **(values or {})}}}).encode()


def test_libsensors_labels_temperature_zero_and_limits_are_preserved():
    row = collector.parse_sensors_json(sensors())[0]
    assert row["chip"] == "coretemp-isa-0000" and row["label"] == "Package id 0"
    assert row["category"] == "cpu" and row["celsius"] == 0
    assert row["max_celsius"] == 90 and row["critical_celsius"] == 105
    assert set(row) == {"id", "chip", "label", "category", "celsius", "max_celsius", "critical_celsius"}
    assert collector.parse_sensors_json(sensors({"temp1_input": 122}))[0]["celsius"] == 122


@pytest.mark.parametrize("chip,category", [("coretemp-isa-0000", "cpu"), ("k10temp-pci-00c3", "cpu"),
    ("zenpower-pci-00c3", "cpu"), ("nvme-pci-0100", "disk"), ("drivetemp-scsi-0-0", "disk"),
    ("nct6798-isa-0290", "board"), ("it8728-isa-0290", "board"), ("it8628-isa-0290", "board"), ("acpitz-acpi-0", "other"), ("unknown", "other")])
def test_categories_come_from_known_chips_not_temp_number_or_cpu_label(chip, category):
    assert collector.parse_sensors_json(sensors(chip=chip, label="CPU temp1"))[0]["category"] == category


@pytest.mark.parametrize("value", [True, False, None, "55", float("nan"), float("inf"), float("-inf"), -100.1, 250.1])
def test_invalid_temperature_does_not_become_zero(value):
    assert collector.parse_sensors_json(sensors({"temp1_input": value})) == []


@pytest.mark.parametrize("value", [-100, 0, 250])
def test_boundary_temperatures_are_real_values(value):
    assert collector.parse_sensors_json(sensors({"temp1_input": value}))[0]["celsius"] == value


def test_unknown_limits_are_null_and_zero_limits_are_kept():
    row = collector.parse_sensors_json(sensors({"temp1_max": False, "temp1_crit": float("nan")}))[0]
    assert row["max_celsius"] is row["critical_celsius"] is None
    row = collector.parse_sensors_json(sensors({"temp1_max": 0, "temp1_crit": None}))[0]
    assert row["max_celsius"] == 0 and row["critical_celsius"] is None


def test_sensor_identity_is_stable_across_label_and_reading_changes_but_separates_chips():
    first = collector.parse_sensors_json(sensors())[0]
    changed = collector.parse_sensors_json(sensors({"temp1_input": 44}, label="renamed CPU package"))[0]
    second_chip = collector.parse_sensors_json(sensors(chip="coretemp-isa-0001"))[0]
    assert first["id"] == changed["id"] and first["id"] != second_chip["id"]


def test_duplicate_channels_are_not_returned_and_non_temperature_metrics_ignored():
    payload = {"chip": {"One": {"temp1_input": 30, "fan1_input": 500, "in1_input": 1},
                        "Two": {"temp1_input": 30}, "Three": {"fan2_input": 99}}}
    rows = collector.parse_sensors_json(json.dumps(payload).encode())
    assert len(rows) == 1


def test_fault_and_disabled_channels_are_not_read_as_valid_temperatures():
    assert collector.parse_sensors_json(sensors({"temp1_fault": 1})) == []
    assert collector.parse_sensors_json(sensors({"temp1_fault": 0.5})) == []
    assert collector.parse_sensors_json(sensors({"temp1_enable": 0})) == []
    assert collector.parse_sensors_json(sensors({"temp1_fault": 0, "temp1_crit_alarm": 1}))[0]["celsius"] == 0


@pytest.mark.parametrize("payload", [b"null", b"[]", b"not JSON", b"\xff", b"{" * 2000])
def test_bad_sources_fail_with_fixed_errors(payload):
    with pytest.raises(collector.SampleError, match="temperature_read_failed"):
        collector.parse_sensors_json(payload)


def test_channel_text_input_and_response_sizes_are_bounded():
    features = {"x" * 500 + str(index): {f"temp{index+1}_input": 12.5} for index in range(300)}
    rows = collector.parse_sensors_json(json.dumps({"\\" * 500: features}).encode())
    assert len(rows) == 256 and len({row["id"] for row in rows}) == 256
    assert all(len(row["chip"].encode()) <= 128 and len(row["label"].encode()) <= 128 for row in rows)
    encoded = collector.encode_json({"version": 1, "node": "node", "sampled_at": 1, "sensors": rows})
    assert len(encoded) <= collector.MAX_RESPONSE_BYTES
    with pytest.raises(collector.SampleError):
        collector.parse_sensors_json(b" " * (collector.MAX_INPUT_BYTES + 1))


def hwmon(tmp_path, *, device="platform/coretemp.0", name="coretemp", number=0, **attributes):
    physical = tmp_path / "devices" / device / "hwmon" / f"hwmon{number}"
    physical.mkdir(parents=True)
    (physical / "name").write_text(name)
    for key, value in attributes.items():
        (physical / key).write_text(str(value))
    root = tmp_path / "class" / "hwmon"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"hwmon{number}").symlink_to(physical, target_is_directory=True)
    return root, physical


def test_sysfs_reads_millidegrees_labels_limits_and_zero(tmp_path):
    root, physical = hwmon(tmp_path, temp1_input=0, temp1_label="Package id 0", temp1_max=90000, temp1_crit=105000,
                           temp2_input=53000, temp2_fault=1, temp3_input=-10000, temp3_max="bad", temp4_input=251000)
    rows = collector.read_hwmon(root)
    assert len(rows) == 2 and rows[0]["celsius"] == 0
    assert rows[0]["max_celsius"] == 90 and rows[0]["critical_celsius"] == 105
    assert rows[0]["label"] == "Package id 0" and rows[0]["category"] == "cpu"
    assert rows[1]["celsius"] == -10 and rows[1]["label"] == "temp3" and rows[1]["max_celsius"] is None
    assert (physical / "temp1_input").read_text() == "0", "sampling must not write sensor attributes"


def test_sysfs_id_survives_hwmon_renumbering_and_separates_same_named_physical_chips(tmp_path):
    root, physical = hwmon(tmp_path, temp1_input=42000, number=0)
    first = collector.read_hwmon(root)[0]
    (root / "hwmon0").unlink()
    renamed = physical.with_name("hwmon9")
    physical.rename(renamed)
    (root / "hwmon9").symlink_to(renamed, target_is_directory=True)
    assert collector.read_hwmon(root)[0]["id"] == first["id"]
    hwmon(tmp_path, device="platform/coretemp.1", number=1, temp1_input=42000)
    assert len({row["id"] for row in collector.read_hwmon(root)}) == 2


def test_sysfs_unknown_fault_or_disabled_sensor_is_not_a_fake_temperature(tmp_path):
    root, _ = hwmon(tmp_path, temp1_input=50000, temp1_fault="bad", temp2_input=50000, temp2_enable=0,
                    temp3_input="NaN", temp4_input="True", temp5_input="99999999999999999999999999999999999999999999")
    assert collector.read_hwmon(root) == []


def test_missing_sysfs_or_no_sensor_is_empty_not_zero(tmp_path):
    assert collector.read_hwmon(tmp_path / "absent") == []
    assert collector.parse_sensors_json(b"{}") == []


def test_sysfs_is_fallback_only_when_fixed_sensors_command_is_missing(monkeypatch):
    seen = []
    monkeypatch.setattr(collector, "read_sensors_output", lambda: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(collector, "read_hwmon", lambda: seen.append("hwmon") or [])
    assert collector.read_temperatures() == [] and seen == ["hwmon"]
    monkeypatch.setattr(collector, "read_sensors_output", lambda: (_ for _ in ()).throw(collector.SampleError("temperature_read_failed")))
    with pytest.raises(collector.SampleError):
        collector.read_temperatures()
    assert seen == ["hwmon"], "installed-but-failed lm-sensors must not silently bypass its labels/configuration"


def command_child(monkeypatch, script):
    real_popen = subprocess.Popen
    seen = []
    def start(command, **kwargs):
        seen.append((command, kwargs))
        assert command == ("/usr/bin/sensors", "-j") and kwargs["shell"] is False
        return real_popen([sys.executable, "-c", script], **kwargs)
    monkeypatch.setattr(collector.subprocess, "Popen", start)
    return seen


def test_sensors_command_is_fixed_shell_free_and_discards_stderr(monkeypatch):
    seen = command_child(monkeypatch, 'import sys; sys.stderr.write("PRIVATE_ERROR"); sys.stdout.write("{}")')
    assert collector.read_sensors_output() == b"{}"
    assert seen[0][1]["stderr"] == subprocess.DEVNULL and seen[0][1]["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("script", ['import sys; sys.stdout.write("x" * 100000)', 'import time; time.sleep(2)', 'raise SystemExit(2)'])
def test_sensors_command_output_timeout_and_nonzero_exit_fail_closed(monkeypatch, script):
    command_child(monkeypatch, script)
    monkeypatch.setattr(collector, "MAX_INPUT_BYTES", 1024)
    monkeypatch.setattr(collector, "COMMAND_TIMEOUT", 0.1)
    started = time.monotonic()
    with pytest.raises(collector.SampleError, match="temperature_read_failed"):
        collector.read_sensors_output()
    assert time.monotonic() - started < 1.5


def test_sampler_cache_coalesces_concurrent_browsers_for_five_seconds():
    now, reads = [0.0], []
    def read():
        reads.append(1)
        time.sleep(0.01)
        return []
    sampler = collector.TemperatureSampler("pve-exact-node", reader=read, monotonic=lambda: now[0], clock=lambda: 123.9)
    with ThreadPoolExecutor(max_workers=12) as workers:
        results = list(workers.map(lambda _: sampler.get(), range(12)))
    assert len(reads) == 1 and all(item == {"version": 1, "node": "pve-exact-node", "sampled_at": 123, "sensors": []} for item in results)
    now[0] = 4.99; sampler.get(); assert len(reads) == 1
    now[0] = 5.0; sampler.get(); assert len(reads) == 2


def test_sampling_error_is_cached_but_never_serves_an_old_success_as_fresh():
    now, reads = [0.0], []
    def read():
        reads.append(1)
        if len(reads) > 1:
            raise collector.SampleError("PRIVATE_SOURCE")
        return []
    sampler = collector.TemperatureSampler("node", reader=read, monotonic=lambda: now[0])
    sampler.get(); now[0] = 5
    with pytest.raises(collector.SampleError, match="temperature_read_failed"):
        sampler.get()
    now[0] = 6
    with pytest.raises(collector.SampleError, match="temperature_read_failed"):
        sampler.get()
    assert len(reads) == 2


@pytest.fixture
def http_server():
    state = {"reads": 0, "failed": False}
    def reader():
        state["reads"] += 1
        if state["failed"]:
            raise collector.SampleError("PRIVATE_SOURCE_ERROR")
        return collector.parse_sensors_json(sensors())
    with collector.TemperatureHTTPServer(("127.0.0.1", 0), collector.TemperatureSampler("pve-node", reader=reader), TOKEN) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        state["server"] = server
        yield state
        server.shutdown()
        worker.join(timeout=2)


def request_http(state, *, method="GET", path="/v1/temperatures", authorization=TOKEN.decode()):
    connection = http.client.HTTPConnection(*state["server"].server_address, timeout=2)
    headers = {} if authorization is None else {"Authorization": "Bearer " + authorization}
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_http_bearer_auth_is_required_and_rejected_requests_never_read_sensors(http_server, capsys):
    for token in (None, "wrong", ""):
        status, headers, body = request_http(http_server, authorization=token)
        assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
        assert TOKEN not in body
    assert http_server["reads"] == 0
    assert capsys.readouterr() == ("", "")


def test_http_exact_get_route_schema_private_cache_and_shared_sampling(http_server):
    status, headers, body = request_http(http_server)
    value = json.loads(body)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert value["version"] == 1 and value["node"] == "pve-node" and value["sensors"][0]["celsius"] == 0
    assert isinstance(value["sampled_at"], int) and TOKEN not in body
    request_http(http_server)
    assert http_server["reads"] == 1
    assert request_http(http_server, path="/temperatures")[0] == 404
    assert request_http(http_server, path="/v1/temperatures?token=PRIVATE")[0] == 404
    assert request_http(http_server, method="POST")[0] == 405
    assert request_http(http_server, method="HEAD")[0] == 405


def test_duplicate_auth_headers_are_rejected_without_sampling(http_server):
    connection = http.client.HTTPConnection(*http_server["server"].server_address, timeout=2)
    connection.putrequest("GET", "/v1/temperatures")
    connection.putheader("Authorization", "Bearer " + TOKEN.decode())
    connection.putheader("Authorization", "Bearer " + TOKEN.decode())
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 401
    response.read(); connection.close()
    assert http_server["reads"] == 0


def test_http_read_error_is_fixed_503_not_empty_success_or_private_output(http_server):
    http_server["failed"] = True
    status, _, body = request_http(http_server)
    assert status == 503 and json.loads(body) == {"error": "temperature_read_failed"}
    assert b"PRIVATE" not in body


def test_token_file_is_required_minimum_length_bounded_and_never_in_errors(tmp_path):
    path = tmp_path / "token"
    path.write_bytes(TOKEN + b"\n")
    assert collector.load_token(path) == TOKEN
    for value in (b"short", b"x" * 257, b"x" * 4097, b"x" * 32 + b" internal-space", b"\xff" * 32):
        path.write_bytes(value)
        with pytest.raises(ValueError) as error:
            collector.load_token(path)
        assert str(value) not in str(error.value)
    with pytest.raises(ValueError, match="cannot read a valid token file"):
        collector.load_token(tmp_path / "missing")


def test_tls_configuration_requires_pair_and_loads_server_context(monkeypatch):
    seen = []
    class Context:
        def __init__(self, protocol):
            seen.append(protocol)
        def load_cert_chain(self, cert, key):
            seen.append((cert, key))
    monkeypatch.setattr(collector.ssl, "SSLContext", Context)
    context = collector.tls_context("cert.pem", "key.pem")
    assert seen == [collector.ssl.PROTOCOL_TLS_SERVER, ("cert.pem", "key.pem")]
    assert context.minimum_version == collector.ssl.TLSVersion.TLSv1_2
    with pytest.raises(ValueError, match="provided together"):
        collector.tls_context("cert.pem", None)


def test_once_outputs_same_schema_without_token_or_http_listener(monkeypatch, capsys):
    monkeypatch.setattr(collector, "read_temperatures", lambda: [])
    assert collector.main(["--node", "pve-exact", "--once"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["node"] == "pve-exact" and value["version"] == 1 and value["sensors"] == []
    monkeypatch.setattr(collector, "read_temperatures", lambda: (_ for _ in ()).throw(collector.SampleError("PRIVATE")))
    assert collector.main(["--node", "pve-exact", "--once"]) == 1
    assert capsys.readouterr().err == "temperature read failed\n"


@pytest.mark.parametrize("arguments", [[], ["--node", "node"], ["--node", " node", "--once"], ["--node", "node.example", "--once"], ["--node", "x" * 65, "--once"],
    ["--node", "node", "--certfile", "cert.pem", "--once"]])
def test_invalid_cli_config_does_not_start_a_listener(arguments):
    with pytest.raises(SystemExit) as error:
        collector.main(arguments)
    assert error.value.code == 2


def test_service_is_nonroot_readonly_loopback_and_requires_explicit_credential():
    unit = (Path(__file__).parents[1] / "plugins/temperatures/collectors/pve-temperature-collector.service").read_text()
    assert "DynamicUser=yes" in unit and "NoNewPrivileges=yes" in unit
    assert "LoadCredential=token:" in unit and "--token-file %d/token" in unit
    assert "--listen" not in unit.split("ExecStart=", 1)[1].splitlines()[0]
    assert "ProtectSystem=strict" in unit and "ProtectKernelModules=yes" in unit


def test_tls_idle_handshake_cannot_block_accept_or_survive_short_timeout(tmp_path, monkeypatch):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("local TLS handshake fixture requires the openssl CLI")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost", "-days", "1"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=True)
    monkeypatch.setattr(collector, "REQUEST_TIMEOUT", 0.15)
    sampler = collector.TemperatureSampler("pve-tls", reader=lambda: [])
    with collector.TemperatureHTTPServer(("127.0.0.1", 0), sampler, TOKEN) as server:
        server.enable_tls(collector.tls_context(cert, key))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        idle = socket.create_connection(server.server_address, timeout=2)
        try:
            client = http.client.HTTPSConnection(*server.server_address, timeout=2,
                                                context=ssl._create_unverified_context())
            client.request("GET", "/v1/temperatures", headers={"Authorization": "Bearer " + TOKEN.decode()})
            response = client.getresponse()
            assert response.status == 200 and json.loads(response.read())["node"] == "pve-tls"
            client.close()
            started = time.monotonic()
            assert idle.recv(1) == b""
            assert time.monotonic() - started < 1.5
        finally:
            idle.close()
            server.shutdown()
            worker.join(timeout=2)
