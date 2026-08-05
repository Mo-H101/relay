"""
Unit tests for the scrypt-backed KeyStore (P5 Phase 1).

Storage only: no auth, API, or provider runtime wiring is exercised here.
"""

import os
import stat
import time

import pytest

from app.services.key_store import (
    RAW_PREFIX,
    SALT_LEN,
    SCRYPT_DKLEN,
    KeyStore,
    KeyStoreError,
    _base62_encode,
    _constant_time_eq,
    _encode_kdf,
)


@pytest.fixture
def store(tmp_path):
    instance = KeyStore(tmp_path / "relay_keys.db")
    yield instance
    instance.close()


def test_create_returns_id_and_raw_key(store):
    key_id, raw = store.create("opencode")
    assert key_id
    assert len(key_id) == 32  # uuid4 hex
    assert raw.startswith(RAW_PREFIX)
    assert len(raw) == len(RAW_PREFIX) + 43


def test_raw_key_not_persisted(store, tmp_path):
    _, raw = store.create("opencode")
    db_bytes = (tmp_path / "relay_keys.db").read_bytes()
    assert raw.encode("utf-8") not in db_bytes


def test_verify_roundtrip(store):
    key_id, raw = store.create("opencode", scopes=["chat", "v1"])
    meta = store.verify(raw)
    assert meta is not None
    assert meta["id"] == key_id
    assert meta["label"] == "opencode"
    assert meta["scopes"] == ["chat", "v1"]


def test_verify_wrong_token(store):
    store.create("opencode")
    assert store.verify(RAW_PREFIX + "a" * 43) is None


def test_verify_empty_token(store):
    assert store.verify("") is None
    assert store.verify(None) is None


def test_salt_uniqueness(store, tmp_path):
    store.create("one")
    store.create("two")
    store.close()

    import sqlite3

    conn = sqlite3.connect(tmp_path / "relay_keys.db")
    conn_rows = conn.execute(
        "SELECT key_salt FROM api_keys ORDER BY created_at"
    ).fetchall()
    conn.close()

    assert len(conn_rows) == 2
    assert conn_rows[0][0] != conn_rows[1][0]


def test_hash_is_scrypt_digest(store, tmp_path):
    _, raw = store.create("opencode")
    store.close()

    import sqlite3

    conn = sqlite3.connect(tmp_path / "relay_keys.db")
    key_hash, key_salt, kdf = conn.execute(
        "SELECT key_hash, key_salt, kdf FROM api_keys"
    ).fetchone()
    conn.close()

    assert len(key_hash) == SCRYPT_DKLEN
    assert len(key_salt) == SALT_LEN
    assert raw.encode("utf-8") not in key_hash
    assert kdf == _encode_kdf()
    assert kdf == "scrypt|16384|8|1"


def test_kdf_encoding_roundtrip():
    assert _encode_kdf() == "scrypt|16384|8|1"

    from app.services.key_store import _parse_kdf

    algorithm, (n, r, p) = _parse_kdf("scrypt|16384|8|1")
    assert (algorithm, n, r, p) == ("scrypt", 16384, 8, 1)

    with pytest.raises(KeyStoreError):
        _parse_kdf("pbkdf2|1000")


def test_expired_key_is_rejected(store):
    _, past_raw = store.create("past", expires_at=0)
    _, future_raw = store.create("future", expires_at=2 ** 31)
    assert store.verify(past_raw) is None
    assert store.verify(future_raw) is not None


def test_expiry_controls_verification(store, tmp_path):
    import time

    key_id, raw = store.create("ephemeral", expires_at=time.time() + 3600)
    assert store.verify(raw)["id"] == key_id

    key_id2, raw2 = store.create("gone", expires_at=time.time() - 10)
    assert store.verify(raw2) is None


def test_revoke_rejects_and_lists(store):
    key_id, raw = store.create("opencode")
    assert store.verify(raw) is not None
    assert store.revoke(key_id) is True
    assert store.verify(raw) is None
    assert store.revoke(key_id) is False

    listed = store.list()
    assert listed[0]["id"] == key_id
    assert listed[0]["revoked_at"] is not None


def test_revoke_unknown_key(store):
    assert store.revoke("does-not-exist") is False


def test_rotate_replaces_and_revokes(store):
    key_id, raw = store.create("opencode")
    new_id, new_raw = store.rotate(key_id)

    assert new_id != key_id
    assert new_raw != raw
    assert store.verify(new_raw)["id"] == new_id
    assert store.verify(raw) is None
    assert store.get_by_id(key_id)["revoked_at"] is not None


def test_rotate_unknown_key(store):
    assert store.rotate("does-not-exist") is None


def test_mark_used(store):
    key_id, raw = store.create("opencode")
    assert store.get_by_id(key_id)["last_used_at"] is None

    store.mark_used(key_id)
    assert store.get_by_id(key_id)["last_used_at"] is not None

    store.mark_used("does-not-exist")  # no-op, must not raise


