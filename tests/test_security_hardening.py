"""
P5 Phase 5 security hardening assertions.

Cross-cutting privacy contract: raw key material (``rl_``, ``sk-``,
``nvapi-``) never survives any render/export/error path; observability
surfaces carry only the opaque uuid ``key_id``; secret-bearing files are
user-only on POSIX.
"""

import os
import stat

import pytest

from app.services.redaction import _REDACTED, redact_dict, redact_text


def _rl() -> str:
    return "rl_" + "a" * 43


# ---------------------------------------------------------------- redaction

def test_rl_key_never_survives_redact_text_quoted_and_bare():
    token = _rl()
    assert token not in redact_text(f"token={token}")
    assert token not in redact_text('{"api_key": "' + token + '"}')
    assert token not in redact_text("{'api_key': '" + token + "'}")


def test_rl_key_never_survives_redact_dict_nested():
    token = _rl()
    payload = {
        "meta": {"note": "plain"},
        "creds": {"token": token},
        "list": [token, f"bearer {token}"],
        "deep": {"api_key": token},
    }
    out = redact_dict(payload)
    rendered = repr(out)
    assert token not in rendered
    assert _REDACTED in rendered
    assert out["meta"]["note"] == "plain"


def test_rl_bearer_header_masked():
    token = _rl()
    redacted = redact_text(f"Authorization: Bearer {token}")
    assert token not in redacted


def test_sk_and_nvapi_shapes_still_masked():
    token = "sk-" + "abcdefghij" * 3
    assert token not in redact_text(f"x={token}")
    nv = "nvapi-" + "b" * 32
    assert nv not in redact_text(f"x={nv}")


# ------------------------------------------------------------ error bodies

def test_rl_token_masked_in_provider_error_body():
    from app.providers.availability import safe_error_body
    from app.providers.base import Provider

    provider = Provider(
        id="nvidia",
        name="NVIDIA",
        base_url="http://localhost",
        api_key="rl_" + "b" * 43,
    )

    body = "unauthorized request"
    text = safe_error_body(provider, 401, body)
    assert text  # readable error, no raw key

    # A Relay key echoed by the provider is stripped by safe_error_body
    # only when it is the provider's own key...
    own = provider.api_key
    text = safe_error_body(provider, 401, f"echo {own}")
    assert own not in text

    # ...and redaction masks any other rl_ token in the same body.
    other = _rl()
    text = safe_error_body(provider, 401, f"echo {other}")
    assert other in text  # safe_error_body only strips its own key
    assert other not in redact_text(text)


# ------------------------------------------- identity in observability

def test_ops_events_carry_only_opaque_key_id(tmp_path):
    from app.services.key_store import KeyStore
    from app.services.ops_store import RequestStatsStore

    store = KeyStore(tmp_path / "relay_keys.db")
    key_id, raw = store.create("opencode")
    store.close()

    ops = RequestStatsStore(window_seconds=3600)
    ops.record_http("GET", "/v1/chat/completions", 200, 12.0, key_id=key_id)

    rendered = " ".join(repr(vars(event)) for event in ops.events())
    assert raw not in rendered
    assert "rl_" not in rendered
    assert key_id in rendered


def test_metrics_carry_only_opaque_key_id(tmp_path):
    from app.services.key_store import KeyStore
    from app.services.metrics import relay_metrics

    relay_metrics.reset()
    store = KeyStore(tmp_path / "relay_keys.db")
    key_id, raw = store.create("opencode")
    store.close()

    try:
        relay_metrics.record_auth(True, True, "bearer", key_id=key_id)
        rendered = relay_metrics.render()
    finally:
        relay_metrics.reset()

    assert raw not in rendered
    assert "rl_" not in rendered
    assert key_id in rendered


# ------------------------------------------------------------ permissions

def test_env_file_user_only_after_write(monkeypatch, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permission check")

    from app.services import config_store

    env_file = tmp_path / ".env"
    monkeypatch.setattr(config_store, "env_file", env_file)

    config_store.set_env("NVIDIA_API_KEY", "sk-secret")

    mode = stat.S_IMODE(os.stat(env_file).st_mode)
    assert mode == 0o600


def test_relay_keys_db_and_sidecars_user_only(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permission check")

    from app.services.key_store import KeyStore

    store = KeyStore(tmp_path / "relay_keys.db")
    store.create("perm-check")
    store.close()

    for suffix in ("", "-wal", "-shm"):
        path = tmp_path / f"relay_keys.db{suffix}"

        if not path.exists():
            continue

        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, suffix


def test_corrupt_backup_user_only(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permission check")

    from app.services.key_store import KeyStore

    path = tmp_path / "relay_keys.db"
    path.write_bytes(b"this is not a sqlite database at all")

    store = KeyStore(path)
    store.create("perm-check")
    store.close()

    backups = list(tmp_path.glob("relay_keys.db.corrupt-*.bak"))
    assert len(backups) == 1
    assert stat.S_IMODE(os.stat(backups[0]).st_mode) == 0o600


# -------------------------------------------------- secret grep over fixtures

def test_secret_grep_over_rendered_diagnostics_fixture():
    fixtures = [
        {
            "request": {"api_key": "rl_" + "a" * 43},
            "logs": [
                "provider returned 'sk-abcdefghijklmnopqrstuvwxyz'",
                "token=nvapi-" + "b" * 32,
                "Authorization: Bearer rl_" + "c" * 43,
            ],
        },
        "rl_" + "d" * 43,
        ["x-relay-api-key: rl_" + "e" * 43, "plain"],
    ]

    rendered = repr(redact_dict(fixtures))
    assert "rl_" + "a" * 43 not in rendered
    assert "rl_" + "c" * 43 not in rendered
    assert "rl_" + "d" * 43 not in rendered
    assert "rl_" + "e" * 43 not in rendered
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in rendered
    assert "nvapi-" + "b" * 32 not in rendered


# ----------------------------------------------- events (P6.2) privacy

def test_events_table_never_holds_durable_secrets(tmp_path, isolated_event_log):
    import sqlite3

    token = "rl_" + "a" * 43
    isolated_event_log.emit(
        "key.create",
        actor="cli",
        target="k-1",
        detail={"label": "ci", "note": f"token={token}"},
    )

    db_bytes = (tmp_path / "events.db").read_bytes()
    assert token.encode("utf-8") not in db_bytes

    conn = sqlite3.connect(tmp_path / "events.db")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    conn.close()

    assert not {"prompt", "response", "message"} & columns
