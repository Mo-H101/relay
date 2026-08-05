"""
``relay provider keys`` subcommands: upstream provider-key management.

Writes route through ``config_store.set_provider_config`` — the single
writer of provider configuration — so the P5 Phase 2 flag behavior is
preserved: keys go to the OS keyring when ``RELAY_KEYRING`` is on and to
``.env`` otherwise. Values are never echoed: ``list`` masks them and
``set``/``remove`` print nothing about the value.
"""

from __future__ import annotations

import getpass
import json
import sys

from app.core.config import settings
from app.providers.registry import PROVIDER_REGISTRY
from app.services import config_store
from app.setup.key_validation import mask_key


def add_migrate_parser(sub) -> None:
    """
    Attach the ``migrate`` subparser. Shared by ``relay provider keys``
    (canonical home) and the ``relay keys provider`` alias so both
    spellings accept the same flags.
    """
    p = sub.add_parser(
        "migrate",
        help=(
            "Move cloud-provider keys from .env into the OS keyring. "
            "Never prints secrets."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and exit without changing anything.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite a conflicting keyring entry with the .env value.",
    )
    p.add_argument(
        "--provider",
        default=None,
        help="Restrict the migration to one provider id (e.g. 'nvidia').",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm non-interactively (required when stdin is not a TTY).",
    )


def add_provider_keys_parser(parser) -> None:
    """
    Attach the ``list``/``set``/``remove``/``migrate`` subparsers to a
    ``relay provider keys`` parser.
    """
    sub = parser.add_subparsers(dest="provider_keys_command")

    p = sub.add_parser(
        "list",
        help="List cloud-provider keys (values always masked).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (no raw material).",
    )

    p = sub.add_parser(
        "set",
        help="Store a provider key. Never echoed.",
    )
    p.add_argument("provider_id", help="Provider id (e.g. 'nvidia').")
    p.add_argument(
        "key",
        nargs="?",
        default=None,
        help="Key value; '-' reads it from stdin; omit for a hidden "
             "prompt on interactive terminals.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm non-interactively (required when stdin is not a TTY).",
    )

    p = sub.add_parser(
        "remove",
        help="Clear a provider key. Idempotent; never echoed.",
    )
    p.add_argument("provider_id", help="Provider id (e.g. 'nvidia').")
    p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm non-interactively (required when stdin is not a TTY).",
    )

    add_migrate_parser(sub)


def _run_provider_keys(args, parser) -> None:
    """Dispatch one ``relay provider keys`` subcommand."""
    if args.provider_command != "keys":
        parser.print_help()
        return

    if args.provider_keys_command == "list":
        _cmd_provider_keys_list(args)
    elif args.provider_keys_command == "set":
        _cmd_provider_keys_set(args, parser)
    elif args.provider_keys_command == "remove":
        _cmd_provider_keys_remove(args, parser)
    elif args.provider_keys_command == "migrate":
        _cmd_provider_keys_migrate(args, parser)
    else:
        parser.print_help()


def _provider_or_fail(provider_id: str):
    """
    Look up a provider definition, or exit 1 with a message for an
    unknown id.
    """
    defn = PROVIDER_REGISTRY.get(provider_id)

    if defn is None:
        print(f"Unknown provider '{provider_id}'.", file=sys.stderr)
        raise SystemExit(1)

    return defn


def _require_key_capable(defn, parser) -> None:
    """Keyless providers (``key_attr`` None) have no key concept."""
    if not defn.key_attr:
        parser.error(f"{defn.id} has no API key concept (keyless provider).")


def _read_stdin(parser) -> str:
    """
    Read the first non-empty line of stdin as a key. Empty input is a
    usage error (exit 2).
    """
    for line in sys.stdin:
        value = line.strip()

        if value:
            return value

    parser.error("no key provided on stdin")


