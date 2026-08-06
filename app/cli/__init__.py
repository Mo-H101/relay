import argparse

from app import __version__
from app.core import terminal
from app.core.config import reload_settings, settings
from app.providers.registry import PROVIDER_REGISTRY
from app.services.setup_state import read_setup_state


def _has_usable_provider() -> bool:
    """
    True when at least one provider is enabled with credentials, or a
    keyless local provider is enabled. Driven by the provider registry so
    adding a provider is a registry entry, not a new branch here.
    """
    from app.providers.factory import resolve_provider_key

    for defn in PROVIDER_REGISTRY.values():
        if not getattr(settings, defn.enabled_attr):
            continue

        if defn.kind == "local":
            return True

        if resolve_provider_key(defn):
            return True

    return False


def _config_configured() -> bool:
    """
    First-run detection hook: configured only when the setup-state marker
    says "configured" AND a usable provider still exists in settings.
    """
    return read_setup_state() == "configured" and _has_usable_provider()


def _cmd_tui() -> None:
    """
    Launch the Relay terminal interface.

    Refuses to start without an interactive terminal (TTY or Windows
    ConPTY), printing guidance instead of crashing. Otherwise re-reads
    `.env` before importing the relay facade so a freshly written setup
    (or an external edit) is reflected in this process's singletons, then
    runs the TUI with an embedded API server that is stopped on exit.
    """
    available, reason = terminal.tui_ready()

    if not available:
        terminal.print_tui_guidance(reason)
        raise SystemExit(0)

    reload_settings()

    from app.core.server import EmbeddedServer
    from app.ui.app import RelayApp

    server = EmbeddedServer()
    try:
        RelayApp(embedded_server=server).run()
    finally:
        server.stop()


def _cmd_setup(args) -> None:
    """
    Interactive setup wizard. On a completed, usable setup it hands off
    straight to the TUI (no second `relay` run needed).
    """
    from app.setup.ui import TerminalUI
    from app.setup.wizard import run_setup

    result = run_setup(TerminalUI())

    if result.usable:
        print("Relay setup complete.")
        _cmd_tui()
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
    the setup-state marker and starts the TUI on a completed setup.
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
        description=(
            "Relay — zero-friction AI gateway platform.\n\n"
            "Run 'relay' (or 'relay tui') for the terminal interface, "
            "'relay serve' for the headless API server, 'relay setup' "
            "to (re)run the setup wizard, 'relay keys' to manage API "
            "keys, 'relay provider keys' to manage provider keys, "
            "'relay events' to tail the security audit log, 'relay "
            "apps' to list connected applications, 'relay migrate' "
            "to consolidate legacy state files into platform.db, "
            "'relay conversations' to inspect project-continuity "
            "metadata, and 'relay config' to show, validate, or diff "
            "configuration."
        ),
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
    subparsers.add_parser(
        "tui",
        help="Launch the terminal interface (same as 'relay' with no "
             "arguments).",
    )
    subparsers.add_parser(
        "serve",
        help="Launch the headless API server (the pre-P2 'relay' "
             "behavior).",
    )

    keys = subparsers.add_parser(
        "keys",
        help="Manage Relay API keys (create, list, revoke, test).",
    )

    from app.cli.keys import add_keys_parser

    add_keys_parser(keys)

    provider = subparsers.add_parser(
        "provider",
        help="Manage provider configuration.",
    )
    provider_keys = provider.add_subparsers(dest="provider_command")
    pk = provider_keys.add_parser(
        "keys",
        help="Manage upstream provider API keys.",
    )

    from app.cli.provider_keys import add_provider_keys_parser

    add_provider_keys_parser(pk)

    migrate = subparsers.add_parser(
        "migrate",
        help="Consolidate legacy state files into platform.db (backup, "
             "verify, manifest; dry-run and rollback supported).",
    )

    from app.cli.migrate import add_migrate_flags

    add_migrate_flags(migrate)

    events = subparsers.add_parser(
        "events",
        help="Tail the durable security-event log (newest first).",
    )

    from app.cli.events import add_events_parser

    add_events_parser(events)

    conversations = subparsers.add_parser(
        "conversations",
        help="Project-continuity metadata surfaces (list, show, archive, "
             "prune).",
    )

    from app.cli.continuity import add_continuity_parser

    add_continuity_parser(conversations)

    apps = subparsers.add_parser(
        "apps",
        help="List connected applications (labeled keys x request log).",
    )

    from app.cli.apps import add_apps_parser

    add_apps_parser(apps)

    config = subparsers.add_parser(
        "config",
        help="Inspect configuration: show effective values (secrets "
             "masked), validate an env file, diff env files or the "
             "running process.",
    )

    from app.cli.config import add_config_parser

    add_config_parser(config)

    args = parser.parse_args(argv)

    if args.command == "setup":
        _cmd_setup(args)
    elif args.command == "tui":
        _cmd_tui()
    elif args.command == "serve":
        _cmd_serve()
    elif args.command == "keys":
        from app.cli.keys import _run_keys

        _run_keys(args, parser)
    elif args.command == "provider":
        from app.cli.provider_keys import _run_provider_keys

        _run_provider_keys(args, parser)
    elif args.command == "migrate":
        from app.cli.migrate import _run_migrate

        _run_migrate(args, parser)
    elif args.command == "events":
        from app.cli.events import _run_events

        _run_events(args, parser)
    elif args.command == "conversations":
        from app.cli.continuity import _run_continuity

        _run_continuity(args, parser)
    elif args.command == "apps":
        from app.cli.apps import _run_apps

        _run_apps(args, parser)
    elif args.command == "config":
        from app.cli.config import _run_config

        _run_config(args, config)
    elif args.command is None:
        if _config_configured():
            _cmd_tui()
        else:
            _first_run()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
