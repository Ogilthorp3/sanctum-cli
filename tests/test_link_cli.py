"""CLI tests for ``sanctum link`` — status (read-only) + install (file boundary)."""

from __future__ import annotations

import os
import plistlib
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands.link import link_app
from sanctum_cli.net import link
from sanctum_cli.net import link as linkmod

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()

# The reference incident's two MACs (randomized live address vs hardware MAC).
RANDOMIZED_MAC = "de:48:45:83:ae:0a"
HARDWARE_MAC = "84:2f:57:02:be:ee"


def _randomized_probe() -> link.WifiProbe:
    return link.WifiProbe(
        iface="en0",
        current_mac=RANDOMIZED_MAC,
        hardware_mac=HARDWARE_MAC,
        ssid="ClosetNet",
    )


def _stable_probe() -> link.WifiProbe:
    return link.WifiProbe(
        iface="en0",
        current_mac=HARDWARE_MAC,
        hardware_mac=HARDWARE_MAC,
        ssid="ClosetNet",
    )


# A LOAD-bound sentinel window (latency tracks load, zero loss).
LOAD_LOG = """\
2026-06-29T21:07:03 ssid=x rtt=2.479/34.863/106.761/36.142 loss=0.0% load=[3.19 3.28 3.17] DEGRADED
2026-06-29T21:10:09 ssid=x rtt=2.531/5.408/13.028/2.637 loss=0.0% load=[2.84 3.19 3.15] ok
2026-06-29T21:13:14 ssid=x rtt=3.425/35.831/164.101/53.661 loss=0.0% load=[3.58 3.86 3.51] DEGRADED
2026-06-29T21:16:19 ssid=x rtt=3.217/42.700/122.192/45.775 loss=0.0% load=[4.49 4.10 3.66] DEGRADED
2026-06-29T21:25:35 ssid=x rtt=4.748/107.547/520.332/138.349 loss=0.0% load=[6.48 4.39 3.89] DEGRADED
"""


def _quarantined_probe() -> link.IdentityProbe:
    """A canned IdentityProbe so the status IDENTITY block fires NO live call.

    ``link status`` now probes the live Wi-Fi identity beside the link-health
    verdict; without this stub the three status tests below would shell out to
    real ``networksetup``/``ifconfig``/``ipconfig``/``route`` and ping the live
    gateway on macOS. Mirrors the hermetic pattern at
    ``test_status_shows_identity_verdict``.
    """
    return link.IdentityProbe(
        iface="en1",
        ssid="Nepveu-6G",
        current_mac="32:a6:f4:de:54:cf",
        hardware_mac="d0:11:e5:1c:88:59",
        security="WPA2_PSK",
        associated=True,
        router_arp_verified=False,
        gateway_reachable=False,
    )


def test_status_load_fixture_prints_load_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "wifi-stability.log"
    log.write_text(LOAD_LOG, encoding="utf-8")
    monkeypatch.setattr("sanctum_cli.net.link.probe_identity", _quarantined_probe)
    result = runner.invoke(app, ["link", "status", "--log", str(log)])
    assert result.exit_code == 0, result.stdout
    assert "VERDICT: LOAD" in result.stdout
    # The honest-headroom remedy must surface the WIRED-uplink truth.
    assert "WIRED" in result.stdout


def test_status_missing_log_exits_zero_with_no_data_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist.log"
    monkeypatch.setattr("sanctum_cli.net.link.probe_identity", _quarantined_probe)
    result = runner.invoke(app, ["link", "status", "--log", str(missing)])
    assert result.exit_code == 0, result.stdout
    assert "NO_DATA" in result.stdout
    # The hint must point the operator at install.
    assert "install" in result.stdout.lower()


