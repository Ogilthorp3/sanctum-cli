# Link Identity Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automate the anti-quarantine / stable-Wi-Fi-identity fix for Sanctum beta users — detect the "associated but LAN-dead on a rotating MAC" signature, auto-classify the node, and (for servers) generate + guide the Fixed-MAC profile with a self-healing sentinel — all inside `sanctum link` + `sanctum onboard`.

**Architecture:** Additive extension of the existing pure-core (`sanctum_cli/net/link.py`) + thin-shell (`sanctum_cli/commands/link.py`, `commands/onboard.py`) pattern. New pure functions are unit-tested behind the existing injected `CommandRunner` seam; the shell mirrors the established `_render`/`_write_profile`/`_run_ha_green` helpers. Nothing in the P1/P2 link feature changes behavior — existing tests must keep passing.

**Tech Stack:** Python 3.12, Typer, Rich, pure stdlib (`plistlib`, `re`, `uuid`). Gate = `make check` (ruff + mypy --strict + pytest). Spec: `docs/superpowers/specs/2026-07-01-link-identity-guard-design.md`.

**Working dir:** `/private/tmp/sanctum-cli-identity` (branch `feat/link-identity-guard`, stacked on `feat/link-optimizer`).

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `sanctum_cli/net/link.py` | pure identity probe/diagnose/classify + Firewalla enrichment + sentinel identity line | **extend** (Tasks 1–4, 7) |
| `sanctum_cli/commands/link.py` | `status` shows IDENTITY; `optimize` gains node-classify + `--verify` | **extend** (Tasks 5–6) |
| `sanctum_cli/commands/onboard.py` | `wifi-identity` gate in the "Your Network" chapter | **extend** (Task 8) |
| `tests/net/test_link.py` | pure-function tests | **extend** (Tasks 1–4, 7) |
| `tests/test_link_cli.py` | CLI tests | **extend** (Tasks 5–6) |
| `tests/test_onboard*.py` | onboard gate test | **extend** (Task 8) |

**Cross-task type contract (names are fixed; later tasks depend on them):**
`IdentityProbe`, `probe_identity`, `IdentityDiagnosis`, `diagnose_identity`, `NodeSignals`, `NodeClass`, `classify_node`, `QuarantineFinding`, `firewalla_quarantine_check`, verdict strings `IDENTITY_QUARANTINED|IDENTITY_ROTATING|IDENTITY_STABLE|IDENTITY_UNVERIFIED`, node-class strings `SERVER|ROAMER|UNKNOWN`.

---

## Task 1: `IdentityProbe` + `probe_identity` (the L3 identity read)

**Files:**
- Modify: `sanctum_cli/net/link.py` (add after `probe_wifi`, ~line 606)
- Test: `tests/net/test_link.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/net/test_link.py — append
from sanctum_cli.net.link import IdentityProbe, probe_identity, _enc_from_security

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
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /private/tmp/sanctum-cli-identity && .venv/bin/pytest tests/net/test_link.py -k identity -q`
Expected: FAIL — `ImportError: cannot import name 'IdentityProbe'`.

- [ ] **Step 3: Implement in `sanctum_cli/net/link.py`**

