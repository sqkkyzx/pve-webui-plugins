import json
from pathlib import Path
import signal
import ssl
import subprocess
import sys

import httpx
import pytest

from app import container
from app.nginx import gateway_path, parse_trusted_proxies, render_nginx_config
from app.settings import Settings


def settings(**changes):
    return Settings(
        upstream="https://192.0.2.10:8006",
        upstreams=("https://192.0.2.10:8006", "https://[2001:db8::11]:8006"),
        tls_verify=False,
        **changes,
    )


def render(config=None, **changes):
    return render_nginx_config(
        config or settings(),
        runtime_dir=Path("/run/pve-webui-plugins"),
        certificate=Path("/app/data/proxy/cert.pem"),
        private_key=Path("/app/data/proxy/key.pem"),
        **changes,
    )


def test_native_writes_have_one_attempt_and_only_reads_can_retry():
    configuration = render()
    native = configuration.split("        location / {", 1)[1].split("        location @pve_read", 1)[0]
    assert "if ($request_method ~ ^(GET|HEAD)$) { return 418; }" in native
    assert "error_page 418 = @pve_read;" in native
    assert "proxy_pass https://pve_original;" in native
    assert "        proxy_next_upstream off;" in configuration.split("        location = /", 1)[0]
    read = configuration.split("        location @pve_read {", 1)[1].split("        location @pve_websocket", 1)[0]
    assert "proxy_next_upstream error timeout http_502 http_503 http_504;" in read
    assert "proxy_next_upstream_tries 2;" in read
    assert "proxy_request_buffering off;" in configuration


def test_native_http_reads_fail_over_without_using_websocket_idle_timeout():
    configuration = render()
    native = configuration.split("        location / {", 1)[1].split("        location @pve_read", 1)[0]
    assert 'map "$request_method:$http_upgrade" $pve_websocket_request' in configuration
    assert '"~*^GET:websocket$" 1;' in configuration
    assert native.index("if ($pve_websocket_request)") < native.index("if ($request_method ~ ^(GET|HEAD)$)")
    read = configuration.split("        location @pve_read {", 1)[1].split("        location @pve_websocket", 1)[0]
    websocket = configuration.split("        location @pve_websocket {", 1)[1].split("        location @pve_plain_desktop", 1)[0]
    desktop = configuration.split("        location @pve_plain_desktop {", 1)[1]
    assert "proxy_read_timeout 10s;" in read
    assert "proxy_read_timeout 10s;" in desktop
    assert "proxy_read_timeout 3600s;" in websocket
    assert "proxy_next_upstream error timeout http_502 http_503 http_504;" in websocket


def test_primary_backup_and_full_external_authority_are_preserved():
    configuration = render(trusted_proxies=parse_trusted_proxies("192.0.2.0/24,2001:db8::/32"))
    assert "server 192.0.2.10:8006 max_fails=1 fail_timeout=5s;" in configuration
    assert "server [2001:db8::11]:8006 max_fails=1 fail_timeout=5s backup;" in configuration
    assert "proxy_set_header Host $http_host;" in configuration
    assert "server 127.0.0.1:8008;" in configuration
    assert "listen 8006 ssl;" in configuration
    assert "listen [::]:8006 ssl;" in configuration
    assert 'map "$pve_trusted_peer:$http_x_forwarded_proto"' in configuration
    assert '"1:http" http;' in configuration
    assert '"1:https" https;' in configuration
    assert "default $scheme;" in configuration
    assert "192.0.2.0/24 1;" in configuration
    assert "2001:db8::/32 1;" in configuration
    assert "access_log off;" in configuration
    assert "error_log /dev/stderr crit;" in configuration
    for path in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"):
        assert f"    {path}_temp_path \"/run/pve-webui-plugins/" in configuration


@pytest.mark.parametrize("nodes,addresses", [
    ("192.0.2.10", ("192.0.2.10:8006",)),
    ("2001:db8::10", ("[2001:db8::10]:8006",)),
    ("192.0.2.10:443", ("192.0.2.10:443",)),
    ("[2001:db8::10]:443", ("[2001:db8::10]:443",)),
    ("192.0.2.10:8443", ("192.0.2.10:8443",)),
    ("192.0.2.10:443,[2001:db8::11]:8443", ("192.0.2.10:443", "[2001:db8::11]:8443")),
    ("192.0.2.10:8443,[2001:db8::11]:443", ("192.0.2.10:8443", "[2001:db8::11]:443")),
])
def test_upstream_target_ports_are_independent_of_gateway_listener(monkeypatch, nodes, addresses):
    monkeypatch.setenv("PVE_UPSTREAM", nodes)
    monkeypatch.setenv("PVE_TLS_VERIFY", "false")
    monkeypatch.delenv("PVE_CA_FILE", raising=False)
    monkeypatch.delenv("PVE_TLS_SERVER_NAMES", raising=False)
    config = Settings.from_env()
    assert config.upstreams == tuple(str(httpx.URL("https://" + address)) for address in addresses)
    configuration = render(config)
    servers = [line.strip() for line in configuration.splitlines() if "max_fails=1" in line]
    assert servers == [
        f"server {address} max_fails=1 fail_timeout=5s{' backup' if index else ''};"
        for index, address in enumerate(addresses)
    ]
    assert "listen 8006 ssl;" in configuration
    assert "listen [::]:8006 ssl;" in configuration
    assert "server 127.0.0.1:8008;" in configuration


