"""
Durable conversation store for the P9 project-continuity layer.

``ConversationStore`` owns the continuity tables on the shared
``platform.db`` (``conversations``, ``conversation_turns``, ``summaries``,
``compaction_records``, ``project_state``, plus the v8
``resume_replays`` replay tracker) behind a single guarded
connection. It follows the ``StateStore`` / ``RequestLogStore``
conventions: WAL + ``busy_timeout 5000`` + ``0600`` sidecars via
``PlatformStore``, a ``threading.Lock`` around every operation,
key-scoped reads and writes, and best-effort audit rows through
``event_log()``.

Boundary (P9): SQLite is never touched from chat request paths; all
continuity writes happen on background/admin paths. Every mutator
validates the ``key_id`` binding before touching a row, so one key can
never read or mutate another key's conversations (S7). Unknown or
mismatched ids simply return nothing -- no oracle.

Privacy contract (Option C): rows hold metadata and derived state only.
Raw prompts, raw responses, generated content, API keys, proxy
credentials, correlation ids, and filesystem paths are never stored.
``summaries.content`` is derived, redacted at write time, and bounded by
``CONTINUITY_SUMMARY_MAX_CHARS``. Resume tokens are stored only as
one-way hashes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Optional

from app.services import platform_store
from app.models.continuity import (
    ConversationRecord,
    ConversationStatus,
)

_logger = logging.getLogger("relay")

# Bounded read ceilings so the CLI/diagnostics surfaces never dump a table.
_MAX_QUERY_LIMIT = 5000
_MAX_TURNS_LIMIT = 1000

# Bounds for opaque ids held by the store (header validation in
# continuity_headers enforces the wire contract; the store guards against
# junk rows regardless of caller).
_MAX_ID_LENGTH = 128
_MAX_RESUME_TOKEN_HASH_LENGTH = 128
_VALID_BUCKETS = frozenset({"cline", "opencode", "continue", "other"})
_VALID_STATUS = frozenset({"active", "archived"})
_VALID_OUTCOMES = frozenset({"ok", "failed", "denied"})


class MalformedInputError(ValueError):
    """Raised when caller-supplied data fails input validation.

    Distinguished from plain ``ValueError`` so that the continuity
    flusher can drop malformed operations (which can never succeed on
    retry) without blocking the write-behind queue.  Includes both
    data-shape validation failures (invalid outcome, seq, etc.) and
    permanent state conditions (conversation not found, archived)
    that cannot succeed on retry.
    """


def _validate_non_negative_int(value, name: str) -> None:
    """Validate that *value* is ``None`` or a non-negative ``int``.

    Rejects booleans (``bool`` is a subclass of ``int`` in Python),
    floats, strings, and negative integers.  Raises
    ``MalformedInputError`` on invalid input so that bad accounting
    data cannot cross the persistence boundary.
    """
    if value is None:
        return
    if type(value) is not int or value < 0:
        raise MalformedInputError(f"invalid {name}: {value!r}")


class ConversationStoreError(Exception):
    """Raised when the conversation store cannot open or read."""


class ConversationStore:
    """
    SQLite-backed durable store for conversation metadata.

    Single guarded connection with WAL journaling and a busy timeout,
    mirroring ``StateStore``. Reads and writes are key-scoped and never
    run on chat request paths.
    """

    SCHEMA_VERSION = platform_store.SCHEMA_VERSION

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or ":memory:"
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._open_attempts = 0
        self._open_errors = 0
        # Conversation ids with un-drained flusher rows (registered by
        # ContinuityFlusher). Guarded by _lock; used to keep background
        # prunes from racing pending flushes (P9d).
        self._in_flight: set = set()

    def mark_in_flight(self, conversation_id: str) -> None:
        """Register a conversation as having queued-but-unflushed rows."""
        if not conversation_id:
            return
        with self._lock:
            self._in_flight.add(conversation_id)

    def clear_in_flight(self, conversation_id: str) -> None:
        """Deregister a conversation once its flusher queue drains."""
        with self._lock:
            self._in_flight.discard(conversation_id)

    @property
    def in_flight(self) -> tuple:
        """Snapshot of in-flight conversation ids (for diagnostics)."""
        with self._lock:
            return tuple(sorted(self._in_flight))

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        """
        Release the underlying SQLite connection.

        Closing is intentionally not terminal: exactly like ``KeyStore``
        (whose ``verify_reopens_after_close`` contract is pinned by
        tests), the store lazily reopens on its next operation. Late
        writers during shutdown drain keep working and background
        flushers can never crash on a closed handle; callers that need a
        truly terminal close must stop scheduling work first.
        """
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def stats(self) -> dict:
        """Diagnostics about the store connection."""
        with self._lock:
            return {
                "path": self._path,
                "open": self._conn is not None,
                "schema_version": self.SCHEMA_VERSION,
                "open_attempts": self._open_attempts,
                "open_errors": self._open_errors,
            }

    # ============================
    # Conversations
    # ============================

    def create(
        self,
        *,
        key_id: str,
        client_bucket: str,
        project_key: str,
        model_chain: Optional[list] = None,
        token_budget: Optional[int] = None,
        conversation_id: Optional[str] = None,
    ) -> dict:
        """
        Create one conversation and return its record. ``key_id`` is the
        authenticated opaque key id that owns every row; ``project_key``
        is the opaque key-scoped project hash. Emits a best-effort
        ``continuity.create`` audit row.
        """
        key_id, client_bucket, project_key = self._validate_scope(
            key_id, client_bucket, project_key
        )
        conversation_id = self._validate_id(conversation_id) or _new_id()

        now = time.time()
        record = ConversationRecord(
            id=conversation_id,
            key_id=key_id,
            client_bucket=client_bucket,
            project_key=project_key,
            status=ConversationStatus.ACTIVE.value,
            model_chain=list(model_chain or []),
            token_budget=token_budget,
            created_at=now,
            updated_at=now,
        )

        conn = self._require_open()

        with self._lock:
            with conn:
                conn.execute(
                    "INSERT INTO conversations ("
                    "  id, key_id, client_bucket, project_key, status,"
                    "  model_chain, token_budget, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.id,
                        record.key_id,
                        record.client_bucket,
                        record.project_key,
                        record.status,
                        json.dumps(record.model_chain),
                        record.token_budget,
                        record.created_at,
                        record.updated_at,
                    ),
                )

        self._audit(
            "continuity.create",
            actor=key_id,
            target=conversation_id,
            outcome="ok",
            detail={"bucket": client_bucket, "project_key": project_key},
        )
        return record.to_dict()

    def get(self, conversation_id: str, key_id: str) -> Optional[dict]:
        """Return one conversation owned by ``key_id``, or None."""
        conn = self._require_open()

        with self._lock:
            row = conn.execute(
                "SELECT id, key_id, client_bucket, project_key, status,"
                "  model_chain, token_budget, created_at, updated_at,"
                "  last_turn_ts"
                " FROM conversations WHERE id = ? AND key_id = ?",
                (conversation_id, key_id),
            ).fetchone()

        if row is None:
            return None

        return self._conversation_row(row)

    def find(self, conversation_id: str) -> Optional[dict]:
        """
        Operator/admin read: return one conversation by id regardless of
        key. Never reachable from chat request paths; used only by the
        ``relay conversations`` CLI surface (which, like ``relay events``,
        is a local operator tool).
        """
        conn = self._require_open()

        with self._lock:
            row = conn.execute(
                "SELECT id, key_id, client_bucket, project_key, status,"
                "  model_chain, token_budget, created_at, updated_at,"
                "  last_turn_ts"
                " FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()

        if row is None:
            return None

        return self._conversation_row(row)

    def list(
        self,
        key_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        """
        Return conversations newest-updated first, optionally filtered by
        key and status. Bounded reads only.
        """
        limit = max(1, min(int(limit), _MAX_QUERY_LIMIT))

        clauses: list = []
        params: list = []

        if key_id:
            clauses.append("key_id = ?")
            params.append(key_id)
        if status:
            clauses.append("status = ?")
            params.append(status)

        where = " WHERE " + " AND ".join(clauses) if clauses else ""

        conn = self._require_open()

        with self._lock:
            rows = conn.execute(
                "SELECT id, key_id, client_bucket, project_key, status,"
                "  model_chain, token_budget, created_at, updated_at,"
                "  last_turn_ts"
                f" FROM conversations{where}"
                " ORDER BY updated_at DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()

        return [self._conversation_row(row) for row in rows]

    def archive(self, conversation_id: str, key_id: str) -> bool:
        """
        Mark a conversation archived (key-scoped). Returns False when the
        conversation does not exist or is not owned by ``key_id``. Emits a
        best-effort ``continuity.archive`` audit row on success.
        """
        conn = self._require_open()

        with self._lock:
            with conn:
                cursor = conn.execute(
                    "UPDATE conversations SET status = ?, updated_at = ?"
                    " WHERE id = ? AND key_id = ?",
                    (ConversationStatus.ARCHIVED.value, time.time(),
                     conversation_id, key_id),
                )

        if cursor.rowcount == 0:
            return False

        self._audit(
            "continuity.archive",
            actor=key_id,
            target=conversation_id,
            outcome="ok",
        )
        return True

    def _prune_candidates(self, days: int) -> list:
        """
        Return conversation ids idle longer than ``days`` days. Shared by
        ``prune_retention`` and ``prune_preview`` so the preview always
        matches what a real prune would remove. ``days <= 0`` yields no
        candidates. In-flight conversations (un-drained flusher rows) are
        always excluded so a prune never removes a conversation whose
        flusher queue still holds rows.
        """
        if days <= 0:
            return []

        cutoff = time.time() - int(days) * 86400
        conn = self._require_open()

        with self._lock:
            exclude_ids = tuple(sorted(self._in_flight))
            placeholders = ",".join("?" for _ in exclude_ids)

            if placeholders:
                rows = conn.execute(
                    "SELECT id FROM conversations"
                    " WHERE id NOT IN ({})"
                    "   AND (status = ? OR last_turn_ts IS NOT NULL)"
                    "   AND COALESCE(last_turn_ts, updated_at) < ?"
                    " ORDER BY id ASC".format(placeholders),
                    (*exclude_ids, ConversationStatus.ARCHIVED.value, cutoff),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM conversations"
                    " WHERE (status = ? OR last_turn_ts IS NOT NULL)"
                    "   AND COALESCE(last_turn_ts, updated_at) < ?"
                    " ORDER BY id ASC",
                    (ConversationStatus.ARCHIVED.value, cutoff),
                ).fetchall()

        return [row[0] for row in rows]

    def prune_retention(self, days: int) -> int:
        """
        Delete conversations idle longer than ``days`` days and their
        turns/summaries/compactions, plus stale project state. Returns the
        number of conversations removed. ``days <= 0`` disables pruning
        (returns 0).

        S8: only archived or long-inactive conversations are pruned; a
        conversation active within the retention window is never removed.
        P9d: in-flight conversations (in the flusher registry) are skipped
        so a background prune cannot race a pending flush.
        Emits a best-effort ``continuity.prune`` audit row.
        """
        if days <= 0:
            return 0

        cutoff = time.time() - int(days) * 86400
        ids = self._prune_candidates(days)

        conn = self._require_open()

        with self._lock:
            with conn:
                for conversation_id in ids:
                    conn.execute(
                        "DELETE FROM conversation_turns"
                        " WHERE conversation_id = ?",
                        (conversation_id,),
                    )
                    conn.execute(
                        "DELETE FROM summaries WHERE conversation_id = ?",
                        (conversation_id,),
                    )
                    conn.execute(
                        "DELETE FROM resume_replays"
                        " WHERE conversation_id = ?",
                        (conversation_id,),
                    )
                    conn.execute(
                        "DELETE FROM compaction_records"
                        " WHERE conversation_id = ?",
                        (conversation_id,),
                    )
                    conn.execute(
                        "DELETE FROM conversations WHERE id = ?",
                        (conversation_id,),
                    )

                conn.execute(
                    "DELETE FROM project_state WHERE last_seen < ?",
                    (cutoff,),
                )

        removed = len(ids)

        if removed:
            self._audit(
                "continuity.prune",
                actor="system",
                target="conversations",
                outcome="ok",
                detail={"removed": removed, "days": int(days)},
            )

        return removed

    def prune_preview(self, days: int) -> list:
        """
        Diagnostic-only: return the ids that ``prune_retention`` would
        remove for ``days`` (same candidate logic, including the in-flight
        exclusion). Never mutates rows.
        """
        return self._prune_candidates(days)

    # ============================
    # Turns
    # ============================

    def last_turn(self, conversation_id: str, key_id: str) -> Optional[dict]:
        """
        Return the most recently appended turn (highest seq) for a
        conversation owned by ``key_id``, or None when no turn exists.
        Used by ``ContinuityRecovery`` to find the last committed resume
        point.
        """
        conn = self._require_open()

        with self._lock:
            row = conn.execute(
                "SELECT t.conversation_id, t.seq, t.provider, t.model,"
                "  t.outcome, t.task, t.tokens_in, t.tokens_out,"
                "  t.latency_ms, t.resume_token, t.ts"
                " FROM conversation_turns t"
                " JOIN conversations c ON c.id = t.conversation_id"
                " WHERE t.conversation_id = ? AND c.key_id = ?"
                " ORDER BY t.seq DESC LIMIT 1",
                (conversation_id, key_id),
            ).fetchone()

        if row is None:
            return None

        return {
            "conversation_id": row[0],
            "seq": row[1],
            "provider": row[2],
            "model": row[3],
            "outcome": row[4],
            "task": row[5],
            "tokens_in": row[6],
            "tokens_out": row[7],
            "latency_ms": row[8],
            "resume_token_hash": row[9],
            "ts": row[10],
        }

    def turn_seqs(self, conversation_id: str, key_id: str) -> list:
        """
        Return the committed turn sequence numbers (ascending) for a
        conversation owned by ``key_id``. Used by ``ContinuityRecovery``
        reconcile to detect seq gaps / duplicates without pulling full
        rows.
        """
        conn = self._require_open()

        with self._lock:
            rows = conn.execute(
                "SELECT t.seq FROM conversation_turns t"
                " JOIN conversations c ON c.id = t.conversation_id"
                " WHERE t.conversation_id = ? AND c.key_id = ?"
                " ORDER BY t.seq ASC",
                (conversation_id, key_id),
            ).fetchall()

        return [row[0] for row in rows]

    def last_summary(self, conversation_id: str, key_id: str) -> Optional[dict]:
        """
        Return the summary with the highest ``up_to_seq`` for a
        conversation owned by ``key_id``, or None. Used by
        ``ContinuityRecovery`` reconcile to verify that the last committed
        summary is consistent with committed turns.
        """
        conn = self._require_open()

        with self._lock:
            row = conn.execute(
                "SELECT s.conversation_id, s.up_to_seq, s.version,"
                "  s.method, s.content, s.tokens_in, s.tokens_out,"
                "  s.created_at"
                " FROM summaries s"
                " JOIN conversations c ON c.id = s.conversation_id"
                " WHERE s.conversation_id = ? AND c.key_id = ?"
                " ORDER BY s.up_to_seq DESC, s.created_at DESC LIMIT 1",
                (conversation_id, key_id),
            ).fetchone()

        if row is None:
            return None

        return {
            "conversation_id": row[0],
            "up_to_seq": row[1],
            "version": row[2],
            "method": row[3],
            # Safe key name: the resume envelope carries this dict into
            # resume responses and must pass the memory-contract negative
            # tests (P9e F-2).
            "summary_text": row[4],
            "tokens_in": row[5],
            "tokens_out": row[6],
            "ts": row[7],
        }

    def append_turn(
        self,
        *,
        conversation_id: str,
        key_id: str,
        seq: int,
        outcome: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        task: Optional[str] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        latency_ms: Optional[int] = None,
        resume_token_hash: Optional[str] = None,
    ) -> dict:
        """
        Append one metadata-only turn to an active conversation owned by
        ``key_id``. Raises ``MalformedInputError`` (a ``ValueError``
        subclass) when the conversation is missing, not owned by the
        key, or archived. Updates the conversation's ``updated_at`` /
        ``last_turn_ts``.
        """
        if outcome not in _VALID_OUTCOMES:
            raise MalformedInputError(f"invalid turn outcome: {outcome!r}")
        if (
            resume_token_hash is not None
            and not isinstance(resume_token_hash, str)
            or (
                resume_token_hash is not None
                and len(resume_token_hash) > _MAX_RESUME_TOKEN_HASH_LENGTH
            )
        ):
            raise MalformedInputError("resume_token_hash must be a short string")
        if not isinstance(seq, int) or seq < 1:
            raise MalformedInputError(f"invalid turn seq: {seq!r}")
        _validate_non_negative_int(tokens_in, "tokens_in")
        _validate_non_negative_int(tokens_out, "tokens_out")
        _validate_non_negative_int(latency_ms, "latency_ms")

        now = time.time()
        conn = self._require_open()

        with self._lock:
            with conn:
                existing = conn.execute(
                    "SELECT status FROM conversations"
                    " WHERE id = ? AND key_id = ?",
                    (conversation_id, key_id),
                ).fetchone()

                if existing is None:
                    raise MalformedInputError("conversation not found")
                if existing[0] != ConversationStatus.ACTIVE.value:
                    raise MalformedInputError("conversation is archived")

                conn.execute(
                    "INSERT INTO conversation_turns ("
                    "  conversation_id, seq, provider, model, outcome, task,"
                    "  tokens_in, tokens_out, latency_ms, resume_token, ts"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        conversation_id,
                        int(seq),
                        provider,
                        model,
                        outcome,
                        task,
                        tokens_in,
                        tokens_out,
                        latency_ms,
                        resume_token_hash,
                        now,
                    ),
                )

                conn.execute(
                    "UPDATE conversations SET updated_at = ?, last_turn_ts = ?"
                    " WHERE id = ?",
                    (now, now, conversation_id),
                )

        return {
            "conversation_id": conversation_id,
            "seq": int(seq),
            "outcome": outcome,
            "ts": now,
        }

    def update_turn(
        self,
        *,
        conversation_id: str,
        key_id: str,
        seq: int,
        outcome: str,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        latency_ms: Optional[int] = None,
    ) -> dict:
        """
        Finalize a previously-appended turn by updating its outcome and
        token counts.  Used by the streaming provisional-turn lifecycle
        (Phase 14): a turn is first appended with ``outcome="denied"``
        (provisional) and later updated to the real outcome once the
        stream completes, fails, or is cancelled.

        Key-scoped: the turn must belong to a conversation owned by
        ``key_id``.  Returns the updated turn record, or ``{}`` when the
        row does not exist or the conversation is not owned by the key.

        F-5 / N-8: accounting fields that are ``None`` are intentionally
        *not* written.  Only explicitly-provided non-``None`` accounting
        values overwrite existing durable data.  This prevents a partial
        update from erasing valid accounting with NULL.
        """
        if outcome not in _VALID_OUTCOMES:
            raise MalformedInputError(f"invalid turn outcome: {outcome!r}")
        if not isinstance(seq, int) or seq < 1:
            raise MalformedInputError(f"invalid turn seq: {seq!r}")
        _validate_non_negative_int(tokens_in, "tokens_in")
        _validate_non_negative_int(tokens_out, "tokens_out")
        _validate_non_negative_int(latency_ms, "latency_ms")

        now = time.time()
        conn = self._require_open()

        sets = ["outcome = ?"]
        params: list = [outcome]
        if tokens_in is not None:
            sets.append("tokens_in = ?")
            params.append(tokens_in)
        if tokens_out is not None:
            sets.append("tokens_out = ?")
            params.append(tokens_out)
        if latency_ms is not None:
            sets.append("latency_ms = ?")
            params.append(latency_ms)

        with self._lock:
            with conn:
                cursor = conn.execute(
                    "UPDATE conversation_turns"
                    f" SET {', '.join(sets)}"
                    " WHERE conversation_id = ?"
                    "   AND seq = ?"
                    "   AND EXISTS ("
                    "    SELECT 1 FROM conversations"
                    "     WHERE id = conversation_turns.conversation_id"
                    "       AND key_id = ?"
                    "  )",
                    (*params, conversation_id, seq, key_id),
                )

                if cursor.rowcount == 0:
                    return {}

                conn.execute(
                    "UPDATE conversations SET updated_at = ?"
                    " WHERE id = ?",
                    (now, conversation_id),
                )

        return {
            "conversation_id": conversation_id,
            "seq": int(seq),
            "outcome": outcome,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
            "ts": now,
        }

    def turns(
        self,
        conversation_id: str,
        key_id: str,
        limit: int = 100,
    ) -> list:
        """Return a conversation's turns, oldest first (key-scoped)."""
        limit = max(1, min(int(limit), _MAX_TURNS_LIMIT))
        conn = self._require_open()

        with self._lock:
            rows = conn.execute(
                "SELECT t.conversation_id, t.seq, t.provider, t.model,"
                "  t.outcome, t.task, t.tokens_in, t.tokens_out,"
                "  t.latency_ms, t.resume_token, t.ts"
                " FROM conversation_turns t"
                " JOIN conversations c ON c.id = t.conversation_id"
                " WHERE t.conversation_id = ? AND c.key_id = ?"
                " ORDER BY t.seq ASC LIMIT ?",
                (conversation_id, key_id, limit),
            ).fetchall()

        return [
            {
                "conversation_id": row[0],
                "seq": row[1],
                "provider": row[2],
                "model": row[3],
                "outcome": row[4],
                "task": row[5],
                "tokens_in": row[6],
                "tokens_out": row[7],
                "latency_ms": row[8],
                "resume_token_hash": row[9],
                "ts": row[10],
            }
            for row in rows
        ]

    # ============================
    # Resume replay tracking (v8)
    # ============================

    def resume_replay_attempts(
        self, conversation_id: str, key_id: str, token_hash: str
    ) -> int:
        """
        Durable replay-attempt count for one ``(conversation, token_hash)``
        pair, key-scoped. Returns 0 when no attempt was ever recorded.
        """
        if not token_hash or len(token_hash) > _MAX_RESUME_TOKEN_HASH_LENGTH:
            return 0
        conn = self._require_open()

        with self._lock:
            row = conn.execute(
                "SELECT r.attempts FROM resume_replays r"
                " JOIN conversations c ON c.id = r.conversation_id"
                " WHERE r.conversation_id = ? AND c.key_id = ?"
                "   AND r.token_hash = ?",
                (conversation_id, key_id, token_hash),
            ).fetchone()

        return row[0] if row else 0

    def record_resume_replay_attempt(
        self, conversation_id: str, key_id: str, token_hash: str
    ) -> int:
        """
        Atomically increment and return the durable replay-attempt count
        for one ``(conversation, token_hash)`` pair, key-scoped. Returns 0
        when the conversation is missing or not owned by ``key_id`` (no
        row is ever created for a foreign key). The increment and read run
        in one transaction under the store lock so concurrent validations
        are serialized (P9e: replay limits survive process restart).
        """
        if not token_hash or len(token_hash) > _MAX_RESUME_TOKEN_HASH_LENGTH:
            return 0
        conn = self._require_open()

        with self._lock:
            with conn:
                owned = conn.execute(
                    "SELECT 1 FROM conversations WHERE id = ? AND key_id = ?",
                    (conversation_id, key_id),
                ).fetchone()

                if owned is None:
                    return 0

                conn.execute(
                    "INSERT INTO resume_replays ("
                    "  conversation_id, token_hash, attempts, last_ts"
                    ") VALUES (?, ?, 1, ?)"
                    " ON CONFLICT(conversation_id, token_hash)"
                    " DO UPDATE SET attempts = attempts + 1,"
                    "  last_ts = excluded.last_ts",
                    (conversation_id, token_hash, time.time()),
                )
                row = conn.execute(
                    "SELECT attempts FROM resume_replays"
                    " WHERE conversation_id = ? AND token_hash = ?",
                    (conversation_id, token_hash),
                ).fetchone()

        return row[0] if row else 0

    def clear_resume_replay(
        self, conversation_id: str, key_id: Optional[str] = None
    ) -> None:
        """
        Delete all durable replay rows for a conversation (single-use token
        lifecycle: a new issuance or a turn commit ends the old token's
        budget). Key-scoped when ``key_id`` is given; without a key the
        delete is still bounded to one opaque conversation id.
        """
        if not conversation_id:
            return
        conn = self._require_open()

        with self._lock:
            with conn:
                if key_id:
                    conn.execute(
                        "DELETE FROM resume_replays"
                        " WHERE conversation_id = ? AND EXISTS ("
                        "  SELECT 1 FROM conversations"
                        "   WHERE id = ? AND key_id = ?)",
                        (conversation_id, conversation_id, key_id),
                    )
                else:
                    conn.execute(
                        "DELETE FROM resume_replays"
                        " WHERE conversation_id = ?",
                        (conversation_id,),
                    )

    # ============================
    # Summaries and compactions
    # ============================

    def record_summary(
        self,
        *,
        conversation_id: str,
        key_id: str,
        up_to_seq: int,
        version: int,
        method: str,
        content: str,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
    ) -> dict:
        """
        Persist one derived summary. ``content`` is redacted and bounded
        before insert (last line of defense); the summary is deduped by
        ``(conversation_id, up_to_seq)`` via the UNIQUE constraint.
        Instruction-shaped content (P9e) is rejected so a poisoned summary
        never counts as trusted state, regardless of which path recorded
        it.
        """
        from app.core.config import settings
        from app.services.redaction import redact_text
        from app.services.summary_verifier import is_instruction_shaped

        _validate_non_negative_int(tokens_in, "tokens_in")
        _validate_non_negative_int(tokens_out, "tokens_out")

        max_chars = settings.continuity_summary_max_chars
        safe_content = redact_text(str(content or ""))[: max_chars]

        if is_instruction_shaped(safe_content):
            raise MalformedInputError("instruction-shaped summary content")

        conn = self._require_open()

        with self._lock:
            with conn:
                existing = conn.execute(
                    "SELECT id FROM conversations"
                    " WHERE id = ? AND key_id = ?",
                    (conversation_id, key_id),
                ).fetchone()

                if existing is None:
                    raise MalformedInputError("conversation not found")

                conn.execute(
                    "INSERT INTO summaries ("
                    "  conversation_id, up_to_seq, version, method, content,"
                    "  tokens_in, tokens_out, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(conversation_id, up_to_seq)"
                    " DO UPDATE SET version = excluded.version,"
                    "  method = excluded.method, content = excluded.content,"
                    "  tokens_in = excluded.tokens_in,"
                    "  tokens_out = excluded.tokens_out",
                    (
                        conversation_id,
                        int(up_to_seq),
                        int(version),
                        method,
                        safe_content,
                        tokens_in,
                        tokens_out,
                        time.time(),
                    ),
                )

        return self._last_summary(conversation_id, key_id)

    def summaries(
        self,
        conversation_id: str,
        key_id: str,
        limit: int = 50,
    ) -> list:
        """Return a conversation's summaries, newest first (key-scoped)."""
        limit = max(1, min(int(limit), _MAX_QUERY_LIMIT))
        conn = self._require_open()

        with self._lock:
            rows = conn.execute(
                "SELECT s.id, s.conversation_id, s.up_to_seq, s.version,"
                "  s.method, s.content, s.tokens_in, s.tokens_out,"
                "  s.created_at"
                " FROM summaries s"
                " JOIN conversations c ON c.id = s.conversation_id"
                " WHERE s.conversation_id = ? AND c.key_id = ?"
                " ORDER BY s.up_to_seq DESC LIMIT ?",
                (conversation_id, key_id, limit),
            ).fetchall()

        return [
            {
                "summary_id": row[0],
                "conversation_id": row[1],
                "up_to_seq": row[2],
                "version": row[3],
                "method": row[4],
                # Safe key name: exported summaries must pass the
                # memory-contract negative tests.
                "summary_text": row[5],
                "tokens_in": row[6],
                "tokens_out": row[7],
                "created_at": row[8],
            }
            for row in rows
        ]

    def record_compaction(
        self,
        *,
        conversation_id: str,
        key_id: str,
        reason: str,
        method: str,
        from_tokens: Optional[int] = None,
        to_tokens: Optional[int] = None,
        summary_id: Optional[int] = None,
    ) -> dict:
        """
        Record one compaction event (metadata only). Emits a best-effort
        ``continuity.compact`` audit row.
        """
        _validate_non_negative_int(from_tokens, "from_tokens")
        _validate_non_negative_int(to_tokens, "to_tokens")

        conn = self._require_open()
        now = time.time()

        with self._lock:
            with conn:
                existing = conn.execute(
                    "SELECT id FROM conversations"
                    " WHERE id = ? AND key_id = ?",
                    (conversation_id, key_id),
                ).fetchone()

                if existing is None:
                    raise MalformedInputError("conversation not found")

                conn.execute(
                    "INSERT INTO compaction_records ("
                    "  conversation_id, at, reason, method, from_tokens,"
                    "  to_tokens, summary_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        conversation_id,
                        now,
                        reason,
                        method,
                        from_tokens,
                        to_tokens,
                        summary_id,
                    ),
                )

        self._audit(
            "continuity.compact",
            actor=key_id,
            target=conversation_id,
            outcome="ok",
            detail={"reason": reason, "method": method},
        )
        return {
            "conversation_id": conversation_id,
            "at": now,
            "reason": reason,
            "method": method,
            "from_tokens": from_tokens,
            "to_tokens": to_tokens,
            "summary_id": summary_id,
        }

    def compactions(
        self,
        conversation_id: str,
        key_id: str,
        limit: int = 50,
    ) -> list:
        """Return a conversation's compaction records, newest first."""
        limit = max(1, min(int(limit), _MAX_QUERY_LIMIT))
        conn = self._require_open()

        with self._lock:
            rows = conn.execute(
                "SELECT r.conversation_id, r.at, r.reason, r.method,"
                "  r.from_tokens, r.to_tokens, r.summary_id"
                " FROM compaction_records r"
                " JOIN conversations c ON c.id = r.conversation_id"
                " WHERE r.conversation_id = ? AND c.key_id = ?"
                " ORDER BY r.at DESC LIMIT ?",
                (conversation_id, key_id, limit),
            ).fetchall()

        return [
            {
                "conversation_id": row[0],
                "at": row[1],
                "reason": row[2],
                "method": row[3],
                "from_tokens": row[4],
                "to_tokens": row[5],
                "summary_id": row[6],
            }
            for row in rows
        ]

    # ============================
    # Project state
    # ============================

    def update_project_state(
        self,
        *,
        key_id: str,
        project_key: str,
        last_models: Optional[list] = None,
        counters: Optional[dict] = None,
    ) -> dict:
        """Upsert bounded derived project state, key-scoped."""
        key_id, _, project_key = self._validate_scope(
            key_id, "other", project_key
        )
        now = time.time()
        conn = self._require_open()

        with self._lock:
            with conn:
                conn.execute(
                    "INSERT INTO project_state ("
                    "  project_key, key_id, last_models, counters, last_seen"
                    ") VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(project_key, key_id)"
                    " DO UPDATE SET last_models = excluded.last_models,"
                    "  counters = excluded.counters,"
                    "  last_seen = excluded.last_seen",
                    (
                        project_key,
                        key_id,
                        json.dumps(list(last_models or [])),
                        json.dumps(counters or {}),
                        now,
                    ),
                )

        return {
            "project_key": project_key,
            "key_id": key_id,
            "last_models": list(last_models or []),
            "counters": dict(counters or {}),
            "last_seen": now,
        }

    def project_state(self, key_id: str, project_key: str) -> Optional[dict]:
        """Return one project's derived state, key-scoped."""
        conn = self._require_open()

        with self._lock:
            row = conn.execute(
                "SELECT project_key, key_id, last_models, counters,"
                "  last_seen FROM project_state"
                " WHERE project_key = ? AND key_id = ?",
                (project_key, key_id),
            ).fetchone()

        if row is None:
            return None

        return {
            "project_key": row[0],
            "key_id": row[1],
            "last_models": json.loads(row[2] or "[]"),
            "counters": json.loads(row[3] or "{}"),
            "last_seen": row[4],
        }

    def project_states(
        self, key_id: Optional[str] = None, limit: int = 50
    ) -> list:
        """
        Read-only bounded projection of the durable ``project_state`` table
        (key-scoped, newest ``last_seen`` first, stable ``project_key``
        tie-break). Each row is the metadata-only checkpoint maintained by
        the write-behind flusher — never used for request-path hydration.
        Best-effort: an unavailable store yields an empty list.
        """
        limit = max(1, min(int(limit), _MAX_QUERY_LIMIT))

        if not self._ensure_open():
            return []

        scope = " WHERE key_id = ?" if key_id else ""

        try:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT project_key, key_id, last_models,"
                    f"  counters, last_seen FROM project_state{scope}"
                    f" ORDER BY last_seen DESC, project_key ASC"
                    f" LIMIT ?",
                    (key_id, limit) if key_id else (limit,),
                ).fetchall()
        except Exception:  # noqa: BLE001 - diagnostics are best-effort
            return []

        return [
            {
                "project_key": row[0],
                "key_id": row[1],
                "last_models": json.loads(row[2] or "[]"),
                "counters": json.loads(row[3] or "{}"),
                "last_seen": row[4],
            }
            for row in rows
        ]

    # ============================
    # Diagnostics
    # ============================

    def counts(self, key_id: Optional[str] = None) -> dict:
        """
        Row counts for the continuity tables, optionally key-scoped.
        Best-effort: an unavailable store yields all-zero counts.
        """
        zero = {
            "conversations": 0,
            "active": 0,
            "archived": 0,
            "turns": 0,
            "summaries": 0,
            "compactions": 0,
            "projects": 0,
            "replays": 0,
        }

        if not self._ensure_open():
            return zero

        scope = " WHERE key_id = ?" if key_id else ""

        try:
            with self._lock:
                conv = self._conn.execute(
                    f"SELECT status, count(*) FROM conversations{scope}"
                    " GROUP BY status"
                    + (" " if scope else ""),
                    (key_id,) if key_id else (),
                ).fetchall()
                turns = self._conn.execute(
                    "SELECT count(*) FROM conversation_turns t"
                    + (
                        " JOIN conversations c ON c.id = t.conversation_id"
                        " WHERE c.key_id = ?"
                        if key_id
                        else ""
                    ),
                    (key_id,) if key_id else (),
                ).fetchone()[0]
                summaries = self._conn.execute(
                    "SELECT count(*) FROM summaries s"
                    + (
                        " JOIN conversations c ON c.id = s.conversation_id"
                        " WHERE c.key_id = ?"
                        if key_id
                        else ""
                    ),
                    (key_id,) if key_id else (),
                ).fetchone()[0]
                compactions = self._conn.execute(
                    "SELECT count(*) FROM compaction_records r"
                    + (
                        " JOIN conversations c ON c.id = r.conversation_id"
                        " WHERE c.key_id = ?"
                        if key_id
                        else ""
                    ),
                    (key_id,) if key_id else (),
                ).fetchone()[0]
                projects = self._conn.execute(
                    f"SELECT count(*) FROM project_state{scope}",
                    (key_id,) if key_id else (),
                ).fetchone()[0]
                replays = self._conn.execute(
                    "SELECT count(*) FROM resume_replays r"
                    + (
                        " JOIN conversations c ON c.id = r.conversation_id"
                        " WHERE c.key_id = ?"
                        if key_id
                        else ""
                    ),
                    (key_id,) if key_id else (),
                ).fetchone()[0]
        except Exception:  # noqa: BLE001 - diagnostics are best-effort
            return zero

        return {
            "conversations": sum(row[1] for row in conv),
            "active": sum(row[1] for row in conv if row[0] == "active"),
            "archived": sum(row[1] for row in conv if row[0] == "archived"),
            "turns": turns,
            "summaries": summaries,
            "compactions": compactions,
            "projects": projects,
            "replays": replays,
        }

    # ============================
    # Internals
    # ============================

    def _validate_scope(self, key_id, client_bucket, project_key):
        key_id = (key_id or "").strip()
        project_key = (project_key or "").strip()

        if not key_id or len(key_id) > _MAX_ID_LENGTH:
            raise MalformedInputError("invalid key id")
        if not project_key or len(project_key) > _MAX_ID_LENGTH:
            raise MalformedInputError("invalid project key")

        client_bucket = (client_bucket or "other").strip().lower()
        if client_bucket not in _VALID_BUCKETS:
            client_bucket = "other"

        return key_id, client_bucket, project_key

    def _validate_id(self, conversation_id):
        conversation_id = (conversation_id or "").strip()

        if not conversation_id:
            return None
        if len(conversation_id) > _MAX_ID_LENGTH:
            raise MalformedInputError("invalid conversation id")

        return conversation_id

    @staticmethod
    def _conversation_row(row) -> dict:
        return {
            "id": row[0],
            "key_id": row[1],
            "client_bucket": row[2],
            "project_key": row[3],
            "status": row[4],
            "model_chain": json.loads(row[5] or "[]"),
            "token_budget": row[6],
            "created_at": row[7],
            "updated_at": row[8],
            "last_turn_ts": row[9],
        }

    def _last_summary(self, conversation_id, key_id) -> dict:
        summaries = self.summaries(conversation_id, key_id, limit=1)
        return summaries[0] if summaries else {}

    def _audit(self, action, *, actor, target, outcome, detail=None) -> None:
        """
        Best-effort continuity audit row. Never raises and never changes
        the store operation's outcome.
        """
        from app.services import event_log as event_log_module

        try:
            event_log_module.event_log().emit(
                action,
                actor=actor,
                target=target,
                outcome=outcome,
                detail=detail or {},
            )
        except Exception:  # noqa: BLE001 - audit must never break the store
            _logger.debug("continuity audit row failed: %s", action)

    def _require_open(self) -> sqlite3.Connection:
        if not self._ensure_open():
            raise OSError("conversation store is not available")
        return self._conn

    def _ensure_open(self) -> bool:
        if self._conn is not None:
            return True

        with self._lock:
            if self._conn is None:
                self._open()

        return self._conn is not None

    def _open(self) -> None:
        try:
            self._conn = platform_store.open_connection(self._path)
            self._open_attempts += 1
        except Exception:
            self._conn = None
            self._open_attempts += 1
            self._open_errors += 1


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex


__all__ = [
    "ConversationStore",
    "ConversationStoreError",
    "ConversationRecord",
    "ConversationScope",
    "ConversationStatus",
    "MalformedInputError",
    "TurnOutcome",
]
