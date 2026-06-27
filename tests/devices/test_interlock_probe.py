"""``interlock.tailscale_oob_live`` — the LAN-independent OOB probe (FIX-3).

The prevent-interlock's "out-of-band channel proven-live" precondition is derived
from a real root-SSH round-trip to the Tailscale-on-box node (``ts-firewalla``,
:data:`sanctum_cli.devices.interlock.TS_FIREWALLA_ADDR`) — the only recovery
channel proven to survive a LAN collapse (06-26). These tests author their
expectations from ssh's REAL exit-code contract (0 live; non-zero / timeout /
spawn-fail = not-live), inject the transport so the gate is exercised OFFLINE, and
prove the boundary owns its argv encoding (a hostile address can never inject an
argument). The fail-OPEN footgun — returning live on a failure — is explicitly
guarded: any non-zero outcome reads as not-live.
"""

from __future__ import annotations

import pytest

from sanctum_cli.devices import interlock


class _RecordingProbe:
    """Records every address probed; returns a scripted exit code (the ssh contract)."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.addrs: list[str] = []

    def __call__(self, addr: str) -> int:
        self.addrs.append(addr)
        return self.code


# ── exit-code mapping: 0 == live, everything else == not-live (fail-closed) ───


def test_oob_live_true_only_on_exit_zero() -> None:
    """A root-SSH round-trip returning exit 0 means the channel is genuinely live."""
    probe = _RecordingProbe(0)
    assert interlock.tailscale_oob_live(probe=probe) is True
    # It probed the Tailscale-on-box node, not a LAN host.
    assert probe.addrs == [interlock.TS_FIREWALLA_ADDR]


@pytest.mark.parametrize("code", [255, 124, 1, 127])
def test_oob_live_false_on_any_nonzero_exit(code: int) -> None:
    """ssh-fail (255), timeout (124), and any other non-zero exit read as NOT-live.

    A non-zero outcome is the ABSENCE of proof the recovery channel is usable —
    fail-closed, never the fail-open that re-creates the 06-26 blindness.
    """
    assert interlock.tailscale_oob_live(probe=_RecordingProbe(code)) is False


def test_oob_live_probes_a_caller_supplied_addr() -> None:
    """The probed address is overridable (the real CLI uses the default constant)."""
    probe = _RecordingProbe(0)
    interlock.tailscale_oob_live(addr="100.68.0.9", probe=probe)
    assert probe.addrs == ["100.68.0.9"]


# ── the SSH argv envelope: key-only, root-over-tailnet, hostile-safe ──────────


def test_ts_ssh_argv_is_key_only_root_over_tailnet() -> None:
    """The probe argv is the key-only BatchMode envelope to ``root@<tailnet-addr>``."""
    argv = interlock._ts_ssh_argv(interlock.TS_FIREWALLA_ADDR, "true")
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "PreferredAuthentications=publickey" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    assert f"root@{interlock.TS_FIREWALLA_ADDR}" in argv
    assert argv[-1] == "true"  # the round-trip command


def test_ts_ssh_argv_hostile_addr_stays_one_argv_element() -> None:
    """A hostile address (space, ``%``, non-ASCII, shell metachars) is ONE argv token.

    Built as a list (never a shell string), so a value carrying ``; rm -rf /`` or a
    space can never split into a separate argument — ssh sees a single (bogus) host
    token and the injected command never executes (CLAUDE.md: own the boundary,
    test the hostile input).
    """
    hostile = "100.68.36.16 ; rm -rf / # café %20"
    argv = interlock._ts_ssh_argv(hostile, "true")
    assert f"root@{hostile}" in argv  # the whole hostile value is exactly one element
    assert argv.count(f"root@{hostile}") == 1


# ── fail-closed transport: a spawn failure is a NON-zero sentinel, never 0 ────


def test_run_probe_returns_nonzero_when_the_binary_cannot_spawn() -> None:
    """A probe whose subprocess cannot even spawn returns non-zero (never 0).

    No network: a nonexistent binary raises OSError, which ``_run_probe`` must
    collapse into a non-zero sentinel so ``tailscale_oob_live`` reads it as not-live
    rather than swallowing the failure into a false "live".
    """
    code = interlock._run_probe(["/nonexistent/sanctum-no-such-binary", "true"])
    assert code != 0


def test_oob_live_false_when_real_probe_cannot_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end fail-closed: the DEFAULT (real) probe path returns not-live when the
    ssh transport cannot run — proven by pointing the argv at a nonexistent binary."""
    monkeypatch.setattr(
        interlock, "_ts_ssh_argv", lambda addr, remote: ["/nonexistent/no-ssh", remote]
    )
    assert interlock.tailscale_oob_live() is False
