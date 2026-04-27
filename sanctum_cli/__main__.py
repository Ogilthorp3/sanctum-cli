"""Allow ``python -m sanctum_cli`` as an alternate entry."""

from __future__ import annotations

from sanctum_cli.cli import app

if __name__ == "__main__":
    app()
