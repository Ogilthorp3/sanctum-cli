"""Per-setup defaults resolve from instance.yaml or the authenticated gh user.

Beta portability: a fresh operator's GitHub org, deadman repo, and bridge host
are their own — these MUST come from the SoT (instance.yaml) or the
authenticated ``gh`` account. There is no baked-in personal account anymore;
when nothing resolves, the CLI raises a clear ``UserError`` rather than
silently pointing at someone else's infrastructure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from sanctum_cli import bridge_client, config
from sanctum_cli.backends import github
from sanctum_cli.commands import deadman
from sanctum_cli.errors import UserError

if TYPE_CHECKING:
    from pathlib import Path


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "instance.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# ─── instance_value primitive ───────────────────────────────────────


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


# ─── github default_owner ────────────────────────────────────────────


def test_default_owner_from_sot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """instance.yaml override wins, even when gh could resolve a different login."""
    monkeypatch.setenv(
        "SANCTUM_INSTANCE_FILE",
        str(_write(tmp_path, "vcs:\n  github_owner: beta-org\n")),
    )
    with patch("sanctum_cli.backends.github._gh_login", return_value="someone-else"):
        assert github.default_owner() == "beta-org"


def test_default_owner_derives_from_gh_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config → resolve the authenticated GitHub user via `gh api user`."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "absent.yaml"))
    with patch("sanctum_cli.backends.github._gh_login", return_value="ghuser"):
        assert github.default_owner() == "ghuser"


def test_default_owner_raises_when_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config + gh unavailable → clear UserError, no personal fallback."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "absent.yaml"))
    with (
        patch("sanctum_cli.backends.github._gh_login", return_value=None),
        pytest.raises(UserError) as exc,
    ):
        github.default_owner()
    assert "vcs.github_owner" in (exc.value.fix or "")


def test_gh_login_parses_login_via_gh_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_gh_login` shells `gh api user --jq .login` and returns the trimmed login."""
    import subprocess

    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ghuser\n", stderr="")
    with (
        patch("sanctum_cli.backends.github.shutil.which", return_value="/usr/local/bin/gh"),
        patch("sanctum_cli.backends.github.subprocess.run", return_value=completed) as run,
    ):
        assert github._gh_login() == "ghuser"
    called = run.call_args[0][0]
    assert called[:3] == ["gh", "api", "user"]
    assert "--jq" in called and ".login" in called


def test_gh_login_none_when_gh_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("sanctum_cli.backends.github.shutil.which", return_value=None):
        assert github._gh_login() is None


# ─── deadman default_repo ────────────────────────────────────────────


def test_default_repo_from_sot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SANCTUM_INSTANCE_FILE",
        str(_write(tmp_path, "vcs:\n  deadman_repo: beta-org/dm\n")),
    )
    with patch("sanctum_cli.commands.deadman._gh_login", return_value="someone-else"):
        assert deadman.default_repo() == "beta-org/dm"


def test_default_repo_derives_from_gh_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "absent.yaml"))
    with patch("sanctum_cli.commands.deadman._gh_login", return_value="ghuser"):
        assert deadman.default_repo() == "ghuser/sanctum-backup-deadman"


def test_default_repo_raises_when_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "absent.yaml"))
    with (
        patch("sanctum_cli.commands.deadman._gh_login", return_value=None),
        pytest.raises(UserError) as exc,
    ):
        deadman.default_repo()
    assert "vcs.deadman_repo" in (exc.value.fix or "")


# ─── bridge base_url ─────────────────────────────────────────────────


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


def test_bridge_url_raises_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env and no instance.yaml domain → UserError, no personal fallback."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "absent.yaml"))
    monkeypatch.delenv("SANCTUM_BRIDGE_URL", raising=False)
    with pytest.raises(UserError) as exc:
        bridge_client.base_url_from_env()
    assert "cloudflare_bridge_domain" in (exc.value.fix or "")
