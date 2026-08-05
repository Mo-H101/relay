"""
Provider configuration persistence.

The *only* module allowed to write provider configuration. For P1 the
target is the user's ``.env`` file (runtime-compatible with ``Settings``);
the P6 ``platform.db`` swap replaces this module's implementation, and the
P5 keyring migration replaces only the ``api_key`` path. Nothing in the
wizard or CLI writes dotenv directly.

Keys are never printed or logged by this module.
"""

import os

from dotenv import dotenv_values, set_key, unset_key

from app.core.config import env_file, settings
from app.providers.registry import ProviderDefinition
from app.services.provider_key_store import provider_key_store


def set_env(key: str, value: str) -> None:
    """
    Write a single value into the active ``.env`` file.

    On POSIX the file is tightened to user-only (``0600``) after the write
    so provider keys never sit at a umask-broad mode; Windows relies on the
    user-profile ACL instead.
    """
    env_file.parent.mkdir(parents=True, exist_ok=True)
    set_key(str(env_file), key, value, quote_mode="always")

    if os.name != "nt":
        try:
            os.chmod(str(env_file), 0o600)
        except OSError:
            pass


def unset_env(key: str) -> None:
    """
    Remove a single value from the active ``.env`` file if present.
    """
    unset_key(str(env_file), key)


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
