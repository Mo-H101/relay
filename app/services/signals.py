"""
Declarative registry of scoring signals.

Each signal normalizes raw input to a [0, 1] score that is combined with
a weight into a single fitness value. The registry is the single source
of truth for signal keys, neutral defaults, weight attribute names, and
feature-flag toggles, so the scorer, the candidate builder, and the
explanation service never hardcode signal names in three places.

New signals are added by appending to SIGNALS. The scorer always emits a
breakdown entry for every registered signal; disabled signals emit a
zero contribution so flag-off behavior stays byte-identical to the
legacy formula.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    """
    Metadata for one scoring signal.

    ``enabled_attr`` names a settings field that gates the signal; when
    the field is absent or false, the signal contributes zero regardless
    of its weight or score. Signals without an ``enabled_attr`` are
    always on.
    """

    key: str
    weight_attr: str
    label: str
    neutral: float
    enabled_attr: str | None = None


SIGNALS = (
    Signal(
        key="priority",
        weight_attr="priority_weight",
        label="priority contribution",
        neutral=0.0,
    ),
    Signal(
        key="success",
        weight_attr="success_weight",
        label="success-rate contribution",
        neutral=0.5,
    ),
    Signal(
        key="latency",
        weight_attr="latency_weight",
        label="latency contribution",
        neutral=0.5,
    ),
    Signal(
        key="failure",
        weight_attr="failure_weight",
        label="failure penalty",
        neutral=1.0,
    ),
    Signal(
        key="preference",
        weight_attr="preference_weight",
        label="preference contribution",
        neutral=0.5,
    ),
    Signal(
        key="task_compatibility",
        weight_attr="task_compatibility_weight",
        label="task-compatibility contribution",
        neutral=0.5,
        enabled_attr="task_catalog_enabled",
    ),
    Signal(
        key="adaptive_reliability",
        weight_attr="adaptive_reliability_weight",
        label="adaptive reliability contribution",
        neutral=0.5,
        enabled_attr="adaptive_routing_enabled",
    ),
    Signal(
        key="adaptive_latency",
        weight_attr="adaptive_latency_weight",
        label="adaptive latency contribution",
        neutral=0.5,
        enabled_attr="adaptive_routing_enabled",
    ),
    Signal(
        key="quality",
        weight_attr="quality_weight",
        label="quality-feedback contribution",
        neutral=0.5,
        enabled_attr="quality_feedback_enabled",
    ),
    Signal(
        key="cost",
        weight_attr="cost_weight",
        label="cost contribution",
        neutral=0.5,
    ),
)

SIGNAL_KEYS = tuple(signal.key for signal in SIGNALS)

_BY_KEY = {signal.key: signal for signal in SIGNALS}


def signal_for(key: str) -> Signal:
    """
    Return the registered signal metadata for a key.
    """
    return _BY_KEY[key]


def weight_attr_for(key: str) -> str:
    """
    Name of the scorer attribute holding the signal's baseline weight.
    """
    return _BY_KEY[key].weight_attr


def label_for(key: str) -> str:
    """
    Human-readable label for a signal key.
    """
    return _BY_KEY[key].label


def neutral_for(key: str) -> float:
    """
    Neutral score a signal takes when its input is unknown.
    """
    return _BY_KEY[key].neutral


def enabled_attr_for(key: str) -> str | None:
    """
    Settings field gating the signal, or None when always enabled.
    """
    return _BY_KEY[key].enabled_attr