def test_status_default_log_path_used_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no --log, status reads the default sentinel path (and tolerates absence)."""
    monkeypatch.setattr(
        "sanctum_cli.net.link.default_log_path",
        lambda: tmp_path / "absent.log",
    )
    monkeypatch.setattr("sanctum_cli.net.link.probe_identity", _quarantined_probe)
    result = runner.invoke(app, ["link", "status"])
    assert result.exit_code == 0, result.stdout
    assert "NO_DATA" in result.stdout


def test_install_writes_sampler_and_plist_and_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install writes the sampler 0755 + a plist naming it, then loads it.

    Paths are redirected into a temp dir and launchctl is stubbed so nothing
    touches the real ~/Library/LaunchAgents. This exercises the real file-writing
    boundary (the artifact is read back), not a mock of it.
    """
    script = tmp_path / "bin" / "wifi-stability-sentinel.sh"
    plist = tmp_path / "LaunchAgents" / "com.sanctum.wifi-stability.plist"
    err_log = tmp_path / "logs" / "wifi-stability.err"
    sample_log = tmp_path / "logs" / "wifi-stability.log"
    monkeypatch.setattr("sanctum_cli.net.link.sentinel_script_path", lambda: script)
    monkeypatch.setattr("sanctum_cli.net.link.sentinel_plist_path", lambda: plist)
    monkeypatch.setattr("sanctum_cli.net.link.default_err_path", lambda: err_log)
    monkeypatch.setattr("sanctum_cli.net.link.default_log_path", lambda: sample_log)

    calls: list[list[str]] = []

    def fake_launchctl(args: list[str], *, check: bool) -> tuple[bool, str]:
        calls.append(args)
        return (True, "")

    monkeypatch.setattr("sanctum_cli.commands.link._launchctl", fake_launchctl)

    result = runner.invoke(app, ["link", "install"])
    assert result.exit_code == 0, result.stdout

    # Real artifacts on disk.
    assert script.exists()
    assert script.read_text(encoding="utf-8").startswith("#!/bin/bash")
    assert oct(script.stat().st_mode & 0o777) == "0o755"

    plist_text = plist.read_text(encoding="utf-8")
    # The plist must name the absolute sampler path (launchd does not expand ~).
    assert str(script) in plist_text
    assert "com.sanctum.wifi-stability" in plist_text
    assert str(err_log) in plist_text
    assert "<integer>180</integer>" in plist_text

    # A bootstrap was attempted into the per-user GUI domain.
    assert any(a[:1] == ["bootstrap"] for a in calls)
    assert any(f"gui/{os.getuid()}" in " ".join(a) for a in calls)


def test_install_reports_when_launchctl_fails_but_still_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed launchctl load is a note, not an abort — files are still installed."""
    monkeypatch.setattr(
        "sanctum_cli.net.link.sentinel_script_path", lambda: tmp_path / "bin" / "s.sh"
    )
    monkeypatch.setattr(
        "sanctum_cli.net.link.sentinel_plist_path", lambda: tmp_path / "la" / "x.plist"
    )
    monkeypatch.setattr(
        "sanctum_cli.net.link.default_err_path", lambda: tmp_path / "logs" / "x.err"
    )
    monkeypatch.setattr(
        "sanctum_cli.net.link.default_log_path", lambda: tmp_path / "logs" / "x.log"
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.link._launchctl",
        lambda args, *, check: (False, "Load failed: 5: Input/output error"),
    )
    result = runner.invoke(app, ["link", "install"])
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "bin" / "s.sh").exists()
    assert "not confirmed" in result.stdout.lower() or "manually" in result.stdout.lower()


# ─── optimize (P2 — Optimize client) ─────────────────────────────────
#
# NOTE (Task 6): ``optimize`` now leads with the identity verdict + node class and
# gates --apply on the node being a fixed-infra SERVER. The four P2 audit tests below
# still assert the LEGACY MAC-stability audit output (RANDOMIZED / STABLE / the written
# profile) — that headline is preserved beneath the new identity block — but they now
# also patch the new module seams (``probe_identity`` / ``_node_signals``) so they stay
# hermetic (NO live radio/router calls) and land deterministically on the enroll path.


def _rotating_id_probe() -> link.IdentityProbe:
    """A SERVER-shaped, at-risk (ROTATING) identity probe for the enroll path."""
    return link.IdentityProbe(
        iface="en0",
        ssid="ClosetNet",
        current_mac=RANDOMIZED_MAC,
        hardware_mac=HARDWARE_MAC,
        security="WPA3_SAE",
        associated=True,
        router_arp_verified=True,
        gateway_reachable=True,
    )


def _server_signals() -> link.NodeSignals:
    return link.NodeSignals(30.0, "Manual", True, 1, False)


def test_optimize_audit_reports_randomized_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default optimize is a read-only audit: probe → analyze → verdict, exit 0."""
    monkeypatch.setattr("sanctum_cli.net.link.probe_wifi", _randomized_probe)
    monkeypatch.setattr("sanctum_cli.net.link.probe_identity", _rotating_id_probe)
    monkeypatch.setattr("sanctum_cli.commands.link._node_signals", _server_signals)
    result = runner.invoke(app, ["link", "optimize"])
    assert result.exit_code == 0, result.stdout
    assert "MAC stability" in result.stdout
    assert "RANDOMIZED" in result.stdout
    # The verdict must surface BOTH MACs (honest-verify: derived from a real read).
    assert HARDWARE_MAC in result.stdout
    assert RANDOMIZED_MAC in result.stdout
    # No profile written without --apply.
    assert "wrote stability profile" not in result.stdout


