"""
Controlled configuration mutation (P7.2).

The orchestration layer behind ``relay config set/unset/reload``.

Every write validates against the registry before persisting, routes
provider keys through ``config_store.set_provider_config`` (so the
``RELAY_KEYRING`` boundary is honored), applies live changes through
``reload_settings`` (the CLI-safe apply path — no ``app.core.relay``
import, no network), and rolls the file back to the previous value on a
failed reload. Report dictionaries never contain values: secrets are
represented by masked display strings in dry-run previews and by field
name only everywhere else.
"""

from __future__ import annotations

from app.core.config import Settings, settings
from app.core.config_spec import (
    SPEC_BY_ATTR,
    SPEC_BY_ENV,
    reloadable_fields,
    validate_value,
)
from app.providers.factory import resolve_provider_key
from app.providers.registry import PROVIDER_REGISTRY
from app.services import config_store
from app.services.reload import _env_overlay, _redact
from app.setup.key_validation import mask_key

# Sentinel: "no value was present" (as opposed to an empty string).
_ABSENT = object()


class ConfigUsageError(ValueError):
    """
    Refused operation: unknown env var, non-CLI-visible field, or an
    invalid value. Maps to exit code 2 and never writes.
    """


class ConfigMutationError(ValueError):
    """
    Operational failure after (or while) writing: persistence failure or a
    reload failure that rolled the file back. Maps to exit code 1.
    """


def _apply_settings_reload(dotenv_path: str) -> None:
    """
    CLI-safe settings reload: validate a fresh ``Settings`` against the
    file, then apply the reloadable allowlist to the singleton.

    This mirrors ``reload.py``'s diff/apply semantics for the settings
    layer (the exact intent of "``reload_settings`` is the CLI apply path")
    without importing ``app.core.relay`` or performing any network I/O. A
    fresh ``Settings`` validates the whole file first, so an invalid file
    raises with the singleton untouched, and a key removed from the file
    correctly reverts the singleton to its default (plain
    ``load_dotenv(override=True)`` could not clear a removed key from
    ``os.environ``).
    """
    with _env_overlay(dotenv_path):
        candidate = Settings()

    for field in reloadable_fields():
        if hasattr(candidate, field):
            setattr(settings, field, getattr(candidate, field))


def _reload_failure(
    env: str, provider_defn, original: object, exc: Exception | None = None
) -> ConfigMutationError:
    """Build the rollback error after a failed reload (redacted reason)."""
    _restore(env, provider_defn, original)
    reason = f": {_redact(exc)}" if exc is not None else ""
    err = ConfigMutationError(
        f"Reload failed for '{env}'{reason}; the previous value was restored."
    )
    err.restored = True
    return err


def _provider_def_for_key_env(env: str):
    """
    The provider whose ``key_env`` is ``env``, or ``None``. Only provider
    key env vars route through ``set_provider_config``.
    """
    for defn in PROVIDER_REGISTRY.values():
        if defn.key_env == env:
            return defn

    return None


def _spec_or_fail(env: str):
    """
    Resolve a settable spec by env var name. Unknown, informational
    (env-less), and non-CLI-visible fields are refused.
    """
    spec = SPEC_BY_ENV.get(env)

    if spec is None:
        attr_spec = SPEC_BY_ATTR.get(env)

        if attr_spec is not None and attr_spec.env is None:
            raise ConfigUsageError(f"'{env}' cannot be set.")

        raise ConfigUsageError(f"Unknown setting '{env}'.")

    if not spec.cli_visible:
        raise ConfigUsageError(f"'{env}' is not settable from the CLI.")

    return spec


def _capture_original(env: str, spec, provider_defn) -> object:
    """
    Snapshot the persisted value we are about to overwrite, so a failed
    reload can restore it. Provider keys resolve from the keyring when
    enabled (the storage ``set_provider_config`` actually wrote to).
    """
    if provider_defn is not None:
        if getattr(settings, "relay_keyring_enabled", False):
            value = resolve_provider_key(provider_defn)
        else:
            value = config_store.get_env(env, None)

        return _ABSENT if not value else value

    value = config_store.get_env(env, None)
    return _ABSENT if value is None else value


def _persist(env: str, raw: str, provider_defn) -> None:
    """Write through the single writer; provider keys keep keyring routing."""
    if provider_defn is not None:
        config_store.set_provider_config(provider_defn, api_key=raw)
    else:
        config_store.set_env(env, raw)


def _restore(env: str, provider_defn, original: object) -> None:
    """Put ``original`` back (or remove the key when nothing was there)."""
    if original is _ABSENT:
        if provider_defn is not None:
            config_store.set_provider_config(provider_defn, api_key="")
        else:
            config_store.unset_env(env)
    else:
        _persist(env, original, provider_defn)


def _old_display(env: str, spec, provider_defn) -> str:
    """Display-safe current value: masked for secrets, else the raw file value."""
    if provider_defn is not None:
        value = resolve_provider_key(provider_defn)
        present = bool(value)
    else:
        value = config_store.get_env(env, None)
        present = value is not None

    if not present:
        return "(unset)"

    return mask_key(value) if spec.secret else value


def _new_display(spec, raw: str) -> str:
    """Display-safe new value: masked for secrets, else the raw value."""
    return mask_key(raw) if spec.secret else raw


