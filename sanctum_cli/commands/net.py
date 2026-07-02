from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

from sanctum_cli import config
from sanctum_cli.devices import firewalla as firewalla_provider
from sanctum_cli.devices import ha_green as ha_green_provider
from sanctum_cli.devices import intents, rails, registry, sagemcom
from sanctum_cli.devices import orbi as orbi_provider
from sanctum_cli.devices.base import Capability, Creds, DeviceError, NetContext, OpResult
from sanctum_cli.errors import ExitCode, LocalError, SanctumError
from sanctum_cli.net import detect, heal, playbooks, render, safety, speedtest, system, verify
from sanctum_cli.net.types import SpeedReport, Verdict

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sanctum_cli.devices.base import DeviceProvider
    from sanctum_cli.net.detect import HttpProbe, Runner
    from sanctum_cli.net.link import CommandRunner

console = Console()
err_console = Console(stderr=True)
net_app = typer.Typer(help="Network topology wizard and diagnostics.")


def _report(exc: SanctumError) -> None:
    """Pretty-print a SanctumError to stderr with its optional fix suggestion.

    Mirrors ``sanctum_cli.cli._report`` but is defined locally to avoid importing
    from ``cli`` (which imports this module — a circular dependency).
    """
    err_console.print(f"[bold red]error:[/] {exc.message}")
    if exc.fix:
        err_console.print(f"[dim]fix:[/] {exc.fix}")


_SNAP_ROOT = Path.home() / ".sanctum" / "net-optimize"

# Per-kind default Keychain (service, account) for a device's admin credential.
# Discovery-first: instance.yaml ``devices.<kind>.keychain.{service,account}``
# overrides these, but a fresh box with no devices block resolves the default so
# the CLI still has a tuple to read the password under. Firewalla is deliberately
# ABSENT — it authenticates with a bearer token from an on-disk secret file, not
# a Keychain password, so it has no entry here (its creds path is unchanged).
_DEVICE_KEYCHAIN_DEFAULTS: dict[str, tuple[str, str]] = {
    "hub": ("bell-hub-admin", "admin"),
    "orbi": ("orbi-admin", "admin"),
}


def device_keychain_ref(
    kind: str,
    *,
    instance_lookup: Callable[..., Any] | None = None,
) -> tuple[str, str]:
    """Resolve the (service, account) Keychain tuple for ``kind``, discovery-first.

    Reads ``devices.<kind>.keychain.service`` / ``.account`` from instance.yaml
    (via :func:`config.instance_value`), falling back to the per-kind default in
    :data:`_DEVICE_KEYCHAIN_DEFAULTS`. A ``kind`` with no built-in default and
    nothing configured resolves to ``("", "")`` — NEVER another kind's tuple — so
    a misconfiguration misses loudly (the provider's Keychain read fails) instead
    of silently addressing the wrong entry. This closes the prior low finding that
    :func:`_hub_creds` hardcoded the Sagemcom tuple: the tuple now comes from
    config-or-default, brand-agnostically, for every kind that uses a Keychain
    password.

    ``instance_lookup`` is an injection seam (defaults to ``config.instance_value``)
    so a test can drive the resolution without a real instance.yaml on disk.
    """
    lookup = instance_lookup if instance_lookup is not None else config.instance_value
    default_service, default_account = _DEVICE_KEYCHAIN_DEFAULTS.get(kind, ("", ""))
    service = str(lookup(f"devices.{kind}.keychain.service", default_service))
    account = str(lookup(f"devices.{kind}.keychain.account", default_account))
    return service, account


def device_creds(kind: str, net: NetContext) -> Creds:
    """Assemble Creds for a resolved device of ``kind``, discovery-first.

    The host is the detected gateway IP; the username is the Keychain *account*
    AND the ``keychain_service`` is the Keychain *service* — BOTH resolved by
    :func:`device_keychain_ref` (instance.yaml ``devices.<kind>.keychain.*``
    override → per-kind default). The ``secret`` is left ``None`` on purpose — the
    provider re-reads the password/token from the Keychain at connect time using
    that resolved ``(service, account)`` tuple (credentials never flow through the
    CLI layer). Threading the resolved *service* through (not just the account)
    closes the prior low finding completely: a haus whose hub admin entry lives
    under a non-default service (``devices.hub.keychain.service``) now reads the
    password from THAT entry, not the brand constant. Generalizes the old per-kind
    ``_hub_creds`` / ``_orbi_creds`` so the Keychain tuple is no longer hardcoded
    per brand.
    """
    service, account = device_keychain_ref(kind)
    return Creds(
        host=net.gateway_ip or "",
        username=account,
        secret=None,
        key_path=None,
        keychain_service=service or None,
    )


def _firewalla_key_path() -> Path:
    """Resolve the Firewalla SSH key path (discovery-first).

    Reads ``firewalla.ssh_key`` from instance.yaml when set; falls back to
    ``~/.ssh/firewalla_ed25519`` for back-compat with the original layout.
    """
    configured = config.instance_value("firewalla.ssh_key", None)
    if configured:
        return Path(str(configured)).expanduser()
    return Path.home() / ".ssh" / "firewalla_ed25519"


def _build_runner() -> Runner:
    gw = detect.parse_default_gateway(system.real_runner(("route",)))
    key_path = _firewalla_key_path()
    fw_key = str(key_path) if key_path.exists() else None
    return system.make_real_runner(fw_gateway=gw, fw_key=fw_key)


def _build_http() -> HttpProbe:
    return system.real_http


def _firewalla_present() -> bool:
    try:
        socket.create_connection(("firewalla.local", 22), timeout=1).close()
        return True
    except OSError:
        return False


@net_app.command("check", help="Audit network topology (read-only, makes no changes).")
def net_check() -> None:
    runner, http = _build_runner(), _build_http()
    rep = detect.detect(runner=runner, http=http, firewalla_present=_firewalla_present())
    console.print(f"[bold]NAT topology:[/] {escape(rep.nat.value)}")
    console.print(
        f"[bold]ISP:[/] {escape(rep.isp)}   [bold]gateway:[/] {escape(rep.gateway_ip or '-')}"
    )
    console.print(escape(rep.reason))


@net_app.command("optimize", help="Guide you from double-NAT to single-NAT (opt-in, reversible).")
def net_optimize(
    assume_yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the presence prompt.")
    ] = False,
    plan_only: Annotated[
        bool, typer.Option("--plan-only", help="Print the plan; skip verify loop.")
    ] = False,
) -> None:
    runner, http = _build_runner(), _build_http()
    rep = detect.detect(runner=runner, http=http, firewalla_present=_firewalla_present())

    if not rep.applicable:
        if not rep.firewalla_present:
            console.print("[green]✓[/] No Firewalla here — nothing to optimize.")
        else:
            console.print(f"[green]✓[/] {escape(rep.reason)}")
        raise typer.Exit(code=0)

    pb = playbooks.BUILTINS.get(rep.isp, playbooks.BUILTINS["generic"])
    console.print(render.render_plan(rep, pb), markup=False)

    if plan_only:
        raise typer.Exit(code=0)

    # Capture the rollback baseline only on the real flow, before the user acts.
    safety.snapshot(rep, root=_SNAP_ROOT)
    if not assume_yes:
        console.print(
            "\n[bold]This briefly drops your internet.[/] Are you at the box, not remote?"
        )
        if not typer.confirm("Proceed?"):
            console.print("No changes made.")
            raise typer.Exit(code=0)

    console.print("\nWhen you've done the steps above, I'll verify. Press Enter to check…")
    typer.prompt("", default="", show_default=False)
    v, reason = verify.verify(runner=runner)
    if v is Verdict.VERIFIED:
        console.print(f"[green]✓ {escape(reason)}[/]")
    elif v is Verdict.APIPA_ROLLBACK:
        console.print(f"[red]✗ {escape(reason)}[/]")
        console.print("Roll back:")
        for line in pb.rollback:
            console.print(f"  ↩ {escape(line)}")
    else:
        console.print(f"[yellow]{escape(reason)}[/]")


