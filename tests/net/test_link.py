"""Tests for the Sanctum Link diagnosis classifier — pure, no network.

Ported from /tmp/link-build/test_link_diagnose.py to the dataclass API
(``Sample`` / ``Diagnosis``) in ``sanctum_cli.net.link``.
"""

from __future__ import annotations

import plistlib

from sanctum_cli.net.link import (
    IdentityProbe,
    Sample,
    _enc_from_security,
    analyze_mac,
    classify,
    diagnose_identity,
    is_locally_administered,
    parse_log,
    probe_identity,
    probe_wifi,
    render_mac_stability_profile,
)


def _s(
    avg: float,
    load: float,
    loss: float = 0.0,
    degraded: bool | None = None,
    mx: float | None = None,
) -> Sample:
    mx = avg * 3 if mx is None else mx
    # Mirror the bash sentinel's degraded rule EXACTLY (net/link.py SENTINEL_SCRIPT:
    # avg>20 OR max>100 OR any loss). Re-inventing it here as `avg>20 or loss>0`
    # let a real low-avg / high-max single-spike window read as "ok" in the tests
    # while the producer flags it — a shared-assumption drift.
    flag = (avg > 20 or mx > 100 or loss > 0) if degraded is None else degraded
    return Sample(min=3.0, avg=avg, max=mx, std=avg, loss=loss, load=load, degraded=flag)


def test_no_data() -> None:
    assert classify([]).verdict == "NO_DATA"


def test_healthy_low_latency() -> None:
    s = [_s(5, 2.0), _s(6, 2.1), _s(4, 1.9), _s(7, 2.2)]
    assert classify(s).verdict == "HEALTHY"


def test_radio_when_loss_present() -> None:
    # Loss > 1% means the radio itself is dropping frames, regardless of load.
    s = [_s(8, 2.0, loss=3.0), _s(9, 2.1, loss=2.0), _s(7, 2.0, loss=4.0)]
    assert classify(s).verdict == "RADIO"


def test_load_when_latency_tracks_load() -> None:
    # Latency rises monotonically with load, zero loss -> LOAD/CAPACITY.
    s = [_s(5, 2.5, degraded=False), _s(22, 3.4), _s(43, 4.5), _s(80, 5.5), _s(110, 6.5)]
    r = classify(s)
    assert r.verdict == "LOAD"
    assert "WIRED" in r.remedy


def test_scan_when_degraded_but_uncorrelated_with_load() -> None:
    # Periodic latency spikes at CONSTANT low load -> off-channel scanning.
    s = [
        _s(5, 2.0, degraded=False),
        _s(60, 2.0),
        _s(5, 2.0, degraded=False),
        _s(70, 2.0),
        _s(5, 2.0, degraded=False),
        _s(65, 2.0),
    ]
    assert classify(s).verdict == "SCAN"


def test_parse_real_sentinel_line() -> None:
    line = (
        "2026-06-29T21:25:35 ssid=<redacted> "
        "rtt=4.748/107.547/520.332/138.349 loss=0.0% "
        "load=[6.48 4.39 3.89] DEGRADED"
    )
    got = parse_log(line)
    assert len(got) == 1
    assert got[0].avg == 107.547
    assert got[0].load == 6.48
    assert got[0].loss == 0.0
    assert got[0].degraded is True


def test_parse_skips_garbage_lines() -> None:
    assert parse_log("hello\n\n# comment") == []


def test_reference_mini_dataset_is_load_bound() -> None:
    # The actual 8-sample window from the Mini (2026-06-29) must classify LOAD.
    raw = """\
2026-06-29T21:07:03 ssid=x rtt=2.479/34.863/106.761/36.142 loss=0.0% load=[3.19 3.28 3.17] DEGRADED
2026-06-29T21:07:57 ssid=x rtt=2.837/53.026/144.474/56.332 loss=0.0% load=[3.17 3.27 3.17] DEGRADED
2026-06-29T21:10:09 ssid=x rtt=2.531/5.408/13.028/2.637 loss=0.0% load=[2.84 3.19 3.15] ok
2026-06-29T21:13:14 ssid=x rtt=3.425/35.831/164.101/53.661 loss=0.0% load=[3.58 3.86 3.51] DEGRADED
2026-06-29T21:16:19 ssid=x rtt=3.217/42.700/122.192/45.775 loss=0.0% load=[4.49 4.10 3.66] DEGRADED
2026-06-29T21:19:24 ssid=x rtt=2.589/30.183/175.012/48.336 loss=0.0% load=[3.87 4.10 3.75] DEGRADED
2026-06-29T21:22:29 ssid=x rtt=3.240/22.226/78.362/23.976 loss=0.0% load=[3.42 3.66 3.62] DEGRADED
2026-06-29T21:25:35 ssid=x rtt=4.748/107.547/520.332/138.349 loss=0.0% load=[6.48 4.39 3.89] DEGRADED
"""
    assert classify(parse_log(raw)).verdict == "LOAD"


