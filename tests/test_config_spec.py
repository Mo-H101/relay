"""
Golden/coverage tests for the P7.1 settings registry.

These tests lock ``app/core/config_spec.py`` to the code that actually runs:
every ``Settings`` attribute has exactly one spec entry, the derived reload
allowlist / secret set / provider triplets reproduce ``app.services.reload``
byte-for-byte (names **and order**), the TUI Configuration rows reproduce
``app.ui.data._CONFIG_ROWS``, and the validation helpers reuse the same
bounds ``Settings.__init__`` enforces. Any drift fails here first, so new
settings must be added to the registry (and these tests) rather than to a
hand-maintained list.
"""

import pytest

from app.core.config import Settings, settings
from app.core.config_spec import (
    SPECS,
    SPEC_BY_ATTR,
    SPEC_BY_ENV,
    INFO,
    LIVE,
    RESTART,
    parse_value,
    render_value,
    reloadable_fields,
    reload_secret_fields,
    secret_fields,
    simple_reloadable_fields,
    tui_fields,
    validate_value,
)
from app.providers.registry import PROVIDER_REGISTRY, RUNTIME_READY


def test_spec_covers_every_settings_attribute():
    expected = set(vars(Settings()))
    assert set(SPEC_BY_ATTR) == expected
    assert len(SPECS) == len(expected) == 103


def test_every_spec_env_is_unique():
    envs = [spec.env for spec in SPECS if spec.env is not None]
    assert len(envs) == len(set(envs))


def test_every_spec_is_classified():
    for spec in SPECS:
        assert spec.effect in (LIVE, RESTART, INFO)
        assert spec.reloadable == (spec.effect == LIVE)
        assert spec.restart_required == (spec.effect == RESTART)
        assert spec.informational == (spec.effect == INFO)


# Frozen pre-P7 hand-maintained allowlist from app/services/reload.py (order
# is significant): the registry must reproduce these byte-for-byte.
_GOLDEN_SIMPLE = (
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
    "relay_auth_store",
)


def _golden_secret() -> tuple:
    return ("relay_api_key",) + tuple(
        defn.key_attr
        for defn in PROVIDER_REGISTRY.values()
        if defn.id in RUNTIME_READY and defn.key_attr
    )


def _golden_reloadable() -> tuple:
    provider_triplets = tuple(
        f"{defn.id}_{suffix}"
        for defn in PROVIDER_REGISTRY.values()
        if defn.id in RUNTIME_READY
        for suffix in ("enabled", "api_key", "model_priority")
    )
    return _GOLDEN_SIMPLE + _golden_secret() + provider_triplets


def test_simple_reloadable_fields_match_golden_allowlist():
    assert tuple(simple_reloadable_fields()) == _GOLDEN_SIMPLE


def test_reload_secret_fields_match_golden_secret_set():
    assert tuple(reload_secret_fields()) == _golden_secret()


def test_reloadable_fields_match_golden_full_allowlist():
    assert tuple(reloadable_fields()) == _golden_reloadable()


def test_reload_module_still_consumes_the_registry():
    # Migration guard: reload.py must keep deriving its allowlist from the
    # registry; if it ever regresses to hand-maintained literals this fails.
    import app.services.reload as reload_module

    assert tuple(reload_module._RELOADABLE_FIELDS) == tuple(reloadable_fields())
    assert tuple(reload_module._SIMPLE_FIELDS) == _GOLDEN_SIMPLE
    assert tuple(reload_module._SECRET_FIELDS) == _golden_secret()


def test_every_reload_secret_is_marked_secret():
    assert set(reload_secret_fields()) <= set(secret_fields())


def test_secret_fields_are_reported_and_masked():
    # The CLI masks everything in secret_fields(); the reload engine
    # additionally only ever reports secrets by name. A secret must never be
    # tui-visible and never a plain editable config row.
    for spec in SPECS:
        if spec.secret:
            assert not spec.tui
            assert not spec.tui_editable


def test_provider_fields_follow_prefix_suffix_convention():
    for defn in PROVIDER_REGISTRY.values():
        if defn.id not in RUNTIME_READY:
            continue

        for suffix in ("enabled", "api_key", "model_priority"):
            field = f"{defn.id}_{suffix}"
            assert field in reloadable_fields(), field

            if field not in vars(Settings()):
                # ollama_api_key is the documented exception: reload lists it
                # (matching the pre-P7 tuple) but Settings defines no such
                # attribute because Ollama is keyless.
                assert defn.id == "ollama" and suffix == "api_key"
                continue

            spec = SPEC_BY_ATTR[field]
            assert spec.provider
            assert spec.reloadable