# ─── speedtest ───────────────────────────────────────────────────────


def _probe_hops(
    runner: Runner, *, firewalla_present: bool
) -> tuple[tuple[tuple[str, int | None], ...], bool | None]:
    """Assemble (hops, on_wifi) from injectable runner probes.

    A hop is (label, link-Mbps). The local NIC link is always included; the
    Firewalla WAN/LAN ports are added when reachable. on_wifi is True/False
    from the hardware-port listing, or None when undetermined.
    """
    iface = system.parse_default_iface(runner(("route",)))
    local_mbps = system.parse_link_speed_mbps(runner(("link_speed",)))
    on_wifi: bool | None = None
    if iface:
        on_wifi = system.iface_is_wifi(iface, runner(("airport_ports",)))

    local_label = f"this machine's {iface or 'NIC'}{' (Wi-Fi)' if on_wifi else ''}"
    hops: list[tuple[str, int | None]] = [(local_label, local_mbps)]

    if firewalla_present:
        # Optional Firewalla port speeds (ethtool eth0/eth3 over the SSH seam).
        fw = runner(("fw_ports",)).strip()
        for line in fw.splitlines():
            # Expect "name\tMbps" rows; ignore anything malformed.
            parts = line.split("\t")
            if len(parts) == 2 and parts[1].strip().isdigit():
                hops.append((f"Firewalla {parts[0].strip()}", int(parts[1].strip())))

    return tuple(hops), on_wifi


def build_speed_report(runner: Runner, *, firewalla_present: bool, run_live: bool) -> SpeedReport:
    """Compose a SpeedReport from injectable probes + the pure interpreters.

    When run_live is False, no live download is requested (the ("live_test",)
    tag is never read) — output is deterministic and endpoint-free.
    """
    hops, on_wifi = _probe_hops(runner, firewalla_present=firewalla_present)
    bottleneck, ceiling = speedtest.find_bottleneck(hops=hops, on_wifi=on_wifi)

    multi = single = None
    inconclusive = False
    if run_live:
        raw = runner(("live_test",))
        multi, single, inconclusive = _parse_live(raw)

    pppoe = "pppoe" in runner(("wan_kind",)).lower()
    verdict, advice = speedtest.classify_throughput(
        multi_gbps=multi,
        single_gbps=single,
        ceiling_gbps=ceiling,
        on_wifi=on_wifi,
        bottleneck=bottleneck,
        pppoe=pppoe,
        test_inconclusive=inconclusive,
    )
    return SpeedReport(
        multi_gbps=multi,
        single_gbps=single,
        ceiling_gbps=ceiling,
        on_wifi=on_wifi,
        hops=hops,
        bottleneck=bottleneck,
        verdict=verdict,
        advice=advice,
        test_inconclusive=inconclusive,
    )


def _parse_live(raw: str) -> tuple[float | None, float | None, bool]:
    """Decode the JSON the live-test runner tag returns; tolerate junk/empty."""
    if not raw.strip():
        return (None, None, True)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return (None, None, True)
    multi = data.get("multi_gbps")
    single = data.get("single_gbps")
    inconclusive = bool(data.get("inconclusive", False))
    return (
        float(multi) if isinstance(multi, (int, float)) else None,
        float(single) if isinstance(single, (int, float)) else None,
        inconclusive,
    )


def _speed_to_dict(r: SpeedReport) -> dict[str, object]:
    return {
        "multi_gbps": r.multi_gbps,
        "single_gbps": r.single_gbps,
        "ceiling_gbps": r.ceiling_gbps,
        "on_wifi": r.on_wifi,
        "hops": [[name, mbps] for name, mbps in r.hops],
        "bottleneck": r.bottleneck,
        "verdict": r.verdict,
        "advice": list(r.advice),
        "test_inconclusive": r.test_inconclusive,
    }


def _live_runner(base: Runner, streams: int) -> Runner:
    """Wrap a runner so ("live_test",) runs the real bounded throughput probe
    and returns its result as JSON (everything else delegates to base)."""

    def runner(tag: tuple[str, ...]) -> str:
        if tag == ("live_test",):
            multi, single, inconclusive = system.live_throughput(streams)
            return json.dumps(
                {"multi_gbps": multi, "single_gbps": single, "inconclusive": inconclusive}
            )
        return base(tag)

    return runner


@net_app.command(
    "speedtest",
    help="Honest throughput doctor: shows your real ceiling, not a misleading single-stream number.",
)
def net_speedtest(
    streams: Annotated[int, typer.Option("--streams", help="Parallel download streams.")] = 8,
    no_test: Annotated[
        bool,
        typer.Option("--no-test", help="Audit only: ceiling + interpretation, no live download."),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of the report.")
    ] = False,
) -> None:
    base = _build_runner()
    present = _firewalla_present()
    run_live = not no_test
    runner = _live_runner(base, streams) if run_live else base
    report = build_speed_report(runner, firewalla_present=present, run_live=run_live)
    if json_output:
        print(json.dumps(_speed_to_dict(report), indent=2))
        return
    render.render_speed(console, report)


# ─── heal (topology-adaptive self-healing) ──────────────────────────
#
# `sanctum net heal` reads the node's live L3 posture, classifies it against the
# pure truth table (heal.diagnose_posture), and guards a heal (heal.plan_heal:
# never-strand + no-loop + fail-closed). Default is a READ-ONLY dry-run; --apply
# executes a *safe* plan behind snapshot → act → verify-back → auto-revert. The
# impure boundary is a single injected CommandRunner (argv -> stdout) so the whole
# flow is unit-testable without a live network / sudo (tests patch _build_heal_runner).

# Lines from `networksetup -getinfo "Wi-Fi"` we snapshot for a manual revert.
_GETINFO_IP_RE = re.compile(r"^IP address:\s*(\S+)", re.MULTILINE)
_GETINFO_MASK_RE = re.compile(r"^Subnet mask:\s*(\S+)", re.MULTILINE)
_GETINFO_ROUTER_RE = re.compile(r"^Router:\s*(\S+)", re.MULTILINE)

# Bounded verify-back after a heal: poll the re-probe a few times for a lease +
# reachable gateway before deciding it failed (a DHCP flip takes a moment). These
# are small so a test's stateful runner settles immediately; the daemon reuses them.
_HEAL_VERIFY_TRIES = 3
_HEAL_VERIFY_DELAY_S = 1.0


def _build_heal_runner() -> CommandRunner:
    """The impure boundary for `net heal` — a real subprocess CommandRunner.

    A module-level seam (mirrors :func:`_build_runner`) so tests inject a fake
    ``argv -> stdout`` runner and drive the entire probe/act/verify/revert flow
    with zero live ``networksetup`` / ``ipconfig`` / ``route`` / ``ping`` / sudo.
    Defaults to ``heal._real_run`` — the same never-raising subprocess seam the
    pure-core posture read uses.
    """
    from sanctum_cli.net.link import _real_run

    return _real_run


def _heal_snapshot(runner: CommandRunner) -> tuple[str, str, str] | None:
    """Snapshot the current manual IPv4 config from ``networksetup -getinfo "Wi-Fi"``.

    Returns ``(ip, mask, router)`` when all three parse, else ``None`` (we refuse
    to fire a heal we cannot cleanly revert — never-strand). This is the rollback
    baseline the auto-revert restores if the heal does not come up healthy.
    """
    info = runner(["networksetup", "-getinfo", "Wi-Fi"])
    ip = _GETINFO_IP_RE.search(info)
    mask = _GETINFO_MASK_RE.search(info)
    router = _GETINFO_ROUTER_RE.search(info)
    if ip and mask and router:
        return (ip.group(1), mask.group(1), router.group(1))
    return None


