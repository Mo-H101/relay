"""
``relay keys`` subcommands: Relay API-key management on the KeyStore.

The raw key is printed exactly once across the entire CLI surface — only
by ``keys add`` — and only the opaque key id (a uuid, not secret) is shown
by ``list``/``remove``. ``keys test`` verifies a token without ever
echoing it, on success or failure. No logging is added here; exceptions
are surfaced as short messages without values.
"""

from __future__ import annotations

import getpass
import json
import sys
import time
from datetime import datetime, timezone

from app.services.key_store import KeyStore


def add_keys_parser(parser) -> None:
    """
    Attach the ``list``/``add``/``remove``/``test`` subparsers to a
    ``relay keys`` parser.
    """
    sub = parser.add_subparsers(dest="keys_command")

    p = sub.add_parser("list", help="List stored API keys (metadata only).")
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON with full ids.",
    )

    p = sub.add_parser(
        "add",
        help="Create a new API key. The raw key is shown exactly once.",
    )
    p.add_argument("--label", required=True, help="Human-readable label.")
    p.add_argument(
        "--scopes",
        default="",
        help="Comma-separated scopes, e.g. 'chat,v1'.",
    )
    p.add_argument(
        "--expires-days",
        type=int,
        default=None,
        help="Days until the key expires (default: no expiry).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON including the raw key.",
    )

    p = sub.add_parser("remove", help="Revoke an API key.")
    p.add_argument("key_id", help="Full or shortened key id.")
    p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm revocation non-interactively.",
    )

    p = sub.add_parser(
        "test",
        help="Verify a key against the store. Never echoes the key.",
    )
    p.add_argument(
        "token",
        nargs="?",
        default=None,
        help="Key to test; '-' reads it from stdin; omit for a hidden "
             "prompt on interactive terminals.",
    )


def _store() -> KeyStore:
    """
    Construct the store at the default location, creating the state
    directory first. Module-level hook so tests can inject a temp-path
    store.
    """
    try:
        from app.core.config import state_dir

        state_dir.mkdir(parents=True, exist_ok=True)
        return KeyStore()
    except Exception as exc:  # noqa: BLE001 - surface short, never the value
        _fail("could not open key store", exc)


def _parse_scopes(raw: str) -> list:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _iso(ts) -> str:
    """Render a unix timestamp as ISO 8601 UTC, or ``-`` when absent."""
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _short(key_id: str) -> str:
    return key_id[:8]


def _json_meta(meta: dict) -> dict:
    """Serialize one key's metadata for ``--json`` output (times ISO or None)."""
    return {
        "id": meta["id"],
        "label": meta["label"],
        "scopes": meta["scopes"],
        "expires_at": _iso(meta["expires_at"]),
        "created_at": _iso(meta["created_at"]),
        "last_used_at": _iso(meta["last_used_at"]),
        "revoked_at": _iso(meta["revoked_at"]),
    }


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


def _obtain_token(args, parser) -> str:
    """
    Resolve the token for ``keys test``: positional ``<key>``, ``-`` for
    stdin, or a hidden getpass prompt on interactive terminals. The token
    is never echoed and never printed.
    """
    token = args.token or ""

    if token == "-":
        token = _read_stdin(parser)

    if not token:
        if sys.stdin.isatty():
            token = getpass.getpass("API key: ")
        else:
            parser.error("a key is required (positional or '-' for stdin)")

    return token.strip()


def _run_keys(args, parser) -> None:
    """Dispatch one ``relay keys`` subcommand."""
    if args.keys_command == "list":
        _cmd_keys_list(args)
    elif args.keys_command == "add":
        _cmd_keys_add(args, parser)
    elif args.keys_command == "remove":
        _cmd_keys_remove(args, parser)
    elif args.keys_command == "test":
        _cmd_keys_test(args, parser)
    else:
        parser.print_help()