def test_total_loss_window_classifies_radio_not_dropped() -> None:
    # BOUNDARY (Contracts-at-the-Boundary): on 100% loss macOS ping prints no
    # round-trip line, so the sampler emits `rtt=NA loss=100.0%`. The parser MUST
    # keep that loss signal (not silently drop the line) -> RADIO, never NO_DATA.
    # Fixture is the sampler's literal printf output, not a hand-built Sample.
    line = (
        "2026-06-29T22:30:00 ssid=Net rtt=NA loss=100.0% "
        "load=[5.00 5.00 5.00] DEGRADED"
    )
    got = parse_log(line)
    assert len(got) == 1
    assert got[0].loss == 100.0
    assert got[0].degraded is True
    assert classify(got).verdict == "RADIO"


def test_mixed_good_and_dead_windows_not_false_healthy() -> None:
    # A flapping link (good samples + total-loss samples) must NOT read HEALTHY.
    raw = "\n".join(
        [
            "2026-06-29T22:00:00 ssid=x rtt=4.0/6.0/9.0/2.0 loss=0.0% load=[2.0 2.0 2.0] ok",
            "2026-06-29T22:03:00 ssid=x rtt=NA loss=100.0% load=[2.1 2.0 2.0] DEGRADED",
            "2026-06-29T22:06:00 ssid=x rtt=4.0/6.0/9.0/2.0 loss=0.0% load=[2.0 2.0 2.0] ok",
            "2026-06-29T22:09:00 ssid=x rtt=NA loss=100.0% load=[2.1 2.0 2.0] DEGRADED",
        ]
    )
    assert classify(parse_log(raw)).verdict != "HEALTHY"


def test_no_gateway_line_is_excluded_not_false_radio() -> None:
    # A NO_GATEWAY sample (couldn't find a gateway to ping) is a MEASUREMENT
    # failure, not a dead radio — it must be EXCLUDED so it never masquerades as
    # RADIO/loss. (A real total-loss line to a gateway, rtt=NA loss=100.0
    # DEGRADED, IS kept — see test_total_loss_window_classifies_radio.)
    line = "2026-06-29T22:30:00 ssid=Net rtt=NA loss=NA% load=[2.0 2.0 2.0] NO_GATEWAY"
    assert parse_log(line) == []


# ─── MAC stability (P2 — Optimize client) ────────────────────────────

# The reference incident's two MACs: the live randomized address the AP saw
# (rotation churn) and the burned-in hardware MAC that fixed the link.
RANDOMIZED_MAC = "de:48:45:83:ae:0a"
HARDWARE_MAC = "84:2f:57:02:be:ee"


def test_analyze_mac_randomized_when_current_differs_from_hardware() -> None:
    a = analyze_mac(RANDOMIZED_MAC, HARDWARE_MAC)
    assert a.randomized is True
    assert a.current == RANDOMIZED_MAC
    assert a.hardware == HARDWARE_MAC
    # The remedy must name the fix that proved it (Private Wi-Fi Address Off).
    assert "Private Wi-Fi Address" in a.remedy


def test_analyze_mac_stable_when_current_equals_hardware() -> None:
    a = analyze_mac(HARDWARE_MAC, HARDWARE_MAC)
    assert a.randomized is False
    assert a.risk == "none — the node is on its stable hardware MAC."


def test_analyze_mac_equality_is_case_insensitive() -> None:
    # The two system reads can differ in case; a case-only diff is NOT randomized.
    assert analyze_mac(HARDWARE_MAC.upper(), HARDWARE_MAC.lower()).randomized is False


