"""Redaction layer for diagnostics exports and log rendering."""

from app.services.redaction import _REDACTED, SENSITIVE_KEYS, redact_dict, redact_text


def test_sensitive_key_markers_include_authorization():
    assert "authorization" in SENSITIVE_KEYS
    assert "api_key" in SENSITIVE_KEYS


def test_sk_key_never_survives_text():
    token = "sk-" + "abcdefghij" * 3
    assert token not in redact_text(f"{{'api_key': '{token}'}}")


def test_nvapi_key_never_survives_text():
    token = "nvapi-" + "a" * 32
    assert token not in redact_text(f"token={token}")


def test_rl_key_never_survives_text():
    token = "rl_" + "a" * 43
    assert token not in redact_text(f"token={token}")


def test_rl_key_never_survives_quoted_text():
    token = "rl_" + "b" * 43
    assert token not in redact_text('{"key": "' + token + '"}')
    assert token not in redact_text("{'key': '" + token + "'}")


def test_rl_key_bearer_header_masked():
    token = "rl_" + "c" * 43
    redacted = redact_text(f"Authorization: Bearer {token}")
    assert token not in redacted


def test_rl_partial_shape_untouched():
    # Only a full rl_ key (prefix + 43 chars) is masked, so partial
    # values are not over-redacted.
    assert redact_text("token=rl_short") == "token=rl_short"


def test_redact_dict_masks_rl_values():
    token = "rl_" + "d" * 43
    out = redact_dict({"auth": {"token": token}, "note": f"key={token}"})
    assert token not in repr(out)


def test_bearer_token_never_survives_text():
    text = "Authorization: Bearer mysecret123456"
    assert "mysecret123456" not in redact_text(text)


def test_authorization_header_value_consumed_whole():
    text = 'headers: {"Authorization": "Bearer abc"}'
    redacted = redact_text(text)
    assert "Bearer abc" not in redacted
    assert "abc" not in redacted


def test_short_authorization_value_never_leaks():
    text = "Authorization: Bearer tiny5"
    assert "tiny5" not in redact_text(text)


def test_x_relay_api_key_header_masked():
    text = '{"x-relay-api-key": "sk-abcdefghij"}'
    redacted = redact_text(text)
    assert "sk-abcdefghij" not in redacted


def test_innocuous_text_untouched():
    text = "model='gpt-4o' latency=12ms provider=openai route=/health"
    assert redact_text(text) == text


def test_redact_dict_masks_by_key_name():
    payload = {
        "api_key": "sk-abcdefghij",
        "model": "gpt-4o",
        "nested": {"token": "abc", "ok": "fine"},
    }
    out = redact_dict(payload)
    assert out["api_key"] == _REDACTED
    assert out["nested"]["token"] == _REDACTED
    assert out["nested"]["ok"] == "fine"
    assert out["model"] == "gpt-4o"


def test_redact_dict_scans_plain_strings():
    out = redact_dict("Bearer abcdefgh")
    assert out == _REDACTED


def test_redact_dict_lists_tuples_and_nested():
    out = redact_dict([{"ok": "sk-abcdefghij"}, ("nvapi-" + "b" * 12,)])
    assert "sk-abcdefghij" not in repr(out)
    assert "nvapi-" + "b" * 12 not in repr(out)


def test_redact_dict_scalars():
    assert redact_dict(42) == 42
    assert redact_dict(None) is None
    assert redact_dict(True) is True


def test_redact_dict_does_not_mutate_input():
    payload = {"api_key": "sk-abcdefghij", "list": [{"secret": "x"}]}
    redact_dict(payload)
    assert payload["api_key"] == "sk-abcdefghij"
    assert payload["list"][0]["secret"] == "x"
