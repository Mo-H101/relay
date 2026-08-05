"""
Scrypt-backed storage for Relay API keys (P5 Phase 1).

KeyStore is the only component that touches the ``relay_keys.db`` SQLite
file. It persists only scrypt hashes of API keys: raw key material is
generated, returned to the caller exactly once, and never written to
disk. The file lives in the per-user state directory and is created with
user-only permissions.

The schema follows the ``StateStore`` migration convention (``MIGRATIONS``
dict + ``PRAGMA user_version``) so the ``api_keys`` table can be folded
into the P6 ``platform.db`` unchanged. The ``key_hash`` column is never
indexed: verification iterates active rows with constant-time digest
comparison so the database cannot leak which key matched through timing.

Nothing in the request path reads this store yet (Phase 1 is storage
only); the auth dependency consumes it in Phase 4.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from typing import List, Optional

from app.core.config import state_dir

SCHEMA_VERSION = 1

# scrypt parameters. Stored per row in the ``kdf`` column so parameters
# can be raised later without invalidating existing hashes.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_LEN = 16

# Raw key format: ``rl_`` + 43 base62 characters (~256 bits of entropy).
# The prefix gives redaction layers a stable shape to mask everywhere.
RAW_PREFIX = "rl_"
RAW_KEY_CHARS = 43

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

_SELECT_COLUMNS = (
    "id, key_hash, key_salt, kdf, label, scopes, expires_at, "
    "created_at, last_used_at, revoked_at"
)

MIGRATIONS: dict = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            key_hash BLOB NOT NULL,
            key_salt BLOB NOT NULL,
            kdf TEXT NOT NULL,
            label TEXT NOT NULL,
            scopes TEXT NOT NULL,
            expires_at REAL,
            created_at REAL NOT NULL,
            last_used_at REAL,
            revoked_at REAL
        )
        """,
    ],
}


class KeyStoreError(Exception):
    """Raised when the key store cannot be opened, migrated, or read."""


def _base62_encode(data: bytes) -> str:
    """
    Encode bytes as a fixed-width base62 string (no leading-zero loss).
    """
    number = int.from_bytes(data, "big")
    chars: List[str] = []

    while number:
        number, remainder = divmod(number, 62)
        chars.append(_BASE62[remainder])

    return "".join(reversed(chars)).rjust(RAW_KEY_CHARS, "0")


def _generate_raw_key() -> str:
    """
    Return a fresh opaque raw key (``rl_`` + 43 base62 chars).
    """
    return RAW_PREFIX + _base62_encode(secrets.token_bytes(32))


def _encode_kdf() -> str:
    """
    Serialize the current KDF algorithm and parameters into the ``kdf``
    column value (e.g. ``"scrypt|16384|8|1"``).
    """
    return f"scrypt|{SCRYPT_N}|{SCRYPT_R}|{SCRYPT_P}"


def _parse_kdf(kdf: str):
    """
    Parse a stored ``kdf`` column value into ``(algorithm, (n, r, p))``.
    """
    parts = kdf.split("|")
    algorithm = parts[0]

    if algorithm != "scrypt":
        raise KeyStoreError(f"unsupported KDF: {kdf!r}")

    return algorithm, (int(parts[1]), int(parts[2]), int(parts[3]))


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    """
    Hash ``password`` with scrypt using the given parameters.
    """
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=SCRYPT_DKLEN,
    )


def _constant_time_eq(left: bytes, right: bytes) -> bool:
    """
    Compare two digests in constant time so neither content nor length
    differences leak through timing.
    """
    return hmac.compare_digest(left, right)