```python
# ─── Link Identity Guard (anti-quarantine: identity probe + diagnose) ──

_LINK_ACTIVE_RE = re.compile(r"LinkStatusActive\s*:\s*(TRUE|FALSE)", re.IGNORECASE)
_ROUTER_ARP_RE = re.compile(r"RouterARPVerified\s*:\s*(TRUE|FALSE)", re.IGNORECASE)
_SECURITY_RE = re.compile(r"^\s*Security\s*:\s*(.+?)\s*$", re.MULTILINE)
_GW_RE = re.compile(r"gateway:\s*([0-9a-fA-F:.]+)")


@dataclass(frozen=True)
class IdentityProbe:
    """Live network *identity* facts (distinct from the sentinel link-health log).

    Read from ``ipconfig getsummary`` (+ one bounded gateway ping) — router-agnostic.
    ``router_arp_verified``/``gateway_reachable`` are None when unknown/unattempted;
    empty ``iface`` (or MACs) means UNVERIFIED — never a silent false-STABLE.
    """

    iface: str
    ssid: str | None
    current_mac: str
    hardware_mac: str
    security: str | None
    associated: bool
    router_arp_verified: bool | None
    gateway_reachable: bool | None


def _enc_from_security(security: str | None) -> str:
    """Map an ``ipconfig getsummary`` Security value to a profile EncryptionType.

    A mismatch would make macOS create a non-joining duplicate network, so this is
    a hard requirement, not a guess; WPA2 is the safe default (covers WPA2/WPA3
    personal transition).
    """
    if security and "WPA3" in security.upper():
        return "WPA3"
    return "WPA2"


def _tri(pattern: re.Pattern[str], text: str) -> bool | None:
    """TRUE/FALSE → bool; field absent → None (unknown, fail-open to None)."""
    m = pattern.search(text)
    if not m:
        return None
    return m.group(1).upper() == "TRUE"


def probe_identity(run: CommandRunner | None = None, *, ping_gateway: bool = True) -> IdentityProbe:
    """Read the node's live Wi-Fi *identity + reachability* behind an injected runner.

    Thin impure boundary: interface + MACs (reused from :func:`probe_wifi`'s reads),
    plus ``ipconfig getsummary`` association/RouterARPVerified/Security, plus a
    bounded default-gateway ping. Fail-closed: no Wi-Fi iface → UNVERIFIED probe.
    """
    runner = run if run is not None else _real_run
    iface = _parse_wifi_iface(runner(["networksetup", "-listallhardwareports"]))
    if not iface:
        return IdentityProbe(
            iface="", ssid=None, current_mac="", hardware_mac="", security=None,
            associated=False, router_arp_verified=None, gateway_reachable=None,
        )
    current = _first_match(_ETHER_RE, runner(["ifconfig", iface])) or ""
    hardware = _first_match(_GETMAC_RE, runner(["networksetup", "-getmacaddress", iface])) or ""
    summary = runner(["ipconfig", "getsummary", iface])
    ssid = _first_match(_SSID_RE, summary)
    security = _first_match(_SECURITY_RE, summary)
    associated = _tri(_LINK_ACTIVE_RE, summary) is True
    arp = _tri(_ROUTER_ARP_RE, summary)

    gw_reachable: bool | None = None
    if ping_gateway and associated:
        gw = _first_match(_GW_RE, runner(["route", "-n", "get", "default"]))
        if gw:
            out = runner(["ping", "-c", "3", "-t", "2", gw])
            gw_reachable = "0.0% packet loss" in out or ("packet loss" in out and "100.0%" not in out)
    return IdentityProbe(
        iface=iface, ssid=ssid, current_mac=current, hardware_mac=hardware,
        security=security, associated=associated,
        router_arp_verified=arp, gateway_reachable=gw_reachable,
    )
```

- [ ] **Step 4: Run to verify pass** — `… pytest tests/net/test_link.py -k "identity or enc_from" -q` → PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(link): IdentityProbe + probe_identity (router-agnostic L3 identity read)"`

---

## Task 2: `diagnose_identity` (the pure IDENTITY truth table)

**Files:** Modify `sanctum_cli/net/link.py`; Test `tests/net/test_link.py`.

- [ ] **Step 1: Write the failing tests**

```python
from sanctum_cli.net.link import IdentityDiagnosis, diagnose_identity

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
```

- [ ] **Step 2: Verify fail** — `ImportError: diagnose_identity`.

- [ ] **Step 3: Implement**

