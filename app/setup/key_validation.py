"""
API-key entry, live validation, and classification for the setup wizard.

Validation hits the provider's real ``GET /models`` (or equivalent) through
``client.key_check``. Failures are bucketed into a small category set so the
wizard can show a specific reason, and the retry/skip loop terminates the
moment the user asks to skip. Keys are never echoed: only a masked suffix
is ever shown.
"""

from dataclasses import dataclass

from app.providers.availability import AVAILABLE, GLYPH, UNAVAILABLE

# Body fragments that make a 401/403 look like a dead/expired credential
# rather than a plain wrong one.
_EXPIRED_PATTERNS = (
    "expired",
    "revoked",
    "invalid",
)

_QUOTA_PATTERNS = (
    "quota",
    "insufficient_quota",
    "billing",
    "usage limit",
    "rate limit",
    "exceeded",
)


@dataclass(frozen=True)
class KeyValidation:
    """
    Result of one key-validation request.
    """

    ok: bool
    category: str  # "auth_error" | "expired" | "quota" | "unavailable" | "ok"
    message: str


@dataclass(frozen=True)
class KeyOutcome:
    """
    How key entry ended: a validated key, or the user skipped the provider.
    """

    action: str  # "ok" | "skipped"
    api_key: str = ""


def mask_key(key: str) -> str:
    """
    Render a key as ``********abcd``; short keys are fully masked.
    """
    if len(key) <= 4:
        return "*" * len(key)
    return f"{'*' * 8}{key[-4:]}"


def _body_matches(body: str, patterns: tuple) -> bool:
    lowered = (body or "").lower()
    return any(pattern in lowered for pattern in patterns)


def classify(status_code: int | None, body: str) -> KeyValidation:
    """
    Map a ``(status_code, body)`` pair to a ``KeyValidation``.
    """
    if status_code == 200:
        return KeyValidation(True, "ok", "Authentication successful")

    if status_code is None:
        return KeyValidation(
            False,
            "unavailable",
            body or "provider is unreachable",
        )

    if status_code in (401, 403):
        if _body_matches(body, _EXPIRED_PATTERNS):
            return KeyValidation(
                False,
                "expired",
                "API key is invalid or has expired.",
            )
        return KeyValidation(
            False,
            "auth_error",
            "The API key was rejected (HTTP 401/403).",
        )

    if status_code in (402, 429) or _body_matches(body, _QUOTA_PATTERNS):
        return KeyValidation(
            False,
            "quota",
            "Quota exceeded or rate limited by the provider.",
        )

    return KeyValidation(
        False,
        "unavailable",
        f"Provider returned HTTP {status_code}.",
    )


def validate_key(client, provider) -> KeyValidation:
    """
    Run a live key check against the provider's catalog endpoint.
    """
    try:
        status_code, body = client.key_check(provider)
    except Exception as exc:  # noqa: BLE001 - any failure is "unavailable"
        return KeyValidation(False, "unavailable", str(exc))

    return classify(status_code, body or "")


def resolve_cloud_key(ui, defn, client, provider, current_key: str = "") -> KeyOutcome:
    """
    Full key entry + validation loop for one cloud provider.

    An existing key is offered first (validated before reuse). Otherwise the
    user is prompted, validated live, and on failure gets a specific reason
    plus a retry/skip choice. Returns a validated key or ``skipped``.
    """
    if current_key and ui.ask_yes_no(
        f"Existing API key detected ({mask_key(current_key)}). Keep it?",
        True,
    ):
        provider.api_key = current_key
        ui.notice("  Validating key...")
        validation = validate_key(client, provider)

        if validation.ok:
            ui.notice(f"  {GLYPH[AVAILABLE]} Authentication successful")
            return KeyOutcome("ok", current_key)

        ui.notice(
            f"  {GLYPH[UNAVAILABLE]} Existing key is invalid: "
            f"{validation.message}"
        )

    while True:
        key = ui.ask(
            f"API key for {defn.display_name} (blank to skip)"
        ).strip()

        if not key:
            return KeyOutcome("skipped")

        provider.api_key = key
        ui.notice("  Validating key...")
        validation = validate_key(client, provider)

        if validation.ok:
            ui.notice(f"  {GLYPH[AVAILABLE]} Authentication successful")
            return KeyOutcome("ok", key)

        ui.notice(f"  {GLYPH[UNAVAILABLE]} Invalid API key")
        ui.notice(f"  Reason: {validation.message}")

        if ui.retry_or_skip("[R]etry or [S]kip this provider").startswith("s"):
            return KeyOutcome("skipped")
