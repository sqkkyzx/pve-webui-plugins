"""Local guest associations and read-only, session-scoped PVE discovery.

Only a guest's stable identity hint is persisted. Node, name and runtime state
always come from the latest visible resource list, so migration is harmless.
No method in this module sends a PVE or Komodo mutation.
"""

import hashlib
import json
import re
import sqlite3
import ssl
import threading
from urllib.parse import urlsplit
import uuid

from fastapi import Request
import httpx


_SERVER_ID = re.compile(r"[0-9a-f]{24}\Z")
_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}\Z")
_NET = re.compile(r"net[0-9]+\Z")
_MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z")
_GUEST_TYPES = frozenset({"qemu", "lxc"})


class GuestLinkError(Exception):
    def __init__(self, code: str, status_code: int = 503) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _origin(value: str, *, https_only: bool) -> str:
    try:
        if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError
        origin = urlsplit(value)
        if (origin.scheme not in ({"https"} if https_only else {"http", "https"})
                or not origin.hostname or origin.username is not None or origin.password is not None
                or origin.path not in {"", "/"} or origin.query or origin.fragment
                or any(char in value for char in "?#\\") or origin.port == 0):
            raise ValueError
        return str(httpx.URL(value)).rstrip("/")
    except (ValueError, TypeError, httpx.InvalidURL):
        raise ValueError("Guest links require a fixed HTTP(S) origin") from None


def _valid_guest(guest: dict) -> tuple[str, int]:
    if not isinstance(guest, dict):
        raise GuestLinkError("invalid_pve_guest", 422)
    guest_type = guest.get("type", guest.get("guest_type"))
    vmid = guest.get("vmid")
    if not isinstance(guest_type, str) or guest_type not in _GUEST_TYPES or type(vmid) is not int or not 1 <= vmid <= 999_999_999:
        raise GuestLinkError("invalid_pve_guest", 422)
    return guest_type, vmid


def _valid_mac(value: str) -> bool:
    return bool(_MAC.fullmatch(value) and int(value[:2], 16) & 1 == 0 and value != "00:00:00:00:00:00")


def _identity(value: dict, guest_type: str) -> dict:
    """Validate and copy the deliberately small identity allowlist."""
    if not isinstance(value, dict) or set(value) != {"kind", "values", "strength"}:
        raise GuestLinkError("invalid_guest_identity", 422)
    expected_kind = "vm_uuid" if guest_type == "qemu" else "ct_mac"
    values = value.get("values")
    if value.get("kind") != expected_kind or not isinstance(values, list) or len(values) > 32:
        raise GuestLinkError("invalid_guest_identity", 422)
    normalized = []
    for item in values:
        if not isinstance(item, str):
            raise GuestLinkError("invalid_guest_identity", 422)
        if guest_type == "qemu":
            try:
                parsed_uuid = uuid.UUID(item)
                if parsed_uuid.int == 0:
                    raise ValueError
                normalized.append(str(parsed_uuid))
            except ValueError:
                raise GuestLinkError("invalid_guest_identity", 422) from None
        elif _valid_mac(item.lower()):
            normalized.append(item.lower())
        else:
            raise GuestLinkError("invalid_guest_identity", 422)
    normalized = sorted(set(normalized))
    if guest_type == "qemu" and len(normalized) > 1:
        raise GuestLinkError("invalid_guest_identity", 422)
    strength = ("strong" if guest_type == "qemu" else "weak") if normalized else "unavailable"
    if value.get("strength") != strength:
        raise GuestLinkError("invalid_guest_identity", 422)
    return {"kind": expected_kind, "values": normalized, "strength": strength}


