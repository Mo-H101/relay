"""
``relay config`` subcommands: ``show``, ``validate``, ``diff`` (P7.1,
read-only) and ``set``, ``unset``, ``reload`` (P7.2).

Reads are side-effect free: nothing here mutates ``.env``, the process
environment, or the in-process settings singleton. Secret fields are masked
with ``mask_key`` in ``show``/``diff`` and reduced to field-name-only errors
by ``reload._redact`` in ``validate``; no raw secret material is ever
printed.

Writes (P7.2) validate before persisting through ``config_store`` (the
single writer), route provider keys through ``set_provider_config`` so the
``RELAY_KEYRING`` boundary is honored, apply live changes with
``reload_settings`` (never importing ``app.core.relay``), roll the file
back on a failed reload, and emit ``config.set``/``config.unset``/
``config.reload`` audit events with counts only. ``--dry-run`` validates and
previews without touching the filesystem.
"""

from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path

from dotenv import dotenv_values

from app.cli import _require_config
from app.cli.provider_keys import _confirm_write, _emit, _read_stdin
from app.core.config import _env_parse_errors, settings
from app.core.config_spec import (
    SPECS,
    INFO,
    LIVE,
    RESTART,
    SPEC_BY_ENV,
    parse_value,
    render_value,
    validate_value,
)
from app.services import config_store
from app.services.reload import _redact
from app.setup.key_validation import mask_key


def add_config_parser(parser) -> None:
    """Attach the ``show``/``validate``/``diff``/``set``/``unset``/``reload`` subparsers."""
    sub = parser.add_subparsers(dest="config_command")

    show = sub.add_parser(
        "show",
        help="Show effective configuration; secret values are masked.",
    )
    show.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (secret values still masked).",
    )

    validate = sub.add_parser(
        "validate",
        help="Validate an env file, reporting every invalid value "
             "(field names only, never values).",
    )
    validate.add_argument(
        "--env-file",
        default=None,
        help="Env file to validate (default: the active .env).",
    )

    diff = sub.add_parser(
        "diff",
        help="Compare an env file against the running process, or two "
             "env files against each other.",
    )
    diff.add_argument(
        "--env-file",
        default=None,
        help="Env file to diff against the running process.",
    )
    diff.add_argument(
        "files",
        nargs="*",
        help="One or two env files to compare (two = file vs file).",
    )

    set_ = sub.add_parser(
        "set",
        help="Set one configuration value. Validates first; secrets are "
             "never echoed and provider keys route through the keyring "
             "when RELAY_KEYRING is on.",
    )
    set_.add_argument("env", help="Env var name (e.g. REQUEST_TIMEOUT).")
    set_.add_argument(
        "value",
        nargs="?",
        default=None,
        help="Value; '-' reads it from stdin; omit for a hidden prompt "
             "(secrets only).",
    )
    set_.add_argument(
        "--yes",
        action="store_true",
        help="Confirm non-interactively (required when stdin is not a TTY).",
    )
    set_.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and preview the change without writing anything.",
    )
    set_.add_argument(
        "--no-reload",
        action="store_true",
        help="Persist only; skip the in-process apply.",
    )
    set_.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable report (values still never included).",
    )

    unset = sub.add_parser(
        "unset",
        help="Remove one setting (restores the default on the next load).",
    )
    unset.add_argument("env", help="Env var name (e.g. REQUEST_TIMEOUT).")
    unset.add_argument(
        "--yes",
        action="store_true",
        help="Confirm non-interactively (required when stdin is not a TTY).",
    )
    unset.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the change without writing anything.",
    )
    unset.add_argument(
        "--no-reload",
        action="store_true",
        help="Persist only; skip the in-process apply.",
    )
    unset.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable report (values still never included).",
    )

    reload_ = sub.add_parser(
        "reload",
        help="Re-read the active .env into the process and report which "
             "live fields applied or stayed unchanged (field names only).",
    )
    reload_.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without mutating anything.",
    )
    reload_.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable report (field names only).",
    )


def _run_config(args, parser) -> None:
    """Dispatch one ``relay config`` subcommand."""
    if args.config_command == "show":
        _cmd_config_show(args)
    elif args.config_command == "validate":
        _cmd_config_validate(args, parser)
    elif args.config_command == "diff":
        _cmd_config_diff(args, parser)
    elif args.config_command == "set":
        _cmd_config_set(args, parser)
    elif args.config_command == "unset":
        _cmd_config_unset(args, parser)
    elif args.config_command == "reload":
        _cmd_config_reload(args, parser)
    else:
        parser.print_help()


