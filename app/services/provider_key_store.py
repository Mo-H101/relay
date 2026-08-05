"""
OS-keyring-backed storage for upstream provider API keys (P5 Phase 1).

Provider keys are stored in the operating system credential store via the
``keyring`` package: service name ``"relay"``, username = provider id
(``nvidia``, ``openai``, ...). The backend encrypts the material itself;
Relay never handles a master key.

``RELAY_KEYRING_BACKEND`` overrides the backend with a dotted
``module.Class`` path (used by tests and headless servers). The backend is
resolved per call, so the override takes effect without a restart. When
the keyring is unavailable, ``get`` falls back to returning ``""`` and
``set``/``remove`` raise; the request path (Phase 2) treats an empty
result as "no stored key" and falls back to the environment value.
"""

from __future__ import annotations

import importlib
import os

import keyring

SERVICE_NAME = "relay"

_BACKEND_ENV = "RELAY_KEYRING_BACKEND"


class ProviderKeyStore:
    """
    Thin wrapper around the OS keyring for per-provider keys.
    """

    def __init__(self, service: str | None = None) -> None:
        self._service = service or SERVICE_NAME

    def get(self, provider_id: str) -> str:
        """
        Return the stored key for a provider, or ``""`` when absent or
        when the keyring is unavailable.
        """
        try:
            value = _keyring().get_password(self._service, provider_id)
        except Exception:
            return ""

        return value or ""

    def set(self, provider_id: str, value: str) -> None:
        """
        Store a provider key. Raises when the keyring backend is
        unavailable.
        """
        _keyring().set_password(self._service, provider_id, value)

    def remove(self, provider_id: str) -> None:
        """
        Delete a provider key. Idempotent: removing a missing entry is a
        no-op. Raises when the keyring backend is unavailable.
        """
        try:
            _keyring().delete_password(self._service, provider_id)
        except keyring.errors.PasswordDeleteError:
            pass


def _keyring():
    """
    Return the keyring module with any ``RELAY_KEYRING_BACKEND`` override
    applied. Reads the override on every call so tests can switch backends
    without restarting.
    """
    backend = _configured_backend()

    if backend is not None:
        keyring.set_keyring(backend)

    return keyring


def _configured_backend():
    """
    Instantiate the backend named by ``RELAY_KEYRING_BACKEND``, or None
    when unset. An unparseable override falls back to the OS default.
    """
    override = os.getenv(_BACKEND_ENV, "").strip()

    if not override:
        return None

    module_name, _, class_name = override.rpartition(".")

    try:
        module = importlib.import_module(module_name)
        return getattr(module, class_name)()
    except Exception:
        return None


provider_key_store = ProviderKeyStore()
