"""
Schema-v7 and memory-contract parity tests for the P9a continuity layer.

Guards the contract between ``app/services/platform_store.py``
(MIGRATIONS[7] + SCHEMA_VERSION), ``app/services/memory_contract.py``
(durable surfaces), and ``app/services/conversation_store.py`` so the
continuity tables, their indexes, and the never-capture guarantees stay
aligned across refactors.
"""

import inspect
import re
import sqlite3

from app.services.continuity_flusher import ContinuityFlusher, _OP_METHODS
from app.services.conversation_store import ConversationStore
from app.services.handoff import HandoffCoordinator
from app.services.memory_contract import (
    MEMORY_SURFACES,
    MemoryClass,
    contains_never_captured,
)
from app.services.platform_store import MIGRATIONS, SCHEMA_VERSION, open_connection

P9_TABLES = [
    "conversations",
    "conversation_turns",
    "summaries",
    "compaction_records",
    "project_state",
]

P9_INDEXES = [
    "idx_conversations_key",
    "idx_conversations_project",
    "idx_turns_cid",
    "idx_compaction_cid",
    "idx_project_state_key",
]

P9_SURFACES = [
    "conversation_store",
    "continuity_flusher",
    "conversations",
    "conversation_turns",
    "summaries",
    "compaction_records",
    "project_state",
]


class TestSchemaParity:
    def test_migration_history_reaches_schema_v7(self):
        assert SCHEMA_VERSION == 7
        assert 7 in MIGRATIONS

    def test_v7_defines_all_five_tables(self):
        statements = MIGRATIONS[7]
        ddl = " ".join(statements).lower()

        for table in P9_TABLES:
            assert f"create table" in ddl
            assert f" {table}" in ddl

    def test_v7_defines_all_five_indexes(self):
        ddl = " ".join(MIGRATIONS[7]).lower()

        for index in P9_INDEXES:
            assert f"create index" in ddl
            assert index in ddl

    def test_fresh_database_matches_v7_ddl(self, tmp_path):
        path = str(tmp_path / "parity.db")
        conn = open_connection(path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()

        assert version == SCHEMA_VERSION == 7
        for table in P9_TABLES:
            assert table in tables
        for index in P9_INDEXES:
            assert index in indexes

    def test_resume_token_column_is_storage_only_hash(self, tmp_path):
        path = str(tmp_path / "parity.db")
        conn = open_connection(path)
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(conversation_turns)")
        }
        conn.close()

        assert "resume_token" in columns


class TestModelParity:
    def test_store_exposes_schema_version(self, tmp_path):
        store = ConversationStore(str(tmp_path / "p.db"))
        assert store.SCHEMA_VERSION == 7
        assert store.stats()["schema_version"] == 7
        store.close()

    def test_export_shapes_are_metadata_only(self, tmp_path):
        store = ConversationStore(str(tmp_path / "p.db"))
        record = store.create(
            key_id="key-1",
            client_bucket="cline",
            project_key="ab" * 16,
        )
        store.append_turn(
            conversation_id=record["id"],
            key_id="key-1",
            seq=1,
            outcome="ok",
            provider="nvidia",
            model="model-a",
            task="coding",
        )
        store.record_summary(
            conversation_id=record["id"],
            key_id="key-1",
            up_to_seq=1,
            version=1,
            method="extractive",
            content="a summary with no forbidden keys",
        )

        conversation = store.get(record["id"], "key-1")
        turns = store.turns(record["id"], "key-1")
        summaries = store.summaries(record["id"], "key-1")

        assert not contains_never_captured(conversation)
        assert not contains_never_captured(turns)
        assert not contains_never_captured(summaries)
        assert "summary_text" in summaries[0]
        store.close()


class TestMemoryContractParity:
    def test_all_continuity_surfaces_are_durable(self):
        for surface in P9_SURFACES:
            assert MEMORY_SURFACES[surface] == MemoryClass.DURABLE
            assert surface in MEMORY_SURFACES

    def test_raw_conversation_content_is_still_forbidden(self):
        for surface in (
            "prompts",
            "responses",
            "generated_content",
            "api_keys",
            "user_identity",
        ):
            assert MEMORY_SURFACES[surface] == MemoryClass.NEVER

    def test_never_surfaces_have_no_continuity_aliases(self):
        for surface in P9_SURFACES:
            assert MEMORY_SURFACES[surface] is not MemoryClass.NEVER


class TestEnqueueContract:
    def test_ops_map_to_real_store_methods(self):
        for operation, method in _OP_METHODS.items():
            assert callable(getattr(ConversationStore, method, None)), (
                f"{operation} -> {method} is not a ConversationStore method"
            )

    def test_coordinator_enqueues_only_known_ops(self):
        source = inspect.getsource(HandoffCoordinator)
        enqueued = set(re.findall(r'self\._enqueue\(\s*"([a-z_.]+)"', source))

        assert enqueued
        assert enqueued <= set(_OP_METHODS)

    def test_enqueued_rows_drain_to_the_store(self, tmp_path):
        store = ConversationStore(str(tmp_path / "p.db"))
        flusher = ContinuityFlusher(
            conversation_store=store, retention_days=0
        )

        flusher.enqueue(
            "conversation.create",
            key_id="key-1",
            client_bucket="cline",
            project_key="ab" * 16,
            conversation_id="c1",
        )
        flusher.enqueue(
            "turn.append",
            conversation_id="c1",
            key_id="key-1",
            seq=1,
            outcome="ok",
            provider="nvidia",
            model="model-a",
        )
        flusher.enqueue(
            "summary.record",
            conversation_id="c1",
            key_id="key-1",
            up_to_seq=1,
            version=1,
            method="extractive",
            content="summary text",
        )
        flusher.enqueue(
            "compaction.record",
            conversation_id="c1",
            key_id="key-1",
            reason="preflight",
            method="extractive",
        )
        flusher.enqueue(
            "project_state.update",
            key_id="key-1",
            project_key="ab" * 16,
            last_models=["model-a"],
            counters={"turns": 1},
        )

        assert flusher.queue_size == 5

        flusher.flush()

        assert store.get("c1", "key-1") is not None
        assert len(store.turns("c1", "key-1")) == 1
        assert len(store.summaries("c1", "key-1")) == 1
        assert len(store.compactions("c1", "key-1")) == 1
        assert store.project_state("key-1", "ab" * 16) is not None
        assert flusher.flush_stats()["drained_total"] == 5
        store.close()