def _heal_reprobe_healthy(runner: CommandRunner) -> heal.NetPosture:
    """Bounded verify-back: re-probe until the gateway answers (or tries run out).

    Honest-verify: a heal is "healed" ONLY from a real re-probe that shows a lease
    (an IP) AND a reachable gateway. Polls a few times so a just-issued DHCP flip
    has a moment to settle; returns the last posture read either way.
    """
    posture = heal.probe_posture(run=runner)
    tries = 0
    while not (posture.ip and posture.gateway_reachable) and tries < _HEAL_VERIFY_TRIES:
        time.sleep(_HEAL_VERIFY_DELAY_S)
        posture = heal.probe_posture(run=runner)
        tries += 1
    return posture


def _render_posture(posture: heal.NetPosture, diag: heal.PostureDiagnosis) -> None:
    """Print the posture read + the verdict + the never-strand spine state."""
    reach = (
        "reachable"
        if posture.gateway_reachable
        else ("dead" if posture.gateway_reachable is False else "unknown")
    )
    console.print(f"[bold]VERDICT:[/] {escape(diag.verdict)}")
    console.print(f"  {escape(diag.detail)}")
    console.print(
        f"  [dim]iface[/] {escape(posture.iface or '-')}  "
        f"[dim]config[/] {escape(posture.config_method or '-')}  "
        f"[dim]ip[/] {escape(posture.ip or '-')}  "
        f"[dim]gateway[/] {escape(posture.gateway or '-')} ({reach})"
    )
    spine = []
    spine.append("tailnet ✓" if posture.on_tailnet else "tailnet ✗")
    spine.append("TB5 ✓" if posture.tb5_up else "TB5 ✗")
    spine_ok = posture.on_tailnet or posture.tb5_up
    style = "green" if spine_ok else "red"
    console.print(f"  [bold]spine:[/] [{style}]{escape(' · '.join(spine))}[/]")


# ─── self-healing daemon install (the one sudo step) ─────────────────
#
# `sanctum net heal --install` writes the wrapper (0755) + a system LaunchDaemon
# and best-effort bootstraps it. Unlike the wifi-stability sentinel (a per-user
# LaunchAgent), this is a *system* daemon (it must setdhcp/renew as root), so the
# bootstrap targets the `system` domain — the single sudo action. `launchctl` is
# behind a module-level seam so tests stub it without shelling out.

_HEAL_LAUNCHCTL_BIN = "/bin/launchctl"
_HEAL_LAUNCHCTL_TIMEOUT_S = 10


def _heal_launchctl(args: list[str], *, check: bool) -> tuple[bool, str]:
    """Run ``launchctl`` once; return (ok, stderr-tail). Never raises.

    Module-level seam (mirrors ``link._launchctl``) so ``--install`` tests stub
    launchctl without shelling out. ``check=False`` is used for the pre-emptive
    bootout (a not-loaded label returning non-zero is expected and ignored).
    """
    try:
        proc = subprocess.run(
            [_HEAL_LAUNCHCTL_BIN, *args],
            capture_output=True,
            text=True,
            timeout=_HEAL_LAUNCHCTL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (False, str(exc)[:160])
    ok = proc.returncode == 0 or not check
    return (ok, proc.stderr.strip()[:160])


def _bootstrap_heal_daemon(plist_path: Path) -> tuple[bool, str]:
    """Best-effort (re)load the net-heal LaunchDaemon. Returns (loaded, detail).

    Idempotent: bootout any prior instance (failure ignored) then bootstrap the
    fresh plist into the SYSTEM domain (this is the sudo-gated action — a system
    daemon so it can mutate the interface).
    """
    label = heal.HEAL_DAEMON_LABEL
    _heal_launchctl(["bootout", f"system/{label}"], check=False)
    ok, detail = _heal_launchctl(["bootstrap", "system", str(plist_path)], check=True)
    if ok:
        return (True, f"bootstrapped {label}")
    return (False, detail or "launchctl bootstrap failed")


def _install_heal_daemon() -> None:
    """Write the net-heal wrapper (0755) + LaunchDaemon plist and load it (root).

    The one sudo step. A non-root shell never partially installs a system daemon:
    it prints the exact ``sudo sanctum net heal --install`` command and returns.
    File writes are the real contract; the ``launchctl`` load is best-effort.
    """
    wrapper_path = heal.heal_wrapper_path()
    plist_path = heal.heal_plist_path()
    err_path = heal.heal_err_path()

    if os.getuid() != 0:
        console.print(
            "\n[yellow]![/] --install writes a system LaunchDaemon to "
            f"{escape(str(plist_path))} — that needs root."
        )
        console.print(
            "  → run it with sudo: [bold]sudo sanctum net heal --install[/]"
        )
        return

    try:
        wrapper_path.parent.mkdir(parents=True, exist_ok=True)
        err_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.parent.mkdir(parents=True, exist_ok=True)

        wrapper_path.write_text(heal.HEAL_WRAPPER, encoding="utf-8")
        wrapper_path.chmod(0o755)

        plist_path.write_text(
            heal.render_heal_plist(wrapper=wrapper_path, err_log=err_path),
            encoding="utf-8",
        )
    except OSError as exc:
        err = LocalError(
            f"failed to install net-heal daemon files: {exc}",
            fix="check that /Library/Application Support/sanctum and "
            "/Library/LaunchDaemons are writable (run as root).",
        )
        _report(err)
        raise typer.Exit(code=int(err.exit_code)) from exc

    console.print(f"[green]✓[/] wrote wrapper     {escape(str(wrapper_path))} [dim](0755)[/]")
    console.print(f"[green]✓[/] wrote LaunchDaemon {escape(str(plist_path))}")

    loaded, detail = _bootstrap_heal_daemon(plist_path)
    if loaded:
        console.print(
            f"[green]✓[/] {escape(detail)} "
            f"[dim](heals every {heal.HEAL_INTERVAL_S}s; kill-switch: "
            f"touch {escape(str(heal._HEAL_DISABLED_SENTINEL))})[/]"
        )
    else:
        console.print(
            f"[yellow]![/] daemon files installed but launchctl load was not "
            f"confirmed: {escape(detail)}"
        )
        console.print(
            f"  [dim]load it manually: sudo launchctl bootstrap system "
            f"{escape(str(plist_path))}[/]"
        )


@net_app.command(
    "heal",
    help="Diagnose + (with --apply) self-heal the node's network posture. Dry-run by default.",
)
def net_heal(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply", help="Execute a safe heal (snapshot→act→verify→auto-revert; needs root)."
        ),
    ] = False,
    install: Annotated[
        bool,
        typer.Option(
            "--install",
            help="Install the self-healing LaunchDaemon (system, root — the one sudo step).",
        ),
    ] = False,
) -> None:
    """Read L3 posture → diagnose → guarded heal (never-strand / no-loop / fail-closed).

    Default is a READ-ONLY dry-run: print the posture, the verdict, the *would-do*
    action, and the never-strand spine state — no mutation. ``--apply`` executes a
    *safe* plan (root-only; the daemon runs as root) behind snapshot → act →
    verify-back → auto-revert: it snapshots the current manual IPv4 config, fires
    the action (``flip_dhcp`` / ``dhcp_renew``), re-probes for a lease + reachable
    gateway, and on failure reverts to the snapshot and stops+alerts. A ✓ is
    printed ONLY from the real re-probe (honest-verify); a risky / UNVERIFIED /
    spine-down verdict never mutates (stays out of the NAT domain, fail-closed).
    ``--install`` writes the ``com.sanctum.net-heal`` LaunchDaemon (the one sudo
    step) so the node self-heals on a ~120s cadence behind the same doctrine
    (kill-switch, no-loop attempts cap, spine check).
    """
    if install:
        _install_heal_daemon()
        return

    runner = _build_heal_runner()
    posture = heal.probe_posture(run=runner)
    diag = heal.diagnose_posture(posture, overlap=heal.overlap_for(posture))
    _render_posture(posture, diag)

    plan = heal.plan_heal(
        diag,
        attempts=0,
        tailnet_ok=posture.on_tailnet,
        tb5_ok=posture.tb5_up,
    )

    if not apply:
        # Dry-run: describe the would-do, mutate nothing.
        if plan.execute and plan.action is not None:
            console.print(
                f"\n[dim]would-do:[/] [bold]{escape(plan.action.kind)}[/] "
                f"[dim]({escape(plan.action.detail)})[/]"
            )
            console.print(
                "[dim]dry-run: no changes made. Re-run with --apply (as root) to heal.[/]"
            )
        else:
            console.print(f"\n[yellow]{escape(plan.reason)}[/]")
            if diag.remedy:
                console.print(f"  → {escape(diag.remedy)}")
        return

    # --apply: a stop-and-alert plan never mutates — surface the reason + the
    # one-line manual fix and exit clean (fail-closed / stays-out-of-NAT).
    if not plan.execute or plan.action is None:
        console.print(f"\n[yellow]stop + alert:[/] {escape(plan.reason)}")
        if diag.remedy:
            console.print(f"  → {escape(diag.remedy)}")
        return

    # Mutations need root — the daemon runs as root; a non-root shell gets the hint.
    if os.getuid() != 0:
        console.print(
            "\n[yellow]![/] --apply needs root to change the interface "
            "(the net-heal daemon runs as root)."
        )
        console.print(
            "  → run it with sudo: [bold]sudo sanctum net heal --apply[/], "
            "or install the daemon: [bold]sanctum net heal --install[/]"
        )
        return

    # Snapshot the current config so a failed heal can be cleanly reverted. No
    # revertable baseline → refuse (never-strand: don't fire what we can't undo).
    snap = _heal_snapshot(runner)
    if snap is None:
        console.print(
            "\n[yellow]stop + alert:[/] could not snapshot the current IPv4 config — "
            "refusing to heal without a revert baseline (never-strand)."
        )
        return

    argv = heal.heal_action_argv(plan.action, posture.iface)
    console.print(f"\n[dim]healing:[/] {escape(' '.join(argv))}")
    runner(argv)

    healed = _heal_reprobe_healthy(runner)
    if healed.ip and healed.gateway_reachable:
        # Honest-verify: ✓ only from the real re-probe (lease + reachable gateway).
        console.print(
            f"[green]✓ healed[/] — {escape(plan.action.kind)} took: "
            f"ip [bold]{escape(healed.ip)}[/], gateway "
            f"[bold]{escape(healed.gateway or '-')}[/] reachable."
        )
        return

    # Heal did not come up healthy → revert to the snapshot and stop + alert.
    ip, mask, router = snap
    console.print(
        "[red]✗ not healed[/] — re-probe still unhealthy; reverting to the snapshot."
    )
    runner(["networksetup", "-setmanual", "Wi-Fi", ip, mask, router])
    console.print(
        f"  [yellow]↩ reverted[/] Wi-Fi to manual {escape(ip)} / {escape(mask)} / "
        f"{escape(router)} and stopped (stop + alert — no loop)."
    )


