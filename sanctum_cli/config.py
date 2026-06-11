"""Configuration schema for sanctum-cli.

The CLI reads ``~/.sanctum/instance.yaml`` (or ``$SANCTUM_INSTANCE_FILE``)
and pulls out the ``cli:`` block. The block is optional — a fresh
install runs with sensible defaults; users add a ``cli:`` block when
they want to customize routing, providers, or telemetry.

Validation is via pydantic v2 with ``model_config = ConfigDict(extra="forbid")``
so typos in keys fail loudly with a precise pointer.
"""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from sanctum_cli.errors import ConfigError

DEFAULT_INSTANCE_FILE = Path("~/.sanctum/instance.yaml").expanduser()
ENV_INSTANCE_FILE = "SANCTUM_INSTANCE_FILE"


# ─── Model fragments ────────────────────────────────────────────────


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KeychainRef(StrictModel):
    """Pointer to a Keychain generic-password entry."""

    service: str
    account: str


class ClaudeProvider(StrictModel):
    """Claude routing config.

    ``via=proxy`` (default) talks to a local OpenAI-compatible proxy that
    shells out to the ``claude`` CLI — billing happens against the user's
    Max subscription, not the API. ``via=direct`` uses the official
    Anthropic SDK and bills against the API key in Keychain.
    """

    via: Literal["proxy", "direct"] = "proxy"
    endpoint: str = "http://127.0.0.1:2001"
    model: str = "claude-opus-4-7"
    keychain: KeychainRef = Field(
        default_factory=lambda: KeychainRef(service="anthropic-api-key", account="sanctum")
    )
    timeout_s: int = Field(default=300, ge=1, le=900)
    max_retries: int = Field(default=2, ge=0, le=10)
    max_tokens: int = Field(default=4096, ge=1, le=200_000)


class GeminiProvider(StrictModel):
    endpoint: str = "https://generativelanguage.googleapis.com"
    model: str = "gemini-2.5-pro"
    keychain: KeychainRef = Field(
        default_factory=lambda: KeychainRef(service="google-ai-api-key", account="sanctum")
    )
    timeout_s: int = Field(default=120, ge=1, le=600)
    max_retries: int = Field(default=2, ge=0, le=10)


class MlxLocalProvider(StrictModel):
    endpoint: str = "http://127.0.0.1:8900"
    model: str = "council-secure"
    timeout_s: int = Field(default=60, ge=1, le=600)
    always_available: bool = True


class Providers(StrictModel):
    claude: ClaudeProvider = Field(default_factory=ClaudeProvider)
    gemini: GeminiProvider = Field(default_factory=GeminiProvider)
    mlx_local: MlxLocalProvider = Field(default_factory=MlxLocalProvider)


# ─── Routing ────────────────────────────────────────────────────────


class RoutingRule(StrictModel):
    """A single routing rule — `when` matched, dispatch to `then`."""

    when: dict[str, Any]
    then: Literal["claude", "gemini", "mlx_local"]


class Routing(StrictModel):
    rules: list[RoutingRule] = Field(default_factory=list)
    fallback: Literal["claude", "gemini", "mlx_local"] = "claude"


# ─── Telemetry ──────────────────────────────────────────────────────


class Telemetry(StrictModel):
    enabled: bool = True
    path: Path = Path("~/.sanctum/telemetry/cli.jsonl")
    redact_prompts: bool = True
    aggregate_window_days: int = Field(default=7, ge=1, le=365)

    @field_validator("path", mode="before")
    @classmethod
    def expand_user(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v).expanduser()
        if isinstance(v, Path):
            return v.expanduser()
        return v


# ─── Cloud backup pointers ──────────────────────────────────────────


class CloudBackupRepo(StrictModel):
    kind: Literal["restic", "borg", "kopia"] = "restic"
    repo: str
    keychain: KeychainRef


class CloudBackupRetention(StrictModel):
    keep_daily: int = Field(default=7, ge=1)
    keep_weekly: int = Field(default=4, ge=0)
    keep_monthly: int = Field(default=12, ge=0)


class CloudBackup(StrictModel):
    primary: CloudBackupRepo | None = None
    secondary: CloudBackupRepo | None = None
    retention: CloudBackupRetention = Field(default_factory=CloudBackupRetention)


# ─── UI ──────────────────────────────────────────────────────────────


class UISettings(StrictModel):
    color: Literal["auto", "always", "never"] = "auto"
    progress: Literal["auto", "rich", "none"] = "auto"
    json_default: bool = False


# ─── Module overrides ────────────────────────────────────────────────


