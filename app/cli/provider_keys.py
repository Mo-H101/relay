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

from app.providers.registry import PROVIDER_REGISTRY
from app.services import config_store
from app.setup.key_validation import mask_key


def add_provider_keys_parser(parser) -> None:
    """
    Attach the ``list``/``set``/``remove`` subparsers to a
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

    p = sub.add_parser(
        "remove",
        help="Clear a provider key. Idempotent; never echoed.",
    )
    p.add_argument("provider_id", help="Provider id (e.g. 'nvidia').")


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

    try:
        config_store.set_provider_config(defn, api_key=value)
    except Exception as exc:  # noqa: BLE001 - surface short, never the value
        _fail("could not store provider key", exc)

    print(f"Stored key for {defn.id}")


def _cmd_provider_keys_remove(args, parser) -> None:
    """``relay provider keys remove``: clear a key through the single writer."""
    defn = _provider_or_fail(args.provider_id)
    _require_key_capable(defn, parser)

    try:
        config_store.set_provider_config(defn, api_key="")
    except Exception as exc:  # noqa: BLE001 - surface short, never the value
        _fail("could not remove provider key", exc)

    print(f"Removed key for {defn.id}")


def _fail(message: str, exc: Exception | None = None) -> None:
    """Print a short error to stderr (no values) and exit 1."""
    if exc is not None:
        print(f"{message}: {exc.__class__.__name__}", file=sys.stderr)
    else:
        print(message, file=sys.stderr)

    raise SystemExit(1)
