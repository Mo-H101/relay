"""
Tests for the Phase 10A ``ConversationStore.project_states`` projection,
the ``relay conversations projects`` CLI surface, the ``/diagnostics``
continuity subsection, and the approved no-authority guardrails.

``project_states`` is a read-only, bounded view of the durable
``project_state`` checkpoint table (metadata only: ``last_models``,
``counters``, ``last_seen``).  It is never used for request-path
hydration, routing, anchor resolution, or sequence assignment.
"""

import json
import time

import pytest

from app.cli import main
from app.core.config import settings
from app.services.conversation_store import ConversationStore


def _store(tmp_path, name="platform.db"):
    return ConversationStore(str(tmp_path / name))


def _upsert(store, *, project_key="pk", key_id="k", last_models=None,
            counters=None, last_seen=None):
    """Directly insert a project_state row for test setup."""
    store.update_project_state(
        key_id=key_id,
        project_key=project_key,
        last_models=last_models or [],
        counters=counters or {},
    )
    if last_seen is not None:
        import sqlite3
        conn = sqlite3.connect(store.path)
        conn.execute(
            "UPDATE project_state SET last_seen = ?"
            " WHERE project_key = ? AND key_id = ?",
            (last_seen, project_key, key_id),
        )
        conn.commit()
        conn.close()


class TestProjectStates:
    def test_empty_project(self, tmp_path):
        store = _store(tmp_path)
        try:
            out = store.project_states("k")
            assert out == []
        finally:
            store.close()

    def test_no_key_filter_returns_all(self, tmp_path):
        store = _store(tmp_path)
        _upsert(store, project_key="pk1", key_id="k")
        _upsert(store, project_key="pk2", key_id="k2")
        try:
            out = store.project_states()
            assert len(out) == 2
        finally:
            store.close()

    def test_key_scoping(self, tmp_path):
        store = _store(tmp_path)
        _upsert(store, project_key="pk1", key_id="k")
        _upsert(store, project_key="pk2", key_id="k2")
        try:
            out = store.project_states(key_id="k")
            assert len(out) == 1
            assert out[0]["key_id"] == "k"
        finally:
            store.close()

    def test_json_parsing(self, tmp_path):
        store = _store(tmp_path)
        _upsert(store, project_key="pk", key_id="k",
                last_models=["m1", "m2"],
                counters={"turns": 10, "switches": 2})
        try:
            out = store.project_states("k")
            assert len(out) == 1
            row = out[0]
            assert row["project_key"] == "pk"
            assert row["key_id"] == "k"
            assert row["last_models"] == ["m1", "m2"]
            assert row["counters"] == {"turns": 10, "switches": 2}
            assert isinstance(row["last_seen"], float)
        finally:
            store.close()

    def test_order_is_newest_last_seen_first(self, tmp_path):
        store = _store(tmp_path)
        now = time.time()
        _upsert(store, project_key="older", key_id="k", last_seen=now - 100)
        _upsert(store, project_key="newer", key_id="k", last_seen=now)
        try:
            out = store.project_states("k")
            assert [r["project_key"] for r in out] == ["newer", "older"]
        finally:
            store.close()

    def test_stable_project_key_tie_break(self, tmp_path):
        store = _store(tmp_path)
        now = time.time()
        _upsert(store, project_key="zz", key_id="k", last_seen=now)
        _upsert(store, project_key="aa", key_id="k", last_seen=now)
        try:
            out = store.project_states("k")
            assert [r["project_key"] for r in out] == ["aa", "zz"]
        finally:
            store.close()

    def test_limit_bounds(self, tmp_path):
        store = _store(tmp_path)
        for i in range(5):
            _upsert(store, project_key=f"pk{i}", key_id="k",
                    last_seen=time.time() + i)
        try:
            out = store.project_states("k", limit=2)
            assert len(out) == 2
        finally:
            store.close()

    def test_unavailable_store_returns_empty(self, tmp_path):
        store = ConversationStore(str(tmp_path))
        assert store.project_states("k") == []
        store.close()

    def test_metadata_only_no_content(self, tmp_path):
        """project_state rows must never contain prompts, responses,
        tokens, or other row content — only bounded metadata."""
        store = _store(tmp_path)
        _upsert(store, project_key="pk", key_id="k",
                last_models=["m1"], counters={"turns": 5})
        try:
            out = store.project_states("k")
            keys = set(out[0].keys())
            assert keys == {"project_key", "key_id", "last_models",
                            "counters", "last_seen"}
        finally:
            store.close()


