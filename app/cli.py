import argparse
import os
import sys

from dotenv import set_key

from app.core.config import PROJECT_ROOT, settings
from app.providers.base import Provider
from app.providers.nvidia_client import NvidiaClient
from app.providers.openai_client import OpenAIClient
from app.services.routing import TASK_CATEGORIES

ENV_FILE = PROJECT_ROOT / ".env"

_PROVIDERS = [
    {
        "name": "NVIDIA",
        "key_env": "NVIDIA_API_KEY",
        "enabled_env": "NVIDIA_ENABLED",
        "enabled_attr": "nvidia_enabled",
        "priority_env": "NVIDIA_MODEL_PRIORITY",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "client_class": NvidiaClient,
    },
    {
        "name": "OpenAI",
        "key_env": "OPENAI_API_KEY",
        "enabled_env": "OPENAI_ENABLED",
        "enabled_attr": "openai_enabled",
        "priority_env": "OPENAI_MODEL_PRIORITY",
        "base_url": "https://api.openai.com/v1",
        "client_class": OpenAIClient,
    },
]

_TASK_LABELS = {
    "coding": "Coding",
    "vision": "Vision",
    "reasoning": "Reasoning",
    "general": "General",
    "creative": "Creative",
    "translation": "Translation",
}


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""

    try:
        return input(f"{prompt}{suffix}: ").strip() or (default or "")
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def _ask_yes_no(prompt: str, default: bool) -> bool:
    value = _ask(
        f"{prompt} (y/n)",
        "y" if default else "n",
    ).lower()

    if value not in ("y", "n"):
        print(f"  Please answer 'y' or 'n'.")
        return _ask_yes_no(prompt, default)

    return value == "y"


def _mask_key(key: str) -> str:
    if len(key) <= 4:
        return "*" * len(key)

    return f"{'*' * 8}{key[-4:]}"


def _update_env(key: str, value: str) -> None:
    set_key(str(ENV_FILE), key, value, quote_mode="always")


def _parse_selection(raw: str, max_index: int):
    """
    Strictly parse a comma-separated list of 1-based indices.
    Rejects malformed input instead of silently skipping it.
    """

    tokens = [token.strip() for token in raw.split(",")]

    if not tokens:
        return []

    indices = []

    for token in tokens:

        if not token.isdigit():
            raise ValueError(
                f"'{token}' is not a valid number. "
                f"Use comma-separated numbers, e.g. 1,3,7."
            )

        number = int(token)

        if not (1 <= number <= max_index):
            raise ValueError(
                f"{number} is out of range (choose 1-{max_index})."
            )

        if number - 1 not in indices:
            indices.append(number - 1)

    return indices


