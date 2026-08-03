"""Classification of client user-agents (heuristic, Application A)."""

from app.services.client_detection import CLIENT_BUCKETS, _MAX_UA, classify_client


def test_bucket_order_is_fixed():
    assert CLIENT_BUCKETS == ("cline", "opencode", "continue", "other")


def test_cline():
    assert classify_client("Cline/3.0 (VS Code 1.90)") == "cline"


def test_opencode():
    assert classify_client("opencode/0.1.0 (macOS)") == "opencode"


def test_continue():
    assert classify_client("Continue/1.2.3 (VS Code)") == "continue"


def test_other():
    assert classify_client("curl/8.6.0") == "other"
    assert classify_client("python-requests/2.32") == "other"


def test_empty_and_none_are_other():
    assert classify_client("") == "other"
    assert classify_client(None) == "other"


def test_case_insensitive():
    assert classify_client("OPENCODE/0.1") == "opencode"
    assert classify_client("Cline") == "cline"


def test_surrounding_whitespace_is_trimmed():
    assert classify_client("  Cline/3.0  ") == "cline"


def test_first_marker_wins():
    assert classify_client("cline continue") == "cline"
    assert classify_client("opencode continue") == "opencode"


def test_ua_capped_before_matching():
    assert classify_client("x" * (_MAX_UA + 10) + "opencode") == "other"


def test_ua_at_cap_still_matches_when_marker_visible():
    marker = "opencode"
    ua = marker + "x" * (_MAX_UA - len(marker))
    assert classify_client(ua) == "opencode"
