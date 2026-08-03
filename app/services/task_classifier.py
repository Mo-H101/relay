"""
Deterministic keyword-based task classification (Phase 7B).

Given a free-text message, classify it into one of the routing task
categories. The classifier is a pure function: it never stores the
message, never touches any memory store, and returns the same result for
the same input.

Ambiguous or weak matches fall back to "general". A message is never
classified outside the routing task categories, so the result is always
safe to feed into routing and candidate building.
"""

from typing import Tuple

from app.services.routing import TASK_CATEGORIES

DEFAULT_THRESHOLD = 0.6

GENERAL = "general"

TASK_KEYWORDS = {
    "coding": (
        "code",
        "coding",
        "function",
        "bug",
        "debug",
        "python",
        "javascript",
        "typescript",
        "sql",
        "algorithm",
        "regex",
        "script",
        "refactor",
        "compile",
        "exception",
        "stack trace",
        "unit test",
    ),
    "vision": (
        "image",
        "vision",
        "visual",
        "photo",
        "picture",
        "ocr",
        "diagram",
        "detect",
        "caption",
        "pixel",
    ),
    "reasoning": (
        "reason",
        "logic",
        "math",
        "solve",
        "puzzle",
        "proof",
        "deduce",
        "infer",
        "probability",
        "hypothesis",
        "analysis",
    ),
    "creative": (
        "write a story",
        "story about",
        "poem",
        "creative",
        "novel",
        "dialogue",
        "lyrics",
        "metaphor",
        "imagine",
        "fiction",
    ),
    "translation": (
        "translate",
        "translation",
        "into french",
        "into spanish",
        "into japanese",
        "into german",
        "translate to",
    ),
    GENERAL: (),
}


def classify_task_with_confidence(
    message: str,
    threshold: float = DEFAULT_THRESHOLD,
) -> Tuple[str, float]:
    """
    Classify a message into a task category with a confidence in [0, 1].

    Confidence is the dominance of the best-matching category (its hit
    count over the total hit count). When no category matches, or the
    best category is not dominant enough, the message falls back to
    "general".
    """
    text = (message or "").lower()
    matches: dict = {}

    for category in TASK_CATEGORIES:
        if category == GENERAL:
            continue

        count = sum(
            1 for keyword in TASK_KEYWORDS[category] if keyword in text
        )

        if count:
            matches[category] = count

    if not matches:
        return GENERAL, 0.0

    best = None
    best_count = 0

    for category, count in matches.items():
        if count > best_count:
            best = category
            best_count = count

    confidence = best_count / sum(matches.values())

    if confidence < threshold:
        return GENERAL, confidence

    return best, confidence


def classify_task(
    message: str,
    threshold: float = DEFAULT_THRESHOLD,
) -> str:
    """
    Classify a message into one of the routing task categories.
    """
    task, _ = classify_task_with_confidence(message, threshold)
    return task