# ─── hub (network-gear provider surface) ─────────────────────────────
#
# Importing sanctum_cli.devices.sagemcom self-registers SagemcomHubProvider under
# kind="hub" (see the module footer), so registry.resolve("hub", net) can find it.
# Referenced here so the import is never pruned as unused.
_ = sagemcom

hub_app = typer.Typer(help="Drive the network gateway (hub) through the device-provider rails.")
net_app.add_typer(hub_app, name="hub")

# DeviceInfo read paths the status summary surfaces. These are the generic
# TR-069 DeviceInfo leaves; a provider that does not expose one returns None and
# the summary prints a dash (the brand-agnostic-via-None contract). The
# bridge-mode read path is NOT hardcoded here — it is resolved from the
# provider's capability_op(BRIDGE_MODE), so a non-TR-069 hub reports bridge mode
# via its own leaf.
_HUB_MODEL_PATH = "Device/DeviceInfo/ModelName"
_HUB_FIRMWARE_PATH = "Device/DeviceInfo/SoftwareVersion"

# The hub admin Keychain (service, account) is NO LONGER hardcoded here to the
# Sagemcom tuple — it is resolved discovery-first by :func:`device_keychain_ref`
# (instance.yaml ``devices.hub.keychain.*`` → per-kind default), so a non-Bell hub
# whose admin account is not "admin" can still be addressed. The provider re-reads
# the password from the Keychain itself; the CLI only supplies the account as the
# Creds username.


def _hub_netcontext() -> NetContext:
    """Build the NetContext the registry fingerprints the hub over.

    Parses the default gateway from the real ``route`` probe (read-only) and
    threads the real runner so a provider's ``detect()`` can probe without owning
    its own subprocess plumbing. Monkeypatched in tests so no shell-out occurs.
    """
    gw = detect.parse_default_gateway(system.real_runner(("route",)))
    return NetContext(gateway_ip=gw, runner=system.real_runner)


def _hub_creds(net: NetContext) -> Creds:
    """Assemble Creds for the resolved hub via the generalized resolver.

    Delegates to :func:`device_creds` so the hub admin account is read from
    instance.yaml (``devices.hub.keychain.account``) or the per-kind default,
    NOT a constant pinned to the Sagemcom module (the prior low finding). The
    secret stays ``None`` — the provider reads the password from the Keychain at
    connect time (credentials never flow through the CLI layer).
    """
    return device_creds("hub", net)


def _resolve_hub() -> DeviceProvider:
    """Resolve + connect the hub provider for the local network.

    Detection is read-only; ``connect`` opens the authenticated session. Any
    transport/auth failure raises a ``SanctumError`` (DeviceError) which the
    command wrappers map to a clean exit code.

    NOTE: the caller MUST release the provider via ``disconnect()`` — a connected
    provider owns a transport (the Sagemcom provider holds a persistent asyncio
    loop + a loop-bound aiohttp session). Use :func:`_connected_hub` (a
    context manager) instead of calling this directly so teardown is guaranteed.

    An optional instance.yaml ``devices.hub.brand`` pins the provider explicitly,
    bypassing ``detect()``. This is the escape hatch for a hub whose read-only
    probe is not implemented: without it, resolution depends on a working
    ``detect()`` and a stubbed probe degrades the real hub to the read-only
    fallback (status prints dashes; set/single-nat refuse). Pin ``sagemcom`` to
    drive a Bell Home Hub end-to-end today.
    """
    net = _hub_netcontext()
    pinned = config.instance_value("devices.hub.brand", None)
    brand_pin = str(pinned) if pinned is not None else None
    provider = registry.resolve("hub", net, brand_pin=brand_pin)
    provider.connect(_hub_creds(net))
    return provider


@contextmanager
def _connected_hub() -> Iterator[DeviceProvider]:
    """Yield a connected hub provider, guaranteeing ``disconnect()`` on exit.

    Closes the lifecycle gap behind every ``sanctum net hub ...`` command: the
    Sagemcom provider opens a persistent event loop + a loop-bound aiohttp
    session at ``connect`` that only ``disconnect()`` releases cleanly. Without
    this each invocation leaked the loop/session to GC (a ResourceWarning, no
    clean SAH logout). ``disconnect`` is part of the ``DeviceProvider`` Protocol
    and is idempotent + safe even if ``connect`` failed, so the ``finally`` can
    always call it.
    """
    provider = _resolve_hub()
    try:
        yield provider
    finally:
        provider.disconnect()