```python
_IDENTITY_REMEDY: dict[str, str] = {
    "IDENTITY_QUARANTINED": (
        "This node is associated but its gateway is unreachable while it presents a "
        "rotating/private MAC — the router does not recognize it (a DHCP-reservation "
        "miss or device quarantine). Pin it to its hardware MAC: sanctum link optimize "
        "--apply (or Private Wi-Fi Address ▸ Off), then re-verify."
    ),
    "IDENTITY_ROTATING": (
        "MAC is randomized (private address) — it works now but will isolate the node "
        "on the next router-trust reset (reboot/renumber). Pin to the hardware MAC: "
        "sanctum link optimize --apply."
    ),
    "IDENTITY_STABLE": "Identity is correct — the node is on its stable hardware MAC.",
    "IDENTITY_UNVERIFIED": "Could not read the Wi-Fi identity — is this node associated? Re-run when connected.",
}


@dataclass(frozen=True)
class IdentityDiagnosis:
    """The IDENTITY verdict (who the node is on the network) + remedy + probe."""

    verdict: str
    detail: str
    remedy: str
    probe: IdentityProbe


def diagnose_identity(probe: IdentityProbe) -> IdentityDiagnosis:
    """PURE: classify the node's Wi-Fi *identity* (orthogonal to link health).

    UNVERIFIED (fail-closed) when we cannot read it; QUARANTINED for the exact
    incident signature (associated + LAN-dead + rotating MAC); ROTATING for the
    at-risk private-MAC case; STABLE when on the hardware MAC.
    """
    if not probe.iface or not probe.current_mac or not probe.hardware_mac or not probe.associated:
        v = "IDENTITY_UNVERIFIED"
        return IdentityDiagnosis(v, "identity could not be read", _IDENTITY_REMEDY[v], probe)
    randomized = (
        is_locally_administered(probe.current_mac)
        or probe.current_mac.lower() != probe.hardware_mac.lower()
    )
    lan_dead = probe.router_arp_verified is False or probe.gateway_reachable is False
    if randomized and lan_dead:
        v, detail = "IDENTITY_QUARANTINED", (
            f"associated on {probe.iface} but gateway unreachable "
            f"(RouterARPVerified={probe.router_arp_verified}) while presenting "
            f"rotating MAC {probe.current_mac} ≠ hardware {probe.hardware_mac}"
        )
    elif randomized:
        v, detail = "IDENTITY_ROTATING", (
            f"rotating MAC {probe.current_mac} ≠ hardware {probe.hardware_mac}; reachable for now"
        )
    else:
        note = "" if not lan_dead else " (gateway unreachable — see `sanctum link status` for a link-health read)"
        v, detail = "IDENTITY_STABLE", f"on hardware MAC {probe.hardware_mac}{note}"
    return IdentityDiagnosis(v, detail, _IDENTITY_REMEDY[v], probe)
```

- [ ] **Step 4: Verify pass.** **Step 5: Commit** — `feat(link): diagnose_identity — QUARANTINED/ROTATING/STABLE/UNVERIFIED truth table`.

---

## Task 3: `classify_node` (SERVER / ROAMER / UNKNOWN)

**Files:** Modify `sanctum_cli/net/link.py`; Test `tests/net/test_link.py`.

- [ ] **Step 1: Failing tests**

```python
from sanctum_cli.net.link import NodeSignals, classify_node

def _sig(**kw):
    base = dict(uptime_days=10.0, ip_config_method="Manual", ip_is_reserved_or_static=True,
                distinct_ssids_seen=1, is_portable=False)
    base.update(kw); return NodeSignals(**base)

def test_classify_server_when_fixed_infra():
    assert classify_node(_sig()).klass == "SERVER"

def test_classify_roamer_when_portable():
    assert classify_node(_sig(is_portable=True)).klass == "ROAMER"

def test_classify_roamer_when_many_ssids():
    assert classify_node(_sig(distinct_ssids_seen=9)).klass == "ROAMER"

def test_classify_unknown_is_conservative():
    # not portable, short uptime, DHCP/no reservation, a couple SSIDs → UNKNOWN
    assert classify_node(_sig(uptime_days=0.2, ip_config_method="DHCP",
                              ip_is_reserved_or_static=False, distinct_ssids_seen=3)).klass == "UNKNOWN"
```

- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement**

