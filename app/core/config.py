from pathlib import Path
from typing import List
import math
import os

from dotenv import load_dotenv

# The project root is the directory that contains the `app` package.
# When installed as a package this is the site-packages directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# True when running from a source checkout (the project root carries a
# pyproject.toml). Source checkouts keep config/state next to the source
# tree so nothing surprises developers; installed packages use a stable
# per-user data directory instead (see _user_data_dir).
IS_SOURCE_CHECKOUT = (PROJECT_ROOT / "pyproject.toml").exists()


def _user_data_dir() -> Path:
    """
    Stable per-user data directory for installed Relay installations.

    Overridable with RELAY_DATA_DIR (also how tests redirect writes).
    Defaults to the platform user data directory (platformdirs), e.g.
    ``~/.local/share/relay`` on Linux and ``%LOCALAPPDATA%\\relay`` on
    Windows.
    """
    override = os.getenv("RELAY_DATA_DIR")
    if override:
        return Path(override)

    import platformdirs

    return Path(platformdirs.user_data_dir(appname="relay", appauthor=False))


def _resolve_env_file() -> Path:
    """
    Locate the `.env` file.

    Source checkouts (priority order):
      1. The RELAY_ENV_FILE override.
      2. `.env` in the current working directory.
      3. `.env` next to the app package.
    Installed packages always use ``<user data dir>/.env`` so the wizard
    has one stable place to write configuration regardless of CWD.
    """
    override = os.getenv("RELAY_ENV_FILE")
    if override:
        return Path(override)

    if IS_SOURCE_CHECKOUT:
        cwd_env = Path.cwd() / ".env"
        if cwd_env.exists():
            return cwd_env

        return PROJECT_ROOT / ".env"

    return _user_data_dir() / ".env"


# The active configuration file. The setup wizard and CLI read/write this
# exact file.
env_file = _resolve_env_file()
load_dotenv(env_file)

# Setup/state storage directory. Holds first-run and setup state so Relay
# can distinguish "never configured" from "configured and ready".
def _resolve_state_dir() -> Path:
    override = os.getenv("RELAY_STATE_DIR")
    if override:
        return Path(override)

    if IS_SOURCE_CHECKOUT:
        return _resolve_env_file().parent / ".relay"

    return _user_data_dir()


state_dir = _resolve_state_dir()


def _resolve_persistence_path() -> Path:
    return _resolve_state_dir() / "platform.db"


def _csv(value: str) -> List[str]:
    """
    Parse a comma-separated list from an env value.
    """
    return [
        item.strip()
        for item in value.split(",")
        if item.strip()
    ]


def _valid_int(name: str, raw: str, minimum: int = 0) -> int:
    """
    Parse a non-negative integer env value, rejecting junk with a clear
    message instead of a traceback.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid value for {name}: {raw!r} (expected an integer)."
        )

    if value < minimum:
        raise ValueError(
            f"Invalid value for {name}: {raw!r} (must be >= {minimum})."
        )

    return value


def _valid_float(
    name: str,
    raw: str,
    minimum: float = 0.0,
    exclusive_minimum: bool = False,
    maximum: float | None = None,
) -> float:
    """
    Parse a finite number env value, rejecting junk, NaN/inf, and values
    outside the allowed range with a clear message.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid value for {name}: {raw!r} (expected a number)."
        )

    if math.isnan(value) or math.isinf(value):
        raise ValueError(
            f"Invalid value for {name}: {raw!r} (must be finite)."
        )

    if exclusive_minimum:
        if value <= minimum:
            raise ValueError(
                f"Invalid value for {name}: {raw!r} (must be > {minimum})."
            )
    elif value < minimum:
        raise ValueError(
            f"Invalid value for {name}: {raw!r} (must be >= {minimum})."
        )

    if maximum is not None and value > maximum:
        raise ValueError(
            f"Invalid value for {name}: {raw!r} (must be <= {maximum})."
        )

    return value


