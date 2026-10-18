"""Lazy, private local configuration. SafeLine credentials never leave the backend."""

from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
import re
import sqlite3
import ssl
import threading
from urllib.parse import urlsplit

import certifi


class SafeLineError(Exception):
    def __init__(self, code, status=502):
        self.code, self.status = code, status
        super().__init__(code)


def origin(value):
    try:
        if not isinstance(value, str) or not value or len(value) > 512 or any(
            ord(c) <= 32 or ord(c) >= 127 or c in "\\?#" for c in value
        ):
            raise ValueError
        p = urlsplit(value)
        if (p.scheme != "https" or not p.hostname or p.username is not None or p.password is not None
                or p.path not in {"", "/"} or p.netloc.endswith(":")):
            raise ValueError
        if p.port is not None and not 1 <= p.port <= 65535:
            raise ValueError
        try:
            address = ipaddress.ip_address(p.hostname)
        except ValueError:
            if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", p.hostname) is None:
                raise ValueError
            if p.hostname.lower() in {"localhost", "metadata.google.internal"} or p.hostname.endswith(".localhost"):
                raise ValueError
        else:
            if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
                raise ValueError
        return value.rstrip("/")
    except (ValueError, UnicodeError):
        raise SafeLineError("invalid_safeline_url", 422) from None


@dataclass(frozen=True, repr=False)
class Connection:
    url: str
    token: str = field(repr=False)
    tls_verify: bool = True
    tls_server_name: str = ""
    ca_pem: str = ""

    def __post_init__(self):
        object.__setattr__(self, "url", origin(self.url))
        if not isinstance(self.token, str) or not 1 <= len(self.token) <= 4096 or any(
            ord(c) < 33 or ord(c) > 126 for c in self.token
        ):
            raise SafeLineError("invalid_safeline_token", 422)
        if type(self.tls_verify) is not bool:
            raise SafeLineError("invalid_tls_settings", 422)
        name = self.tls_server_name
        if not isinstance(name, str) or len(name) > 253 or (name and re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", name
        ) is None):
            raise SafeLineError("invalid_tls_server_name", 422)
        if not isinstance(self.ca_pem, str) or len(self.ca_pem) > 65536 or (self.ca_pem and not self.tls_verify):
            raise SafeLineError("invalid_tls_settings", 422)
        self.verify()

    def verify(self):
        if not self.tls_verify:
            return False
        try:
            context = ssl.create_default_context(cafile=certifi.where())
            if self.ca_pem:
                context.load_verify_locations(cadata=self.ca_pem)
            return context
        except (ValueError, ssl.SSLError):
            raise SafeLineError("invalid_ca_certificate", 422) from None

    def public(self):
        return {"url": self.url, "token_configured": True, "tls_verify": self.tls_verify,
                "tls_server_name": self.tls_server_name, "ca_pem": self.ca_pem}


class ConnectionStore:
    def __init__(self, path, pve_origin):
        self.path, self.pve_origin = str(path), pve_origin
        self._connection = None
        self._lock = threading.RLock()

    def _db(self):
        if self._connection is not None:
            return self._connection
        db = None
        try:
            path = Path(self.path)
            if self.path != ":memory:":
                path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                # Create before sqlite opens it, so no world-readable interval exists.
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                os.fchmod(fd, 0o600)
                os.close(fd)
            db = sqlite3.connect(self.path, timeout=3, check_same_thread=False, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS connection (
                id INTEGER PRIMARY KEY CHECK (id=1), pve_origin TEXT NOT NULL,
                url TEXT NOT NULL, token TEXT NOT NULL, tls_verify INTEGER NOT NULL,
                tls_server_name TEXT NOT NULL, ca_pem TEXT NOT NULL, revision INTEGER NOT NULL)""")
            row = db.execute("SELECT pve_origin FROM connection WHERE id=1").fetchone()
            if row and row[0] != self.pve_origin:
                raise SafeLineError("safeline_instance_mismatch", 503)
            db.commit()
            self._connection = db
            return db
        except SafeLineError:
            if db is not None:
                db.close()
            raise
        except (OSError, ValueError, sqlite3.Error):
            if db is not None:
                db.close()
            raise SafeLineError("safeline_store_unavailable", 503) from None

    def load(self):
        with self._lock:
            try:
                row = self._db().execute("SELECT * FROM connection WHERE id=1").fetchone()
                if row:
                    return (Connection(row["url"], row["token"], bool(row["tls_verify"]), row["tls_server_name"], row["ca_pem"])
                            if row["url"] else None), row["revision"], "saved"
                # Deployment credentials only seed an installation with no saved record.
                url, token = os.environ.get("SAFELINE_URL"), os.environ.get("SAFELINE_API_TOKEN")
                if url and token:
                    ca = os.environ.get("SAFELINE_CA_FILE")
                    return Connection(url, token, True, os.environ.get("SAFELINE_TLS_SERVER_NAME", ""),
                                      Path(ca).read_text() if ca else ""), 0, "environment"
                return None, 0, "none"
            except (OSError, sqlite3.Error, TypeError, ValueError):
                raise SafeLineError("safeline_store_unavailable", 503) from None

    def save(self, config, expected_revision):
        with self._lock:
            db = self._db()
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT revision FROM connection WHERE id=1").fetchone()
                revision = row[0] if row else 0
                if type(expected_revision) is not int or expected_revision != revision:
                    raise SafeLineError("safeline_connection_changed", 409)
                if config is None:
                    values = ("", "", 1, "", "")
                else:
                    values = (config.url, config.token, int(config.tls_verify), config.tls_server_name, config.ca_pem)
                db.execute("INSERT OR REPLACE INTO connection VALUES (1,?,?,?,?,?,?,?)",
                           (self.pve_origin, *values, revision + 1))
                db.commit()
            except SafeLineError:
                db.rollback()
                raise
            except sqlite3.Error:
                db.rollback()
                raise SafeLineError("safeline_store_unavailable", 503) from None
        return self.load()

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
