"""Fresh-Mac first-run scaffolding.

The v0.9.0 onboarding blocker: ``onboard`` called ``config.load()``, which
hard-raises ``ConfigError`` when ``~/.sanctum/instance.yaml`` is absent — so a
stranger on a brand-new Mac hit a wall on the documented one-command path.
``config.ensure()`` fixes that by scaffolding a minimal, valid instance file
when none exists, then loading it.
"""
from __future__ import annotations

from sanctum_cli import config


def test_ensure_scaffolds_missing_instance_file(tmp_path):
    target = tmp_path / "instance.yaml"
    assert not target.exists()

    cfg = config.ensure(target)

    assert target.exists(), "ensure() must create the file when absent"
    assert cfg.instance.name, "scaffolded config needs a usable name"
    assert cfg.instance.slug, "scaffolded config needs a usable slug"
    # the scaffolded file must satisfy the strict loader on its own
    assert config.load(target).instance.slug == cfg.instance.slug


def test_ensure_does_not_clobber_existing(tmp_path):
    target = tmp_path / "instance.yaml"
    target.write_text("instance:\n  name: Existing Haus\n  slug: existing-haus\n")

    cfg = config.ensure(target)

    assert cfg.instance.name == "Existing Haus", "must not overwrite an existing file"
    assert cfg.instance.slug == "existing-haus"


def test_scaffold_slug_is_filesystem_safe(tmp_path):
    target = tmp_path / "instance.yaml"
    cfg = config.ensure(target)
    # slug is used in bucket names (sanctum-restic-<slug>-…) so it must be lowercase
    # and contain only url/bucket-safe chars.
    assert cfg.instance.slug == cfg.instance.slug.lower()
    assert all(c.isalnum() or c == "-" for c in cfg.instance.slug)
    assert cfg.instance.slug, "slug must not be empty even from a weird hostname"


def test_version_matches_package_metadata():
    """`sanctum --version` must report the real installed version, not the
    hardcoded '0.1.0a1' literal that lingered through the v0.9.0 release."""
    from importlib.metadata import version

    import sanctum_cli

    assert sanctum_cli.__version__ == version("sanctum-cli")
    assert sanctum_cli.__version__ != "0.1.0a1"
