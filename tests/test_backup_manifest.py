from sanctum_cli.modules.registry import ModuleRegistry


def test_backup_manifest_is_builtin_and_valid():
    reg = ModuleRegistry.discover()
    assert "backup" in reg.names()
    m = reg.get("backup")
    # every revoke target is a declared secret (contract enforced by the model,
    # but assert here against the on-disk file as a different-author check)
    declared = {s.service for s in m.secrets}
    assert set(m.uninstall.revoke_secrets) <= declared
    # cloud keys are operator-supplied, never auto-generated/copied
    assert all(s.generate.value == "none" for s in m.secrets)
    # docs + demo are present (the docs+demo gate needs them)
    assert m.docs and m.demo
