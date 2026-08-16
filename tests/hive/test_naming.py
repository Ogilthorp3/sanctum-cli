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
    assert is_valid_hive_name("manoir")
    assert is_valid_hive_name("chalet")
    assert is_valid_hive_name("mbp")
    assert is_valid_hive_name("manoir-fw")
    assert is_valid_hive_name("manoir-ha")
    assert is_valid_hive_name("chalet-orbi")


def test_forbidden_patterns() -> None:
    assert not is_valid_hive_name("berts-mbp")
    assert not is_valid_hive_name("MM64")
    assert not is_valid_hive_name("mbp128")
    assert not is_valid_hive_name("10.0.0.10")
    assert not is_valid_hive_name("manoir.local")


def test_classify() -> None:
    assert classify_name("manoir") == "site_brain"
    assert classify_name("mbp") == "mobile"
    assert classify_name("manoir-fw") == "site_infra"
    assert classify_name("berts-mbp") == "invalid"


def test_legacy_alias_to_preferred() -> None:
    assert LEGACY_ALIASES["berts-mbp"] == "mbp"
    nodes = {
        "mbp": {"type": "mobile", "tailscale_name": "mbp", "aliases": ["berts-mbp"]},
    }
    assert preferred_name("berts-mbp", nodes) == "mbp"
    assert preferred_name("mbp", nodes) == "mbp"


def test_validate_dual_name_without_alias() -> None:
    problems = validate_node_naming(
        "mbp",
        {"type": "mobile", "tailscale_name": "berts-mbp"},
    )
    assert problems  # must complain


def test_validate_with_alias_ok() -> None:
    problems = validate_node_naming(
        "mbp",
        {"type": "mobile", "tailscale_name": "mbp", "aliases": ["berts-mbp"]},
    )
    assert problems == []


def test_suggest_infra() -> None:
    assert suggest_infra_name("manoir", "firewalla") == "manoir-fw"
    assert suggest_infra_name("chalet", "ha") == "chalet-ha"


def test_roster_duplicate_stems() -> None:
    nodes = {
        "mbp": {"aliases": ["road"]},
        "other": {"aliases": ["road"]},
    }
    problems = validate_roster_naming(nodes)
    assert any("road" in p for p in problems)
