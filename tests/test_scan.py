"""
Scan engine (P1): concurrency, ordering, status mapping, callbacks.
"""

import threading
import time

from app.providers.availability import AVAILABLE, OVERLOADED, UNAVAILABLE
from app.providers.base import ModelProbe, Provider
from app.setup.scan import ScanEngine


def make_provider():
    return Provider(name="Fake", base_url="http://fake/v1", api_key="k")


class SequentialClient:
    """Returns a fixed probe per model id (by index)."""

    def __init__(self, probes):
        self.probes = probes

    def probe_model(self, provider, model):
        return self.probes[model]


def test_status_mapping():
    client = SequentialClient({
        "ok": ModelProbe(True, 10, 200, ""),
        "busy429": ModelProbe(False, 10, 429, "rate limited"),
        "busy529": ModelProbe(False, 10, 529, "overloaded"),
        "timeout": ModelProbe(False, 10, 0, "timeout"),
        "auth": ModelProbe(False, 10, 403, "denied"),
        "server": ModelProbe(False, 10, 500, "oops"),
        "net": ModelProbe(False, 10, 0, "connection refused"),
    })

    models = list(client.probes)
    results = ScanEngine(concurrency=8).scan(client, make_provider(), models)

    statuses = {r.model: r.status for r in results}

    assert statuses["ok"] == AVAILABLE
    assert statuses["busy429"] == OVERLOADED
    assert statuses["busy529"] == OVERLOADED
    assert statuses["timeout"] == OVERLOADED
    assert statuses["auth"] == UNAVAILABLE
    assert statuses["server"] == UNAVAILABLE
    assert statuses["net"] == UNAVAILABLE


def test_results_ordered_even_with_out_of_order_completion():
    class DelayedClient:
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()

        def probe_model(self, provider, model):
            with self.lock:
                self.calls += 1
                order = self.calls
            time.sleep(0.05 - (order * 0.01))
            return ModelProbe(True, 10, 200, "")

    client = DelayedClient()
    models = [f"m{i}" for i in range(8)]

    results = ScanEngine(concurrency=8).scan(client, make_provider(), models)

    assert [r.model for r in results] == models


def test_on_update_once_per_model_monotonic():
    client = SequentialClient({
        "a": ModelProbe(True, 1, 200, ""),
        "b": ModelProbe(True, 1, 200, ""),
        "c": ModelProbe(True, 1, 200, ""),
    })
    updates = []

    def on_update(done, total, result):
        updates.append((done, total, result.model))

    ScanEngine(concurrency=3).scan(
        client, make_provider(), ["a", "b", "c"], on_update=on_update
    )

    assert [u[0] for u in updates] == [1, 2, 3]
    assert all(u[1] == 3 for u in updates)


def test_on_update_receives_latest_result():
    client = SequentialClient({
        "a": ModelProbe(True, 1, 200, ""),
        "b": ModelProbe(False, 2, 500, "boom"),
    })
    seen = []

    def on_update(done, total, result):
        seen.append(result)

    ScanEngine(concurrency=2).scan(
        client, make_provider(), ["a", "b"], on_update=on_update
    )

    statuses = {r.model: r.status for r in seen}
    assert statuses["a"] == AVAILABLE
    assert statuses["b"] == UNAVAILABLE


def test_in_flight_work_bounded_by_concurrency():
    active = 0
    max_active = 0
    lock = threading.Lock()

    class TrackingClient:
        def probe_model(self, provider, model):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return ModelProbe(True, 1, 200, "")

    client = TrackingClient()
    models = [f"m{i}" for i in range(20)]

    ScanEngine(concurrency=2).scan(client, make_provider(), models)

    assert max_active <= 2


def test_probe_exception_becomes_unavailable():
    class BoomClient:
        def probe_model(self, provider, model):
            raise RuntimeError("boom")

    results = ScanEngine(concurrency=4).scan(
        BoomClient(), make_provider(), ["a", "b"]
    )

    assert all(r.status == UNAVAILABLE for r in results)
    # Raw exception text must not leak into scan results (3840baf redacts
    # provider errors behind the safe boundary).
    assert all(r.error == "Provider request failed." for r in results)
    assert all("boom" not in r.error for r in results)


def test_empty_models():
    results = ScanEngine().scan(SequentialClient({}), make_provider(), [])
    assert results == []
