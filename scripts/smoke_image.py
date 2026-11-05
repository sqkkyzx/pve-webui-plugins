"""Check fresh defaults and all optional combinations in the actual image."""

import argparse
from itertools import combinations
import json
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
PLUGINS = {
    "komodo": ("/pve-plugins/api/komodo/connection", "/pve-plugins/assets/komodo/extension.js", "extension.js"),
    "temperatures": ("/pve-plugins/api/temperatures/nodes/pve-a", "/pve-plugins/assets/temperatures/temperatures.js", "temperatures.js"),
    "sites": ("/pve-plugins/api/sites", "/pve-plugins/assets/sites/sites.js", "sites.js"),
    "safeline": ("/pve-plugins/api/safeline/connection", "/pve-plugins/assets/safeline/safeline.js", "safeline.js"),
}
OPTIONAL_PLUGINS = ("komodo", "temperatures", "safeline")
BOOTSTRAP = """
import os, sys
descriptor = os.open('/app/data/plugins.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'w') as handle:
    handle.write(sys.argv[1])
os.execvp(sys.argv[2], sys.argv[2:])
"""


def configuration(selected):
    return {
        "schema_version": 2,
        "revision": 1,
        "enabled_plugins": [plugin_id for plugin_id in PLUGINS if plugin_id == "sites" or plugin_id in selected],
        "modules": {
            "komodo": {"url": "", "api_key": "", "api_secret": "", "tls_verify": True},
            "temperatures": {"nodes": {}},
        },
        "navigation": {
            "entry_order": list(OPTIONAL_PLUGINS), "module_aliases": {}, "module_icons": {},
        },
    }


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=30).strip()


def fetch(base, path):
    try:
        with urlopen(base + path, timeout=3) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def verify(image, selected=None):
    name = "pve-webui-plugins-smoke-" + uuid4().hex[:12]
    snapshot = configuration(selected) if selected is not None else None
    expected = snapshot["enabled_plugins"] if snapshot is not None else ["sites"]
    arguments = [
        "run", "--detach", "--name", name, "--read-only", "--cap-drop=ALL",
        "--memory=384m", "--pids-limit=128",
        "--security-opt=no-new-privileges:true",
        "--tmpfs", "/run/pve-webui-plugins:rw,noexec,nosuid,size=8m,uid=10001,gid=10001,mode=0700",
        "--tmpfs", "/app/data:rw,noexec,nosuid,size=1m,uid=10001,gid=10001,mode=0700",
        "--publish", "127.0.0.1::8008", "--env", "PVE_UPSTREAM=192.0.2.1",
    ]
    try:
        if snapshot is not None:
            image_config = json.loads(docker("image", "inspect", "--format", "{{json .Config}}", image))
            command = [*(image_config.get("Entrypoint") or []), *(image_config.get("Cmd") or [])]
            assert isinstance(command, list) and command and all(isinstance(part, str) for part in command)
            arguments.extend(["--entrypoint", "python", image, "-c", BOOTSTRAP, json.dumps(snapshot), *command])
        else:
            arguments.append(image)
        docker(*arguments)
        port = docker("port", name, "8008/tcp").splitlines()[0].rpartition(":")[2]
        base = "http://127.0.0.1:" + port
        deadline = time.monotonic() + 90
        inspected_at = 0.0
        while time.monotonic() < deadline:
            try:
                status, body = fetch(base, "/pve-plugins/health")
                if status == 200:
                    break
            except (URLError, TimeoutError, ConnectionError):
                pass
            if time.monotonic() - inspected_at >= 1:
                state = json.loads(docker("inspect", "--format", "{{json .State}}", name))
                inspected_at = time.monotonic()
                if state.get("OOMKilled") or not state.get("Running"):
                    raise AssertionError("Container exited before readiness: " + json.dumps({
                        key: state.get(key) for key in ("Status", "Running", "OOMKilled", "ExitCode", "Error")
                    }))
            time.sleep(0.25)
        else:
            raise AssertionError("Container did not become ready within 90 seconds")
        assert json.loads(body)["plugins"] == expected
        assert fetch(base, "/pve-komodo/health")[0] == 404
        runtime = json.loads(docker("exec", name, "python", "-c", """
import json, os
from pathlib import Path
root = Path('/run/pve-webui-plugins')
private = Path('/app/data/plugins.json')
print(json.dumps({'uid': os.getuid(), 'manifest': json.loads((root/'plugins.json').read_text()),
                  'head': (root/'desktop-head.html').read_text(),
                  'private_mode': private.stat().st_mode & 0o777 if private.exists() else None,
                  'licenses': all(Path('/app', name).is_file() for name in ('LICENSE', 'NOTICE.md'))}))
"""))
        assert runtime["uid"] == 10001
        assert runtime["licenses"]
        assert runtime["private_mode"] == (0o600 if snapshot is not None else None)
        assert runtime["manifest"]["enabled"] == expected
        for plugin_id, (api, asset, filename) in PLUGINS.items():
            active = plugin_id in expected
            assert fetch(base, api)[0] == (401 if active else 404), (selected, api)
            code, asset_body = fetch(base, asset)
            assert code == (200 if active else 404), (selected, asset)
            assert (asset in runtime["head"]) == active
            if active:
                assert asset_body == (ROOT / "plugins" / plugin_id / "static" / filename).read_bytes()
        assert fetch(base, "/pve-plugins/api/plugins")[0] == 401
        assert fetch(base, "/pve-plugins/api/config")[0] == 401
        assert fetch(base, "/pve-plugins/api/navigation")[0] == 401
        for filename in ("navigation.js", "manager.js"):
            asset = "/pve-plugins/assets/core/" + filename
            assert fetch(base, asset) == (200, (ROOT / "app/static" / filename).read_bytes())
            assert asset in runtime["head"]
        label = "fresh defaults" if selected is None else ",".join(expected)
        print("Verified image combination: " + label, flush=True)
    except Exception:
        diagnostic = subprocess.run(["docker", "inspect", "--format", "{{json .State}}", name],
                                    check=False, capture_output=True, text=True, timeout=15)
        if diagnostic.returncode == 0:
            state = json.loads(diagnostic.stdout)
            print("Container state before cleanup: " + json.dumps({
                key: state.get(key) for key in ("Status", "Running", "OOMKilled", "ExitCode", "Error", "StartedAt", "FinishedAt")
            }), flush=True)
        logs = subprocess.run(["docker", "logs", "--tail", "100", name], check=False,
                              capture_output=True, text=True, timeout=15)
        print(logs.stdout + logs.stderr, flush=True)
        raise
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True, timeout=60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    options = parser.parse_args()
    verify(options.image)
    for count in range(len(OPTIONAL_PLUGINS) + 1):
        for selected in combinations(OPTIONAL_PLUGINS, count):
            verify(options.image, selected)
