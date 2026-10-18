"""Durable request reservations prevent duplicate submissions after a reload.

Only hashes, selected resource IDs and sanitized results are stored. An
interrupted reservation is never resumed automatically: the caller must inspect
Komodo's operation history before consciously submitting another request.
"""

import hashlib
import json
import sqlite3
import time


class ActionStoreError(Exception):
    def __init__(self, code: str, status_code: int = 503) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class ActionStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._connection = None

    def _db(self) -> sqlite3.Connection:
        # Open on the first write request, so read-only pages still work if the
        # state mount is unavailable. Fail closed instead of falling back to an
        # in-memory ledger that could repeat a destructive action after reload.
        if self._connection is None:
            self._connection = sqlite3.connect(self.path, timeout=2, check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("""CREATE TABLE IF NOT EXISTS actions (
                scope TEXT NOT NULL, request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, created REAL NOT NULL,
                status INTEGER, result TEXT,
                PRIMARY KEY(scope, request_id)
            )""")
            self._connection.commit()
        return self._connection

    @staticmethod
    def fingerprint(payload: dict) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def reserve(self, scope: str, request_id: str, payload: dict) -> tuple[int, dict] | None:
        fingerprint = self.fingerprint(payload)
        try:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT fingerprint, status, result FROM actions WHERE scope=? AND request_id=?",
                (scope, request_id),
            ).fetchone()
            if row is not None:
                db.commit()
                if row[0] != fingerprint:
                    raise ActionStoreError("idempotency_conflict", 409)
                if row[1] is None:
                    raise ActionStoreError("action_already_requested", 409)
                return row[1], json.loads(row[2])
            # Keep receipts, including accepted/unknown outcomes, indefinitely.
            # Expiring them could silently turn an old UUID into another write.
            if db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] >= 4096:
                db.rollback()
                raise ActionStoreError("action_store_full")
            db.execute(
                "INSERT INTO actions(scope,request_id,fingerprint,created) VALUES(?,?,?,?)",
                (scope, request_id, fingerprint, time.time()),
            )
            db.commit()
        except (sqlite3.Error, ValueError):
            if self._connection is not None:
                self._connection.rollback()
            raise ActionStoreError("action_store_unavailable") from None
        return None

    def finish(self, scope: str, request_id: str, status: int, result: dict) -> None:
        try:
            db = self._db()
            db.execute(
                "UPDATE actions SET status=?,result=? WHERE scope=? AND request_id=? AND status IS NULL",
                (status, json.dumps(result, separators=(",", ":")), scope, request_id),
            )
            db.commit()
        except sqlite3.Error:
            if self._connection is not None:
                self._connection.rollback()
            # Keep the reservation pending on failure. Never suggest retrying
            # an operation just because writing its receipt failed.
            raise ActionStoreError("action_receipt_unavailable") from None

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
