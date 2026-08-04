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
from app.providers.base import apply_model_priority
from app.providers.factory import build_runtime_provider
from app.providers.registry import PROVIDER_REGISTRY, RUNTIME_READY

# Settings read dynamically at request time and safe to reload in place.
_SIMPLE_FIELDS = (
    "request_timeout",
    "max_retries",
    "retry_honor_retry_after",
    "retry_after_max_seconds",
    "retry_backoff_base_seconds",
    "retry_backoff_max_seconds",
    "request_timeout_budget_seconds",
    "task_routing_enabled",
    "cross_provider_model_selection",
    "task_coding",
    "task_vision",
    "task_reasoning",
    "task_general",
    "task_creative",
    "task_translation",
    "health_aware_routing",
    "health_ttl_seconds",
    "health_degraded_ttl_seconds",
    "health_unavailable_ttl_seconds",
    "health_feedback_enabled",
    "health_feedback_model_server_error_threshold",
    "health_feedback_provider_server_error_threshold",
    "health_feedback_model_timeout_degraded_threshold",
    "health_feedback_model_timeout_unavailable_threshold",
    "health_feedback_model_invalid_request_unavailable_threshold",
    "health_feedback_model_unknown_degraded_threshold",
    "health_freshness_exponent",
    "scoring_priority_weight",
    "scoring_success_weight",
    "scoring_latency_weight",
    "scoring_failure_weight",
    "scoring_preference_weight",
    "scoring_priority_denom",
    "scoring_latency_ref_ms",
    "scoring_failure_ref_count",
    "scoring_task_compatibility_weight",
    "adaptive_routing_enabled",
    "adaptive_min_samples",
    "adaptive_learning_rate",
    "adaptive_latency_weight",
    "adaptive_reliability_weight",
    "quality_feedback_enabled",
    "quality_feedback_min_samples",
    "quality_feedback_learning_rate",
    "quality_feedback_retention_limit",
    "quality_feedback_weight",
    "scoring_cost_weight",
    "decision_engine_enabled",
    "persistence_retention_days",
    "task_classification_enabled",
    "task_classification_threshold",
    "task_catalog_enabled",
    "telemetry_enabled",
    "decision_explanations_enabled",
    "ops_window_seconds",
    "ops_max_events",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "proxy_enabled",
)

# Secrets: reported by field name only.
_SECRET_FIELDS = ("relay_api_key",) + tuple(
    defn.key_attr
    for defn in PROVIDER_REGISTRY.values()
    if defn.id in RUNTIME_READY and defn.key_attr
)

# Runtime provider specs are derived from the provider registry (P4.1) so
# the registry is the single source of runtime truth. Only providers wired
# into routing (RUNTIME_READY) are reloadable in this phase.
_PROVIDER_SPECS = tuple(
    {
        "id": defn.id,
        "prefix": defn.id,
        "factory": build_runtime_provider,
        "client": defn.client,
    }
    for defn in PROVIDER_REGISTRY.values()
    if defn.id in RUNTIME_READY
)

_RELOADABLE_FIELDS = (
    tuple(_SIMPLE_FIELDS)
    + _SECRET_FIELDS
    + tuple(
        f"{spec['prefix']}_{suffix}"
        for spec in _PROVIDER_SPECS
        for suffix in ("enabled", "api_key", "model_priority")
    )
)

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


def _apply_provider_side_effects(relay, env, applied_set: set, failures: list) -> None:
    """
    Enable/disable providers, refresh API keys (with model discovery), and
    reorder models by priority. Mutates Provider objects in place and
    never swaps the provider list wholesale. Side-effect failures are
    non-fatal: the old models are kept.
    """
    for spec in _PROVIDER_SPECS:
        prefix = spec["prefix"]
        provider = relay.provider_manager.get(spec["id"])

        enabled_changed = f"{prefix}_enabled" in applied_set
        key_changed = f"{prefix}_api_key" in applied_set
        priority_changed = f"{prefix}_model_priority" in applied_set

        if not (enabled_changed or key_changed or priority_changed):
            continue

        new_enabled = bool(getattr(env, f"{prefix}_enabled"))

        if provider is None:
            if new_enabled:
                try:
                    relay.provider_manager.register(spec["factory"]())
                except Exception as exc:
                    failures.append(
                        {"field": f"{prefix}_enabled", "error": _redact(exc)}
                    )
            continue

        provider.enabled = new_enabled

        if key_changed:
            provider.api_key = getattr(env, f"{prefix}_api_key")

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
                        {"field": f"{prefix}_api_key", "error": _redact(exc)}
                    )
        elif priority_changed:
            priority = list(getattr(env, f"{prefix}_model_priority"))
            provider.models = apply_model_priority(provider.models, priority)
            provider.priority_models = [
                model for model in priority if model in provider.models
            ]


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

            _apply_provider_side_effects(relay, env, set(applied), failures)

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
