"""
Setup wizard orchestration (the product spec's six steps).

``run_setup`` prints the welcome, walks the numbered provider menu, and for
each selected provider runs the full flow: key entry + live validation
(cloud) or connectivity check (local), catalog discovery, a one-bar
availability scan, optional model priority (restricted to available
models), and a single ``config_store`` write. On completion it writes the
setup-state marker and returns a ``SetupResult`` the CLI uses to hand off
to the server.

The wizard never touches dotenv directly; every persistence write goes
through ``app.services.config_store`` and ``app.setup.persistence``.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import List

from app.providers.availability import GLYPH, UNAVAILABLE
from app.providers.registry import PROVIDER_MENU, RUNTIME_READY
from app.services import config_store, setup_state
from app.setup.key_validation import resolve_cloud_key
from app.setup.persistence import write_model_status
from app.setup.reporting import ROLLING_WINDOW
from app.setup.scan import ScanEngine

_MAX_VISIBLE = 60


@dataclass
class SetupResult:
    """
    Outcome of one wizard run.
    """

    completed: bool  # user reached "Finish" (vs. quit early)
    usable: bool  # at least one usable provider configured
    state: str = "incomplete"  # state marker written
    configured: List[str] = field(default_factory=list)
    deferred: List[str] = field(default_factory=list)


def _parse_selection(raw: str, max_index: int) -> List[int]:
    """
    Strictly parse a comma-separated list of 1-based indices.
    """
    tokens = [token.strip() for token in raw.split(",")]

    indices: List[int] = []

    for token in tokens:
        if not token.isdigit():
            raise ValueError(
                f"'{token}' is not a valid number. "
                f"Use comma-separated numbers, e.g. 1,3,7."
            )

        number = int(token)

        if not (1 <= number <= max_index):
            raise ValueError(f"{number} is out of range (choose 1-{max_index}).")

        if number - 1 not in indices:
            indices.append(number - 1)

    return indices


def _select_available(ui, models: List[str], prompt: str) -> List[str]:
    """
    Searchable, strictly-validated model selector over an available set.
    Returns the selected ids in selection order, or [] to skip.
    """
    if not models:
        ui.notice("  No models available.")
        return []

    shown = models

    while True:
        query = ui.ask(f"{prompt} (search text, or blank to list all)").strip().lower()

        shown = [
            model for model in models if not query or query in model.lower()
        ]

        if not shown:
            ui.notice("  No models match that search. Try again.")
            continue

        visible = shown[:_MAX_VISIBLE]

        if len(shown) > _MAX_VISIBLE:
            ui.notice(
                f"  {len(shown)} models match "
                f"(showing first {len(visible)}; refine the search to see more):"
            )
        else:
            ui.notice(f"  {len(shown)} model(s) match:")

        for index, model in enumerate(visible, start=1):
            ui.notice(f"  {index:>3}. {model}")

        raw = ui.ask(
            "Enter numbers (comma-separated), 's' to search again, "
            "or blank to skip"
        )

        if raw.strip().lower() == "s":
            continue

        if not raw.strip():
            return []

        try:
            indices = _parse_selection(raw, len(visible))
        except ValueError as exc:
            ui.notice(f"  Invalid input: {exc}")
            continue

        return [visible[index] for index in indices]


def _save(store, defn, api_key: str | None = None, priority: List[str] | None = None):
    kwargs = {"enabled": True}

    if api_key is not None:
        kwargs["api_key"] = api_key
    if priority:
        kwargs["priority_models"] = priority

    store.set_provider_config(defn, **kwargs)


def _scan(ui, client, provider, models: List[str]):
    engine = ScanEngine()
    reporter = ui.progress()
    reporter.begin_scan(len(models))

    recent = deque(maxlen=ROLLING_WINDOW)

    def on_update(done, total, result):
        recent.append(result)
        reporter.update(done, total, result.model, list(recent))

    results = engine.scan(client, provider, models, on_update=on_update)
    reporter.end_scan(results)

    available = [result.model for result in results if result.status != UNAVAILABLE]

    if ui.ask_yes_no("View available models? (y/n)", False):
        reporter.detail(results)

    return available, results


def _catalog_and_scan(
    ui,
    defn,
    client,
    provider,
    store,
    *,
    api_key: str | None = None,
    models: List[str] | None = None,
) -> bool:
    if models is None:
        ui.notice("  Fetching model catalog...")

        try:
            models = client.list_models(provider)
        except Exception as exc:  # noqa: BLE001 - surface, don't crash
            ui.notice(f"  Could not fetch models: {exc}")
            _save(store, defn, api_key)
            return True

    ui.notice(f"  Found: {len(models)} models")

    available, results = _scan(ui, client, provider, models)
    write_model_status(defn.id, results)

    priority = None

    if available and ui.ask_yes_no("Set a custom model priority order?", False):
        priority = _select_available(
            ui,
            available,
            "Select models to prioritize (order = priority)",
        )

        if not priority:
            ui.notice("  Keeping default model order.")

    _save(store, defn, api_key, priority)
    return True


def _configure_cloud(ui, defn, client, provider, store) -> bool:
    from app.core.config import settings
    from app.services.provider_key_store import provider_key_store

    # Keyring-aware existing-key detection (G6): a keyring-only install
    # (post `relay provider keys migrate`) is seen as configured, so the
    # wizard offers the stored key instead of prompting for a fresh one.
    # A keyring entry wins; otherwise the wizard's own store (the .env
    # source of truth) supplies the existing key, exactly as before.
    current_key = ""

    if defn.key_env:
        if settings.relay_keyring_enabled:
            try:
                current_key = provider_key_store.get(defn.id)
            except Exception:
                current_key = ""

        if not current_key:
            get_env = getattr(store, "get_env", None)
            current_key = get_env(defn.key_env) if get_env else ""

    outcome = resolve_cloud_key(ui, defn, client, provider, current_key)

    if outcome.action == "skipped":
        ui.notice(f"  {defn.display_name} skipped.")
        return False

    provider.api_key = outcome.api_key

    return _catalog_and_scan(
        ui,
        defn,
        client,
        provider,
        store,
        api_key=outcome.api_key,
    )


def _configure_local(ui, defn, client, provider, store) -> bool:
    ui.notice(f"  Checking {defn.display_name} connectivity...")

    try:
        models = client.list_models(provider)
    except Exception as exc:  # noqa: BLE001 - surface, don't crash
        ui.notice(f"  {GLYPH[UNAVAILABLE]} Not reachable: {exc}")
        return False

    ui.notice(f"  Found: {len(models)} models")

    return _catalog_and_scan(ui, defn, client, provider, store, models=models)


def _configure_provider(ui, defn, store) -> bool:
    ui.notice(f"== {defn.display_name} ==")

    client = defn.client()
    provider = defn.build_provider()

    if defn.kind == "local":
        return _configure_local(ui, defn, client, provider, store)

    return _configure_cloud(ui, defn, client, provider, store)


def _write_state(state: str, provider_ids: List[str]) -> None:
    setup_state.write_setup_state(
        state,
        configured_providers=provider_ids,
        last_setup_at=time.time(),
    )


def _finish(ui, configured, completed: bool) -> SetupResult:
    provider_ids = sorted(configured)
    deferred = [pid for pid in provider_ids if pid not in RUNTIME_READY]

    if not provider_ids:
        _write_state("incomplete", [])
        return SetupResult(completed=completed, usable=False, state="incomplete")

    _write_state("configured", provider_ids)

    if not any(pid in RUNTIME_READY for pid in provider_ids):
        ui.notice(
            "Note: the configured providers are not wired into chat routing "
            "yet; that lands in a later phase."
        )

    return SetupResult(
        completed=completed,
        usable=True,
        state="configured",
        configured=provider_ids,
        deferred=deferred,
    )


def run_setup(
    ui,
    *,
    menu=None,
    store=None,
) -> SetupResult:
    """
    Run the interactive setup wizard and return its outcome.

    ``menu``/``store`` are injectable for tests; defaults come from the
    provider registry and the single-writer config store.
    """
    if menu is None:
        menu = PROVIDER_MENU
    store = store or config_store

    state = setup_state.read_setup_state()

    if state == "not_configured":
        ui.notice("Welcome to Relay!")
        ui.notice("This looks like the first run. Let's configure it.")
    elif state == "incomplete":
        ui.notice("Relay setup was not completed. Let's finish it.")
    else:
        ui.notice("Relay setup")

    configured = {}

    while True:
        options = [defn.display_name for defn in menu] + ["Finish setup", "Quit"]
        choice = ui.menu(options, "Select a provider to configure")

        if choice is None or choice == len(options):
            return _finish(ui, configured, completed=False)

        if choice == len(options) - 1:
            break

        defn = menu[choice - 1]

        if _configure_provider(ui, defn, store):
            configured[defn.id] = True

    return _finish(ui, configured, completed=True)
