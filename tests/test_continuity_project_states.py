"""
Tests for the Phase 10A ``ConversationStore.project_states`` projection.

The projection is a read-only, bounded view of the durable conversation
table for one project (key-scoped): each conversation's last committed
turn plus the project's total conversation count. It reuses the same
single-turn read as ``ContinuityRecovery`` and never replays events.
"""

from app.services.conversation_store import ConversationStore


def _store(tmp_path, name="platform.db"):
    return ConversationStore(str(tmp_path / name))


def _create(store, cid, *, key_id="k", project_key="pk", bucket="opencode"):
    store.create(
        key_id=key_id, client_bucket=bucket, project_key=project_key,
        conversation_id=cid,
    )
    return cid


def _turn(store, cid, seq, *, key_id="k", outcome="ok",
          provider="p", model="m"):
    store.append_turn(
        conversation_id=cid, key_id=key_id, seq=seq, outcome=outcome,
        provider=provider, model=model,
    )
    return cid


class TestProjectStates:
    def test_created_but_turn_less_conversation_is_listed(self, tmp_path):
        store = _store(tmp_path)
        cid = _create(store, "a" * 32)
        try:
            out = store.project_states("k", "pk")
            assert out["conversation_count"] == 1
            state = out["states"][0]
            assert state["conversation_id"] == cid
            assert state["last_seq"] is None
            assert state["last_model"] is None
            assert state["last_provider"] is None
            assert state["last_outcome"] is None
        finally:
            store.close()

    def test_no_conversations_at_all(self, tmp_path):
        store = _store(tmp_path)
        try:
            out = store.project_states("k", "pk")
            assert out == {"conversation_count": 0, "states": []}
        finally:
            store.close()

    def test_each_state_reports_last_committed_turn(self, tmp_path):
        store = _store(tmp_path)
        cid = _create(store, "c" * 32)
        _turn(store, cid, 1, model="m1")
        _turn(store, cid, 2, model="m2")
        _turn(store, cid, 3, model="m3")
        try:
            out = store.project_states("k", "pk")
            assert out["conversation_count"] == 1
            assert len(out["states"]) == 1
            state = out["states"][0]
            assert state["project_key"] == "pk"
            assert state["client_bucket"] == "opencode"
            assert state["conversation_id"] == cid
            assert state["key_id"] == "k"
            assert state["last_seq"] == 3
            assert state["last_model"] == "m3"
            assert state["last_provider"] == "p"
            assert state["last_outcome"] == "ok"
        finally:
            store.close()

    def test_key_and_project_scoping(self, tmp_path):
        store = _store(tmp_path)
        in_project = _create(store, "a" * 32)
        other_key = _create(store, "b" * 32, key_id="k2")
        other_project = _create(store, "d" * 32, project_key="pk2")
        _turn(store, in_project, 1)
        _turn(store, other_key, 1, key_id="k2")
        _turn(store, other_project, 1)
        try:
            out = store.project_states("k", "pk")
            assert out["conversation_count"] == 1
            assert [s["conversation_id"] for s in out["states"]] == [
                in_project
            ]
        finally:
            store.close()

    def test_limit_bounds_states_but_not_count(self, tmp_path):
        store = _store(tmp_path)
        ids = [_create(store, f"{i:02d}" * 16) for i in range(5)]
        for cid in ids:
            _turn(store, cid, 1)
        try:
            out = store.project_states("k", "pk", limit=2)
            assert out["conversation_count"] == 5
            assert len(out["states"]) == 2
        finally:
            store.close()

    def test_order_is_newest_updated_first(self, tmp_path):
        store = _store(tmp_path)
        older = _create(store, "a" * 32)
        newer = _create(store, "b" * 32)
        _turn(store, older, 1)
        _turn(store, newer, 1)
        try:
            out = store.project_states("k", "pk")
            assert [s["conversation_id"] for s in out["states"]] == [
                newer, older,
            ]
        finally:
            store.close()

    def test_last_model_ignores_model_less_last_turn(self, tmp_path):
        # Phase 10A: the last-seq projection is independent of model
        # presence -- seq advances, the model stays None.
        store = _store(tmp_path)
        cid = _create(store, "c" * 32)
        _turn(store, cid, 1, model="m1")
        store.append_turn(
            conversation_id=cid, key_id="k", seq=2, outcome="ok",
            provider="p", model=None,
        )
        try:
            out = store.project_states("k", "pk")
            state = out["states"][0]
            assert state["last_seq"] == 2
            assert state["last_model"] is None
            assert state["last_provider"] == "p"
        finally:
            store.close()
