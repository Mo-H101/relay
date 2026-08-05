from app.core.config import settings

# The module-level Relay() singleton in app/core/relay.py performs network
# I/O (provider model discovery) at import time. Disable provider loading
# for the whole test session so importing app.main never hits the network.
settings.nvidia_enabled = False
settings.openai_enabled = False
settings.anthropic_enabled = False
settings.gemini_enabled = False
settings.lmstudio_enabled = False
settings.ollama_enabled = False

# Session-wide audit-log isolation. The auth dependency emits best-effort
# security events, and the module-level event-log accessor defaults to the
# real state dir. Point it at a throwaway temp database for the whole test
# session so no test touches (or creates) the developer's platform.db.
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from app.services import event_log as _event_log_module  # noqa: E402

_audit_log = _event_log_module.EventLog(
    str(Path(tempfile.mkdtemp(prefix="relay-test-audit-")) / "platform.db")
)
_event_log_module.event_log = lambda: _audit_log


@pytest.fixture
def isolated_event_log(monkeypatch, tmp_path):
    """
    Per-test isolated event log, overriding the session-wide audit log so
    tests can assert on exact event rows without cross-test contamination.
    """
    from app.services import event_log as event_log_module

    log = event_log_module.EventLog(str(tmp_path / "events.db"))
    monkeypatch.setattr(event_log_module, "event_log", lambda: log)
    yield log
    log.close()
