"""
Metadata-only quality feedback store (Phase 7D).

QualityStore aggregates optional user quality feedback for
(provider, model) pairs into bounded, in-memory statistical summaries:
sample counts, positive/negative tallies, an EWMA score, and a confidence
ramp. It never stores prompts, responses, generated content, API keys, or
user identity. Raw feedback is never exposed; only aggregates are.

Learning is noise-resistant by construction:
- ratings are clamped to the allowed range,
- the EWMA learning rate is capped to [0, 1] so no single rating can move
  the estimate by more than its full weight,
- duplicate feedback for the same (provider, model, correlation id) is
  ignored, so double-submits and retry storms cannot inflate a pair,
- aggregates are bounded by a retention limit.

Routing integration: a pair's quality estimate is only exposed once it
has at least min_samples confident samples; below that the signal
resolves to neutral for every candidate, so sparse or noisy feedback
never steers ordering. Quality only ever reorders within an existing
health band; it never overrides health safety or operational reliability.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Optional
import threading
import time

_RATING_MIN = 1
_RATING_MAX = 5
_MAX_LEARNING_RATE = 1.0


@dataclass(frozen=True)
class QualitySignal:
    """
    Confidence-gated quality estimate for one (provider, model) pair.

    ``score`` is the EWMA rating normalized to [0, 1], or None until the
    pair has enough confident samples (min_samples). ``confidence`` ramps
    linearly from 0 to 1 as the sample count approaches min_samples.
    """

    provider: str
    model: str
    sample_count: int
    confidence: float
    score: Optional[float]


@dataclass
class _Aggregate:
    """
    Mutable per-pair running summary. Metadata only.
    """

    sample_count: int = 0
    positive_count: int = 0
    negative_count: int = 0
    ewma_score: Optional[float] = None
    categories: Dict[str, int] = field(default_factory=dict)
    last_updated: float = 0.0


def _normalize_rating(rating) -> int:
    """
    Clamp a rating to the allowed [1, 5] range.
    """
    try:
        value = int(rating)
    except (TypeError, ValueError):
        return _RATING_MIN
    return max(_RATING_MIN, min(_RATING_MAX, value))


def _opt_float(value) -> Optional[float]:
    """
    Coerce a persisted numeric value to float, tolerating None/absent
    keys so older exports (without a score) still import cleanly.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class QualityStore:
    """
    Thread-safe, bounded, in-memory quality feedback store.

    Keyed by (provider, model). Retention is bounded by a maximum number
    of distinct aggregates; the least-recently-updated pair is evicted
    when the bound is exceeded. Deduplicated by correlation id per pair,
    with the dedupe ledger itself bounded by the retention limit.
    """

    def __init__(
        self,
        min_samples: int = 10,
        learning_rate: float = 0.1,
        retention_limit: int = 10000,
        now=None,
    ) -> None:
        self._min_samples = max(1, int(min_samples))
        self._learning_rate = min(
            _MAX_LEARNING_RATE, max(0.0, float(learning_rate))
        )
        self._retention_limit = max(1, int(retention_limit))
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._aggregates: "OrderedDict[tuple, _Aggregate]" = OrderedDict()
        self._seen: "OrderedDict[tuple, None]" = OrderedDict()

    def set_alpha(self, alpha: float) -> None:
        """
        Update the EWMA learning rate applied to future ratings. The
        value is capped to [0, 1]; existing estimates are unchanged.
        """
        with self._lock:
            self._learning_rate = min(
                _MAX_LEARNING_RATE, max(0.0, float(alpha))
            )

    def set_min_samples(self, min_samples: int) -> None:
        """
        Update the confidence gate applied to future reads.
        """
        with self._lock:
            self._min_samples = max(1, int(min_samples))

    def set_retention_limit(self, limit: int) -> None:
        """
        Update the aggregate bound, evicting least-recently-updated pairs
        if the new bound is smaller than the current size.
        """
        with self._lock:
            self._retention_limit = max(1, int(limit))
            self._evict()

    def record(
        self,
        provider: str,
        model: str,
        rating: int,
        category: str | None = None,
        correlation_id: str | None = None,
    ) -> bool:
        """
        Record one quality rating for a (provider, model) pair.

        Returns False when the rating is a duplicate for the same
        correlation id and pair (already recorded), so double-submits do
        not inflate the aggregate. Metadata only: provider, model,
        rating, optional category, and optional correlation id.
        """
        provider = str(provider)
        model = str(model)
        rating = _normalize_rating(rating)
        category = str(category) if category else None
        dedupe_key = (
            (provider, model, str(correlation_id))
            if correlation_id is not None
            else None
        )
        key = (provider, model)
        now = self._now()

        with self._lock:
            if dedupe_key is not None:
                if dedupe_key in self._seen:
                    return False
                self._seen[dedupe_key] = None
                self._seen.move_to_end(dedupe_key)

                while len(self._seen) > self._retention_limit:
                    self._seen.popitem(last=False)

            aggregate = self._aggregates.get(key)

            if aggregate is None:
                aggregate = _Aggregate(last_updated=now)
                self._aggregates[key] = aggregate
            else:
                self._aggregates.move_to_end(key)

            aggregate.sample_count += 1
            aggregate.last_updated = now

            if rating >= 4:
                aggregate.positive_count += 1
            elif rating <= 2:
                aggregate.negative_count += 1

            if category:
                aggregate.categories[category] = (
                    aggregate.categories.get(category, 0) + 1
                )

            score = (rating - _RATING_MIN) / (_RATING_MAX - _RATING_MIN)

            if aggregate.ewma_score is None:
                aggregate.ewma_score = score
            else:
                aggregate.ewma_score += self._learning_rate * (
                    score - aggregate.ewma_score
                )

            self._evict()
            return True

    def quality_signal(
        self,
        provider: str,
        model: str,
    ) -> Optional[QualitySignal]:
        """
        Confidence-gated quality estimate for a pair, or None when the
        pair has no feedback.
        """
        key = (str(provider), str(model))

        with self._lock:
            aggregate = self._aggregates.get(key)

            if aggregate is None:
                return None

            confidence = min(1.0, aggregate.sample_count / self._min_samples)
            score = (
                aggregate.ewma_score
                if aggregate.sample_count >= self._min_samples
                else None
            )

            return QualitySignal(
                provider=key[0],
                model=key[1],
                sample_count=aggregate.sample_count,
                confidence=round(confidence, 4),
                score=round(score, 4) if score is not None else None,
            )

    def aggregate(self, provider: str, model: str) -> Optional[dict]:
        """
        Diagnostics view for one pair, or None when the pair has no
        feedback.
        """
        signal = self.quality_signal(provider, model)

        if signal is None:
            return None

        key = (str(provider), str(model))

        with self._lock:
            aggregate = self._aggregates[key]
            positive_rate = (
                aggregate.positive_count / aggregate.sample_count
                if aggregate.sample_count
                else None
            )

            return {
                "provider": key[0],
                "model": key[1],
                "sample_count": signal.sample_count,
                "positive_count": aggregate.positive_count,
                "negative_count": aggregate.negative_count,
                "neutral_count": max(
                    0,
                    aggregate.sample_count
                    - aggregate.positive_count
                    - aggregate.negative_count,
                ),
                "positive_rate": (
                    round(positive_rate, 4)
                    if positive_rate is not None
                    else None
                ),
                "ewma_score": signal.score,
                "confidence": signal.confidence,
                "categories": dict(aggregate.categories),
            }

    def aggregates(self) -> list:
        """
        Diagnostics view for every pair with feedback, bounded by the
        retention limit.
        """
        with self._lock:
            return [self._view(key, aggregate) for key, aggregate in self._aggregates.items()]

    def stats(self) -> dict:
        """
        Store-level summary for diagnostics. Metadata only.
        """
        with self._lock:
            total_ratings = sum(
                aggregate.sample_count
                for aggregate in self._aggregates.values()
            )
            confident_pairs = sum(
                1
                for aggregate in self._aggregates.values()
                if aggregate.sample_count >= self._min_samples
            )

            return {
                "min_samples": self._min_samples,
                "learning_rate": self._learning_rate,
                "retention_limit": self._retention_limit,
                "pairs": len(self._aggregates),
                "total_ratings": total_ratings,
                "confident_pairs": confident_pairs,
            }

    def export_state(self) -> list:
        """
        Export all aggregates for persistence (Phase 7F).

        Returns a list of per-(provider, model) dicts in StateStore
        format. Metadata only: provider/model identifiers, sample counts,
        tallies, the EWMA score, category tallies, and a wall-clock
        timestamp. Raw feedback content is never exported; the dedupe
        ledger is not persisted (double-submit protection is rebuilt from
        fresh in-memory correlation ids after a restart).
        """
        with self._lock:
            wall_now = time.time()
            mono_now = time.monotonic()
            result = []

            for (provider, model), aggregate in self._aggregates.items():
                last_updated_wall = wall_now
                if aggregate.last_updated > 0:
                    last_updated_wall = wall_now - max(
                        0.0, mono_now - aggregate.last_updated
                    )

                result.append(
                    {
                        "provider": provider,
                        "model": model,
                        "sample_count": aggregate.sample_count,
                        "positive_count": aggregate.positive_count,
                        "negative_count": aggregate.negative_count,
                        "ewma_score": aggregate.ewma_score,
                        "categories": dict(aggregate.categories),
                        "last_updated_wall": last_updated_wall,
                    }
                )

            return result

    def import_state(self, entries: list) -> None:
        """
        Restore aggregates from an export (replacing any existing data).

        Wall-clock timestamps are converted back to monotonic. Aggregates
        without samples are skipped; the dedupe ledger starts empty so a
        fresh correlation id can be recorded again after a restart.
        """
        with self._lock:
            self._aggregates.clear()
            self._seen.clear()
            wall_now = time.time()
            mono_now = time.monotonic()

            for data in entries:
                provider = data.get("provider")
                model = data.get("model")

                if not provider or not model:
                    continue

                sample_count = int(data.get("sample_count", 0))

                if sample_count <= 0:
                    continue

                last_updated = mono_now
                last_updated_wall = _opt_float(data.get("last_updated_wall"))

                if last_updated_wall is not None and last_updated_wall > 0:
                    last_updated = mono_now - max(0.0, wall_now - last_updated_wall)

                categories = data.get("categories") or {}

                aggregate = _Aggregate(
                    sample_count=sample_count,
                    positive_count=int(data.get("positive_count", 0)),
                    negative_count=int(data.get("negative_count", 0)),
                    ewma_score=_opt_float(data.get("ewma_score")),
                    categories={
                        str(key): max(0, int(value))
                        for key, value in categories.items()
                    },
                    last_updated=last_updated,
                )

                self._aggregates[(provider, model)] = aggregate

            self._evict()

    def clear(self) -> None:
        """
        Remove all aggregates and the dedupe ledger.
        """
        with self._lock:
            self._aggregates.clear()
            self._seen.clear()

    def _evict(self) -> None:
        """
        Drop least-recently-updated aggregates until under the bound.
        """
        while len(self._aggregates) > self._retention_limit:
            self._aggregates.popitem(last=False)

    def _view(self, key, aggregate: _Aggregate) -> dict:
        confidence = min(1.0, aggregate.sample_count / self._min_samples)
        score = (
            aggregate.ewma_score
            if aggregate.sample_count >= self._min_samples
            else None
        )
        positive_rate = (
            aggregate.positive_count / aggregate.sample_count
            if aggregate.sample_count
            else None
        )

        return {
            "provider": key[0],
            "model": key[1],
            "sample_count": aggregate.sample_count,
            "positive_count": aggregate.positive_count,
            "negative_count": aggregate.negative_count,
            "neutral_count": max(
                0,
                aggregate.sample_count
                - aggregate.positive_count
                - aggregate.negative_count,
            ),
            "positive_rate": (
                round(positive_rate, 4) if positive_rate is not None else None
            ),
            "ewma_score": round(score, 4) if score is not None else None,
            "confidence": round(confidence, 4),
            "categories": dict(aggregate.categories),
        }


__all__ = ["QualitySignal", "QualityStore"]
