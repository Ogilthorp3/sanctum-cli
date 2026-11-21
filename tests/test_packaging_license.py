"""Contract tests for canonical public license and README metadata."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_and_license_canonical_fsl_apache() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    assert project["license"] == {"text": "FSL-1.1-Apache-2.0"}
    assert "LICENSE" in project["license-files"]

    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith(
        "Functional Source License, Version 1.1, Apache 2.0 Future License"
    )
    assert "FSL-1.1-Apache-2.0" in license_text
    assert "Copyright 2026 Bertrand Nepveu" in license_text
    assert "Apache License, Version 2.0" in license_text
    assert "FSL-1.1-MIT" not in license_text


def test_readme_matches_version_and_license() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"v{version}" in readme
    assert "FSL-1.1-Apache-2.0" in readme
    assert "Apache License, Version 2.0" in readme
    assert "FSL-1.1-MIT" not in readme
    assert "Sigstore release signing" in readme
    release_wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "sigstore/cosign-installer@" in release_wf
    assert "actions/attest-build-provenance@" in release_wf
    assert "id-token: write" in release_wf
