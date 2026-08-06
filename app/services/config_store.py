"""
Provider configuration persistence.

The *only* module allowed to write provider configuration. For P1 the
target is the user's ``.env`` file (runtime-compatible with ``Settings``);
the P6 ``platform.db`` swap replaces this module's implementation, and the
P5 keyring migration replaces only the ``api_key`` path. Nothing in the
wizard or CLI writes dotenv directly.

Every write is atomic (P7.2): the change is merged in memory on a sibling
temp file, then ``os.replace`` swaps it over the target, so a concurrent
reader never observes a half-written file and a crashed writer never leaves
a partial ``.env``. A best-effort advisory lock serializes concurrent
writers so read-modify-write cycles do not lose each other's updates.

Keys are never printed or logged by this module.
"""

import contextlib
import os
import tempfile
import threading
import time

from dotenv import dotenv_values, set_key, unset_key

from app.core.config import env_file, settings
from app.providers.registry import ProviderDefinition
from app.services.provider_key_store import provider_key_store

# Guards writers inside this process; the advisory file lock covers other
# processes (the CLI and the embedded server share one .env).
_lock = threading.Lock()


@contextlib.contextmanager
def _advisory_lock():
    """
    Best-effort exclusive advisory lock on ``<.env>.lock``.

    Serializes separate writer processes around the read-modify-replace
    cycle. Acquisition retries briefly and then proceeds: atomic replace
    already prevents corruption, so a contended lock only risks a lost
    update, never a broken file.
    """
    lock_path = env_file.parent / (env_file.name + ".lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    locked = False

    try:
        if os.path.getsize(lock_path) == 0:
            os.write(fd, b"\x00")
            os.fsync(fd)

        os.lseek(fd, 0, os.SEEK_SET)

        if os.name == "nt":
            import msvcrt

            for _ in range(20):
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl

            for _ in range(20):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.05)

        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001 - unlock is best-effort
                pass

        os.close(fd)


def _apply_atomic(changes: dict[str, str], removals: set[str]) -> None:
    """
    Merge ``changes`` (set) and ``removals`` (unset) into the active
    ``.env`` in one atomic replace.

    The current content is copied to a sibling temp file, the mutation is
    applied there with the same python-dotenv formatters the previous
    in-place writers used (quote_mode="always", existing lines preserved),
    then the temp file is fsynced and ``os.replace`` swaps it into place.
    On POSIX the final file is tightened to ``0600``. The temp file is
    removed on any failure.
    """
    env_file.parent.mkdir(parents=True, exist_ok=True)

    with _lock:
        with _advisory_lock():
            source = str(env_file)

            tmp_path = None
            tmp = None
            try:
                tmp = tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=env_file.name + ".",
                    suffix=".tmp",
                    dir=str(env_file.parent),
                    delete=False,
                )
                tmp_path = tmp.name

                if os.path.exists(source):
                    with open(source, "rb") as src:
                        tmp.write(src.read())
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp.close()
                tmp = None

                for key, value in changes.items():
                    set_key(tmp_path, key, value, quote_mode="always")
                for key in removals:
                    unset_key(tmp_path, key)

                with open(tmp_path, "r+b") as finalized:
                    os.fsync(finalized.fileno())

                if os.name != "nt":
                    os.chmod(tmp_path, 0o600)

                os.replace(tmp_path, source)

                if os.name != "nt":
                    os.chmod(source, 0o600)
            finally:
                if tmp is not None:
                    try:
                        tmp.close()
                    except OSError:
                        pass
                if tmp_path is not None and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass


def set_env(key: str, value: str) -> None:
    """
    Write a single value into the active ``.env`` file atomically.

    On POSIX the file is tightened to user-only (``0600``) so provider keys
    never sit at a umask-broad mode; Windows relies on the user-profile ACL
    instead.
    """
    _apply_atomic({key: value}, set())


def unset_env(key: str) -> None:
    """
    Remove a single value from the active ``.env`` file if present.
    """
    _apply_atomic({}, {key})


def get_env(key: str, default: str = "") -> str:
    """
    Read a value from the active ``.env`` file, falling back to the
    process environment. The file is the single writer's source of truth,
    so a value saved but not yet reloaded is still visible here (this is
    what makes restore-on-failure rollback correct).
    """
    values = dotenv_values(str(env_file))

    if key in values:
        return values[key]

    return os.getenv(key, default)


def set_provider_config(
    defn: ProviderDefinition,
    *,
    enabled: bool | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    priority_models: list[str] | None = None,
) -> None:
    """
    Persist one provider's configuration.

    ``None`` means "leave unchanged"; pass ``""`` explicitly to clear a
    value. The caller (wizard) decides what to send; this module never
    logs, echoes, or masks keys — it only writes them.
    """
    if enabled is not None:
        set_env(defn.enabled_env, "true" if enabled else "false")

    if api_key is not None and defn.key_env:
        if settings.relay_keyring_enabled:
            # Keyring-first writes (P5 Phase 2): keys go to the OS vault,
            # never to .env. A key write failure raises (no plaintext
            # fallback). Non-key fields are still written to .env below.
            if api_key:
                provider_key_store.set(defn.id, api_key)
            else:
                provider_key_store.remove(defn.id)
        else:
            set_env(defn.key_env, api_key)

    if base_url is not None and defn.base_url_env:
        set_env(defn.base_url_env, base_url)

    if priority_models is not None:
        if defn.priority_env:
            set_env(defn.priority_env, ",".join(priority_models))