def _resolve_key_value(args, parser, defn) -> str:
    """
    Resolve the key for ``set``: positional ``<key>``, ``-`` for stdin, or
    a hidden getpass prompt on interactive terminals. Never echoed.
    """
    value = args.key or ""

    if value == "-":
        value = _read_stdin(parser)

    if not value:
        if sys.stdin.isatty():
            value = getpass.getpass(f"API key for {defn.display_name}: ")
        else:
            parser.error(
                f"a key is required for {defn.id} "
                "(positional or '-' for stdin)"
            )

    value = value.strip()

    if not value:
        parser.error(
            f"a key is required for {defn.id} (positional or '-' for stdin)"
        )

    return value


def _cmd_provider_keys_list(args) -> None:
    """``relay provider keys list``: one masked line per cloud provider."""
    from app.providers.factory import resolve_provider_key

    rows = []

    for defn in PROVIDER_REGISTRY.values():
        if defn.kind != "cloud":
            continue

        key = resolve_provider_key(defn)

        rows.append(
            {
                "id": defn.id,
                "requires_key": defn.requires_api_key,
                "has_key": bool(key),
                "key": mask_key(key) if key else "-",
            }
        )

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    for row in rows:
        print(
            f"{row['id']}  {row['requires_key']}  {row['has_key']}  {row['key']}"
        )


def _cmd_provider_keys_set(args, parser) -> None:
    """``relay provider keys set``: store a key through the single writer."""
    defn = _provider_or_fail(args.provider_id)
    _require_key_capable(defn, parser)

    value = _resolve_key_value(args, parser, defn)

    _confirm_write(args, parser, f"Store {mask_key(value)} for {defn.id}?")

    try:
        config_store.set_provider_config(defn, api_key=value)
    except Exception as exc:  # noqa: BLE001 - surface short, never the value
        _emit("provider_key.set", target=defn.id, outcome="failed")
        _fail("could not store provider key", exc)

    _emit("provider_key.set", target=defn.id, outcome="ok")
    print(f"Stored key for {defn.id}")


def _cmd_provider_keys_remove(args, parser) -> None:
    """``relay provider keys remove``: clear a key through the single writer."""
    defn = _provider_or_fail(args.provider_id)
    _require_key_capable(defn, parser)

    _confirm_write(args, parser, f"Remove the stored key for {defn.id}?")

    try:
        config_store.set_provider_config(defn, api_key="")
    except Exception as exc:  # noqa: BLE001 - surface short, never the value
        _emit("provider_key.remove", target=defn.id, outcome="failed")
        _fail("could not remove provider key", exc)

    _emit("provider_key.remove", target=defn.id, outcome="ok")
    print(f"Removed key for {defn.id}")