@hub_app.command("status", help="Read-only hub summary: model, firmware, bridge-mode.")
def hub_status() -> None:
    try:
        with _connected_hub() as provider:
            model = provider.get(_HUB_MODEL_PATH)
            firmware = provider.get(_HUB_FIRMWARE_PATH)
            # Resolve the bridge-mode leaf from the provider's own vocabulary so
            # the CLI never hardcodes a Bell XPath; None → the provider has no
            # bridge-mode op and we print a dash.
            bridge_op = provider.capability_op(Capability.BRIDGE_MODE)
            bridge = provider.get(bridge_op.path) if bridge_op is not None else None
            brand, kind = provider.brand, provider.kind
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    console.print(f"[bold]hub:[/] {escape(brand)} ({escape(kind)})")
    console.print(f"[bold]model:[/] {escape(model or '-')}")
    console.print(f"[bold]firmware:[/] {escape(firmware or '-')}")
    console.print(f"[bold]bridge-mode:[/] {escape(bridge or '-')}")


@hub_app.command("get", help="Read one hub leaf value by its provider path.")
def hub_get(
    path: Annotated[str, typer.Argument(help="Provider-specific path (e.g. an XPath).")],
) -> None:
    try:
        with _connected_hub() as provider:
            value = provider.get(path)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    console.print(escape(value if value is not None else "-"))


@hub_app.command("set", help="Write one hub leaf value, guarded by snapshot→verify→rollback.")
def hub_set(
    path: Annotated[str, typer.Argument(help="Provider-specific path (e.g. an XPath).")],
    value: Annotated[str, typer.Argument(help="New value to write.")],
    force: Annotated[bool, typer.Option("--force", help="Skip the confirmation prompt.")] = False,
    no_rollback: Annotated[
        bool,
        typer.Option("--no-rollback", help="Leave a failed change in place for inspection."),
    ] = False,
) -> None:
    try:
        with _connected_hub() as provider:

            def change(pv: DeviceProvider) -> OpResult:
                # path/value are passed straight through to the provider, which owns
                # the SAH-boundary encoding (the hostile-input contract is enforced
                # at the provider's transport seam, not re-encoded here). Return the
                # OpResult so the rails catch a return-convention provider's ok=False
                # (Sagemcom raises, but a generic/return-convention hub does not) —
                # no call site silently discards a refused write.
                return pv.set(path, value)

            result = rails.guarded_apply(
                provider,
                change,
                # No real-world verify wired for an arbitrary leaf set — a single
                # write is committed if it does not raise. Intents (single-nat)
                # carry their own real-site verify.
                verify_fn=lambda: True,
                confirm=lambda plan: typer.confirm(f"{plan}\nProceed?"),
                force=force,
                rollback=not no_rollback,
            )
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    if result.ok:
        console.print(f"[green]✓[/] {escape(result.detail)}")
    else:
        console.print(f"[yellow]{escape(result.detail)}[/]")
        raise typer.Exit(code=1)


@hub_app.command(
    "single-nat",
    help="Put the hub in bridge mode (single NAT). Dry-run by default; pass --apply to fire.",
)
def hub_single_nat(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Actually fire the cutover (attended-only; drops internet)."),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Skip the confirmation prompt (with --apply).")
    ] = False,
) -> None:
    try:
        with _connected_hub() as provider:
            # Verify must run over the Firewalla-key-bound runner (the same one
            # net_optimize/net_check use), NOT the bare system.real_runner. Only
            # _build_runner() → make_real_runner() resolves the ("fw_wan_ip",) /
            # ("fw_wan_mac",) tags over the SSH seam; the bare real_runner returns
            # "" for them (system.py:18), which would make verify.verify see a
            # None WAN IP — disabling the APIPA/DHCP-fail auto-rollback trigger and
            # biasing classify_nat toward NOT-VERIFIED on a successful cutover.
            result = intents.single_nat(
                provider,
                force=force,
                apply=apply,
                runner=_build_runner() if apply else None,
                confirm=lambda plan: typer.confirm(f"{plan}\nProceed?"),
            )
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    for line in result.plan:
        console.print(escape(line))
    if not result.applied:
        console.print("\n[dim]dry-run: no changes made. Re-run with --apply to fire.[/]")
        return
    assert result.result is not None  # apply path always carries an OpResult
    if result.result.ok:
        console.print(f"\n[green]✓[/] {escape(result.result.detail)}")
    else:
        console.print(f"\n[yellow]{escape(result.result.detail)}[/]")
        raise typer.Exit(code=1)


# ─── firewalla (network-gear provider surface) ───────────────────────
#
# Importing sanctum_cli.devices.firewalla self-registers FirewallaProvider under
# kind="firewalla" (see the module footer), so registry.resolve("firewalla", net)
# can find it. Referenced here so the import is never pruned as unused.
_firewalla_registered = firewalla_provider

# Importing sanctum_cli.devices.orbi self-registers OrbiProvider under kind="orbi"
# (see the module footer), so registry.resolve("orbi", net) can find it — and a
# devices.orbi.brand pin resolves to the real provider instead of the read-only
# GenericReadOnlyProvider fallback. Registration is import-triggered and there is
# NO dynamic provider auto-discovery, so this explicit import is the ONLY thing
# that wires the Orbi provider into a real install. Referenced here so the import
# is never pruned as unused; the `sanctum net orbi` sub-app below is what surfaces
# the provider's read + guest-wifi commands.
_orbi_registered = orbi_provider

firewalla_app = typer.Typer(
    help="Drive the Firewalla box (firewall) through the device-provider rails."
)
net_app.add_typer(firewalla_app, name="firewalla")

# Bridge read paths the firewalla surface surfaces. These mirror the provider's
# own endpoint vocabulary (sanctum_cli.devices.firewalla); a provider that has no
# body for one returns None and the command prints a dash.
_FW_INFO_PATH = "/info"
_FW_POLICIES_PATH = "/policies"
_FW_FLOWS_PATH = "/flows"

# The Firewalla bridge admin account. The provider self-resolves its bearer token
# from the env / on-disk secret at connect time (credentials never flow through
# the CLI layer), so the secret here stays None.
_FW_USERNAME = "pi"


def _firewalla_netcontext() -> NetContext:
    """Build the NetContext the registry fingerprints the Firewalla over.

    Parses the default gateway from the real ``route`` probe (read-only) and
    threads the real runner so a provider's ``detect()`` can probe without owning
    its own subprocess plumbing. Monkeypatched in tests so no shell-out occurs.
    """
    gw = detect.parse_default_gateway(system.real_runner(("route",)))
    return NetContext(gateway_ip=gw, runner=system.real_runner)


def _firewalla_creds(net: NetContext) -> Creds:
    """Assemble Creds for the resolved Firewalla.

    The host is the detected gateway IP; the username is the box admin account.
    The secret is left ``None`` on purpose — the provider reads the bearer token
    from the env / on-disk secret at connect time (credentials never flow through
    the CLI layer). The durable SSH key is resolved by the provider itself.
    """
    return Creds(
        host=net.gateway_ip or "",
        username=_FW_USERNAME,
        secret=None,
        key_path=None,
    )