def test_only_root_has_native_page_fallback():
    configuration = render()
    assert configuration.count("error_page 502 503 504 = @pve_plain_desktop;") == 1
    plugin = configuration.split("        location ^~ /pve-plugins/ {", 1)[1].split("        location /", 1)[0]
    assert "error_page" not in plugin
    assert "proxy_pass http://pve_plugins_backend;" in plugin


def test_tls_verification_uses_each_selected_node_name():
    config = Settings(
        upstream="https://192.0.2.10:8006",
        upstreams=("https://192.0.2.10:8006", "https://[2001:db8::11]:8006"),
        tls_verify=True,
        upstream_server_names=("pve-a.example", "pve-b.example"),
        ca_file="/run/pve-ca.pem",
    )
    configuration = render(config)
    assert "volatile;" in configuration
    assert '"pve-a.example";' in configuration
    assert '"pve-b.example";' in configuration
    assert ':8006$" "pve-b.example";' in configuration
    assert "proxy_ssl_name $pve_upstream_name;" in configuration
    assert "proxy_ssl_verify on;" in configuration
    assert 'proxy_ssl_trusted_certificate "/run/pve-ca.pem";' in configuration
    with pytest.raises(ValueError, match="PVE_TLS_SERVER_NAMES"):
        render(Settings(upstream="https://192.0.2.10:8006", tls_verify=True))


@pytest.mark.parametrize("value", ["192.0.2.0/24,", "proxy.example", "127.0.0.1; include evil", "::1\n0/0"])
def test_trusted_proxy_networks_reject_invalid_configuration(value):
    with pytest.raises(ValueError, match="PVE_TRUSTED_PROXIES"):
        parse_trusted_proxies(value)


def test_trusted_proxy_networks_canonicalize_and_deduplicate():
    assert parse_trusted_proxies(" 192.0.2.20/24,192.0.2.0/24,::1 ") == ("192.0.2.0/24", "::1/128")
    assert parse_trusted_proxies("") == ()


@pytest.mark.parametrize("value", ["/run/$runtime", "/run/name\ninclude", "/run/name\x00"])
def test_gateway_paths_reject_dynamic_or_control_characters(value):
    with pytest.raises(ValueError, match="Gateway paths"):
        gateway_path(value)


def test_relative_certificate_runtime_and_ca_paths_resolve_against_app_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    certificate = Path("certs/public.pem")
    key = Path("certs/private.pem")
    certificate.parent.mkdir()
    certificate.write_text("mounted certificate")
    key.write_text("mounted key")
    monkeypatch.setenv("PVE_PROXY_CERT_FILE", str(certificate))
    monkeypatch.setenv("PVE_PROXY_KEY_FILE", str(key))
    paths = container.gateway_certificate(tmp_path / "unused-data")
    assert paths == (certificate.resolve(), key.resolve())
    configuration = render_nginx_config(
        Settings(upstream="https://192.0.2.10:8006", upstream_server_names=("pve.example",), ca_file="ca.pem"),
        runtime_dir=Path("runtime"), certificate=certificate, private_key=key,
    )
    assert f'ssl_certificate "{tmp_path}/certs/public.pem";' in configuration
    assert f'ssl_certificate_key "{tmp_path}/certs/private.pem";' in configuration
    assert f'pid "{tmp_path}/runtime/nginx.pid";' in configuration
    assert f'proxy_ssl_trusted_certificate "{tmp_path}/ca.pem";' in configuration


def test_self_signed_certificate_is_private_and_survives_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("PVE_PROXY_CERT_FILE", raising=False)
    monkeypatch.delenv("PVE_PROXY_KEY_FILE", raising=False)
    paths = container.gateway_certificate(tmp_path)
    first = tuple(path.read_bytes() for path in paths)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    assert (tmp_path / "proxy").stat().st_mode & 0o777 == 0o700
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*map(str, paths))
    assert container.gateway_certificate(tmp_path) == paths
    assert tuple(path.read_bytes() for path in paths) == first
    assert not list(tmp_path.glob(".proxy-*"))


