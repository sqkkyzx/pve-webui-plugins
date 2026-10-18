"""Check every plugin combination in the actual read-only, non-root image."""

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
}


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=30).strip()


def fetch(base, path):
    try:
        with urlopen(base + path, timeout=3) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def verify(image, selected):
    name = "pve-webui-plugins-smoke-" + uuid4().hex[:12]
    docker("run", "--detach", "--name", name, "--read-only", "--cap-drop=ALL",
           "--security-opt=no-new-privileges:true",
           "--tmpfs", "/run/pve-webui-plugins:rw,noexec,nosuid,size=1m,uid=10001,gid=10001,mode=0700",
           "--tmpfs", "/app/data:rw,noexec,nosuid,size=1m,uid=10001,gid=10001,mode=0700",
           "--publish", "127.0.0.1::8000", "--env", "PVE_UPSTREAM=https://pve.invalid:8006",
           "--env", "PVE_KOMODO_ACTION_STATE_FILE=/app/data/actions.sqlite3",
           "--env", "PVE_PLUGINS=" + ",".join(selected), image)
    try:
        port = docker("port", name, "8000/tcp").splitlines()[0].rpartition(":")[2]
        base = "http://127.0.0.1:" + port
        for _ in range(60):
            try:
                status, body = fetch(base, "/pve-plugins/health")
                if status == 200:
                    break
            except (URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(0.25)
        else:
            raise AssertionError("Container did not become healthy")
        assert json.loads(body)["plugins"] == list(selected)
        assert fetch(base, "/pve-komodo/health")[0] == 404
        runtime = json.loads(docker("exec", name, "python", "-c", """
import json, os
from pathlib import Path
root = Path('/run/pve-webui-plugins')
print(json.dumps({'uid': os.getuid(), 'manifest': json.loads((root/'plugins.json').read_text()),
                  'head': (root/'desktop-head.html').read_text()}))
"""))
        assert runtime["uid"] == 10001
        assert runtime["manifest"]["enabled"] == list(selected)
        for plugin_id, (api, asset, filename) in PLUGINS.items():
            active = plugin_id in selected
            assert fetch(base, api)[0] == (401 if active else 404), (selected, api)
            code, asset_body = fetch(base, asset)
            assert code == (200 if active else 404), (selected, asset)
            assert (asset in runtime["head"]) == active
            if active:
                assert asset_body == (ROOT / "plugins" / plugin_id / "static" / filename).read_bytes()
        assert fetch(base, "/pve-plugins/api/plugins")[0] == 401
        print("Verified image combination: " + (",".join(selected) or "(none)"), flush=True)
    except Exception:
        print(docker("logs", name), flush=True)
        raise
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True, timeout=15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    options = parser.parse_args()
    for count in range(len(PLUGINS) + 1):
        for selected in combinations(PLUGINS, count):
            verify(options.image, selected)
