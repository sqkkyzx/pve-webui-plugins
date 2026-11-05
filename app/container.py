"""Run the App and optional HTTPS gateway under one non-root container entrypoint."""

import json
import os
from pathlib import Path
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
from urllib import request

from app.nginx import gateway_path, parse_trusted_proxies, render_nginx_config
from app.settings import Settings, env_bool


DEFAULT_COMMAND = [
    "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8008",
    "--no-access-log", "--no-proxy-headers", "--timeout-graceful-shutdown", "15",
]


def gateway_certificate(data_dir: Path) -> tuple[Path, Path]:
    certificate = os.environ.get("PVE_PROXY_CERT_FILE")
    private_key = os.environ.get("PVE_PROXY_KEY_FILE")
    if certificate or private_key:
        if not certificate or not private_key:
            raise ValueError("PVE_PROXY_CERT_FILE and PVE_PROXY_KEY_FILE must be configured together")
        paths = gateway_path(certificate), gateway_path(private_key)
        if not all(path.is_file() for path in paths):
            raise ValueError("The configured gateway certificate and key must be readable files")
        return paths

    data_dir = gateway_path(data_dir)
    directory = data_dir / "proxy"
    paths = directory / "cert.pem", directory / "key.pem"
    if directory.exists():
        if not all(path.is_file() for path in paths):
            raise ValueError("The saved gateway certificate pair is incomplete")
        return paths
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".proxy-", dir=data_dir))
    try:
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "3650",
            "-subj", "/CN=PVE WebUI Plugins", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1",
            "-keyout", str(temporary / "key.pem"), "-out", str(temporary / "cert.pem"),
        ], check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        for path in temporary.iterdir():
            path.chmod(0o600)
        # Publish both files together. A terminated first startup leaves only an
        # unused temporary directory, never half of the active certificate pair.
        temporary.replace(directory)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return paths


def healthcheck() -> int:
    try:
        urls = ["http://127.0.0.1:8008/pve-plugins/health"]
        if env_bool("PVE_INTERNAL_PROXY_ENABLED", True):
            urls.append("https://127.0.0.1:8006/pve-plugins/health")
        opener = request.build_opener(
            request.ProxyHandler({}), request.HTTPSHandler(context=ssl._create_unverified_context()),
        )
        for url in urls:
            with opener.open(url, timeout=2) as response:
                if response.status != 200 or not isinstance(json.loads(response.read(65536)), dict):
                    return 1
    except (OSError, ValueError):
        return 1
    return 0


def _signal_group(process: subprocess.Popen, signum: int) -> None:
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def _stop_processes(processes: list[subprocess.Popen]) -> None:
    for index, process in enumerate(processes):
        _signal_group(process, signal.SIGQUIT if index else signal.SIGTERM)
    deadline = time.monotonic() + 20
    for process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    # Kill remaining descendants even if their direct parent already exited.
    for process in processes:
        _signal_group(process, signal.SIGKILL)
        process.wait()
    # PID 1 also inherits orphaned workers after a child crashes.
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            break


def run(command: list[str]) -> int:
    if not env_bool("PVE_INTERNAL_PROXY_ENABLED", True):
        os.execvp(command[0], command)
        return 0

    settings = Settings.from_env()
    runtime = gateway_path(settings.runtime_dir or "/run/pve-webui-plugins")
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    trusted = parse_trusted_proxies(os.environ.get("PVE_TRUSTED_PROXIES"))
    certificate, private_key = gateway_certificate(Path(settings.data_dir or "/app/data"))
    configuration = render_nginx_config(
        settings, runtime_dir=runtime, certificate=certificate, private_key=private_key, trusted_proxies=trusted,
    )
    config_file = runtime / "nginx.conf"
    config_file.write_text(configuration)
    config_file.chmod(0o600)
    subprocess.run(["nginx", "-e", "/dev/stderr", "-t", "-c", str(config_file)], check=True)

    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    previous_handlers = {number: signal.signal(number, stop) for number in (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT)}
    processes = []
    try:
        processes.append(subprocess.Popen(command, start_new_session=True))
        processes.append(subprocess.Popen(["nginx", "-e", "/dev/stderr", "-c", str(config_file)], start_new_session=True))
        while not stopping:
            if any(process.poll() is not None for process in processes):
                return 1
            time.sleep(0.2)
        return 0
    finally:
        _stop_processes(processes)
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--healthcheck"]:
        return healthcheck()
    try:
        return run(arguments or DEFAULT_COMMAND)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Container startup failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
