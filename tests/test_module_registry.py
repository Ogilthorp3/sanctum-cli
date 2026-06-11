import pytest

from sanctum_cli.modules.manifest import ModuleManifest
from sanctum_cli.modules.registry import DependencyError, ModuleRegistry


def _m(name, deps=None):
    return ModuleManifest.model_validate({
        "module": name, "version": "1.0.0", "description": name,
        "depends_on": deps or [], "docs": "https://x.invalid", "demo": "true",
    })

def test_resolve_order_topological():
    reg = ModuleRegistry(manifests={"a": _m("a", ["b"]), "b": _m("b")})
    assert reg.resolve_order() == ["b", "a"]

def test_missing_dependency_raises():
    reg = ModuleRegistry(manifests={"a": _m("a", ["ghost"])})
    with pytest.raises(DependencyError, match="ghost"):
        reg.resolve_order()

def test_cycle_raises():
    reg = ModuleRegistry(manifests={"a": _m("a", ["b"]), "b": _m("b", ["a"])})
    with pytest.raises(DependencyError, match="cycle"):
        reg.resolve_order()

def test_user_overrides_builtin(tmp_path, monkeypatch):
    # a user manifest with the same `module` name wins over the built-in
    (tmp_path / "backup.module.yaml").write_text(
        "module: backup\nversion: 9.9.9\ndescription: user\ndocs: https://x.invalid\ndemo: 'true'\n")
    monkeypatch.setenv("SANCTUM_MODULES_DIR", str(tmp_path))
    reg = ModuleRegistry.discover()
    assert reg.get("backup").version == "9.9.9"
