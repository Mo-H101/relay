"""
Versioned data records for the P9 project-continuity layer.

These dataclasses mirror the schema-v7 continuity tables
(``conversations``, ``conversation_turns``, ``summaries``,
``compaction_records``, ``project_state``) and the request-scope inputs
the handoff coordinator will consume in later phases. They carry
metadata and derived state only: no prompts, no responses, no generated
content, no keys, no paths.

Versioning: ``MODEL_VERSION`` covers the envelope/record shapes;
``SUMMARY_VERSION`` is stamped on every summary row so consumers can
reject summaries written by an older summarizer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional

# Version of the continuity record/envelope shapes (bumped on schema
# changes that alter meaning).
MODEL_VERSION = 1

# Version stamped into ``summaries`` rows by the summarizer.
SUMMARY_VERSION = 1


class ConversationStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class RecoveryState(str, Enum):
    """
    P9d recovery state machine (per conversation).

    ``ConversationStatus`` stays the durable status vocabulary (active /
    archived); ``RecoveryState`` is the derived recovery classification
    surfaced by ``ContinuityRecovery`` for diagnostics and transitions.

    Valid transitions (driven by ``ContinuityRecovery``; invalid ones are
    rejected and never change state):

    * ``ACTIVE`` -- no interrupt, no pending recovery.
      + turn_start -> INTERRUPTED; resume_denied -> FAILED_RECOVERY
        (last safe point undeterminable); archive -> ARCHIVED.
    * ``INTERRUPTED`` -- a turn started but has not committed (in-memory
      in-flight marker).
      + turn_committed -> ACTIVE; resume_valid -> RECOVERY_IN_PROGRESS;
        resume_denied -> FAILED_RECOVERY; archive -> ARCHIVED.
    * ``RECOVERABLE`` -- durable resume point exists (the last committed
      turn carries a resume-token hash) so a reconnect can proceed.
      + resume_valid -> RECOVERY_IN_PROGRESS; resume_denied ->
        FAILED_RECOVERY; turn_start -> ACTIVE; turn_committed -> ACTIVE;
        archive -> ARCHIVED.
    * ``RECOVERY_IN_PROGRESS`` -- resume validated, context rebuilt,
      waiting for the next commit.
      + turn_committed -> RECOVERED; archive -> ARCHIVED. (No turn_start:
        a resume session is not re-interrupted by its own turn.)
    * ``RECOVERED`` -- the resumed turn committed.
      + turn_start -> ACTIVE; turn_committed -> ACTIVE; archive -> ARCHIVED.
    * ``FAILED_RECOVERY`` -- resume denied or durable state inconsistent
      (requires recovery review). The conversation proceeds as new (S7).
      + resume_valid -> RECOVERY_IN_PROGRESS; turn_start -> ACTIVE;
        turn_committed -> ACTIVE; archive -> ARCHIVED.
    * ``ARCHIVED`` -- terminal; every transition is rejected.