def _confirm_write(args, parser, prompt: str) -> None:
    """
    Guard parity with ``relay migrate`` (Decision G): interactive
    terminals confirm with a y/N prompt; non-interactive runs require
    ``--yes`` and are refused otherwise.
    """
    if args.yes:
        return

    if sys.stdin.isatty():
        confirm = input(f"{prompt} [y/N] ")

        if confirm.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            raise SystemExit(0)

        return

    print(
        "Refusing to run non-interactively: pass --yes to confirm.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _cmd_provider_keys_migrate(args, parser) -> None:
    """
    ``relay provider keys migrate``: move cloud-provider keys from ``.env``
    into the OS keyring.

    Writes land first and ``.env`` entries are removed only after every
    write succeeds, so a keyring failure aborts with ``.env`` untouched.
    Values never leave ``get_env -> set`` in raw form; all display uses
    ``mask_key``.
    """
    from app.services.provider_key_store import provider_key_store

    if args.provider:
        if PROVIDER_REGISTRY.get(args.provider) is None:
            parser.error(f"unknown provider '{args.provider}'")

    providers = [
        defn
        for defn in PROVIDER_REGISTRY.values()
        if defn.kind == "cloud"
        and defn.key_attr
        and defn.key_env
        and (args.provider is None or defn.id == args.provider)
    ]

    if not providers:
        print("No cloud providers with a keyed API to migrate.")
        return

    if not getattr(settings, "relay_keyring_enabled", False):
        print(
            "warning: RELAY_KEYRING is not true; set it in .env before "
            "relying on migrated keys.",
            file=sys.stderr,
        )

    plan = []

    for defn in providers:
        env_value = config_store.get_env(defn.key_env)

        if not env_value:
            continue

        keyring_value = provider_key_store.get(defn.id)

        if keyring_value == env_value:
            status = "already"
        elif keyring_value:
            status = "conflict"
        else:
            status = "migrate"

        plan.append((defn, status, env_value, keyring_value))

    if args.dry_run:
        for defn, status, env_value, keyring_value in plan:
            if status == "migrate":
                print(
                    f"{defn.id:<10} migrate    env->keyring "
                    "(already stored: no | conflict: no)"
                )
            elif status == "already":
                print(
                    f"{defn.id:<10} already    env->keyring "
                    "(already stored: yes)"
                )
            else:
                print(
                    f"{defn.id:<10} conflict   env={mask_key(env_value)} "
                    f"keyring={mask_key(keyring_value)} "
                    "(--force to overwrite)"
                )
        return

    if not args.yes:
        if sys.stdin.isatty():
            confirm = input(
                f"Move {len(plan)} provider key(s) from .env into the "
                "OS keyring? [y/N] "
            )
            if confirm.strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return
        else:
            print(
                "Refusing to run non-interactively: pass --yes to confirm "
                "the migration.",
                file=sys.stderr,
            )
            raise SystemExit(1)

    # Write phase: every pending key into the keyring first. A failure
    # here aborts before any .env key is removed.
    moved = []
    skipped_conflicts = []

    for defn, status, env_value, keyring_value in plan:
        if status == "conflict" and not args.force:
            skipped_conflicts.append((defn, env_value, keyring_value))
            continue

        if status == "already":
            continue

        try:
            provider_key_store.set(defn.id, env_value)
        except Exception as exc:  # noqa: BLE001 - never surface the value
            _fail("could not write provider key to the keyring", exc)

        moved.append((defn, status, env_value))
        _emit(
            "provider_key.migrate",
            target=defn.id,
            outcome="ok",
            detail={"source": "env", "destination": "keyring"},
        )

    # Cleanup phase: only after every write succeeded.
    for defn, _status, _env_value in moved:
        try:
            config_store.unset_env(defn.key_env)
        except Exception as exc:  # noqa: BLE001 - surface short
            _fail("could not remove provider key from .env", exc)

    for defn, status, _env_value in moved:
        label = "migrated" if status == "migrate" else "overwritten"
        print(f"{defn.id:<10} {label}    env->keyring")

    for defn, env_value, keyring_value in skipped_conflicts:
        print(
            f"{defn.id}: conflict: env={mask_key(env_value)} "
            f"keyring={mask_key(keyring_value)}; use --force to overwrite",
            file=sys.stderr,
        )

    if skipped_conflicts:
        print(
            f"{len(moved)} provider(s) migrated, "
            f"{len(skipped_conflicts)} conflict(s) skipped; .env untouched "
            "for the conflicts.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if moved:
        print(
            f"Migrated {len(moved)} provider key(s) from .env into the "
            "OS keyring."
        )
    else:
        print("Nothing to migrate.")


def _fail(message: str, exc: Exception | None = None) -> None:
    """Print a short error to stderr (no values) and exit 1."""
    if exc is not None:
        print(f"{message}: {exc.__class__.__name__}", file=sys.stderr)
    else:
        print(message, file=sys.stderr)

    raise SystemExit(1)


def _emit(
    action: str,
    *,
    actor: str = "cli",
    target: str = "",
    outcome: str = "ok",
    detail: dict | None = None,
) -> None:
    """
    Best-effort security-event write from the CLI. A failed audit write
    never fails the command; it surfaces as a stderr warning so the
    operator knows the action was not durably recorded.
    """
    from app.services.event_log import event_log

    try:
        recorded = event_log().emit(
            action,
            actor=actor,
            target=target,
            outcome=outcome,
            detail=detail,
        )
    except Exception:  # noqa: BLE001 - audit failure must not crash the CLI
        recorded = False

    if not recorded:
        print(
            "warning: audit event not recorded",
            file=sys.stderr,
        )