def _active_env_file() -> Path:
    """The active ``.env`` (same file the single writer and runtime use)."""
    return Path(getattr(config_store, "env_file"))


# ------------------------------------------------------------------------- show

def _display_value(attr: str, spec) -> str:
    """Render one effective value; secrets are always masked."""
    value = getattr(settings, attr, None)

    if spec.secret:
        return mask_key(str(value)) if value else "(unset)"

    return render_value(spec, value)


def _cmd_config_show(args) -> None:
    """``relay config show``: every env-backed setting, masked secrets."""
    _require_config()

    rows = []

    for spec in SPECS:
        if spec.env is None or not spec.cli_visible:
            continue

        present = config_store.get_env(spec.env, None) is not None

        rows.append(
            {
                "env": spec.env,
                "attr": spec.attr,
                "type": spec.type,
                "effect": spec.effect,
                "category": spec.category,
                "secret": spec.secret,
                "source": "env" if present else "default",
                "value": _display_value(spec.attr, spec),
                "default": (
                    "(derived)" if spec.default is None
                    else "(hidden)" if spec.secret
                    else render_value(spec, spec.default)
                ),
            }
        )

    rows.sort(key=lambda row: row["env"])

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    width = max(len(row["env"]) for row in rows)
    print(f"{'ENV':<{width}}  {'TYPE':<5}  {'EFFECT':<8}  VALUE")

    for row in rows:
        print(
            f"{row['env']:<{width}}  {row['type']:<5}  "
            f"{row['effect']:<8}  {row['value']}"
        )


# --------------------------------------------------------------------- validate

def _validate_file(path: Path) -> list[str]:
    """
    Validate every present value in ``path`` against the registry. Returns a
    redacted error message (field name only) per invalid value, plus one
    message per unparseable line; never a raw value.
    """
    values = dotenv_values(str(path))
    errors: list[str] = []

    for problem in _env_parse_errors(path):
        if problem.startswith("line "):
            errors.append(f"{problem}: not a valid KEY=VALUE statement")
        else:
            errors.append(problem)

    for spec in SPECS:
        if spec.env is None:
            continue

        raw = values.get(spec.env)

        if raw is None:
            continue

        try:
            validate_value(spec, raw)
        except ValueError as exc:
            errors.append(_redact(exc))

    return errors


def _cmd_config_validate(args, parser) -> None:
    """``relay config validate``: all errors, exit 0 valid / 2 invalid."""
    target = Path(args.env_file) if args.env_file else _active_env_file()

    if not target.is_file():
        print(f"Config file not found: {target}", file=sys.stderr)
        raise SystemExit(1)

    errors = _validate_file(target)

    known = {spec.env for spec in SPECS if spec.env is not None}
    unknown = sorted(
        key
        for key, value in dotenv_values(str(target)).items()
        if value is not None and key not in known
    )

    if errors:
        print(f"Invalid values in {target}:", file=sys.stderr)

        for error in errors:
            print(f"  {error}", file=sys.stderr)

        if unknown:
            print(
                f"  note: {len(unknown)} unknown key(s) not in the spec: "
                f"{', '.join(unknown)}",
                file=sys.stderr,
            )

        raise SystemExit(2)

    checked = sum(
        1
        for spec in SPECS
        if spec.env is not None
        and dotenv_values(str(target)).get(spec.env) is not None
    )

    print(f"Config OK: {checked} value(s) validated in {target}.")

    if unknown:
        print(
            f"  note: {len(unknown)} unknown key(s) not in the spec: "
            f"{', '.join(unknown)}",
            file=sys.stderr,
        )


# ------------------------------------------------------------------------- diff

def _parse_side(spec, raw: str | None):
    """
    Parse a raw env value for comparison, or return ``None`` when absent and
    ``("invalid",)`` when the value does not parse.
    """
    if raw is None:
        return None

    try:
        return parse_value(spec, raw)
    except ValueError:
        return ("invalid",)


def _secret_side(spec, raw: str | None) -> str:
    """Display one side of a secret comparison by masked value only."""
    if raw is None:
        return "(absent)"
    return mask_key(raw) if raw else "(empty)"


def _display_side(spec, parsed, raw: str | None) -> str:
    if spec.secret:
        return _secret_side(spec, raw)
    if parsed == ("invalid",):
        return "(invalid)"
    return render_value(spec, parsed)