def _select_models(models, prompt: str):
    """
    Searchable, strictly-validated model selector.
    Returns the selected model ids in selection order, or [] to skip.
    """

    if not models:
        print("  No models available.")
        return []

    shown = models

    while True:

        query = _ask(f"{prompt} (search text, or blank to list all)")

        lowered = query.strip().lower()

        shown = [
            model for model in models
            if not lowered or lowered in model.lower()
        ]

        if not shown:
            print("  No models match that search. Try again.")
            continue

        visible = shown[:60]

        if len(shown) > 60:
            print(
                f"  {len(shown)} models match "
                f"(showing first {len(visible)}; refine the search to see more):"
            )
        else:
            print(f"  {len(shown)} model(s) match:")

        for index, model in enumerate(visible, start=1):
            print(f"  {index:>3}. {model}")

        raw = _ask(
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
            print(f"  Invalid input: {exc}")
            continue

        return [visible[index] for index in indices]


def _setup_provider(cfg):
    """
    Configure a single provider. Returns the Provider (with models) or
    None when the provider is disabled or has no API key.
    """

    name = cfg["name"]

    print(f"\n== {name} ==")

    current_enabled = getattr(settings, cfg["enabled_attr"])
    enabled = _ask_yes_no(f"Enable {name}", current_enabled)

    _update_env(cfg["enabled_env"], "true" if enabled else "false")

    if not enabled:
        print(f"  {name} disabled.")
        return None

    key_env = cfg["key_env"]
    current_key = os.getenv(key_env, "")

    if current_key:

        if _ask_yes_no(
            f"Existing API key detected ({_mask_key(current_key)}). "
            f"Keep it?",
            True,
        ):
            key = current_key
        else:
            key = _ask("Enter a new API key (blank = keep current)")

            if not key:
                key = current_key
                print("  Keeping current key.")
            else:
                _update_env(key_env, key)
    else:

        key = _ask(f"API key (blank to skip)")

        if not key:
            print(f"  No API key set. Skipping model setup for {name}.")
            return None

        _update_env(key_env, key)

    provider = Provider(
        name=name,
        base_url=cfg["base_url"],
        api_key=key,
    )

    client = cfg["client_class"]()

    try:
        models = client.list_models(provider)
    except Exception as exc:
        print(f"  Could not fetch models: {exc}")
        return provider

    print(f"  {len(models)} models available.")

    provider.models = models

    if _ask_yes_no("Set a custom model priority order?", False):
        priority = _select_models(
            models,
            "Select models to prioritize (order = priority)",
        )

        if priority:
            _update_env(cfg["priority_env"], ",".join(priority))
            print(f"  Model priority saved ({len(priority)} models).")
        else:
            print("  Keeping default model order.")
    else:
        print("  Keeping default model order.")

    return provider


def _configure_routing(providers) -> None:
    """
    Optional task-specific routing. Skippable end to end; every category
    is individually skippable.
    """

    print("\n== Task Routing ==")

    enabled = _ask_yes_no(
        "Would you like to configure task-specific routing?",
        False,
    )

    if not enabled:
        _update_env("TASK_ROUTING_ENABLED", "false")
        print("  Task routing skipped (normal priority ordering used).")
        return

    pool = []

    for provider in providers.values():
        for model in provider.models:
            if model not in pool:
                pool.append(model)

    if not pool:
        print("  No models available for routing. Skipped.")
        _update_env("TASK_ROUTING_ENABLED", "false")
        return

    assigned_any = False

    for category in TASK_CATEGORIES:
        label = _TASK_LABELS[category]
        env_name = f"TASK_{category.upper()}"

        existing = [
            model for model in os.getenv(env_name, "").split(",") if model
        ]

        if existing:
            print(f"  Currently assigned: {', '.join(existing)}")

        if not _ask_yes_no(f"Assign preferred models for {label}?", False):
            print(f"  {label} skipped (uses normal ordering).")
            _update_env(env_name, "")
            continue

        selected = _select_models(
            pool,
            f"Select preferred models for {label}",
        )

        if selected:
            _update_env(env_name, ",".join(selected))
            assigned_any = True
            print(f"  {label} routing saved ({len(selected)} models).")
        else:
            print(f"  {label} skipped (uses normal ordering).")
            _update_env(env_name, "")

    _update_env(
        "TASK_ROUTING_ENABLED",
        "true" if assigned_any else "false",
    )

    if assigned_any:
        print("  Task routing saved.")
    else:
        print("  Task routing skipped (normal priority ordering used).")


def _test_provider(cfg, provider) -> None:
    """
    Verify a provider API key by probing its highest-priority model.
    """

    client = cfg["client_class"]()

    model = provider.models[0] if provider.models else None

    if not model:
        print(f"  No models to test for {cfg['name']}.")
        return

    print(f"  Testing {cfg['name']} with '{model}' ...")

    try:
        probe = client.probe_model(provider, model)
    except Exception as exc:
        print(f"  FAILED: {exc}")
        return

    if probe.healthy:
        print(f"  OK: {model} (latency {probe.latency_ms}ms)")
        return

    reason = probe.error or (
        f"HTTP {probe.status_code}" if probe.status_code else "unknown error"
    )

    print(f"  FAILED: {model} ({reason})")


def _cmd_setup(args) -> None:
    print("Relay setup")

    providers = {}

    for cfg in _PROVIDERS:
        provider = _setup_provider(cfg)

        if provider is not None:
            providers[cfg["name"]] = provider

    _configure_routing(providers)

    print("\nConfiguration saved to .env. Restart the server to apply.")

    for cfg in _PROVIDERS:
        provider = providers.get(cfg["name"])

        if provider is None or not provider.models:
            continue

        if _ask_yes_no(f"Test {cfg['name']} provider now?", True):
            _test_provider(cfg, provider)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="relay",
        description="Relay configuration CLI.",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "setup",
        help="Interactive setup: providers, API keys, model priority, "
             "task routing, and provider tests.",
    )

    args = parser.parse_args(argv)

    if args.command == "setup":
        _cmd_setup(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
