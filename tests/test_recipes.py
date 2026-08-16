"""Tests for backup recipes — built-ins, resolution, expansion, excludes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sanctum_cli import recipes
from sanctum_cli.config import CliConfig, Recipe
from sanctum_cli.errors import UserError


def test_builtin_recipes_have_expected_names() -> None:
    assert set(recipes.BUILTINS) == {"family", "operator", "code"}


def test_family_recipe_excludes_photos_libraries() -> None:
    r = recipes.BUILTINS["family"]
    assert any("Photos Library.photoslibrary" in e for e in r.excludes)
    assert r.auto_exclude_icloud_photos is True
    # Family is sized for free-tier; sources must be modest
    assert "~/Documents" in r.sources
    assert "~/.ssh" in r.sources


def test_operator_recipe_does_not_auto_exclude_photos() -> None:
    """Operators rarely have a Photos library on a Sanctum host; the recipe
    is meant to back up infra config, so the auto-exclude defaults off."""
    r = recipes.BUILTINS["operator"]
    assert r.auto_exclude_icloud_photos is False
    assert "~/.sanctum" in r.sources
    assert "~/.openclaw" in r.sources


def test_resolve_builtin() -> None:
    cfg = CliConfig()
    assert recipes.resolve("family", cfg).description.startswith("Crucial family data")


def test_resolve_user_override_wins() -> None:
    cfg = CliConfig(
        recipes={
            "family": Recipe(description="user's version", sources=["~/MyDocs"], target="primary")
        }
    )
    r = recipes.resolve("family", cfg)
    assert r.description == "user's version"
    assert r.sources == ["~/MyDocs"]


def test_resolve_unknown_raises_user_error() -> None:
    cfg = CliConfig()
    with pytest.raises(UserError) as ei:
        recipes.resolve("nonexistent", cfg)
    assert "unknown recipe" in ei.value.message
    assert "family" in ei.value.message  # advertises the available ones


def test_list_all_includes_overrides() -> None:
    cfg = CliConfig(
        recipes={"custom": Recipe(description="mine", sources=["~/x"], target="primary")}
    )
    rows = recipes.list_all(cfg)
    assert "custom" in rows
    assert "family" in rows
    assert rows["custom"].description == "mine"


def test_default_name_honors_config_then_falls_back_to_operator() -> None:
    assert recipes.default_name(CliConfig()) == "operator"
    assert recipes.default_name(CliConfig(default_recipe="family")) == "family"


def test_expand_sources_drops_nonexistent(tmp_path: Path) -> None:
    here = tmp_path / "here"
    here.mkdir()
    r = Recipe(
        description="x",
        sources=[str(here), str(tmp_path / "missing")],
        target="primary",
    )
    paths = recipes.expand_sources(r)
    assert len(paths) == 1
    assert paths[0] == here


def test_expand_sources_handles_tilde() -> None:
    r = Recipe(description="x", sources=["~/.zshrc"], target="primary")
    # ~/.zshrc may or may not exist depending on the box; test that ~ expands.
    out = recipes.expand_sources(r)
    if Path("~/.zshrc").expanduser().exists():
        assert out
        assert str(out[0]).startswith("/")


def test_effective_excludes_adds_photos_when_present_and_opted_in() -> None:
    r = Recipe(
        description="x",
        sources=["~"],
        excludes=["**/foo"],
        target="primary",
        auto_exclude_icloud_photos=True,
    )
    with patch("sanctum_cli.recipes.icloud_photos_present", return_value=True):
        out = recipes.effective_excludes(r)
    assert any("Photos Library.photoslibrary" in e for e in out)
    assert "**/foo" in out


def test_effective_excludes_skips_photos_when_absent() -> None:
    r = Recipe(
        description="x",
        sources=["~"],
        excludes=["**/foo"],
        target="primary",
        auto_exclude_icloud_photos=True,
    )
    with patch("sanctum_cli.recipes.icloud_photos_present", return_value=False):
        out = recipes.effective_excludes(r)
    assert "**/foo" in out
    assert not any("Photos Library.photoslibrary" in e for e in out if e != "**/foo")


def test_effective_excludes_does_not_dup_when_already_specified() -> None:
    """Operator recipe's excludes already mention Photos in MEDIA_LIBRARY — when
    auto-detect adds the same pattern, we shouldn't duplicate."""
    r = Recipe(
        description="x",
        sources=["~"],
        excludes=[
            "**/Photos Library.photoslibrary/**",
            "**/Pictures/Photos Library.photoslibrary/**",
        ],
        target="primary",
        auto_exclude_icloud_photos=True,
    )
    with patch("sanctum_cli.recipes.icloud_photos_present", return_value=True):
        out = recipes.effective_excludes(r)
    photos_count = sum(1 for e in out if "Photos Library.photoslibrary" in e)
    assert photos_count == 2  # only the original two; no duplication


def test_icloud_photos_detection_is_a_path_check(tmp_path: Path) -> None:
    """icloud_photos_present() is a heuristic on the file system."""
    # Test the function exists and is callable; precise behavior depends on host.
    result = recipes.icloud_photos_present()
    assert isinstance(result, bool)
