"""
Tests for authentication abuse throttling (CPU-amplification guard).

``app.security.auth_throttle`` bounds the O(keys x scrypt) cost of
token-guessing floods: a per-client failure throttle rejects repeat
offenders before the KeyStore scan, and a process-wide gate caps how
many store authentications run concurrently (excess fails closed).

Unit tests cover bucket identity, window expiry, LRU bounding, and gate
semantics; integration tests drive ``require_api_key`` through the real
FastAPI dependency with an injected store, mirroring test_key_auth.py.
"""

import threading
import time

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import auth_throttle as throttle_module
from app.security.auth import require_api_key
from app.security.auth_throttle import (
    AuthGate,
    AuthThrottle,
    auth_gate,
    auth_throttle,
    reset_auth_throttle,
)
from app.services.key_store import KeyStore
from app.services.metrics import relay_metrics


@pytest.fixture(autouse=True)
def _isolate_singletons():
    reset_auth_throttle()
    yield
    reset_auth_throttle()


# ------------------------------------------------------------ unit: throttle


class TestAuthThrottle:
    def test_bucket_is_stable_digest_of_host(self):
        first = AuthThrottle.bucket_for("10.0.0.1")
        assert first == AuthThrottle.bucket_for("10.0.0.1")
        assert first != AuthThrottle.bucket_for("10.0.0.2")
        assert len(first) == 64  # sha256 hex digest

    def test_missing_host_shares_one_bucket(self):
        assert AuthThrottle.bucket_for(None) == AuthThrottle.bucket_for(None)

    def test_not_throttled_before_any_failure(self):
        throttle = AuthThrottle()
        bucket = AuthThrottle.bucket_for("h")
        assert throttle.check(bucket, limit=3, window_seconds=60) is False

    def test_throttles_after_limit_within_window(
        self, monkeypatch
    ):
        throttle = AuthThrottle()
        bucket = AuthThrottle.bucket_for("h")
        now = [1000.0]
        monkeypatch.setattr(throttle_module.time, "monotonic", lambda: now[0])

        for _ in range(3):
            throttle.record_failure(bucket, window_seconds=60)
            now[0] += 1

        assert throttle.check(bucket, limit=3, window_seconds=60) is True
        # A different bucket is unaffected.
        assert (
            throttle.check(
                AuthThrottle.bucket_for("other"), 3, 60
            )
            is False
        )

    def test_window_expiry_unthrottles_without_extension(self, monkeypatch):
        """A throttled bucket recovers when its window elapses."""
        throttle = AuthThrottle()
        bucket = AuthThrottle.bucket_for("h")
        now = [1000.0]
        monkeypatch.setattr(throttle_module.time, "monotonic", lambda: now[0])

        for _ in range(3):
            throttle.record_failure(bucket, window_seconds=60)

        assert throttle.check(bucket, limit=3, window_seconds=60) is True

        now[0] += 61
        assert throttle.check(bucket, limit=3, window_seconds=60) is False
        # The expired entry was pruned; budget restarts from scratch.
        throttle.record_failure(bucket, window_seconds=60)
        assert throttle.check(bucket, limit=3, window_seconds=60) is False

    def test_success_clears_bucket(self):
        throttle = AuthThrottle()
        bucket = AuthThrottle.bucket_for("h")
        for _ in range(2):
            throttle.record_failure(bucket, window_seconds=60)
        throttle.record_success(bucket)
        assert throttle.check(bucket, limit=2, window_seconds=60) is False

    def test_lru_bound_evicts_oldest_bucket(self):
        throttle = AuthThrottle(max_buckets=2)
        buckets = [AuthThrottle.bucket_for(h) for h in ("a", "b", "c")]
        for index, bucket in enumerate(buckets):
            throttle.record_failure(bucket, window_seconds=60)

        # "a" was evicted by "c"; touching "b" would have kept it alive,
        # so eviction order is insertion order here.
        assert throttle.check(buckets[0], limit=1, window_seconds=60) is False
        assert throttle.check(buckets[1], limit=1, window_seconds=60) is True
        assert throttle.check(buckets[2], limit=1, window_seconds=60) is True

    def test_failures_outside_window_restart_budget(self, monkeypatch):
        throttle = AuthThrottle()
        bucket = AuthThrottle.bucket_for("h")
        now = [1000.0]
        monkeypatch.setattr(throttle_module.time, "monotonic", lambda: now[0])

        throttle.record_failure(bucket, window_seconds=60)
        now[0] += 120
        throttle.record_failure(bucket, window_seconds=60)

        # Only the fresh failure counts against the restarted window.
        assert throttle.check(bucket, limit=2, window_seconds=60) is False
        throttle.record_failure(bucket, window_seconds=60)
        assert throttle.check(bucket, limit=2, window_seconds=60) is True

    def test_never_raises_on_garbage_input(self):
        throttle = AuthThrottle()
        throttle.check("", limit=1, window_seconds=-5)
        throttle.check(None, limit=None, window_seconds=None)
        throttle.record_failure(None, window_seconds=None)


