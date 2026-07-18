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
    """The probed address is overridable (the real CLI uses the resolved default)."""
    probe = _RecordingProbe(0)
    interlock.tailscale_oob_live(addr="100.68.0.9", probe=probe)
    assert probe.addrs == ["100.68.0.9"]


# ── FIX-d1: the tailnet recovery address is config-first, not hardcoded ────────


def test_firewalla_ts_addr_reads_instance_yaml(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``devices.firewalla.ts_addr`` in instance.yaml is the resolved tailnet node.

    A haus pins its OWN Tailscale-on-box node — the recovery channel is per-haus, so
    the address must come from config, never a baked-in constant.
    """
    cfg = tmp_path / "instance.yaml"  # type: ignore[operator]
    cfg.write_text(
        "devices:\n  firewalla:\n    ts_addr: 100.99.1.2\n", encoding="utf-8"
    )
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(cfg))
    assert interlock.firewalla_ts_addr() == "100.99.1.2"


def test_firewalla_ts_addr_falls_back_to_shipped_default(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config → the shipped :data:`TS_FIREWALLA_ADDR` default (tool unchanged)."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "absent.yaml"))  # type: ignore[operator]
    assert interlock.firewalla_ts_addr() == interlock.TS_FIREWALLA_ADDR


def test_oob_live_probes_the_config_resolved_addr(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no explicit ``addr``, the probe targets the CONFIG-resolved tailnet node.

    Proves the OOB probe threads the config-first address (``devices.firewalla.ts_addr``)
    through to the real round-trip target, not the module constant.
    """
    cfg = tmp_path / "instance.yaml"  # type: ignore[operator]
    cfg.write_text(
        "devices:\n  firewalla:\n    ts_addr: 100.99.7.7\n", encoding="utf-8"
    )
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(cfg))
    probe = _RecordingProbe(0)
    assert interlock.tailscale_oob_live(probe=probe) is True
    assert probe.addrs == ["100.99.7.7"]


# ── the SSH argv envelope: key-only, root-over-tailnet, hostile-safe ──────────


def test_ts_ssh_argv_is_key_only_pi_over_tailnet() -> None:
    """The probe argv is the key-only BatchMode envelope to ``pi@<tailnet-addr>``.

    The Firewalla's SSH user is ``pi`` (root login is denied) — LIVE-VERIFIED 2026-06-27
    against the real box (``ssh pi@…`` succeeds, ``ssh root@…`` is "Permission denied"),
    and it matches the runner's transport. The expectation is derived from the box's REAL
    behavior, not the producer's old ``root@`` assumption (which the prod code + this test
    once shared — CLAUDE.md "don't share assumptions between test and production").
    """
    argv = interlock._ts_ssh_argv(interlock.TS_FIREWALLA_ADDR, "true")
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "PreferredAuthentications=publickey" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    assert f"pi@{interlock.TS_FIREWALLA_ADDR}" in argv
    assert f"root@{interlock.TS_FIREWALLA_ADDR}" not in argv  # root is denied on the box
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
    assert f"pi@{hostile}" in argv  # the whole hostile value is exactly one element
    assert argv.count(f"pi@{hostile}") == 1


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
