"""
P1 setup wizard package.

Exposes the wizard entry point; the rest is internal (UI, key validation,
scan engine, reporting, persistence).
"""

from app.setup.wizard import SetupResult, run_setup

__all__ = ["run_setup", "SetupResult"]