class GuestLinkStore:
    """A durable one-to-one map with optimistic revisions, including tombstones.

    ``cluster_id`` identifies this installation independently of guest node
    names. A pinned PVE origin prevents accidentally inheriting links when an
    installation changes its upstream. It is not a PVE cluster identity claim.
    A fixed Komodo origin namespaces server IDs. Restoring the database restores
    the namespace, upstream pin and all optimistic revisions.
    """

    def __init__(self, path: str, *, komodo_origin: str, pve_origin: str) -> None:
        origin = urlsplit(_origin(komodo_origin, https_only=False))
        port = origin.port or (443 if origin.scheme == "https" else 80)
        host = f"[{origin.hostname.lower()}]" if ":" in origin.hostname else origin.hostname.lower()
        authority = f"{origin.scheme.lower()}://{host}:{port}"
        self.scope = hashlib.sha256(authority.encode()).hexdigest()
        self._pve_origin = _origin(pve_origin, https_only=True)
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            # SQLite treats an empty path as an ephemeral temporary database.
            # Only the explicit :memory: spelling is permitted in tests.
            if not isinstance(self.path, str) or not self.path.strip():
                raise GuestLinkError("guest_link_store_unavailable")
            connection = sqlite3.connect(self.path, timeout=2, check_same_thread=False)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE IF NOT EXISTS guest_link_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("""CREATE TABLE IF NOT EXISTS guest_links (
                    scope TEXT NOT NULL, server_id TEXT NOT NULL,
                    guest_type TEXT, vmid INTEGER, identity TEXT,
                    revision INTEGER NOT NULL, active INTEGER NOT NULL,
                    PRIMARY KEY(scope, server_id)
                )""")
                connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS guest_link_target
                    ON guest_links(scope, guest_type, vmid) WHERE active=1""")
                pin = connection.execute("SELECT value FROM guest_link_meta WHERE key='pve_origin'").fetchone()
                if pin is not None and pin[0] != self._pve_origin:
                    raise GuestLinkError("pve_instance_mismatch")
                if pin is None:
                    if connection.execute("SELECT COUNT(*) FROM guest_links").fetchone()[0]:
                        raise GuestLinkError("pve_instance_mismatch")
                    connection.execute("INSERT INTO guest_link_meta(key,value) VALUES('pve_origin',?)", (self._pve_origin,))
                connection.execute("INSERT OR IGNORE INTO guest_link_meta(key,value) VALUES('cluster_id',?)", (str(uuid.uuid4()),))
                connection.commit()
            except Exception:
                connection.close()
                raise
            self._connection = connection
        return self._connection

    @staticmethod
    def _server(server_id: str) -> str:
        if not isinstance(server_id, str) or _SERVER_ID.fullmatch(server_id) is None:
            raise GuestLinkError("invalid_server_id", 422)
        return server_id

    @staticmethod
    def _revision(expected_revision: int) -> None:
        if type(expected_revision) is not int or expected_revision < 0:
            raise GuestLinkError("invalid_guest_link_revision", 422)

    def _cluster_id(self, db: sqlite3.Connection) -> str:
        row = db.execute("SELECT value FROM guest_link_meta WHERE key='cluster_id'").fetchone()
        return str(uuid.UUID(row[0]))

    def _rollback(self) -> None:
        with self._lock:
            if self._connection is not None:
                try:
                    self._connection.rollback()
                except sqlite3.Error:
                    # Preserve the fixed, sanitized error on an unavailable
                    # filesystem even if rolling back also encounters I/O.
                    pass

    def cluster_id(self) -> str:
        try:
            with self._lock:
                return self._cluster_id(self._db())
        except (sqlite3.Error, ValueError, TypeError):
            raise GuestLinkError("guest_link_store_unavailable") from None

    def _link(self, db: sqlite3.Connection, row: tuple) -> dict:
        server_id, guest_type, vmid, identity, revision, active = row
        try:
            self._server(server_id)
        except GuestLinkError:
            raise ValueError("Invalid stored server ID") from None
        if type(revision) is not int or revision < 1 or active not in {0, 1}:
            raise ValueError("Invalid stored guest link")
        if not active:
            return {"server_id": server_id, "cluster_id": self._cluster_id(db), "revision": revision, "active": False}
        try:
            _valid_guest({"type": guest_type, "vmid": vmid})
            fingerprint = _identity(json.loads(identity), guest_type)
        except GuestLinkError:
            raise ValueError("Invalid stored guest identity") from None
        return {
            "server_id": server_id, "cluster_id": self._cluster_id(db),
            "guest_type": guest_type, "vmid": vmid, "revision": revision,
            "identity": fingerprint, "active": True,
        }

    def get(self, server_id: str, *, include_inactive: bool = False) -> dict | None:
        self._server(server_id)
        try:
            with self._lock:
                db = self._db()
                row = db.execute("SELECT server_id,guest_type,vmid,identity,revision,active FROM guest_links WHERE scope=? AND server_id=?", (self.scope, server_id)).fetchone()
                if row is None or (not row[5] and not include_inactive):
                    return None
                return self._link(db, row)
        except (sqlite3.Error, ValueError, TypeError):
            raise GuestLinkError("guest_link_store_unavailable") from None

    def revision(self, server_id: str) -> int:
        row = self.get(server_id, include_inactive=True)
        return row["revision"] if row else 0

    def list(self) -> list[dict]:
        try:
            with self._lock:
                db = self._db()
                rows = db.execute("SELECT server_id,guest_type,vmid,identity,revision,active FROM guest_links WHERE scope=? AND active=1 ORDER BY server_id", (self.scope,)).fetchall()
                return [self._link(db, row) for row in rows]
        except (sqlite3.Error, ValueError, TypeError):
            raise GuestLinkError("guest_link_store_unavailable") from None

    def save(self, server_id: str, guest: dict, identity: dict, *, expected_revision: int, confirmed: bool = False) -> dict:
        self._server(server_id)
        self._revision(expected_revision)
        guest_type, vmid = _valid_guest(guest)
        fingerprint = _identity(identity, guest_type)
        if not fingerprint["values"]:
            raise GuestLinkError("guest_identity_unavailable", 409)
        if type(confirmed) is not bool:
            raise GuestLinkError("invalid_guest_link_confirmation", 422)
        try:
            with self._lock:
                db = self._db()
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT server_id,guest_type,vmid,identity,revision,active FROM guest_links WHERE scope=? AND server_id=?", (self.scope, server_id)).fetchone()
                revision = row[4] if row else 0
                if expected_revision != revision:
                    raise GuestLinkError("guest_link_revision_conflict", 409)
                if row and row[5] and (row[1:3] != (guest_type, vmid) or json.loads(row[3]) != fingerprint) and not confirmed:
                    raise GuestLinkError("guest_link_confirmation_required", 409)
                owner = db.execute("SELECT server_id FROM guest_links WHERE scope=? AND guest_type=? AND vmid=? AND active=1", (self.scope, guest_type, vmid)).fetchone()
                if owner and owner[0] != server_id:
                    raise GuestLinkError("pve_guest_already_linked", 409)
                revision += 1
                db.execute("""INSERT INTO guest_links(scope,server_id,guest_type,vmid,identity,revision,active)
                    VALUES(?,?,?,?,?,?,1) ON CONFLICT(scope,server_id) DO UPDATE SET
                    guest_type=excluded.guest_type,vmid=excluded.vmid,identity=excluded.identity,
                    revision=excluded.revision,active=1""", (self.scope, server_id, guest_type, vmid, json.dumps(fingerprint, separators=(",", ":")), revision))
                db.commit()
                return self._link(db, (server_id, guest_type, vmid, json.dumps(fingerprint), revision, 1))
        except GuestLinkError:
            self._rollback()
            raise
        except (sqlite3.Error, ValueError, TypeError):
            self._rollback()
            raise GuestLinkError("guest_link_store_unavailable") from None

    def unlink(self, server_id: str, *, expected_revision: int) -> dict:
        self._server(server_id)
        self._revision(expected_revision)
        try:
            with self._lock:
                db = self._db()
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT revision,active FROM guest_links WHERE scope=? AND server_id=?", (self.scope, server_id)).fetchone()
                revision = row[0] if row else 0
                if expected_revision != revision:
                    raise GuestLinkError("guest_link_revision_conflict", 409)
                if row and row[1]:
                    revision += 1
                    db.execute("UPDATE guest_links SET guest_type=NULL,vmid=NULL,identity=NULL,revision=?,active=0 WHERE scope=? AND server_id=?", (revision, self.scope, server_id))
                db.commit()
                return {"server_id": server_id, "cluster_id": self._cluster_id(db), "revision": revision, "active": False}
        except GuestLinkError:
            self._rollback()
            raise
        except (sqlite3.Error, ValueError, TypeError):
            self._rollback()
            raise GuestLinkError("guest_link_store_unavailable") from None

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None


class PveGuestClient:
    def __init__(self, upstream: str, *, verify: ssl.SSLContext | bool = True, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.upstream = _origin(upstream, https_only=True)
        self.verify = verify
        self.transport = transport

    async def _get(self, request: Request, path: str, *, params: dict | None = None):
        # The outer route authenticates the session. Keep this boundary strict
        # as well: never forward arbitrary cookies, credentials or auth headers.
        cookie = request.cookies.get("PVEAuthCookie", "")
        if (not cookie or len(cookie) > 4096
                or any(ord(char) < 0x21 or ord(char) > 0x7e or char in ";," for char in cookie)):
            raise GuestLinkError("pve_guest_session_required", 401)
        try:
            async with httpx.AsyncClient(verify=self.verify, transport=self.transport,
                    timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False, trust_env=False) as client:
                response = await client.get(self.upstream + path, params=params,
                    headers={"Cookie": "PVEAuthCookie=" + cookie, "Accept": "application/json"})
        except httpx.HTTPError:
            raise GuestLinkError("pve_guest_unavailable") from None
        if response.status_code in {401, 403, 404}:
            raise GuestLinkError("pve_guest_inaccessible", 409)
        if response.status_code != 200:
            raise GuestLinkError("pve_guest_unavailable")
        try:
            payload = response.json()
        except ValueError:
            raise GuestLinkError("pve_guest_unavailable") from None
        if (not isinstance(payload, dict) or "errors" in payload
                or payload.get("success", True) is not True or "data" not in payload):
            raise GuestLinkError("pve_guest_unavailable")
        return payload["data"]

    async def list_guests(self, request: Request) -> list[dict]:
        data = await self._get(request, "/api2/json/cluster/resources", params={"type": "vm"})
        if not isinstance(data, list) or len(data) > 100_000:
            raise GuestLinkError("pve_guest_unavailable")
        guests, seen = [], set()
        for row in data:
            if not isinstance(row, dict):
                raise GuestLinkError("pve_guest_unavailable")
            if not isinstance(row.get("type"), str):
                raise GuestLinkError("pve_guest_unavailable")
            if row.get("type") not in _GUEST_TYPES:
                continue
            try:
                guest_type, vmid = _valid_guest(row)
            except GuestLinkError:
                raise GuestLinkError("pve_guest_unavailable") from None
            node = row.get("node")
            name = row.get("name", "")
            status = row.get("status", "unknown")
            template = row.get("template", 0)
            if (not isinstance(node, str) or _NODE.fullmatch(node) is None
                    or not isinstance(name, str) or len(name) > 256
                    or type(template) not in {int, bool} or template not in {0, 1}
                    or (guest_type, vmid) in seen):
                raise GuestLinkError("pve_guest_unavailable")
            if not isinstance(status, str) or status not in {"running", "stopped", "unknown", "paused"}:
                status = "unknown"
            seen.add((guest_type, vmid))
            guests.append({"id": f"{guest_type}/{vmid}", "guest_type": guest_type, "vmid": vmid,
                "node": node, "name": name, "status": status, "template": bool(template)})
        return guests

    async def fingerprint(self, request: Request, guest: dict) -> dict:
        guest_type, vmid = _valid_guest(guest)
        node = guest.get("node")
        if not isinstance(node, str) or _NODE.fullmatch(node) is None:
            raise GuestLinkError("invalid_pve_guest", 422)
        data = await self._get(request, f"/api2/json/nodes/{node}/{guest_type}/{vmid}/config")
        if not isinstance(data, dict):
            raise GuestLinkError("pve_guest_unavailable")
        if guest_type == "qemu":
            values = []
            smbios = data.get("smbios1")
            if isinstance(smbios, str):
                candidates = [item.partition("=")[2] for item in smbios.split(",") if item.partition("=")[0] == "uuid"]
                if len(candidates) == 1:
                    try:
                        value = uuid.UUID(candidates[0])
                        if value.int != 0:
                            values = [str(value)]
                    except ValueError:
                        pass
            return {"kind": "vm_uuid", "values": values, "strength": "strong" if values else "unavailable"}
        values = set()
        for key, value in data.items():
            if isinstance(key, str) and _NET.fullmatch(key) and isinstance(value, str):
                macs = [part.partition("=")[2].lower() for part in value.split(",") if part.partition("=")[0] == "hwaddr"]
                if len(macs) == 1 and _valid_mac(macs[0]):
                    values.add(macs[0])
        return {"kind": "ct_mac", "values": sorted(values), "strength": "weak" if values else "unavailable"}


def assess_link(link: dict, guests: list[dict] | None, identity: dict | None = None, *, unavailable: bool = False) -> dict:
    """Resolve a link against fresh visibility and identity without mutating it.

    ``guests=None`` or ``unavailable=True`` means a temporary query failure;
    absence from a successful visible list means missing *or inaccessible*.
    Neither condition deletes/rebinds the persistent association.
    """
    result = {"server_id": link["server_id"], "cluster_id": link["cluster_id"],
        "guest_type": link["guest_type"], "vmid": link["vmid"], "revision": link["revision"],
        "identity_strength": link["identity"]["strength"], "status": "missing",
        "reason": "missing_or_inaccessible", "can_navigate": False,
        "node": None, "name": None, "guest_status": None}
    if guests is None:
        return {**result, "status": "unavailable", "reason": "pve_guest_unavailable"}
    guest = next((row for row in guests if row["guest_type"] == link["guest_type"] and row["vmid"] == link["vmid"]), None)
    if guest is None:
        return result
    result.update({"node": guest["node"], "name": guest["name"], "guest_status": guest["status"]})
    if unavailable:
        return {**result, "status": "unavailable", "reason": "pve_guest_unavailable"}
    if identity is None:
        return {**result, "status": "unavailable", "reason": "identity_inaccessible"}
    try:
        observed = _identity(identity, link["guest_type"])
        expected = _identity(link["identity"], link["guest_type"])
    except GuestLinkError:
        return {**result, "status": "needs_confirmation", "reason": "identity_unavailable"}
    if not expected["values"] or not observed["values"]:
        return {**result, "status": "needs_confirmation", "reason": "identity_unavailable"}
    if expected != observed:
        return {**result, "status": "needs_confirmation", "reason": "identity_changed"}
    return {**result, "status": "linked", "reason": None, "can_navigate": True}
