"""Tests for ``sanctum deadman`` — off-box backup dead-man's-switch heartbeat."""

from __future__ import annotations

import base64
import json
import os
from typing import TYPE_CHECKING

from sanctum_cli.commands import deadman

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_merge_heartbeat_sets_key() -> None:
    out = deadman.merge_heartbeat({}, "manoir:backup-fresh", ts=100, max_hours=26)
    assert out == {"manoir:backup-fresh": {"ts": 100, "max_hours": 26}}


def test_merge_heartbeat_is_pure_and_preserves_others() -> None:
    base: dict[str, object] = {"mbp:restore-drill": {"ts": 1, "max_hours": 192}}
    out = deadman.merge_heartbeat(base, "manoir:backup-fresh", ts=200, max_hours=26)
    assert out["mbp:restore-drill"] == {"ts": 1, "max_hours": 192}
    assert out["manoir:backup-fresh"] == {"ts": 200, "max_hours": 26}
    assert base == {"mbp:restore-drill": {"ts": 1, "max_hours": 192}}  # input untouched


def _write_gh_stub(bindir: Path, capture: Path) -> None:
    """Fake ``gh`` on PATH: GET returns sha + base64('{}'); PUT records content= and succeeds."""
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "gh"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, base64\n"
        "args = sys.argv[1:]\n"
        "if '-X' in args and 'PUT' in args:\n"
        "    content = next((a.split('=', 1)[1] for a in args if a.startswith('content=')), '')\n"
        f"    open({str(capture)!r}, 'w').write(content)\n"
        "    print('{\"commit\": {\"sha\": \"new\"}}')\n"
        "else:\n"
        "    print('{\"sha\": \"abc\", \"content\": \"' + base64.b64encode(b'{}').decode() + '\"}')\n"
    )
    stub.chmod(0o755)


def test_beat_writes_heartbeat_via_gh_contents_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub `gh` (the external/network boundary): assert beat() reads, merges,
    # and PUTs a heartbeats.json that contains our key.
    capture = tmp_path / "put_content.txt"
    _write_gh_stub(tmp_path / "bin", capture)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    key = deadman.beat("backup-fresh", repo="owner/repo")
    assert key.endswith(":backup-fresh")

    data = json.loads(base64.b64decode(capture.read_text()).decode("utf-8"))
    assert key in data
    assert data[key]["max_hours"] == 26
    assert isinstance(data[key]["ts"], int)