```python
SERVER_UPTIME_DAYS = 3.0
SSID_ROAMER_THRESHOLD = 5
SSID_SERVER_MAX = 3


@dataclass(frozen=True)
class NodeSignals:
    uptime_days: float
    ip_config_method: str  # "Manual" | "DHCP" | ""
    ip_is_reserved_or_static: bool
    distinct_ssids_seen: int
    is_portable: bool


@dataclass(frozen=True)
class NodeClass:
    klass: str  # "SERVER" | "ROAMER" | "UNKNOWN"
    reason: str


def classify_node(signals: NodeSignals) -> NodeClass:
    """PURE: is this a fixed-infra SERVER (auto-enroll) or a ROAMER (opt-in only)?

    Conservative and privacy-first: portability or many-SSIDs → ROAMER; a
    non-portable, long-lived / static-or-reserved, single-SSID node → SERVER;
    anything ambiguous → UNKNOWN (treated as ROAMER downstream, never auto-enrolled).
    """
    if signals.is_portable or signals.distinct_ssids_seen > SSID_ROAMER_THRESHOLD:
        return NodeClass("ROAMER", "portable or roams across many networks")
    fixed = signals.uptime_days >= SERVER_UPTIME_DAYS or signals.ip_is_reserved_or_static
    if not signals.is_portable and fixed and signals.distinct_ssids_seen <= SSID_SERVER_MAX:
        return NodeClass("SERVER", "always-on / static-or-reserved IP / single network")
    return NodeClass("UNKNOWN", "insufficient signal — treated as roamer (privacy-first)")
```

- [ ] **Step 4: Verify pass.** **Step 5: Commit** — `feat(link): classify_node — server vs roamer (privacy-first UNKNOWN→roamer)`.

---

## Task 4: `firewalla_quarantine_check` (optional enrichment)

**Files:** Modify `sanctum_cli/net/link.py`; Test `tests/net/test_link.py`.

- [ ] **Step 1: Failing tests**

```python
from sanctum_cli.net.link import QuarantineFinding, firewalla_quarantine_check

def test_fw_check_none_when_no_transport():
    assert firewalla_quarantine_check("32:a6:f4:de:54:cf") is None

def test_fw_check_none_when_empty_response():
    assert firewalla_quarantine_check("32:a6", transport=lambda mac: "") is None

def test_fw_check_reports_quarantine_tag():
    f = firewalla_quarantine_check("32:a6", transport=lambda mac: '["18"]')
    assert f is not None and f.quarantined is True and f.tag == "18"

def test_fw_check_reports_untagged():
    f = firewalla_quarantine_check("d0:11", transport=lambda mac: "[]")
    assert f is not None and f.quarantined is False
```

- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement**

```python
QuarantineTransport = Callable[[str], str]
"""mac → raw tags response (e.g. redis ``hget policy:mac:<MAC> tags``); "" when absent."""


@dataclass(frozen=True)
class QuarantineFinding:
    quarantined: bool
    tag: str | None
    detail: str


def firewalla_quarantine_check(
    mac: str, transport: QuarantineTransport | None = None
) -> QuarantineFinding | None:
    """OPTIONAL enrichment: is ``mac`` in a Firewalla quarantine tag?

    Returns None when no Firewalla transport is wired or it does not answer — the
    router-agnostic path never depends on this. When a transport answers with a
    tags array, a non-empty tag list means quarantined (tag "18" == the DAP
    Quarantine group observed in the incident).
    """
    if transport is None:
        return None
    raw = transport(mac).strip()
    if not raw:
        return None
    tags = re.findall(r'"(\d+)"', raw)
    if not tags:
        return QuarantineFinding(False, None, "device present, no quarantine tag")
    return QuarantineFinding(True, tags[0], f"in Firewalla tag {tags[0]} (quarantine)")
```

- [ ] **Step 4: Verify pass.** **Step 5: Commit** — `feat(link): optional firewalla_quarantine_check enrichment (None when absent)`.

---

## Task 5: `sanctum link status` shows the IDENTITY verdict

**Files:** Modify `sanctum_cli/commands/link.py`; Test `tests/test_link_cli.py`.

- [ ] **Step 1: Failing test**