def test_optimize_audit_reports_stable_for_hardware_mac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sanctum_cli.net.link.probe_wifi", _stable_probe)
    # A stable identity probe so neither the identity block nor the audit says RANDOMIZED.
    stable_id = link.IdentityProbe(
        iface="en0",
        ssid="ClosetNet",
        current_mac=HARDWARE_MAC,
        hardware_mac=HARDWARE_MAC,
        security="WPA3_SAE",
        associated=True,
        router_arp_verified=True,
        gateway_reachable=True,
    )
    monkeypatch.setattr("sanctum_cli.net.link.probe_identity", lambda: stable_id)
    monkeypatch.setattr("sanctum_cli.commands.link._node_signals", _server_signals)
    result = runner.invoke(app, ["link", "optimize"])
    assert result.exit_code == 0, result.stdout
    assert "STABLE" in result.stdout
    assert "RANDOMIZED" not in result.stdout


def test_optimize_apply_writes_valid_mobileconfig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--apply --profile-out writes a real .mobileconfig that parses as a plist.

    Real artifact through the real boundary: the file is read back and fed to
    plistlib, not just checked for existence.
    """
    monkeypatch.setattr("sanctum_cli.net.link.probe_wifi", _randomized_probe)
    monkeypatch.setattr("sanctum_cli.net.link.probe_identity", _rotating_id_probe)
    monkeypatch.setattr("sanctum_cli.commands.link._node_signals", _server_signals)
    out = tmp_path / "sub" / "wifi-mac-stability.mobileconfig"
    result = runner.invoke(app, ["link", "optimize", "--apply", "--profile-out", str(out)])
    assert result.exit_code == 0, result.stdout
    assert out.exists()
    assert oct(out.stat().st_mode & 0o777) == "0o644"

    parsed = plistlib.loads(out.read_bytes())
    assert parsed["PayloadType"] == "Configuration"
    payload = parsed["PayloadContent"][0]
    assert payload["SSID_STR"] == "ClosetNet"
    assert payload["MACAddressRandomization"] is False

    # Apple-like guidance (open + approve), and the honest no-toggle note.
    assert "Private Wi-Fi Address: Off" in result.stdout
    assert "never toggles the radio" in result.stdout


def test_optimize_apply_default_profile_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no --profile-out, --apply writes to the default sanctum path."""
    monkeypatch.setattr("sanctum_cli.net.link.probe_wifi", _randomized_probe)
    monkeypatch.setattr("sanctum_cli.net.link.probe_identity", _rotating_id_probe)
    monkeypatch.setattr("sanctum_cli.commands.link._node_signals", _server_signals)
    out = tmp_path / "wifi-mac-stability.mobileconfig"
    monkeypatch.setattr("sanctum_cli.net.link.default_profile_path", lambda: out)
    result = runner.invoke(app, ["link", "optimize", "--apply"])
    assert result.exit_code == 0, result.stdout
    assert out.exists()
    assert plistlib.loads(out.read_bytes())["PayloadType"] == "Configuration"


