"""
Textual import-boundary tests.

The design constraint is that Textual may only be imported by ``app/ui``
(and ``app/ui/data.py`` and ``app/ui/theme.py`` must stay Textual-free
too). This suite enforces both statically and at runtime.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = PROJECT_ROOT / "app"
UI_ROOT = APP_ROOT / "ui"

_TEXTUAL_IMPORT = re.compile(r"(?m)^\s*(?:from\s+textual|import\s+textual)\b")

_CORE_OR_PROVIDER_IMPORT = re.compile(
    r"(?m)^\s*(?:from\s+app\.(?:core|providers)|import\s+app\.(?:core|providers))\b"
)


def test_no_textual_imports_outside_ui():
    offenders = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        if UI_ROOT in path.parents:
            continue
        if _TEXTUAL_IMPORT.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert not offenders, f"Textual imported outside app/ui: {offenders}"


def test_ui_data_layer_stays_textual_free():
    for filename in ("data.py", "theme.py"):
        text = (UI_ROOT / filename).read_text(encoding="utf-8")
        assert not _TEXTUAL_IMPORT.search(text), f"{filename} imports Textual"


def test_screens_do_not_import_core_or_providers():
    """
    P2e: screens must read Relay state through the ServiceFacade
    (``app.ui.data``) and never import ``app.core`` / ``app.providers``
    directly.
    """
    offenders = []
    for path in sorted((UI_ROOT / "screens").rglob("*.py")):
        if _CORE_OR_PROVIDER_IMPORT.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert not offenders, f"Screens import core/provider directly: {offenders}"


def test_core_and_cli_import_without_textual_in_runtime():
    """
    Import the server path, the CLI, and the embedded-server module in a
    subprocess and assert Textual never enters sys.modules. Guards against
    a stray top-level `import textual` slipping in via a core module.
    """
    probe = (
        "import sys; "
        "import app.cli, app.main, app.core.server, app.core.relay; "
        "assert 'textual' not in sys.modules, "
        "f'textual leaked: {sys.modules[\"textual\"]}'"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "size",
    [(100, 30), (80, 24), (60, 20), (40, 15)],
    ids=["100x30", "80x24", "60x20", "40x15"],
)
async def test_all_screens_mount_at_multiple_sizes(size):
    """
    Stage C: every redesigned screen must mount without error at all
    terminal sizes. Exercises CSS layout / overflow handling.
    """
    from app.ui.app import RelayApp

    app = RelayApp(start_server=False)
    async with app.run_test(
        headless=True, size=size, notifications=False
    ) as pilot:
        await pilot.pause()
        # Walk all 7 tabs
        for key in ("2", "3", "4", "5", "6", "7", "1"):
            await pilot.press(key)
            await pilot.pause()
