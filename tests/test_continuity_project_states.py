"""
Tests for the Phase 10A ``ConversationStore.project_states`` projection
and the ``HandoffCoordinator.build_conversation_snapshot`` surface.

The projection is a read-only, bounded view of the durable conversation
table for one project (key-scoped): each conversation's last committed
turn plus the project's total conversation count. It reuses the same
single-turn read as ``ContinuityRecovery`` and never replays events. The
snapshot attaches the live turn's identity to the durable projection and
never raises on store failure.
"""

from app.services.context_manager import ContextManager
from app.services.conversation_store import ConversationStore
from app.services.handoff import HandoffCoordinator


class FakeFlusher:
    """Records enqueued operations for inspection."""

    def __init__(self):
        self.enqueue_calls = []

    def enqueue(self, operation, **kwargs):
        self.enqueue_calls.append((operation, kwargs))


def _coordinator(flusher=None, **kwargs):
    return HandoffCoordinator(
        flusher=flusher if flusher is not None else FakeFlusher(),
        context_manager=ContextManager(
            char_token_ratio=4,
            context_token_budget=2048,
            output_reserve_tokens=128,
        ),
        **kwargs,
    )


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


class TestBuildConversationSnapshot:
    def _recovery(self, store):
        from app.services.continuity_recovery import ContinuityRecovery

        return ContinuityRecovery(store)

    def _start(self, coord, cid, *, key_id="k", project_key="pk"):
        return coord.start(
            key_id=key_id, client_bucket="opencode",
            project_key=project_key, conversation_id=cid,
        )

    def test_snapshot_attaches_live_turn_fields(self, tmp_path):
        store = _store(tmp_path)
        cid = _create(store, "c" * 32)
        _turn(store, cid, 1, model="m1")
        try:
            coord = _coordinator(recovery=self._recovery(store))
            turn = self._start(coord, cid)

            snap = coord.build_conversation_snapshot(turn, "k")

            assert snap["conversation"]["conversation_id"] == cid
            assert snap["conversation"]["key_id"] == "k"
            assert snap["conversation"]["project_key"] == "pk"
            assert snap["conversation"]["seq"] == 2
            assert snap["conversation"]["turn_id"] == f"{cid}:2"
            assert snap["conversation"]["last_seq"] == 1
            assert snap["conversation"]["anchor_provider"] == "p"
            assert snap["conversation"]["anchor_model"] == "m1"
            assert snap["project"]["conversation_count"] == 1
            assert snap["project"]["states"][0]["last_seq"] == 1
        finally:
            store.close()

    def test_snapshot_last_seq_falls_back_to_durable_before_commit(
        self, tmp_path
    ):
        # A fresh state (nothing committed in-memory yet) reports the
        # durable last_seq, not seq-1 of a restarted conversation.
        store = _store(tmp_path)
        cid = _create(store, "c" * 32)
        _turn(store, cid, 1, model="m1")
        _turn(store, cid, 2, model="m2")
        _turn(store, cid, 3, model="m3")
        try:
            coord = _coordinator(recovery=self._recovery(store))
            turn = self._start(coord, cid)

            snap = coord.build_conversation_snapshot(turn, "k")

            assert snap["conversation"]["seq"] == 4
            assert snap["conversation"]["last_seq"] == 3
            assert snap["project"]["states"][0]["last_seq"] == 3
        finally:
            store.close()

    def test_snapshot_reflects_live_commit(self, tmp_path):
        # After an in-memory commit the live seq/anchors win over the
        # durable projection for the conversation block. The anchor is the
        # turn-start continuation anchor (P9B): it is refreshed by durable
        # resumes, not by the in-memory commit itself.
        store = _store(tmp_path)
        cid = _create(store, "c" * 32)
        _turn(store, cid, 1, model="m1")
        try:
            coord = _coordinator(recovery=self._recovery(store))
            turn = self._start(coord, cid)
            coord.commit(turn, provider="p", model="m2")

            snap = coord.build_conversation_snapshot(turn, "k")

            assert snap["conversation"]["seq"] == 3
            assert snap["conversation"]["last_seq"] == 2
            assert snap["conversation"]["anchor_model"] == "m1"
            assert snap["project"]["states"][0]["last_seq"] == 1
        finally:
            store.close()

    def test_snapshot_degrades_to_live_state_without_recovery(self):
        # No recovery service: the project block is absent but the live
        # conversation state is still described.
        coord = _coordinator(recovery=None)
        turn = self._start(coord, "c" * 32)

        snap = coord.build_conversation_snapshot(turn, "k")

        assert snap["project"] is None
        assert snap["conversation"]["seq"] == 1
        assert snap["conversation"]["last_seq"] is None
        assert snap["conversation"]["anchor_model"] is None

    def test_snapshot_never_raises_when_store_unavailable(self, tmp_path):
        # A store that cannot open (path is a directory) behind the
        # recovery service must not raise: the snapshot degrades to the
        # live state alone.
        store = ConversationStore(str(tmp_path))
        coord = _coordinator(recovery=self._recovery(store))
        turn = self._start(coord, "c" * 32)

        snap = coord.build_conversation_snapshot(turn, "k")

        assert snap["project"] is None
        assert snap["conversation"]["seq"] == 1
        assert snap["conversation"]["last_seq"] is None

    def test_snapshot_project_states_bounded(self, tmp_path):
        store = _store(tmp_path)
        for i in range(5):
            _turn(store, _create(store, f"{i:02d}" * 16), 1)
        try:
            coord = _coordinator(recovery=self._recovery(store))
            turn = self._start(coord, "c" * 32)

            snap = coord.build_conversation_snapshot(turn, "k", project_limit=2)

            assert snap["project"]["conversation_count"] == 5
            assert len(snap["project"]["states"]) == 2
        finally:
            store.close()