```python
# tests/test_link_cli.py — append. Uses the existing CliRunner + link_app import.
from unittest.mock import patch
from sanctum_cli.net import link as linkmod

def test_status_shows_identity_verdict(tmp_path):
    log = tmp_path / "wifi.log"
    log.write_text("2026-07-01T10:00:00 ssid=X rtt=2/3/4/1 loss=0.0% load=[1] ok\n")
    quarantined = linkmod.IdentityProbe(
        iface="en1", ssid="Nepveu-6G", current_mac="32:a6:f4:de:54:cf",
        hardware_mac="d0:11:e5:1c:88:59", security="WPA2_PSK",
        associated=True, router_arp_verified=False, gateway_reachable=False)
    with patch.object(linkmod, "probe_identity", return_value=quarantined):
        r = runner.invoke(link_app, ["status", "--log", str(log)])
    assert r.exit_code == 0
    assert "IDENTITY_QUARANTINED" in r.stdout
    assert "HEALTHY" in r.stdout  # existing link-health verdict still shown
```

- [ ] **Step 2: Verify fail** — no `IDENTITY_QUARANTINED` in output.
- [ ] **Step 3: Implement** — in `link_status`, after the existing `_render(link.classify(recent))`, add an identity read. Add a `_render_identity(diag)` helper mirroring `_render` (colour map: `IDENTITY_STABLE`→green, `IDENTITY_ROTATING`→yellow, `IDENTITY_QUARANTINED`→red, `IDENTITY_UNVERIFIED`→dim). Call `link.diagnose_identity(link.probe_identity())` inside a `try/except` that degrades to UNVERIFIED on any error (never breaks `status`). Print a `[bold]IDENTITY:[/]` block. Keep exit 0.

```python
_IDENTITY_STYLE = {"IDENTITY_STABLE": "green", "IDENTITY_ROTATING": "yellow",
                   "IDENTITY_QUARANTINED": "red", "IDENTITY_UNVERIFIED": "dim"}

def _render_identity(diag: "link.IdentityDiagnosis") -> None:
    style = _IDENTITY_STYLE.get(diag.verdict, "white")
    console.print(f"[bold]IDENTITY:[/] [{style}]{escape(diag.verdict)}[/]")
    console.print(f"  {escape(diag.detail)}")
    console.print(f"  → {escape(diag.remedy)}")

# ...in link_status, after _render(link.classify(recent)):
try:
    _render_identity(link.diagnose_identity(link.probe_identity()))
except Exception:  # noqa: BLE001 — status must never break on a probe hiccup
    console.print("[bold]IDENTITY:[/] [dim]IDENTITY_UNVERIFIED[/]")
```

- [ ] **Step 4: Verify pass** + run the whole existing CLI suite (`pytest tests/test_link_cli.py -q`) to prove no regression.
- [ ] **Step 5: Commit** — `feat(link): status surfaces the IDENTITY verdict beside link health`.

---

## Task 6: `sanctum link optimize` — node-classify + `--verify`

**Files:** Modify `sanctum_cli/commands/link.py`; Test `tests/test_link_cli.py`.

This extends the existing `link_optimize`. Behavior:
- Gather `IdentityProbe` (`link.probe_identity()`) + `NodeSignals` (a new thin `_node_signals()` reader) → `link.diagnose_identity` + `link.classify_node`.
- Print the IDENTITY verdict + node class.
- `--apply`: enroll only when `klass == "SERVER"` AND verdict in {`IDENTITY_QUARANTINED`, `IDENTITY_ROTATING`}, OR `--force`. Reuse `_write_profile`, but pass the **detected** encryption: extend `_write_profile` to accept `enc` and call `link.render_mac_stability_profile(ssid, hw, encryption_type=enc)` where `enc = link._enc_from_security(probe.security)`. ROAMER/UNKNOWN without `--force` → print the opt-in nudge, no write.
- `--verify`: re-probe, print ✓ only when `current_mac == hardware_mac` AND `router_arp_verified is True` (honest-verify), else ✗ with the current state.

- [ ] **Step 1: Failing tests**

