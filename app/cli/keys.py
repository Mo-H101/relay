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

from app.services.key_store import KeyStore, _PRUNE_GRACE_DAYS


def add_keys_parser(parser) -> None:
    """
    Attach the ``list``/``add``/``remove``/``test``/``rotate``/``prune``
    subparsers to a ``relay keys`` parser.
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
        "rotate",
        help=(
            "Replace a key with a fresh one. The new raw key is shown "
            "exactly once; the previous key is revoked."
        ),
    )
    p.add_argument("key_id", help="Full or shortened key id.")
    p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm rotation non-interactively.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON including the new raw key.",
    )

    p = sub.add_parser(
        "prune",
        help=(
            "Delete terminal keys (revoked, or expired) older than the "
            "grace window. Default is a dry run."
        ),
    )
    p.add_argument(
        "--older-than-days",
        type=int,
        default=_PRUNE_GRACE_DAYS,
        help=f"Delete keys terminal for at least N days (default "
             f"{_PRUNE_GRACE_DAYS}).",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete the listed keys (default is a dry run).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON listing the candidates.",
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

    # Alias: ``relay keys provider migrate`` dispatches to the same
    # handler as ``relay provider keys migrate`` (canonical home).
    from app.cli.provider_keys import add_migrate_parser

    provider_alias = sub.add_parser(
        "provider",
        help="Manage upstream provider API keys (alias for "
             "'relay provider keys').",
    )
    add_migrate_parser(provider_alias.add_subparsers(dest="keys_provider_command"))


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
        "expires_soon": bool(meta.get("expires_soon")),
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
    elif args.keys_command == "rotate":
        _cmd_keys_rotate(args, parser)
    elif args.keys_command == "prune":
        _cmd_keys_prune(args, parser)
    elif args.keys_command == "test":
        _cmd_keys_test(args, parser)
    elif args.keys_command == "provider":
        from app.cli.provider_keys import _cmd_provider_keys_migrate

        if args.keys_provider_command == "migrate":
            _cmd_provider_keys_migrate(args, parser)
        else:
            parser.print_help()
    else:
        parser.print_help()


def _cmd_keys_list(args) -> None:
    """``relay keys list``: one line per key, metadata only."""
    entries = _store().list()

    if args.json:
        print(json.dumps([_json_meta(entry) for entry in entries], indent=2))
        return

    for entry in entries:
        marker = "exp" if entry.get("expires_soon") else "-"
        print(
            f"{_short(entry['id'])}  {entry['label']}  "
            f"{','.join(entry['scopes']) or '-'}  {_iso(entry['expires_at'])}  "
            f"{_iso(entry['created_at'])}  {_iso(entry['last_used_at'])}  "
            f"{_iso(entry['revoked_at'])}  {marker}"
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

    _emit(
        "key.create",
        actor="cli",
        target=key_id,
        detail={"scope_count": len(scopes), "label": label},
    )

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
    _emit("key.revoke", actor="cli", target=meta["id"])
    print(f"Revoked {_short(meta['id'])}")


def _cmd_keys_rotate(args, parser) -> None:
    """
    ``relay keys rotate``: create a replacement key and revoke the
    original. The new raw key is printed exactly once; non-interactive
    runs require ``--yes``.
    """
    key_id = args.key_id.strip()
    store = _store()
    meta = _resolve_key_id(store, key_id)

    if meta is None:
        _fail(f"unknown key {_short(key_id)}")

    if meta["revoked_at"] is not None:
        _fail(f"cannot rotate revoked key {_short(meta['id'])}")

    if not args.yes:
        if sys.stdin.isatty():
            confirm = input(f"Rotate key {_short(meta['id'])}? [y/N] ")
            if confirm.strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return
        else:
            print(
                f"Refusing to rotate {_short(meta['id'])}: pass --yes to "
                "confirm non-interactively.",
                file=sys.stderr,
            )
            raise SystemExit(1)

    try:
        result = store.rotate(meta["id"])
    except Exception as exc:  # noqa: BLE001 - surface short, never the value
        _fail("could not rotate key", exc)

    if result is None:
        _fail(f"unknown key {_short(meta['id'])}")

    new_id, raw_key = result
    _emit(
        "key.rotate",
        actor="cli",
        target=meta["id"],
        detail={"new_key_id": new_id},
    )

    if args.json:
        print(
            json.dumps(
                {
                    "key_id": new_id,
                    "label": meta["label"],
                    "scopes": meta["scopes"],
                    "expires_at": _iso(meta["expires_at"]),
                    "api_key": raw_key,
                },
                indent=2,
            )
        )
        return

    print(f"Key ID: {new_id}")
    print(f"Label: {meta['label']}")
    print(f"Scopes: {','.join(meta['scopes']) or '-'}")
    print(f"Expires: {_iso(meta['expires_at'])}")
    print("---")
    print(f"API Key: {raw_key}")
    print("Shown once — store it now. The previous key has been revoked.")


def _terminal_before(meta: dict, cutoff_ts: float) -> bool:
    """
    True when ``meta`` describes a terminal row (revoked, or expired with
    ``expires_at`` in the past) that became terminal before ``cutoff_ts``.
    Mirrors the ``KeyStore.prune`` predicate for the dry-run listing.
    """
    revoked_at = meta.get("revoked_at")
    if revoked_at is not None:
        return revoked_at < cutoff_ts

    expires_at = meta.get("expires_at")
    if expires_at is not None and expires_at <= time.time():
        return expires_at < cutoff_ts

    return False


def _cmd_keys_prune(args, parser) -> None:
    """
    ``relay keys prune``: delete terminal keys older than the grace
    window. Default is a dry run listing the candidates; ``--yes`` runs
    the delete. Active keys are never touched.
    """
    if args.older_than_days <= 0:
        parser.error("--older-than-days must be a positive number of days")

    store = _store()

    try:
        cutoff = time.time() - args.older_than_days * 86400
        candidates = [
            entry
            for entry in store.list()
            if _terminal_before(entry, cutoff)
        ]
    except Exception as exc:  # noqa: BLE001 - surface short, never the value
        _fail("could not list keys", exc)

    removed = scanned = 0
    if args.yes:
        try:
            removed, scanned = store.prune(cutoff)
        except Exception as exc:  # noqa: BLE001 - surface short, never the value
            _fail("could not prune keys", exc)

        _emit(
            "key.prune",
            actor="cli",
            outcome="ok",
            detail={"removed": removed, "scanned": scanned},
        )

    if args.json:
        print(
            json.dumps(
                {
                    "dry_run": not args.yes,
                    "older_than_days": args.older_than_days,
                    "candidates": [_json_meta(entry) for entry in candidates],
                    "removed": removed,
                    "scanned": scanned,
                },
                indent=2,
            )
        )
        return

    if not candidates:
        print("No terminal keys to prune.")
        return

    print(
        f"{len(candidates)} terminal key(s) older than "
        f"{args.older_than_days} day(s):"
    )

    for entry in candidates:
        print(f"  {_short(entry['id'])}  {entry['label']}")

    if not args.yes:
        print("Dry run: nothing changed. Pass --yes to prune.")
        return

    print(f"Removed {removed} terminal key(s).")


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
