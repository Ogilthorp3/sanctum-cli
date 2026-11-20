"""Static and argv-safety regression tests for deploy scripts."""

from __future__ import annotations

import os
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
    for forbidden in ("chalet", "manoir", "/users/bert", "0.0.0.0"):
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
