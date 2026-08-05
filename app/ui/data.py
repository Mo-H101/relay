"""
View-model layer for the Relay TUI.

This module is deliberately **Textual-free**: it only imports core and
service modules, so it can be unit-tested headlessly and so the import
boundary between the TUI and the rest of Relay stays verifiable. Screens
call ``ServiceFacade`` and never touch ``app.core.relay`` directly.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any

from app.core.config import settings
from app.core.relay import relay
from app.providers.availability import GLYPH
from app.providers.registry import PROVIDER_MENU, PROVIDER_REGISTRY
from app.security.auth import auth_configured
from app.services import config_store as config_store_module
from app.services import setup_state
from app.services.capabilities import is_chat_testable
from app.services.client_tracking import ClientActivityEntry, client_tracking
from app.services.diagnostics import DiagnosticsService
from app.services.metrics import relay_metrics
from app.services.ops_store import ops_store
from app.services.redaction import redact_dict, redact_text
from app.services.reload import reload_config
from app.setup import persistence
from app.setup.scan import ScanEngine


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    base_url: str
    enabled: bool
    priority: int
    requires_api_key: bool
    has_api_key: bool
    models: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    name: str
    status: str  # "healthy" | "degraded" | "unavailable" | "unsupported" | "unknown"
    latency_ms: int | None = None
    error: str = ""


@dataclass(frozen=True)
class ServerStatus:
    running: bool
    host: str
    port: int
    url: str


@dataclass(frozen=True)
class ChatCandidate:
    """
    A chat-testable (provider, model) pair for the specific-model picker,
    tagged with its last known availability status.
    """

    provider: str
    model: str
    status: str  # healthy | degraded | unavailable | unsupported | unknown


@dataclass(frozen=True)
class ProviderCatalogEntry:
    """
    One registry provider merged with its live runtime state for the
    Providers screen. Key material is reduced to a boolean (``has_api_key``)
    so no secret ever reaches the UI.
    """

    id: str
    display_name: str
    kind: str  # "cloud" | "local"
    requires_api_key: bool
    has_api_key: bool
    enabled: bool
    configured: bool  # a runtime provider object is loaded
    base_url: str
    model_count: int


# Health-status -> availability glyph mapping shared by the picker and the
# inline probe. Probe statuses (available/overloaded/unavailable) come from
# app.providers.availability.GLYPH directly.
_HEALTH_GLYPH = {
    "healthy": GLYPH["available"],
    "degraded": GLYPH["overloaded"],
    "unavailable": GLYPH["unavailable"],
    "unsupported": "\u003f",
    "unknown": "-",
}


def candidate_glyph(status: str) -> str:
    return _HEALTH_GLYPH.get(status, "-")


def probe_glyph(status: str) -> str:
    """
    Availability glyph for a ScanEngine probe status
    (available/overloaded/unavailable). Screens call this instead of
    reading ``app.providers.availability.GLYPH`` directly.
    """
    return GLYPH.get(status, "?")


# Availability-snapshot statuses (probe terms) -> health terms used by
# ModelInfo. Unrecognized snapshot values degrade to "unknown".
_AVAILABILITY_STATUS = {
    "available": "healthy",
    "overloaded": "degraded",
    "unavailable": "unavailable",
}

# Runtime provider.name -> registry provider id, so availability snapshots
# (keyed by defn.id) can be joined onto runtime provider models.
_DEFN_BY_NAME = {
    defn.provider_name: defn.id for defn in PROVIDER_REGISTRY.values()
}


@dataclass(frozen=True)
class DashboardSummary:
    relay_name: str
    server: ServerStatus
    setup_state: str
    provider_count: int
    enabled_providers: int
    default_provider: str
    model_count: int
    healthy_models: int
    requests: int
    successes: int
    failures: int
    success_rate: float | None
    average_latency_ms: float | None
    chats: int
    chat_attempts: int
    persistence_enabled: bool
    persistence_error: str
    env_file: str
    state_dir: str


@dataclass(frozen=True)
class ConfigField:
    """
    One row of the Configuration form.

    ``kind`` is ``"bool"``, ``"text"``, or ``"csv"``. Exactly one of
    ``reloadable`` / ``restart_required`` / ``informational`` describes how
    the field takes effect. No secret or API-key field is ever exposed here.
    """

    env: str
    label: str
    value: str
    kind: str
    group: str
    editable: bool
    reloadable: bool
    restart_required: bool
    informational: bool
    hint: str = ""


@dataclass(frozen=True)
class OpsEventView:
    """Display-ready view of one metadata-only ops event."""

    age_seconds: int
    kind: str
    method: str
    route: str
    status: int | None
    latency_ms: float
    endpoint: str
    provider: str
    model: str
    stream: str
    success: bool
    fallback: bool


@dataclass(frozen=True)
class LogEntryView:
    """Display-ready, redacted view of one JSON log line."""

    ts: str
    level: str
    logger: str
    event: str
    data: str


@dataclass(frozen=True)
class AuthStatus:
    """
    Authentication posture for the Applications surface.

    ``authenticated``/``failures`` come from the cumulative auth metrics;
    ``presented`` is the per-scheme distribution of credential methods
    seen by the request middleware (metadata only).
    """

    enabled: bool
    authenticated: float
    failures: float
    by_method: dict[str, float]
    by_reason: dict[str, float]
    presented: dict[str, int]


# ------------------------------------------------------------------ config

# (env, settings attr, kind, group, editable, reloadable, restart_required,
#  informational, label, hint). No secret/API-key fields appear here; keys
# stay managed by the Providers flow and never reach the Configuration UI.
_CONFIG_ROWS = (
    ("TASK_ROUTING_ENABLED", "task_routing_enabled", "bool", "routing", True, True, False, False, "Enable task routing", "Route requests by task category using the TASK_* model preferences."),
    ("CROSS_PROVIDER_MODEL_SELECTION", "cross_provider_model_selection", "bool", "routing", True, True, False, False, "Cross-provider model selection", "Allow a bare model reference to match every provider that has it."),
    ("TASK_CODING", "task_coding", "csv", "routing", True, True, False, False, "Coding models", "Comma-separated model refs (model or Provider:model)."),
    ("TASK_VISION", "task_vision", "csv", "routing", True, True, False, False, "Vision models", "Comma-separated model refs (model or Provider:model)."),
    ("TASK_REASONING", "task_reasoning", "csv", "routing", True, True, False, False, "Reasoning models", "Comma-separated model refs (model or Provider:model)."),
    ("TASK_GENERAL", "task_general", "csv", "routing", True, True, False, False, "General models", "Comma-separated model refs (model or Provider:model)."),
    ("TASK_CREATIVE", "task_creative", "csv", "routing", True, True, False, False, "Creative models", "Comma-separated model refs (model or Provider:model)."),
    ("TASK_TRANSLATION", "task_translation", "csv", "routing", True, True, False, False, "Translation models", "Comma-separated model refs (model or Provider:model)."),
    ("REQUEST_TIMEOUT", "request_timeout", "text", "failover", True, True, False, False, "Request timeout (s)", "Seconds before a provider request times out."),
    ("MAX_RETRIES", "max_retries", "text", "failover", True, True, False, False, "Max retries", "Retries across fallback candidates."),
    ("RETRY_HONOR_RETRY_AFTER", "retry_honor_retry_after", "bool", "failover", True, True, False, False, "Honor Retry-After", "Wait for a provider's Retry-After on rate-limit responses."),
    ("RETRY_AFTER_MAX_SECONDS", "retry_after_max_seconds", "text", "failover", True, True, False, False, "Retry-After cap (s)", "Upper bound for a Retry-After wait."),
    ("RETRY_BACKOFF_BASE_SECONDS", "retry_backoff_base_seconds", "text", "failover", True, True, False, False, "Backoff base (s)", "Exponential backoff base between retries (0 = immediate)."),
    ("RETRY_BACKOFF_MAX_SECONDS", "retry_backoff_max_seconds", "text", "failover", True, True, False, False, "Backoff max (s)", "Cap for exponential backoff."),
    ("REQUEST_TIMEOUT_BUDGET_SECONDS", "request_timeout_budget_seconds", "text", "failover", True, True, False, False, "Request budget (s)", "Overall wall-clock budget for a chat request (0 = off)."),
    ("RELAY_HOST", "relay_host", "text", "restart", False, False, True, False, "Host", "Server bind host. Restart required."),
    ("RELAY_PORT", "relay_port", "text", "restart", False, False, True, False, "Port", "Server bind port. Restart required."),
    ("PERSISTENCE_ENABLED", "persistence_enabled", "bool", "restart", False, False, True, False, "Persistence", "Learned-state persistence. Restart required."),
    ("PERSISTENCE_PATH", "persistence_path", "text", "restart", False, False, True, False, "Persistence path", "State store path. Restart required."),
    ("PERSISTENCE_FLUSH_INTERVAL_SECONDS", "persistence_flush_interval_seconds", "text", "restart", False, False, True, False, "Flush interval (s)", "State flush cadence. Restart required."),
    ("LOG_LEVEL", "log_level", "text", "restart", False, False, True, False, "Log level", "Logging verbosity. Restart required."),
    ("LOG_FILE", "log_file", "text", "restart", False, False, True, False, "Log file", "JSON log file path. Restart required."),
    ("LMSTUDIO_BASE_URL", "lmstudio_base_url", "text", "restart", False, False, True, False, "LM Studio URL", "LM Studio server base URL. Restart required."),
)

_EDITABLE_FIELDS = frozenset(row[0] for row in _CONFIG_ROWS if row[4])
_RESTART_FIELDS = frozenset(row[0] for row in _CONFIG_ROWS if row[6])
_GROUP_TITLES = {
    "routing": "Routing (TASK_*) — applied live",
    "failover": "Failover & retry — applied live",
    "restart": "Restart required (read-only)",
    "info": "Informational (read-only)",
}


def _tail_lines(path: Path, *, max_bytes: int = 65536, limit: int = 50) -> list[str]:
    """
    Read the last ``limit`` lines of ``path`` by scanning its final
    ``max_bytes``, so a huge log never loads into memory.
    """
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - max_bytes)
        handle.seek(start)
        text = handle.read().decode("utf-8", "replace")
    return text.splitlines()[-limit:]


class ServiceFacade:
    """
    View-model projection over the shared ``relay``/``settings`` singletons
    plus the provider registry and setup persistence. Screens read through
    this and never touch ``app.core.relay`` directly.

    Reads stay side-effect free. The P2c mutation points
    (``set_provider_enabled``/``set_model_priority``) persist through
    ``app.services.config_store`` (the single writer) and apply in-process
    through ``reload_config(relay)``; both are injectable so tests stay
    hermetic and never write the real ``.env``.
    """

    def __init__(
        self,
        relay_instance: Any = relay,
        *,
        store=None,
        reloader=None,
    ) -> None:
        self._relay = relay_instance
        self._store = store if store is not None else config_store_module
        self._reload = reloader if reloader is not None else reload_config

    # ------------------------------------------------------------- server

    def server_running(self) -> bool:
        return getattr(self._relay, "_embedded_server_running", False)

    def server_url(self) -> str:
        host = settings.relay_host
        port = settings.relay_port
        display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        return f"http://{display_host}:{port}"

    # ------------------------------------------------------------ settings

    def relay_name(self) -> str:
        return settings.relay_name

    def default_provider(self) -> str:
        """
        Runtime top-priority enabled provider (the one chat selects first).

        ``DEFAULT_PROVIDER`` was retired in P6.3 (informational-only, no
        behavior); this tile now reports runtime truth instead of a dead
        setting.
        """
        ranked = self._relay.provider_manager.ranked()
        return ranked[0].name if ranked else "-"

    def persistence_enabled(self) -> bool:
        return settings.persistence_enabled

    def env_file_path(self) -> str:
        from app.core.config import env_file
        return str(env_file)

    def state_dir_path(self) -> str:
        from app.core.config import state_dir
        return str(state_dir)

    def setup_state(self) -> str:
        return setup_state.read_setup_state()

    # ------------------------------------------------------------- health

    def health(self, deep: bool = False) -> dict:
        return self._relay.health(deep=deep)

    # ------------------------------------------------------------ providers

    def providers(self) -> list[ProviderInfo]:
        return [
            ProviderInfo(
                name=provider.name,
                base_url=provider.base_url,
                enabled=provider.enabled,
                priority=provider.priority,
                requires_api_key=provider.requires_api_key,
                has_api_key=provider.has_api_key(),
                models=list(provider.models),
            )
            for provider in self._relay.provider_manager.all()
        ]

    # --------------------------------------------------------------- models

    def _snapshot_statuses(self) -> dict[str, dict[str, str]]:
        """
        Availability-snapshot statuses joined by runtime provider name.
        Only snapshot entries for known registry providers are returned.
        """
        data = persistence.read_all()
        result: dict[str, dict[str, str]] = {}

        for defn in PROVIDER_REGISTRY.values():
            snapshot = data["providers"].get(defn.id)

            if not snapshot:
                continue

            result[defn.provider_name] = {
                entry["model"]: _AVAILABILITY_STATUS.get(entry["status"], "unknown")
                for entry in snapshot.get("models", [])
            }

        return result

    def models(self) -> list[ModelInfo]:
        """
        Union of models across providers, tagged with availability.

        Status resolution order: the health store report (most recent
        runtime probe) first, then the availability snapshot written by a
        setup/rescan scan, then "unknown" for catalog models never probed.
        """
        snapshot_statuses = self._snapshot_statuses()
        results: list[ModelInfo] = []
        seen: set[tuple[str, str]] = set()

        for provider in self._relay.provider_manager.all():
            report = self._relay.health_store.get(provider.name)
            reported: set[str] = set()
            snap = snapshot_statuses.get(provider.name, {})

            if report is not None:
                for model in report.models:
                    key = (provider.name, model.name)
                    if key in seen:
                        continue
                    seen.add(key)
                    reported.add(model.name)
                    results.append(
                        ModelInfo(
                            provider=provider.name,
                            name=model.name,
                            status=model.status,
                            latency_ms=model.latency_ms,
                            error=model.error,
                        )
                    )

            for model_name in provider.models:
                key = (provider.name, model_name)
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    ModelInfo(
                        provider=provider.name,
                        name=model_name,
                        status=snap.get(model_name, "unknown"),
                    )
                )

        return results

    # ------------------------------------------------------ provider catalog

    def provider_catalog(self) -> list[ProviderCatalogEntry]:
        """
        Every registry provider merged with its live runtime state. API-key
        presence is exposed as a boolean only; no key material is rendered.
        """
        entries: list[ProviderCatalogEntry] = []

        for defn in PROVIDER_REGISTRY.values():
            provider = self._relay.provider_manager.get(defn.provider_name)

            if provider is not None:
                enabled = provider.enabled
                has_key = provider.has_api_key()
                base_url = provider.base_url
                count = len(provider.models)
                configured = True
            else:
                enabled = bool(getattr(settings, defn.enabled_attr, False))
                has_key = (
                    bool(getattr(settings, defn.key_attr, ""))
                    if defn.key_attr is not None
                    else False
                )
                base_url = defn.base_url_default
                count = 0
                configured = False

            entries.append(
                ProviderCatalogEntry(
                    id=defn.id,
                    display_name=defn.display_name,
                    kind=defn.kind,
                    requires_api_key=defn.requires_api_key,
                    has_api_key=has_key,
                    enabled=enabled,
                    configured=configured,
                    base_url=base_url,
                    model_count=count,
                )
            )

        return entries

    def provider_defn_id(self, provider_name: str) -> str | None:
        """
        Runtime provider name -> registry definition id, or None for
        providers not backed by a registry entry.
        """
        return _DEFN_BY_NAME.get(provider_name)

    def provider_menu(self) -> list:
        """
        The full provider setup menu in registry order, for the
        "Re-run setup" wizard entry.
        """
        return PROVIDER_MENU

    def unconfigured_provider_defs(self) -> list:
        """
        Registry definitions for providers not yet configured, for the
        "Add provider" wizard entry. Derived from the catalog projection
        so the screen never reads the registry directly.
        """
        return [
            PROVIDER_REGISTRY[entry.id]
            for entry in self.provider_catalog()
            if not entry.configured
        ]

    def model_priority(self, provider_name: str) -> list[str]:
        """
        The current priority order for a provider's models: the runtime
        ``priority_models`` when configured, else the provider's available
        models in catalog order. Availability follows ``models()``.
        """
        provider = self._relay.provider_manager.get(provider_name)

        if provider is None:
            return []

        priority = getattr(provider, "priority_models", None)
        if priority:
            return list(priority)

        return [
            model.name
            for model in self.models()
            if model.provider == provider_name and model.status != "unavailable"
        ]

    # ------------------------------------------------------- config writes

    def set_provider_enabled(self, defn_id: str, enabled: bool) -> dict:
        """
        Persist a provider's enabled flag via ``config_store`` and apply it
        in-process with ``reload_config(relay)``. Returns the reload report.
        """
        defn = PROVIDER_REGISTRY.get(defn_id)

        if defn is None:
            return {"ok": False, "error": f"Unknown provider id '{defn_id}'."}

        self._store.set_provider_config(defn, enabled=enabled)
        return self._reload(self._relay)

    def set_model_priority(self, defn_id: str, priority: list[str]) -> dict:
        """
        Persist a provider's model priority via ``config_store`` and apply it
        in-process with ``reload_config(relay)``. Returns the reload report.
        """
        defn = PROVIDER_REGISTRY.get(defn_id)

        if defn is None:
            return {"ok": False, "error": f"Unknown provider id '{defn_id}'."}

        self._store.set_provider_config(defn, priority_models=priority)
        return self._reload(self._relay)

    def rescan_models(self, defn_id: str) -> dict:
        """
        Re-run the availability scan for one provider and write a fresh
        snapshot. The Models screen picks the new statuses up from the
        snapshot merge on its next refresh.
        """
        defn = PROVIDER_REGISTRY.get(defn_id)

        if defn is None:
            return {"ok": False, "error": f"Unknown provider id '{defn_id}'."}

        provider = self._relay.provider_manager.get(defn.provider_name)

        if provider is None:
            return {
                "ok": False,
                "error": f"{defn.display_name} is not configured yet.",
            }

        client = self._relay.chat_service.registry.get(defn.provider_name)

        if client is None:
            return {
                "ok": False,
                "error": f"No client registered for {defn.display_name}.",
            }

        if not provider.models:
            return {
                "ok": False,
                "error": f"{defn.display_name} has no models to scan.",
            }

        engine = ScanEngine()
        results = engine.scan(client, provider, list(provider.models))
        persistence.write_snapshot(defn.id, results)

        available = sum(1 for result in results if result.status != "unavailable")

        return {
            "ok": True,
            "provider": defn.display_name,
            "models": len(results),
            "available": available,
            "unavailable": len(results) - available,
        }

    # ------------------------------------------------------------ setup flow

    def run_setup(self, ui, *, menu=None, store=None):
        """
        Run the interactive setup wizard behind a UI object (the TUI's
        ``SetupAdapter`` in production). ``menu``/``store`` are injectable;
        defaults come from the provider registry and the single writer.
        """
        from app.setup.wizard import run_setup

        return run_setup(ui, menu=menu, store=store or self._store)

    def configure_provider(self, ui, defn_id: str, *, store=None) -> bool:
        """
        Run the single-provider wizard flow (key entry/validation, catalog
        scan, priority, save) for one registry definition.
        """
        from app.setup.wizard import _configure_provider

        defn = PROVIDER_REGISTRY.get(defn_id)

        if defn is None:
            return False

        return _configure_provider(ui, defn, store or self._store)

    # ------------------------------------------------------------ activity

    def ops_stats(self) -> dict:
        return ops_store.stats()

    def ops_events(self) -> list:
        return ops_store.events()

    # ------------------------------------------------------ configuration

    def config_form(self) -> list[ConfigField]:
        """
        Current Configuration form values with their live/restart/
        informational classification. No secret fields are included.
        """
        return [
            ConfigField(
                env=env,
                label=label,
                value=self._field_value(attr, kind),
                kind=kind,
                group=group,
                editable=editable,
                reloadable=reloadable,
                restart_required=restart_required,
                informational=informational,
                hint=hint,
            )
            for env, attr, kind, group, editable, reloadable, restart_required, informational, label, hint in _CONFIG_ROWS
        ]

    def _field_value(self, attr: str, kind: str) -> str:
        value = getattr(settings, attr, "")

        if kind == "bool":
            return "true" if value else "false"

        if kind == "csv":
            return ",".join(value or [])

        return str(value)

    def config_restart_required_fields(self) -> list[str]:
        """
        Env names that never change without a restart (server bind,
        persistence, logging, LM Studio URL). Read-only in the form.
        """
        return sorted(_RESTART_FIELDS)

    def save_config(self, changes: dict[str, str]) -> dict:
        """
        Save a Configuration change through the single writer.

        Flow: write via ``config_store`` -> validate with a dry-run reload
        -> apply with a real reload -> report. On any validation/apply
        failure the previous ``.env`` values are restored so the file and
        the in-process state stay consistent. Secret fields are not
        accepted here (they are managed by the provider flows).
        """
        if not changes:
            return {"saved": False, "error": "No changes to save."}

        unknown = sorted(set(changes) - _EDITABLE_FIELDS)

        if unknown:
            return {
                "saved": False,
                "error": (
                    "Read-only or unknown field(s): "
                    + ", ".join(unknown)
                    + ". Restart-required and informational settings are "
                    "read-only; secrets are managed on the Providers screen."
                ),
            }

        originals = {key: self._store.get_env(key, None) for key in changes}

        try:
            for key, value in changes.items():
                self._store.set_env(key, value)

            dry = self._reload_call(dry_run=True)

            if not dry.get("reloaded"):
                self._restore_env(originals)
                return {"saved": False, **dry}

            report = self._reload_call()

            if not report.get("reloaded"):
                self._restore_env(originals)

            return {"saved": bool(report.get("reloaded")), **report}
        except Exception as exc:  # noqa: BLE001 - surface in the status line
            self._restore_env(originals)
            return {"saved": False, "error": f"Reload failed: {exc}"}

    def _reload_call(self, **kwargs) -> dict:
        env_file = getattr(self._store, "env_file", None)

        if env_file is not None:
            kwargs["dotenv_path"] = str(env_file)

        return self._reload(self._relay, **kwargs)

    def _restore_env(self, originals: dict[str, str | None]) -> None:
        for key, value in originals.items():
            if value is None:
                self._store.unset_env(key)
            else:
                self._store.set_env(key, value)

    # ---------------------------------------------------------- applications

    def client_activity(self) -> list[ClientActivityEntry]:
        """
        Client activity rows (bucket, trimmed UA, route, counters, auth
        schemes). Metadata only; no credentials, bodies, or messages.
        """
        return client_tracking.activity()

    def auth_status(self) -> AuthStatus:
        enabled = auth_configured()

        return AuthStatus(
            enabled=enabled,
            authenticated=relay_metrics.auth_success.total(),
            failures=relay_metrics.auth_failures.total(),
            by_method={
                "bearer": relay_metrics.auth_success.value(method="bearer"),
                "header": relay_metrics.auth_success.value(method="header"),
            },
            by_reason={
                "missing": relay_metrics.auth_failures.value(reason="missing"),
                "invalid": relay_metrics.auth_failures.value(reason="invalid"),
            },
            presented=client_tracking.auth_totals(),
        )

    def endpoint_status(self) -> dict:
        """
        Rolling endpoint summary from the ops window plus totals.
        """
        stats = ops_store.stats()
        return {
            "requests": stats["requests"],
            "successes": stats["successes"],
            "failures": stats["failures"],
            "endpoints": stats["endpoints"],
        }

    # ---------------------------------------------------------- diagnostics

    def ops_tail(self, limit: int = 100) -> list[OpsEventView]:
        """
        Newest-first metadata-only ops events (bounded window).
        """
        now = time.monotonic()

        return [
            OpsEventView(
                age_seconds=max(0, int(now - event.ts)),
                kind=event.kind,
                method=event.method,
                route=event.route,
                status=event.status,
                latency_ms=round(event.latency_ms, 1),
                endpoint=event.endpoint,
                provider=event.provider,
                model=event.model,
                stream=event.stream,
                success=event.success,
                fallback=event.fallback,
            )
            for event in reversed(ops_store.events()[-limit:])
        ]

    def log_tail(self, limit: int = 50) -> dict:
        """
        Redacted tail of the JSON file log (only when LOG_FILE is set).
        Unparseable lines are skipped; secret-shaped ``data`` keys are
        masked before rendering.
        """
        path = getattr(settings, "log_file", "")

        if not path:
            return {
                "available": False,
                "entries": [],
                "error": "LOG_FILE is not configured.",
            }

        target = Path(path)

        if not target.is_file():
            return {
                "available": False,
                "entries": [],
                "error": f"Log file not found: {path}",
            }

        try:
            lines = _tail_lines(target, limit=limit)
        except OSError as exc:
            return {
                "available": False,
                "entries": [],
                "error": f"Cannot read log: {exc}",
            }

        entries: list[LogEntryView] = []
        skipped = 0

        for line in lines:
            try:
                payload = json.loads(line)
            except (ValueError, TypeError):
                skipped += 1
                continue

            if not isinstance(payload, dict):
                skipped += 1
                continue

            data = redact_dict(payload.get("data") or {})
            entries.append(
                LogEntryView(
                    ts=str(payload.get("ts", "")),
                    level=str(payload.get("level", "")),
                    logger=str(payload.get("logger", "")),
                    event=str(payload.get("event", "")),
                    data=json.dumps(data)[:200],
                )
            )

        return {"available": True, "entries": entries, "skipped": skipped}

    def provider_health_deep(self, provider_name: str) -> dict:
        """
        Per-model health deep view for one provider: runtime health report
        joined with the availability snapshot and learned marks. Read-only.
        """
        provider = self._relay.provider_manager.get(provider_name)

        if provider is None:
            return {"provider": provider_name, "found": False}

        report = self._relay.health_store.get(provider_name)
        snapshot_statuses = self._snapshot_statuses().get(provider_name, {})
        learn_export = getattr(
            self._relay.health_store,
            "export_learned_state",
            lambda: {},
        )()
        marks = ((learn_export or {}).get(provider_name) or {}).get(
            "model_marks"
        ) or {}

        by_name = (
            {model.name: model for model in report.models}
            if report is not None
            else {}
        )

        models = [
            {
                "name": name,
                "health": (
                    by_name[name].status if name in by_name else "not_checked"
                ),
                "latency_ms": (
                    by_name[name].latency_ms if name in by_name else None
                ),
                "error": (by_name[name].error or "") if name in by_name else "",
                "snapshot": snapshot_statuses.get(name, "unknown"),
                "learned": list(marks.get(name) or []),
            }
            for name in provider.models
        ]

        return {
            "provider": provider_name,
            "found": True,
            "enabled": provider.enabled,
            "requires_api_key": provider.requires_api_key,
            "has_api_key": provider.has_api_key(),
            "status": (
                getattr(report, "status", "not_checked")
                if report is not None
                else "not_checked"
            ),
            "connectivity": (
                getattr(report, "connectivity", None)
                if report is not None
                else None
            ),
            "last_checked": (
                getattr(report, "last_checked", None)
                if report is not None
                else None
            ),
            "rate_limit_status": (
                getattr(report, "rate_limit_status", None)
                if report is not None
                else None
            ),
            "models": models,
        }

    def test_connection(self, provider_name: str) -> dict:
        """
        Explicit per-provider test connection: probes one chat-testable
        model through the P1 availability path. Network I/O; run off the
        UI thread. Never mutates state.
        """
        provider = self._relay.provider_manager.get(provider_name)

        if provider is None:
            return {"ok": False, "error": f"Unknown provider '{provider_name}'."}

        model = next(
            (candidate for candidate in provider.models if is_chat_testable(candidate)),
            None,
        )

        if model is None:
            return {
                "ok": False,
                "error": f"No chat-testable model for {provider_name}.",
            }

        result = self.probe_model(provider_name, model)

        if result is None:
            return {"ok": False, "error": f"Probe failed for {provider_name}."}

        return {
            "ok": result.status != "unavailable",
            "provider": provider_name,
            "model": model,
            "status": result.status,
            "latency_ms": result.latency_ms,
            "error": result.error,
        }

    def export_diagnostics(self, path: str) -> dict:
        """
        Export the redacted diagnostics snapshot to ``path``.

        Every export passes through the redaction layer before the atomic
        file write, so even an unexpected secret shape in the snapshot can
        never survive into the file.
        """
        try:
            snapshot = DiagnosticsService().build_snapshot(self._relay)
            text = json.dumps(snapshot, indent=2, default=str)
            redacted = redact_text(text)

            target = Path(path)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(redacted + "\n", encoding="utf-8")
            os.replace(str(tmp), str(target))

            return {
                "ok": True,
                "path": str(target),
                "generated_at": snapshot.get("generated_at", ""),
                "bytes": len(redacted.encode("utf-8")),
            }
        except Exception as exc:  # noqa: BLE001 - surface in the status line
            return {"ok": False, "error": str(exc)}

    # ---------------------------------------------------------------- chat

    def specific_model_candidates(self) -> list[ChatCandidate]:
        """
        Chat-testable (provider, model) pairs across all providers, tagged
        with the health store's last known status.
        """
        results: list[ChatCandidate] = []

        for provider in self._relay.provider_manager.all():
            report = self._relay.health_store.get(provider.name)
            status_by_model: dict[str, str] = {}

            if report is not None:
                status_by_model = {
                    model.name: model.status for model in report.models
                }

            for model in provider.models:
                if not is_chat_testable(model):
                    continue
                results.append(
                    ChatCandidate(
                        provider=provider.name,
                        model=model,
                        status=status_by_model.get(model, "unknown"),
                    )
                )

        return results

    def _chat_candidates(self, provider, model: str | None):
        """
        Build the candidate list for a chat request: a specific model when
        given, otherwise every chat-testable model of the provider.
        """
        if model is not None:
            return [(provider, model)]

        return [
            (provider, candidate_model)
            for candidate_model in provider.models
            if is_chat_testable(candidate_model)
        ]

    def random_chat(self, message: str, **generation_kwargs: Any) -> dict:
        """
        Chat against the provider Relay would select first (same candidate
        path as /chat), failing over across its chat-testable models.
        Returns the chat_across result dict; never raises.
        """
        provider = self._relay.choose_provider()

        if provider is None:
            return {
                "success": False,
                "error": "No provider available. Configure a provider first.",
            }

        candidates = self._chat_candidates(provider, None)

        if not candidates:
            return {
                "success": False,
                "error": f"No chat-testable models for {provider.name}.",
            }

        return self._relay.chat_service.chat_across(
            candidates,
            message,
            max_retries=settings.max_retries,
            **generation_kwargs,
        )

    def specific_chat(
        self,
        provider_name: str,
        model: str,
        message: str,
        **generation_kwargs: Any,
    ) -> dict:
        """
        Non-streaming chat against a specific (provider, model). Returns
        the chat_across result dict; never raises.
        """
        provider = self._relay.provider_manager.get(provider_name)

        if provider is None:
            return {
                "success": False,
                "error": f"Unknown provider '{provider_name}'.",
            }

        return self._relay.chat_service.chat_across(
            [(provider, model)],
            message,
            max_retries=settings.max_retries,
            **generation_kwargs,
        )

    def start_stream(
        self,
        provider_name: str,
        model: str,
        message: str,
        **generation_kwargs: Any,
    ) -> dict:
        """
        Start a streaming chat against a specific (provider, model).

        Returns the chat_across_stream_messages result dict with a
        ``stream_gen`` (yielding parsed chunk dicts) on success. The
        caller consumes the generator off the UI thread.
        """
        provider = self._relay.provider_manager.get(provider_name)

        if provider is None:
            return {
                "success": False,
                "stream_gen": None,
                "error": f"Unknown provider '{provider_name}'.",
                "attempts": [],
            }

        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": message}],
            "model": model,
            "stream": True,
        }
        for key in ("temperature", "top_p", "max_tokens"):
            if key in generation_kwargs and generation_kwargs[key] is not None:
                payload[key] = generation_kwargs[key]

        return self._relay.chat_service.chat_across_stream_messages(
            [(provider, model)],
            payload,
            max_retries=settings.max_retries,
        )

    def probe_model(self, provider_name: str, model: str):
        """
        Single-model live availability probe (inline ✓/⚠/✗ test) using
        ScanEngine. Returns a ScanResult or None for unknown providers.
        """
        provider = self._relay.provider_manager.get(provider_name)

        if provider is None:
            return None

        client = self._relay.chat_service.registry.get(provider_name)
        engine = ScanEngine(concurrency=1)
        results = engine.scan(client, provider, [model])

        return results[0]

    # ----------------------------------------------------------- dashboard

    def dashboard_summary(self) -> DashboardSummary:
        stats = self.ops_stats()
        models = self.models()

        return DashboardSummary(
            relay_name=self.relay_name(),
            server=ServerStatus(
                running=self.server_running(),
                host=settings.relay_host,
                port=settings.relay_port,
                url=self.server_url(),
            ),
            setup_state=self.setup_state(),
            provider_count=len(self._relay.provider_manager.all()),
            enabled_providers=len(self._relay.provider_manager.enabled()),
            default_provider=self.default_provider(),
            model_count=len(models),
            healthy_models=sum(
                1 for model in models if model.status == "healthy"
            ),
            requests=stats.get("requests", 0),
            successes=stats.get("successes", 0),
            failures=stats.get("failures", 0),
            success_rate=stats.get("success_rate"),
            average_latency_ms=stats.get("average_latency_ms"),
            chats=stats.get("chats", 0),
            chat_attempts=stats.get("chat_attempts", 0),
            persistence_enabled=self.persistence_enabled(),
            persistence_error=self._relay.persistence_init_error or "",
            env_file=self.env_file_path(),
            state_dir=self.state_dir_path(),
        )
