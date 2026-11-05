"""Core and bundled-plugin settings. Optional plugins validate their own sources."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import ssl

from app.pve_upstream import canonical_upstream_origin, parse_upstream_nodes, parse_upstream_server_names


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, str(default)).lower().strip()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    upstream: str
    injection_enabled: bool = True
    enabled_plugins: tuple[str, ...] = ("sites",)
    runtime_dir: str | None = None
    data_dir: str | None = None
    tls_verify: bool = True
    ca_file: str | None = None
    admin_users: tuple[str, ...] = ("root@pam",)
    komodo_url: str | None = None
    komodo_api_key: str | None = field(default=None, repr=False)
    komodo_api_secret: str | None = field(default=None, repr=False)
    komodo_tls_verify: bool = True
    action_state_file: str | None = None
    guest_links_file: str | None = None
    temperature_sources_file: str | None = None
    site_state_file: str | None = None
    config_state_file: str | None = None
    safeline_state_file: str | None = None
    temperature_sources: dict | None = field(default=None, repr=False)
    upstreams: tuple[str, ...] = ()
    upstream_server_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.upstreams, tuple) or not isinstance(self.upstream_server_names, tuple)
                or any(not isinstance(name, str) for name in self.upstream_server_names)):
            raise ValueError("PVE upstreams and TLS server names must be tuples")
        primary = canonical_upstream_origin(self.upstream)
        origins = tuple(canonical_upstream_origin(value) for value in self.upstreams) if self.upstreams else (primary,)
        if origins[0] != primary or len(origins) > 16 or len(set(origins)) != len(origins):
            raise ValueError("PVE upstreams must be unique and start with the stable primary origin")
        names = parse_upstream_server_names(",".join(self.upstream_server_names), len(origins))
        object.__setattr__(self, "upstream", primary)
        object.__setattr__(self, "upstreams", origins)
        object.__setattr__(self, "upstream_server_names", names)
        if self.ca_file and not self.tls_verify:
            raise ValueError("PVE_CA_FILE cannot be combined with PVE_TLS_VERIFY=false")

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("PVE_PLUGINS_DATA_DIR") or Path.cwd() / "data").resolve()
        upstreams = parse_upstream_nodes(os.environ["PVE_UPSTREAM"])
        return cls(
            upstream=upstreams[0],
            upstreams=upstreams,
            upstream_server_names=parse_upstream_server_names(os.environ.get("PVE_TLS_SERVER_NAMES"), len(upstreams)),
            injection_enabled=env_bool("PVE_PLUGINS_INJECTION_ENABLED", True),
            data_dir=str(data_dir),
            runtime_dir=os.environ.get("PVE_PLUGINS_RUNTIME_DIR") or None,
            tls_verify=env_bool("PVE_TLS_VERIFY", False),
            ca_file=os.environ.get("PVE_CA_FILE") or None,
            admin_users=tuple(user.strip() for user in os.environ.get("PVE_ADMIN_USERS", "root@pam").split(",") if user.strip()),
            action_state_file=str(data_dir / "actions.sqlite3"),
            guest_links_file=str(data_dir / "guest-links.sqlite3"),
            site_state_file=str(data_dir / "sites.sqlite3"),
            config_state_file=str(data_dir / "plugins.json"),
            safeline_state_file=str(data_dir / "safeline.sqlite3"),
        )

    def tls_context(self) -> ssl.SSLContext | bool:
        if not self.tls_verify:
            return False
        return ssl.create_default_context(cafile=self.ca_file)
