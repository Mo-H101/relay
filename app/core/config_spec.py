"""
Declarative settings registry (P7.1).

The single source of truth describing every Relay setting: env var, type,
default, description, category, secret flag, effect (live / restart /
info), validation constraints, CLI visibility, and the TUI Configuration
form metadata for the fields it exposes.

``Settings.__init__`` in ``app.core.config`` is intentionally **not**
rewritten: it stays the executable parser, and this module is the
declarative mirror. ``tests/test_config_spec.py`` asserts a 1:1 mapping
between ``vars(Settings())`` and ``SPECS`` plus exact reproduction of the
reload allowlist (``app.services.reload``), the reload secret set, and the
TUI config rows (``app.ui.data._CONFIG_ROWS``), so the registry cannot
drift from the code that actually runs.

The reloadable field tuples are reproduced **in the same order** the
runtime builds them, so ``app.services.reload`` can consume this module
verbatim without changing any report ordering or rollback semantics.

Provider fields follow the ``<prefix>_<suffix>`` convention (``nvidia_`` +
``enabled`` etc.), derived from ``PROVIDER_REGISTRY`` exactly like the
reload engine derives them, so adding a provider is still a registry
entry, not a new branch here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from app.core.config import (
    Settings,
    _csv,
    _valid_float,
    _valid_int,
    _valid_url,
)
from app.providers.registry import PROVIDER_REGISTRY, RUNTIME_READY

# Effect (how a change takes effect) values:
LIVE = "live"        # applied by hot reload without a restart
RESTART = "restart"  # only takes effect when the process restarts
INFO = "info"        # informational/introspection, never live-applied


@dataclass(frozen=True)
class SettingSpec:
    """One declarative setting entry."""

    env: str | None
    attr: str
    type: str                      # bool | int | float | str | csv | url
    default: object
    description: str
    category: str
    secret: bool = False
    effect: str = INFO             # LIVE | RESTART | INFO
    provider: bool = False         # provider-attributed (derived reload triplet)
    # Validation constraints (mirror Settings.__init__ call sites).
    minimum: float | None = None
    exclusive_minimum: bool = False
    maximum: float | None = None
    # CLI visibility.
    cli_visible: bool = True
    # TUI Configuration form metadata (only the fields _CONFIG_ROWS lists).
    tui: bool = False
    tui_kind: str | None = None    # bool | text | csv
    tui_group: str | None = None   # routing | failover | restart | info
    tui_editable: bool = False
    label: str | None = None
    hint: str | None = None

    @property
    def reloadable(self) -> bool:
        return self.effect == LIVE

    @property
    def restart_required(self) -> bool:
        return self.effect == RESTART

    @property
    def informational(self) -> bool:
        return self.effect == INFO


def _simple(
    env,
    attr,
    type_,
    default,
    description,
    category,
    minimum=None,
    exclusive_minimum=False,
    maximum=None,
):
    """Build a live-reloadable, non-secret, non-provider entry."""
    return SettingSpec(
        env=env,
        attr=attr,
        type=type_,
        default=default,
        description=description,
        category=category,
        effect=LIVE,
        minimum=minimum,
        exclusive_minimum=exclusive_minimum,
        maximum=maximum,
    )


def _restart(
    env,
    attr,
    type_,
    default,
    description,
    category,
    secret=False,
    provider=False,
    minimum=None,
    cli_visible=True,
):
    """Build a restart-required entry."""
    return SettingSpec(
        env=env,
        attr=attr,
        type=type_,
        default=default,
        description=description,
        category=category,
        secret=secret,
        effect=RESTART,
        provider=provider,
        minimum=minimum,
        cli_visible=cli_visible,
    )


def _provider(
    env,
    attr,
    type_,
    default,
    description,
    category,
    secret=False,
    minimum=None,
):
    """Build a provider-attributed live-reloadable entry."""
    return SettingSpec(
        env=env,
        attr=attr,
        type=type_,
        default=default,
        description=description,
        category=category,
        secret=secret,
        effect=LIVE,
        provider=True,
        minimum=minimum,
    )


# ---------------------------------------------------------------------------
# _RAW_SPECS is ordered so that filtering by ``reloadable and not secret and
# not provider`` yields the exact order of reload's _SIMPLE_FIELDS. The 61
# live-reloadable simple fields are listed first, in that order, then the
# provider/relay/restart/info fields (their order is not constrained). TUI
# metadata is attached afterwards to build the final SPECS tuple.
# ---------------------------------------------------------------------------
_RAW_SPECS = (
    # ---------------------------------------------------------------- request
    _simple("REQUEST_TIMEOUT", "request_timeout", "int", 120,
            "Seconds before a provider request times out.", "general",
            minimum=1),
    _simple("MAX_RETRIES", "max_retries", "int", 1,
            "Retries across fallback candidates.", "general", minimum=0),
    _simple("RETRY_HONOR_RETRY_AFTER", "retry_honor_retry_after", "bool", False,
            "Wait for a provider's Retry-After on rate-limit responses.",
            "general"),
    _simple("RETRY_AFTER_MAX_SECONDS", "retry_after_max_seconds", "int", 60,
            "Upper bound for a Retry-After wait.", "general", minimum=0),
    _simple("RETRY_BACKOFF_BASE_SECONDS", "retry_backoff_base_seconds", "int", 0,
            "Exponential backoff base between retries (0 = immediate).",
            "general", minimum=0),
    _simple("RETRY_BACKOFF_MAX_SECONDS", "retry_backoff_max_seconds", "int", 60,
            "Cap for exponential backoff.", "general", minimum=0),
    _simple("REQUEST_TIMEOUT_BUDGET_SECONDS", "request_timeout_budget_seconds",
            "int", 0,
            "Overall wall-clock budget for a chat request (0 = off).",
            "general", minimum=0),

    # ------------------------------------------------------------ task routing
    _simple("TASK_ROUTING_ENABLED", "task_routing_enabled", "bool", False,
            "Route requests by task category using the TASK_* preferences.",
            "task_routing"),
    _simple("CROSS_PROVIDER_MODEL_SELECTION", "cross_provider_model_selection",
            "bool", False,
            "Allow a bare model reference to match every provider that has it.",
            "task_routing"),
    _simple("TASK_CODING", "task_coding", "csv", [],
            "Coding models (model or Provider:model refs).", "task_routing"),
    _simple("TASK_VISION", "task_vision", "csv", [],
            "Vision models (model or Provider:model refs).", "task_routing"),
    _simple("TASK_REASONING", "task_reasoning", "csv", [],
            "Reasoning models (model or Provider:model refs).", "task_routing"),
    _simple("TASK_GENERAL", "task_general", "csv", [],
            "General models (model or Provider:model refs).", "task_routing"),
    _simple("TASK_CREATIVE", "task_creative", "csv", [],
            "Creative models (model or Provider:model refs).", "task_routing"),
    _simple("TASK_TRANSLATION", "task_translation", "csv", [],
            "Translation models (model or Provider:model refs).",
            "task_routing"),

    # ----------------------------------------------------------------- health
    _simple("HEALTH_AWARE_ROUTING", "health_aware_routing", "bool", False,
            "Enable health-aware routing.", "health"),
    _simple("HEALTH_TTL_SECONDS", "health_ttl_seconds", "int", 300,
            "Health probe result TTL, in seconds.", "health", minimum=0),
    _simple("HEALTH_DEGRADED_TTL_SECONDS", "health_degraded_ttl_seconds",
            "int", 60, "TTL for degraded health marks, in seconds.", "health",
            minimum=0),
    _simple("HEALTH_UNAVAILABLE_TTL_SECONDS", "health_unavailable_ttl_seconds",
            "int", 900, "TTL for unavailable health marks, in seconds.",
            "health", minimum=0),
    _simple("HEALTH_FEEDBACK_ENABLED", "health_feedback_enabled", "bool", False,
            "Enable model health feedback learning.", "health"),
    _simple("HEALTH_FEEDBACK_MODEL_SERVER_ERROR_THRESHOLD",
            "health_feedback_model_server_error_threshold", "int", 1,
            "Server-error marks before a model is degraded.", "health",
            minimum=1),
    _simple("HEALTH_FEEDBACK_PROVIDER_SERVER_ERROR_THRESHOLD",
            "health_feedback_provider_server_error_threshold", "int", 3,
            "Server-error marks before a provider is degraded.", "health",
            minimum=1),
    _simple("HEALTH_FEEDBACK_MODEL_TIMEOUT_DEGRADED_THRESHOLD",
            "health_feedback_model_timeout_degraded_threshold", "int", 2,
            "Timeout marks before a model is degraded.", "health", minimum=1),
    _simple("HEALTH_FEEDBACK_MODEL_TIMEOUT_UNAVAILABLE_THRESHOLD",
            "health_feedback_model_timeout_unavailable_threshold", "int", 5,
            "Timeout marks before a model is unavailable.", "health",
            minimum=1),
    _simple("HEALTH_FEEDBACK_MODEL_INVALID_REQUEST_UNAVAILABLE_THRESHOLD",
            "health_feedback_model_invalid_request_unavailable_threshold",
            "int", 3,
            "Invalid-request marks before a model is unavailable.", "health",
            minimum=1),
    _simple("HEALTH_FEEDBACK_MODEL_UNKNOWN_DEGRADED_THRESHOLD",
            "health_feedback_model_unknown_degraded_threshold", "int", 3,
            "Unknown-status marks before a model is degraded.", "health",
            minimum=1),
    _simple("HEALTH_FRESHNESS_EXPONENT", "health_freshness_exponent", "float",
            1.0, "Health freshness decay exponent.", "health", minimum=0.0),

    # ---------------------------------------------------------------- scoring
    _simple("SCORING_PRIORITY_WEIGHT", "scoring_priority_weight", "float", 1.0,
            "Weight of the priority signal in candidate scoring.", "scoring",
            minimum=0.0),
    _simple("SCORING_SUCCESS_WEIGHT", "scoring_success_weight", "float", 1.0,
            "Weight of the success signal in candidate scoring.", "scoring",
            minimum=0.0),
    _simple("SCORING_LATENCY_WEIGHT", "scoring_latency_weight", "float", 1.0,
            "Weight of the latency signal in candidate scoring.", "scoring",
            minimum=0.0),
    _simple("SCORING_FAILURE_WEIGHT", "scoring_failure_weight", "float", 1.0,
            "Weight of the failure signal in candidate scoring.", "scoring",
            minimum=0.0),
    _simple("SCORING_PREFERENCE_WEIGHT", "scoring_preference_weight", "float",
            1.0, "Weight of the preference signal in candidate scoring.",
            "scoring", minimum=0.0),
    _simple("SCORING_PRIORITY_DENOM", "scoring_priority_denom", "float", 10.0,
            "Denominator scaling the priority signal (must be > 0).",
            "scoring", minimum=0.0, exclusive_minimum=True),
    _simple("SCORING_LATENCY_REF_MS", "scoring_latency_ref_ms", "float", 250.0,
            "Reference latency, in ms, normalizing the latency signal "
            "(must be > 0).", "scoring", minimum=0.0,
            exclusive_minimum=True),
    _simple("SCORING_FAILURE_REF_COUNT", "scoring_failure_ref_count", "int", 5,
            "Reference failure count normalizing the failure signal.",
            "scoring", minimum=1),
    _simple("SCORING_TASK_COMPATIBILITY_WEIGHT",
            "scoring_task_compatibility_weight", "float", 1.0,
            "Weight of the task-capability catalog signal.", "task_catalog",
            minimum=0.0),

    # -------------------------------------------------------------- adaptive
    _simple("ADAPTIVE_ROUTING_ENABLED", "adaptive_routing_enabled", "bool",
            False, "Enable adaptive within-band reordering.", "adaptive"),
    _simple("ADAPTIVE_MIN_SAMPLES", "adaptive_min_samples", "int", 10,
            "Minimum observed samples before adaptive signals are trusted.",
            "adaptive", minimum=1),
    _simple("ADAPTIVE_LEARNING_RATE", "adaptive_learning_rate", "float", 0.1,
            "EWMA learning rate for adaptive signals.", "adaptive",
            minimum=0.0),
    _simple("ADAPTIVE_LATENCY_WEIGHT", "adaptive_latency_weight", "float", 1.0,
            "Weight of the adaptive latency signal.", "adaptive", minimum=0.0),
    _simple("ADAPTIVE_RELIABILITY_WEIGHT", "adaptive_reliability_weight",
            "float", 1.0, "Weight of the adaptive reliability signal.",
            "adaptive", minimum=0.0),

    # --------------------------------------------------------------- quality
    _simple("QUALITY_FEEDBACK_ENABLED", "quality_feedback_enabled", "bool",
            False, "Enable user quality-feedback routing.", "quality"),
    _simple("QUALITY_FEEDBACK_MIN_SAMPLES", "quality_feedback_min_samples",
            "int", 10,
            "Minimum ratings before a quality estimate is trusted.", "quality",
            minimum=1),
    _simple("QUALITY_FEEDBACK_LEARNING_RATE", "quality_feedback_learning_rate",
            "float", 0.1, "EWMA learning rate for quality feedback.",
            "quality", minimum=0.0, maximum=1.0),
    _simple("QUALITY_FEEDBACK_RETENTION_LIMIT",
            "quality_feedback_retention_limit", "int", 10000,
            "Cap on distinct quality aggregates kept in memory.", "quality",
            minimum=1),
    _simple("QUALITY_FEEDBACK_WEIGHT", "quality_feedback_weight", "float", 1.0,
            "Weight of the within-band quality contribution.", "quality",
            minimum=0.0),

    _simple("SCORING_COST_WEIGHT", "scoring_cost_weight", "float", 0.0,
            "Weight of the cost signal in candidate scoring (placeholder).",
            "scoring", minimum=0.0),

    # --------------------------------------------------------------- decision
    _simple("DECISION_ENGINE_ENABLED", "decision_engine_enabled", "bool", False,
            "Route selection through the explicit decision engine.",
            "decision"),

    # ------------------------------------------------------------ persistence
    _simple("PERSISTENCE_RETENTION_DAYS", "persistence_retention_days", "int",
            0, "Retention, in days, for persisted telemetry (0 disables "
            "pruning).", "persistence", minimum=0),

    # ---------------------------------------------------- task classification
    _simple("TASK_CLASSIFICATION_ENABLED", "task_classification_enabled",
            "bool", False,
            "Enable free-text task classification for /chat requests.",
            "task_classification"),
    _simple("TASK_CLASSIFICATION_THRESHOLD", "task_classification_threshold",
            "float", 0.6, "Confidence threshold for task classification.",
            "task_classification", minimum=0.0),

    # ---------------------------------------------------------- task catalog
    _simple("TASK_CATALOG_ENABLED", "task_catalog_enabled", "bool", False,
            "Enable the model capability catalog signal.", "task_catalog"),

    # -------------------------------------------------------------- telemetry
    _simple("TELEMETRY_ENABLED", "telemetry_enabled", "bool", False,
            "Enable runtime telemetry collection.", "telemetry"),

    # ----------------------------------------------------- decision explainers
    _simple("DECISION_EXPLANATIONS_ENABLED", "decision_explanations_enabled",
            "bool", False, "Emit explainable decision metadata.", "decision"),

    # ---------------------------------------------------------- observability
    _simple("OPS_WINDOW_SECONDS", "ops_window_seconds", "int", 300,
            "Rolling operations window, in seconds (0 disables pruning).",
            "observability", minimum=0),
    _simple("OPS_MAX_EVENTS", "ops_max_events", "int", 10000,
            "Cap on in-memory operations events.", "observability", minimum=1),

    # ---------------------------------------------------------------- proxy
    _simple("HTTP_PROXY", "http_proxy", "str", "",
            "HTTP proxy for outbound provider requests.", "proxy"),
    _simple("HTTPS_PROXY", "https_proxy", "str", "",
            "HTTPS proxy for outbound provider requests.", "proxy"),
    _simple("NO_PROXY", "no_proxy", "str", "",
            "Hosts that bypass the proxy.", "proxy"),
    _simple("PROXY_ENABLED", "proxy_enabled", "bool", True,
            "Honor HTTP(S)_PROXY / NO_PROXY for outbound requests.", "proxy"),

    # ------------------------------------------------------------------ auth
    _simple("RELAY_AUTH_STORE", "relay_auth_store", "bool", False,
            "Accept store-backed API keys (platform.db) in addition to the "
            "bootstrap key.", "auth"),

    # ==================================================================
    # Provider fields (reloadable triplets: enabled / api_key / priority,
    # plus non-reloadable base URLs). Order is unconstrained here; the
    # derived provider reloadable tuple follows PROVIDER_REGISTRY order.
    # ==================================================================

    # --------------------------------------------------------------- NVIDIA
    _provider("NVIDIA_ENABLED", "nvidia_enabled", "bool", True,
              "Load the NVIDIA provider at runtime.", "providers"),
    _provider("NVIDIA_API_KEY", "nvidia_api_key", "str", "",
              "NVIDIA provider API key.", "providers", secret=True),
    _provider("NVIDIA_MODEL_PRIORITY", "nvidia_model_priority", "csv", [],
              "NVIDIA model priority order.", "providers"),

    # --------------------------------------------------------------- OpenAI
    _provider("OPENAI_ENABLED", "openai_enabled", "bool", False,
              "Load the OpenAI provider at runtime.", "providers"),
    _provider("OPENAI_API_KEY", "openai_api_key", "str", "",
              "OpenAI provider API key.", "providers", secret=True),
    _provider("OPENAI_MODEL_PRIORITY", "openai_model_priority", "csv", [],
              "OpenAI model priority order.", "providers"),

    # ------------------------------------------------------------ Anthropic
    _provider("ANTHROPIC_ENABLED", "anthropic_enabled", "bool", False,
              "Load the Anthropic provider at runtime.", "providers"),
    _provider("ANTHROPIC_API_KEY", "anthropic_api_key", "str", "",
              "Anthropic provider API key.", "providers", secret=True),
    _restart("ANTHROPIC_BASE_URL", "anthropic_base_url", "str",
             "https://api.anthropic.com/v1",
             "Anthropic API base URL.", "providers", provider=True),
    _provider("ANTHROPIC_MODEL_PRIORITY", "anthropic_model_priority", "csv",
              [], "Anthropic model priority order.", "providers"),

    # ----------------------------------------------------------- OpenRouter
    _restart("OPENROUTER_API_KEY", "openrouter_api_key", "str", "",
             "OpenRouter provider API key.", "providers", secret=True),

    # --------------------------------------------------------------- Gemini
    _provider("GEMINI_ENABLED", "gemini_enabled", "bool", False,
              "Load the Gemini provider at runtime.", "providers"),
    _provider("GEMINI_API_KEY", "gemini_api_key", "str", "",
              "Gemini provider API key.", "providers", secret=True),
    _restart("GEMINI_BASE_URL", "gemini_base_url", "str",
             "https://generativelanguage.googleapis.com/v1beta",
             "Gemini API base URL.", "providers", provider=True),
    _provider("GEMINI_MODEL_PRIORITY", "gemini_model_priority", "csv", [],
              "Gemini model priority order.", "providers"),

    # ----------------------------------------------------------------- Groq
    _restart("GROQ_API_KEY", "groq_api_key", "str", "",
             "Groq provider API key.", "providers", secret=True),

    # ------------------------------------------------------------ LM Studio
    _provider("LMSTUDIO_ENABLED", "lmstudio_enabled", "bool", False,
              "Load the LM Studio provider at runtime.", "providers"),
    _restart("LMSTUDIO_BASE_URL", "lmstudio_base_url", "url",
             "http://localhost:1234/v1",
             "LM Studio server base URL.", "providers", provider=True),
    _provider("LMSTUDIO_API_KEY", "lmstudio_api_key", "str", "",
              "LM Studio provider API key.", "providers", secret=True),
    _restart("LMSTUDIO_PRIORITY", "lmstudio_priority", "int", 1,
             "LM Studio routing priority.", "providers", provider=True,
             minimum=0),
    _provider("LMSTUDIO_MODEL_PRIORITY", "lmstudio_model_priority", "csv", [],
              "LM Studio model priority order.", "providers"),

    # ---------------------------------------------------------------- Ollama
    _restart("OLLAMA_BASE_URL", "ollama_base_url", "str",
             "http://localhost:11434",
             "Ollama server base URL.", "providers", provider=True),
    _provider("OLLAMA_ENABLED", "ollama_enabled", "bool", False,
              "Load the Ollama provider at runtime.", "providers"),
    _provider("OLLAMA_MODEL_PRIORITY", "ollama_model_priority", "csv", [],
              "Ollama model priority order.", "providers"),

    # ==================================================================
    # Relay / logging / restart-affecting fields (order unconstrained).
    # ==================================================================

    SettingSpec(
        env=None,
        attr="relay_name",
        type="str",
        default="Relay",
        description="Fixed display name of this Relay instance.",
        category="general",
        effect=INFO,
        cli_visible=False,
    ),
    _restart("RELAY_HOST", "relay_host", "str", "127.0.0.1",
             "Server bind host.", "general"),
    _restart("RELAY_PORT", "relay_port", "int", 8000,
             "Server bind port.", "general", minimum=0),
    _restart("RELAY_KEYRING", "relay_keyring_enabled", "bool", False,
             "Resolve and write provider keys through the OS keyring.",
             "relay"),
    _restart("RELAY_KEYRING_BACKEND", "relay_keyring_backend", "str", "",
             "Introspection mirror of the keyring backend.", "relay"),
    _restart("RELAY_TUI_NO_EMBED", "relay_tui_no_embed", "bool", False,
             "Run the TUI UI-only against a separately running server.",
             "relay"),
    _provider("RELAY_API_KEY", "relay_api_key", "str", "",
              "Bootstrap API key for every endpoint outside the public "
              "allowlist.", "auth", secret=True),

    # -------------------------------------------------------------- logging
    _restart("LOG_LEVEL", "log_level", "str", "INFO",
             "Logging verbosity.", "logging"),
    _restart("LOG_FILE", "log_file", "str", "",
             "JSON log file path.", "logging"),

    # ------------------------------------------------- health refresh (restart)
    _restart("HEALTH_REFRESH_ENABLED", "health_refresh_enabled", "bool", False,
             "Enable background health refresh.", "health"),
    _restart("HEALTH_REFRESH_INTERVAL_SECONDS",
             "health_refresh_interval_seconds", "int", 300,
             "Background health refresh interval, in seconds.", "health",
             minimum=1),
    _restart("HEALTH_DEEP_REFRESH_ENABLED", "health_deep_refresh_enabled",
             "bool", False, "Enable deep health refresh.", "health"),

    # ------------------------------------------------- telemetry capacity
    _restart("TELEMETRY_MAX_FAILURE_HISTORY", "telemetry_max_failure_history",
             "int", 50, "Cap on retained failure-history entries.",
             "telemetry", minimum=1),

    # ------------------------------------------------------------ persistence
    _restart("PERSISTENCE_ENABLED", "persistence_enabled", "bool", False,
             "Persist learned state to platform.db.", "persistence"),
    _restart("PERSISTENCE_PATH", "persistence_path", "str", "",
             "State store path (derived when unset).", "persistence"),
    _restart("PERSISTENCE_FLUSH_INTERVAL_SECONDS",
             "persistence_flush_interval_seconds", "int", 60,
             "State flush cadence, in seconds.", "persistence", minimum=1),

    # ------------------------------------------------------------ request log
    _restart("REQUEST_LOG_FLUSH_INTERVAL_SECONDS",
             "request_log_flush_interval_seconds", "int", 5,
             "Durable request-log flush cadence, in seconds.",
             "request_log", minimum=1),
    _restart("REQUEST_LOG_RETENTION_DAYS", "request_log_retention_days",
             "int", 30,
             "Durable request-log retention, in days (0 disables pruning).",
             "request_log", minimum=0),
)

# ---------------------------------------------------------------------------
# TUI Configuration form metadata (mirrors app/ui/data.py _CONFIG_ROWS).
# Attached here so the golden tests can prove 1:1 parity and P7.3 can derive
# the form from this registry.
# ---------------------------------------------------------------------------
_TUI_META = {
    "TASK_ROUTING_ENABLED": ("bool", "routing", True, "Enable task routing",
                             "Route requests by task category using the TASK_* model preferences."),
    "CROSS_PROVIDER_MODEL_SELECTION": ("bool", "routing", True,
                                       "Cross-provider model selection",
                                       "Allow a bare model reference to match every provider that has it."),
    "TASK_CODING": ("csv", "routing", True, "Coding models",
                    "Comma-separated model refs (model or Provider:model)."),
    "TASK_VISION": ("csv", "routing", True, "Vision models",
                    "Comma-separated model refs (model or Provider:model)."),
    "TASK_REASONING": ("csv", "routing", True, "Reasoning models",
                       "Comma-separated model refs (model or Provider:model)."),
    "TASK_GENERAL": ("csv", "routing", True, "General models",
                     "Comma-separated model refs (model or Provider:model)."),
    "TASK_CREATIVE": ("csv", "routing", True, "Creative models",
                      "Comma-separated model refs (model or Provider:model)."),
    "TASK_TRANSLATION": ("csv", "routing", True, "Translation models",
                         "Comma-separated model refs (model or Provider:model)."),
    "REQUEST_TIMEOUT": ("text", "failover", True, "Request timeout (s)",
                        "Seconds before a provider request times out."),
    "MAX_RETRIES": ("text", "failover", True, "Max retries",
                    "Retries across fallback candidates."),
    "RETRY_HONOR_RETRY_AFTER": ("bool", "failover", True, "Honor Retry-After",
                                "Wait for a provider's Retry-After on rate-limit responses."),
    "RETRY_AFTER_MAX_SECONDS": ("text", "failover", True, "Retry-After cap (s)",
                                "Upper bound for a Retry-After wait."),
    "RETRY_BACKOFF_BASE_SECONDS": ("text", "failover", True, "Backoff base (s)",
                                   "Exponential backoff base between retries (0 = immediate)."),
    "RETRY_BACKOFF_MAX_SECONDS": ("text", "failover", True, "Backoff max (s)",
                                  "Cap for exponential backoff."),
    "REQUEST_TIMEOUT_BUDGET_SECONDS": ("text", "failover", True,
                                       "Request budget (s)",
                                       "Overall wall-clock budget for a chat request (0 = off)."),
    "RELAY_HOST": ("text", "restart", False, "Host",
                   "Server bind host. Restart required."),
    "RELAY_PORT": ("text", "restart", False, "Port",
                   "Server bind port. Restart required."),
    "PERSISTENCE_ENABLED": ("bool", "restart", False, "Persistence",
                            "Learned-state persistence. Restart required."),
    "PERSISTENCE_PATH": ("text", "restart", False, "Persistence path",
                         "State store path. Restart required."),
    "PERSISTENCE_FLUSH_INTERVAL_SECONDS": ("text", "restart", False,
                                           "Flush interval (s)",
                                           "State flush cadence. Restart required."),
    "LOG_LEVEL": ("text", "restart", False, "Log level",
                  "Logging verbosity. Restart required."),
    "LOG_FILE": ("text", "restart", False, "Log file",
                 "JSON log file path. Restart required."),
    "LMSTUDIO_BASE_URL": ("text", "restart", False, "LM Studio URL",
                          "LM Studio server base URL. Restart required."),
}

# Attach TUI metadata to the matching spec entries by rebuilding the tuple.
def _with_tui(spec: SettingSpec) -> SettingSpec:
    kind, group, editable, label, hint = _TUI_META[spec.env]
    return SettingSpec(
        env=spec.env,
        attr=spec.attr,
        type=spec.type,
        default=spec.default,
        description=spec.description,
        category=spec.category,
        secret=spec.secret,
        effect=spec.effect,
        provider=spec.provider,
        minimum=spec.minimum,
        exclusive_minimum=spec.exclusive_minimum,
        maximum=spec.maximum,
        cli_visible=spec.cli_visible,
        tui=True,
        tui_kind=kind,
        tui_group=group,
        tui_editable=editable,
        label=label,
        hint=hint,
    )


SPECS = tuple(
    _with_tui(spec) if spec.env in _TUI_META else spec for spec in _RAW_SPECS
)

# ---------------------------------------------------------------------------
# Lookup maps.
# ---------------------------------------------------------------------------
SPEC_BY_ENV = {spec.env: spec for spec in SPECS if spec.env is not None}
SPEC_BY_ATTR = {spec.attr: spec for spec in SPECS}

# Sanity: no duplicate env or attr (fail at import time, not at a later diff).
if len(SPEC_BY_ENV) != sum(1 for spec in SPECS if spec.env is not None):
    raise AssertionError("config_spec: duplicate env var in SPECS")
if len(SPEC_BY_ATTR) != len(SPECS):
    raise AssertionError("config_spec: duplicate attribute in SPECS")


def reload_secret_fields() -> tuple[str, ...]:
    """
    Fields whose values reload reports by name only — the exact rule the
    reload engine uses: the bootstrap key plus every RUNTIME_READY provider
    key attribute.
    """
    return ("relay_api_key",) + tuple(
        defn.key_attr
        for defn in PROVIDER_REGISTRY.values()
        if defn.id in RUNTIME_READY and defn.key_attr
    )


def provider_reloadable_fields() -> tuple[str, ...]:
    """
    Provider fields hot-reload applies: ``<prefix>_enabled``,
    ``<prefix>_api_key``, ``<prefix>_model_priority`` for every RUNTIME_READY
    provider, in registry order (reproduces reload's triplets exactly).
    """
    return tuple(
        f"{defn.id}_{suffix}"
        for defn in PROVIDER_REGISTRY.values()
        if defn.id in RUNTIME_READY
        for suffix in ("enabled", "api_key", "model_priority")
    )


def simple_reloadable_fields() -> tuple[str, ...]:
    """
    Live-reloadable fields that are neither secrets nor provider-attributed,
    in SPECS order (== reload's _SIMPLE_FIELDS order).
    """
    return tuple(
        spec.attr
        for spec in SPECS
        if spec.reloadable and not spec.secret and not spec.provider
    )


def reloadable_fields() -> tuple[str, ...]:
    """
    The complete reload allowlist, built the same way the reload engine
    builds it: simple fields + reload secrets + provider triplets. Order and
    duplicates reproduce ``reload._RELOADABLE_FIELDS`` exactly.
    """
    return (
        simple_reloadable_fields()
        + reload_secret_fields()
        + provider_reloadable_fields()
    )


def secret_fields() -> tuple[str, ...]:
    """Every field whose value must never be displayed in raw form."""
    return tuple(spec.attr for spec in SPECS if spec.secret)


def tui_fields() -> tuple[SettingSpec, ...]:
    """The spec entries surfaced by the TUI Configuration form."""
    return tuple(spec for spec in SPECS if spec.tui)


# ---------------------------------------------------------------------------
# Validation and parsing (mirrors Settings.__init__ call sites).
# ---------------------------------------------------------------------------
def validate_value(spec: SettingSpec, raw: str) -> None:
    """
    Raise ``ValueError`` when ``raw`` is invalid for ``spec``, using the same
    validators (and bounds) ``Settings.__init__`` uses.
    """
    if spec.type == "int":
        _valid_int(spec.env, raw, minimum=int(spec.minimum or 0))
    elif spec.type == "float":
        _valid_float(
            spec.env,
            raw,
            minimum=float(spec.minimum or 0.0),
            exclusive_minimum=spec.exclusive_minimum,
            maximum=spec.maximum,
        )
    elif spec.type == "url":
        _valid_url(spec.env, raw, str(spec.default or ""))


def parse_value(spec: SettingSpec, raw: str):
    """
    Parse ``raw`` the way ``Settings`` would (typed value). Raises
    ``ValueError`` on junk.
    """
    if spec.type == "bool":
        return raw.lower() == "true"
    if spec.type == "int":
        _valid_int(spec.env, raw, minimum=int(spec.minimum or 0))
        return int(raw)
    if spec.type == "float":
        _valid_float(
            spec.env,
            raw,
            minimum=float(spec.minimum or 0.0),
            exclusive_minimum=spec.exclusive_minimum,
            maximum=spec.maximum,
        )
        return float(raw)
    if spec.type == "csv":
        return _csv(raw)
    if spec.type == "url":
        return _valid_url(spec.env, raw, str(spec.default or ""))
    return raw


def render_value(spec: SettingSpec, value) -> str:
    """Render a typed value the way ``Settings`` stores it for display."""
    if spec.type == "bool":
        return "true" if value else "false"
    if spec.type == "csv":
        return ",".join(value or [])
    if value is None:
        return ""
    return str(value)