def _diff_field(rows_a, rows_b, spec, mode):
    """
    One spec's diff result: ``None`` (both absent) or a
    ``(status, detail)`` tuple.
    """
    raw_a = rows_a.get(spec.env) if rows_a is not None else None
    parsed_a = _parse_side(spec, raw_a)

    if mode == "file-file":
        raw_b = rows_b.get(spec.env) if rows_b is not None else None
        parsed_b = _parse_side(spec, raw_b)

        if parsed_a is None and parsed_b is None:
            return None

        if parsed_a is None:
            return ("missing", f"{spec.env} absent from {rows_b.get('__label__', '?')}")

        if parsed_b is None:
            return ("missing", f"{spec.env} absent from {rows_a.get('__label__', '?')}")

        if parsed_a == parsed_b:
            return ("unchanged", None)

        return (
            "changed",
            f"{spec.env}: {_display_side(spec, parsed_a, raw_a)} -> "
            f"{_display_side(spec, parsed_b, raw_b)}",
        )

    # mode == "file-process": compare the file against the running settings.
    process_value = getattr(settings, spec.attr, None)

    if parsed_a is None:
        if process_value == spec.default:
            # Absent from the file and the process runs the spec default:
            # adopting this file changes nothing for this field.
            return None

        current = (
            mask_key(str(process_value))
            if spec.secret
            else render_value(spec, process_value)
        )

        return (
            "missing",
            f"{spec.env} not set in {rows_a.get('__label__', '?')}; "
            f"process uses {current}",
        )

    if parsed_a == process_value:
        return ("unchanged", None)

    return (
        "changed",
        f"{spec.env}: {_display_side(spec, parsed_a, raw_a)} -> "
        f"{render_value(spec, process_value)}",
    )


def _print_diff(result) -> None:
    changed, unchanged, missing = result

    if not (changed or unchanged or missing):
        print("  (no configurable fields present in either side)")
        return

    if changed:
        print("CHANGED")

        for line in changed:
            print(f"  {line}")

    if unchanged:
        print(f"UNCHANGED ({len(unchanged)})")

        for env in unchanged:
            print(f"  {env}")

    if missing:
        print("MISSING")

        for line in missing:
            print(f"  {line}")


def _cmd_config_diff(args, parser) -> None:
    """``relay config diff``: file vs process, or two files."""
    if args.env_file and args.files:
        parser.error("--env-file and positional files are mutually exclusive")

    if len(args.files) > 2:
        parser.error("diff accepts at most two env files")

    env_specs = [spec for spec in SPECS if spec.env is not None]

    if len(args.files) == 2:
        path_a, path_b = Path(args.files[0]), Path(args.files[1])

        for path in (path_a, path_b):
            if not path.is_file():
                print(f"Config file not found: {path}", file=sys.stderr)
                raise SystemExit(1)

        rows_a = dotenv_values(str(path_a))
        rows_b = dotenv_values(str(path_b))
        rows_a["__label__"] = str(path_a)
        rows_b["__label__"] = str(path_b)

        print(f"diff {path_a} vs {path_b}")

        _print_diff(_collect_diff(rows_a, rows_b, env_specs, mode="file-file"))
        return

    # Comparing against the running process requires valid settings.
    _require_config()

    target = (
        Path(args.files[0])
        if args.files
        else Path(args.env_file)
        if args.env_file
        else _active_env_file()
    )

    if not target.is_file():
        print(f"Config file not found: {target}", file=sys.stderr)
        raise SystemExit(1)

    rows_file = dotenv_values(str(target))
    rows_file["__label__"] = str(target)
    rows_process = {"__label__": "running process"}

    print(f"diff {target} vs running process")

    _print_diff(
        _collect_diff(rows_file, rows_process, env_specs, mode="file-process")
    )


def _collect_diff(rows_a, rows_b, env_specs, mode):
    changed: list[str] = []
    unchanged: list[str] = []
    missing: list[str] = []

    for spec in env_specs:
        result = _diff_field(rows_a, rows_b, spec, mode)

        if result is None:
            continue

        status, detail = result

        if status == "changed":
            changed.append(detail)
        elif status == "unchanged":
            unchanged.append(spec.env)
        else:
            missing.append(detail)

    return changed, unchanged, missing


# -------------------------------------------------------------- set / unset

def _resolve_value(args, parser, env: str, spec) -> str:
    """
    Resolve a ``set`` value: positional, ``-`` for stdin, or a hidden
    getpass prompt for secret fields on interactive terminals. Never echoed.
    """
    value = args.value or ""

    if value == "-":
        value = _read_stdin(parser)

    if not value:
        if spec is not None and spec.secret and sys.stdin.isatty():
            value = getpass.getpass(f"{env}: ")
        else:
            parser.error(
                f"a value is required for {env} (positional or '-' for stdin)"
            )

    value = value.strip()

    if not value:
        parser.error(f"a value is required for {env} (positional or '-' for stdin)")

    return value