class KeyStore:
    """
    SQLite-backed store for scrypt-hashed Relay API keys.

    Single guarded connection with WAL journaling and a busy timeout,
    mirroring ``StateStore``. The database is created on first open with
    user-only file permissions. Raw keys are never persisted: ``create``
    returns the raw key once and every other surface returns metadata
    only.
    """

    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or str(state_dir / "relay_keys.db")
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def create(
        self,
        label: str,
        scopes: Optional[list] = None,
        expires_at: Optional[float] = None,
    ) -> tuple:
        """
        Create a new API key. Returns ``(key_id, raw_key)``; the raw key
        is generated here, returned exactly once, and never persisted.
        """
        label = (label or "").strip()

        if not label:
            raise ValueError("label is required")

        raw_key = _generate_raw_key()
        key_id = uuid.uuid4().hex
        salt = secrets.token_bytes(SALT_LEN)
        digest = _scrypt(raw_key, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
        now = time.time()

        self._ensure_open()

        with self._lock:
            with self._conn:
                self._conn.execute(
                    f"INSERT INTO api_keys ("
                    f"  {_SELECT_COLUMNS}"
                    f") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key_id,
                        digest,
                        salt,
                        _encode_kdf(),
                        label,
                        json.dumps(list(scopes or [])),
                        expires_at,
                        now,
                        None,
                        None,
                    ),
                )

        return key_id, raw_key

    def get_by_id(self, key_id: str) -> Optional[dict]:
        """
        Return metadata for one key (never the hash or raw material).
        """
        self._ensure_open()

        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM api_keys WHERE id = ?",
                (key_id,),
            ).fetchone()

        return self._row_to_meta(row) if row is not None else None

    def list(self) -> List[dict]:
        """
        Return metadata for every key, oldest created first.
        """
        self._ensure_open()

        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM api_keys"
                " ORDER BY created_at"
            ).fetchall()

        return [self._row_to_meta(row) for row in rows]

    def revoke(self, key_id: str) -> bool:
        """
        Revoke a key. Returns True when a previously active key was
        revoked; False when the key is unknown or already revoked.
        """
        self._ensure_open()

        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE api_keys SET revoked_at = ?"
                    " WHERE id = ? AND revoked_at IS NULL",
                    (time.time(), key_id),
                )

        return cursor.rowcount > 0

    def mark_used(self, key_id: str) -> None:
        """
        Record the last successful use time for a key. No-op for unknown
        or revoked keys.
        """
        self._ensure_open()

        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE api_keys SET last_used_at = ?"
                    " WHERE id = ? AND revoked_at IS NULL",
                    (time.time(), key_id),
                )

    def rotate(self, key_id: str) -> Optional[tuple]:
        """
        Replace a key with a fresh one: create the replacement, then
        revoke the original. Returns ``(new_key_id, new_raw_key)`` or
        None when the original key does not exist.
        """
        meta = self.get_by_id(key_id)

        if meta is None:
            return None

        new_id, raw_key = self.create(
            label=meta["label"],
            scopes=meta["scopes"],
            expires_at=meta["expires_at"],
        )
        self.revoke(key_id)
        return new_id, raw_key

    def classify(self, token: str) -> dict:
        """
        Classify a raw key against every stored row (P5 Phase 3).

        Read-only: never records ``last_used_at`` and never mutates state.
        Returns ``{"status": ..., "meta": ...}`` where ``status`` is one
        of ``ok`` / ``invalid`` / ``expired`` / ``revoked`` and ``meta``
        is the matched key's metadata (or None). The same constant-time
        scrypt + digest loop as ``verify`` scans all rows, not just active
        ones, so a revoked or expired key still resolves to its row.
        """
        if not token:
            return {"status": "invalid", "meta": None}

        now = time.time()
        self._ensure_open()

        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM api_keys"
            ).fetchall()

        for row in rows:
            _, (n, r, p) = _parse_kdf(row[3])
            digest = _scrypt(token, row[2], n, r, p)

            if not _constant_time_eq(digest, row[1]):
                continue

            meta = self._row_to_meta(row)

            if row[9] is not None:
                return {"status": "revoked", "meta": meta}

            if row[6] is not None and row[6] <= now:
                return {"status": "expired", "meta": meta}

            return {"status": "ok", "meta": meta}

        return {"status": "invalid", "meta": None}

    def verify(self, token: str) -> Optional[dict]:
        """
        Verify a raw key against active (not revoked, not expired) rows.

        Every active row is hashed and compared in constant time; a match
        records ``last_used_at`` and returns the key metadata. Returns
        None when the token matches no active key.
        """
        if not token:
            return None

        now = time.time()
        self._ensure_open()

        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM api_keys"
                " WHERE revoked_at IS NULL"
            ).fetchall()

        matched = None

        for row in rows:
            expires_at = row[6]

            if expires_at is not None and expires_at <= now:
                continue

            _, (n, r, p) = _parse_kdf(row[3])
            digest = _scrypt(token, row[2], n, r, p)

            if _constant_time_eq(digest, row[1]):
                matched = row

        if matched is not None:
            self.mark_used(matched[0])

        return self._row_to_meta(matched) if matched is not None else None

    def memory_counts(self) -> dict:
        """
        Count persisted rows by revocation status. Metadata only; never
        exposes stored values.
        """
        self._ensure_open()

        with self._lock:
            total = self._conn.execute(
                "SELECT count(*) FROM api_keys"
            ).fetchone()[0]
            revoked = self._conn.execute(
                "SELECT count(*) FROM api_keys WHERE revoked_at IS NOT NULL"
            ).fetchone()[0]

        return {
            "total": total,
            "active": total - revoked,
            "revoked": revoked,
        }

    # ============================
    # Internals
    # ============================

    @staticmethod
    def _row_to_meta(row) -> dict:
        return {
            "id": row[0],
            "label": row[4],
            "scopes": KeyStore._decode_scopes(row[5]),
            "expires_at": row[6],
            "created_at": row[7],
            "last_used_at": row[8],
            "revoked_at": row[9],
        }

    @staticmethod
    def _decode_scopes(text: str) -> list:
        if not text:
            return []

        try:
            value = json.loads(text)
        except ValueError:
            return []

        return value if isinstance(value, list) else []

    def _ensure_open(self) -> None:
        with self._lock:
            if self._conn is None:
                self._open()

    def _open(self) -> None:
        last_error: Optional[Exception] = None

        for attempt in range(2):
            conn = sqlite3.connect(self._path, check_same_thread=False)

            try:
                conn.execute("PRAGMA busy_timeout = 5000")
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                self._migrate(conn)
            except KeyStoreError:
                conn.close()
                raise
            except sqlite3.Error as exc:
                conn.close()
                last_error = exc

                if attempt == 0:
                    self._backup_corrupt()
                    continue

            else:
                self._conn = conn
                self._secure_file_permissions()
                return

        raise KeyStoreError(f"cannot open key store database: {last_error}")

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]

        if version > self.SCHEMA_VERSION:
            raise KeyStoreError(
                f"key store schema version {version} is newer than "
                f"supported version {self.SCHEMA_VERSION}; upgrade the app."
            )

        for target in range(version + 1, self.SCHEMA_VERSION + 1):
            statements = MIGRATIONS.get(target)

            if not statements:
                raise KeyStoreError(
                    f"no migration defined for schema version {target}"
                )

            with conn:
                for statement in statements:
                    conn.execute(statement)

                conn.execute(f"PRAGMA user_version = {target}")

    def _secure_file_permissions(self) -> None:
        if os.name == "nt":
            return

        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def _backup_corrupt(self) -> None:
        backup_path = f"{self._path}.corrupt-{int(time.time())}.bak"

        try:
            if os.path.exists(self._path):
                shutil.copy2(self._path, backup_path)
                os.remove(self._path)
        except OSError:
            return

        for suffix in ("-wal", "-shm"):
            try:
                side = f"{self._path}{suffix}"

                if os.path.exists(side):
                    os.remove(side)
            except OSError:
                pass
