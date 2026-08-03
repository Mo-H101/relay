"""
Regression tests for the post-setup configuration refresh
(``app.core.config.reload_settings``).

Guards the P2 critical finding: `.env` writes via the wizard/config
store never touched ``os.environ``, so an already-imported ``settings``
singleton stayed stale. ``reload_settings()`` must rebuild the singleton
in place from the freshly written file.
"""

import os

import pytest

from app.core import config as config_mod


def _restore_environment(saved_env: dict) -> None:
    os.environ.clear()
    os.environ.update(saved_env)
    config_mod.settings.__init__()


def test_reload_settings_refreshes_singleton_after_env_write(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config_mod, "env_file", env_file)
    saved_env = dict(os.environ)

    try:
        env_file.write_text("RELAY_PORT=9000\n", encoding="utf-8")
        config_mod.reload_settings()
        assert config_mod.settings.relay_port == 9000

        env_file.write_text(
            "RELAY_PORT=9001\nRELAY_HOST=0.0.0.0\n", encoding="utf-8"
        )
        config_mod.reload_settings()
        assert config_mod.settings.relay_port == 9001
        assert config_mod.settings.relay_host == "0.0.0.0"
    finally:
        _restore_environment(saved_env)


def test_reload_settings_rejects_invalid_env(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config_mod, "env_file", env_file)
    saved_env = dict(os.environ)

    try:
        env_file.write_text("RELAY_PORT=not-a-number\n", encoding="utf-8")
        with pytest.raises(ValueError, match="RELAY_PORT"):
            config_mod.reload_settings()
    finally:
        _restore_environment(saved_env)
