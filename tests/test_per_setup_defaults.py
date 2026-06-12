"""Per-setup defaults resolve from instance.yaml, not a baked-in Ogilthorp3/nepveu.

Beta portability: a fresh operator's GitHub org, deadman repo, and bridge host
are their own — these must come from the SoT with the old literal only as a
last-resort fallback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli import bridge_client, config
from sanctum_cli.backends import github
from sanctum_cli.commands import deadman

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "instance.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_instance_value_reads_nested_and_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _write(tmp_path, "vcs:\n  github_owner: acme\nservices:\n  x:\n    port: 9\n")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(p))
    assert config.instance_value("vcs.github_owner", "fallback") == "acme"
    assert config.instance_value("services.x.port", 0) == 9
    assert config.instance_value("vcs.missing", "fb") == "fb"
    assert config.instance_value("nope.nope") is None


def test_instance_value_missing_file_returns_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "absent.yaml"))
    assert config.instance_value("vcs.github_owner", "fb") == "fb"


def test_default_owner_from_sot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(_write(tmp_path, "vcs:\n  github_owner: beta-org\n")))
    assert github.default_owner() == "beta-org"


def test_default_owner_fallback_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "absent.yaml"))
    assert github.default_owner() == github.DEFAULT_OWNER


def test_default_repo_from_sot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(_write(tmp_path, "vcs:\n  deadman_repo: beta-org/dm\n")))
    assert deadman.default_repo() == "beta-org/dm"


def test_bridge_url_from_sot_domain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SANCTUM_INSTANCE_FILE",
        str(_write(tmp_path, "secrets:\n  cloudflare_bridge_domain: bridge.acme.test\n")),
    )
    monkeypatch.delenv("SANCTUM_BRIDGE_URL", raising=False)
    assert bridge_client.base_url_from_env() == "https://bridge.acme.test"


def test_bridge_url_env_overrides_sot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SANCTUM_INSTANCE_FILE",
        str(_write(tmp_path, "secrets:\n  cloudflare_bridge_domain: bridge.acme.test\n")),
    )
    monkeypatch.setenv("SANCTUM_BRIDGE_URL", "https://override.test/")
    assert bridge_client.base_url_from_env() == "https://override.test"