```python
def _mk_probe(**kw):
    base = dict(iface="en1", ssid="Nepveu-6G", current_mac="32:a6:f4:de:54:cf",
                hardware_mac="d0:11:e5:1c:88:59", security="WPA2_PSK",
                associated=True, router_arp_verified=False, gateway_reachable=False)
    base.update(kw); return linkmod.IdentityProbe(**base)

def test_optimize_apply_enrolls_server(tmp_path):
    out = tmp_path / "p.mobileconfig"
    with patch.object(linkmod, "probe_identity", return_value=_mk_probe()), \
         patch.object(linkmod, "probe_wifi", return_value=linkmod.WifiProbe("en1","32:a6:f4:de:54:cf","d0:11:e5:1c:88:59","Nepveu-6G")), \
         patch("sanctum_cli.commands.link._node_signals",
               return_value=linkmod.NodeSignals(30.0,"Manual",True,1,False)):
        r = runner.invoke(link_app, ["optimize", "--apply", "--profile-out", str(out)])
    assert r.exit_code == 0
    assert out.exists()
    assert 'WPA2' in out.read_text()  # detected encryption carried, not the WPA3 default

def test_optimize_apply_roamer_nudges_no_write(tmp_path):
    out = tmp_path / "p.mobileconfig"
    with patch.object(linkmod, "probe_identity", return_value=_mk_probe()), \
         patch("sanctum_cli.commands.link._node_signals",
               return_value=linkmod.NodeSignals(30.0,"DHCP",False,9,True)):
        r = runner.invoke(link_app, ["optimize", "--apply", "--profile-out", str(out)])
    assert r.exit_code == 0
    assert not out.exists()
    assert "opt-in" in r.stdout.lower() or "--force" in r.stdout

def test_optimize_verify_honest_pass_and_fail():
    with patch.object(linkmod, "probe_identity",
                      return_value=_mk_probe(current_mac="d0:11:e5:1c:88:59",
                                             router_arp_verified=True, gateway_reachable=True)):
        r = runner.invoke(link_app, ["optimize", "--verify"])
    assert r.exit_code == 0 and "✓" in r.stdout
    with patch.object(linkmod, "probe_identity", return_value=_mk_probe()):
        r2 = runner.invoke(link_app, ["optimize", "--verify"])
    assert "✗" in r2.stdout or "not" in r2.stdout.lower()
```

- [ ] **Step 2: Verify fail** (unknown `--verify` option / no `_node_signals`).
- [ ] **Step 3: Implement**
  - Add `_node_signals() -> link.NodeSignals`: read `sysctl -n kern.boottime` → uptime_days; `ipconfig getsummary en*`/`networksetup -getinfo "Wi-Fi"` → ConfigMethod (Manual vs DHCP) + reserved/static; `sysctl -n hw.model` contains "Book" → is_portable; distinct SSIDs from the sentinel log (count unique `ssid=` tokens). Behind the module `_real_run` seam; monkeypatched in tests.
  - Add `--verify` / `--force` Typer options to `link_optimize`.
  - Extend `_write_profile(probe, profile_out, *, enc="WPA3")` to pass `encryption_type=enc`.
  - Branch the command: verify → re-probe + honest ✓/✗; else classify + (apply→gate on SERVER/force) or audit.
- [ ] **Step 4: Verify pass** + full `pytest tests/test_link_cli.py -q` (existing optimize tests keep passing — the default no-flag audit path is preserved).
- [ ] **Step 5: Commit** — `feat(link): optimize auto-classifies node + --verify (honest re-probe), server-gated --apply`.

---

## Task 7: Sentinel logs the identity tuple + drift detection

**Files:** Modify `sanctum_cli/net/link.py` (`SENTINEL_SCRIPT` + a pure parser); Test `tests/net/test_link.py`.

**Invariant to preserve (Contracts at the Boundary):** the existing `_LINE` regex matches up to `load=[<num>` and `parse_log` reads `degraded` via `endswith("DEGRADED")`. The identity token MUST be inserted **before** the trailing `ok`/`DEGRADED` flag so both invariants hold. Run the *existing* `parse_log` tests to prove no regression.

- [ ] **Step 1: Failing tests**

