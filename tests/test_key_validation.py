"""
API-key validation classification and the retry/skip loop (P1).
"""

from app.providers.base import Provider
from app.setup.key_validation import (
    KeyOutcome,
    classify,
    mask_key,
    resolve_cloud_key,
    validate_key,
)
from app.setup.ui import ScriptedUI


class FakeClient:
    """
    Returns a fixed (status_code, body) or a scripted sequence of responses;
    the last response is reused once the sequence is exhausted.
    """

    def __init__(self, status_code, body="", sequence=None):
        if sequence is not None:
            self.responses = list(sequence)
        else:
            self.responses = [(status_code, body)]
        self.checked = 0

    def key_check(self, provider):
        self.checked += 1
        status_code, body = self.responses[min(self.checked - 1, len(self.responses) - 1)]
        if isinstance(status_code, Exception):
            raise status_code
        return status_code, body


def make_provider(key="sk-test"):
    return Provider(name="Fake", base_url="http://fake/v1", api_key=key)


def test_classify_ok():
    result = classify(200, "{}")
    assert result.ok
    assert result.category == "ok"


def test_classify_unreachable():
    result = classify(None, "connection refused")
    assert not result.ok
    assert result.category == "unavailable"


def test_classify_auth_error():
    result = classify(401, "bad key")
    assert result.category == "auth_error"


def test_classify_expired_body_detection():
    result = classify(401, "Your API key has expired")
    assert result.category == "expired"


def test_classify_revoked_body_detection():
    result = classify(403, "key revoked")
    assert result.category == "expired"


def test_classify_quota_on_429():
    assert classify(429, "rate limit").category == "quota"


def test_classify_quota_on_402():
    assert classify(402, "payment required").category == "quota"


def test_classify_quota_from_body():
    assert classify(500, "insufficient_quota").category == "quota"


def test_classify_unavailable_on_5xx():
    result = classify(503, "service unavailable")
    assert result.category == "unavailable"


def test_validate_key_ok():
    client = FakeClient(200, "{}")
    result = validate_key(client, make_provider())
    assert result.ok
    assert client.checked == 1


def test_validate_key_unavailable_on_exception():
    client = FakeClient(ValueError("boom"))
    result = validate_key(client, make_provider())
    assert result.category == "unavailable"
    assert "boom" in result.message


def test_mask_key():
    assert mask_key("sk-abcdefgh1234") == "********1234"
    assert mask_key("abc") == "***"


class FakeDefn:
    display_name = "Fake Provider"
    id = "fake"


def test_resolve_keeps_valid_existing_key():
    ui = ScriptedUI(["y"])
    client = FakeClient(200)
    outcome = resolve_cloud_key(ui, FakeDefn(), client, make_provider("old-key"), "old-key")

    assert outcome == KeyOutcome("ok", "old-key")
    assert any("Authentication successful" in n for n in ui.notices)


def test_resolve_rejects_invalid_existing_then_accepts_new():
    ui = ScriptedUI(["y", "new-key"])
    client = FakeClient(200, sequence=[(401, "invalid"), (200, "{}")])
    provider = make_provider("old-key")
    outcome = resolve_cloud_key(ui, FakeDefn(), client, provider, "old-key")

    assert outcome.action == "ok"
    assert outcome.api_key == "new-key"
    assert any("Existing key is invalid" in n for n in ui.notices)


def test_resolve_retries_after_invalid_then_succeeds():
    ui = ScriptedUI(["sk-bad", "r", "sk-good"])
    client = FakeClient(200, sequence=[(401, "invalid key"), (200, "{}")])
    outcome = resolve_cloud_key(ui, FakeDefn(), client, make_provider())

    assert outcome.action == "ok"
    assert outcome.api_key == "sk-good"
    assert any("Invalid API key" in n for n in ui.notices)
    assert any("Reason:" in n for n in ui.notices)


def test_resolve_skips_on_invalid():
    ui = ScriptedUI(["sk-bad", "s"])
    client = FakeClient(401, "invalid key")
    outcome = resolve_cloud_key(ui, FakeDefn(), client, make_provider())

    assert outcome.action == "skipped"


def test_resolve_blank_key_skips():
    ui = ScriptedUI([""])
    client = FakeClient(200)
    outcome = resolve_cloud_key(ui, FakeDefn(), client, make_provider())

    assert outcome.action == "skipped"


def test_resolve_quota_reason_shown():
    ui = ScriptedUI(["sk-bad", "s"])
    client = FakeClient(429, "rate limit exceeded")
    resolve_cloud_key(ui, FakeDefn(), client, make_provider())

    assert any("Quota exceeded or rate limited" in n for n in ui.notices)