def _unset_display(spec) -> str:
    """What ``unset`` produces: the default value, or ``(default)`` when secret."""
    if spec.secret:
        return "(default)"

    if spec.default is None:
        return "(default)"

    return str(spec.default)


def set_setting(env: str, raw: str, *, reload: bool = True, dry_run: bool = False) -> dict:
    """
    Validate and persist one setting.

    Returns a report dict with no raw values: ``{saved, env, effect,
    reloaded, applied, restored}`` plus an ``old``/``new`` masked preview
    when ``dry_run``. Raises ``ConfigUsageError`` (never writes) for
    unknown/refused/invalid input, and ``ConfigMutationError`` for a write
    or reload failure (the file is restored on the latter).
    """
    spec = _spec_or_fail(env)

    try:
        validate_value(spec, raw)
    except ValueError as exc:
        raise ConfigUsageError(_redact(exc)) from None

    provider_defn = _provider_def_for_key_env(env)

    if dry_run:
        return {
            "saved": False,
            "dry_run": True,
            "env": env,
            "effect": spec.effect,
            "would_reload": bool(reload) and spec.reloadable,
            "old": _old_display(env, spec, provider_defn),
            "new": _new_display(spec, raw),
        }

    original = _capture_original(env, spec, provider_defn)

    try:
        _persist(env, raw, provider_defn)
    except Exception as exc:  # noqa: BLE001 - surface class only, never the value
        raise ConfigMutationError(
            f"Could not write '{env}': {exc.__class__.__name__}"
        ) from None

    if reload and spec.reloadable:
        try:
            _apply_settings_reload(str(config_store.env_file))
        except ValueError as exc:
            raise _reload_failure(env, provider_defn, original, exc) from None

        return {
            "saved": True,
            "env": env,
            "effect": spec.effect,
            "reloaded": True,
            "applied": True,
            "restored": False,
        }

    return {
        "saved": True,
        "env": env,
        "effect": spec.effect,
        "reloaded": False,
        "applied": False,
        "restored": False,
    }


def unset_setting(env: str, *, reload: bool = True, dry_run: bool = False) -> dict:
    """
    Remove one setting (restores the default on the next load).

    Mirrors ``set_setting`` with removal semantics. Raises
    ``ConfigUsageError`` for unknown/refused input and ``ConfigMutationError``
    for a write or reload failure.
    """
    spec = _spec_or_fail(env)
    provider_defn = _provider_def_for_key_env(env)

    if dry_run:
        return {
            "saved": False,
            "dry_run": True,
            "env": env,
            "effect": spec.effect,
            "would_reload": bool(reload) and spec.reloadable,
            "old": _old_display(env, spec, provider_defn),
            "new": _unset_display(spec),
        }

    original = _capture_original(env, spec, provider_defn)

    try:
        if provider_defn is not None:
            config_store.set_provider_config(provider_defn, api_key="")
        else:
            config_store.unset_env(env)
    except Exception as exc:  # noqa: BLE001 - surface class only, never the value
        raise ConfigMutationError(
            f"Could not clear '{env}': {exc.__class__.__name__}"
        ) from None

    if reload and spec.reloadable:
        try:
            _apply_settings_reload(str(config_store.env_file))
        except ValueError as exc:
            raise _reload_failure(env, provider_defn, original, exc) from None

        return {
            "saved": True,
            "env": env,
            "effect": spec.effect,
            "reloaded": True,
            "applied": True,
            "restored": False,
        }

    return {
        "saved": True,
        "env": env,
        "effect": spec.effect,
        "reloaded": False,
        "applied": False,
        "restored": False,
    }


def _diff_applied(fields, candidate) -> tuple[list[str], list[str]]:
    """Field names (never values) where the candidate differs from the singleton."""
    applied: list[str] = []
    unchanged: list[str] = []

    for field in fields:
        if not hasattr(settings, field) or not hasattr(candidate, field):
            continue

        if getattr(settings, field) != getattr(candidate, field):
            applied.append(field)
        else:
            unchanged.append(field)

    return sorted(applied), sorted(unchanged)


def reload_settings_report(*, dry_run: bool = False) -> dict:
    """
    Re-read the active ``.env`` in-process and report applied/unchanged
    reloadable field names (never values).

    ``dry_run`` validates a fresh ``Settings`` against the file without
    mutating the singleton. The real path guards the singleton: a failed
    parse restores the prior attribute snapshot before raising.
    """
    fields = reloadable_fields()

    if dry_run:
        try:
            with _env_overlay(str(config_store.env_file)):
                candidate = Settings()
        except ValueError as exc:
            raise ValueError(_redact(exc)) from None

        applied, unchanged = _diff_applied(fields, candidate)
        return {
            "reloaded": False,
            "dry_run": True,
            "applied": applied,
            "unchanged": unchanged,
        }

    before = {field: getattr(settings, field) for field in fields if hasattr(settings, field)}

    try:
        _apply_settings_reload(str(config_store.env_file))
    except ValueError as exc:
        raise ValueError(_redact(exc)) from None

    applied = sorted(
        field for field in before
        if getattr(settings, field) != before[field]
    )
    unchanged = sorted(
        field for field in before
        if getattr(settings, field) == before[field]
    )
    return {
        "reloaded": True,
        "dry_run": False,
        "applied": applied,
        "unchanged": unchanged,
    }
