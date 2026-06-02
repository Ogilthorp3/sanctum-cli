"""Declarative module manifest — the contract a Sanctum module ships.

A module describes the services it owns, the secrets it needs, its health
probes, its alert routing, its uninstall steps, docs, demo, and soak target.
The manifest is the boundary between a module and the spine; consumers
(doctor --ship, module commands, self-test) never reach past it.
"""
from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

if TYPE_CHECKING:
    from pathlib import Path


class ManifestError(ValueError):
    """Raised when a manifest is structurally valid YAML but semantically invalid."""


class ServiceKind(StrEnum):
    launchagent = "launchagent"
    launchdaemon = "launchdaemon"


class SecretGenerate(StrEnum):
    hex64 = "hex64"   # mint 64 hex chars on install if absent
    none = "none"     # operator must supply (e.g. cloud keys); never copied from Bert


class ServiceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    kind: ServiceKind = ServiceKind.launchagent
    keepalive: bool = False
    health_probe: str | None = None   # dotted path into the probe registry


class SecretSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str = "sanctum"
    service: str
    required: bool = True
    generate: SecretGenerate = SecretGenerate.none


class PagerCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    probe: str
    severity: str  # p0 | p1 | p2


class AlertSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sink: str = "chitti"
    pager_conditions: list[PagerCondition] = []


class UninstallSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bootout_labels: list[str] = []
    revoke_secrets: list[str] = []     # service names (must be declared in secrets)
    remove_paths: list[str] = []       # only removed under --purge
    rename_suffix: str = ".uninstalled-{date}"


class SoakSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_days: int = 7
    result_path: str = "~/.sanctum/soak/{module}.json"


class ModuleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    module: str
    version: str
    description: str
    depends_on: list[str] = []
    services: list[ServiceSpec] = []
    secrets: list[SecretSpec] = []
    probes: list[str] = []
    alerts: AlertSpec = AlertSpec()
    uninstall: UninstallSpec = UninstallSpec()
    docs: str
    demo: str
    soak: SoakSpec = SoakSpec()

    @model_validator(mode="after")
    def _check_revoke_secrets_declared(self) -> ModuleManifest:
        declared = {s.service for s in self.secrets}
        for svc in self.uninstall.revoke_secrets:
            if svc not in declared:
                raise ManifestError(
                    f"uninstall.revoke_secrets references undeclared secret '{svc}' "
                    f"(declared: {sorted(declared)})"
                )
        return self

    @classmethod
    def model_validate(  # type: ignore[override]
        cls,
        obj: Any,
        *,
        strict: bool | None = None,
        from_attributes: bool | None = None,
        context: Any = None,
    ) -> ModuleManifest:
        """Validate and unwrap ManifestError from pydantic's ValidationError wrapper."""
        try:
            return super().model_validate(
                obj,
                strict=strict,
                from_attributes=from_attributes,
                context=context,
            )
        except ValidationError as exc:
            for err in exc.errors():
                if err.get("type") == "value_error":
                    # Reconstruct the ManifestError from the message pydantic captured
                    msg = err.get("msg", str(exc))
                    # Strip pydantic's "Value error, " prefix if present
                    if msg.startswith("Value error, "):
                        msg = msg[len("Value error, "):]
                    raise ManifestError(msg) from exc
            raise


def load_manifest(path: Path) -> ModuleManifest:
    """Load + validate a manifest YAML file. Raises ManifestError on bad data."""
    from pathlib import Path as _Path

    try:
        data = yaml.safe_load(_Path(path).read_text())
    except OSError as e:
        raise ManifestError(f"cannot read manifest {path}: {e}") from e
    if not isinstance(data, dict):
        raise ManifestError(f"manifest {path} is not a mapping")
    return ModuleManifest.model_validate(data)
