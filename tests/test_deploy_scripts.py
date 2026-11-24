"""Static and argv-safety regression tests for deploy scripts."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPO_ROOT / "deploy" / "ahsoka-agent-deploy.sh"


def test_ahsoka_deploy_script_bash_syntax_ok() -> None:
    proc = subprocess.run(
        ["bash", "-n", str(DEPLOY_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_ahsoka_deploy_script_has_no_personal_literals_or_wildcard_bind() -> None:
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    lowered = text.lower()
    for forbidden in ("cha" + "let", "man" + "oir", "/users/" + "be" + "rt", "0.0.0.0"):
        assert forbidden not in lowered, f"found forbidden literal {forbidden!r}"
    assert "127.0.0.1" in text
    assert "/bin/sh -s --" in text


@pytest.mark.parametrize(
    ("host", "name"),
    [
        ("-oProxyCommand=id", "ahsoka"),
        ("host;id", "ahsoka"),
        ("user@-evil", "ahsoka"),
        ("valid-host", 'bad"name'),
        ("valid-host", "a;id"),
        ("valid-host", "-leading-dash"),
    ],
)
def test_ahsoka_deploy_script_rejects_hostile_args_before_ssh(
    tmp_path: Path, host: str, name: str
) -> None:
    marker = tmp_path / "ssh-invoked"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for tool in ("ssh", "scp"):
        script = fake_bin / tool
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 99\n", encoding="utf-8")
        script.chmod(stat.S_IRWXU)

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    proc = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), host, name],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode != 0
    assert not marker.exists(), "ssh/scp must not be invoked for invalid args"


def test_ahsoka_deploy_script_never_starts_or_targets_a_plain_listener() -> None:
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "1338" not in text
    assert "mlx_lm server" not in text  # stock server has no mTLS; the brain is sanctum-mlx
    assert "http://127.0.0.1:" not in text.replace("HA_URL=http://127.0.0.1:8123", "")
    for flag in ("--no-plain", "--tls-port", "--tls-cert", "--tls-key", "--tls-client-ca"):
        assert flag in text
    assert "https://127.0.0.1:" in text
    assert "--cert" in text  # the health check presents the client certificate


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_ahsoka_deploy_script_passes_shellcheck() -> None:
    proc = subprocess.run(
        ["shellcheck", "-S", "warning", str(DEPLOY_SCRIPT)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout


def _run_deploy(tmp_path: Path, env_extra: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], Path]:
    marker = tmp_path / "ssh-invoked"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for tool in ("ssh", "scp"):
        script = fake_bin / tool
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 99\n", encoding="utf-8")
        script.chmod(stat.S_IRWXU)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SANCTUM_BRAIN_")}
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env.update(env_extra)
    proc = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "valid-host", "ahsoka"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return proc, marker


def test_ahsoka_deploy_script_refuses_without_client_tls_parameters(tmp_path: Path) -> None:
    proc, marker = _run_deploy(tmp_path, {})
    assert proc.returncode == 2
    assert "SANCTUM_BRAIN_CLIENT_CERT" in proc.stderr
    assert not marker.exists(), "nothing may reach the node without the client TLS parameters"


def test_ahsoka_deploy_script_refuses_without_the_provider_request_block(tmp_path: Path) -> None:
    env = {
        "SANCTUM_BRAIN_CLIENT_CERT": "/certs/client.crt",
        "SANCTUM_BRAIN_CLIENT_KEY": "/certs/client.key",
        "SANCTUM_BRAIN_CLIENT_CA": "/certs/ca.crt",
    }
    proc, marker = _run_deploy(tmp_path, env)
    assert proc.returncode == 2
    assert "SANCTUM_BRAIN_PROVIDER_REQUEST" in proc.stderr
    assert not marker.exists()
    bad = tmp_path / "request.json"
    bad.write_text('{"no": "tls"}', encoding="utf-8")
    again = tmp_path / "again"
    again.mkdir()
    proc, marker = _run_deploy(again, {**env, "SANCTUM_BRAIN_PROVIDER_REQUEST": str(bad)})
    assert proc.returncode == 2
    assert not marker.exists()
