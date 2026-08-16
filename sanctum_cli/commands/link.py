"""``sanctum link`` — the Sanctum Link Optimizer (Measure + Diagnose slice).

Universal: every Sanctum node runs the stability sentinel and can read its own
verdict. This slice ships two commands:

* ``status``  — read the sentinel log, classify it, print verdict + metrics +
  remedy. Read-only; exits non-zero only on a real read error. A missing log is
  NOT an error — it prints a friendly NO_DATA hint and exits 0.
* ``install`` — write the sentinel sampler (0755) + its LaunchAgent and
  best-effort ``launchctl bootstrap`` it. Idempotent.

Later phases (optimize / SQM / failover) layer on top; they are deliberately not
built here.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
from pathlib import Path  # noqa: TC003 - Typer resolves this annotation at runtime
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from sanctum_cli.errors import LocalError, SanctumError
from sanctum_cli.net import link

console = Console()
err_console = Console(stderr=True)

link_app = typer.Typer(help="Measure + diagnose the node's network link (Wi-Fi stability).")

_LAUNCHCTL_BIN = "/bin/launchctl"
_LAUNCHCTL_TIMEOUT_S = 5

# Verdict → Rich style, so a glance at the colour reads the health.
_VERDICT_STYLE: dict[str, str] = {
    "HEALTHY": "green",
    "LOAD": "yellow",
    "SCAN": "yellow",
    "RADIO": "red",
    "NO_DATA": "dim",
}

# IDENTITY verdict → Rich style (mirrors _VERDICT_STYLE for the identity block).
_IDENTITY_STYLE: dict[str, str] = {
    "IDENTITY_STABLE": "green",
    "IDENTITY_ROTATING": "yellow",
    "IDENTITY_QUARANTINED": "red",
    "IDENTITY_UNVERIFIED": "dim",
}


def _report(exc: SanctumError) -> None:
    """Pretty-print a SanctumError to stderr with its optional fix suggestion.

    Mirrors ``net._report`` so the link sub-app reports failures the same way the
    rest of the CLI does (it cannot import from ``cli`` without a cycle).
    """
    err_console.print(f"[bold red]error:[/] {exc.message}")
    if exc.fix:
        err_console.print(f"[dim]fix:[/] {exc.fix}")


def _render(diag: link.Diagnosis) -> None:
    """Print a diagnosis: verdict (coloured) + detail + metrics + remedy."""
    style = _VERDICT_STYLE.get(diag.verdict, "white")
    console.print(f"[bold]VERDICT:[/] [{style}]{escape(diag.verdict)}[/]")
    console.print(f"  {escape(diag.detail)}")
    m = diag.metrics
    if m is not None:
        console.print(
            f"  [dim]({m.samples} samples, {m.degraded_pct}% degraded, "
            f"p50 {m.p50_avg_ms}ms, worst {m.worst_avg_ms}ms, "
            f"loss {m.mean_loss_pct}%)[/]"
        )
    console.print(f"  → {escape(diag.remedy)}")


def _render_identity(diag: link.IdentityDiagnosis) -> None:
    """Print the IDENTITY block: verdict (coloured) + detail + remedy.

    Mirrors :func:`_render` for the link-health verdict, so a glance at the colour
    reads who the node is on the network alongside how healthy the link is.
    """
    style = _IDENTITY_STYLE.get(diag.verdict, "white")
    console.print(f"[bold]IDENTITY:[/] [{style}]{escape(diag.verdict)}[/]")
    console.print(f"  {escape(diag.detail)}")
    console.print(f"  → {escape(diag.remedy)}")


@link_app.command(
    "status",
    help="Diagnose link stability from the sentinel log (read-only).",
)
def link_status(
    log: Annotated[
        Path | None,
        typer.Option(
            "--log",
            help="Sentinel log path (default: ~/.sanctum/logs/wifi-stability.log).",
        ),
    ] = None,
) -> None:
    """Classify the sentinel log and print the verdict.

    A missing log is the expected fresh-install state, NOT a failure: print a
    NO_DATA hint pointing at ``sanctum link install`` and exit 0. Only a genuine
    read error (permission denied, log path is a directory) exits non-zero.
    """
    log_path = log if log is not None else link.default_log_path()
    try:
        text = log_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # No log yet → friendly NO_DATA, exit 0.
        console.print("[bold]VERDICT:[/] [dim]NO_DATA[/]")
        console.print(f"  no sentinel log at {escape(str(log_path))}")
        console.print(
            "  → Run [bold]sanctum link install[/] to start the stability "
            "sentinel, then re-run in a few minutes."
        )
        return
    except OSError as exc:
        err = LocalError(
            f"cannot read sentinel log {log_path}: {exc}",
            fix="check the path + permissions, or pass --log <file>.",
        )
        _report(err)
        raise typer.Exit(code=int(err.exit_code)) from exc

    # Window to the most recent slice: the verdict answers "is my link OK NOW?",
    # so ancient history (a node that was LOAD-bound months ago) must not dilute
    # it. The sampler also caps the on-disk log, so neither side grows unbounded.
    samples = link.parse_log(text)
    recent = samples[-link.STATUS_WINDOW_SAMPLES :]
    _render(link.classify(recent))

    # IDENTITY (who the node is on the network) sits beside link health. A probe
    # hiccup must never break ``status`` — degrade to a fail-closed UNVERIFIED.
    try:
        _render_identity(link.diagnose_identity(link.probe_identity()))
    except Exception:  # status must never break on a probe hiccup — fail closed
        console.print("[bold]IDENTITY:[/] [dim]IDENTITY_UNVERIFIED[/]")


def _launchctl(args: list[str], *, check: bool) -> tuple[bool, str]:
    """Run ``launchctl`` once; return (ok, stderr-tail). Never raises.

    Module-level seam so ``install`` tests can stub launchctl without shelling
    out. ``check=False`` is used for the pre-emptive bootout (a not-loaded label
    returning non-zero is expected and ignored).
    """
    try:
        proc = subprocess.run(
            [_LAUNCHCTL_BIN, *args],
            capture_output=True,
            text=True,
            timeout=_LAUNCHCTL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (False, str(exc)[:160])
    ok = proc.returncode == 0 or not check
    return (ok, proc.stderr.strip()[:160])


def _bootstrap_sentinel(plist_path: Path) -> tuple[bool, str]:
    """Best-effort (re)load the sentinel LaunchAgent. Returns (loaded, detail).

    Idempotent: bootout any prior instance (failure ignored) then bootstrap the
    fresh plist into the per-user GUI domain, mirroring ``agent restart``.
    """
    target = f"gui/{os.getuid()}"
    label = link.SENTINEL_LABEL
    _launchctl(["bootout", f"{target}/{label}"], check=False)
    ok, detail = _launchctl(["bootstrap", target, str(plist_path)], check=True)
    if ok:
        return (True, f"bootstrapped {label}")
    return (False, detail or "launchctl bootstrap failed")


@link_app.command(
    "install",
    help="Install the Wi-Fi stability sentinel (script + LaunchAgent) on this node.",
)
def link_install() -> None:
    """Write the sentinel sampler + LaunchAgent and best-effort load it.

    Idempotent — re-running overwrites to the same end state. File writes are the
    real contract here; the ``launchctl`` load is best-effort and never aborts the
    command (status prints what actually happened).
    """
    script_path = link.sentinel_script_path()
    plist_path = link.sentinel_plist_path()
    err_path = link.default_err_path()
    log_path = link.default_log_path()

    try:
        script_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.parent.mkdir(parents=True, exist_ok=True)

        script_path.write_text(link.SENTINEL_SCRIPT, encoding="utf-8")
        script_path.chmod(0o755)

        plist_path.write_text(
            link.render_plist(script=script_path, err_log=err_path),
            encoding="utf-8",
        )
    except OSError as exc:
        err = LocalError(
            f"failed to install sentinel files: {exc}",
            fix="check that ~/.sanctum/bin and ~/Library/LaunchAgents are writable.",
        )
        _report(err)
        raise typer.Exit(code=int(err.exit_code)) from exc

    console.print(f"[green]✓[/] wrote sampler   {escape(str(script_path))} [dim](0755)[/]")
    console.print(f"[green]✓[/] wrote LaunchAgent {escape(str(plist_path))}")

    loaded, detail = _bootstrap_sentinel(plist_path)
    if loaded:
        console.print(
            f"[green]✓[/] {escape(detail)} "
            f"[dim](samples every {link.SENTINEL_INTERVAL_S}s → {log_path})[/]"
        )
    else:
        console.print(
            f"[yellow]![/] sentinel files installed but launchctl load was not "
            f"confirmed: {escape(detail)}"
        )
        console.print(
            f"  [dim]load it manually: launchctl bootstrap gui/$(id -u) "
            f"{escape(str(plist_path))}[/]"
        )
    console.print(
        f"[dim]Run [bold]sanctum link status[/] once samples accumulate "
        f"(~{link.SENTINEL_INTERVAL_S}s cadence).[/]"
    )


def _render_mac_audit(probe: link.WifiProbe, audit: link.MacAudit) -> None:
    """Print the MAC-stability hardening report — the headline ✓/⚠ check.

    honest-verify: the verdict is derived from a REAL probe read. When the live
    MAC could not be read at all (both empty), we do NOT claim stable — we print a
    ⚠ that says we could not verify, because a false ✓ here is exactly the silent
    regression this tool exists to catch.
    """
    if not probe.current_mac or not probe.hardware_mac:
        console.print(
            "[yellow]⚠[/] MAC stability: [yellow]UNVERIFIED[/] — could not read the "
            f"Wi-Fi MAC on [bold]{escape(probe.iface)}[/]"
        )
        console.print("  [dim]Is this node on Wi-Fi? Connect to the network, then re-run.[/]")
        return
    if audit.randomized:
        console.print(
            f"[red]⚠[/] MAC stability: [red]RANDOMIZED[/] on [bold]{escape(probe.iface)}[/]"
        )
        console.print(
            f"  live MAC [red]{escape(audit.current)}[/] ≠ hardware MAC "
            f"[bold]{escape(audit.hardware)}[/] → Private Wi-Fi Address is ON"
        )
        console.print(f"  [dim]risk:[/] {escape(audit.risk)}")
    else:
        console.print(
            f"[green]✓[/] MAC stability: [green]STABLE[/] on [bold]{escape(probe.iface)}[/]"
        )
        console.print(
            f"  live MAC = hardware MAC [bold]{escape(audit.hardware)}[/] → "
            "Private Wi-Fi Address is Off"
        )
    console.print(f"  → {escape(audit.remedy)}")


def _write_profile(probe: link.WifiProbe, profile_out: Path, *, enc: str = "WPA3") -> None:
    """Render the stability profile to ``profile_out`` (0644) and guide install.

    Never touches the radio: it GENERATES the .mobileconfig and tells the operator
    how to install it (open the file / approve in System Settings). Modern macOS
    requires user approval for a configuration profile, so we deliberately do NOT
    shell out to ``profiles install``.

    ``enc`` is the EncryptionType the managed payload must declare so macOS does not
    create a credential-less duplicate network that disrupts the association. It
    defaults to the renderer's ``WPA3`` (unchanged for the legacy P2 audit path);
    the identity flow passes the DETECTED value (``link._enc_from_security``).
    """
    if not probe.ssid:
        err = LocalError(
            "cannot render a MAC-stability profile: this node is not associated "
            "to a Wi-Fi network (no SSID).",
            fix="connect the node to its Wi-Fi network, then re-run with --apply.",
        )
        _report(err)
        raise typer.Exit(code=int(err.exit_code))
    if not probe.hardware_mac:
        err = LocalError(
            "cannot render a MAC-stability profile: could not read this node's hardware MAC.",
            fix="confirm the node has a Wi-Fi interface, then re-run.",
        )
        _report(err)
        raise typer.Exit(code=int(err.exit_code))

    profile_xml = link.render_mac_stability_profile(
        probe.ssid, probe.hardware_mac, encryption_type=enc
    )
    try:
        profile_out.parent.mkdir(parents=True, exist_ok=True)
        profile_out.write_text(profile_xml, encoding="utf-8")
        profile_out.chmod(0o644)
    except OSError as exc:
        err = LocalError(
            f"failed to write profile {profile_out}: {exc}",
            fix="check the path + permissions, or pass --profile-out <file>.",
        )
        _report(err)
        raise typer.Exit(code=int(err.exit_code)) from exc

    console.print(f"[green]✓[/] wrote stability profile {escape(str(profile_out))} [dim](0644)[/]")
    console.print(
        f"  [dim]scoped to SSID[/] [bold]{escape(probe.ssid)}[/] "
        f"[dim]· pins MAC[/] [bold]{escape(probe.hardware_mac)}[/]"
    )
    console.print(
        "\n[bold]Recommended (zero-risk):[/] System Settings ▸ Wi-Fi ▸ "
        f"[bold]{escape(probe.ssid)}[/] ▸ Details… ▸ [bold]Private Wi-Fi Address ▸ "
        "Off[/]. This is what fixed the reference node and it can't drop your link."
    )
    console.print(
        "\n[bold]Durable enforce (advanced):[/] install the profile so macOS can't "
        "silently flip it back to Rotating:"
    )
    console.print(f"  1. [bold]open {escape(str(profile_out))}[/]")
    console.print(
        "  2. System Settings ▸ Privacy & Security ▸ Profiles → approve "
        "[bold]Wi-Fi MAC Stability[/]"
    )
    console.print(
        "  3. Confirm Wi-Fi ▸ [your network] ▸ Details… shows [bold]Private Wi-Fi Address: Off[/]."
    )
    console.print(
        "[yellow]⚠ on a sole-link node:[/] this is a managed Wi-Fi payload — macOS "
        "may re-prompt for the password and briefly re-associate on approval. Do it "
        "[bold]attended[/] and confirm the connection survives before trusting it."
    )
    console.print("[dim]The tool generates + guides + verifies; it never toggles the radio.[/]")


def _node_signals() -> link.NodeSignals:
    """Best-effort, router-agnostic host signals for ``link.classify_node``.

    A thin impure boundary (tests patch it wholesale): reads THIS node's uptime,
    IP-config method / reserved-or-static, portability, and the count of distinct
    SSIDs the stability sentinel has seen. Every system read goes through the module
    ``link._real_run`` seam so tests never touch a real radio. Fail-open to the most
    CONSERVATIVE (privacy-first) value on any read miss — an unreadable signal must
    never up-classify a node to SERVER (the only class that auto-enrolls). So a miss
    yields short-uptime / DHCP / not-reserved / portable / more-SSIDs, all of which
    push ``classify_node`` toward ROAMER/UNKNOWN, never SERVER.
    """
    run = link._real_run

    # Uptime (days) from `sysctl -n kern.boottime` → "{ sec = <epoch>, ... }".
    uptime_days = 0.0
    m = re.search(r"sec\s*=\s*(\d+)", run(["sysctl", "-n", "kern.boottime"]))
    if m:
        with contextlib.suppress(ValueError, OverflowError):
            uptime_days = max(0.0, (time.time() - float(m.group(1))) / 86400.0)

    # IP-config method + reserved/static from the live Wi-Fi interface summary.
    ip_config_method = ""
    ip_reserved_or_static = False
    iface = link._parse_wifi_iface(run(["networksetup", "-listallhardwareports"]))
    if iface:
        summary = run(["ipconfig", "getsummary", iface])
        cm = re.search(r"ConfigMethod\s*:\s*(\w+)", summary)
        if cm:
            ip_config_method = cm.group(1)
        # `networksetup -getinfo "Wi-Fi"` names Manual configs "Manually" and a
        # DHCP-with-manual-address as "DHCP with manual address" — both mean the
        # node's address is pinned (a reservation-equivalent, static identity).
        getinfo = run(["networksetup", "-getinfo", "Wi-Fi"])
        ip_reserved_or_static = (
            ip_config_method.lower() == "manual"
            or "manually" in getinfo.lower()
            or "manual address" in getinfo.lower()
        )

    # Portability: a laptop (model contains "Book") is portable; a desktop/server
    # (Mac mini / Studio / Pro / iMac) is not. Unknown model → portable
    # (conservative: never auto-enroll a node we cannot prove is fixed).
    model = run(["sysctl", "-n", "hw.model"]).strip()
    is_portable = ("book" in model.lower()) or (model == "")

    # Distinct SSIDs the sentinel has logged (a node that hops many networks is a
    # roamer, not fixed infra). Missing log → 1 (single/current network), the
    # neutral value; it never fabricates roaming that would down-classify away from
    # a SERVER on a read miss.
    distinct_ssids = 1
    with contextlib.suppress(OSError):
        text = link.default_log_path().read_text(encoding="utf-8")
        seen = {mm.group(1) for mm in re.finditer(r"\bssid=(\S+)", text)}
        if seen:
            distinct_ssids = len(seen)

    return link.NodeSignals(
        uptime_days=uptime_days,
        ip_config_method=ip_config_method,
        ip_is_reserved_or_static=ip_reserved_or_static,
        distinct_ssids_seen=distinct_ssids,
        is_portable=is_portable,
    )


def _render_verify(probe: link.IdentityProbe) -> None:
    """HONEST-VERIFY: print ✓ ONLY when the node is truly pinned + router-trusted.

    A ✓ requires BOTH ``current_mac == hardware_mac`` (on the stable hardware MAC)
    AND ``router_arp_verified is True`` (the router actually recognizes it). Anything
    else — a still-rotating MAC, an unverified/timed-out ARP, or an unreadable probe
    — prints ✗ with the concrete current state, never a hopeful green check.
    """
    on_hw = bool(probe.hardware_mac) and probe.current_mac.lower() == probe.hardware_mac.lower()
    arp_ok = probe.router_arp_verified is True
    if on_hw and arp_ok:
        console.print(
            f"[green]✓[/] identity verified — on hardware MAC "
            f"[bold]{escape(probe.hardware_mac)}[/] and router-trusted "
            f"[dim](RouterARPVerified=TRUE)[/] on [bold]{escape(probe.iface)}[/]"
        )
        return
    console.print("[red]✗[/] not verified — the node is [red]not[/] confirmed stable + trusted:")
    mac_state = (
        f"on hardware MAC {probe.hardware_mac}"
        if on_hw
        else f"rotating MAC {probe.current_mac or '<unread>'} ≠ hardware "
        f"{probe.hardware_mac or '<unread>'}"
    )
    console.print(f"  [dim]MAC:[/] {escape(mac_state)}")
    console.print(f"  [dim]RouterARPVerified:[/] {escape(str(probe.router_arp_verified))}")
    console.print(
        "  → Re-run [bold]sanctum link optimize --apply[/] (approve the profile / set "
        "Private Wi-Fi Address ▸ Off), then re-verify."
    )


@link_app.command(
    "optimize",
    help="Classify the node + audit Wi-Fi MAC stability (and, with --apply, enforce it).",
)
def link_optimize(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Render an enforcement profile (read-only audit without it).",
        ),
    ] = False,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify",
            help="Re-probe and honestly report whether the node is stable + "
            "router-trusted (✓ only when both hold).",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Enroll even a ROAMER/UNKNOWN node with --apply (opt in explicitly).",
        ),
    ] = False,
    profile_out: Annotated[
        Path | None,
        typer.Option(
            "--profile-out",
            help="Where --apply writes the .mobileconfig "
            "(default: ~/.sanctum/wifi-mac-stability.mobileconfig).",
        ),
    ] = None,
) -> None:
    """Classify the node + audit Wi-Fi MAC stability; with ``--apply`` enforce it.

    Reads the live Wi-Fi identity (``link.probe_identity``) + this node's host
    signals (``_node_signals``) → the IDENTITY verdict (``diagnose_identity``) and
    node class (``classify_node``), and prints both. Then:

    * ``--verify`` re-probes and prints an HONEST ✓/✗ — ✓ only when the node is on
      its hardware MAC AND the router has verified it (never from "the step ran").
    * ``--apply`` enrolls (renders the ``.mobileconfig``, carrying the DETECTED
      encryption) only for a fixed-infra SERVER on an at-risk verdict, OR with
      ``--force``. A ROAMER/UNKNOWN without ``--force`` gets a one-line opt-in nudge
      and NOTHING is written (privacy-first / per-SSID / home-only).
    * default is a read-only audit.

    It NEVER mutates the live Wi-Fi association — the node's only link — it
    generates, guides, and verifies.
    """
    # --verify short-circuits: a fresh honest re-probe, no classify/audit noise.
    if verify:
        _render_verify(link.probe_identity())
        return

    diag = link.diagnose_identity(link.probe_identity())
    node = link.classify_node(_node_signals())

    console.print("[bold]Wi-Fi link hardening — identity + MAC stability[/]")
    _render_identity(diag)
    console.print(
        f"[bold]NODE CLASS:[/] [bold]{escape(node.klass)}[/] [dim]({escape(node.reason)})[/]"
    )

    # Legacy MAC-stability audit is preserved as the headline hardening check
    # (the P2 behavior the existing audit tests assert on), rendered beneath the
    # identity verdict from the same real probe.
    wifi = link.probe_wifi()
    _render_mac_audit(wifi, link.analyze_mac(wifi.current_mac, wifi.hardware_mac))

    if not apply:
        if diag.verdict in ("IDENTITY_QUARANTINED", "IDENTITY_ROTATING"):
            console.print(
                "\n[dim]Re-run with [bold]--apply[/] to render an enforcement profile "
                "that pins this node to its stable MAC.[/]"
            )
        return

    # --apply gate: only a fixed-infra SERVER on an at-risk verdict auto-enrolls;
    # a ROAMER/UNKNOWN must opt in explicitly with --force (privacy-first).
    at_risk = diag.verdict in ("IDENTITY_QUARANTINED", "IDENTITY_ROTATING")
    enroll = force or (node.klass == "SERVER" and at_risk)
    if not enroll:
        if node.klass == "SERVER" and not at_risk:
            console.print(
                f"\n[dim]This SERVER is already {escape(diag.verdict)} — no enforcement "
                "profile needed.[/]"
            )
        else:
            console.print(
                f"\n[yellow]![/] This looks like a [bold]{escape(node.klass)}[/] node — "
                "MAC stability is opt-in for roamers (privacy-first; a rotating MAC is "
                "fine for a device that moves between networks)."
            )
            console.print(
                "  → To pin it on THIS network anyway, re-run "
                "[bold]sanctum link optimize --apply --force[/]."
            )
        return

    # Enroll: render for the WifiProbe shape (_write_profile's contract), carrying the
    # DETECTED encryption from the identity probe so the managed payload matches the
    # saved network (a WPA3 default on a WPA2 net would make a non-joining duplicate).
    enc = link._enc_from_security(diag.probe.security)
    out = profile_out if profile_out is not None else link.default_profile_path()
    console.print()
    _write_profile(wifi, out, enc=enc)
