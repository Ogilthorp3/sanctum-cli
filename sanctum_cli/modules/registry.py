from __future__ import annotations

import os
from pathlib import Path

from sanctum_cli.modules.manifest import ManifestError, ModuleManifest, load_manifest

BUILTIN_DIR = Path(__file__).parent / "builtins"


class DependencyError(ManifestError):
    """Missing dependency or dependency cycle in the module graph."""


def _user_dir() -> Path:
    return Path(os.environ.get("SANCTUM_MODULES_DIR", Path("~/.sanctum/modules").expanduser()))


class ModuleRegistry:
    def __init__(self, manifests: dict[str, ModuleManifest]):
        self.manifests = manifests

    @classmethod
    def discover(cls) -> ModuleRegistry:
        found: dict[str, ModuleManifest] = {}
        for d in (BUILTIN_DIR, _user_dir()):  # user dir second => overrides
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.module.yaml")):
                m = load_manifest(f)
                found[m.module] = m
        return cls(found)

    def get(self, name: str) -> ModuleManifest:
        if name not in self.manifests:
            raise ManifestError(f"unknown module '{name}' (installed: {sorted(self.manifests)})")
        return self.manifests[name]

    def names(self) -> list[str]:
        return sorted(self.manifests)

    def resolve_order(self) -> list[str]:
        order: list[str] = []
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(name: str) -> None:
            if name in done:
                return
            if name in visiting:
                raise DependencyError(f"dependency cycle through '{name}'")
            if name not in self.manifests:
                raise DependencyError(f"missing dependency '{name}'")
            visiting.add(name)
            for dep in self.manifests[name].depends_on:
                visit(dep)
            visiting.discard(name)
            done.add(name)
            order.append(name)

        for n in sorted(self.manifests):
            visit(n)
        return order
