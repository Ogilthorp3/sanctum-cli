"""sanctum-cli — unified terminal binary for Sanctum hosts.

Public API is intentionally tiny. Everything user-facing lives behind the
``sanctum`` console entry point; library imports should be limited to
``__version__`` for tooling that wants to identify the version.
"""

from __future__ import annotations

__version__ = "0.1.0a1"
__all__ = ["__version__"]