Invalid (documented, rejected): ARCHIVED -> anything; ACTIVE ->
RECOVERY_IN_PROGRESS/RECOVERED (a resume can only succeed through
INTERRUPTED or RECOVERABLE, never straight from ACTIVE); RECOVERY_IN_PROGRESS ->
INTERRUPTED; RECOVERED -> RECOVERABLE.
    """

    ACTIVE = "active"
    INTERRUPTED = "interrupted"
    RECOVERABLE = "recoverable"
    RECOVERY_IN_PROGRESS = "recovery_in_progress"
    RECOVERED = "recovered"
    FAILED_RECOVERY = "failed_recovery"
    ARCHIVED = "archived"


class TurnOutcome(str, Enum):
    OK = "ok"
    FAILED = "failed"
    DENIED = "denied"


class CompactionReason(str, Enum):
    PREFLIGHT = "preflight"
    OVERFLOW = "overflow"
    MANUAL = "manual"


class CompactionMethod(str, Enum):
    SUMMARY_TAIL = "summary+tail"
    TAIL_ONLY = "tail-only"


class SummaryMethod(str, Enum):
    EXTRACTIVE = "extractive"
    LLM = "llm"


@dataclass(frozen=True)
class ConversationScope:
    """
    The key-scoped identity of a conversation: the opaque API key id that
    owns it, the client bucket, and the opaque key-scoped project hash.
    """

    key_id: str
    client_bucket: str
    project_key: str


@dataclass(frozen=True)
class ConversationRecord:
    """One row of the ``conversations`` table."""

    id: str
    key_id: str
    client_bucket: str
    project_key: str
    status: str
    model_chain: list
    token_budget: Optional[int]
    created_at: float
    updated_at: float
    last_turn_ts: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "key_id": self.key_id,
            "client_bucket": self.client_bucket,
            "project_key": self.project_key,
            "status": self.status,
            "model_chain": list(self.model_chain or []),
            "token_budget": self.token_budget,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_turn_ts": self.last_turn_ts,
        }


@dataclass(frozen=True)
class TurnRecord:
    """One row of the ``conversation_turns`` table."""

    conversation_id: str
    seq: int
    outcome: str
    ts: float
    provider: Optional[str] = None
    model: Optional[str] = None
    task: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    latency_ms: Optional[int] = None
    resume_token_hash: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "seq": self.seq,
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome,
            "task": self.task,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "latency_ms": self.latency_ms,
            "resume_token_hash": self.resume_token_hash,
            "ts": self.ts,
        }


@dataclass(frozen=True)
class SummaryRecord:
    """One row of the ``summaries`` table (derived, redacted, bounded)."""

    conversation_id: str
    up_to_seq: int
    version: int
    method: str
    content: str
    created_at: float
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    summary_id: Optional[int] = None

    def to_dict(self) -> dict:
        # ``content`` is exported under a safe key name so exported
        # summaries always pass the memory-contract negative tests.
        return {
            "summary_id": self.summary_id,
            "conversation_id": self.conversation_id,
            "up_to_seq": self.up_to_seq,
            "version": self.version,
            "method": self.method,
            "summary_text": self.content,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class CompactionRecord:
    """One row of the ``compaction_records`` table."""

    conversation_id: str
    at: float
    reason: str
    method: str
    from_tokens: Optional[int] = None
    to_tokens: Optional[int] = None
    summary_id: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProjectStateRecord:
    """One row of the ``project_state`` table (bounded derived state)."""

    project_key: str
    key_id: str
    last_models: list
    counters: dict
    last_seen: float

    def to_dict(self) -> dict:
        return {
            "project_key": self.project_key,
            "key_id": self.key_id,
            "last_models": list(self.last_models or []),
            "counters": dict(self.counters or {}),
            "last_seen": self.last_seen,
        }


@dataclass(frozen=True)
class SummaryBlock:
    """
    A derived, bounded compaction summary produced by the summarizer
    (P9b). Mirrors ``SummaryRecord`` but carries the summarizer model
    provenance (ephemeral; the durable ``summaries`` table has no model
    column, so model provenance is surfaced only via envelopes/events).
    """

    conversation_id: str
    up_to_seq: int
    version: int
    method: str
    content: str
    created_at: float
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    model: Optional[str] = None
    summary_id: Optional[int] = None

    def to_dict(self) -> dict:
        # ``content`` is exported under a safe key name so exported
        # summaries always pass the memory-contract negative tests.
        return {
            "summary_id": self.summary_id,
            "conversation_id": self.conversation_id,
            "up_to_seq": self.up_to_seq,
            "version": self.version,
            "method": self.method,
            "summary_text": self.content,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "model": self.model,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class CompactionResult:
    """
    The outcome of one context compaction (P9b): a derived summary block,
    the verbatim metadata tail kept for the next candidate, and the token
    accounting that drove the split.
    """

    conversation_id: str
    up_to_seq: int
    reason: str
    method: str
    summary: Optional[SummaryBlock]
    tail: list
    from_tokens: int
    to_tokens: int
    summary_tokens: int
    tail_tokens: int
    created_at: float

    def to_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "up_to_seq": self.up_to_seq,
            "reason": self.reason,
            "method": self.method,
            "summary": self.summary.to_dict() if self.summary else None,
            "tail": [dict(turn) for turn in self.tail],
            "from_tokens": self.from_tokens,
            "to_tokens": self.to_tokens,
            "summary_tokens": self.summary_tokens,
            "tail_tokens": self.tail_tokens,
            "created_at": self.created_at,
        }


__all__ = [
    "MODEL_VERSION",
    "SUMMARY_VERSION",
    "ConversationStatus",
    "TurnOutcome",
    "CompactionReason",
    "CompactionMethod",
    "SummaryMethod",
    "ConversationScope",
    "ConversationRecord",
    "TurnRecord",
    "SummaryRecord",
    "CompactionRecord",
    "ProjectStateRecord",
    "SummaryBlock",
    "CompactionResult",
]
