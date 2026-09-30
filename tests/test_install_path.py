"""World-writable install path is refused."""

from __future__ import annotations

import stat
from pathlib import Path

from sanctum_cli.install_path import world_writable


class _Mode:
    def __init__(self, mode: int) -> None:
        self.st_mode = mode


def test_private_tree_is_accepted(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(Path, "stat", lambda self, follow_symlinks=True: _Mode(0o755))
    assert world_writable(tmp_path / "bin" / "sanctum") is None


def test_world_writable_directory_is_refused(monkeypatch, tmp_path: Path):
    exe = tmp_path / "bin" / "sanctum"
    real_stat = Path.stat

    def fake_stat(self: Path, follow_symlinks: bool = True):
        if self == tmp_path:
            return _Mode(stat.S_IFDIR | 0o777)
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", fake_stat)
    assert world_writable(exe) == tmp_path


def test_world_writable_binary_is_refused(monkeypatch, tmp_path: Path):
    exe = tmp_path / "sanctum"

    def fake_stat(self: Path, follow_symlinks: bool = True):
        del follow_symlinks
        if self == exe:
            return _Mode(stat.S_IFREG | 0o666)
        return _Mode(0o755)

    monkeypatch.setattr(Path, "stat", fake_stat)
    assert world_writable(exe) == exe
