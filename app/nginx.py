"""Render the optional container gateway without changing the PVE server."""

import ipaddress
from pathlib import Path
import re
from urllib.parse import urlsplit

from app.settings import Settings


def parse_trusted_proxies(value: str | None) -> tuple[str, ...]:
    if not value or not value.strip():
        return ()
    networks = []
    for item in value.split(","):
        try:
            network = str(ipaddress.ip_network(item.strip(), strict=False))
        except ValueError:
            raise ValueError("PVE_TRUSTED_PROXIES must contain comma-separated IP networks") from None
        if network not in networks:
            networks.append(network)
    return tuple(networks)


def _quoted(value: str | Path) -> str:
    text = str(value)
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError("Gateway paths must not contain control characters")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def gateway_path(value: str | Path) -> Path:
    """Resolve paths against the App working directory as literal Nginx paths."""
    text = str(value)
    if "$" in text or any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError("Gateway paths must not contain dollar signs or control characters")
    path = Path(value).resolve()
    if "$" in str(path):
        raise ValueError("Gateway paths must not contain dollar signs")
    return path


def render_nginx_config(
    settings: Settings,
    *,
    runtime_dir: Path,
    certificate: Path,
    private_key: Path,
    trusted_proxies: tuple[str, ...] = (),
) -> str:
    """Keep safe reads retryable and every other method on one upstream attempt."""
    runtime_dir = gateway_path(runtime_dir)
    certificate = gateway_path(certificate)
    private_key = gateway_path(private_key)
    origins = settings.upstreams or (settings.upstream,)
    names = settings.upstream_server_names
    if settings.tls_verify and len(names) != len(origins):
        raise ValueError("PVE_TLS_SERVER_NAMES must name every node when the internal proxy verifies TLS")
    servers = []
    tls_names = []
    for index, origin in enumerate(origins):
        parsed = urlsplit(origin)
        host = parsed.hostname
        if not host:
            raise ValueError("Gateway upstreams require valid HTTPS origins")
        # Settings canonicalizes HTTPS :443 away; bare input IPs already have
        # their default :8006 supplied by parse_upstream_nodes.
        port = parsed.port or 443
        address = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        backup = " backup" if index else ""
        servers.append(f"        server {address} max_fails=1 fail_timeout=5s{backup};")
        name = names[index] if names else host
        # A retried request has a comma-separated upstream_addr. Match the last
        # address and evaluate the map again for each TLS handshake.
        pattern = "~" + re.escape(address) + "$"
        tls_names.append(f"        {_quoted(pattern)} {_quoted(name)};")
    networks = "\n".join(f"        {network} 1;" for network in trusted_proxies)
    ca_file = gateway_path(settings.ca_file or "/etc/ssl/certs/ca-certificates.crt")
    tls_policy = "\n".join([
        f"        proxy_ssl_verify {'on' if settings.tls_verify else 'off'};",
        f"        proxy_ssl_trusted_certificate {_quoted(ca_file)};" if settings.tls_verify else "",
    ]).rstrip()
    return f"""# Generated at container startup. Configure the gateway through environment variables.
worker_processes 1;
daemon off;
pid {_quoted(runtime_dir / 'nginx.pid')};
# Network errors can include console tickets in their request URI.
error_log /dev/stderr crit;

events {{
    worker_connections 1024;
}}

http {{
    access_log off;
    server_tokens off;
    client_body_temp_path {_quoted(runtime_dir / 'client-body')};
    proxy_temp_path {_quoted(runtime_dir / 'proxy-temp')};
    fastcgi_temp_path {_quoted(runtime_dir / 'fastcgi-temp')};
    uwsgi_temp_path {_quoted(runtime_dir / 'uwsgi-temp')};
    scgi_temp_path {_quoted(runtime_dir / 'scgi-temp')};

    map $http_upgrade $pve_connection_upgrade {{
        default upgrade;
        '' close;
    }}
    map "$request_method:$http_upgrade" $pve_websocket_request {{
        default 0;
        "~*^GET:websocket$" 1;
    }}
    geo $pve_trusted_peer {{
        default 0;
{networks}
    }}
    map "$pve_trusted_peer:$http_x_forwarded_proto" $pve_forwarded_proto {{
        default $scheme;
        "1:http" http;
        "1:https" https;
    }}
    map $pve_trusted_peer $pve_forwarded_for {{
        default $remote_addr;
        1 $proxy_add_x_forwarded_for;
    }}
    map $upstream_addr $pve_upstream_name {{
        volatile;
        default {_quoted(names[0] if names else urlsplit(origins[0]).hostname)};
{chr(10).join(tls_names)}
    }}

    upstream pve_original {{
{chr(10).join(servers)}
    }}
    upstream pve_plugins_backend {{
        server 127.0.0.1:8008;
    }}

    server {{
        listen 8006 ssl;
        listen [::]:8006 ssl;
        ssl_certificate {_quoted(certificate)};
        ssl_certificate_key {_quoted(private_key)};
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_session_tickets off;

        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $pve_forwarded_proto;
        proxy_set_header X-Forwarded-For $pve_forwarded_for;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $pve_connection_upgrade;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_connect_timeout 5s;
        client_body_timeout 3600s;
        client_max_body_size 0;
        proxy_next_upstream off;
        proxy_ssl_server_name on;
        proxy_ssl_name $pve_upstream_name;
{tls_policy}

        location = / {{
            if ($request_method !~ ^(GET|HEAD)$) {{ return 405; }}
            proxy_pass http://pve_plugins_backend;
            proxy_intercept_errors on;
            error_page 502 503 504 = @pve_plain_desktop;
            proxy_hide_header Cache-Control;
            add_header Cache-Control "private, no-store" always;
        }}

        location ^~ /pve-plugins/ {{
            proxy_pass http://pve_plugins_backend;
        }}

        location / {{
            # The internal redirect retains the original URI and query string.
            error_page 418 = @pve_read;
            error_page 419 = @pve_websocket;
            if ($pve_websocket_request) {{ return 419; }}
            if ($request_method ~ ^(GET|HEAD)$) {{ return 418; }}
            proxy_pass https://pve_original;
        }}

        location @pve_read {{
            proxy_pass https://pve_original;
            proxy_read_timeout 10s;
            proxy_next_upstream error timeout http_502 http_503 http_504;
            proxy_next_upstream_tries {len(origins)};
        }}

        location @pve_websocket {{
            proxy_pass https://pve_original;
            proxy_read_timeout 3600s;
            proxy_next_upstream error timeout http_502 http_503 http_504;
            proxy_next_upstream_tries {len(origins)};
        }}

        location @pve_plain_desktop {{
            proxy_pass https://pve_original;
            proxy_read_timeout 10s;
            proxy_next_upstream error timeout http_502 http_503 http_504;
            proxy_next_upstream_tries {len(origins)};
            proxy_hide_header Cache-Control;
            add_header Cache-Control "private, no-store" always;
        }}
    }}
}}
"""