def _cmd_keys_list(args) -> None:
    """``relay keys list``: one line per key, metadata only."""
    entries = _store().list()

    if args.json:
        print(json.dumps([_json_meta(entry) for entry in entries], indent=2))
        return

    for entry in entries:
        print(
            f"{_short(entry['id'])}  {entry['label']}  "
            f"{','.join(entry['scopes']) or '-'}  {_iso(entry['expires_at'])}  "
            f"{_iso(entry['created_at'])}  {_iso(entry['last_used_at'])}  "
            f"{_iso(entry['revoked_at'])}"
        )


def _cmd_keys_add(args, parser) -> None:
    """
    ``relay keys add``: create a key. The raw key is printed exactly once
    here (and in ``--json`` mode), then never again by the CLI.
    """
    label = (args.label or "").strip()

    if not label:
        parser.error("--label is required")

    scopes = _parse_scopes(args.scopes)
    expires_at = None

    if args.expires_days is not None:
        if args.expires_days <= 0:
            parser.error("--expires-days must be a positive number of days")
        expires_at = time.time() + args.expires_days * 86400

    try:
        key_id, raw_key = _store().create(label, scopes=scopes, expires_at=expires_at)
    except Exception as exc:  # noqa: BLE001 - surface short, never the value
        _fail("could not create key", exc)

    if args.json:
        print(
            json.dumps(
                {
                    "key_id": key_id,
                    "label": label,
                    "scopes": scopes,
                    "expires_at": _iso(expires_at),
                    "api_key": raw_key,
                },
                indent=2,
            )
        )
        return

    print(f"Key ID: {key_id}")
    print(f"Label: {label}")
    print(f"Scopes: {','.join(scopes) or '-'}")
    print(f"Expires: {_iso(expires_at)}")
    print("---")
    print(f"API Key: {raw_key}")
    print("Shown once — store it now.")


def _resolve_key_id(store: KeyStore, key_id: str):
    """
    Resolve a full or unique-shortened key id to its metadata, or None.
    """
    meta = store.get_by_id(key_id)

    if meta is not None:
        return meta

    matches = [entry for entry in store.list() if entry["id"].startswith(key_id)]

    if len(matches) == 1:
        return matches[0]

    return None


def _cmd_keys_remove(args, parser) -> None:
    """
    ``relay keys remove``: revoke (soft-delete) a key. Interactive
    terminals confirm with a y/N prompt; non-interactive runs require
    ``--yes`` and are refused otherwise.
    """
    key_id = args.key_id.strip()
    store = _store()
    meta = _resolve_key_id(store, key_id)

    if meta is None:
        _fail(f"unknown key {_short(key_id)}")

    if not args.yes:
        if sys.stdin.isatty():
            confirm = input(f"Revoke key {_short(meta['id'])}? [y/N] ")
            if confirm.strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return
        else:
            print(
                f"Refusing to revoke {_short(meta['id'])}: pass --yes to "
                "confirm non-interactively.",
                file=sys.stderr,
            )
            raise SystemExit(1)

    if meta["revoked_at"] is not None:
        print(f"Already revoked {_short(meta['id'])}")
        return

    store.revoke(meta["id"])
    print(f"Revoked {_short(meta['id'])}")


def _cmd_keys_test(args, parser) -> None:
    """``relay keys test``: report ok/invalid/expired/revoked, never the key."""
    token = _obtain_token(args, parser)

    try:
        result = _store().classify(token)
    except Exception as exc:  # noqa: BLE001 - surface short, never the value
        _fail("could not test key", exc)

    if result["status"] == "ok":
        print(f"ok {result['meta']['label']}")
        return

    print(result["status"])


def _fail(message: str, exc: Exception | None = None) -> None:
    """Print a short error to stderr (no values) and exit 1."""
    if exc is not None:
        print(f"{message}: {exc.__class__.__name__}", file=sys.stderr)
    else:
        print(message, file=sys.stderr)

    raise SystemExit(1)
