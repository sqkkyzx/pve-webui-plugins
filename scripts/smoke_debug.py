"""Verify source mounts and all four reload inputs in the debug image."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

from smoke_image import ROOT, docker, fetch


def wait_for(check, description):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if check():
                print("Verified debug reload: " + description, flush=True)
                return
        except (OSError, ValueError, subprocess.CalledProcessError):
            pass
        time.sleep(0.5)
    raise AssertionError("Debug reload failed: " + description)


def verify(image):
    command = subprocess.run([
        "docker", "compose", "--project-directory", str(ROOT),
        "-f", str(ROOT / "compose.yaml"), "-f", str(ROOT / "compose.debug.yaml"),
        "config", "--format", "json",
    ], env={**os.environ, "PVE_UPSTREAM": "https://pve.invalid:8006"},
        capture_output=True, text=True, check=True, timeout=30)
    config = json.loads(command.stdout)["services"]["pve-webui-plugins"]
    assert config["build"]["target"] == "debug"
    name = "pve-webui-debug-smoke-" + uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="pve-webui-debug-") as work:
        source = Path(work)
        source.chmod(0o755)
        mounts = []
        for directory in ("app", "plugins", "templates"):
            shutil.copytree(ROOT / directory, source / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for path in (source / directory).rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o644)
            (source / directory).chmod(0o755)
            mounts.extend(["--mount", f"type=bind,source={source / directory},target=/app/{directory},readonly"])
        docker("run", "--detach", "--name", name, "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges:true",
               "--tmpfs", "/run/pve-webui-plugins:rw,noexec,nosuid,size=1m,uid=10001,gid=10001,mode=0700",
               "--publish", "127.0.0.1::8000", "--env", "PVE_UPSTREAM=https://pve.invalid:8006",
               "--env", "PVE_PLUGINS=sites", "--env", "WATCHFILES_FORCE_POLLING=true",
               *mounts, image, *config["command"])
        try:
            port = docker("port", name, "8000/tcp").splitlines()[0].rpartition(":")[2]
            base = "http://127.0.0.1:" + port
            wait_for(lambda: fetch(base, "/pve-plugins/health")[0] == 200, "startup")

            def generated(filename):
                return docker("exec", name, "python", "-c",
                              "from pathlib import Path; print(Path('/run/pve-webui-plugins/" + filename + "').read_text())")

            main = source / "app/main.py"
            current_version = json.loads(fetch(base, "/pve-plugins/health")[1])["version"]
            changed_version = current_version + "-debug-test"
            main.write_text(main.read_text().replace(f'VERSION = "{current_version}"', f'VERSION = "{changed_version}"'))
            wait_for(lambda: json.loads(fetch(base, "/pve-plugins/health")[1])["version"] == changed_version, "Python")

            asset = source / "plugins/sites/static/sites.js"
            asset.write_text(asset.read_text() + "\n// debug-smoke-source-mount\n")
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()[:16]
            wait_for(lambda: digest in generated("desktop-head.html"), "JavaScript and asset version")
            assert fetch(base, "/pve-plugins/assets/sites/sites.js")[1] == asset.read_bytes()

            manifest = source / "plugins/sites/plugin.json"
            data = json.loads(manifest.read_text())
            data["name"] = "Debug reload verified"
            manifest.write_text(json.dumps(data))
            wait_for(lambda: "Debug reload verified" in generated("plugins.json"), "plugin JSON")

            template = source / "templates/desktop-head.html.tmpl"
            template.write_text(template.read_text() + "\n<!-- debug-smoke-template -->\n")
            wait_for(lambda: "debug-smoke-template" in generated("desktop-head.html"), "template")
            assert docker("exec", name, "id", "-u") == "10001"
        except Exception:
            print(docker("logs", name), flush=True)
            raise
        finally:
            subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True, timeout=15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    verify(parser.parse_args().image)
