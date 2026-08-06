"""
Setup wizard flows (P1): every branch driven through ScriptedUI + fake
clients, with state and snapshot writes isolated to a temp dir.
"""

import json

import pytest

from app.providers.availability import AVAILABLE, UNAVAILABLE
from app.providers.base import ModelProbe
from app.providers.registry import ProviderDefinition
from app.setup.ui import ScriptedUI
from app.setup.wizard import SetupResult, run_setup


def make_client(
    key_status=200,
    models=("m1", "m2", "m3"),
    probes=None,
    key_statuses=None,
    list_error=None,
):
    """
    Build a fake setup-capable client class (constructed with no args, as
    ``ProviderDefinition.client`` requires). ``key_statuses`` overrides
    ``key_status`` to replay a sequence of key-check responses.
    """
    models = list(models)
    probes = probes or {}

    class FakeClient:
        checked = 0
        listed = 0

        def key_check(self, provider):
            FakeClient.checked += 1
            if key_statuses is not None:
                code = key_statuses[min(FakeClient.checked - 1, len(key_statuses) - 1)]
            else:
                code = key_status
            return code, "ok" if code == 200 else "invalid key"

        def list_models(self, provider):
            FakeClient.listed += 1
            if list_error is not None:
                raise list_error
            return list(models)

        def probe_model(self, provider, model):
            status = probes.get(model, AVAILABLE)
            if status == UNAVAILABLE:
                return ModelProbe(False, 5, 403, "denied")
            return ModelProbe(True, 5, 200, "")

    return FakeClient


class FakeStore:
    def __init__(self, env=None):
        self.writes = []
        self.env = dict(env or {})

    def get_env(self, key, default=""):
        return self.env.get(key, default)

    def set_provider_config(self, defn, **kwargs):
        self.writes.append((defn.id, kwargs))


def make_defn(
    provider_id,
    name,
    client_class,
    kind="cloud",
    key_env="FAKE_KEY",
):
    return ProviderDefinition(
        id=provider_id,
        display_name=name,
        provider_name=name,
        kind=kind,
        requires_api_key=(kind == "cloud"),
        key_env=key_env if kind == "cloud" else None,
        enabled_env="FAKE_ENABLED",
        key_attr="fake_key" if kind == "cloud" else None,
        enabled_attr="fake_enabled",
        base_url_env=None,
        base_url_default="http://fake/v1",
        priority_env="FAKE_PRIORITY",
        health_endpoint="/models",
        client_class=client_class,
        runtime_priority=1,
    )


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    from app.services import platform_store
    from app.services import setup_state
    from app.setup import persistence

    monkeypatch.setattr(setup_state, "state_dir", tmp_path)
    monkeypatch.setattr(persistence, "state_dir", tmp_path)
    monkeypatch.setattr(platform_store, "state_dir", tmp_path)
    return tmp_path


def test_first_run_welcome_then_skip_all_is_incomplete(isolated_state):
    menu = [make_defn("openai", "OpenAI", make_client())]
    ui = ScriptedUI([2])  # "Finish setup" is option 2 with one provider

    result = run_setup(ui, menu=menu, store=FakeStore())

    assert result.completed
    assert result.usable is False
    assert result.state == "incomplete"
    assert "Welcome to Relay!" in ui.notices
    assert "This looks like the first run" in "\n".join(ui.notices)
    assert result.configured == []

    from app.services import setup_state
    assert setup_state.read_setup_state() == "incomplete"


def test_full_cloud_flow_ends_configured(isolated_state):
    menu = [make_defn("openai", "OpenAI", make_client())]
    store = FakeStore()
    ui = ScriptedUI([1, "sk-test", "n", "n", 2])

    result = run_setup(ui, menu=menu, store=store)

    assert result.usable
    assert result.state == "configured"
    assert result.configured == ["openai"]
    assert store.writes == [("openai", {"enabled": True, "api_key": "sk-test"})]

    from app.setup import persistence

    statuses = persistence.read_model_status(isolated_state / "platform.db")
    assert set(statuses.get("openai", {}))  # scan results landed in model_status

    data = json.loads((isolated_state / "state.json").read_text(encoding="utf-8"))
    assert data["setup_state"] == "configured"
    assert data["configured_providers"] == ["openai"]
    assert "last_setup_at" in data


def test_custom_priority_restricted_to_available_models(isolated_state):
    probes = {"m2": UNAVAILABLE}
    menu = [make_defn("openai", "OpenAI", make_client(probes=probes))]
    store = FakeStore()
    # Choose provider, key, no view, priority yes, blank search, select 1 (only m1)
    ui = ScriptedUI([1, "sk-test", "n", "y", "", "1", 2])

    run_setup(ui, menu=menu, store=store)

    assert store.writes[-1][1]["priority_models"] == ["m1"]

    listing = "\n".join(ui.notices)
    assert "1. m1" in listing
    assert "2. m3" in listing
    assert "m2" not in listing  # unavailable model never offered