def _resolve_firewalla() -> DeviceProvider:
    """Resolve + connect the Firewalla provider for the local network.

    Detection is read-only; ``connect`` resolves the bridge token + SSH key. Any
    transport/auth failure raises a ``SanctumError`` (DeviceError) which the
    command wrappers map to a clean exit code.

    NOTE: the caller MUST release the provider via ``disconnect()`` — use
    :func:`_connected_firewalla` (a context manager) instead of calling this
    directly so teardown is guaranteed.

    An optional instance.yaml ``devices.firewalla.brand`` pins the provider
    explicitly, bypassing ``detect()`` — the escape hatch for a box whose
    read-only probe is not implemented (without it a stubbed probe degrades the
    real box to the read-only fallback).
    """
    net = _firewalla_netcontext()
    pinned = config.instance_value("devices.firewalla.brand", None)
    brand_pin = str(pinned) if pinned is not None else None
    provider = registry.resolve("firewalla", net, brand_pin=brand_pin)
    provider.connect(_firewalla_creds(net))
    return provider


@contextmanager
def _connected_firewalla() -> Iterator[DeviceProvider]:
    """Yield a connected Firewalla provider, guaranteeing ``disconnect()`` on exit.

    Closes the lifecycle gap behind every ``sanctum net firewalla ...`` command:
    ``disconnect`` is part of the ``DeviceProvider`` Protocol and is idempotent +
    safe even if ``connect`` failed, so the ``finally`` can always call it.
    """
    provider = _resolve_firewalla()
    try:
        yield provider
    finally:
        provider.disconnect()


def _fw_pause_path(target: str) -> str:
    """The bridge path that pauses policy ``target`` (RAW — encoding is the provider's).

    ``target`` is interpolated VERBATIM here; the percent-encoding is owned by ONE
    layer — the provider's bridge seam (``firewalla._encode_path``, applied inside
    ``_fetch_bridge_json`` / ``_post_bridge_json``). Encoding here *as well* would
    double-encode: a literal-``%`` target id would become ``%2525`` at the wire —
    the exact footgun the boundary fix is meant to prevent — because both layers
    would percent-encode the same bytes (CLAUDE.md "a test cannot catch a bug it
    shares"). The path shape (``/policies/<id>/pause``) mirrors the provider's
    existing ``/policies`` vocabulary; the provider seam keeps the ``/`` separators
    literal and encodes everything else exactly once.
    """
    return f"/policies/{target}/pause"


# NOTE: the pause-apply verify helper (re-read /policies, confirm the target id is
# present post-flip) was removed with the pause-apply descope (Task 6 finding 2):
# /policies presence cannot detect a refused PAUSE anyway (the id is present
# whether or not it is paused), and the invented pause/restore routes do not exist
# in the bridge contract. When pause is re-enabled, the verify must check the
# *paused* state (not mere id presence) against the real route shape.


@firewalla_app.command("status", help="Read-only Firewalla summary: brand + box info.")
def firewalla_status() -> None:
    try:
        with _connected_firewalla() as provider:
            info = provider.get(_FW_INFO_PATH)
            brand, kind = provider.brand, provider.kind
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    console.print(f"[bold]firewalla:[/] {escape(brand)} ({escape(kind)})")
    console.print(f"[bold]info:[/] {escape(info or '-')}")


@firewalla_app.command("policies", help="Read-only: the box's policy state.")
def firewalla_policies() -> None:
    try:
        with _connected_firewalla() as provider:
            body = provider.get(_FW_POLICIES_PATH)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    console.print(escape(body if body is not None else "-"))


@firewalla_app.command("flows", help="Read-only: recent network flows seen by the box.")
def firewalla_flows() -> None:
    try:
        with _connected_firewalla() as provider:
            body = provider.get(_FW_FLOWS_PATH)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    console.print(escape(body if body is not None else "-"))


# The mutating pause is DESCOPED until the bridge routes exist (Task 6 finding 2).
#
# The apply path writes POST /policies/<id>/pause and rolls back via POST
# /policies/restore. NEITHER endpoint exists in the established Firewalla bridge
# contract: the shipping screen_time surface only GETs /info, /policies,
# /host/<mac>, /hosts and references the one documented mutate POST
# /policies/purge (screen_time.py:330). /policies/<id>/pause and /policies/restore
# are introduced fresh by this branch and appear NOWHERE in the bridge server
# (which lives outside this repo and cannot be verified safely here). Against the
# real box both 404 → the apply set() returns ok=False → (finding 1's fix) the
# change raises → rails trip rollback → rollback POSTs to the also-nonexistent
# /policies/restore → ok=False → "ROLLBACK FAILED — device left half-applied".
# So the mutate path cannot succeed AND emits a scary worst-case message on the
# happy path (CLAUDE.md "Contracts at the Boundary": a cross-layer mutate contract
# must be proven against the real consumer; it was only exercised against mocks
# that accept any path). Per the finding's remedy, pause is descoped to read-only
# preview until the routes are implemented + an env-gated contract smoke confirms
# their shapes. The provider's snapshot/rollback machinery stays in place for that
# future re-enable; only the unverifiable fire is removed.
_PAUSE_DESCOPED_FIX = (
    "the bridge POST /policies/<id>/pause + /policies/restore routes are not yet "
    "part of the Firewalla bridge contract; this preview describes the intended "
    "flow without firing it. Re-enable once the routes exist and an env-gated "
    "read-only contract smoke confirms their shapes."
)


@firewalla_app.command(
    "pause",
    help="Preview a policy pause (read-only). Mutating apply is descoped until the bridge routes exist.",
)
def firewalla_pause(
    target: Annotated[str, typer.Argument(help="Policy id (or target) to pause.")],
    apply: Annotated[
        bool,
        typer.Option("--apply", help="DESCOPED: the bridge pause/restore routes do not exist yet."),
    ] = False,
    force: Annotated[  # noqa: ARG001 - retained for CLI stability; apply is descoped
        bool, typer.Option("--force", help="(no effect — apply is descoped)")
    ] = False,
) -> None:
    pause_path = _fw_pause_path(target)
    plan = [
        f"pause plan for target {target!r} (PREVIEW — not fired):",
        f"  1. POST {pause_path}  (pause the policy)",
        "  2. verify: the policy is still reachable after the flip",
        "  3. on verify failure: roll back to the pre-change policy snapshot",
    ]
    for line in plan:
        console.print(escape(line))

    if apply:
        # The mutating fire is descoped: refuse loudly instead of POSTing to a
        # route that does not exist (which would 404 → ok=False → a "ROLLBACK
        # FAILED" message on a route that also does not exist). No provider.set /
        # guarded_apply is reached — the hard guardrail for the overnight build.
        exc = DeviceError(
            "firewalla pause --apply is descoped (bridge routes not in the contract)",
            fix=_PAUSE_DESCOPED_FIX,
        )
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code))

    console.print(f"\n[dim]preview only: no changes made. {escape(_PAUSE_DESCOPED_FIX)}[/]")


# ─── orbi (network-gear provider surface) ────────────────────────────
#
# Mirrors the hub + firewalla sub-apps: an `orbi` Typer sub-app under net_app,
# resolved via registry.resolve("orbi", net) and connected through the same
# DeviceProvider rails. Reads (status / firmware / channels) are read-only;
# guest-wifi is MUTATING and defaults to a dry-run that fires ZERO set calls —
# the apply path routes through guarded_apply via the provider's GUEST_WIFI
# capability_op (snapshot → confirm → set → verify → rollback). The overnight
# build never mutates live gear (we have no live Orbi creds yet either).

orbi_app = typer.Typer(help="Drive the NETGEAR Orbi mesh router through the device-provider rails.")
net_app.add_typer(orbi_app, name="orbi")