class ModuleConfig(StrictModel):
    """Per-module operator overrides.

    Only ``enabled`` for now (YAGNI). Keyed by module name under
    ``cli.modules`` in instance.yaml, e.g.::

        cli:
          modules:
            backup:
              enabled: false
    """

    enabled: bool = True


# ─── Top-level CLI section ──────────────────────────────────────────


class Recipe(StrictModel):
    """A backup recipe — a named bundle of sources/excludes for an audience.

    Recipes solve the ``what do I back up?`` question. ``family`` covers the
    crucial-but-small data a typical household cares about (documents,
    secrets) and is sized to fit the R2 free tier with dedup. ``operator``
    is the Sanctum-host configuration recipe used by the original bash
    script. Users can override built-ins or add new ones via ``instance.yaml``.
    """

    description: str = ""
    sources: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    target: Literal["primary", "secondary"] = "primary"
    auto_exclude_icloud_photos: bool = True


class CliConfig(StrictModel):
    """The ``cli:`` block in instance.yaml."""

    default_provider: Literal["claude", "gemini", "mlx_local"] = "claude"
    routing: Routing = Field(default_factory=Routing)
    providers: Providers = Field(default_factory=Providers)
    telemetry: Telemetry = Field(default_factory=Telemetry)
    cloud_backup: CloudBackup | None = None
    ui: UISettings = Field(default_factory=UISettings)
    recipes: dict[str, Recipe] = Field(default_factory=dict)
    default_recipe: str | None = None
    modules: dict[str, ModuleConfig] = Field(default_factory=dict)


class InstanceMetadata(StrictModel):
    """Subset of ``instance:`` block we actually read."""

    name: str
    slug: str
    timezone: str | None = None


class Config(StrictModel):
    """Composite of instance metadata + cli section.

    Other top-level keys in instance.yaml (services, paths, network, …)
    are deliberately not modeled here — they belong to other tools.
    """

    instance: InstanceMetadata
    cli: CliConfig = Field(default_factory=CliConfig)


# ─── Loader ──────────────────────────────────────────────────────────


def instance_path() -> Path:
    override = os.environ.get(ENV_INSTANCE_FILE)
    return Path(override).expanduser() if override else DEFAULT_INSTANCE_FILE


def load(path: Path | None = None) -> Config:
    """Read and validate the instance config.

    Raises ``ConfigError`` for any read or schema failure with a precise
    pointer to the offending key.
    """
    target = path or instance_path()
    if not target.exists():
        msg = f"instance config not found: {target}"
        raise ConfigError(
            msg,
            fix=(
                f"create {target} with at minimum:\n"
                "  instance:\n    name: My Sanctum\n    slug: my-sanctum\n"
            ),
        )

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        msg = f"YAML parse error in {target}: {exc}"
        raise ConfigError(msg, fix="fix the YAML syntax error noted above") from exc

    if not isinstance(raw, dict):
        msg = f"top-level YAML must be a mapping in {target}, got {type(raw).__name__}"
        raise ConfigError(msg)

    relevant = {k: raw[k] for k in ("instance", "cli") if k in raw}
    try:
        return Config.model_validate(relevant)
    except ValidationError as exc:
        msg = _format_validation_error(exc, target)
        raise ConfigError(msg) from exc


def _format_validation_error(exc: ValidationError, target: Path) -> str:
    lines = [f"schema violation in {target}:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


# ─── First-run scaffolding ───────────────────────────────────────────


def _default_identity() -> tuple[str, str]:
    """Derive a friendly ``(name, slug)`` from the hostname for first-run setup."""
    host = (socket.gethostname() or "sanctum").split(".")[0]
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "sanctum"
    return f"{host} Sanctum", slug


def scaffold_instance(path: Path | None = None) -> Path:
    """Write a minimal, valid ``instance.yaml`` when the user has none.

    Backs the ``sanctum init`` command and :func:`ensure` so a brand-new machine
    can run the CLI without hand-writing YAML. Returns the path written.
    """
    target = path or instance_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    name, slug = _default_identity()
    target.write_text(f"instance:\n  name: {name}\n  slug: {slug}\n", encoding="utf-8")
    return target


def ensure(path: Path | None = None) -> Config:
    """Load the instance config, scaffolding a minimal one if it is absent.

    :func:`load` hard-raises when the file is missing — correct for most
    commands, but fatal on a fresh Mac's first ``onboard``. ``ensure`` creates a
    minimal stub first so first-run works, then loads normally.
    """
    target = path or instance_path()
    if not target.exists():
        scaffold_instance(target)
    return load(target)