def test_is_locally_administered_bit() -> None:
    # 0xde = 1101 1110 → LAA bit (bit 1) set → randomized/private.
    assert is_locally_administered(RANDOMIZED_MAC) is True
    # 0x84 = 1000 0100 → LAA bit clear → universally-administered hardware MAC.
    assert is_locally_administered(HARDWARE_MAC) is False
    # Malformed first octet cannot be proven locally-administered.
    assert is_locally_administered("") is False
    assert is_locally_administered("zz:00:00:00:00:00") is False


def test_render_profile_round_trips_via_plistlib() -> None:
    ssid = "Sanctum-Closet-5G"
    xml = render_mac_stability_profile(ssid, HARDWARE_MAC)
    # Real artifact through the real consumer (Contracts at the Boundary): the
    # rendered bytes MUST parse as a plist, not just "look like" one.
    parsed = plistlib.loads(xml.encode("utf-8"))
    assert parsed["PayloadType"] == "Configuration"
    payload = parsed["PayloadContent"][0]
    assert payload["PayloadType"] == "com.apple.wifi.managed"
    # The headline contract: MAC randomization disabled for this SSID.
    assert payload["SSID_STR"] == ssid
    assert payload["MACAddressRandomization"] is False


def test_render_profile_contains_ssid_and_randomization_key() -> None:
    ssid = "Sanctum-Closet-5G"
    xml = render_mac_stability_profile(ssid, HARDWARE_MAC)
    assert ssid in xml
    assert "MACAddressRandomization" in xml
    assert "<false/>" in xml


def test_render_profile_is_deterministic() -> None:
    # Same inputs → byte-identical output (UUIDs derived from a hash, sorted keys),
    # so re-applying never churns the installed profile's identity.
    a = render_mac_stability_profile("NetA", HARDWARE_MAC)
    b = render_mac_stability_profile("NetA", HARDWARE_MAC)
    assert a == b
    # Different inputs → different identity (no UUID collision across networks).
    assert render_mac_stability_profile("NetB", HARDWARE_MAC) != a


def test_probe_wifi_with_fake_runner() -> None:
    # Fake the four system reads from REAL macOS command output shapes (derived
    # from `networksetup`/`ifconfig`/`ipconfig` on a live Mac — a different source
    # than the parser), so the probe's parsing contract is exercised end to end.
    outputs: dict[tuple[str, ...], str] = {
        ("networksetup", "-listallhardwareports"): (
            "Hardware Port: Ethernet\nDevice: en4\nEthernet Address: 00:11:22:33:44:55\n\n"
            "Hardware Port: Wi-Fi\nDevice: en0\nEthernet Address: 84:2f:57:02:be:ee\n"
        ),
        ("ifconfig", "en0"): f"\tether {RANDOMIZED_MAC}\n",
        ("networksetup", "-getmacaddress", "en0"): (
            f"Ethernet Address: {HARDWARE_MAC} (Device: en0)\n"
        ),
        ("ipconfig", "getsummary", "en0"): "  BSSID : aa:bb:cc:dd:ee:ff\n  SSID : ClosetNet\n",
    }

    def fake_run(argv: list[str]) -> str:
        return outputs[tuple(argv)]

    probe = probe_wifi(run=fake_run)
    assert probe.iface == "en0"
    assert probe.current_mac == RANDOMIZED_MAC
    assert probe.hardware_mac == HARDWARE_MAC
    assert probe.ssid == "ClosetNet"
    # And the pure analysis over the probe is the rotation verdict.
    assert analyze_mac(probe.current_mac, probe.hardware_mac).randomized is True


def test_probe_wifi_unverified_when_iface_not_found() -> None:
    # No "Wi-Fi" hardware port -> probe must NOT silently fall back to en0
    # (Ethernet on a Mac mini, which reads a false-STABLE); it returns an
    # UNVERIFIED probe (empty iface + MACs) so the audit reports UNVERIFIED.
    def fake_run(argv: list[str]) -> str:
        if argv == ["networksetup", "-listallhardwareports"]:
            return "Hardware Port: Ethernet\nDevice: en4\nEthernet Address: 00:11:22:33:44:55\n"
        raise AssertionError(f"must not read an interface when Wi-Fi is absent: {argv}")

    probe = probe_wifi(run=fake_run)
    assert probe.iface == ""
    assert probe.current_mac == ""
    assert probe.hardware_mac == ""


