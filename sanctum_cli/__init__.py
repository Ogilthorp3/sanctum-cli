"""sanctum-cli — unified terminal binary for Sanctum hosts.

Public API is intentionally tiny. Everything user-facing lives behind the
``sanctum`` console entry point; library imports should be limited to
``__version__`` for tooling that wants to identify the version.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the installed package metadata (pyproject version).
    # Avoids the stale-hardcoded-literal drift that shipped "0.1.0a1" through v0.9.0.
    __version__ = version("sanctum-cli")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+source"

__all__ = ["__version__"]