```python
from sanctum_cli.net.link import parse_identity, identity_is_drift

def test_parse_identity_reads_id_token():
    line = "2026-07-01T10:00:00 ssid=X rtt=2/3/4/1 loss=0.0% load=[1] id=cur=32:a6:f4:de:54:cf,hw=d0:11:e5:1c:88:59,arp=FALSE DEGRADED"
    ids = parse_identity(line)
    assert ids is not None
    assert ids["cur"] == "32:a6:f4:de:54:cf" and ids["hw"] == "d0:11:e5:1c:88:59" and ids["arp"] == "FALSE"

def test_identity_drift_true_on_random_mac_and_arp_false():
    assert identity_is_drift(parse_identity(
        "x rtt=NA loss=100.0% load=[1] id=cur=32:a6:f4:de:54:cf,hw=d0:11:e5:1c:88:59,arp=FALSE DEGRADED")) is True

def test_identity_drift_false_on_hardware_mac():
    assert identity_is_drift(parse_identity(
        "x rtt=2/3/4/1 loss=0.0% load=[1] id=cur=d0:11:e5:1c:88:59,hw=d0:11:e5:1c:88:59,arp=TRUE ok")) is False

def test_existing_parse_log_still_reads_degraded_with_id_token():
    # regression guard: the trailing flag + rtt/loss/load parse survive the id= insert
    line = "x ssid=Y rtt=2/3/4/1 loss=0.0% load=[1.5] id=cur=aa:bb:cc:dd:ee:ff,hw=aa:bb:cc:dd:ee:ff,arp=TRUE DEGRADED"
    s = parse_log(line)
    assert len(s) == 1 and s[0].degraded is True and s[0].load == 1.5
```

- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement**
  - In `SENTINEL_SCRIPT`, after computing `LOAD`/before the final `printf`, add reads (still no scan): `CURMAC="$(ifconfig "$IFACE" 2>/dev/null | awk '/ether/{print $2; exit}')"`, `HWMAC="$(networksetup -getmacaddress "$IFACE" 2>/dev/null | awk '/Ethernet Address/{print $3; exit}')"`, `ARP="$(ipconfig getsummary "$IFACE" 2>/dev/null | awk -F': ' '/RouterARPVerified/{print $2; exit}')"; [ -z "$ARP" ] && ARP="NA"`. Change the main `printf` to insert `id=cur=%s,hw=%s,arp=%s ` **before** the `%s` flag: `... load=[%s] id=cur=%s,hw=%s,arp=%s %s\n`. (The `NO_GATEWAY` branch is unchanged.)
  - Add pure `parse_identity(line) -> dict[str,str] | None` (regex `id=cur=(?P<cur>[0-9a-fA-F:]+),hw=(?P<hw>[0-9a-fA-F:]+),arp=(?P<arp>TRUE|FALSE|NA)`), and `identity_is_drift(ids) -> bool` (True when `ids` present AND `is_locally_administered(cur) or cur.lower()!=hw.lower()` AND `arp == "FALSE"`).
- [ ] **Step 4: Verify pass** + `pytest tests/net/test_link.py -q` (ALL of it — proves the existing parse_log/classify tests still pass with the format change).
- [ ] **Step 5: Commit** — `feat(link): sentinel logs identity tuple; parse_identity + drift detection (existing parser preserved)`.

---

## Task 8: `wifi-identity` onboard gate ("Your Network" chapter)

**Files:** Modify `sanctum_cli/commands/onboard.py`; Test the onboard test module (mirror the `ha-green` gate test).

- [ ] **Step 1: Failing test** — mirror the existing `ha-green` gate test: assert `_run_gate("wifi-identity", yes=True)` runs, is skippable, returns a bool, prints a green check on a mocked SERVER+quarantined probe, and makes **no live calls** (patch `link.probe_identity`, `link.classify_node` inputs, and the profile writer). Assert `"wifi-identity"` is in `_CHAPTER_GATES["Your Network"]` and in `RECIPE_GATES` for `family`/`operator`/`code`.

```python
def test_wifi_identity_gate_registered_and_runs():
    from sanctum_cli.commands import onboard
    assert "wifi-identity" in onboard._CHAPTER_GATES["Your Network"]
    for r in ("family", "operator", "code"):
        assert "wifi-identity" in onboard.RECIPE_GATES[r]
    with patch.object(onboard, "_run_wifi_identity", return_value=True) as g:
        assert onboard._run_gate("wifi-identity", yes=True) is True
        g.assert_called_once()
```

