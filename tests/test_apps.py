"""
Connected-applications projection + ``relay apps`` CLI tests (P6.5).

Exercises ``app.services.apps_projection`` (labels x durable request log)
and the ``relay apps`` command. All assertions are metadata-only: labels,
opaque key ids, routes, counters, auth schemes, and last-seen. Raw keys
and Authorization values must never surface.
"""

import json

import pytest

from app.services import request_log as request_log_module
from app.services.key_store import KeyStore


@pytest.fixture
def projection(monkeypatch, tmp_path):
    """
    Isolated request log + key store wired into the projection and the
    request-log accessor so the facade/CLI never touch the real state dir.
    """
    import app.services.apps_projection as apps_projection

    reqlog = request_log_module.RequestLogStore(
        str(tmp_path / "reqlog.db"),
        flush_interval_seconds=0,
        retention_days=0,
    )
    keys = KeyStore(str(tmp_path / "keys.db"))
    monkeypatch.setattr(request_log_module, "request_log", lambda: reqlog)
    monkeypatch.setattr(apps_projection, "_key_store", lambda: keys)
    yield reqlog, keys
    reqlog.close()
    keys.close()


def _seed(reqlog, **kwargs):
    defaults = {
        "route": "/chat",
        "client_bucket": "cline",
        "client_ua": "Cline/3.0",
        "status": 200,
        "auth_scheme": "bearer",
    }
    defaults.update(kwargs)
    reqlog.record(**defaults)


class TestAppsProjection:
    def test_groups_by_identity_and_route(self, projection):
        reqlog, keys = projection
        key_id, _ = keys.create("ci")
        _seed(reqlog, key_id=key_id, status=200)
        _seed(reqlog, key_id=key_id, status=500)
        _seed(reqlog, key_id=None, route="/health", auth_scheme="none")
        reqlog.flush()

        from app.services.apps_projection import apps

        rows = apps()
        assert len(rows) == 2

        by_route = {row.route: row for row in rows}
        chat = by_route["/chat"]
        assert chat.label == "ci"
        assert chat.key_id == key_id
        assert chat.requests == 2
        assert chat.successes == 1
        assert chat.failures == 1
        assert chat.auth_schemes == ("bearer",)

        health = by_route["/health"]
        assert health.label == "none"
        assert health.key_id is None
        assert health.requests == 1

    def test_unknown_key_falls_back_to_short_id(self, projection):
        reqlog, _ = projection
        _seed(reqlog, key_id="0123456789abcdef")
        reqlog.flush()

        from app.services.apps_projection import apps

        row = apps()[0]
        assert row.label == "01234567"
        assert row.key_id == "0123456789abcdef"

    def test_never_leaks_raw_key_material(self, projection):
        reqlog, keys = projection
        key_id, raw = keys.create("ci")
        _seed(reqlog, key_id=key_id)
        reqlog.flush()

        from app.services.apps_projection import apps

        rendered = repr(apps())
        assert raw not in rendered
        assert "sk-" not in rendered

    def test_sorted_newest_last_seen_first(self, projection):
        reqlog, keys = projection
        key_a, _ = keys.create("a")
        key_b, _ = keys.create("b")
        _seed(reqlog, key_id=key_a, ts=100.0)
        _seed(reqlog, key_id=key_b, ts=200.0)
        reqlog.flush()

        from app.services.apps_projection import apps

        rows = apps()
        assert [row.key_id for row in rows] == [key_b, key_a]


class TestClientActivity:
    def test_aggregates_by_bucket_and_route(self, projection):
        reqlog, _ = projection
        _seed(reqlog, client_bucket="cline", status=200)
        _seed(reqlog, client_bucket="cline", status=500)
        _seed(reqlog, client_bucket="opencode", client_ua="opencode/0.1")
        reqlog.flush()

        from app.services.apps_projection import client_activity

        rows = client_activity()
        assert len(rows) == 2
        cline = next(r for r in rows if r.bucket == "cline")
        assert cline.requests == 2
        assert cline.successes == 1
        assert cline.failures == 1
        assert cline.ua == "Cline/3.0"
        assert all(r.last_seen >= 0 for r in rows)

    def test_auth_totals(self, projection):
        reqlog, _ = projection
        _seed(reqlog, auth_scheme="bearer")
        _seed(reqlog, auth_scheme="none")
        reqlog.flush()

        from app.services.apps_projection import auth_totals

        assert auth_totals() == {"bearer": 1, "none": 1}


class TestAppsCli:
    def test_json_emits_metadata_only(self, projection, capsys):
        from app.cli import main

        reqlog, keys = projection
        key_id, raw = keys.create("ci")
        _seed(reqlog, key_id=key_id, status=200)
        _seed(reqlog, key_id=key_id, status=200)
        reqlog.flush()

        main(["apps", "--json"])
        out, _ = capsys.readouterr()
        rows = json.loads(out)
        assert len(rows) == 1
        row = rows[0]
        assert row["label"] == "ci"
        assert row["key_id"] == key_id
        assert row["route"] == "/chat"
        assert row["requests"] == 2
        assert row["successes"] == 2
        assert row["auth_schemes"] == ["bearer"]
        assert "last_seen" in row
        assert raw not in out

    def test_table_renders_header_and_rows(self, projection, capsys):
        from app.cli import main

        reqlog, keys = projection
        key_id, _ = keys.create("ci")
        _seed(reqlog, key_id=key_id)
        reqlog.flush()

        main(["apps"])
        out, _ = capsys.readouterr()
        assert "CLIENT" in out
        assert "ci" in out
        assert key_id[:8] in out

    def test_empty_table_message(self, projection, capsys):
        from app.cli import main

        main(["apps"])
        out, _ = capsys.readouterr()
        assert "No connected applications." in out

    def test_nonpositive_limit_rejected(self, projection, capsys):
        from app.cli import main

        with pytest.raises(SystemExit) as exc:
            main(["apps", "--limit", "0"])
        assert exc.value.code == 2