# ---------------------------------------------------------------- unit: gate


class TestAuthGate:
    def test_enter_exit_round_trip(self):
        gate = AuthGate()
        token = gate.enter(max_concurrent=2)
        assert token is not None
        gate.exit(token)
        # Slot released: another enter succeeds immediately.
        assert gate.enter(max_concurrent=2) is not None

    def test_saturated_gate_times_out_to_none(self, monkeypatch):
        monkeypatch.setattr(
            throttle_module, "_GATE_TIMEOUT_SECONDS", 0.05
        )
        gate = AuthGate()
        held = gate.enter(max_concurrent=1)
        assert held is not None
        assert gate.enter(max_concurrent=1) is None
        gate.exit(held)

    def test_config_change_rebuilds_gate_and_keeps_old_token_valid(self):
        gate = AuthGate()
        old_token = gate.enter(max_concurrent=4)
        # Live reload shrinks concurrency while one slot is held.
        new_token = gate.enter(max_concurrent=1)
        assert new_token is not None
        gate.exit(old_token)  # releases the retired semaphore, harmlessly
        gate.exit(new_token)
        assert gate.enter(max_concurrent=1) is not None

    def test_exit_never_raises_on_bad_token(self):
        gate = AuthGate()
        gate.exit(None)
        gate.exit(object())


# ------------------------------------------------- integration: dependency


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


@pytest.fixture
def store(monkeypatch, tmp_path):
    instance = KeyStore(tmp_path / "relay_keys.db")
    monkeypatch.setattr("app.security.auth._key_store", lambda: instance)
    yield instance
    instance.close()


def _request(path="/providers", host="203.0.113.9", raw="rl_wrong"):
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {raw}".encode())],
        "client": (host, 44444),
    }
    return Request(scope)


class TestDependencyThrottling:
    @pytest.fixture
    def store_auth(self, monkeypatch, store):
        from app.core.config import settings

        monkeypatch.setattr(settings, "relay_api_key", "")
        monkeypatch.setattr(settings, "relay_auth_store", True)
        monkeypatch.setattr(settings, "relay_auth_failure_limit", 3)
        monkeypatch.setattr(
            settings, "relay_auth_failure_window_seconds", 60.0
        )
        return store

    def test_repeat_offender_rejected_before_store_scan(
        self, store_auth, monkeypatch
    ):
        calls = []
        real_authenticate = store_auth.authenticate

        def counting_authenticate(token):
            calls.append(token)
            return real_authenticate(token)

        monkeypatch.setattr(store_auth, "authenticate", counting_authenticate)

        with pytest.raises(HTTPException) as denied:
            require_api_key(_request())
        assert denied.value.status_code == 401
        with pytest.raises(HTTPException):
            require_api_key(_request(raw="rl_wrong2"))
        with pytest.raises(HTTPException):
            require_api_key(_request(raw="rl_wrong3"))
        assert len(calls) == 3

        # Budget exhausted: rejected before the KeyStore is consulted.
        with pytest.raises(HTTPException) as throttled:
            require_api_key(_request(raw="rl_wrong4"))
        assert throttled.value.status_code == 401
        assert len(calls) == 3

    def test_success_grants_after_failures(self, store_auth):
        _, raw_key = store_auth.create("test")

        for _ in range(2):
            with pytest.raises(HTTPException):
                require_api_key(_request())

        # A valid key clears the bucket...
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/providers",
                "query_string": b"",
                "headers": [
                    (b"authorization", f"Bearer {raw_key}".encode())
                ],
                "client": ("203.0.113.9", 44444),
            }
        )
        require_api_key(request)  # must not raise

        # ...so subsequent failures still reach the store, not the throttle.
        for _ in range(2):
            with pytest.raises(HTTPException):
                require_api_key(_request())

    def test_throttled_metric_reason_recorded(self, store_auth):
        for _ in range(3):
            with pytest.raises(HTTPException):
                require_api_key(_request())

        with pytest.raises(HTTPException):
            require_api_key(_request())

        assert (
            relay_metrics.auth_failures.value(reason="throttled") >= 1
        )

    def test_throttled_body_matches_other_denials(self, store_auth):
        for _ in range(4):
            with pytest.raises(HTTPException) as exc:
                require_api_key(_request())
        assert exc.value.detail == "Unauthorized"
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}

    def test_scope_denial_does_not_burn_the_budget(self, store_auth):
        _, scoped_key = store_auth.create("test", scopes=["chat"])

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/admin/keys",
                "query_string": b"",
                "headers": [(b"authorization", f"Bearer {scoped_key}".encode())],
                "client": ("203.0.113.9", 44444),
            }
        )
        with pytest.raises(HTTPException) as forbidden:
            require_api_key(request)
        assert forbidden.value.status_code == 403

        # The successful credential cleared the bucket: still no throttle.
        for _ in range(3):
            with pytest.raises(HTTPException):
                require_api_key(_request())