def test_render_profile_carries_encryption_type() -> None:
    # The managed Wi-Fi payload must carry EncryptionType so it is not a
    # credential-less duplicate that disrupts the existing association.
    parsed = plistlib.loads(render_mac_stability_profile("NetA", HARDWARE_MAC).encode())
    assert parsed["PayloadContent"][0]["EncryptionType"] == "WPA3"
    parsed2 = plistlib.loads(
        render_mac_stability_profile("NetA", HARDWARE_MAC, encryption_type="WPA2").encode()
    )
    assert parsed2["PayloadContent"][0]["EncryptionType"] == "WPA2"


# ─── Link Identity Guard — Task 1: IdentityProbe + probe_identity ─────────────
# The LITERAL ipconfig getsummary shape captured on the Mini during the incident.
_MINI_GETSUMMARY = """  SSID : Nepveu-6G
  Security : WPA2_PSK
  LinkStatusActive : TRUE
  RouterARPVerified : FALSE
  RouterARPTimedOut : TRUE
"""
_HEALTHY_GETSUMMARY = """  SSID : Nepveu-6G
  Security : WPA3_SAE
  LinkStatusActive : TRUE
  RouterARPVerified : TRUE
"""


def _fake_runner(mapping):
    def run(argv):
        key = " ".join(argv)
        for pat, out in mapping.items():
            if pat in key:
                return out
        return ""
    return run


def test_probe_identity_reads_quarantine_signature():
    run = _fake_runner({
        "listallhardwareports": "Hardware Port: Wi-Fi\nDevice: en1\nEthernet Address: d0:11:e5:1c:88:59",
        "ifconfig en1": "\tether 32:a6:f4:de:54:cf",
        "getmacaddress en1": "Ethernet Address: d0:11:e5:1c:88:59",
        "getsummary en1": _MINI_GETSUMMARY,
        "route -n get default": "gateway: 10.0.0.1\ninterface: en1",
        "ping": "0 packets received, 100.0% packet loss",
    })
    p = probe_identity(run=run)
    assert p.iface == "en1"
    assert p.current_mac == "32:a6:f4:de:54:cf"
    assert p.hardware_mac == "d0:11:e5:1c:88:59"
    assert p.ssid == "Nepveu-6G"
    assert p.security == "WPA2_PSK"
    assert p.associated is True
    assert p.router_arp_verified is False
    assert p.gateway_reachable is False


def test_probe_identity_iface_absent_is_unverified():
    p = probe_identity(run=_fake_runner({}))
    assert p.iface == ""
    assert p.associated is False
    assert p.router_arp_verified is None


def test_enc_from_security_maps_wpa3_and_defaults_wpa2():
    assert _enc_from_security("WPA3_SAE") == "WPA3"
    assert _enc_from_security("WPA2_PSK") == "WPA2"
    assert _enc_from_security(None) == "WPA2"
    assert _enc_from_security("weird") == "WPA2"


# ─── Link Identity Guard — Task 2: diagnose_identity truth table ──────────────


def _probe(**kw):
    base = dict(iface="en1", ssid="Nepveu-6G", current_mac="d0:11:e5:1c:88:59",
                hardware_mac="d0:11:e5:1c:88:59", security="WPA2_PSK",
                associated=True, router_arp_verified=True, gateway_reachable=True)
    base.update(kw)
    return IdentityProbe(**base)


def test_diagnose_quarantined_is_the_mini_signature():
    d = diagnose_identity(_probe(current_mac="32:a6:f4:de:54:cf",
                                 router_arp_verified=False, gateway_reachable=False))
    assert d.verdict == "IDENTITY_QUARANTINED"


def test_diagnose_rotating_when_random_mac_but_reachable():
    d = diagnose_identity(_probe(current_mac="32:a6:f4:de:54:cf"))
    assert d.verdict == "IDENTITY_ROTATING"


def test_diagnose_stable_on_hardware_mac():
    assert diagnose_identity(_probe()).verdict == "IDENTITY_STABLE"


def test_diagnose_unverified_when_not_associated_or_unread():
    assert diagnose_identity(_probe(associated=False)).verdict == "IDENTITY_UNVERIFIED"
    assert diagnose_identity(_probe(iface="", current_mac="", hardware_mac="")).verdict == "IDENTITY_UNVERIFIED"
