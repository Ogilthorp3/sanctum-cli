"""Pure routing function.

Given an intent, attachments, flags, and config, ``route()`` returns
the chosen provider name and the rule that fired. No I/O, no global
state — easy to property-test.

Override hierarchy (highest first):
    1. CLI flag    --provider=claude|gemini|mlx_local
    2. Env var     SANCTUM_PROVIDER=...
    3. Subcommand  vision/code/local imply a provider
    4. Config rules (first match wins)
    5. Connectivity gate (offline → mlx_local if always_available)
    6. Fallback (config.routing.fallback)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from sanctum_cli.config import CliConfig

ProviderName = Literal["claude", "gemini", "mlx_local"]
ENV_PROVIDER = "SANCTUM_PROVIDER"
VALID_PROVIDERS: frozenset[str] = frozenset({"claude", "gemini", "mlx_local"})


@dataclass(frozen=True, slots=True)
class Intent:
    """What the operator is asking for."""

    kind: Literal["chat", "vision", "code", "spatial", "general"] = "general"
    implied_provider: ProviderName | None = None


@dataclass(frozen=True, slots=True)
class Attachment:
    kind: Literal["image", "video", "text", "file"]
    path: str | None = None


@dataclass(frozen=True, slots=True)
class Flags:
    provider: ProviderName | None = None
    offline: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    """Output of route() — Provider name + audit trail."""

    provider: ProviderName
    rule: str  # human-readable identifier of the matched rule
    overrides: tuple[str, ...] = field(default_factory=tuple)


def _matches(when: dict[str, object], intent: Intent, attachments: list[Attachment]) -> bool:
    """Tiny matcher for routing rule predicates.

    Supported keys:
        has_image: bool
        has_video: bool
        intent: 'chat'|'vision'|'code'|'spatial'|'general'
        offline: bool  (consulted via Flags, not here)
    """
    for key, expected in when.items():
        if key == "has_image":
            actual = any(a.kind == "image" for a in attachments)
            if actual != expected:
                return False
        elif key == "has_video":
            actual = any(a.kind == "video" for a in attachments)
            if actual != expected:
                return False
        elif key == "intent":
            if intent.kind != expected:
                return False
        elif key == "offline":
            return False  # offline routed elsewhere; rule-level offline n/a here
        else:
            return False
    return True


def route(
    intent: Intent,
    attachments: list[Attachment],
    flags: Flags,
    cfg: CliConfig,
) -> Decision:
    """Pure dispatcher. Order is documented above and tested in tests/test_router.py."""

    # 1. Explicit CLI flag
    if flags.provider:
        return Decision(flags.provider, rule="flag.provider")

    # 2. Env override
    env_provider = os.environ.get(ENV_PROVIDER, "").strip().lower()
    if env_provider:
        if env_provider not in VALID_PROVIDERS:
            # Treat invalid env as if unset; surface the issue elsewhere
            pass
        else:
            return Decision(env_provider, rule="env.SANCTUM_PROVIDER")  # type: ignore[arg-type]

    # 3. Subcommand-implied
    if intent.implied_provider is not None:
        return Decision(intent.implied_provider, rule=f"intent.{intent.kind}")

    # 4. Config rules
    for idx, rule in enumerate(cfg.routing.rules):
        if _matches(rule.when, intent, attachments):
            return Decision(rule.then, rule=f"config.routing.rules[{idx}]")

    # 5. Offline → MLX-local if available
    if flags.offline and cfg.providers.mlx_local.always_available:
        return Decision("mlx_local", rule="offline.fallback")

    # 6. Configured fallback
    return Decision(cfg.routing.fallback, rule="config.routing.fallback")
