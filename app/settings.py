"""Core and bundled-plugin settings. Optional plugins validate their own sources."""

from dataclasses import dataclass, field
import os
import ssl
from urllib.parse import urlsplit


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
    enabled_plugins: tuple[str, ...] = ("komodo", "temperatures", "sites")
    runtime_dir: str | None = None
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

    def __post_init__(self) -> None:
        parsed = urlsplit(self.upstream)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("PVE_UPSTREAM must be a fixed HTTPS origin, e.g. https://pve.example:8006")
        # Validate the port now, instead of turning every request into a 500.
        _ = parsed.port
        if self.ca_file and not self.tls_verify:
            raise ValueError("PVE_CA_FILE cannot be combined with PVE_TLS_VERIFY=false")

    @classmethod
    def from_env(cls) -> "Settings":
        plugins = tuple(value.strip() for value in os.environ.get(
            "PVE_PLUGINS", "komodo,temperatures,sites").split(",") if value.strip())
        return cls(
            upstream=os.environ["PVE_UPSTREAM"].rstrip("/"),
            injection_enabled=env_bool("PVE_PLUGINS_INJECTION_ENABLED", True),
            enabled_plugins=plugins,
            runtime_dir=os.environ.get("PVE_PLUGINS_RUNTIME_DIR") or None,
            tls_verify=env_bool("PVE_TLS_VERIFY", True),
            ca_file=os.environ.get("PVE_CA_FILE") or None,
            admin_users=tuple(user.strip() for user in os.environ.get("PVE_ADMIN_USERS", "root@pam").split(",") if user.strip()),
            komodo_url=os.environ.get("PVE_KOMODO_URL") or None,
            komodo_api_key=os.environ.get("PVE_KOMODO_API_KEY") or None,
            komodo_api_secret=os.environ.get("PVE_KOMODO_API_SECRET") or None,
            komodo_tls_verify=env_bool("PVE_KOMODO_TLS_VERIFY", True) if "komodo" in plugins else True,
            action_state_file=os.environ.get("PVE_KOMODO_ACTION_STATE_FILE") or None,
            guest_links_file=os.environ.get("PVE_KOMODO_GUEST_LINKS_FILE") or None,
            temperature_sources_file=os.environ.get("PVE_TEMPERATURES_SOURCES_FILE") or None,
            site_state_file=os.environ.get("PVE_SITES_STATE_FILE") or None,
        )

    def tls_context(self) -> ssl.SSLContext | bool:
        if not self.tls_verify:
            return False
        return ssl.create_default_context(cafile=self.ca_file)
