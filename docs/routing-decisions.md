# How Relay Picks a Provider and Model

Relay builds an ordered list of `(provider, model)` candidates per
request, then tries them in order with retry and failover. This document
explains that ordering.

## 1. Provider ranking

`ProviderManager.ranked()` orders the registered providers by their
configured `priority`. Providers can be disabled (`enabled: false`) or
excluded for missing credentials; disabled providers never appear in the
candidate list.

## 2. Candidate set

`CandidateBuilder.build()` turns the ranked providers into concrete
model candidates:

- **Task routing enabled** (`TASK_ROUTING_ENABLED`): the routing engine
  resolves the request task (one of `coding`, `vision`, `reasoning`,
  `general`, `creative`, `translation`) against the `TASK_*` model
  preference lists. With `CROSS_PROVIDER_MODEL_SELECTION`, a bare model
  id becomes a candidate on every provider that serves it. The task's
  preferred models define the candidate set and its order.
- **Otherwise**: every chat-capable model across the providers, in
  provider/model order.

## 3. Health bands

When `HEALTH_AWARE_ROUTING` is enabled, candidates are filtered and
ordered by health band, which is the **primary** ordering key:

| Band | Meaning |
| --- | --- |
| `HEALTHY` (0) | Healthy and recently verified. |
| `DEGRADED` (1) | Degraded (timeouts/429s) or learned degraded. |
| `NOT_CHECKED` (2) | No health data yet. |
| `UNAVAILABLE` (3) | Unavailable or learned unavailable. |

Models marked unavailable/unsupported by a health report or by learned
feedback state are removed. If filtering would remove every candidate,
the original order is returned so the request still has a path forward.

Within a band, ordering depends on what signal data exists:

- No telemetry or quality signal: candidates stay in their natural
  (task-preference/priority) order, sorted only by band.
- Otherwise candidates are passed to the **scorer**.

## 4. Within-band scoring

The scorer produces a normalized score per candidate from weighted
signals. Every weight defaults to `1.0` (except the cost placeholder,
which defaults to `0.0`), and each signal is feature-gated so enabling
features is additive:

- **Priority** — the provider's configured `priority`.
- **Telemetry success rate** — from `TELEMETRY_ENABLED` attempt history.
- **Telemetry latency** — normalized against `SCORING_LATENCY_REF_MS`.
- **Recent failures** — normalized against `SCORING_FAILURE_REF_COUNT`.
- **Task preference** — position in the task's preferred model list.
- **Task compatibility** — only when `TASK_CATALOG_ENABLED` (model
  capability catalog for the task).
- **Quality EWMA** — only when `QUALITY_FEEDBACK_ENABLED` and the pair
  has at least `QUALITY_FEEDBACK_MIN_SAMPLES` ratings.
- **Adaptive EWMA reliability/latency** — only when
  `ADAPTIVE_ROUTING_ENABLED` and the pair has at least
  `ADAPTIVE_MIN_SAMPLES` observations; a cold pair resolves neutral.

The health band is never overridden by these within-band signals:
adaptive/quality/telemetry reorder *within* a band, and learned
degraded/unavailable state can demote a provider or remove a model
entirely.

## 5. Decision engine

When `DECISION_ENGINE_ENABLED`, the same ordered candidates are scored
explicitly into `DecisionScore` objects with per-signal contributions and
an overall confidence. Selection stays identical to the hot-path first
candidate; the engine adds explainability and decision statistics.
`GET /decision/explain` (when `DECISION_EXPLANATIONS_ENABLED`) reports
the most recent ranking, the per-signal breakdown, and why each candidate
ranked where it did.

## 6. Attempt and failover

`ChatService.chat_across()` walks the ordered candidates:

- Each `(provider, model)` gets up to `MAX_RETRIES + 1` attempts.
- Failures are classified (`timeout`, `rate_limit`, `quota_exhausted`,
  `invalid_request`, `server_error`, `auth_error`, `unknown`).
- Timeouts, rate limits, server errors, and unknown failures are
  **retryable**. Auth and quota-exhausted failures are **provider-level**:
  the whole provider is skipped for the request.
- On success, the result records which candidate won, why it fell back
  (the most recent failed attempt's reason), and per-attempt telemetry.
- Between retries of the same candidate, Relay may wait: the provider's
  `Retry-After` (honored when `RETRY_HONOR_RETRY_AFTER=true`, capped) or an
  exponential backoff (`RETRY_BACKOFF_BASE_SECONDS`). When
  `REQUEST_TIMEOUT_BUDGET_SECONDS` is set, retries and waits never exceed
  the remaining budget. With defaults (all off), retries are immediate —
  see [known-limitations.md](known-limitations.md) item 1.

Telemetry (when enabled) and health feedback (when enabled) record every
attempt, so future requests route around what just failed — until the
degraded/unavailable marks expire or succeed again.

## 7. Cold start

With no health, telemetry, or quality data, ordering reduces to
configured priority and task preference — deterministic and predictable.
Learned signals kick in only after enough real observations
(`ADAPTIVE_MIN_SAMPLES`, `QUALITY_FEEDBACK_MIN_SAMPLES`) or after
feedback thresholds are crossed (`HEALTH_FEEDBACK_*_THRESHOLD`).