def test_list_returns_metadata_only(store):
    store.create("one")
    store.create("two")

    entries = store.list()
    assert [entry["label"] for entry in entries] == ["one", "two"]

    for entry in entries:
        assert set(entry.keys()) == {
            "id",
            "label",
            "scopes",
            "expires_at",
            "created_at",
            "last_used_at",
            "revoked_at",
            "expires_soon",
        }


def test_get_by_id_metadata_only(store):
    key_id, _ = store.create("opencode")
    meta = store.get_by_id(key_id)
    assert meta["id"] == key_id
    assert "key_hash" not in meta
    assert "key_salt" not in meta
    assert store.get_by_id("missing") is None


def test_scopes_default_and_custom(store):
    key_id, _ = store.create("default-scopes")
    assert store.get_by_id(key_id)["scopes"] == []

    key_id, _ = store.create("scoped", scopes=["admin"])
    assert store.get_by_id(key_id)["scopes"] == ["admin"]


def test_expires_soon_window(store):
    now = time.time()

    soon_id, _ = store.create("soon", expires_at=now + 3 * 86400)
    later_id, _ = store.create("later", expires_at=now + 30 * 86400)
    none_id, _ = store.create("none")
    expired_id, _ = store.create("expired", expires_at=now - 1)

    assert store.get_by_id(soon_id)["expires_soon"] is True
    assert store.get_by_id(later_id)["expires_soon"] is False
    assert store.get_by_id(none_id)["expires_soon"] is False
    assert store.get_by_id(expired_id)["expires_soon"] is False

    # Listing carries the same flag.
    flags = {entry["label"]: entry["expires_soon"] for entry in store.list()}
    assert flags["soon"] is True
    assert flags["later"] is False


def test_label_is_required(store):
    with pytest.raises(ValueError):
        store.create("")


def test_schema_version_and_table(store, tmp_path):
    store.close()

    import sqlite3

    conn = sqlite3.connect(tmp_path / "relay_keys.db")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'api_keys'"
    ).fetchone()
    conn.close()

    assert version == KeyStore.SCHEMA_VERSION
    assert table is not None


def test_wal_mode(store):
    with store._lock:
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_file_permissions_user_only(store, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permission check")

    mode = stat.S_IMODE(os.stat(tmp_path / "relay_keys.db").st_mode)
    assert mode == 0o600


def test_sidecar_permissions_user_only(store, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permission check")

    store.create("sidecar-perm")

    for suffix in ("", "-wal", "-shm"):
        path = tmp_path / f"relay_keys.db{suffix}"

        if not path.exists():
            continue

        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, suffix


def test_corrupt_backup_permissions_user_only(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permission check")

    path = tmp_path / "relay_keys.db"
    path.write_bytes(b"this is not a sqlite database at all")

    store = KeyStore(path)
    store.create("after-corruption")
    store.close()

    backups = list(tmp_path.glob("relay_keys.db.corrupt-*.bak"))
    assert len(backups) == 1

    mode = stat.S_IMODE(os.stat(backups[0]).st_mode)
    assert mode == 0o600


def test_memory_counts(store):
    key_id, _ = store.create("opencode")
    store.create("scoped", scopes=["admin"])
    store.revoke(key_id)

    counts = store.memory_counts()
    assert counts["total"] == 2
    assert counts["active"] == 1
    assert counts["revoked"] == 1


def test_reopen_roundtrip(tmp_path):
    path = tmp_path / "relay_keys.db"

    first = KeyStore(path)
    key_id, raw = first.create("persistent")
    first.close()

    second = KeyStore(path)
    assert second.verify(raw)["id"] == key_id
    second.close()


def test_corrupt_database_recovers(tmp_path):
    path = tmp_path / "relay_keys.db"
    path.write_bytes(b"this is not a sqlite database at all")

    store = KeyStore(path)
    key_id, raw = store.create("after-corruption")
    assert store.verify(raw)["id"] == key_id
    store.close()

    backups = list(tmp_path.glob("relay_keys.db.corrupt-*.bak"))
    assert len(backups) == 1


def test_close_is_idempotent(store):
    store.close()
    store.close()


def test_verify_reopens_after_close(store):
    store.close()
    key_id, raw = store.create("reopened")
    assert store.verify(raw)["id"] == key_id


def test_constant_time_eq():
    assert _constant_time_eq(b"a" * 32, b"a" * 32)
    assert not _constant_time_eq(b"a" * 32, b"b" * 32)
    assert not _constant_time_eq(b"a" * 31, b"a" * 32)


def test_verify_matches_any_active_row(store):
    store.create("first")
    key_id, raw = store.create("second")
    assert store.verify(raw)["id"] == key_id


def test_base62_encode_fixed_width():
    assert len(_base62_encode(b"\x00" * 32)) == 43
    assert len(_base62_encode(b"\xff" * 32)) == 43
