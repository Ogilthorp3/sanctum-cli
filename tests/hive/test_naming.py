"""Hive naming contract tests."""

from __future__ import annotations

from sanctum_cli.hive.naming import (
    LEGACY_ALIASES,
    classify_name,
    is_valid_hive_name,
    preferred_name,
    suggest_infra_name,
    validate_node_naming,
    validate_roster_naming,
)


def test_valid_patterns() -> None:
    assert is_valid_hive_name("home")
    assert is_valid_hive_name("studio")
    assert is_valid_hive_name("mbp")
    assert is_valid_hive_name("home-fw")
    assert is_valid_hive_name("home-ha")
    assert is_valid_hive_name("studio-orbi")


def test_forbidden_patterns() -> None:
    assert not is_valid_hive_name("alice-mbp")
    assert not is_valid_hive_name("owner-mbp")
    assert not is_valid_hive_name("MM64")
    assert not is_valid_hive_name("mbp128")
    assert not is_valid_hive_name("10.0.0.10")
    assert not is_valid_hive_name("home.local")


def test_classify() -> None:
    assert classify_name("home") == "site_brain"
    assert classify_name("mbp") == "mobile"
    assert classify_name("home-fw") == "site_infra"
    assert classify_name("alice-mbp") == "invalid"


def test_legacy_alias_to_preferred() -> None:
    assert LEGACY_ALIASES == {}
    nodes = {
        "mbp": {"type": "mobile", "tailscale_name": "mbp", "aliases": ["alice-mbp"]},
    }
    assert preferred_name("alice-mbp", nodes) == "mbp"
    assert preferred_name("mbp", nodes) == "mbp"
    assert preferred_name("home-firewalla") == "home-fw"
    assert preferred_name("studio-homeassistant") == "studio-ha"


def test_validate_dual_name_without_alias() -> None:
    problems = validate_node_naming(
        "mbp",
        {"type": "mobile", "tailscale_name": "alice-mbp"},
    )
    assert problems  # must complain


def test_validate_with_alias_ok() -> None:
    problems = validate_node_naming(
        "mbp",
        {"type": "mobile", "tailscale_name": "mbp", "aliases": ["alice-mbp"]},
    )
    assert problems == []


def test_suggest_infra() -> None:
    assert suggest_infra_name("home", "firewalla") == "home-fw"
    assert suggest_infra_name("studio", "ha") == "studio-ha"


def test_roster_duplicate_stems() -> None:
    nodes = {
        "mbp": {"aliases": ["road"]},
        "other": {"aliases": ["road"]},
    }
    problems = validate_roster_naming(nodes)
    assert any("road" in p for p in problems)
