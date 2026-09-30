"""Refuse to run when the executable lives on a world-writable path.

A binary dropped in ``/tmp`` or another directory anyone can write is a
PATH hijack. Homebrew's prefix and a normal home checkout are not
world-writable, so this stays quiet for a real install and for ``pytest``.
"""

from __future__ import annotations

import shutil
import stat
import sys
from pathlib import Path


class InstallPathError(Exception):
    """The running executable sits in a directory other users can write."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"refusing to run from a world-writable path: {path}")


def resolve_executable(argv0: str | None = None) -> Path:
    """Absolute path of the invoked binary, following PATH and symlinks."""
    raw = Path(sys.argv[0] if argv0 is None else argv0)
    if not raw.is_absolute():
        found = shutil.which(str(raw))
        if found:
            raw = Path(found)
    return raw.resolve()


def world_writable(path: Path) -> Path | None:
    """First world-writable path from ``path`` up through its ancestors.

    The executable itself counts: a mode ``0666`` binary is the same hole
    as a mode ``0777`` directory.
    """
    current = path
    seen: set[Path] = set()
    while current not in seen:
        seen.add(current)
        try:
            mode = current.stat().st_mode
        except OSError:
            mode = 0
        if mode & stat.S_IWOTH:
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def enforce_install_path(argv0: str | None = None) -> None:
    """Raise :class:`InstallPathError` when the executable is hijackable."""
    bad = world_writable(resolve_executable(argv0))
    if bad is not None:
        raise InstallPathError(bad)