# Provider-path leaves the orbi read commands surface. These mirror the provider's
# own path vocabulary (sanctum_cli.devices.orbi); a path the provider does not
# expose returns None and the command prints a dash. The guest-wifi WRITE leaf is
# NOT hardcoded here — it is resolved from the provider's capability_op(GUEST_WIFI),
# so a non-pynetgear mesh brand toggles guest wifi via its own leaf.
_ORBI_GUEST_2G = "guest_wifi/2g"
_ORBI_GUEST_5G = "guest_wifi/5g"
_ORBI_CHANNEL_2G = "channel/2g"
_ORBI_CHANNEL_5G = "channel/5g"
_ORBI_FIRMWARE_PATH = "firmware/new"
_ORBI_MODEL_PATH = "info/model"

# The Orbi admin Keychain (service, account) is resolved discovery-first by
# :func:`device_keychain_ref` (instance.yaml ``devices.orbi.keychain.*`` → per-kind
# default) rather than hardcoded to the Orbi-provider tuple. The provider re-reads
# the password from the Keychain itself; the CLI only supplies the account as the
# Creds username (credentials never flow through the CLI layer).


def _orbi_netcontext() -> NetContext:
    """Build the NetContext the registry fingerprints the Orbi over.

    Parses the default gateway from the real ``route`` probe (read-only) and
    threads the real runner so a provider's ``detect()`` can probe without owning
    its own subprocess plumbing. Monkeypatched in tests so no shell-out occurs.
    """
    gw = detect.parse_default_gateway(system.real_runner(("route",)))
    return NetContext(gateway_ip=gw, runner=system.real_runner)


def _orbi_creds(net: NetContext) -> Creds:
    """Assemble Creds for the resolved Orbi via the generalized resolver.

    Delegates to :func:`device_creds` so the Orbi admin account is read from
    instance.yaml (``devices.orbi.keychain.account``) or the per-kind default.
    The secret stays ``None`` — the provider reads the password from the Keychain
    at connect time (credentials never flow through the CLI layer).
    """
    return device_creds("orbi", net)


def _resolve_orbi() -> DeviceProvider:
    """Resolve + connect the Orbi provider for the local network.

    Detection is read-only; ``connect`` opens the (best-effort) pynetgear session.
    Any transport/auth failure on a later read raises a ``SanctumError``
    (DeviceError) which the command wrappers map to a clean exit code.

    NOTE: the caller MUST release the provider via ``disconnect()`` — use
    :func:`_connected_orbi` (a context manager) instead of calling this directly
    so teardown is guaranteed.

    An optional instance.yaml ``devices.orbi.brand`` pins the provider explicitly,
    bypassing ``detect()`` — the escape hatch for a box whose read-only probe is
    not implemented (without it a stubbed probe degrades the real Orbi to the
    read-only fallback). Pin ``orbi`` to drive a NETGEAR Orbi end-to-end.
    """
    net = _orbi_netcontext()
    pinned = config.instance_value("devices.orbi.brand", None)
    brand_pin = str(pinned) if pinned is not None else None
    provider = registry.resolve("orbi", net, brand_pin=brand_pin)
    provider.connect(_orbi_creds(net))
    return provider


@contextmanager
def _connected_orbi() -> Iterator[DeviceProvider]:
    """Yield a connected Orbi provider, guaranteeing ``disconnect()`` on exit.

    Closes the lifecycle gap behind every ``sanctum net orbi ...`` command:
    ``disconnect`` is part of the ``DeviceProvider`` Protocol and is idempotent +
    safe even if ``connect`` failed, so the ``finally`` can always call it.
    """
    provider = _resolve_orbi()
    try:
        yield provider
    finally:
        provider.disconnect()


@orbi_app.command("status", help="Read-only Orbi summary: brand, model, guest-wifi state.")
def orbi_status() -> None:
    try:
        with _connected_orbi() as provider:
            model = provider.get(_ORBI_MODEL_PATH)
            guest_2g = provider.get(_ORBI_GUEST_2G)
            guest_5g = provider.get(_ORBI_GUEST_5G)
            brand, kind = provider.brand, provider.kind
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    console.print(f"[bold]orbi:[/] {escape(brand)} ({escape(kind)})")
    console.print(f"[bold]model:[/] {escape(model or '-')}")
    console.print(f"[bold]guest-wifi 2g:[/] {escape(guest_2g or '-')}")
    console.print(f"[bold]guest-wifi 5g:[/] {escape(guest_5g or '-')}")


@orbi_app.command("firmware", help="Read-only: the Orbi's available firmware update, if any.")
def orbi_firmware() -> None:
    try:
        with _connected_orbi() as provider:
            new = provider.get(_ORBI_FIRMWARE_PATH)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    console.print(f"[bold]firmware (new):[/] {escape(new or '-')}")


@orbi_app.command("channels", help="Read-only: the Orbi's 2.4 GHz / 5 GHz radio channels.")
def orbi_channels() -> None:
    try:
        with _connected_orbi() as provider:
            ch_2g = provider.get(_ORBI_CHANNEL_2G)
            ch_5g = provider.get(_ORBI_CHANNEL_5G)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    console.print(f"[bold]channel 2g:[/] {escape(ch_2g or '-')}")
    console.print(f"[bold]channel 5g:[/] {escape(ch_5g or '-')}")


@orbi_app.command(
    "guest-wifi",
    help="Turn the Orbi guest network on/off. Dry-run by default; pass --apply to fire.",
)
def orbi_guest_wifi(
    state: Annotated[str, typer.Argument(help="Desired guest-wifi state: on | off.")],
    apply: Annotated[
        bool,
        typer.Option(
            "--apply", help="Actually fire the change (guarded by snapshot→verify→rollback)."
        ),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Skip the confirmation prompt (with --apply).")
    ] = False,
) -> None:
    desired = state.strip().lower()
    if desired not in ("on", "off"):
        exc = DeviceError(
            f"invalid guest-wifi state {state!r} (expected 'on' or 'off')",
            fix="pass 'on' or 'off', e.g. `sanctum net orbi guest-wifi on`",
        )
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code))

    try:
        with _connected_orbi() as provider:
            # Resolve the guest-wifi leaf from the provider's own vocabulary so the
            # CLI never hardcodes an Orbi path; None → the provider has no guest-wifi
            # op and we refuse rather than mutate an unknown leaf.
            op = provider.capability_op(Capability.GUEST_WIFI)
            if op is None:
                exc = DeviceError(
                    f"{provider.brand} ({provider.kind}) does not support guest-wifi toggling",
                    fix="use a provider that advertises Capability.GUEST_WIFI, or pin the right brand.",
                )
                _report(exc)
                raise typer.Exit(code=int(exc.exit_code))
            # The value that ENGAGES the capability is op.engaged ("on"); the
            # disengaged value is the other state. Derive it so a brand whose
            # engaged sentinel is not literally "on" still flips correctly.
            target_value = (
                op.engaged if desired == "on" else ("off" if op.engaged == "on" else "on")
            )

            plan = [
                f"guest-wifi {desired} plan:",
                f"  1. set {op.path} = {target_value}  (Orbi guest network → {desired})",
                "  2. verify: the guest-wifi leaf reflects the requested state",
                "  3. on verify failure: roll back to the pre-change snapshot",
            ]
            for line in plan:
                console.print(escape(line))

            if not apply:
                # Dry-run: describe, do not mutate. The hard guardrail — no
                # provider.set / guarded_apply is reached on this branch.
                console.print("\n[dim]dry-run: no changes made. Re-run with --apply to fire.[/]")
                return

            def change(pv: DeviceProvider) -> OpResult:
                # Return the OpResult straight through so the rails can inspect it:
                # OrbiProvider.set signals a refused write by RETURNING ok=False (no
                # raise), and guarded_apply now treats a returned ok=False as a
                # failed apply (rollback + ok=False). Discarding it here would let a
                # refused write reach the verify gate and commit if the leaf merely
                # LOOKS correct on re-read — the P2 silent-discard finding.
                return pv.set(op.path, target_value)

            def verify_fn() -> bool:
                # Real-world verify: re-read the leaf and confirm it reflects the
                # requested state (a refused/no-op set leaves the old value, which
                # trips rollback). This is a read of the SAME leaf we wrote.
                #
                # provider.get() RAISES DeviceError on a transport/auth flake
                # (base.py Protocol; orbi._get_guest). guarded_apply calls
                # verify_fn() UNGUARDED, so a raising read-back here would escape
                # the rails AFTER the change applied — leaving the guest network
                # flipped with no rollback (the exact half-applied state the rails
                # exist to prevent, and plausible right after a wifi-radio change).
                # Treat a failed read-back as a failed verify so the rails roll
                # back rather than commit a half-applied device.
                try:
                    return provider.get(op.path) == target_value
                except DeviceError:
                    return False

            result = rails.guarded_apply(
                provider,
                change,
                verify_fn=verify_fn,
                confirm=lambda p: typer.confirm(f"{p}\nProceed?"),
                force=force,
                rollback=True,
            )
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc

    if result.ok:
        console.print(f"\n[green]✓[/] {escape(result.detail)}")
    else:
        console.print(f"\n[yellow]{escape(result.detail)}[/]")
        raise typer.Exit(code=1)


