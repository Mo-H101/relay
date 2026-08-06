"""
Tests for the P9a ConversationStore and ContinuityFlusher.

Covers the schema-v7 migration matrix, the store's create/append/archive/
prune/scope semantics, audit rows, retention, privacy, and the
write-behind flusher lifecycle.
"""

import sqlite3
import time

import pytest

import app.services.platform_store as platform_store
from app.services.conversation_store import ConversationStore
from app.services.continuity_flusher import ContinuityFlusher
from app.services.memory_contract import contains_never_captured


def _store(tmp_path, name="platform.db"):
    return ConversationStore(str(tmp_path / name))


def _age(id_, *, path, days, last_turn=True):
    """Back-date a conversation's timestamps (WAL multi-connection ok)."""
    cutoff = time.time() - int(days) * 86400
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE conversations SET updated_at = ?, last_turn_ts = ?"
        " WHERE id = ?",
        (cutoff, cutoff if last_turn else None, id_),
    )
    conn.commit()
    conn.close()


class TestSchemaMigration:
    def test_fresh_store_is_schema_v7(self, tmp_path):
        store = _store(tmp_path)
        conn = platform_store.open_connection(store.path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        conn.close()
        store.close()

        assert version == 7
        assert {
            "conversations",
            "conversation_turns",
            "summaries",
            "compaction_records",
            "project_state",
        } <= tables

    def test_v6_file_migrates_to_v7_additively(self, tmp_path):
        path = str(tmp_path / "v6.db")
        conn = sqlite3.connect(path)

        for target in range(1, 7):
            for statement in platform_store.MIGRATIONS[target]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target}")

        conn.execute(
            "INSERT INTO request_log (ts, route, client_bucket)"
            " VALUES (?, ?, ?)",
            (1234.0, "/chat", "cline"),
        )
        conn.commit()
        conn.close()

        opened = platform_store.open_connection(path)
        version = opened.execute("PRAGMA user_version").fetchone()[0]
        integrity = opened.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {
            row[0]
            for row in opened.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        route = opened.execute(
            "SELECT route FROM request_log WHERE ts = 1234.0"
        ).fetchone()[0]
        opened.close()

        assert version == platform_store.SCHEMA_VERSION == 7
        assert integrity == "ok"
        assert "conversations" in tables
        assert "project_state" in tables
        assert route == "/chat"

    def test_newer_schema_refused(self, tmp_path):
        path = tmp_path / "v8.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 8")
        conn.commit()
        conn.close()

        with pytest.raises(platform_store.PlatformStoreError):
            platform_store.open_connection(str(path))

    def test_reopen_is_idempotent(self, tmp_path):
        store = _store(tmp_path)
        store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.close()

        reopened = _store(tmp_path)
        conn = platform_store.open_connection(reopened.path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        reopened.close()

        assert version == 7


class TestConversations:
    def test_create_and_get_roundtrip(self, tmp_path):
        store = _store(tmp_path)
        record = store.create(
            key_id="key-1",
            client_bucket="opencode",
            project_key="ab" * 16,
            model_chain=["nvidia:model-a", "nvidia:model-b"],
            token_budget=32768,
        )

        fetched = store.get(record["id"], "key-1")
        store.close()

        assert fetched["key_id"] == "key-1"
        assert fetched["client_bucket"] == "opencode"
        assert fetched["project_key"] == "ab" * 16
        assert fetched["status"] == "active"
        assert fetched["model_chain"] == ["nvidia:model-a", "nvidia:model-b"]
        assert fetched["token_budget"] == 32768

    def test_create_emits_audit_row(self, tmp_path, isolated_event_log):
        store = _store(tmp_path)
        record = store.create(
            key_id="key-1", client_bucket="cline", project_key="ab" * 16
        )

        rows = isolated_event_log.query(action="continuity.create")
        store.close()

        assert len(rows) == 1
        assert rows[0]["actor"] == "key-1"
        assert rows[0]["target"] == record["id"]
        assert rows[0]["outcome"] == "ok"
        assert not contains_never_captured(rows[0])

    def test_scope_isolation(self, tmp_path):
        store = _store(tmp_path)
        record = store.create(
            key_id="key-1", client_bucket="cline", project_key="ab" * 16
        )

        assert store.get(record["id"], "other-key") is None
        assert store.archive(record["id"], "other-key") is False
        with pytest.raises(ValueError, match="not found"):
            store.append_turn(
                conversation_id=record["id"],
                key_id="other-key",
                seq=1,
                outcome="ok",
            )
        assert store.list(key_id="other-key") == []
        store.close()

    def test_find_is_operator_read_only(self, tmp_path):
        store = _store(tmp_path)
        record = store.create(
            key_id="key-1", client_bucket="cline", project_key="ab" * 16
        )
        assert store.find(record["id"])["key_id"] == "key-1"
        assert store.find("does-not-exist") is None
        store.close()

    def test_archive_and_audit(self, tmp_path, isolated_event_log):
        store = _store(tmp_path)
        record = store.create(
            key_id="key-1", client_bucket="cline", project_key="ab" * 16
        )

        assert store.archive(record["id"], "key-1") is True
        assert store.get(record["id"], "key-1")["status"] == "archived"

        rows = isolated_event_log.query(action="continuity.archive")
        store.close()

        assert len(rows) == 1
        assert rows[0]["outcome"] == "ok"


class TestTurns:
    def test_append_turn_updates_conversation(self, tmp_path):
        store = _store(tmp_path)
        record = store.create(
            key_id="key-1", client_bucket="cline", project_key="ab" * 16
        )

        store.append_turn(
            conversation_id=record["id"],
            key_id="key-1",
            seq=1,
            outcome="ok",
            provider="nvidia",
            model="model-a",
            task="coding",
            tokens_in=10,
            tokens_out=20,
            latency_ms=250,
        )

        turns = store.turns(record["id"], "key-1")
        conversation = store.get(record["id"], "key-1")
        store.close()

        assert len(turns) == 1
        assert turns[0]["provider"] == "nvidia"
        assert turns[0]["tokens_in"] == 10
        assert turns[0]["tokens_out"] == 20
        assert turns[0]["outcome"] == "ok"
        assert conversation["last_turn_ts"] is not None

    def test_append_turn_rejects_archived(self, tmp_path):
        store = _store(tmp_path)
        record = store.create(
            key_id="key-1", client_bucket="cline", project_key="ab" * 16
        )
        store.archive(record["id"], "key-1")

        with pytest.raises(ValueError, match="archived"):
            store.append_turn(
                conversation_id=record["id"],
                key_id="key-1",
                seq=1,
                outcome="ok",
            )
        store.close()

    def test_append_turn_rejects_bad_outcome(self, tmp_path):
        store = _store(tmp_path)
        record = store.create(
            key_id="key-1", client_bucket="cline", project_key="ab" * 16
        )

        with pytest.raises(ValueError, match="outcome"):
            store.append_turn(
                conversation_id=record["id"],
                key_id="key-1",
                seq=1,
                outcome="unknown",
            )
        store.close()


class TestRetention:
    def _seed(self, store, path):
        recent = store.create(
            conversation_id="recent-active",
            key_id="key-1",
            client_bucket="cline",
            project_key="ab" * 16,
        )
        store.append_turn(
            conversation_id=recent["id"], key_id="key-1", seq=1, outcome="ok"
        )

        archived_old = store.create(
            conversation_id="archived-old",
            key_id="key-1",
            client_bucket="cline",
            project_key="ab" * 16,
        )
        store.archive(archived_old["id"], "key-1")
        _age(archived_old["id"], path=path, days=10)

        inactive_old = store.create(
            conversation_id="inactive-old",
            key_id="key-1",
            client_bucket="cline",
            project_key="ab" * 16,
        )
        store.append_turn(
            conversation_id=inactive_old["id"],
            key_id="key-1",
            seq=1,
            outcome="ok",
        )
        _age(inactive_old["id"], path=path, days=10)

        fresh_archived = store.create(
            conversation_id="fresh-archived",
            key_id="key-1",
            client_bucket="cline",
            project_key="ab" * 16,
        )
        store.archive(fresh_archived["id"], "key-1")

        return recent, archived_old, inactive_old, fresh_archived

    def test_prune_removes_only_idle_old_conversations(self, tmp_path):
        store = _store(tmp_path)
        recent, archived_old, inactive_old, fresh_archived = self._seed(
            store, store.path
        )

        removed = store.prune_retention(days=1)
        ids = {row["id"] for row in store.list()}
        conn = platform_store.open_connection(store.path)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        store.close()

        assert removed == 2
        assert archived_old["id"] not in ids
        assert inactive_old["id"] not in ids
        assert recent["id"] in ids
        assert fresh_archived["id"] in ids
        assert integrity == "ok"

    def test_prune_zero_disables(self, tmp_path):
        store = _store(tmp_path)
        _, archived_old, _, _ = self._seed(store, store.path)

        assert store.prune_retention(days=0) == 0
        assert store.get(archived_old["id"], "key-1") is not None
        store.close()

    def test_prune_emits_audit_row(self, tmp_path, isolated_event_log):
        store = _store(tmp_path)
        self._seed(store, store.path)

        assert store.prune_retention(days=1) > 0
        rows = isolated_event_log.query(action="continuity.prune")
        store.close()

        assert len(rows) == 1
        assert rows[0]["outcome"] == "ok"
        assert rows[0]["detail"]["removed"] == 2
        assert not contains_never_captured(rows[0])


class TestSummariesAndCompactions:
    def test_summary_roundtrip_and_dedupe(self, tmp_path):
        store = _store(tmp_path)
        record = store.create(
            key_id="key-1", client_bucket="cline", project_key="ab" * 16
        )

        first = store.record_summary(
            conversation_id=record["id"],
            key_id="key-1",
            up_to_seq=3,
            version=1,
            method="extractive",
            content="decided to use python and ruff",
        )
        second = store.record_summary(
            conversation_id=record["id"],
            key_id="key-1",
            up_to_seq=3,
            version=1,
            method="extractive",
            content="replaced the summary",
        )
        summaries = store.summaries(record["id"], "key-1")
        store.close()

        assert len(summaries) == 1
        assert summaries[0]["summary_text"] == "replaced the summary"
        assert first["summary_id"] == second["summary_id"]

    def test_summary_content_is_redacted_and_bounded(self, tmp_path):
        store = _store(tmp_path)
        record = store.create(
            key_id="key-1", client_bucket="cline", project_key="ab" * 16
        )

        store.record_summary(
            conversation_id=record["id"],
            key_id="key-1",
            up_to_seq=1,
            version=1,
            method="extractive",
            content="secret sk-ABCDEFGH123456789 leaked here" + ("x" * 100000),
        )
        summaries = store.summaries(record["id"], "key-1")
        store.close()

        text = summaries[0]["summary_text"]
        assert "sk-" not in text
        assert len(text) <= 4096
        assert not contains_never_captured(summaries[0])

    def test_compaction_record_and_audit(self, tmp_path, isolated_event_log):
        store = _store(tmp_path)
        record = store.create(
            key_id="key-1", client_bucket="cline", project_key="ab" * 16
        )

        result = store.record_compaction(
            conversation_id=record["id"],
            key_id="key-1",
            reason="overflow",
            method="summary+tail",
            from_tokens=1000,
            to_tokens=400,
        )
        rows = isolated_event_log.query(action="continuity.compact")
        store.close()

        assert result["reason"] == "overflow"
        assert len(store.compactions(record["id"], "key-1")) == 1
        assert len(rows) == 1
        assert not contains_never_captured(rows[0])


class TestProjectState:
    def test_upsert_and_read(self, tmp_path):
        store = _store(tmp_path)
        store.update_project_state(
            key_id="key-1",
            project_key="ab" * 16,
            last_models=["nvidia:model-a"],
            counters={"switches": 1},
        )
        store.update_project_state(
            key_id="key-1",
            project_key="ab" * 16,
            last_models=["nvidia:model-b"],
            counters={"switches": 2},
        )

        state = store.project_state("key-1", "ab" * 16)
        counts = store.counts("key-1")
        store.close()

        assert state["last_models"] == ["nvidia:model-b"]
        assert state["counters"] == {"switches": 2}
        assert counts["projects"] == 1

    def test_project_state_is_key_scoped(self, tmp_path):
        store = _store(tmp_path)
        store.update_project_state(
            key_id="key-1", project_key="ab" * 16, counters={"switches": 1}
        )
        assert store.project_state("key-2", "ab" * 16) is None
        store.close()


class TestCountsAndDiagnostics:
    def test_counts_are_key_scoped(self, tmp_path):
        store = _store(tmp_path)
        for key in ("key-1", "key-2"):
            record = store.create(
                key_id=key, client_bucket="cline", project_key="ab" * 16
            )
            store.append_turn(
                conversation_id=record["id"], key_id=key, seq=1, outcome="ok"
            )

        one = store.counts("key-1")
        total = store.counts()
        store.close()

        assert one["conversations"] == 1
        assert one["turns"] == 1
        assert total["conversations"] == 2
        assert total["turns"] == 2

    def test_store_stats(self, tmp_path):
        store = _store(tmp_path)
        stats = store.stats()
        store.close()

        assert stats["schema_version"] == 7
        assert stats["path"] == store.path

    def test_unavailable_store_degrades_gracefully(self, tmp_path):
        store = ConversationStore(str(tmp_path))  # path is a directory

        with pytest.raises(OSError):
            store.create(
                key_id="k", client_bucket="cline", project_key="p" * 32
            )
        assert store.counts() == {
            "conversations": 0,
            "active": 0,
            "archived": 0,
            "turns": 0,
            "summaries": 0,
            "compactions": 0,
            "projects": 0,
        }
        assert store.stats()["open_errors"] >= 1
        store.close()


class TestContinuityFlusher:
    def test_flush_prunes_retention(self, tmp_path):
        store = _store(tmp_path)
        record = store.create(
            conversation_id="old",
            key_id="key-1",
            client_bucket="cline",
            project_key="ab" * 16,
        )
        store.archive(record["id"], "key-1")
        _age(record["id"], path=store.path, days=10)

        flusher = ContinuityFlusher(
            conversation_store=store,
            interval_seconds=5,
            retention_days=1,
        )
        pruned = flusher.flush()
        stats = flusher.flush_stats()
        store.close()

        assert pruned == 1
        assert stats["flush_count"] == 1
        assert stats["retention_days"] == 1

    def test_flush_with_pruning_disabled(self, tmp_path):
        store = _store(tmp_path)
        flusher = ContinuityFlusher(
            conversation_store=store,
            interval_seconds=5,
            retention_days=0,
        )
        assert flusher.flush() == 0
        store.close()

    def test_flush_survives_store_failure(self, tmp_path):
        store = ConversationStore(str(tmp_path))  # unopenable path
        flusher = ContinuityFlusher(
            conversation_store=store,
            interval_seconds=5,
            retention_days=1,
        )
        assert flusher.flush() == 0  # never raises
        store.close()

    def test_start_stop_lifecycle(self, tmp_path):
        store = _store(tmp_path)
        flusher = ContinuityFlusher(
            conversation_store=store,
            interval_seconds=60,
            retention_days=1,
        )
        flusher.start()
        assert flusher.is_running
        flusher.start()  # idempotent
        flusher.stop()
        assert not flusher.is_running
        store.close()
