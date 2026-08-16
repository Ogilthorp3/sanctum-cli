"""Property tests + targeted unit tests for the pure router."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sanctum_cli.config import CliConfig, RoutingRule
from sanctum_cli.router import Attachment, Decision, Flags, Intent, route


@pytest.fixture
def base_cfg() -> CliConfig:
    return CliConfig()


def test_explicit_flag_wins(base_cfg: CliConfig) -> None:
    d = route(Intent(), [], Flags(provider="gemini"), base_cfg)
    assert d == Decision(provider="gemini", rule="flag.provider")


def test_env_override(base_cfg: CliConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_PROVIDER", "mlx_local")
    d = route(Intent(), [], Flags(), base_cfg)
    assert d.provider == "mlx_local"
    assert d.rule == "env.SANCTUM_PROVIDER"


def test_env_invalid_falls_through(base_cfg: CliConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_PROVIDER", "openai")
    d = route(Intent(), [], Flags(), base_cfg)
    assert d.provider == "claude"
    assert d.rule == "config.routing.fallback"


def test_subcommand_implied(base_cfg: CliConfig) -> None:
    d = route(Intent(kind="vision", implied_provider="gemini"), [], Flags(), base_cfg)
    assert d.provider == "gemini"
    assert d.rule == "intent.vision"


def test_config_rule_matches_image() -> None:
    cfg = CliConfig.model_validate(
        {
            "routing": {
                "rules": [{"when": {"has_image": True}, "then": "gemini"}],
                "fallback": "claude",
            }
        }
    )
    d = route(Intent(), [Attachment(kind="image")], Flags(), cfg)
    assert d.provider == "gemini"
    assert d.rule == "config.routing.rules[0]"


def test_first_matching_rule_wins() -> None:
    cfg = CliConfig.model_validate(
        {
            "routing": {
                "rules": [
                    {"when": {"has_image": True}, "then": "gemini"},
                    {"when": {"has_image": True}, "then": "mlx_local"},
                ],
                "fallback": "claude",
            }
        }
    )
    d = route(Intent(), [Attachment(kind="image")], Flags(), cfg)
    assert d.provider == "gemini"
    assert d.rule == "config.routing.rules[0]"


def test_offline_routes_to_mlx_local_when_available(base_cfg: CliConfig) -> None:
    d = route(Intent(), [], Flags(offline=True), base_cfg)
    assert d.provider == "mlx_local"
    assert d.rule == "offline.fallback"


def test_unknown_when_key_is_a_no_match() -> None:
    cfg = CliConfig.model_validate(
        {
            "routing": {
                "rules": [{"when": {"unsupported_key": True}, "then": "gemini"}],
                "fallback": "claude",
            }
        }
    )
    d = route(Intent(), [], Flags(), cfg)
    assert d.provider == "claude"
    assert d.rule == "config.routing.fallback"


def test_fallback_is_terminal(base_cfg: CliConfig) -> None:
    d = route(Intent(), [], Flags(), base_cfg)
    assert d.provider == base_cfg.routing.fallback
    assert d.rule == "config.routing.fallback"


# ─── Property tests ─────────────────────────────────────────────────


@given(
    flag=st.sampled_from(["claude", "gemini", "mlx_local"]),
    has_image=st.booleans(),
    offline=st.booleans(),
)
def test_explicit_flag_always_wins(flag: str, has_image: bool, offline: bool) -> None:
    """No matter what config or attachments say, the CLI flag has priority."""
    cfg = CliConfig.model_validate(
        {
            "routing": {
                "rules": [{"when": {"has_image": True}, "then": "gemini"}],
                "fallback": "mlx_local",
            }
        }
    )
    attachments = [Attachment(kind="image")] if has_image else []
    d = route(
        Intent(),
        attachments,
        Flags(provider=flag, offline=offline),  # type: ignore[arg-type]
        cfg,
    )
    assert d.provider == flag
    assert d.rule == "flag.provider"


@given(rule_then=st.sampled_from(["claude", "gemini", "mlx_local"]))
def test_route_returns_valid_provider_name(rule_then: str) -> None:
    """Whatever path is taken, the result is one of the known providers."""
    cfg = CliConfig.model_validate(
        {
            "routing": {
                "rules": [{"when": {"has_image": True}, "then": rule_then}],
                "fallback": "claude",
            }
        }
    )
    d = route(Intent(), [Attachment(kind="image")], Flags(), cfg)
    assert d.provider in {"claude", "gemini", "mlx_local"}


def test_validation_rejects_unknown_rule_target() -> None:
    """The schema, not the router, is the one that rejects bogus 'then' values."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RoutingRule.model_validate({"when": {"has_image": True}, "then": "openai"})
