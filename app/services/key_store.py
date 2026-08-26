"""
Scrypt-backed storage for Relay API keys (P5 Phase 1).

KeyStore reads and writes the ``api_keys`` table in the shared
``platform.db`` SQLite file (``state_dir/platform.db``). It persists only
scrypt hashes of API keys: raw key material is generated, returned to
the caller exactly once, and never written to disk. The file is created
with user-only permissions.

The ``api_keys`` table is part of the platform migration history
(``PlatformStore.MIGRATIONS`` + ``PRAGMA user_version``), folded unchanged
from the legacy ``relay_keys.db`` schema. The ``key_hash`` column is
never indexed: verification iterates active rows with constant-time
digest comparison so the database cannot leak which key matched through
timing.

File-level concerns (migrations, permissions, corruption recovery) are
owned by ``PlatformStore``; this module keeps the public ``KeyStore`` API
unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import threading
import time
import uuid
from typing import List, Optional

from app.services import platform_store
from app.services.platform_store import PlatformStoreError

SCHEMA_VERSION = platform_store.SCHEMA_VERSION

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

# P6.2 prune grace window (D4): terminal rows (revoked, or expired with
# ``expires_at`` in the past) become prune candidates only after this many
# days; ``relay keys prune`` defaults to it.
_PRUNE_GRACE_DAYS = 30

# P6.2 expiring-soon window (D6): a key whose ``expires_at`` lands within
# this many days is flagged ``expires_soon`` in its metadata.
_EXPIRY_WINDOW_DAYS = 7

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

_SELECT_COLUMNS = (
    "id, key_hash, key_salt, kdf, label, scopes, expires_at, "
    "created_at, last_used_at, revoked_at"
)


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


def validate_expires_at(expires_at: Optional[float]) -> Optional[float]:
    """Validate and normalize an optional finite Unix expiry timestamp.

    Booleans are rejected explicitly because ``bool`` is an ``int``
    subclass.  The same rule is used by the API and the persistence layer
    so a direct store caller cannot create a key that bypasses expiration
    semantics with NaN or infinity.
    """
    if expires_at is None:
        return None
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise ValueError("expires_at must be a finite unix timestamp")

    try:
        normalized = float(expires_at)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("expires_at must be a finite unix timestamp") from exc

    if not math.isfinite(normalized):
        raise ValueError("expires_at must be a finite unix timestamp")
    return normalized


def _safe_persisted_expiry(expires_at) -> Optional[float]:
    """Treat legacy/corrupt non-finite expiry values as already expired."""
    try:
        return validate_expires_at(expires_at)
    except ValueError:
        return 0.0


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
        self._path = path or str(platform_store.default_path())
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

        expires_at = validate_expires_at(expires_at)

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

    def delete(self, key_id: str) -> bool:
        """
        Permanently remove a key row (P5 Phase 4).

        Hard delete regardless of revocation state. Returns True when a
        row was removed; False when the key was unknown. Intended for
        administrative cleanup only; the normal lifecycle path is
        ``revoke``.
        """
        self._ensure_open()

        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM api_keys WHERE id = ?",
                    (key_id,),
                )

        return cursor.rowcount > 0

    def prune(self, cutoff_ts: float) -> tuple:
        """
        Delete terminal key rows that became terminal before ``cutoff_ts``.

        Terminal means revoked, or expired (``expires_at`` in the past).
        Rows still valid are never touched. Returns ``(removed, scanned)``
        where ``scanned`` is the number of rows examined.
        """
        self._ensure_open()

        with self._lock:
            scanned = self._conn.execute(
                "SELECT count(*) FROM api_keys"
            ).fetchone()[0]

            with self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM api_keys"
                    " WHERE (revoked_at IS NOT NULL AND revoked_at < ?)"
                    "    OR (expires_at IS NOT NULL AND expires_at <= ?)",
                    (cutoff_ts, cutoff_ts),
                )
                removed = cursor.rowcount

        return removed, scanned

    def list_terminal(self, cutoff_ts: float) -> List[dict]:
        """
        Return metadata for every key ``prune`` would delete at
        ``cutoff_ts``, oldest created first.

        Uses the exact ``prune`` predicate (P6.3 dedupe of the CLI's
        Python mirror, which previously drifted at the expiry boundary:
        the SQL predicate removes keys whose ``expires_at`` equals the
        cutoff).
        """
        self._ensure_open()

        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM api_keys"
                " WHERE (revoked_at IS NOT NULL AND revoked_at < ?)"
                "    OR (expires_at IS NOT NULL AND expires_at <= ?)"
                " ORDER BY created_at",
                (cutoff_ts, cutoff_ts),
            ).fetchall()

        return [self._row_to_meta(row) for row in rows]

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
            try:
                _, (n, r, p) = _parse_kdf(row[3])
                digest = _scrypt(token, row[2], n, r, p)
            except (KeyStoreError, ValueError, IndexError, MemoryError):
                continue

            if not _constant_time_eq(digest, row[1]):
                continue

            meta = self._row_to_meta(row)

            if row[9] is not None:
                return {"status": "revoked", "meta": meta}

            expires_at = _safe_persisted_expiry(row[6])
            if expires_at is not None and expires_at <= now:
                return {"status": "expired", "meta": meta}

            return {"status": "ok", "meta": meta}

        return {"status": "invalid", "meta": None}

    def authenticate(self, token: str) -> dict:
        """
        Verify a token and classify a matching revoked/expired row in one
        constant-time scan.

        The auth dependency needs both the successful metadata and the
        failure status for an unsuccessful token.  Keeping those operations
        together prevents one request from performing the full scrypt scan
        twice.  Existing ``verify`` and ``classify`` callers retain their
        original behavior; this method is the combined request-path API.
        """
        if not token:
            return {"status": "invalid", "meta": None}

        now = time.time()
        self._ensure_open()

        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM api_keys"
            ).fetchall()

        matched_status = None
        matched_meta = None

        for row in rows:
            try:
                _, (n, r, p) = _parse_kdf(row[3])
                digest = _scrypt(token, row[2], n, r, p)
            except (KeyStoreError, ValueError, IndexError, MemoryError):
                continue

            if not _constant_time_eq(digest, row[1]):
                continue

            meta = self._row_to_meta(row)

            if row[9] is not None:
                matched_status = "revoked"
            else:
                expires_at = _safe_persisted_expiry(row[6])
                if expires_at is not None and expires_at <= now:
                    matched_status = "expired"
                else:
                    matched_status = "ok"

            matched_meta = meta

        if matched_status == "ok":
            self.mark_used(matched_meta["id"])

        return {"status": matched_status or "invalid", "meta": matched_meta}

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
            expires_at = _safe_persisted_expiry(row[6])

            if expires_at is not None and expires_at <= now:
                continue

            try:
                _, (n, r, p) = _parse_kdf(row[3])
                digest = _scrypt(token, row[2], n, r, p)
            except (KeyStoreError, ValueError, IndexError, MemoryError):
                continue

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
        expires_at = _safe_persisted_expiry(row[6])
        return {
            "id": row[0],
            "label": row[4],
            "scopes": KeyStore._decode_scopes(row[5]),
            "expires_at": expires_at,
            "created_at": row[7],
            "last_used_at": row[8],
            "revoked_at": row[9],
            "expires_soon": KeyStore._expires_soon(expires_at),
        }

    @staticmethod
    def _expires_soon(expires_at: Optional[float]) -> bool:
        """
        True when ``expires_at`` lands within the expiring-soon window.
        False for no-expiry keys and for keys already past expiry.
        """
        expires_at = _safe_persisted_expiry(expires_at)
        if expires_at is None:
            return False

        now = time.time()
        return now < expires_at <= now + _EXPIRY_WINDOW_DAYS * 86400

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
        try:
            self._conn = platform_store.open_connection(self._path)
        except PlatformStoreError as exc:
            raise KeyStoreError(str(exc)) from exc
