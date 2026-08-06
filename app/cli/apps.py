"""
``relay apps``: list connected applications.

Reads the connected-applications projection (labeled ``api_keys`` x
``request_log`` in ``state_dir/platform.db``) and prints metadata-only
rows: label, opaque key id, route/bucket, request/success/failure counts,
auth schemes, and last-seen. Never renders secrets - the projection
exposes only opaque key ids and labels.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from app.services.apps_projection import apps


def add_apps_parser(parser) -> None:
    """
    Attach the ``relay apps`` flags to the apps subparser.
    """
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum rows to show (default 200).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )


def _run_apps(args, parser) -> None:
    """``relay apps``: list connected applications, newest last-seen first."""
    if args.limit <= 0:
        parser.error("--limit must be a positive number")

    try:
        rows = apps()
    except Exception as exc:  # noqa: BLE001 - surface short, never values
        _fail("could not read the request log", exc)

    rows = rows[: args.limit]

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "label": row.label,
                        "key_id": row.key_id,
                        "bucket": row.bucket,
                        "route": row.route,
                        "requests": row.requests,
                        "successes": row.successes,
                        "failures": row.failures,
                        "auth_schemes": list(row.auth_schemes),
                        "last_seen": _iso(row.last_seen),
                    }
                    for row in rows
                ],
                indent=2,
            )
        )
        return

    if not rows:
        print("No connected applications.")
        return

    print(
        f"{'CLIENT':<24} {'KEY':<10} {'BUCKET':<10} {'ROUTE':<28} "
        f"{'REQ':>4} {'OK':>4} {'FAIL':>4} {'AUTH':<14} {'LAST SEEN'}"
    )

    for row in rows:
        print(
            f"{row.label[:24]:<24} {_short(row.key_id):<10} "
            f"{row.bucket:<10} {row.route[:28]:<28} "
            f"{row.requests:>4} {row.successes:>4} {row.failures:>4} "
            f"{'/'.join(row.auth_schemes)[:14]:<14} {_iso(row.last_seen)}"
        )


def _short(key_id: str | None) -> str:
    return (key_id or "")[:8] or "-"


def _iso(ts) -> str:
    """Render a unix timestamp as ISO 8601 UTC, or ``-`` when absent."""
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _fail(message: str, exc: Exception | None = None) -> None:
    """Print a short error to stderr (no values) and exit 1."""
    if exc is not None:
        print(f"{message}: {exc.__class__.__name__}", file=sys.stderr)
    else:
        print(message, file=sys.stderr)

    raise SystemExit(1)