# ─── ha-green (Home Assistant appliance provider surface) ────────────
#
# Mirrors the firewalla sub-app: a `ha-green` Typer sub-app under net_app,
# resolved via registry.resolve("ha-green", net) and connected through the same
# DeviceProvider rails. The HA Green is the haus Home Assistant appliance — a
# Bearer-(owner-)token REST box exactly like the Firewalla bridge, so it onboards
# and reports the SAME way. The surface is READ-ONLY (HA mutations ride the
# ha-green-toolkit's WebSocket path), so there is no set/pause command — only the
# honest health `status`. Importing sanctum_cli.devices.ha_green self-registers
# HaGreenProvider under kind="ha-green" (see the module footer); referenced here so
# the import is never pruned as unused.
_ha_green_registered = ha_green_provider

ha_green_app = typer.Typer(
    help="Read the HA Green (Home Assistant appliance) through the device-provider rails."
)
net_app.add_typer(ha_green_app, name="ha-green")

# The Green's REST read paths the status summary surfaces (the provider's own
# vocabulary). ``/api/`` is the running-marker oracle; ``/api/config`` carries the
# Core version. The bearer owner token is self-resolved by the provider at connect
# time (env / on-disk secret), so the CLI never carries it; the username is a
# label only.
_HA_API_PATH = "/api/"
_HA_CONFIG_PATH = "/api/config"
_HA_USERNAME = "owner"


def _ha_green_netcontext() -> NetContext:
    """Build the NetContext the registry fingerprints the HA Green over.

    Parses the default gateway from the real ``route`` probe (read-only) and
    threads the real runner so a provider's ``detect()`` can probe without owning
    its own subprocess plumbing. Monkeypatched in tests so no shell-out occurs.
    """
    gw = detect.parse_default_gateway(system.real_runner(("route",)))
    return NetContext(gateway_ip=gw, runner=system.real_runner)


def _ha_green_creds(net: NetContext) -> Creds:
    """Assemble Creds for the resolved HA Green.

    The host is the Green's LAN IP (the gateway is unused — HA is not the gateway,
    so fall back to the provider's documented LAN reservation); the username is a
    label. The secret is left ``None`` on purpose — the provider reads the owner
    token from the env / on-disk secret at connect time (credentials never flow
    through the CLI layer), exactly like the Firewalla bearer token.
    """
    return Creds(
        host=net.gateway_ip or "",
        username=_HA_USERNAME,
        secret=None,
        key_path=None,
    )


def _resolve_ha_green() -> DeviceProvider:
    """Resolve + connect the HA Green provider for the local network.

    Detection is read-only; ``connect`` resolves the owner token. Any transport /
    auth failure on a later strict read raises a ``SanctumError`` (DeviceError)
    which the command wrappers map to a clean exit code.

    NOTE: the caller MUST release the provider via ``disconnect()`` — use
    :func:`_connected_ha_green` (a context manager) instead of calling this
    directly so teardown is guaranteed.

    An optional instance.yaml ``devices.ha-green.brand`` pins the provider
    explicitly, bypassing ``detect()`` — the escape hatch for a Green whose
    read-only probe is stubbed (without it a stubbed probe degrades the real Green
    to the read-only fallback).
    """
    net = _ha_green_netcontext()
    pinned = config.instance_value("devices.ha-green.brand", None)
    brand_pin = str(pinned) if pinned is not None else None
    provider = registry.resolve("ha-green", net, brand_pin=brand_pin)
    provider.connect(_ha_green_creds(net))
    return provider


@contextmanager
def _connected_ha_green() -> Iterator[DeviceProvider]:
    """Yield a connected HA Green provider, guaranteeing ``disconnect()`` on exit.

    Closes the lifecycle gap behind every ``sanctum net ha-green ...`` command:
    ``disconnect`` is part of the ``DeviceProvider`` Protocol and is idempotent +
    safe even if ``connect`` failed, so the ``finally`` can always call it.
    """
    provider = _resolve_ha_green()
    try:
        yield provider
    finally:
        provider.disconnect()


@ha_green_app.command(
    "status",
    help="Read-only HA Green health: LAN reachable, HA API up + version, Tailscale node.",
)
def ha_green_status() -> None:
    """Honest health report for the haus Home Assistant Green.

    Every ✓/✗ derives from a REAL check, never from "the command ran" (HONEST-VERIFY):

    * LAN — a TCP connect to the Green's host:port (``lan_reachable``);
    * HA API — ``GET /api/`` returning the ``"API running."`` marker AND the owner
      token authenticating (``api_running``), plus the Core ``version`` when up;
    * Tailscale — the tailnet listing the ``homeassistant`` node
      (``tailscale_node_present``).

    Exits ``LOCAL_ERROR`` when the Core API is NOT up — so the command is a
    scriptable health gate that fails honestly on a down/unreachable Green rather
    than printing a dash and exiting 0.
    """
    try:
        with _connected_ha_green() as provider:
            api_up = ha_green_provider.api_running()
            version = ha_green_provider.ha_version() if api_up else None
            lan_up = ha_green_provider.lan_reachable()
            tailnet_up = ha_green_provider.tailscale_node_present()
            brand, kind = provider.brand, provider.kind
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc

    host, port = ha_green_provider._url_host_port()
    lan_mark = "[green]reachable ✓[/]" if lan_up else "[red]unreachable ✗[/]"
    api_mark = f"[green]up ✓[/] (version {escape(version or '?')})" if api_up else "[red]down ✗[/]"
    tail_mark = (
        f"[green]joined ✓[/] ({escape(ha_green_provider._TAILNET_NODE)}."
        f"{escape(ha_green_provider._TAILNET_SUFFIX)})"
        if tailnet_up
        else "[yellow]not joined ✗[/]"
    )

    console.print(f"[bold]ha-green:[/] {escape(brand)} ({escape(kind)})")
    console.print(f"[bold]LAN ({escape(host)}:{port}):[/] {lan_mark}")
    console.print(f"[bold]HA API:[/] {api_mark}")
    console.print(f"[bold]Tailscale '{escape(ha_green_provider._TAILNET_NODE)}':[/] {tail_mark}")

    if not api_up:
        # Honest unhealthy signal: never report a green status over a Core that did
        # not answer the running marker (a down box / missing token / wrong body).
        raise typer.Exit(code=int(ExitCode.LOCAL_ERROR))