def test_non_reloadable_fields_are_never_live():
    non_reloadable = sorted(
        set(vars(Settings())) - set(reloadable_fields())
    )
    assert non_reloadable == sorted(
        {
            "anthropic_base_url",
            "gemini_base_url",
            "groq_api_key",
            "health_deep_refresh_enabled",
            "health_refresh_enabled",
            "health_refresh_interval_seconds",
            "lmstudio_base_url",
            "lmstudio_priority",
            "log_file",
            "log_level",
            "ollama_base_url",
            "openrouter_api_key",
            "persistence_enabled",
            "persistence_flush_interval_seconds",
            "persistence_path",
            "relay_host",
            "relay_keyring_backend",
            "relay_keyring_enabled",
            "relay_name",
            "relay_port",
            "relay_tui_no_embed",
            "request_log_flush_interval_seconds",
            "request_log_retention_days",
            "telemetry_max_failure_history",
        }
    )


def test_attributed_tuning_groups_stay_reloadable():
    # The retry / health / ops / quality / scoring / task / decision /
    # adaptive / request-timing fields must never be pulled out of the live
    # reload allowlist (P6.5 semantics).
    must_be_live = (
        "request_timeout",
        "max_retries",
        "health_ttl_seconds",
        "ops_window_seconds",
        "quality_feedback_enabled",
        "scoring_priority_weight",
        "task_routing_enabled",
        "decision_engine_enabled",
        "adaptive_learning_rate",
    )
    live = set(reloadable_fields())
    for attr in must_be_live:
        assert attr in live, attr


def test_tui_rows_reproduce_ui_config_rows():
    from app.ui.data import _CONFIG_ROWS

    rows = {row[0]: row for row in _CONFIG_ROWS}

    assert len(tui_fields()) == len(rows) == 23

    for env, row in rows.items():
        _, attr, kind, group, editable, reloadable, restart, info, label, hint = row
        spec = SPEC_BY_ENV[env]

        assert spec.attr == attr
        assert spec.tui
        assert spec.tui_kind == kind
        assert spec.tui_group == group
        assert spec.tui_editable == editable
        assert spec.reloadable is reloadable
        assert spec.restart_required is restart
        assert spec.informational is info
        assert spec.label == label
        assert spec.hint == hint
        assert not spec.secret


def test_tui_fields_are_exactly_the_config_rows():
    from app.ui.data import _CONFIG_ROWS

    row_envs = {row[0] for row in _CONFIG_ROWS}
    assert {spec.env for spec in tui_fields()} == row_envs


def test_no_tui_field_is_secret():
    assert not any(spec.secret for spec in tui_fields())


def test_cli_visibility_only_for_env_backed_settings():
    for spec in SPECS:
        if spec.env is None:
            assert not spec.cli_visible
        else:
            assert spec.cli_visible


def test_defaults_validate_clean():
    for spec in SPECS:
        if spec.type not in ("int", "float", "url"):
            continue
        validate_value(spec, render_value(spec, spec.default))


@pytest.mark.parametrize(
    "env,raw,attr,expected",
    [
        ("REQUEST_TIMEOUT", "45", "request_timeout", 45),
        ("MAX_RETRIES", "3", "max_retries", 3),
        ("RETRY_HONOR_RETRY_AFTER", "true", "retry_honor_retry_after", True),
        ("TASK_CODING", "a, b", "task_coding", ["a", "b"]),
        ("HEALTH_FRESHNESS_EXPONENT", "0.5", "health_freshness_exponent", 0.5),
        ("LMSTUDIO_BASE_URL", "http://localhost:9999/v1/",
         "lmstudio_base_url", "http://localhost:9999/v1"),
        ("OLLAMA_BASE_URL", "http://localhost:11434", "ollama_base_url",
         "http://localhost:11434"),
        ("LOG_LEVEL", "DEBUG", "log_level", "DEBUG"),
    ],
)
def test_parse_value_matches_settings(monkeypatch, env, raw, attr, expected):
    spec = SPEC_BY_ENV[env]
    monkeypatch.setenv(env, raw)
    assert parse_value(spec, raw) == expected
    assert getattr(Settings(), attr) == expected


@pytest.mark.parametrize(
    "env,raw",
    [
        ("REQUEST_TIMEOUT", "abc"),
        ("MAX_RETRIES", "-1"),
        ("HEALTH_FRESHNESS_EXPONENT", "nan"),
        ("SCORING_PRIORITY_DENOM", "0"),
        ("LMSTUDIO_BASE_URL", "ftp://bad"),
        ("RELAY_PORT", "not-a-port"),
    ],
)
def test_validate_value_rejects_like_settings(monkeypatch, env, raw):
    spec = SPEC_BY_ENV[env]
    monkeypatch.setenv(env, raw)

    with pytest.raises(ValueError) as spec_err:
        validate_value(spec, raw)
    with pytest.raises(ValueError) as settings_err:
        Settings()

    assert f"Invalid value for {env}:" in str(spec_err.value)
    assert f"Invalid value for {env}:" in str(settings_err.value)


def test_every_spec_has_a_nonempty_description_and_category():
    for spec in SPECS:
        assert spec.description, spec.attr
        assert spec.category, spec.attr


def test_settings_singleton_attributes_all_covered():
    for attr in vars(settings):
        assert attr in SPEC_BY_ATTR, attr
