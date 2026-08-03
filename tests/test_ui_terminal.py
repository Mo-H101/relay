"""
P2e TTY/ConPTY preflight tests for ``app.core.terminal``.

The TUI must only start in a real interactive terminal; everywhere else
``app.cli`` prints guidance and exits cleanly instead of crashing. These
tests exercise the preflight predicate with fake streams and platform /
console mocks, plus the CLI guard path.
"""

import sys

import pytest

import app.core.terminal as terminal


class _FakeStream:
    def __init__(self, interactive: bool) -> None:
        self._interactive = interactive

    def isatty(self) -> bool:
        return self._interactive


def _patch_streams(monkeypatch, *, stdin=True, stdout=True) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStream(stdin))
    monkeypatch.setattr(sys, "stdout", _FakeStream(stdout))


# ------------------------------------------------------------- POSIX


def test_posix_interactive_stdin_and_stdout(monkeypatch):
    monkeypatch.setattr(terminal, "is_windows", lambda: False)
    _patch_streams(monkeypatch)
    available, reason = terminal.tui_ready()
    assert available is True
    assert reason == ""


def test_posix_non_interactive_stdin(monkeypatch):
    monkeypatch.setattr(terminal, "is_windows", lambda: False)
    _patch_streams(monkeypatch, stdin=False)
    available, reason = terminal.tui_ready()
    assert available is False
    assert "standard input" in reason


def test_posix_non_interactive_stdout(monkeypatch):
    monkeypatch.setattr(terminal, "is_windows", lambda: False)
    _patch_streams(monkeypatch, stdout=False)
    available, reason = terminal.tui_ready()
    assert available is False
    assert "standard output" in reason


def test_isatty_exception_is_treated_as_non_interactive(monkeypatch):
    class _BrokenStream:
        def isatty(self) -> bool:
            raise OSError("stream closed")

    monkeypatch.setattr(terminal, "is_windows", lambda: False)
    monkeypatch.setattr(sys, "stdin", _BrokenStream())
    monkeypatch.setattr(sys, "stdout", _FakeStream(True))
    available, reason = terminal.tui_ready()
    assert available is False
    assert "standard input" in reason


# ------------------------------------------------------------- Windows


def test_windows_conpty_env_allows_tui(monkeypatch):
    monkeypatch.setattr(terminal, "is_windows", lambda: True)
    monkeypatch.setattr(terminal, "_windows_console_available", lambda: False)
    monkeypatch.setenv("WT_SESSION", "relay-session")
    _patch_streams(monkeypatch)
    available, reason = terminal.tui_ready()
    assert available is True
    assert reason == ""


def test_windows_terminal_program_allows_tui(monkeypatch):
    monkeypatch.setattr(terminal, "is_windows", lambda: True)
    monkeypatch.setattr(terminal, "_windows_console_available", lambda: False)
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    _patch_streams(monkeypatch)
    available, _ = terminal.tui_ready()
    assert available is True


def test_windows_console_handle_allows_tui(monkeypatch):
    monkeypatch.setattr(terminal, "is_windows", lambda: True)
    monkeypatch.setattr(terminal, "_windows_console_available", lambda: True)
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    _patch_streams(monkeypatch)
    available, _ = terminal.tui_ready()
    assert available is True


def test_windows_no_console_handle_rejected(monkeypatch):
    monkeypatch.setattr(terminal, "is_windows", lambda: True)
    monkeypatch.setattr(terminal, "_windows_console_available", lambda: False)
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    _patch_streams(monkeypatch)
    available, reason = terminal.tui_ready()
    assert available is False
    assert "console" in reason


# ------------------------------------------------------------- guidance


def test_guidance_mentions_real_terminal_and_serve(capsys):
    terminal.print_tui_guidance("standard input is not an interactive terminal")
    out = capsys.readouterr().out
    assert "interactive terminal" in out
    assert "relay serve" in out
    assert "relay tui" in out


# ------------------------------------------------------------- CLI guard


def test_cli_tui_prints_guidance_and_exits_when_no_terminal(monkeypatch, capsys):
    import app.cli as cli

    monkeypatch.setattr(terminal, "tui_ready", lambda: (False, "no terminal"))
    with pytest.raises(SystemExit) as excinfo:
        cli._cmd_tui()
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "relay serve" in out
    assert "no terminal" in out