class TestNoAuthorityGuardrail:
    """project_state / project_states must never be read on the
    request/chat execution path (relay, handoff, openai, chat)."""

    def test_relay_does_not_import_project_states(self):
        import re
        import inspect
        from app.core import relay as relay_mod
        src = inspect.getsource(relay_mod)
        for pattern in (r"\.project_states?\s*\(",):
            assert not re.search(pattern, src), (
                "relay.py must not call project_state read methods"
            )

    def test_handoff_does_not_call_project_states(self):
        import re
        import inspect
        from app.services import handoff as handoff_mod
        src = inspect.getsource(handoff_mod)
        for pattern in (r"\.project_states?\s*\(",):
            assert not re.search(pattern, src), (
                "handoff.py must not call project_state read methods"
            )

    def test_openai_does_not_call_project_states(self):
        import re
        import inspect
        from app.api import openai as openai_mod
        src = inspect.getsource(openai_mod)
        for pattern in (r"\.project_states?\s*\(",):
            assert not re.search(pattern, src), (
                "openai.py must not call project_state read methods"
            )

    def test_project_state_is_read_only_operator_surface(self):
        """update_project_state is a flusher write; project_state and
        project_states are diagnostic reads.  None of them influence
        routing, anchor, or sequence decisions."""
        import re
        import inspect
        from app.services import handoff as handoff_mod
        src = inspect.getsource(handoff_mod)
        # The flusher enqueues "project_state.update" — that is the write
        # path and is legitimate.  Guard against calling the read methods
        # (project_state() / project_states()) on the request path.
        for pattern in (r"\.project_states?\s*\(",):
            assert not re.search(pattern, src), (
                "handoff.py must not call project_state read methods"
            )


