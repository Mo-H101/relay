"""
Memory contract for Relay (Phase 0).

Classifies every piece of state Relay holds into one of three classes:

- durable: persisted across restarts (SQLite via StateStore).
- ephemeral: process-lifetime only (in-memory stores, logs, response
  headers, correlation ids, task classifications).
- never: must never be captured anywhere (prompts, responses, API keys,
  proxy credentials, user identity, generated content).

This mapping is the single source of truth for what may be persisted.
Negative tests use ``contains_never_captured()`` to assert that store
exports, ops events, metric renders, and log payloads stay free of
forbidden keys and content.
"""

from enum import Enum
from typing import Any


class MemoryClass(str, Enum):
    """
    One of the three memory classes in the contract.
    """

    DURABLE = "durable"
    EPHEMERAL = "ephemeral"
    NEVER = "never"


MEMORY_SURFACES = {
    # Durable: persisted across restarts.
    "state_store": MemoryClass.DURABLE,
    "state_flusher": MemoryClass.DURABLE,
    "learned_health_feedback": MemoryClass.DURABLE,
    "telemetry_aggregates": MemoryClass.DURABLE,
    "telemetry_failure_history": MemoryClass.DURABLE,
    "adaptive_routing_learning": MemoryClass.DURABLE,
    "quality_feedback_aggregates": MemoryClass.DURABLE,
    "decision_stats": MemoryClass.DURABLE,
    # P9 project continuity: metadata-only surfaces on the shared
    # platform.db (schema v7, plus the v8 resume_replays tracker).
    # Raw prompts/responses are never stored.
    "conversation_store": MemoryClass.DURABLE,
    "continuity_flusher": MemoryClass.DURABLE,
    "conversations": MemoryClass.DURABLE,
    "conversation_turns": MemoryClass.DURABLE,
    "summaries": MemoryClass.DURABLE,
    "compaction_records": MemoryClass.DURABLE,
    "project_state": MemoryClass.DURABLE,
    "resume_replays": MemoryClass.DURABLE,
    # Ephemeral: process-lifetime only.
    "health_snapshots": MemoryClass.EPHEMERAL,
    "ops_store": MemoryClass.EPHEMERAL,
    "metrics": MemoryClass.EPHEMERAL,
    "logs": MemoryClass.EPHEMERAL,
    "correlation_ids": MemoryClass.EPHEMERAL,
    "task_classifications": MemoryClass.EPHEMERAL,
    "decision_scores": MemoryClass.EPHEMERAL,
    "decision_explanations": MemoryClass.EPHEMERAL,
    # Never: forbidden anywhere, in any memory class.
    "prompts": MemoryClass.NEVER,
    "responses": MemoryClass.NEVER,
    "generated_content": MemoryClass.NEVER,
    "api_keys": MemoryClass.NEVER,
    "proxy_credentials": MemoryClass.NEVER,
    "user_identity": MemoryClass.NEVER,
}

# Keys that must never appear in any recorded output, regardless of the
# surrounding context. Matched case-insensitively by
# contains_never_captured().
FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "prompts",
        "prompt_text",
        "message",
        "messages",
        "user_message",
        "response",
        "responses",
        "model_response",
        "content",
        "api_key",
        "api-key",
        "apikey",
        "authorization",
        "proxy",
        "proxy_url",
        "password",
        "secret",
        "secret_value",
        "user_identity",
        "identity",
    }
)


def contains_never_captured(data: Any) -> bool:
    """
    Recursively search ``data`` for a forbidden key.

    Returns True when any dict key at any nesting depth is a forbidden
    key name, so negative tests can assert that exports, events, renders,
    and log payloads never carry content-shaped fields.
    """
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                return True

            if contains_never_captured(value):
                return True

        return False

    if isinstance(data, (list, tuple, set, frozenset)):
        return any(contains_never_captured(item) for item in data)

    return False