def test_invalid_key_retry_then_valid(isolated_state):
    menu = [make_defn("openai", "OpenAI", make_client(key_statuses=[401, 200]))]
    store = FakeStore()
    ui = ScriptedUI([1, "sk-bad", "r", "sk-good", "n", "n", 2])

    result = run_setup(ui, menu=menu, store=store)

    assert result.usable
    assert store.writes == [
        ("openai", {"enabled": True, "api_key": "sk-good"})
    ]
    assert any("Invalid API key" in n for n in ui.notices)
    assert any("Reason:" in n for n in ui.notices)


def test_invalid_key_skip_moves_to_next_provider(isolated_state):
    cloud = make_defn("openai", "OpenAI", make_client(key_status=401))
    local = make_defn("ollama", "Ollama (local)", make_client(), kind="local")
    store = FakeStore()
    # OpenAI: key -> invalid -> skip; Ollama: configure, no priority; Finish (option 3)
    ui = ScriptedUI([1, "sk-bad", "s", 2, "n", "n", 3])

    result = run_setup(ui, menu=[cloud, local], store=store)

    assert result.usable
    assert result.configured == ["ollama"]
    assert ("ollama", {"enabled": True}) in store.writes


def test_quota_reason_surfaced(isolated_state):
    menu = [make_defn("openai", "OpenAI", make_client(key_status=429))]
    ui = ScriptedUI([1, "sk-test", "s", 2])

    run_setup(ui, menu=menu, store=FakeStore())

    assert any("Quota exceeded or rate limited" in n for n in ui.notices)


def test_local_connectivity_failure_is_incomplete(isolated_state):
    from app.providers.exceptions import ProviderHTTPError

    menu = [
        make_defn(
            "ollama",
            "Ollama (local)",
            make_client(list_error=ProviderHTTPError(0, "connection refused")),
            kind="local",
        )
    ]
    ui = ScriptedUI([1, 2])

    result = run_setup(ui, menu=menu, store=FakeStore())

    assert result.usable is False
    assert result.state == "incomplete"
    assert any("Not reachable" in n for n in ui.notices)


def test_existing_key_kept_and_revalidated(isolated_state):
    # The wizard offers an existing key (from its store / keyring, G6)
    # and revalidates it instead of prompting for a fresh one.
    menu = [make_defn("openai", "OpenAI", make_client())]
    store = FakeStore(env={"FAKE_KEY": "old-key"})
    ui = ScriptedUI([1, "y", "n", "n", 2])

    result = run_setup(ui, menu=menu, store=store)

    assert result.usable
    assert store.writes == [("openai", {"enabled": True, "api_key": "old-key"})]


def test_deferred_provider_gets_runtime_note(isolated_state):
    menu = [make_defn("openrouter", "OpenRouter", make_client())]
    ui = ScriptedUI([1, "sk-test", "n", "n", 2])

    result = run_setup(ui, menu=menu, store=FakeStore())

    assert result.usable
    assert result.deferred == ["openrouter"]
    assert any("not wired into chat routing" in n for n in ui.notices)


def test_resume_message_for_incomplete_state(isolated_state):
    from app.services import setup_state
    setup_state.write_setup_state("incomplete")

    menu = [make_defn("openai", "OpenAI", make_client())]
    ui = ScriptedUI([2])

    run_setup(ui, menu=menu, store=FakeStore())

    assert any("Relay setup was not completed" in n for n in ui.notices)


def test_quit_without_finishing_is_not_complete(isolated_state):
    menu = [make_defn("openai", "OpenAI", make_client())]
    ui = ScriptedUI([3])  # Quit is option 3 with one provider

    result = run_setup(ui, menu=menu, store=FakeStore())

    assert result.completed is False
    assert result.usable is False
    assert result.state == "incomplete"


def test_script_exhaustion_fails_loudly(isolated_state):
    menu = [make_defn("openai", "OpenAI", make_client())]
    ui = ScriptedUI([1])  # too short: missing the rest of the flow

    with pytest.raises(RuntimeError, match="script exhausted"):
        run_setup(ui, menu=menu, store=FakeStore())


# ------------------------------------------------------------------ CLI handoff

def test_cli_hands_off_to_tui(monkeypatch, capsys):
    import app.cli as cli

    tui = []
    monkeypatch.setattr(cli, "_cmd_tui", lambda: tui.append(True))
    monkeypatch.setattr(
        "app.setup.wizard.run_setup",
        lambda ui: SetupResult(
            completed=True, usable=True, configured=["openai"], state="configured"
        ),
    )

    cli._cmd_setup(None)

    assert tui == [True]
    assert "Relay setup complete." in capsys.readouterr().out


def test_cli_incomplete_does_not_launch_tui(monkeypatch, capsys):
    import app.cli as cli

    tui = []
    monkeypatch.setattr(cli, "_cmd_tui", lambda: tui.append(True))
    monkeypatch.setattr(
        "app.setup.wizard.run_setup",
        lambda ui: SetupResult(completed=True, usable=False, state="incomplete"),
    )

    cli._cmd_setup(None)

    assert tui == []
    assert "not fully configured" in capsys.readouterr().out
