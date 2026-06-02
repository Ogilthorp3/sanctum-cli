import pytest
from sanctum_cli.modules.manifest import ModuleManifest, ManifestError, load_manifest

VALID = {
    "module": "backup",
    "version": "1.0.0",
    "description": "Encrypted restic backup to the operator's own cloud repo",
    "depends_on": [],
    "services": [],
    "secrets": [
        {"account": "sanctum", "service": "r2-access-key-id", "required": True, "generate": "none"}
    ],
    "probes": ["backup.backup_recent"],
    "alerts": {"sink": "chitti", "pager_conditions": []},
    "uninstall": {"bootout_labels": [], "revoke_secrets": ["r2-access-key-id"],
                  "remove_paths": [], "rename_suffix": ".uninstalled-{date}"},
    "docs": "https://example.invalid/backup",
    "demo": "sanctum backup snapshots",
    "soak": {"min_days": 7, "result_path": "~/.sanctum/soak/backup.json"},
}

def test_valid_manifest_parses():
    m = ModuleManifest.model_validate(VALID)
    assert m.module == "backup"
    assert m.secrets[0].service == "r2-access-key-id"
    assert m.soak.min_days == 7

def test_revoke_secret_must_be_declared():
    bad = {**VALID, "uninstall": {**VALID["uninstall"], "revoke_secrets": ["undeclared-key"]}}
    with pytest.raises(ManifestError, match="revoke_secrets references undeclared secret"):
        ModuleManifest.model_validate(bad)

def test_generate_enum_rejects_unknown():
    bad = {**VALID, "secrets": [{"account": "sanctum", "service": "x", "required": True, "generate": "rsa"}]}
    with pytest.raises(Exception):
        ModuleManifest.model_validate(bad)
