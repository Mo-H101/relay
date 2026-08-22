"""
Hot configuration reload for Relay.

Re-reads the project .env, validates a fresh Settings() instance, and
applies only an explicit allowlist of reloadable fields by mutating the
in-process settings singleton and Provider objects in place. Never
touches persistence enabling, path, or flush timing, logging,
health-refresh timing, the LM Studio base URL, or server bind settings.
The sole persistence exception is the retention window
(persistence_retention_days), which is pushed to the running flusher so
pruning can be tightened without a restart.

Provider model discovery on reload is best-effort: a failure keeps the
existing models and is reported as a redacted failure instead of rolling
back the whole reload. Secrets (API keys) are reported by field name
only and are never echoed in responses, logs, or exceptions.

Dry-run mode reports what would change without mutating anything.
"""

import os
import re
import threading
from contextlib import contextmanager

from dotenv import dotenv_values

from app.core.config import PROJECT_ROOT, Settings, settings
from app.core.config_spec import (
    reload_secret_fields as _spec_reload_secret_fields,
    reloadable_fields as _spec_reloadable_fields,
    simple_reloadable_fields as _spec_simple_fields,
)
from app.providers.base import apply_model_priority
from app.providers.factory import build_runtime_provider, resolve_provider_key
from app.providers.registry import PROVIDER_REGISTRY, RUNTIME_READY
from app.services.redaction import safe_provider_error

# Settings read dynamically at request time and safe to reload in place.
# The reload allowlist lives in app/core/config_spec.py (the single source
# of truth for setting metadata); the derived tuples below reproduce the
# exact field names and ordering the hand-maintained lists previously had.
_SIMPLE_FIELDS = _spec_simple_fields()

# Secrets: reported by field name only.
_SECRET_FIELDS = _spec_reload_secret_fields()

# Runtime provider specs are derived from the provider registry (P4.1) so
# the registry is the single source of runtime truth. Only providers wired
# into routing (RUNTIME_READY) are reloadable in this phase.
_PROVIDER_SPECS = tuple(
    {
        "id": defn.id,
        "prefix": defn.id,
        "defn": defn,
        "factory": build_runtime_provider,
        "client": defn.client,
    }
    for defn in PROVIDER_REGISTRY.values()
    if defn.id in RUNTIME_READY
)

_RELOADABLE_FIELDS = _spec_reloadable_fields()

_lock = threading.Lock()


@contextmanager
def _env_overlay(dotenv_path):
    """
    Temporarily overlay a .env file onto os.environ so a fresh
    Settings() validates the file's values, then restore the previous
    environment. The in-process settings singleton is what takes effect;
    os.environ is only touched while building the candidate Settings.
    """
    if dotenv_path is None:
        yield
        return

    values = dotenv_values(dotenv_path)
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(
        {key: value for key, value in values.items() if value is not None}
    )

    try:
        yield
    finally:
        for key in values:
            previous = saved.get(key)
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def _redact(message) -> str:
    """
    Strip the offending value out of a validation error, keeping only the
    env var name so no secret or payload-shaped value leaks out.
    """
    match = re.match(r"Invalid value for ([A-Z0-9_]+):", str(message))
    if match:
        return f"Invalid value for {match.group(1)}"
    return str(message)[:200]


def _snapshot(relay) -> list:
    """
    Capture the mutable reload surface for rollback: the settings fields
    plus the enabled/key/models of every registered provider.
    """
    snap = [
        (settings, field, getattr(settings, field))
        for field in _RELOADABLE_FIELDS
        if hasattr(settings, field)
    ]

    for spec in _PROVIDER_SPECS:
        provider = relay.provider_manager.get(spec["id"])

        if provider is None:
            continue

        snap.append((provider, "enabled", provider.enabled))
        snap.append((provider, "api_key", provider.api_key))
        snap.append((provider, "models", list(provider.models)))
        snap.append((provider, "priority_models", list(provider.priority_models)))

    return snap


def _restore(snapshot: list) -> None:
    for obj, attr, value in snapshot:
        try:
            setattr(obj, attr, value)
        except Exception:
            pass


def _apply_provider_side_effects(relay, env, applied_set: set, failures: list) -> list:
    """
    Enable/disable providers, refresh API keys (with model discovery), and
    reorder models by priority. Mutates Provider objects in place and
    never swaps the provider list wholesale. Side-effect failures are
    non-fatal: the old models are kept.

    Returns the field names applied outside the env diff (keyring-driven
    key changes), so the caller can keep the reload report truthful. With
    keyring disabled this is always an empty list.
    """
    additionally_applied: list = []
    keyring_enabled = bool(getattr(env, "relay_keyring_enabled", False))

    for spec in _PROVIDER_SPECS:
        prefix = spec["prefix"]
        provider = relay.provider_manager.get(spec["id"])

        enabled_changed = f"{prefix}_enabled" in applied_set
        env_key_changed = f"{prefix}_api_key" in applied_set
        priority_changed = f"{prefix}_model_priority" in applied_set

        if not (enabled_changed or env_key_changed or priority_changed):
            if not keyring_enabled or provider is None:
                continue

        # Effective key: keyring-first with the validated env as fallback.
        # A keyring entry change is only observable when the running
        # provider's effective key differs from the resolved one.
        new_key = resolve_provider_key(spec["defn"], env)
        key_changed = env_key_changed or (
            keyring_enabled and new_key != provider.api_key
        )

        if not (enabled_changed or key_changed or priority_changed):
            continue

        if key_changed and not env_key_changed and keyring_enabled:
            additionally_applied.append(f"{prefix}_api_key")

        new_enabled = bool(getattr(env, f"{prefix}_enabled"))

        if provider is None:
            if new_enabled:
                try:
                    relay.provider_manager.register(
                        spec["factory"](spec["defn"])
                    )
                except Exception as exc:
                    failures.append(
                        {
                            "field": f"{prefix}_enabled",
                            "error": safe_provider_error(exc),
                        }
                    )
            continue

        provider.enabled = new_enabled

        if key_changed:
            provider.api_key = new_key

            if new_enabled and (
                provider.has_api_key() or not provider.requires_api_key
            ):
                try:
                    models = spec["client"]().list_models(provider)
                    priority = list(getattr(env, f"{prefix}_model_priority"))
                    provider.models = apply_model_priority(models, priority)
                    provider.priority_models = [
                        model for model in priority if model in provider.models
                    ]
                except Exception as exc:
                    failures.append(
                        {
                            "field": f"{prefix}_api_key",
                            "error": safe_provider_error(exc),
                        }
                    )
        elif priority_changed:
            priority = list(getattr(env, f"{prefix}_model_priority"))
            provider.models = apply_model_priority(provider.models, priority)
            provider.priority_models = [
                model for model in priority if model in provider.models
            ]

    return additionally_applied