def test_status_shows_identity_verdict(tmp_path: Path) -> None:
    log = tmp_path / "wifi.log"
    log.write_text("2026-07-01T10:00:00 ssid=X rtt=2/3/4/1 loss=0.0% load=[1] ok\n")
    quarantined = linkmod.IdentityProbe(
        iface="en1",
        ssid="Nepveu-6G",
        current_mac="32:a6:f4:de:54:cf",
        hardware_mac="d0:11:e5:1c:88:59",
        security="WPA2_PSK",
        associated=True,
        router_arp_verified=False,
        gateway_reachable=False,
    )
    with patch.object(linkmod, "probe_identity", return_value=quarantined):
        r = runner.invoke(link_app, ["status", "--log", str(log)])
    assert r.exit_code == 0
    assert "IDENTITY_QUARANTINED" in r.stdout
    assert "HEALTHY" in r.stdout  # existing link-health verdict still shown


# ─── optimize (Task 6 — node-classify + --verify + server-gated --apply) ──


def _mk_probe(**kw: object) -> link.IdentityProbe:
    base: dict[str, object] = dict(
        iface="en1",
        ssid="Nepveu-6G",
        current_mac="32:a6:f4:de:54:cf",
        hardware_mac="d0:11:e5:1c:88:59",
        security="WPA2_PSK",
        associated=True,
        router_arp_verified=False,
        gateway_reachable=False,
    )
    base.update(kw)
    return linkmod.IdentityProbe(**base)  # type: ignore[arg-type]


def test_optimize_apply_enrolls_server(tmp_path: Path) -> None:
    """A SERVER on a rotating MAC → --apply enrolls, carrying the DETECTED encryption."""
    out = tmp_path / "p.mobileconfig"
    with (
        patch.object(linkmod, "probe_identity", return_value=_mk_probe()),
        patch.object(
            linkmod,
            "probe_wifi",
            return_value=linkmod.WifiProbe(
                "en1", "32:a6:f4:de:54:cf", "d0:11:e5:1c:88:59", "Nepveu-6G"
            ),
        ),
        patch(
            "sanctum_cli.commands.link._node_signals",
            return_value=linkmod.NodeSignals(30.0, "Manual", True, 1, False),
        ),
    ):
        r = runner.invoke(link_app, ["optimize", "--apply", "--profile-out", str(out)])
    assert r.exit_code == 0, r.stdout
    assert out.exists()
    assert "WPA2" in out.read_text()  # detected encryption carried, not the WPA3 default


def test_optimize_apply_roamer_nudges_no_write(tmp_path: Path) -> None:
    """A ROAMER → --apply nudges to opt in and writes NOTHING (privacy-first)."""
    out = tmp_path / "p.mobileconfig"
    with (
        patch.object(linkmod, "probe_identity", return_value=_mk_probe()),
        patch(
            "sanctum_cli.commands.link._node_signals",
            return_value=linkmod.NodeSignals(30.0, "DHCP", False, 9, True),
        ),
    ):
        r = runner.invoke(link_app, ["optimize", "--apply", "--profile-out", str(out)])
    assert r.exit_code == 0, r.stdout
    assert not out.exists()
    assert "opt-in" in r.stdout.lower() or "--force" in r.stdout


def test_optimize_verify_honest_pass_and_fail() -> None:
    """--verify prints ✓ ONLY on hardware-MAC + RouterARPVerified; else ✗ (honest)."""
    with patch.object(
        linkmod,
        "probe_identity",
        return_value=_mk_probe(
            current_mac="d0:11:e5:1c:88:59",
            router_arp_verified=True,
            gateway_reachable=True,
        ),
    ):
        r = runner.invoke(link_app, ["optimize", "--verify"])
    assert r.exit_code == 0 and "✓" in r.stdout
    with patch.object(linkmod, "probe_identity", return_value=_mk_probe()):
        r2 = runner.invoke(link_app, ["optimize", "--verify"])
    assert "✗" in r2.stdout or "not" in r2.stdout.lower()
