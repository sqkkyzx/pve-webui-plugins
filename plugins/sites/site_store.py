"""Shared administrator site links, pinned to one PVE instance."""

from contextlib import contextmanager
import ipaddress
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlsplit
import uuid

import httpx


_ID = re.compile(r"[0-9a-f]{32}\Z")
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_MAX_REVISION = 9_007_199_254_740_991
_MAX_SITES = 100


class SiteError(Exception):
    def __init__(self, code, status_code=503):
        self.code, self.status_code = code, status_code
        super().__init__(code)


def _controls(value):
    return any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF for char in value)


def _name(value):
    if not isinstance(value, str) or _controls(value):
        raise SiteError("invalid_site_name", 422)
    value = value.strip()
    if not 1 <= len(value) <= 100:
        raise SiteError("invalid_site_name", 422)
    return value


def _url(value):
    try:
        if not isinstance(value, str) or _controls(value) or "\\" in value:
            raise ValueError
        value = value.strip()
        if not 1 <= len(value) <= 2048 or any(char.isspace() for char in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.netloc.endswith(":")):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        host = parsed.hostname
        if "%" in host:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            ascii_host = host.encode("idna").decode("ascii").rstrip(".")
            if not ascii_host or len(ascii_host) > 253 or any(_HOST_LABEL.fullmatch(label) is None for label in ascii_host.split(".")):
                raise ValueError
        else:
            if address.version == 6 and not parsed.netloc.startswith("["):
                raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise SiteError("invalid_site_url", 422) from None


def _origin(value):
    try:
        canonical = _url(value)
        parsed = urlsplit(canonical)
        if (canonical != value or parsed.scheme != "https" or parsed.path not in {"", "/"}
                or any(char in canonical for char in "?#")):
            raise ValueError
        return str(httpx.URL(canonical)).rstrip("/")
    except (SiteError, ValueError, httpx.InvalidURL):
        raise ValueError("Sites require a fixed PVE HTTPS origin") from None


def _site_id(value):
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise SiteError("invalid_site_id", 422)
    return value


def _revision(value):
    if type(value) is not int or not 1 <= value <= _MAX_REVISION:
        raise SiteError("invalid_site_revision", 422)
    return value


def _row(row):
    try:
        result = {"id": _site_id(row["id"]), "name": _name(row["name"]),
                  "url": _url(row["url"]), "revision": _revision(row["revision"])}
        if result["name"] != row["name"] or result["url"] != row["url"]:
            raise ValueError
        return result
    except (SiteError, ValueError, TypeError, KeyError, IndexError):
        raise SiteError("site_store_unavailable") from None


class SitesStore:
    def __init__(self, path, *, pve_origin):
        self.path, self.pve_origin = path, _origin(pve_origin)
        self._connection = None
        self._lock = threading.RLock()

    def _db(self):
        if self._connection is not None:
            return self._connection
        connection = None
        try:
            if not isinstance(self.path, (str, Path)) or not str(self.path).strip():
                raise ValueError
            filename = str(self.path)
            if filename != ":memory:":
                Path(filename).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            connection = sqlite3.connect(filename, timeout=3, check_same_thread=False, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS site_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS sites (id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, revision INTEGER NOT NULL)")
            pinned = connection.execute("SELECT value FROM site_metadata WHERE key='pve_origin'").fetchone()
            if pinned is None:
                connection.execute("INSERT INTO site_metadata (key,value) VALUES ('pve_origin',?)", (self.pve_origin,))
            elif pinned["value"] != self.pve_origin:
                raise SiteError("pve_instance_mismatch")
            connection.commit()
            self._connection = connection
            return connection
        except (sqlite3.Error, OSError, ValueError, TypeError):
            if connection is not None:
                connection.close()
            raise SiteError("site_store_unavailable") from None
        except SiteError:
            if connection is not None:
                connection.close()
            raise

    @contextmanager
    def _transaction(self):
        with self._lock:
            connection = self._db()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except SiteError:
                connection.rollback()
                raise
            except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
                connection.rollback()
                raise SiteError("site_store_unavailable") from None

    def list(self):
        with self._lock:
            try:
                rows = self._db().execute("SELECT id,name,url,revision FROM sites ORDER BY rowid LIMIT 101").fetchall()
                if len(rows) > _MAX_SITES:
                    raise SiteError("site_store_unavailable")
                return [_row(row) for row in rows]
            except sqlite3.Error:
                raise SiteError("site_store_unavailable") from None

    def create(self, name, url):
        name, url = _name(name), _url(url)
        with self._transaction() as connection:
            count = connection.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
            if count >= _MAX_SITES:
                raise SiteError("site_limit_reached", 409)
            site = {"id": uuid.uuid4().hex, "name": name, "url": url, "revision": 1}
            connection.execute("INSERT INTO sites (id,name,url,revision) VALUES (?,?,?,?)",
                               (site["id"], name, url, 1))
            return site

    def update(self, site_id, name, url, *, expected_revision):
        site_id, revision = _site_id(site_id), _revision(expected_revision)
        name, url = _name(name), _url(url)
        with self._transaction() as connection:
            current = connection.execute("SELECT id,name,url,revision FROM sites WHERE id=?", (site_id,)).fetchone()
            if current is None:
                raise SiteError("site_not_found", 404)
            current = _row(current)
            if current["revision"] != revision or revision == _MAX_REVISION:
                raise SiteError("site_revision_conflict", 409)
            connection.execute("UPDATE sites SET name=?,url=?,revision=? WHERE id=? AND revision=?",
                               (name, url, revision + 1, site_id, revision))
            return {"id": site_id, "name": name, "url": url, "revision": revision + 1}

    def delete(self, site_id, *, expected_revision):
        site_id, revision = _site_id(site_id), _revision(expected_revision)
        with self._transaction() as connection:
            current = connection.execute("SELECT id,name,url,revision FROM sites WHERE id=?", (site_id,)).fetchone()
            if current is None:
                raise SiteError("site_not_found", 404)
            if _row(current)["revision"] != revision:
                raise SiteError("site_revision_conflict", 409)
            connection.execute("DELETE FROM sites WHERE id=? AND revision=?", (site_id, revision))
            return {"deleted": True, "id": site_id}

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
