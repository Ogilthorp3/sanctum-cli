"""Tests for the env > config > default resolver."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli.discovery import env_var_for, resolve

if TYPE_CHECKING:
    import pytest


def test_env_var_naming() -> None:
    assert env_var_for("providers.claude.endpoint") == "SANCTUM_PROVIDERS_CLAUDE_ENDPOINT"
    assert env_var_for("foo-bar.baz") == "SANCTUM_FOO_BAR_BAZ"
    assert env_var_for("simple") == "SANCTUM_SIMPLE"


def test_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_FOO", "from-env")
    assert resolve("foo", "from-config", "from-default") == "from-env"


def test_config_wins_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANCTUM_FOO", raising=False)
    assert resolve("foo", "from-config", "from-default") == "from-config"


def test_default_when_both_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANCTUM_FOO", raising=False)
    assert resolve("foo", None, "from-default") == "from-default"


def test_empty_env_string_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty env value is still an explicit override; resolver returns it."""
    monkeypatch.setenv("SANCTUM_FOO", "")
    assert resolve("foo", "from-config", "from-default") == ""