def _confirm_prompt(env: str, spec, value: str) -> str:
    """Confirmation prompt with a masked secret / plain value."""
    shown = mask_key(value) if spec is not None and spec.secret else value
    return f"Set {env}={shown}?"


def _print_mutation_report(report: dict, args) -> None:
    """Render a set/unset report; secrets are already masked in the dict."""
    if args.json:
        print(json.dumps(report, indent=2))
        return

    if report.get("dry_run"):
        print(f"{report['env']}: {report['old']} -> {report['new']}")
        print(
            f"effect={report['effect']}"
            + ("; would apply in-process" if report.get("would_reload") else "")
            + " (dry run, nothing written)"
        )
        return

    print(f"{report['env']} saved (effect={report['effect']}).")

    if report.get("reloaded"):
        print("Applied in-process.")
    elif report.get("effect") == RESTART:
        print("Restart required for this change to take effect.")

    if report.get("restored"):
        print("Reload failed; the previous value was restored.")


def _cmd_config_set(args, parser) -> None:
    """``relay config set <ENV> <VALUE>``: validate, confirm, write, apply."""
    from app.services import config_mutation

    spec = SPEC_BY_ENV.get(args.env)
    value = _resolve_value(args, parser, args.env, spec)

    if args.dry_run:
        report = config_mutation.set_setting(
            args.env, value, reload=not args.no_reload, dry_run=True
        )
        _print_mutation_report(report, args)
        return

    _confirm_write(args, parser, _confirm_prompt(args.env, spec, value))

    try:
        report = config_mutation.set_setting(
            args.env, value, reload=not args.no_reload
        )
    except config_mutation.ConfigUsageError as exc:
        _emit(
            "config.set",
            target=args.env,
            outcome="failed",
            detail={"reloaded": False, "restored": False},
        )
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except config_mutation.ConfigMutationError as exc:
        _emit(
            "config.set",
            target=args.env,
            outcome="failed",
            detail={
                "reloaded": False,
                "restored": bool(getattr(exc, "restored", False)),
            },
        )
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    _emit(
        "config.set",
        target=args.env,
        outcome="ok",
        detail={
            "reloaded": report.get("reloaded"),
            "restored": report.get("restored"),
        },
    )
    _print_mutation_report(report, args)


def _cmd_config_unset(args, parser) -> None:
    """``relay config unset <ENV>``: remove a key, restore the default."""
    from app.services import config_mutation

    if args.dry_run:
        report = config_mutation.unset_setting(
            args.env, reload=not args.no_reload, dry_run=True
        )
        _print_mutation_report(report, args)
        return

    _confirm_write(args, parser, f"Unset {args.env}?")

    try:
        report = config_mutation.unset_setting(
            args.env, reload=not args.no_reload
        )
    except config_mutation.ConfigUsageError as exc:
        _emit(
            "config.unset",
            target=args.env,
            outcome="failed",
            detail={"reloaded": False, "restored": False},
        )
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except config_mutation.ConfigMutationError as exc:
        _emit(
            "config.unset",
            target=args.env,
            outcome="failed",
            detail={
                "reloaded": False,
                "restored": bool(getattr(exc, "restored", False)),
            },
        )
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    _emit(
        "config.unset",
        target=args.env,
        outcome="ok",
        detail={
            "reloaded": report.get("reloaded"),
            "restored": report.get("restored"),
        },
    )
    _print_mutation_report(report, args)


def _cmd_config_reload(args, parser) -> None:
    """``relay config reload``: re-read .env in-process, report field names."""
    _require_config()

    from app.services import config_mutation

    try:
        report = config_mutation.reload_settings_report(dry_run=args.dry_run)
    except ValueError as exc:
        _emit("config.reload", target="", outcome="failed", detail={})
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    if not args.dry_run:
        _emit(
            "config.reload",
            target="",
            outcome="ok",
            detail={
                "applied": len(report["applied"]),
                "unchanged": len(report["unchanged"]),
            },
        )

    if args.json:
        print(json.dumps(report, indent=2))
        return

    print("Dry run: nothing changed." if args.dry_run else "Configuration reloaded.")
    print(
        f"applied ({len(report['applied'])}): "
        + (", ".join(report["applied"]) or "(none)")
    )
    print(
        f"unchanged ({len(report['unchanged'])}): "
        + (", ".join(report["unchanged"]) or "(none)")
    )
