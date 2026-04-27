"""Provider factory — single import surface for ``router`` and the CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli import keychain
from sanctum_cli.errors import UserError
from sanctum_cli.providers.claude import ClaudeProvider
from sanctum_cli.providers.gemini import GeminiProvider
from sanctum_cli.providers.mlx_local import MlxLocalProvider

if TYPE_CHECKING:
    from sanctum_cli.config import Providers
    from sanctum_cli.providers.base import Provider


def make_provider(name: str, cfg: Providers) -> Provider:
    """Return a constructed provider for ``name``.

    API-key-bearing providers read from Keychain at construction time so a
    missing/locked Keychain surfaces immediately with an actionable error,
    not three hops deep inside a streaming generator.
    """
    if name == "claude":
        api_key = keychain.read(
            account=cfg.claude.keychain.account,
            service=cfg.claude.keychain.service,
        )
        return ClaudeProvider(cfg.claude, api_key)
    if name == "gemini":
        api_key = keychain.read(
            account=cfg.gemini.keychain.account,
            service=cfg.gemini.keychain.service,
        )
        return GeminiProvider(cfg.gemini, api_key)
    if name == "mlx_local":
        return MlxLocalProvider(cfg.mlx_local)
    msg = f"unknown provider: {name!r} (expected one of: claude, gemini, mlx_local)"
    raise UserError(msg)