- [ ] **Step 2: Verify fail** — `KeyError`/`AssertionError` (gate not registered).
- [ ] **Step 3: Implement** — four registration edits + the gate fn (mirror `_run_ha_green`, which is the sibling in this same chapter):
  1. `RECIPE_GATES`: add `"wifi-identity"` to the Your-Network group for `family`, `operator`, `code` (place it BEFORE `ha-green`, after `network-gear`).
  2. `_CHAPTER_GATES["Your Network"]`: append `"wifi-identity"`.
  3. `_GATE_LABELS`: `"wifi-identity": "Wi-Fi identity (stable MAC)"`.
  4. `_run_gate`: add `if gate == "wifi-identity": return _run_wifi_identity(yes=yes)`.
  5. `def _run_wifi_identity(*, yes: bool) -> bool:` — probe (`link.probe_identity`) + classify (`link.classify_node(_node_signals())`) → if SERVER and verdict in {QUARANTINED, ROTATING}: generate the profile (reuse the CLI `_write_profile` path or `link.render_mac_stability_profile`) + narrate the one-click approve + `green_check(...)` only from an honest re-probe (`router_arp_verified is True` or, pre-approve, a clear "approve then it verifies" line). ROAMER/UNKNOWN → one-line informational nudge, return False. Skippable + `--yes` fast-path, matching `_run_ha_green`. **No live calls in tests** — everything behind the injected/patched seams.
- [ ] **Step 4: Verify pass** + run the existing onboard suite (`pytest -k onboard -q`) to prove the 18 interactive tests + chapter/recap tests still pass.
- [ ] **Step 5: Commit** — `feat(onboard): Wi-Fi-identity gate in Your Network — auto-enroll servers on the home SSID`.

---

## Task 9: Full gate + whole-branch review prep

**Files:** none (verification task).

- [ ] **Step 1: `make check`** → ruff clean, mypy --strict clean, pytest all green (new + existing). Fix any finding inline (TDD).
- [ ] **Step 2:** Manual smoke on this Mac: `sanctum link status` (shows IDENTITY), `sanctum link optimize` (classifies, read-only), `sanctum link optimize --verify`. Confirm no live radio mutation.
- [ ] **Step 3:** Update `docs/superpowers/specs/2026-07-01-link-identity-guard-design.md` "Open questions" if anything shifted; note the `sanctum link rescue` (sibling TB5 Layer-3) integration point as future.
- [ ] **Step 4: Commit** any gate fixes — `chore(link): make check green — Link Identity Guard end to end`.
- [ ] **Step 5:** Hand to jedi-council whole-branch review (dimensions: honest-verify, fail-closed, privacy/per-SSID, PSK-never-logged, no-live-calls-in-tests, existing-tests-intact), then merge.

---

## Self-review

**Spec coverage:** IDENTITY detection (T1–2), auto-classify (T3), FW enrichment (T4), status (T5), optimize+verify (T6), sentinel self-heal signal (T7), onboard gate (T8), gate+distribution (T9) — all spec sections mapped. **Privacy** (per-SSID/home-only, roamer opt-in) enforced in T6/T8 gating. **Honest-verify** in T5/T6/T8 (✓ only from a real re-probe). **Fail-closed** in T1/T2 (UNVERIFIED). **PSK-never-logged / hostile-value** — the profile render already round-trips via plistlib and the PSK is not part of `render_mac_stability_profile` (it takes only ssid + hardware_mac + enc); the managed payload carries no plaintext PSK, so there is no PSK-leak surface in this feature (noted: the earlier hand-built profile embedded a PSK; the productized renderer deliberately does NOT — a strictly safer contract). **Deterministic uuid5** already in the reused renderer.

**Placeholder scan:** none — every step has real code or exact edits.

**Type consistency:** `IdentityProbe`/`IdentityDiagnosis`/`NodeSignals`/`NodeClass`/`QuarantineFinding` and the verdict/class strings are used identically across T1–T8.