def reload_config(
    relay,
    *,
    dry_run: bool = False,
    env=None,
    dotenv_path=None,
) -> dict:
    """
    Reload configuration for the running Relay.

    When ``env`` is omitted, the project .env is re-read (with override)
    and a fresh validated Settings() is built; invalid values abort with
    a redacted error and no mutation. When ``dotenv_path`` is provided,
    that file is temporarily overlaid onto the environment for
    validation (used by the HTTP endpoint; tests inject ``env`` directly
    to stay hermetic).

    Returns a report dict: reloaded, dry_run, applied (field names,
    secrets by name only), unchanged, and an optional failures list.
    Validation failures are marked with error_kind="validation";
    mid-apply failures roll back and are marked error_kind="apply".
    """
    with _lock:
        with _env_overlay(dotenv_path):
            if env is None:
                try:
                    env = Settings()
                except ValueError as exc:
                    return {
                        "reloaded": False,
                        "dry_run": dry_run,
                        "applied": [],
                        "unchanged": [],
                        "failures": [],
                        "error_kind": "validation",
                        "error": _redact(exc),
                    }

        applied: list = []
        unchanged: list = []

        for field in _RELOADABLE_FIELDS:
            if not hasattr(env, field):
                continue

            if getattr(settings, field) != getattr(env, field):
                applied.append(field)
            else:
                unchanged.append(field)

        if dry_run:
            return {
                "reloaded": True,
                "dry_run": True,
                "applied": applied,
                "unchanged": unchanged,
                "failures": [],
            }

        snapshot = _snapshot(relay)
        failures: list = []

        try:
            for field in applied:
                setattr(settings, field, getattr(env, field))

            keyring_applied = _apply_provider_side_effects(
                relay, env, set(applied), failures
            )

            # Keep the report truthful when a key was applied from the
            # keyring even though its env field did not change.
            for field in keyring_applied:
                if field not in applied:
                    applied.append(field)

                while field in unchanged:
                    unchanged.remove(field)

            relay.routing.refresh()
            relay.health_store.refresh_thresholds()
            relay.candidate_builder.refresh_scorer()
            relay.decision_engine.refresh()
            relay.telemetry.set_ewma_alpha(settings.adaptive_learning_rate)
            relay.quality_store.set_alpha(
                settings.quality_feedback_learning_rate
            )
            relay.quality_store.set_min_samples(
                settings.quality_feedback_min_samples
            )
            relay.quality_store.set_retention_limit(
                settings.quality_feedback_retention_limit
            )
            flusher = getattr(relay, "state_flusher", None)
            if flusher is not None:
                flusher.set_retention_days(
                    settings.persistence_retention_days
                )
        except Exception as exc:
            _restore(snapshot)

            # Push the restored settings into every live component so
            # nothing is left half-applied. Each refresh is guarded
            # independently: a failure restoring one component must not
            # prevent the others from being restored.
            rollback_refreshers = (
                relay.routing.refresh,
                relay.health_store.refresh_thresholds,
                relay.candidate_builder.refresh_scorer,
                relay.decision_engine.refresh,
                lambda: relay.telemetry.set_ewma_alpha(
                    settings.adaptive_learning_rate
                ),
                lambda: relay.quality_store.set_alpha(
                    settings.quality_feedback_learning_rate
                ),
                lambda: relay.quality_store.set_min_samples(
                    settings.quality_feedback_min_samples
                ),
                lambda: relay.quality_store.set_retention_limit(
                    settings.quality_feedback_retention_limit
                ),
                lambda: (
                    relay.state_flusher.set_retention_days(
                        settings.persistence_retention_days
                    )
                    if getattr(relay, "state_flusher", None) is not None
                    else None
                ),
            )

            for refresher in rollback_refreshers:
                try:
                    refresher()
                except Exception:
                    pass

            return {
                "reloaded": False,
                "dry_run": False,
                "applied": [],
                "unchanged": [],
                "failures": failures,
                "error_kind": "apply",
                "error": _redact(exc),
            }

        return {
            "reloaded": True,
            "dry_run": False,
            "applied": applied,
            "unchanged": unchanged,
            "failures": failures,
        }
