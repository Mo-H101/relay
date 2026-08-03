"""Privacy-preserving client activity tracking (Application B)."""

from app.services.client_tracking import ClientTracker


def _fresh(**kwargs):
    kwargs.setdefault("window_seconds", 3600)
    kwargs.setdefault("max_entries", 10)
    return ClientTracker(**kwargs)


def test_record_and_activity_aggregates():
    tracker = _fresh()
    tracker.record("cline", "Cline/3.0", "/chat", 200, "bearer")
    tracker.record("cline", "Cline/3.0", "/chat", 500, "bearer")
    tracker.record("opencode", "opencode/0.1", "/v1/chat/completions", 200, "none")

    rows = tracker.activity()
    by_key = {(row.bucket, row.route): row for row in rows}
    assert len(rows) == 2

    cline = by_key[("cline", "/chat")]
    assert cline.requests == 2
    assert cline.successes == 1
    assert cline.failures == 1
    assert cline.auth_schemes == ("bearer",)
    assert cline.ua == "Cline/3.0"
    assert cline.last_seen is not None

    opencode = by_key[("opencode", "/v1/chat/completions")]
    assert opencode.requests == 1
    assert opencode.successes == 1
    assert opencode.auth_schemes == ("none",)


def test_activity_sorted_by_recency():
    tracker = _fresh()
    tracker.record("cline", "ua", "/old", 200, "none")
    tracker.record("opencode", "ua", "/new", 200, "none")
    rows = tracker.activity()
    assert rows[0].bucket == "opencode"
    assert rows[1].bucket == "cline"


def test_auth_totals():
    tracker = _fresh()
    tracker.record("a", "ua", "/x", 200, "bearer")
    tracker.record("a", "ua", "/y", 401, "none")
    tracker.record("b", "ua", "/z", 200, "bearer")
    assert tracker.auth_totals() == {"bearer": 2, "none": 1}


def test_bounded_max_entries_prunes_oldest():
    tracker = _fresh(max_entries=3)
    for i in range(6):
        tracker.record("cline", "ua", f"/route-{i}", 200, "none")
    assert len(tracker.activity()) == 3


def test_expired_entries_are_pruned():
    import time as time_mod

    tracker = _fresh(window_seconds=60)
    tracker.record("cline", "ua", "/x", 200, "none")
    future = time_mod.monotonic() + 120
    tracker._prune(future)
    assert tracker.activity() == []


def test_ua_is_trimmed():
    tracker = _fresh()
    tracker.record("cline", "x" * 500, "/x", 200, "none")
    rows = tracker.activity()
    assert all(len(row.ua) <= 200 for row in rows)


def test_never_stores_raw_authorization_or_secrets():
    tracker = _fresh()
    secret = "sk-super-secret-token-123456789"
    tracker.record("cline", "Cline/3.0", "/chat", 200, "bearer")
    rendered = repr(tracker.activity()) + repr(tracker.auth_totals())
    assert secret not in rendered
    assert "sk-super-secret" not in rendered


def test_clear():
    tracker = _fresh()
    tracker.record("cline", "ua", "/x", 200, "none")
    tracker.clear()
    assert tracker.activity() == []
    assert tracker.auth_totals() == {}


def test_multiple_auth_schemes_are_deduplicated():
    tracker = _fresh()
    tracker.record("cline", "ua", "/x", 200, "bearer")
    tracker.record("cline", "ua", "/x", 200, "bearer")
    tracker.record("cline", "ua", "/x", 200, "none")
    row = tracker.activity()[0]
    assert row.auth_schemes == ("bearer", "none")
