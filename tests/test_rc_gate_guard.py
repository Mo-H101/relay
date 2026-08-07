"""
RC gate guard: the release validation suite must fail loudly (not silently
skip) when the OpenAI SDK dependency is unavailable, so a broken or
incomplete test environment can never present the gate as green.

Runs a subprocess with the ``openai`` import simulated as missing and
asserts ``tests/test_rc_validation.py`` raises ``RuntimeError`` on
collection. Deliberately does not import openai at module level, so this
guard always runs even in an environment without the SDK.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_GUARD_PROBE = r"""
import builtins

real_import = builtins.__import__


def fake_import(name, *args, **kwargs):
    if name == "openai" or name.startswith("openai."):
        raise ImportError("No module named 'openai' (simulated missing)")
    return real_import(name, *args, **kwargs)


builtins.__import__ = fake_import

try:
    import tests.test_rc_validation  # noqa: F401
except RuntimeError as exc:
    print("RUNTIME_ERROR:", exc)
    raise SystemExit(0)
except Exception as exc:  # noqa: BLE001 - report any unexpected failure shape
    print("UNEXPECTED:", type(exc).__name__, exc)
    raise SystemExit(1)

print("NO_ERROR: gate collected without failing")
raise SystemExit(2)
"""


def test_rc_gate_fails_when_openai_missing() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _GUARD_PROBE],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"RC gate guard did not raise RuntimeError (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "RUNTIME_ERROR:" in proc.stdout
    assert "must never skip" in proc.stdout
