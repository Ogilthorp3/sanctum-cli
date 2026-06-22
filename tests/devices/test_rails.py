"""Layer-2 safety rails — :func:`guarded_apply` snapshot→confirm→verify→rollback.

``guarded_apply`` is the one seam every mutating intent goes through: it takes a
snapshot, asks for confirmation (unless ``force``), runs the change, verifies the
result, and — on a failed verify — automatically rolls the device back to the
snapshot. Each branch is a hard contract, so each gets its own test driven by the
:class:`FakeProvider` (no real gear, no network).

The audit log is written through an explicit ``log_path`` so these tests target a
``tmp_path`` file and NEVER touch the real ``~/.sanctum/logs/``. The default path
is exercised only structurally (we assert the default resolves under
``~/.sanctum/logs/`` without writing to it).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sanctum_cli.devices.base import Capability, OpResult, Snapshot
from sanctum_cli.devices.rails import guarded_apply

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class FakeProvider:
    """Minimal in-memory provider mirroring the plan's conformance fake."""

    kind = "hub"
    brand = "fake-hub"

    def __init__(self) -> None:
        self._v: dict[str, str] = {"WanMode": "gpon"}
        self.rollback_calls = 0

    @staticmethod
    def detect(net: object) -> float:
        return 1.0

    def connect(self, creds: object | None) -> None:
        return None

    def get(self, path: str) -> str:
        return self._v[path]

    def set(self, path: str, value: str) -> OpResult:
        before = self._v.get(path)
        self._v[path] = value
        return OpResult(ok=True, detail="set", before=before, after=value)

    def capabilities(self) -> set[Capability]:
        return {Capability.READ, Capability.SET}

    def snapshot(self, scope: str | None = None) -> Snapshot:
        return Snapshot(brand=self.brand, taken_at="t", data=dict(self._v))

    def rollback(self, snap: Snapshot) -> OpResult:
        self.rollback_calls += 1
        self._v = dict(snap.data)
        return OpResult(ok=True, detail="rolled back")


def _set_wan(value: str) -> Callable[[FakeProvider], None]:
    def change(pv: FakeProvider) -> None:
        pv.set("WanMode", value)

    return change


def test_guarded_apply_commits_on_verify_pass(tmp_path: Path) -> None:
    """verify_fn True → change is kept and ok=True; no rollback."""
    p = FakeProvider()
    res = guarded_apply(
        p,
        _set_wan("xgspon"),
        verify_fn=lambda: True,
        confirm=lambda _plan: True,
        force=True,
        rollback=True,
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.ok is True
    assert p.get("WanMode") == "xgspon"
    assert p.rollback_calls == 0


def test_guarded_apply_rolls_back_on_verify_fail(tmp_path: Path) -> None:
    """verify_fn False → auto-rollback to the snapshot and ok=False."""
    p = FakeProvider()
    res = guarded_apply(
        p,
        _set_wan("xgspon"),
        verify_fn=lambda: False,
        confirm=lambda _plan: True,
        force=True,
        rollback=True,
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.ok is False
    assert p.get("WanMode") == "gpon"  # rolled back
    assert p.rollback_calls == 1


def test_guarded_apply_no_rollback_leaves_state(tmp_path: Path) -> None:
    """rollback=False → a failed verify leaves the device mutated, ok=False."""
    p = FakeProvider()
    res = guarded_apply(
        p,
        _set_wan("xgspon"),
        verify_fn=lambda: False,
        confirm=lambda _plan: True,
        force=True,
        rollback=False,
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.ok is False
    assert p.get("WanMode") == "xgspon"  # NOT rolled back
    assert p.rollback_calls == 0


def test_guarded_apply_force_skips_confirm(tmp_path: Path) -> None:
    """force=True must NOT call confirm — it proceeds straight to the change."""
    p = FakeProvider()
    confirm_calls = 0

    def confirm(_plan: str) -> bool:
        nonlocal confirm_calls
        confirm_calls += 1
        return True

    res = guarded_apply(
        p,
        _set_wan("xgspon"),
        verify_fn=lambda: True,
        confirm=confirm,
        force=True,
        rollback=True,
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.ok is True
    assert confirm_calls == 0


def test_guarded_apply_confirm_declined_aborts(tmp_path: Path) -> None:
    """force=False + confirm→False → no change, no verify, ok=False."""
    p = FakeProvider()
    verify_calls = 0

    def verify() -> bool:
        nonlocal verify_calls
        verify_calls += 1
        return True

    res = guarded_apply(
        p,
        _set_wan("xgspon"),
        verify_fn=verify,
        confirm=lambda _plan: False,
        force=False,
        rollback=True,
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.ok is False
    assert p.get("WanMode") == "gpon"  # never mutated
    assert p.rollback_calls == 0
    assert verify_calls == 0  # verify never reached when confirm declines


def test_guarded_apply_confirm_accepted_applies(tmp_path: Path) -> None:
    """force=False + confirm→True → the change runs and verify gates it."""
    p = FakeProvider()
    res = guarded_apply(
        p,
        _set_wan("xgspon"),
        verify_fn=lambda: True,
        confirm=lambda _plan: True,
        force=False,
        rollback=True,
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.ok is True
    assert p.get("WanMode") == "xgspon"


def test_guarded_apply_writes_audit_line(tmp_path: Path) -> None:
    """One JSON audit line is appended with the outcome + before/after."""
    log = tmp_path / "audit.jsonl"
    p = FakeProvider()
    guarded_apply(
        p,
        _set_wan("xgspon"),
        verify_fn=lambda: False,
        confirm=lambda _plan: True,
        force=True,
        rollback=True,
        log_path=log,
    )
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["brand"] == "fake-hub"
    assert rec["ok"] is False
    assert rec["rolled_back"] is True
    assert rec["ts"]  # ISO-8601 stamp present


def test_guarded_apply_audit_appends(tmp_path: Path) -> None:
    """Two applies append two lines (append-only, not truncating)."""
    log = tmp_path / "audit.jsonl"
    p = FakeProvider()
    for _ in range(2):
        guarded_apply(
            p,
            _set_wan("xgspon"),
            verify_fn=lambda: True,
            confirm=lambda _plan: True,
            force=True,
            rollback=True,
            log_path=log,
        )
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_guarded_apply_change_raises_rolls_back(tmp_path: Path) -> None:
    """If the change itself raises mid-flight, rollback fires and ok=False."""
    p = FakeProvider()

    def boom(pv: FakeProvider) -> None:
        pv.set("WanMode", "xgspon")
        msg = "transport died mid-change"
        raise RuntimeError(msg)

    res = guarded_apply(
        p,
        boom,
        verify_fn=lambda: True,
        confirm=lambda _plan: True,
        force=True,
        rollback=True,
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.ok is False
    assert p.get("WanMode") == "gpon"  # rolled back after the raise
    assert p.rollback_calls == 1


def test_guarded_apply_default_log_path_under_sanctum() -> None:
    """The default audit path resolves under ~/.sanctum/logs/ (not written here)."""
    from pathlib import Path

    from sanctum_cli.devices import rails

    expected = Path.home() / ".sanctum/logs/netgear-audit.jsonl"
    assert expected == rails.DEFAULT_AUDIT_LOG