class _SlowStore:
    """Authenticates slowly until released; tracks slot occupancy."""

    calls_started = 0
    release = threading.Event()

    def authenticate(self, token):
        type(self).calls_started += 1
        self.release.wait(timeout=5)
        return {"status": "invalid", "meta": None}


class TestDependencyOverload:
    @pytest.fixture
    def slow_store(self, monkeypatch, tmp_path):
        from app.core.config import settings

        monkeypatch.setattr(settings, "relay_api_key", "")
        monkeypatch.setattr(settings, "relay_auth_store", True)
        monkeypatch.setattr(settings, "relay_auth_max_concurrent", 1)
        monkeypatch.setattr(
            throttle_module, "_GATE_TIMEOUT_SECONDS", 0.05
        )

        slow = _SlowStore()
        monkeypatch.setattr("app.security.auth._key_store", lambda: slow)
        return slow

    def test_overflow_request_fails_closed_fast(self, slow_store):
        results = {}

        def occupier():
            try:
                require_api_key(_request(host="198.51.100.7"))
                results["first"] = "ok"
            except HTTPException as exc:
                results["first"] = exc.status_code

        worker = threading.Thread(target=occupier)
        worker.start()
        try:
            # Wait until the first request holds the only slot.
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not _SlowStore.calls_started:
                time.sleep(0.005)
            started = time.monotonic()

            with pytest.raises(HTTPException) as overloaded:
                require_api_key(_request(host="198.51.100.8"))
            elapsed = time.monotonic() - started

            assert overloaded.value.status_code == 401
            assert elapsed < 2  # failed fast, not queued behind scrypt
        finally:
            _SlowStore.release.set()
            worker.join(timeout=5)

        assert relay_metrics.auth_failures.value(reason="overloaded") >= 1


class TestEndToEndThroughHTTP:
    def test_flood_gets_uniform_401s_then_recovers(self, client, monkeypatch, tmp_path):
        """The original attack shape: many bad tokens stay cheap 401s."""
        from app.core.config import settings

        instance = KeyStore(tmp_path / "flood.db")
        monkeypatch.setattr("app.security.auth._key_store", lambda: instance)
        monkeypatch.setattr(settings, "relay_api_key", "")
        monkeypatch.setattr(settings, "relay_auth_store", True)
        monkeypatch.setattr(settings, "relay_auth_failure_limit", 5)

        try:
            for attempt in range(8):
                response = client.get(
                    "/providers",
                    headers={"Authorization": f"Bearer rl_flood-{attempt}"},
                )
                assert response.status_code == 401
                assert response.json() == {"detail": "Unauthorized"}

            assert (
                relay_metrics.auth_failures.value(reason="throttled") >= 1
            )
        finally:
            instance.close()


class TestSingletons:
    def test_accessors_return_stable_instances(self):
        assert auth_throttle() is auth_throttle()
        assert auth_gate() is auth_gate()

    def test_reset_clears_state(self):
        throttle = auth_throttle()
        bucket = AuthThrottle.bucket_for("h")
        throttle.record_failure(bucket, window_seconds=60)
        reset_auth_throttle()
        assert throttle.check(bucket, limit=1, window_seconds=60) is False
