import argparse

from app import __version__
from app.core.config import settings
from app.providers.registry import PROVIDER_REGISTRY
from app.services.setup_state import read_setup_state


def _has_usable_provider() -> bool:
    """
    True when at least one provider is enabled with credentials, or a
    keyless local provider is enabled. Driven by the provider registry so
    adding a provider is a registry entry, not a new branch here.
    """
    for defn in PROVIDER_REGISTRY.values():
        if not getattr(settings, defn.enabled_attr):
            continue

        if defn.kind == "local":
            return True

        key = getattr(settings, defn.key_attr) if defn.key_attr else ""

        if key:
            return True

    return False


def _config_configured() -> bool:
    """
    First-run detection hook: configured only when the setup-state marker
    says "configured" AND a usable provider still exists in settings.
    """
    return read_setup_state() == "configured" and _has_usable_provider()


def _cmd_setup(args) -> None:
    """
    Interactive setup wizard. On a completed, usable setup it hands off
    straight to the server (no second `relay` run needed).
    """
    from app.setup.ui import TerminalUI
    from app.setup.wizard import run_setup

    result = run_setup(TerminalUI())

    if result.usable:
        print("Relay setup complete.")
        _cmd_serve()
    elif result.completed:
        print(
            "Relay is not fully configured yet. "
            "Run 'relay' again to continue, or run 'relay setup'."
        )
    else:
        print("Setup cancelled. Run 'relay' when ready.")


def _first_run() -> None:
    """
    First-launch path: the wizard decides the welcome/resume wording from
    the setup-state marker and starts the server on a completed setup.
    """
    _cmd_setup(None)


def _cmd_serve() -> None:
    """Launch the Relay API server with uvicorn."""
    import uvicorn

    host = settings.relay_host
    port = settings.relay_port

    print(f"Starting Relay at http://{host}:{port}")
    uvicorn.run("app.main:app", host=host, port=port)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="relay",
        description="Relay — zero-friction AI gateway platform.",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "setup",
        help="Interactive setup: providers, API keys, model priority, "
             "and availability scans.",
    )

    args = parser.parse_args(argv)

    if args.command == "setup":
        _cmd_setup(args)
    elif args.command is None:
        if _config_configured():
            _cmd_serve()
        else:
            _first_run()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