def _valid_url(name: str, raw: str, default: str) -> str:
    """
    Parse an HTTP(S) base URL, rejecting other schemes with a clear
    message. A trailing slash is stripped so endpoints can be built with
    "/<path>" safely.
    """
    value = (raw or default).strip()

    if not value.startswith(("http://", "https://")):
        raise ValueError(
            f"Invalid value for {name}: {value!r} "
            "(must start with http:// or https://)."
        )

    return value.rstrip("/")


class Settings:
    """
    Relay configuration.
    """

    def __init__(self):
        # =========================
        # General
        # =========================

        self.relay_name = "Relay"
        self.relay_host = os.getenv("RELAY_HOST", "127.0.0.1")
        self.relay_port = _valid_int(
            "RELAY_PORT",
            os.getenv("RELAY_PORT", "8000"),
            minimum=0,
        )
        self.request_timeout = _valid_int(
            "REQUEST_TIMEOUT",
            os.getenv("REQUEST_TIMEOUT", "120"),
            minimum=1,
        )
        self.max_retries = _valid_int(
            "MAX_RETRIES",
            os.getenv("MAX_RETRIES", "1"),
            minimum=0,
        )
        # Honor the provider's Retry-After on 429 (and other) responses.
        # Off by default to preserve immediate-retry behavior.
        self.retry_honor_retry_after = (
            os.getenv("RETRY_HONOR_RETRY_AFTER", "false").lower() == "true"
        )
        self.retry_after_max_seconds = _valid_int(
            "RETRY_AFTER_MAX_SECONDS",
            os.getenv("RETRY_AFTER_MAX_SECONDS", "60"),
            minimum=0,
        )
        # Exponential backoff base between retries, in seconds. 0 disables
        # backoff (immediate retry), preserving current behavior.
        self.retry_backoff_base_seconds = _valid_int(
            "RETRY_BACKOFF_BASE_SECONDS",
            os.getenv("RETRY_BACKOFF_BASE_SECONDS", "0"),
            minimum=0,
        )
        self.retry_backoff_max_seconds = _valid_int(
            "RETRY_BACKOFF_MAX_SECONDS",
            os.getenv("RETRY_BACKOFF_MAX_SECONDS", "60"),
            minimum=0,
        )
        # Overall wall-clock budget for a chat request, in seconds. 0
        # disables the budget, preserving current behavior.
        self.request_timeout_budget_seconds = _valid_int(
            "REQUEST_TIMEOUT_BUDGET_SECONDS",
            os.getenv("REQUEST_TIMEOUT_BUDGET_SECONDS", "0"),
            minimum=0,
        )

        # =========================
        # Logging
        # =========================

        self.log_level = os.getenv("LOG_LEVEL", "INFO")
        self.log_file = os.getenv("LOG_FILE", "")

        # =========================
        # Provider toggles
        # =========================

        self.nvidia_enabled = (
            os.getenv("NVIDIA_ENABLED", "true").lower() == "true"
        )
        self.openai_enabled = (
            os.getenv("OPENAI_ENABLED", "false").lower() == "true"
        )

        # =========================
        # Model priority
        # =========================

        self.nvidia_model_priority = _csv(
            os.getenv("NVIDIA_MODEL_PRIORITY", "")
        )
        self.openai_model_priority = _csv(
            os.getenv("OPENAI_MODEL_PRIORITY", "")
        )

        # =========================
        # NVIDIA
        # =========================

        self.nvidia_api_key = os.getenv("NVIDIA_API_KEY", "")

        # =========================
        # OpenAI
        # =========================

        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")

        # =========================
        # Anthropic
        # =========================

        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.anthropic_enabled = (
            os.getenv("ANTHROPIC_ENABLED", "false").lower() == "true"
        )
        self.anthropic_base_url = os.getenv(
            "ANTHROPIC_BASE_URL",
            "https://api.anthropic.com/v1",
        )
        self.anthropic_model_priority = _csv(
            os.getenv("ANTHROPIC_MODEL_PRIORITY", "")
        )

        # =========================
        # OpenRouter
        # =========================

        self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")

        # =========================
        # Google Gemini
        # =========================

        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "")
        self.gemini_enabled = (
            os.getenv("GEMINI_ENABLED", "false").lower() == "true"
        )
        self.gemini_base_url = os.getenv(
            "GEMINI_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta",
        )
        self.gemini_model_priority = _csv(
            os.getenv("GEMINI_MODEL_PRIORITY", "")
        )

        # =========================
        # Groq
        # =========================

        self.groq_api_key = os.getenv("GROQ_API_KEY", "")

        # =========================
        # Local Providers
        # =========================

        self.lmstudio_enabled = (
            os.getenv("LMSTUDIO_ENABLED", "false").lower() == "true"
        )
        self.lmstudio_base_url = _valid_url(
            "LMSTUDIO_BASE_URL",
            os.getenv("LMSTUDIO_BASE_URL", ""),
            "http://localhost:1234/v1",
        )
        self.lmstudio_api_key = os.getenv("LMSTUDIO_API_KEY", "")
        self.lmstudio_priority = _valid_int(
            "LMSTUDIO_PRIORITY",
            os.getenv("LMSTUDIO_PRIORITY", "1"),
            minimum=0,
        )
        self.lmstudio_model_priority = _csv(
            os.getenv("LMSTUDIO_MODEL_PRIORITY", "")
        )

        self.ollama_base_url = os.getenv(
            "OLLAMA_BASE_URL",
            "http://localhost:11434",
        )
        self.ollama_enabled = (
            os.getenv("OLLAMA_ENABLED", "false").lower() == "true"
        )
        self.ollama_model_priority = _csv(
            os.getenv("OLLAMA_MODEL_PRIORITY", "")
        )

        # =========================
        # Relay
        # =========================

        # Keyring (P5 Phase 2): keyring-first provider-key resolution.
        # Opt-in and non-reloadable (read at startup only). When enabled,
        # runtime provider keys resolve keyring-first with the .env value
        # as fallback, and config writes for api_key go to the keyring
        # instead of .env. RELAY_KEYRING_BACKEND mirrors the keyring
        # store's dynamic per-call read for introspection.
        self.relay_keyring_enabled = (
            os.getenv("RELAY_KEYRING", "false").lower() == "true"
        )
        self.relay_keyring_backend = os.getenv("RELAY_KEYRING_BACKEND", "")

        # =========================
        # Observability
        # =========================

        # Rolling operations window for diagnostics. In-memory only;
        # never persisted to SQLite. 0 disables time-based pruning.
        self.ops_window_seconds = _valid_int(
            "OPS_WINDOW_SECONDS",
            os.getenv("OPS_WINDOW_SECONDS", "300"),
            minimum=0,
        )
        self.ops_max_events = _valid_int(
            "OPS_MAX_EVENTS",
            os.getenv("OPS_MAX_EVENTS", "10000"),
            minimum=1,
        )

        # =========================
        # Proxy
        # =========================

        # Outbound provider requests honor HTTP_PROXY / HTTPS_PROXY /
        # NO_PROXY. Defaults preserve httpx's own trust_env behavior.
        # Set PROXY_ENABLED=false to disable proxy support entirely, or
        # override per-provider via the Provider.proxy field.
        self.http_proxy = os.getenv("HTTP_PROXY", "")
        self.https_proxy = os.getenv("HTTPS_PROXY", "")
        self.no_proxy = os.getenv("NO_PROXY", "")
        self.proxy_enabled = (
            os.getenv("PROXY_ENABLED", "true").lower() == "true"
        )

        # =========================
        # API authentication
        # =========================

        # Empty (default) disables authentication and preserves existing
        # behavior. When set, every endpoint except the public allowlist
        # requires this API key via either the Authorization Bearer header
        # or the X-Relay-API-Key header.
        self.relay_api_key = os.getenv("RELAY_API_KEY", "")

        # Store-backed API-key authentication (P5 Phase 4): when enabled,
        # the auth dependency also accepts keys from the KeyStore
        # (platform.db) with scope enforcement, after the bootstrap key
        # above. Read per request like RELAY_API_KEY, so flipping it takes
        # effect without a restart. Off by default, preserving the
        # bootstrap-only behavior. Fails closed: with this on, non-public
        # requests require a valid key.
        self.relay_auth_store = (
            os.getenv("RELAY_AUTH_STORE", "false").lower() == "true"
        )

        # =========================
        # Terminal UI
        # =========================

        # When set, the TUI runs UI-only and expects a separately running
        # `relay serve` (e.g., managed by a service manager) instead of
        # starting an embedded API server thread.
        self.relay_tui_no_embed = (
            os.getenv("RELAY_TUI_NO_EMBED", "").lower() == "true"
        )

        # =========================
        # Task routing
        # =========================

        self.task_routing_enabled = (
            os.getenv("TASK_ROUTING_ENABLED", "false").lower() == "true"
        )

        self.cross_provider_model_selection = (
            os.getenv("CROSS_PROVIDER_MODEL_SELECTION", "false").lower() == "true"
        )

        self.task_coding = _csv(os.getenv("TASK_CODING", ""))
        self.task_vision = _csv(os.getenv("TASK_VISION", ""))
        self.task_reasoning = _csv(os.getenv("TASK_REASONING", ""))
        self.task_general = _csv(os.getenv("TASK_GENERAL", ""))
        self.task_creative = _csv(os.getenv("TASK_CREATIVE", ""))
        self.task_translation = _csv(os.getenv("TASK_TRANSLATION", ""))

        # =========================
        # Health-aware routing
        # =========================

        self.health_aware_routing = (
            os.getenv("HEALTH_AWARE_ROUTING", "false").lower() == "true"
        )
        self.health_ttl_seconds = _valid_int(
            "HEALTH_TTL_SECONDS",
            os.getenv("HEALTH_TTL_SECONDS", "300"),
            minimum=0,
        )
        self.health_feedback_enabled = (
            os.getenv("HEALTH_FEEDBACK_ENABLED", "false").lower() == "true"
        )
        self.health_degraded_ttl_seconds = _valid_int(
            "HEALTH_DEGRADED_TTL_SECONDS",
            os.getenv("HEALTH_DEGRADED_TTL_SECONDS", "60"),
            minimum=0,
        )
        self.health_unavailable_ttl_seconds = _valid_int(
            "HEALTH_UNAVAILABLE_TTL_SECONDS",
            os.getenv("HEALTH_UNAVAILABLE_TTL_SECONDS", "900"),
            minimum=0,
        )
        self.health_refresh_enabled = (
            os.getenv("HEALTH_REFRESH_ENABLED", "false").lower() == "true"
        )
        self.health_refresh_interval_seconds = _valid_int(
            "HEALTH_REFRESH_INTERVAL_SECONDS",
            os.getenv("HEALTH_REFRESH_INTERVAL_SECONDS", "300"),
            minimum=1,
        )
        self.health_deep_refresh_enabled = (
            os.getenv("HEALTH_DEEP_REFRESH_ENABLED", "false").lower() == "true"
        )

        # =========================
        # Health feedback tuning
        # =========================

        self.health_feedback_model_server_error_threshold = _valid_int(
            "HEALTH_FEEDBACK_MODEL_SERVER_ERROR_THRESHOLD",
            os.getenv("HEALTH_FEEDBACK_MODEL_SERVER_ERROR_THRESHOLD", "1"),
            minimum=1,
        )
        self.health_feedback_provider_server_error_threshold = _valid_int(
            "HEALTH_FEEDBACK_PROVIDER_SERVER_ERROR_THRESHOLD",
            os.getenv("HEALTH_FEEDBACK_PROVIDER_SERVER_ERROR_THRESHOLD", "3"),
            minimum=1,
        )
        self.health_feedback_model_timeout_degraded_threshold = _valid_int(
            "HEALTH_FEEDBACK_MODEL_TIMEOUT_DEGRADED_THRESHOLD",
            os.getenv("HEALTH_FEEDBACK_MODEL_TIMEOUT_DEGRADED_THRESHOLD", "2"),
            minimum=1,
        )
        self.health_feedback_model_timeout_unavailable_threshold = _valid_int(
            "HEALTH_FEEDBACK_MODEL_TIMEOUT_UNAVAILABLE_THRESHOLD",
            os.getenv("HEALTH_FEEDBACK_MODEL_TIMEOUT_UNAVAILABLE_THRESHOLD", "5"),
            minimum=1,
        )
        self.health_feedback_model_invalid_request_unavailable_threshold = _valid_int(
            "HEALTH_FEEDBACK_MODEL_INVALID_REQUEST_UNAVAILABLE_THRESHOLD",
            os.getenv(
                "HEALTH_FEEDBACK_MODEL_INVALID_REQUEST_UNAVAILABLE_THRESHOLD",
                "3",
            ),
            minimum=1,
        )
        self.health_feedback_model_unknown_degraded_threshold = _valid_int(
            "HEALTH_FEEDBACK_MODEL_UNKNOWN_DEGRADED_THRESHOLD",
            os.getenv("HEALTH_FEEDBACK_MODEL_UNKNOWN_DEGRADED_THRESHOLD", "3"),
            minimum=1,
        )
        self.health_freshness_exponent = _valid_float(
            "HEALTH_FRESHNESS_EXPONENT",
            os.getenv("HEALTH_FRESHNESS_EXPONENT", "1.0"),
            minimum=0.0,
        )

        # =========================
        # Scoring tuning
        # =========================

        self.scoring_priority_weight = _valid_float(
            "SCORING_PRIORITY_WEIGHT",
            os.getenv("SCORING_PRIORITY_WEIGHT", "1.0"),
            minimum=0.0,
        )
        self.scoring_success_weight = _valid_float(
            "SCORING_SUCCESS_WEIGHT",
            os.getenv("SCORING_SUCCESS_WEIGHT", "1.0"),
            minimum=0.0,
        )
        self.scoring_latency_weight = _valid_float(
            "SCORING_LATENCY_WEIGHT",
            os.getenv("SCORING_LATENCY_WEIGHT", "1.0"),
            minimum=0.0,
        )
        self.scoring_failure_weight = _valid_float(
            "SCORING_FAILURE_WEIGHT",
            os.getenv("SCORING_FAILURE_WEIGHT", "1.0"),
            minimum=0.0,
        )
        self.scoring_preference_weight = _valid_float(
            "SCORING_PREFERENCE_WEIGHT",
            os.getenv("SCORING_PREFERENCE_WEIGHT", "1.0"),
            minimum=0.0,
        )
        self.scoring_priority_denom = _valid_float(
            "SCORING_PRIORITY_DENOM",
            os.getenv("SCORING_PRIORITY_DENOM", "10"),
            minimum=0.0,
            exclusive_minimum=True,
        )
        self.scoring_latency_ref_ms = _valid_float(
            "SCORING_LATENCY_REF_MS",
            os.getenv("SCORING_LATENCY_REF_MS", "250"),
            minimum=0.0,
            exclusive_minimum=True,
        )
        self.scoring_failure_ref_count = _valid_int(
            "SCORING_FAILURE_REF_COUNT",
            os.getenv("SCORING_FAILURE_REF_COUNT", "5"),
            minimum=1,
        )
        # Future cost placeholder weight (Phase 7E). Defaults to zero so
        # candidate ordering and fitness stay byte-identical to the
        # current formula; the placeholder only becomes meaningful once
        # cost data and a nonzero weight are configured.
        self.scoring_cost_weight = _valid_float(
            "SCORING_COST_WEIGHT",
            os.getenv("SCORING_COST_WEIGHT", "0.0"),
            minimum=0.0,
        )

        # =========================
        # Task classification
        # =========================

        # Free-text task classification for /chat requests (Phase 7B).
        # Off by default: the endpoint keeps validating the explicit
        # task field exactly as before.
        self.task_classification_enabled = (
            os.getenv("TASK_CLASSIFICATION_ENABLED", "false").lower() == "true"
        )
        self.task_classification_threshold = _valid_float(
            "TASK_CLASSIFICATION_THRESHOLD",
            os.getenv("TASK_CLASSIFICATION_THRESHOLD", "0.6"),
            minimum=0.0,
        )

        # =========================
        # Task capability catalog
        # =========================

        # Model capability catalog (Phase 7A): adds a task-compatibility
        # signal to within-band scoring. Off by default so candidate
        # ordering stays byte-identical to the legacy formula.
        self.task_catalog_enabled = (
            os.getenv("TASK_CATALOG_ENABLED", "false").lower() == "true"
        )
        self.scoring_task_compatibility_weight = _valid_float(
            "SCORING_TASK_COMPATIBILITY_WEIGHT",
            os.getenv("SCORING_TASK_COMPATIBILITY_WEIGHT", "1.0"),
            minimum=0.0,
        )

        # =========================
        # Adaptive routing
        # =========================

        # Adaptive within-band reordering (Phase 7C): the scoring layer
        # learns EWMA reliability and latency from telemetry and nudges
        # the reliability/latency signal weights per candidate. Off by
        # default so candidate ordering stays byte-identical to the
        # legacy formula. The health band always remains the primary
        # ordering key; adaptive signals only reorder within a band.
        self.adaptive_routing_enabled = (
            os.getenv("ADAPTIVE_ROUTING_ENABLED", "false").lower() == "true"
        )
        # Minimum observed samples before a pair's EWMA state is trusted.
        # Below this, adaptive signals resolve to neutral for every
        # candidate, so cold-start data never steers ordering.
        self.adaptive_min_samples = _valid_int(
            "ADAPTIVE_MIN_SAMPLES",
            os.getenv("ADAPTIVE_MIN_SAMPLES", "10"),
            minimum=1,
        )
        # EWMA learning rate, capped to [0, 1] so no single observation
        # can move the estimate by more than its full weight.
        self.adaptive_learning_rate = _valid_float(
            "ADAPTIVE_LEARNING_RATE",
            os.getenv("ADAPTIVE_LEARNING_RATE", "0.1"),
            minimum=0.0,
        )
        self.adaptive_latency_weight = _valid_float(
            "ADAPTIVE_LATENCY_WEIGHT",
            os.getenv("ADAPTIVE_LATENCY_WEIGHT", "1.0"),
            minimum=0.0,
        )
        self.adaptive_reliability_weight = _valid_float(
            "ADAPTIVE_RELIABILITY_WEIGHT",
            os.getenv("ADAPTIVE_RELIABILITY_WEIGHT", "1.0"),
            minimum=0.0,
        )

        # =========================
        # Quality feedback
        # =========================

        # Optional user quality feedback routing (Phase 7D): metadata-only
        # quality ratings nudge within-band ordering once a pair has
        # enough confident samples. Off by default so candidate ordering
        # stays byte-identical to the legacy formula. Quality feedback
        # only ever reorders within an existing health band; it never
        # overrides health safety or operational reliability.
        self.quality_feedback_enabled = (
            os.getenv("QUALITY_FEEDBACK_ENABLED", "false").lower() == "true"
        )
        # Minimum ratings before a pair's quality estimate is trusted.
        # Below this the quality signal resolves to neutral for every
        # candidate, so sparse or noisy feedback never steers ordering.
        self.quality_feedback_min_samples = _valid_int(
            "QUALITY_FEEDBACK_MIN_SAMPLES",
            os.getenv("QUALITY_FEEDBACK_MIN_SAMPLES", "10"),
            minimum=1,
        )
        # EWMA learning rate for quality, capped to [0, 1] so a single
        # rating can never move the estimate by more than its full weight.
        # Values above 1.0 would be silently clamped by the store anyway,
        # so they are rejected at config time.
        self.quality_feedback_learning_rate = _valid_float(
            "QUALITY_FEEDBACK_LEARNING_RATE",
            os.getenv("QUALITY_FEEDBACK_LEARNING_RATE", "0.1"),
            minimum=0.0,
            maximum=1.0,
        )
        # Cap on distinct (provider, model) quality aggregates kept in
        # memory, so the store stays bounded under any feedback volume.
        self.quality_feedback_retention_limit = _valid_int(
            "QUALITY_FEEDBACK_RETENTION_LIMIT",
            os.getenv("QUALITY_FEEDBACK_RETENTION_LIMIT", "10000"),
            minimum=1,
        )
        # Weight of the within-band quality contribution.
        self.quality_feedback_weight = _valid_float(
            "QUALITY_FEEDBACK_WEIGHT",
            os.getenv("QUALITY_FEEDBACK_WEIGHT", "1.0"),
            minimum=0.0,
        )

        # =========================
        # Decision engine
        # =========================

        # Explicit explainable decision layer (Phase 7E). When enabled,
        # provider selection flows through DecisionEngine, which produces
        # explicit DecisionScore objects with per-signal contributions,
        # confidence, and explanation metadata. When disabled, the
        # existing candidate path drives selection unchanged. The engine
        # always preserves the health-band ordering invariant and every
        # per-signal feature gate.
        self.decision_engine_enabled = (
            os.getenv("DECISION_ENGINE_ENABLED", "false").lower() == "true"
        )

        # =========================
        # Telemetry
        # =========================

        self.telemetry_enabled = (
            os.getenv("TELEMETRY_ENABLED", "false").lower() == "true"
        )
        self.telemetry_max_failure_history = _valid_int(
            "TELEMETRY_MAX_FAILURE_HISTORY",
            os.getenv("TELEMETRY_MAX_FAILURE_HISTORY", "50"),
            minimum=1,
        )

        # =========================
        # Decision explanations
        # =========================

        self.decision_explanations_enabled = (
            os.getenv("DECISION_EXPLANATIONS_ENABLED", "false").lower() == "true"
        )

        # =========================
        # Persistence
        # =========================

        # Disabled by default: Relay runs entirely in memory unless
        # explicitly enabled, preserving current behavior.
        self.persistence_enabled = (
            os.getenv("PERSISTENCE_ENABLED", "false").lower() == "true"
        )
        self.persistence_path = os.getenv("PERSISTENCE_PATH", "") or str(
            _resolve_persistence_path()
        )
        self.persistence_flush_interval_seconds = _valid_int(
            "PERSISTENCE_FLUSH_INTERVAL_SECONDS",
            os.getenv("PERSISTENCE_FLUSH_INTERVAL_SECONDS", "60"),
            minimum=1,
        )
        # Retention for persisted telemetry failure history, in days.
        # 0 disables retention pruning.
        self.persistence_retention_days = _valid_int(
            "PERSISTENCE_RETENTION_DAYS",
            os.getenv("PERSISTENCE_RETENTION_DAYS", "0"),
            minimum=0,
        )

        # Durable request log (schema v6). Flush cadence for the
        # background write-behind; retention bounds how many days of
        # metadata rows are kept (0 disables pruning).
        self.request_log_flush_interval_seconds = _valid_int(
            "REQUEST_LOG_FLUSH_INTERVAL_SECONDS",
            os.getenv("REQUEST_LOG_FLUSH_INTERVAL_SECONDS", "5"),
            minimum=1,
        )
        self.request_log_retention_days = _valid_int(
            "REQUEST_LOG_RETENTION_DAYS",
            os.getenv("REQUEST_LOG_RETENTION_DAYS", "30"),
            minimum=0,
        )

        # =========================
        # Project continuity & model handoff (P9)
        # =========================

        # Disabled by default: continuity surfaces are inert, no rows are
        # ever written, and chat routing stays byte-identical until this
        # flag is explicitly enabled. Every continuity setting is
        # restart-required (none are hot-reloadable).
        self.continuity_enabled = (
            os.getenv("CONTINUITY_ENABLED", "false").lower() == "true"
        )
        self.continuity_retention_days = _valid_int(
            "CONTINUITY_RETENTION_DAYS",
            os.getenv("CONTINUITY_RETENTION_DAYS", "30"),
            minimum=0,
        )
        self.continuity_flush_interval_seconds = _valid_int(
            "CONTINUITY_FLUSH_INTERVAL_SECONDS",
            os.getenv("CONTINUITY_FLUSH_INTERVAL_SECONDS", "5"),
            minimum=1,
        )
        self.continuity_context_token_budget = _valid_int(
            "CONTINUITY_CONTEXT_TOKEN_BUDGET",
            os.getenv("CONTINUITY_CONTEXT_TOKEN_BUDGET", "32768"),
            minimum=1,
        )
        self.continuity_output_reserve_tokens = _valid_int(
            "CONTINUITY_OUTPUT_RESERVE_TOKENS",
            os.getenv("CONTINUITY_OUTPUT_RESERVE_TOKENS", "2048"),
            minimum=0,
        )
        self.continuity_summary_share = _valid_float(
            "CONTINUITY_SUMMARY_SHARE",
            os.getenv("CONTINUITY_SUMMARY_SHARE", "0.4"),
            minimum=0.0,
            maximum=1.0,
        )
        self.continuity_summary_max_chars = _valid_int(
            "CONTINUITY_SUMMARY_MAX_CHARS",
            os.getenv("CONTINUITY_SUMMARY_MAX_CHARS", "4096"),
            minimum=1,
        )
        self.continuity_tail_max_items = _valid_int(
            "CONTINUITY_TAIL_MAX_ITEMS",
            os.getenv("CONTINUITY_TAIL_MAX_ITEMS", "20"),
            minimum=1,
        )
        self.continuity_chars_per_token = _valid_int(
            "CONTINUITY_CHARS_PER_TOKEN",
            os.getenv("CONTINUITY_CHARS_PER_TOKEN", "4"),
            minimum=1,
        )
        self.continuity_summarizer_model = os.getenv(
            "CONTINUITY_SUMMARIZER_MODEL", ""
        ).strip()
        self.max_switches_per_turn = _valid_int(
            "MAX_SWITCHES_PER_TURN",
            os.getenv("MAX_SWITCHES_PER_TURN", "3"),
            minimum=1,
        )
        self.max_switches_per_window = _valid_int(
            "MAX_SWITCHES_PER_WINDOW",
            os.getenv("MAX_SWITCHES_PER_WINDOW", "5"),
            minimum=1,
        )
        self.max_resume_replays = _valid_int(
            "MAX_RESUME_REPLAYS",
            os.getenv("MAX_RESUME_REPLAYS", "3"),
            minimum=0,
        )


settings = Settings()


def reload_settings() -> Settings:
    """
    Re-read the active ``.env`` into the process environment and re-run
    ``Settings.__init__`` on the module singleton in place.

    This is how a post-setup or post-write configuration becomes live in
    an already-running process: every module that imported ``settings``
    keeps the same object, so no re-import is needed. ``load_dotenv`` is
    called with ``override=True`` because ``dotenv.set_key`` (used by the
    config store) never touches ``os.environ``.
    """
    load_dotenv(env_file, override=True)
    settings.__init__()
    return settings