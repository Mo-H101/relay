"""
Provider configuration persistence.

The *only* module allowed to write provider configuration. For P1 the
target is the user's ``.env`` file (runtime-compatible with ``Settings``);
the P6 ``relay.db`` swap replaces this module's implementation, and the
P5 keyring migration replaces only the ``api_key`` path. Nothing in the
wizard or CLI writes dotenv directly.

Keys are never printed or logged by this module.
"""

import os

from dotenv import set_key

from app.core.config import env_file
from app.providers.registry import ProviderDefinition


def set_env(key: str, value: str) -> None:
    """
    Write a single value into the active ``.env`` file.
    """
    set_key(str(env_file), key, value, quote_mode="always")


def get_env(key: str, default: str = "") -> str:
    """
    Read a value from the process environment.
    """
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
        set_env(defn.key_env, api_key)

    if base_url is not None and defn.base_url_env:
        set_env(defn.base_url_env, base_url)

    if priority_models is not None:
        if defn.priority_env:
            set_env(defn.priority_env, ",".join(priority_models))