class TestConversationsProjectsCLI:
    """Tests for ``relay conversations projects`` (Phase 10A §E/F/H).

    The relay module exposes a module-level ``relay = Relay()`` singleton.
    CLI code does ``from app.core.relay import relay`` and reads
    ``relay.conversation_store``.  We monkeypatch the singleton's
    ``conversation_store`` attribute directly.
    """

    def test_empty_text(self, capsys, monkeypatch):
        monkeypatch.setattr(settings, "continuity_enabled", True)
        import app.core.relay as relay_mod
        monkeypatch.setattr(relay_mod.relay, "conversation_store", None)
        with pytest.raises(SystemExit):
            main(["conversations", "projects"])

    def test_empty_json(self, capsys, monkeypatch):
        monkeypatch.setattr(settings, "continuity_enabled", True)
        import app.core.relay as relay_mod
        monkeypatch.setattr(relay_mod.relay, "conversation_store", None)
        with pytest.raises(SystemExit):
            main(["conversations", "projects", "--json"])

    def test_json_shape(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "continuity_enabled", True)
        store = _store(tmp_path)
        _upsert(store, project_key="pk", key_id="k",
                last_models=["m1"], counters={"turns": 5})
        try:
            import app.core.relay as relay_mod
            monkeypatch.setattr(relay_mod.relay, "conversation_store", store)
            main(["conversations", "projects", "--json"])
            out = capsys.readouterr()
            data = json.loads(out.out)
            assert "projects" in data
            assert len(data["projects"]) == 1
            row = data["projects"][0]
            assert row["project_key"] == "pk"
            assert row["last_models"] == ["m1"]
        finally:
            store.close()

    def test_text_output(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "continuity_enabled", True)
        store = _store(tmp_path)
        _upsert(store, project_key="pk", key_id="k",
                last_models=["m1"], counters={"turns": 5})
        try:
            import app.core.relay as relay_mod
            monkeypatch.setattr(relay_mod.relay, "conversation_store", store)
            main(["conversations", "projects"])
            out = capsys.readouterr()
            assert "pk" in out.out
            assert "turns=5" in out.out
        finally:
            store.close()

    def test_disabled(self, capsys, monkeypatch):
        monkeypatch.setattr(settings, "continuity_enabled", False)
        main(["conversations", "projects"])
        out = capsys.readouterr()
        assert "continuity disabled" in out.out

    def test_disabled_json(self, capsys, monkeypatch):
        monkeypatch.setattr(settings, "continuity_enabled", False)
        main(["conversations", "projects", "--json"])
        out = capsys.readouterr()
        assert "continuity disabled" in out.out

    def test_limit_bounds(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "continuity_enabled", True)
        store = _store(tmp_path)
        for i in range(5):
            _upsert(store, project_key=f"pk{i}", key_id="k",
                    last_seen=time.time() + i)
        try:
            import app.core.relay as relay_mod
            monkeypatch.setattr(relay_mod.relay, "conversation_store", store)
            main(["conversations", "projects", "--json", "--limit", "2"])
            out = capsys.readouterr()
            data = json.loads(out.out)
            assert len(data["projects"]) == 2
        finally:
            store.close()

    def test_unavailable_store(self, capsys, monkeypatch):
        monkeypatch.setattr(settings, "continuity_enabled", True)
        import app.core.relay as relay_mod
        monkeypatch.setattr(relay_mod.relay, "conversation_store", None)
        with pytest.raises(SystemExit):
            main(["conversations", "projects"])


class TestDiagnosticsContinuity:
    """Tests for the ``/diagnostics`` continuity subsection (Phase 10A §F).

    Tests call ``_continuity`` directly to avoid needing a fully-mocked
    relay (``build_snapshot`` calls many other sub-methods).
    """

    def _call(self, monkeypatch, **overrides):
        from app.services.diagnostics import DiagnosticsService
        from types import SimpleNamespace

        store = overrides.get("store")
        fake_relay = SimpleNamespace(conversation_store=store)
        return DiagnosticsService()._continuity(fake_relay)

    def test_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "continuity_enabled", False)
        assert self._call(monkeypatch) == {"enabled": False}

    def test_enabled_without_store(self, monkeypatch):
        monkeypatch.setattr(settings, "continuity_enabled", True)
        result = self._call(monkeypatch, store=None)
        assert result["enabled"] is True
        assert result.get("available") is False

    def test_enabled_with_store(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "continuity_enabled", True)
        store = _store(tmp_path)
        _upsert(store, project_key="pk", key_id="k",
                last_models=["m1"], counters={"turns": 5})
        try:
            result = self._call(monkeypatch, store=store)
            assert result["enabled"] is True
            assert "conversations" in result
            assert "turns" in result
            assert "projects" in result
            assert "replays" in result
        finally:
            store.close()

    def test_zero_counts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "continuity_enabled", True)
        store = _store(tmp_path)
        try:
            result = self._call(monkeypatch, store=store)
            assert result["conversations"] == 0
            assert result["turns"] == 0
            assert result["projects"] == 0
            assert result["replays"] == 0
        finally:
            store.close()

    def test_expected_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "continuity_enabled", True)
        store = _store(tmp_path)
        try:
            result = self._call(monkeypatch, store=store)
            expected = {"enabled", "conversations", "active", "archived",
                        "turns", "summaries", "compactions", "projects",
                        "replays"}
            assert set(result.keys()) == expected
        finally:
            store.close()
