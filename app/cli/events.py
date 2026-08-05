"""
``relay events``: tail the durable security-event log.

Reads the ``events`` table in ``state_dir/platform.db`` (schema v5),
newest first, with bounded reads. Rows are redacted at write time, so the
CLI never renders secrets; it only filters and formats.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from app.services.event_log import EVENT_ACTIONS, _OUTCOMES


def add_events_parser(parser) -> None:
    """
    Attach the ``relay events`` flags to the events subparser.
    """
    parser.add_argument(
        "--action",
        default=None,
        help="Filter by action (e.g. 'key.rotate').",
    )
    parser.add_argument(
        "--outcome",
        default=None,
        help="Filter by outcome (ok|failed|denied).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum rows to show (default 50).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )


def _run_events(args, parser) -> None:
    """``relay events``: tail the security-event log, newest first."""
    if args.outcome is not None and args.outcome not in _OUTCOMES:
        parser.error(f"--outcome must be one of: {', '.join(sorted(_OUTCOMES))}")

    if args.limit <= 0:
        parser.error("--limit must be a positive number")

    from app.services.event_log import event_log

    try:
        rows = event_log().query(
            action=args.action,
            outcome=args.outcome,
            limit=args.limit,
        )
    except Exception as exc:  # noqa: BLE001 - surface short, never values
        _fail("could not read the event log", exc)

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    if not rows:
        print("No events.")
        return

    for row in rows:
        detail = row["detail"] or {}
        detail_text = json.dumps(detail, separators=(",", ":")) if detail else "-"
        print(
            f"{_iso(row['ts'])}  {row['outcome']:<7}  {row['action']}  "
            f"{row['actor']}  {row['target'] or '-'}  {detail_text}"
        )


def _iso(ts) -> str:
    """Render a unix timestamp as ISO 8601 UTC."""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _fail(message: str, exc: Exception | None = None) -> None:
    """Print a short error to stderr (no values) and exit 1."""
    if exc is not None:
        print(f"{message}: {exc.__class__.__name__}", file=sys.stderr)
    else:
        print(message, file=sys.stderr)

    raise SystemExit(1)
