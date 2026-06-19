"""Built-in backup recipes.

A recipe is a named source-list + exclude-list bundle. The CLI ships three
defaults, all overridable via ``instance.yaml``::

    family   — crucial family data (documents, secrets) sized to fit R2 free.
    operator — Sanctum-host configuration; matches the bash sanctum-backup.sh.
    code     — source-code projects only; pairs with free GitHub Tier 0.

Resolution order at runtime: instance.yaml ``cli.recipes.<name>`` first,
falling back to BUILTINS. Users can extend with their own recipes too.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sanctum_cli.config import Recipe
from sanctum_cli.errors import UserError

if TYPE_CHECKING:
    from sanctum_cli.config import CliConfig


# Excludes shared by every recipe — never back these up regardless of
# audience. node_modules / .venv / __pycache__ are rebuildable; .DS_Store
# is noise; macOS spotlight + trashes are per-volume metadata.
COMMON_EXCLUDES: list[str] = [
    "**/.DS_Store",
    "**/.Spotlight-V100",
    "**/.Trashes",
    "**/.fseventsd",
    "**/node_modules",
    "**/.venv",
    "**/venv",
    "**/__pycache__",
    "**/*.pyc",
    "**/.pytest_cache",
    "**/.mypy_cache",
    "**/.ruff_cache",
    "**/target",  # Rust build dir
    "**/dist",
    "**/build",
    "**/.next",
]

# Media-library excludes — large bundles macOS apps manage themselves.
# We never want to back these up byte-by-byte; iCloud / Apple's services
# already cover them and the bundle structure is hostile to dedup.
MEDIA_LIBRARY_EXCLUDES: list[str] = [
    "**/Photos Library.photoslibrary/**",
    "**/Pictures/Photos Library.photoslibrary/**",
    "**/Music/iTunes/iTunes Media/**",
    "**/Music/Music/Media.localized/**",
    "**/Movies/TV Library.tvlibrary/**",
    "**/Movies/TV.app/**",
]


BUILTINS: dict[str, Recipe] = {
    "family": Recipe(
        description=(
            "Crucial family data — documents, secrets, keys. Sized to fit "
            "the R2 free tier (10 GB) with restic dedup. Photos handled "
            "by iCloud Photos; not backed up here."
        ),
        sources=[
            "~/Documents",
            "~/Desktop",
            "~/.ssh",
            "~/.gitconfig",
            "~/.zshrc",
            "~/.zprofile",
        ],
        excludes=COMMON_EXCLUDES + MEDIA_LIBRARY_EXCLUDES,
        target="primary",
        auto_exclude_icloud_photos=True,
    ),
    "operator": Recipe(
        description=(
            "Sanctum host configuration — matches the original bash "
            "sanctum-backup.sh source list. Use this on a Sanctum operator "
            "host (e.g. a hub Mac Mini or a laptop) to back up infra config + dev projects."
        ),
        sources=[
            "~/.sanctum",
            "~/.openclaw",
            "~/.claude",
            "~/Library/LaunchAgents",
            "~/.ssh",
            "~/.zshrc",
            "~/.zprofile",
            "~/.gitconfig",
            "~/.local/share/tts",
        ],
        excludes=[
            *COMMON_EXCLUDES,
            "**/*.safetensors",
            "**/*.gguf",
            "**/*.pth",
            "**/models",
            "**/.cache",
            "**/projects",  # ~/.claude/projects = transcripts; very large
            "**/file-history",
            "**/paste-cache",
            "**/usage-data",
            "**/shell-snapshots",
        ],
        target="primary",
        auto_exclude_icloud_photos=False,
    ),
    "code": Recipe(
        description=(
            "Source-code projects only. Designed to pair with a free private "
            "GitHub Tier 0 — git-tracked code lives there directly; this "
            "recipe captures uncommitted state + non-git project trees."
        ),
        sources=["~/Projects"],
        excludes=COMMON_EXCLUDES,
        target="primary",
        auto_exclude_icloud_photos=True,
    ),
}


# ─── Resolution ─────────────────────────────────────────────────────


def resolve(name: str, cfg: CliConfig) -> Recipe:
    """Return the recipe for ``name`` — user override wins over built-in."""
    if name in cfg.recipes:
        return cfg.recipes[name]
    if name in BUILTINS:
        return BUILTINS[name]
    available = sorted(set(BUILTINS) | set(cfg.recipes))
    msg = f"unknown recipe {name!r} (available: {', '.join(available)})"
    raise UserError(msg, fix="sanctum backup recipes  # to list available recipes")


def list_all(cfg: CliConfig) -> dict[str, Recipe]:
    """Return all recipes (built-ins + user overrides). Overrides win."""
    merged: dict[str, Recipe] = dict(BUILTINS)
    merged.update(cfg.recipes)
    return merged


def default_name(cfg: CliConfig) -> str:
    """Pick the active recipe name. Honors cli.default_recipe; else 'operator'."""
    if cfg.default_recipe:
        return cfg.default_recipe
    return "operator"


# ─── Helpers ────────────────────────────────────────────────────────


def expand_sources(recipe: Recipe) -> list[Path]:
    """Expand ``~`` and resolve recipe.sources into Path objects.

    Non-existent sources are silently dropped — the recipe may target
    paths that don't exist on every host (e.g., ~/Documents on a server)
    and we don't want a host-specific gap to fail every backup.
    """
    paths: list[Path] = []
    for s in recipe.sources:
        p = Path(s).expanduser()
        if p.exists():
            paths.append(p)
    return paths


def effective_excludes(recipe: Recipe) -> list[str]:
    """Recipe excludes, plus auto-detected iCloud Photos if requested."""
    excludes = list(recipe.excludes)
    if recipe.auto_exclude_icloud_photos and icloud_photos_present():
        # The MEDIA_LIBRARY_EXCLUDES already cover this for the family +
        # code recipes — but operators who opt in here still get the same
        # safety net regardless of what they put in the recipe.
        for pattern in (
            "**/Photos Library.photoslibrary/**",
            "**/Pictures/Photos Library.photoslibrary/**",
        ):
            if pattern not in excludes:
                excludes.append(pattern)
    return excludes


def icloud_photos_present() -> bool:
    """Heuristic: a Photos Library bundle exists in ~/Pictures.

    We don't try to read the iCloud Photos enabled-state — the bundle
    existing is sufficient grounds to skip it (Photos manages its own
    storage; backing up the bundle byte-by-byte fights both Apple and
    restic's dedup).
    """
    return Path("~/Pictures/Photos Library.photoslibrary").expanduser().exists()