def test_incomplete_saved_pair_is_rejected_without_replacement(tmp_path, monkeypatch):
    monkeypatch.delenv("PVE_PROXY_CERT_FILE", raising=False)
    monkeypatch.delenv("PVE_PROXY_KEY_FILE", raising=False)
    directory = tmp_path / "proxy"
    directory.mkdir()
    certificate = directory / "cert.pem"
    certificate.write_bytes(b"saved certificate")
    with pytest.raises(ValueError, match="incomplete"):
        container.gateway_certificate(tmp_path)
    assert certificate.read_bytes() == b"saved certificate"
    assert not (directory / "key.pem").exists()


def test_mounted_certificate_pair_does_not_initialize_data(tmp_path, monkeypatch):
    certificate = tmp_path / "external.pem"
    key = tmp_path / "external.key"
    certificate.write_text("mounted certificate")
    key.write_text("mounted key")
    monkeypatch.setenv("PVE_PROXY_CERT_FILE", str(certificate))
    monkeypatch.setenv("PVE_PROXY_KEY_FILE", str(key))
    data = tmp_path / "data"
    assert container.gateway_certificate(data) == (certificate, key)
    assert not data.exists()
    monkeypatch.delenv("PVE_PROXY_KEY_FILE")
    with pytest.raises(ValueError, match="together"):
        container.gateway_certificate(data)


def test_failed_generation_never_publishes_half_a_certificate_pair(tmp_path, monkeypatch):
    monkeypatch.delenv("PVE_PROXY_CERT_FILE", raising=False)
    monkeypatch.delenv("PVE_PROXY_KEY_FILE", raising=False)

    def fail(command, **kwargs):
        Path(command[command.index("-keyout") + 1]).write_text("partial private key")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(container.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        container.gateway_certificate(tmp_path)
    assert not (tmp_path / "proxy").exists()
    assert not list(tmp_path.glob(".proxy-*"))


def test_disabled_proxy_executes_only_app_without_reading_settings(monkeypatch):
    monkeypatch.setenv("PVE_INTERNAL_PROXY_ENABLED", "false")
    monkeypatch.setattr(container.Settings, "from_env", lambda: pytest.fail("disabled gateway read settings"))
    monkeypatch.setattr(container, "gateway_certificate", lambda _: pytest.fail("disabled gateway initialized certificate"))
    executed = []
    monkeypatch.setattr(container.os, "execvp", lambda executable, command: executed.append((executable, command)))
    assert container.run(["uvicorn", "app.main:create_app", "--reload"]) == 0
    assert executed == [("uvicorn", ["uvicorn", "app.main:create_app", "--reload"])]


@pytest.mark.parametrize("enabled,count", [("false", 1), ("true", 2)])
def test_healthcheck_covers_gateway_only_when_enabled(monkeypatch, enabled, count):
    monkeypatch.setenv("PVE_INTERNAL_PROXY_ENABLED", enabled)
    urls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, size):
            return json.dumps({"version": "test"}).encode()

    class Opener:
        def open(self, url, timeout):
            urls.append(url)
            return Response()

    monkeypatch.setattr(container.request, "build_opener", lambda *args: Opener())
    assert container.healthcheck() == 0
    assert len(urls) == count
    assert urls[0] == "http://127.0.0.1:8008/pve-plugins/health"
    if count == 2:
        assert urls[1] == "https://127.0.0.1:8006/pve-plugins/health"


def test_supervisor_stops_and_reaps_both_process_groups():
    processes = [
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        for _ in range(2)
    ]
    container._stop_processes(processes)
    assert processes[0].returncode == -signal.SIGTERM
    assert processes[1].returncode == -signal.SIGQUIT


def test_unexpected_app_exit_stops_gateway_and_fails_container(tmp_path, monkeypatch):
    monkeypatch.setenv("PVE_INTERNAL_PROXY_ENABLED", "true")
    config = settings(data_dir=str(tmp_path / "data"), runtime_dir=str(tmp_path / "runtime"))
    monkeypatch.setattr(container.Settings, "from_env", lambda: config)
    monkeypatch.setattr(container, "gateway_certificate", lambda _: (tmp_path / "cert", tmp_path / "key"))
    monkeypatch.setattr(container, "render_nginx_config", lambda *args, **kwargs: "test gateway config")
    monkeypatch.setattr(container.subprocess, "run", lambda *args, **kwargs: None)
    popen = subprocess.Popen
    started = []

    def start(command, **kwargs):
        if command[0] == "nginx":
            command = [sys.executable, "-c", "import time; time.sleep(30)"]
        process = popen(command, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(container.subprocess, "Popen", start)
    handlers = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT)}
    assert container.run([sys.executable, "-c", "pass"]) == 1
    assert len(started) == 2
    assert started[0].returncode == 0
    assert started[1].returncode == -signal.SIGQUIT
    assert all(signal.getsignal(number) == handler for number, handler in handlers.items())
