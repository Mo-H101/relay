"""
``relay conversations``: metadata-only project-continuity surfaces.

Reads the schema-v7 continuity tables in ``state_dir/platform.db`` and
renders metadata only: conversation ids, key scopes, buckets, project
hashes, statuses, token counts, and compaction summaries. Prompts,
responses, generated content, keys, and paths are never rendered.

When ``CONTINUITY_ENABLED=false`` the command prints ``continuity
disabled`` and exits 0 (P9a DoD).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from app.core.config import settings


def add_continuity_parser(parser) -> None:
    """
    Attach the ``relay conversations`` subcommands.
    """
    sub = parser.add_subparsers(dest="continuity_command")

    lst = sub.add_parser(
        "list",
        help="List conversations (metadata only).",
    )
    lst.add_argument(
        "--status",
        choices=("active", "archived"),
        default=None,
        help="Filter by conversation status.",
    )
    lst.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum rows to show (default 50).",
    )
    lst.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )

    show = sub.add_parser(
        "show",
        help="Show one conversation's metadata and turns.",
    )
    show.add_argument("conversation_id")
    show.add_argument(
        "--turns",
        type=int,
        default=20,
        help="Maximum turns to show (default 20).",
    )
    show.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )

    arch = sub.add_parser(
        "archive",
        help="Archive a conversation.",
    )
    arch.add_argument("conversation_id")

    prune = sub.add_parser(
        "prune",
        help="Prune conversations idle past the retention window.",
    )
    prune.add_argument(
        "--days",
        type=int,
        default=None,
        help="Retention window, in days (default: "
             "CONTINUITY_RETENTION_DAYS).",
    )
    prune.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )


def _run_continuity(args, parser) -> None:
    """``relay conversations``: metadata-only continuity surfaces."""
    if not settings.continuity_enabled:
        print("continuity disabled")
        return

    from app.core.relay import relay

    store = relay.conversation_store

    if store is None:
        print("continuity unavailable", file=sys.stderr)
        raise SystemExit(1)

    command = getattr(args, "continuity_command", None)

    if command == "list":
        _list(store, args)
    elif command == "show":
        _show(store, args)
    elif command == "archive":
        _archive(store, args)
    elif command == "prune":
        _prune(store, args)
    else:
        _summary(store)


def _list(store, args) -> None:
    try:
        rows = store.list(status=args.status, limit=args.limit)
    except Exception as exc:  # noqa: BLE001 - surface short, never values
        _fail("could not list conversations", exc)

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    if not rows:
        print("No conversations.")
        return

    for row in rows:
        print(
            f"{row['id']}  {row['status']:<8}  {row['key_id']}  "
            f"{row['client_bucket']:<8}  {_iso(row['updated_at'])}"
        )


def _show(store, args) -> None:
    try:
        record = store.find(args.conversation_id)
    except Exception as exc:  # noqa: BLE001
        _fail("could not read the conversation", exc)

    if record is None:
        print("No such conversation.")
        return

    key_id = record["key_id"]
    turns = store.turns(args.conversation_id, key_id, limit=args.turns)
    summaries = store.summaries(args.conversation_id, key_id, limit=5)

    if args.json:
        print(json.dumps({"conversation": record, "turns": turns,
                          "summaries": summaries}, indent=2))
        return

    print(
        f"id:           {record['id']}\n"
        f"status:       {record['status']}\n"
        f"key_id:       {record['key_id']}\n"
        f"client_bucket:{record['client_bucket']}\n"
        f"model_chain:  {','.join(record['model_chain'])}\n"
        f"turns:        {len(turns)}\n"
        f"summaries:    {len(summaries)}"
    )


def _archive(store, args) -> None:
    record = store.find(args.conversation_id)

    if record is None:
        print("No such conversation.")
        return

    if store.archive(args.conversation_id, record["key_id"]):
        print(f"archived {args.conversation_id}")
    else:
        print("could not archive the conversation", file=sys.stderr)
        raise SystemExit(1)


def _prune(store, args) -> None:
    days = args.days if args.days is not None else settings.continuity_retention_days

    try:
        removed = store.prune_retention(days)
    except Exception as exc:  # noqa: BLE001
        _fail("could not prune conversations", exc)

    if args.json:
        print(json.dumps({"removed": removed, "days": days}))
        return

    print(f"pruned {removed} conversations (window: {days} days)")


def _summary(store) -> None:
    counts = store.counts()

    print(
        f"continuity enabled\n"
        f"conversations: {counts['conversations']} "
        f"(active {counts['active']}, archived {counts['archived']})\n"
        f"turns: {counts['turns']}\n"
        f"summaries: {counts['summaries']}\n"
        f"compactions: {counts['compactions']}\n"
        f"project state rows: {counts['projects']}"
    )


def _iso(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _fail(message: str, exc: Exception | None = None) -> None:
    if exc is not None:
        print(f"{message}: {exc.__class__.__name__}", file=sys.stderr)
    else:
        print(message, file=sys.stderr)

    raise SystemExit(1)
