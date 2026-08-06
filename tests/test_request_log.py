"""
Request-log store tests (P6.5).

Covers the durable ``request_log`` write-behind store: record/flush/query
round-trips, filter and retention behavior, the never-raise capture
contract on an unavailable store, bounded reads, and the module singleton.
Privacy assertions live in the middleware tests; this file checks the
store's mechanics only.
"""

import time

import pytest

from app.services.request_log import RequestLogStore


@pytest.fixture
def store(tmp_path):
    instance = RequestLogStore(
        str(tmp_path / "reqlog.db"),
        flush_interval_seconds=0,
        retention_days=0,
    )
    yield instance
    instance.close()


def _record(instance, **kwargs):
    defaults = {
        "route": "/chat",
        "client_bucket": "cline",
        "client_ua": "Cline/3.0",
        "method": "POST",
        "status": 200,
        "latency_ms": 12.5,
        "auth_scheme": "bearer",
    }
    defaults.update(kwargs)
    instance.record(**defaults)


class TestRecordAndFlush:
    def test_round_trip(self, store, tmp_path):
        ts = time.time()
        _record(
            store,
            ts=ts,
            key_id="abc123",
            latency_ms=12.3456,
        )
        assert store.flush() == 1

        rows = store.query()
        assert len(rows) == 1
        row = rows[0]
        assert row["ts"] == ts
        assert row["route"] == "/chat"
        assert row["method"] == "POST"
        assert row["status"] == 200
        assert row["latency_ms"] == 12.346  # rounded to 3
        assert row["key_id"] == "abc123"
        assert row["client_bucket"] == "cline"
        assert row["ua"] == "Cline/3.0"
        assert row["auth_scheme"] == "bearer"

    def test_defaults_for_sparse_records(self, store):
        store.record(route="", client_bucket="", client_ua="   ")
        assert store.flush() == 1

        row = store.query()[0]
        assert row["route"] == "unmatched"
        assert row["client_bucket"] == "other"
        assert row["ua"] == ""
        assert row["auth_scheme"] == "none"
        assert row["key_id"] is None
        assert row["status"] is None
        assert row["latency_ms"] is None

    def test_flush_writes_pending_rows(self, store):
        _record(store)
        _record(store)
        assert store.flush() == 2
        assert store.flush() == 0  # buffer drained
        assert store.count() == 2

    def test_newest_first_ordering(self, store):
        for offset in range(3):
            _record(store, ts=1000.0 + offset, status=200)

        store.flush()
        rows = store.query(limit=2)
        assert [row["ts"] for row in rows] == [1002.0, 1001.0]


class TestQueries:
    def test_filter_by_route(self, store):
        _record(store, route="/health", ts=1.0)
        _record(store, route="/chat", ts=2.0)
        store.flush()

        rows = store.query(route="/health")
        assert [row["route"] for row in rows] == ["/health"]

    def test_filter_by_bucket_and_key(self, store):
        _record(store, client_bucket="cline", key_id="k1", ts=1.0)
        _record(store, client_bucket="opencode", key_id="k2", ts=2.0)
        store.flush()

        assert [r["key_id"] for r in store.query(client_bucket="opencode")] == ["k2"]
        assert [r["key_id"] for r in store.query(key_id="k1")] == ["k1"]

    def test_filter_by_since(self, store):
        _record(store, ts=10.0)
        _record(store, ts=20.0)
        store.flush()

        rows = store.query(since=15.0)
        assert [r["ts"] for r in rows] == [20.0]

    def test_query_limit_is_bounded(self, store):
        for offset in range(25):
            _record(store, ts=float(offset))

        store.flush()
        assert len(store.query(limit=5)) == 5

    def test_auth_totals(self, store):
        _record(store, auth_scheme="bearer")
        _record(store, auth_scheme="bearer")
        _record(store, auth_scheme="none")
        store.flush()

        assert store.auth_totals() == {"bearer": 2, "none": 1}


class TestRetention:
    def test_prune_removes_old_rows(self, tmp_path):
        instance = RequestLogStore(
            str(tmp_path / "reqlog.db"),
            flush_interval_seconds=0,
            retention_days=30,
        )
        _record(instance, ts=time.time() - 60 * 86400)
        _record(instance, ts=time.time())
        assert instance.flush() == 2

        # The old row is pruned by the flush's retention pass.
        assert instance.count() == 1

        instance.close()

    def test_prune_disabled_for_zero(self, store):
        _record(store)
        store.flush()
        assert store.prune_retention(days=0) == 0


class TestUnavailableStore:
    def test_record_never_raises(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        store = RequestLogStore(
            str(blocker / "reqlog.db"), flush_interval_seconds=0
        )

        _record(store)  # must not raise
        assert store.flush() == 0
        assert store.stats()["dropped"] == 1

        store.close()

    def test_reads_raise_oserror_when_unavailable(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        store = RequestLogStore(
            str(blocker / "reqlog.db"), flush_interval_seconds=0
        )

        with pytest.raises(OSError):
            store.query()
        with pytest.raises(OSError):
            store.count()
        assert store.auth_totals() == {}  # best-effort

        store.close()


class TestSingleton:
    def test_stats_shape(self, store):
        stats = store.stats()
        assert set(stats) == {
            "path",
            "buffered",
            "flushed",
            "dropped",
            "last_flush_at",
            "flush_errors",
            "running",
        }
        assert stats["running"] is False  # zero interval leaves loop stopped
