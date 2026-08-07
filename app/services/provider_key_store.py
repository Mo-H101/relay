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
import logging
import os
import time

import keyring

SERVICE_NAME = "relay"

_BACKEND_ENV = "RELAY_KEYRING_BACKEND"

_logger = logging.getLogger("relay")


class ProviderKeyStore:
    """
    Thin wrapper around the OS keyring for per-provider keys.
    """

    def __init__(self, service: str | None = None) -> None:
        self._service = service or SERVICE_NAME
        self.last_error: str | None = None
        self.last_error_at: float | None = None

    def get(self, provider_id: str) -> str:
        """
        Return the stored key for a provider, or ``""`` when absent or
        when the keyring is unavailable.

        A keyring failure is never silent: it is logged as a warning and
        recorded in ``last_error`` / ``diagnostics()`` so callers and the
        diagnostics surface can distinguish "no stored key" from "keyring
        broken". The ``""`` fallback is preserved so the request path can
        keep recovering to the environment value.
        """
        try:
            value = _keyring().get_password(self._service, provider_id)
        except Exception as exc:  # noqa: BLE001 - surface, never crash
            self._record_failure(provider_id, exc)
            return ""

        self._clear_failure()
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

    def diagnostics(self) -> dict:
        """
        Keyring health for the diagnostics surface. ``ok`` is False only
        when the most recent ``get`` raised; the error text never contains
        key material.
        """
        if self.last_error is None:
            return {"ok": True, "error": None, "error_age_ms": None}

        age_ms = None
        if self.last_error_at is not None:
            age_ms = int((time.monotonic() - self.last_error_at) * 1000)

        return {"ok": False, "error": self.last_error, "error_age_ms": age_ms}

    def _record_failure(self, provider_id: str, exc: Exception) -> None:
        detail = f"{type(exc).__name__}: {exc}"
        self.last_error = f"keyring read for '{provider_id}' failed: {detail}"
        self.last_error_at = time.monotonic()
        _logger.warning(
            "provider keyring read failed for '%s': %s",
            provider_id,
            detail,
        )

    def _clear_failure(self) -> None:
        self.last_error = None
        self.last_error_at = None


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
