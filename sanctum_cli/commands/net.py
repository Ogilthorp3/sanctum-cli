from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TypeVar

import typer
from rich.console import Console
from rich.markup import escape

from sanctum_cli import config
from sanctum_cli.devices import firewalla as firewalla_provider
from sanctum_cli.devices import flip, intents, interlock, rails, registry, sagemcom
from sanctum_cli.devices import ha_green as ha_green_provider
from sanctum_cli.devices import orbi as orbi_provider
from sanctum_cli.devices.armor import SinglenatArmorInstaller
from sanctum_cli.devices.base import Capability, Creds, DeviceError, NetContext, OpResult
from sanctum_cli.errors import ExitCode, LocalError, SanctumError
from sanctum_cli.net import (
    detect,
    heal,
    playbooks,
    render,
    safety,
    speedtest,
    system,
    verify,
)
from sanctum_cli.net import (
    status as net_status,
)
from sanctum_cli.commands.home_net import home_app
from sanctum_cli.net.types import Nat, SpeedReport, Verdict
from sanctum_cli.onboard_experience import chapter_banner, green_check

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sanctum_cli.devices.base import DeviceProvider
    from sanctum_cli.devices.intents import ArmorInstaller
    from sanctum_cli.net.detect import HttpProbe, Runner
    from sanctum_cli.net.link import CommandRunner, IdentityDiagnosis
    from sanctum_cli.net.types import TopologyReport

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


def _firewalla_host() -> str | None:
    """Resolve the box (Firewalla) host the SSH transport targets — config-first.

    Reads ``devices.firewalla.host`` from instance.yaml at CALL TIME (so an off-LAN
    cutover perch pins its tailnet box IP), falling back to the detected default
    gateway — the shipped general-purpose behavior, which on the LAN resolves to the
    box's own address. Returns ``None`` only when nothing is configured AND no
    gateway parses (no box → fail-closed at the runner/gate). The SAME resolution
    backs both :func:`_build_runner` (observe_lease / verify box reads + the recovery
    re-lease) and :func:`_firewalla_recovery_host` (the gate), so they never diverge.
    """
    override = config.instance_value("devices.firewalla.host", None)
    if override is not None:
        return str(override)
    return detect.parse_default_gateway(system.real_runner(("route",))) or None


def _build_runner() -> Runner:
    host = _firewalla_host()
    key_path = _firewalla_key_path()
    fw_key = str(key_path) if key_path.exists() else None
    return system.make_real_runner(fw_gateway=host, fw_key=fw_key)


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


def _emit_heal_result(outcome: str) -> None:
    """Print the machine-readable ``NET_HEAL_RESULT=<outcome>`` token on its own line.

    This is the daemon wrapper's contract, NOT for humans (the ✓/✗ prose lines are
    kept for them). ``outcome`` is exactly one of ``healed`` (real re-probe passed:
    lease + reachable gateway), ``reverted`` (fired but stayed unhealthy → reverted),
    or ``noop`` (dry-run / stop-and-alert / nothing to do / non-root). The wrapper
    resets the no-loop attempts counter ONLY on ``healed``; every other token (or its
    absence) increments it, so the MAX_HEAL_ATTEMPTS cap accrues.

    The token derives from the SAME real outcome as the human line (honest-verify),
    and is printed with ``markup=False`` so the literal ``=`` / token bytes reach
    stdout verbatim for the wrapper's anchored ``grep -q 'NET_HEAL_RESULT=healed'``.
    """
    token = {
        "healed": heal.HEAL_RESULT_HEALED,
        "reverted": heal.HEAL_RESULT_REVERTED,
        "noop": heal.HEAL_RESULT_NOOP,
    }[outcome]
    console.print(token, markup=False, highlight=False)


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
    Under ``--apply`` it also emits an unambiguous machine-readable result token on
    its own line — ``NET_HEAL_RESULT=healed`` / ``=reverted`` / ``=noop`` — derived
    from the SAME real re-probe as the ✓; the daemon wrapper keys its no-loop
    attempts counter on that token, not the human prose (the ``=healed`` token is
    the only thing that resets the cap, so a reverted heal accrues it).
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
        _emit_heal_result("noop")
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
        _emit_heal_result("noop")
        return

    # Snapshot the current config so a failed heal can be cleanly reverted. No
    # revertable baseline → refuse (never-strand: don't fire what we can't undo).
    snap = _heal_snapshot(runner)
    if snap is None:
        console.print(
            "\n[yellow]stop + alert:[/] could not snapshot the current IPv4 config — "
            "refusing to heal without a revert baseline (never-strand)."
        )
        _emit_heal_result("noop")
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
        # Machine-readable result derived from the SAME real re-probe as the ✓.
        _emit_heal_result("healed")
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
    # Machine-readable result: fired but stayed unhealthy → reverted. The daemon
    # wrapper's no-loop counter keys on this token (NOT the human prose), so it
    # accrues the MAX_HEAL_ATTEMPTS cap instead of resetting on the word "healed".
    _emit_heal_result("reverted")


# ─── status (one-glance whole-node network roll-up) ──────────────────
#
# `sanctum net status` collapses what today takes 3-4 commands (+ an SSH to the
# Firewalla) into ONE read-only pane. Each subsystem is gathered behind its own
# module-level probe seam (so tests patch them with zero live calls); each seam is
# wrapped by `_safe_probe` so a raised probe → that row degrades to UNKNOWN and the
# pane still renders (fail-closed per row, never a crash). The gathered value
# objects feed the pure `net_status.build_status_report` assembler; the result is
# rendered as an apple-like rich panel. READ-ONLY: no mutation, no sudo.

# The Firewalla trust-guardian heartbeat file (epoch seconds) + its freshness
# window. FRESH when the heartbeat is younger than this; STALE otherwise.
_GUARDIAN_HEARTBEAT_PATH = "/home/pi/.sanctum/trust-guardian/heartbeat"
_GUARDIAN_FRESH_S = 25 * 60  # 25 minutes
_GUARDIAN_SSH_TIMEOUT_S = 8

# The heal-daemon heartbeat log: the wrapper appends "<iso> <status ...>" lines each
# cycle. A daemon launchctl still lists as "loaded" but whose last heartbeat is older
# than this window is WEDGED (not firing on its interval) — so the status pane gates
# the loaded->OK mapping on freshness, the same way the guardian row does.
_HEAL_HEARTBEAT_FRESH_S = heal.HEAL_INTERVAL_S * 5  # ~10 min at the 120s default

_P = TypeVar("_P")


def _status_probe_posture() -> heal.PostureDiagnosis:
    """Probe + diagnose the node's L3 posture (reuses the heal pure core)."""
    runner = _build_heal_runner()
    posture = heal.probe_posture(run=runner)
    return heal.diagnose_posture(posture, overlap=heal.overlap_for(posture))


def _status_probe_spine() -> net_status.SpineInfo:
    """Read the never-strand spine (tailnet / TB5) from ifconfig (reuses heal)."""
    from sanctum_cli.net.link import _real_run

    on_tailnet, tb5_up = heal._spine_from_ifconfig(_real_run(["ifconfig"]))
    return net_status.SpineInfo(on_tailnet=on_tailnet, tb5_up=tb5_up)


def _parse_daemon_heartbeat(
    text: str, *, now: datetime | None = None
) -> tuple[str | None, int | None, bool | None]:
    """Parse the last heal heartbeat line into (last_status, age_seconds, fresh).

    Empty log -> (None, None, None). When the last line has no parseable ISO
    timestamp the status is still surfaced but age/fresh are None (fail-open: never
    fabricate a stale reading). ``fresh`` is whether the heartbeat is younger than
    ``_HEAL_HEARTBEAT_FRESH_S``; a loaded daemon with a stale heartbeat is WEDGED.
    """
    if now is None:
        now = datetime.now()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return (None, None, None)
    line = lines[-1].strip()
    parts = line.split(maxsplit=1)
    rest = parts[1] if len(parts) > 1 else ""
    try:
        hb = datetime.strptime(parts[0], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return (line, None, None)
    age = int((now - hb).total_seconds())
    return (rest, age, age <= _HEAL_HEARTBEAT_FRESH_S)


def _daemon_last_result() -> str | None:
    """Best-effort: the status word from the last net-heal heartbeat line, or None."""
    try:
        text = heal._HEAL_HEARTBEAT_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_daemon_heartbeat(text)[0]


def _status_probe_daemon() -> net_status.DaemonInfo:
    """Is the com.sanctum.net-heal LaunchDaemon loaded? + its last-known result.

    Read-only: `launchctl print system/<label>` returns 0 when the daemon is
    loaded, non-zero otherwise. The last-known result comes from the daemon's own
    heartbeat log (never a live heal)."""
    loaded, _ = _heal_launchctl(
        ["print", f"system/{heal.HEAL_DAEMON_LABEL}"], check=True
    )
    try:
        text = heal._HEAL_HEARTBEAT_FILE.read_text(encoding="utf-8")
    except OSError:
        text = ""
    last, age, fresh = _parse_daemon_heartbeat(text)
    return net_status.DaemonInfo(
        loaded=loaded, last_result=last, age_seconds=age, fresh=fresh
    )


def _status_probe_identity() -> IdentityDiagnosis:
    """Probe + diagnose the node's on-network Wi-Fi identity (reuses the link core)."""
    from sanctum_cli.net.link import _real_run, diagnose_identity, probe_identity

    return diagnose_identity(probe_identity(run=_real_run))


def _status_probe_topology() -> TopologyReport:
    """Classify NAT topology (reuses the `sanctum net` detector)."""
    runner, http = _build_runner(), _build_http()
    return detect.detect(runner=runner, http=http, firewalla_present=_firewalla_present())


def _status_probe_guardian() -> net_status.GuardianInfo:
    """Best-effort: read the Firewalla trust-guardian heartbeat age over the existing
    SSH seam. UNKNOWN (reachable=False) when the FW is unreachable / no key — never
    a crash, never a block. FRESH when the heartbeat is younger than the window."""
    gw = detect.parse_default_gateway(system.real_runner(("route",)))
    key_path = _firewalla_key_path()
    if not gw or not key_path.exists():
        return net_status.GuardianInfo(reachable=False, fresh=None, age_seconds=None)
    epoch = _firewalla_guardian_epoch(gw, str(key_path))
    if epoch is None:
        return net_status.GuardianInfo(reachable=False, fresh=None, age_seconds=None)
    age = max(0, int(time.time()) - epoch)
    return net_status.GuardianInfo(
        reachable=True, fresh=age < _GUARDIAN_FRESH_S, age_seconds=age
    )


def _firewalla_guardian_epoch(gateway: str, key: str, user: str = "pi") -> int | None:
    """SSH to the Firewalla (key-only, read-only) and read the guardian heartbeat epoch.

    Mirrors ``system.firewalla_wan_via_ssh``'s hardened transport (BatchMode,
    publickey-only, accept-new, bounded connect). Returns the epoch int, or None on
    any failure (unreachable, no file, unparseable) — best-effort, never raises."""
    argv = [
        "ssh",
        "-i",
        key,
        "-o",
        "BatchMode=yes",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=5",
        f"{user}@{gateway}",
        f"cat {_GUARDIAN_HEARTBEAT_PATH} 2>/dev/null",
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_GUARDIAN_SSH_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    m = re.search(r"\b(\d{9,})\b", proc.stdout)
    return int(m.group(1)) if m else None


def _safe_probe(probe: Callable[[], _P]) -> _P | None:
    """Run a probe seam; any raise / failure → None (fail-closed → UNKNOWN row).

    This is the per-row guard the pane's never-crash contract rests on: one failing
    subsystem probe degrades ONLY its own row to UNKNOWN, never the whole pane."""
    try:
        return probe()
    except Exception:
        return None


_STATUS_ROW_STYLE = {
    net_status.RowStatus.OK: ("green", "✓"),
    net_status.RowStatus.ATTENTION: ("yellow", "!"),
    net_status.RowStatus.DOWN: ("red", "✗"),
    net_status.RowStatus.UNKNOWN: ("dim", "?"),
}

_OVERALL_STYLE = {"GREEN": "green", "ATTENTION": "yellow", "DEGRADED": "red"}


def _render_status(report: net_status.StatusReport) -> None:
    """Render the roll-up as an apple-like rich panel (one glance, read-only)."""
    from rich.panel import Panel
    from rich.table import Table

    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", no_wrap=True)  # glyph
    table.add_column(no_wrap=True)  # label
    table.add_column(no_wrap=True)  # status word
    table.add_column(overflow="fold")  # detail
    for row in report.rows:
        style, glyph = _STATUS_ROW_STYLE[row.status]
        table.add_row(
            f"[{style}]{glyph}[/]",
            f"[bold]{escape(row.label)}[/]",
            f"[{style}]{escape(row.status.name)}[/]",
            f"[dim]{escape(row.detail)}[/]",
        )
    overall_style = _OVERALL_STYLE.get(report.overall, "dim")
    title = f"Network status — [{overall_style}]{escape(report.overall)}[/]"
    console.print(Panel(table, title=title, title_align="left", border_style=overall_style))


@net_app.command(
    "status",
    help="One-glance whole-node network health roll-up (read-only, no changes).",
)
def net_status_cmd() -> None:
    """Collapse posture + spine + heal-daemon + identity + topology + Firewalla
    guardian into ONE read-only pane with a single overall verdict.

    Each subsystem is probed behind its own guarded seam: a probe that fails or
    raises degrades ONLY its own row to UNKNOWN — the pane always renders (never a
    crash, never a block). The Firewalla trust-guardian read is best-effort over the
    existing SSH seam and shows UNKNOWN when the box is unreachable or no key is
    present. Nothing here mutates anything or needs sudo."""
    report = net_status.build_status_report(
        posture=_safe_probe(_status_probe_posture),
        spine=_safe_probe(_status_probe_spine),
        daemon=_safe_probe(_status_probe_daemon),
        identity=_safe_probe(_status_probe_identity),
        topology=_safe_probe(_status_probe_topology),
        guardian=_safe_probe(_status_probe_guardian),
    )
    _render_status(report)


# ─── hub (network-gear provider surface) ─────────────────────────────
#
# Importing sanctum_cli.devices.sagemcom self-registers SagemcomHubProvider under
# kind="hub" (see the module footer), so registry.resolve("hub", net) can find it.
# Referenced here so the import is never pruned as unused.
_ = sagemcom

hub_app = typer.Typer(help="Drive the network gateway (hub) through the device-provider rails.")
net_app.add_typer(hub_app, name="hub")
net_app.add_typer(home_app, name="home")

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

    Prefer ``devices.hub.host`` from instance.yaml when set (e.g. Bell hub mgmt
    while the house default gateway is Firewalla). Falls back to the default
    gateway from the real ``route`` probe. Monkeypatched in tests so no shell-out
    occurs.
    """
    pinned = config.instance_value("devices.hub.host", None)
    if pinned:
        gw = str(pinned)
    else:
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


# ``sanctum net single-nat`` is the FULL Bell Advanced-DMZ + /32 cutover surface,
# driving the staged :func:`intents.single_nat_dmz` orchestrator (the brain is the
# pure :mod:`sanctum_cli.devices.flip` machine). It supersedes the old single-leaf
# ``net hub single-nat`` (SetBridgeMode), which is now a deprecation shim that
# redirects here (see :func:`hub_single_nat`). Dry-run by default — bare invocation
# makes ZERO device writes; ``--apply`` fires the cutover ONLY when the out-of-band
# recovery probe says reachable; ``--rollback`` undoes a prior cutover (disable DMZ
# + re-lease DHCP).

# The armor kit checkout dir is NO LONGER hardcoded here (FIX-d2) — it resolves
# config-first through the SHARED ``intents._armor_kit_dir`` seam
# (``paths.armor_kit_dir`` → the shipped default), the SAME resolver
# ``intents._default_armor_installer`` uses, so the CLI installer and the intents
# fallback never drift on the checkout path. The box + Mini HOSTS likewise resolve
# config-first through the ``intents._armor_*`` seams (``devices.firewalla.host`` /
# ``.ssh_user`` / ``devices.mini.host`` → LAN default), so an off-LAN operator's
# deploy rides the tailnet while the shipped default stays the LAN coordinates.

# The SHIPPED-default out-of-band recovery host the cutover gate probes: the Mini
# jump host, which the armor kit reaches on a SEPARATE link from the WAN the flip is
# changing (the README deploys via the Mini LAN jump host). A reachable Mini means there
# is a way to recover the hub if the cutover strands it; its absence makes the flip
# refuse. This is NO LONGER a hardcoded probe target (FIX-b): the gate resolves the
# Mini config-first through :func:`_out_of_band_host` (``devices.mini.host`` → this
# LAN default), so an off-LAN operator on the Bell hub Wi-Fi probes the tailnet Mini
# (which survives the /1 collapse) while the shipped default stays the LAN address.
_OUT_OF_BAND_HOST = "10.0.0.10"  # ip-allow: shipped LAN default for the OOB recovery Mini jump host; overridden config-first via devices.mini.host
_OUT_OF_BAND_PORT = 22

# The SSH port the recovery re-lease reaches the Firewalla over (the same port
# ``net.system._fw_ssh_argv`` uses for the ``dhcp_release`` op). The Firewalla host
# itself is NOT a constant — it is resolved at gate time as the default gateway
# (see :func:`_firewalla_recovery_host`), the same box ``_build_runner`` targets.
_FIREWALLA_SSH_PORT = 22


def _firewalla_recovery_host() -> str | None:
    """Resolve the Firewalla the recovery re-lease will SSH to (config-first, FIX-b).

    The unwind on a failed cutover (and an explicit ``--rollback``) fires the
    ``dhcp_release`` runner tag, which SSHes to the Firewalla — and ``_build_runner``
    resolves that box via :func:`_firewalla_host` (``devices.firewalla.host`` override
    → detected default gateway). The gate MUST probe the SAME host so "the recovery
    re-lease can reach its box" is what it actually verifies — so it shares
    :func:`_firewalla_host`. When Bert pins the tailnet box, the gate probes the
    tailnet box; on a default install it probes the LAN gateway. Returns ``None`` when
    neither resolves (no recovery host → no recovery path).
    """
    return _firewalla_host()


def _out_of_band_host() -> str:
    """Resolve the Mini jump host the OOB recovery gate TCP-probes — config-first (FIX-b).

    The recovery-path gate (:func:`_out_of_band_reachable`) checks the Mini is
    reachable before authorizing the cutover. Reads ``devices.mini.host`` from
    instance.yaml at CALL TIME and STRIPS any ``user@`` prefix (the gate does a bare
    TCP connect, not an SSH login — ``bert@<tailnet-ip>`` → ``<tailnet-ip>``),
    falling back to the shipped LAN default ``_OUT_OF_BAND_HOST`` so the
    general-purpose tool is unchanged. It shares the SAME ``devices.mini.host`` key
    the armor Mini deploy uses (:func:`intents._armor_mini_host`), so the gate and the
    deploy reach the SAME Mini. This closes the keystone asymmetry: before FIX-b the
    box leg of this gate was config-driven but the Mini leg was LAN-hardcoded, so an
    off-LAN perch (Bell hub Wi-Fi, ``192.168/16``) could not reach the ``10/8`` LAN Mini behind
    the Firewalla NAT and the fail-closed gate refused the cutover it was meant to
    authorize.
    """
    configured = config.instance_value("devices.mini.host", None)
    if configured is None:
        return _OUT_OF_BAND_HOST
    # The gate connects by TCP, not SSH — drop any ``user@`` login prefix.
    return str(configured).rsplit("@", 1)[-1]


def _build_armor_installer() -> ArmorInstaller:
    """Build the real single-NAT armor installer, box + Mini hosts config-first (FIX-b).

    The one seam through which the ``net single-nat`` command reaches a concrete
    armor install (stages ``stage_armor`` + ``apply_armor``). The box + Mini deploy
    targets resolve config-first via the shared ``intents._armor_*`` seams
    (``devices.firewalla.host`` / ``devices.firewalla.ssh_user`` / ``devices.mini.host``
    → LAN default) — one source of truth with :func:`intents._default_armor_installer`
    — so an off-LAN operator's scp/ssh rides the tailnet while the shipped default
    stays the LAN coordinates. Built only on the apply path (never the dry-run /
    gate-refused paths, which make zero host contact); tests swap this for a
    recording double so the wiring is exercised without shelling out.
    """
    return SinglenatArmorInstaller(
        kit_dir=intents._armor_kit_dir(),
        firewalla_host=intents._armor_firewalla_host(),
        firewalla_user=intents._armor_firewalla_user(),
        mini_host=intents._armor_mini_host(),
    )


def _single_nat_live_armed() -> bool:
    """True iff the live single-NAT cutover is explicitly armed in instance.yaml.

    ``net.single_nat_live: true`` is the Phase-2 arming switch (Jedi council 2026-07-18).
    Default False: the ``--apply`` DMZ engage refuses until an attended dry-run has
    validated the real hub/box seams. Reads :func:`config.instance_value`; a test
    monkeypatches THIS function so the whole config layer stays untouched.
    """
    return bool(config.instance_value("net.single_nat_live", False))


def _single_nat_requires_armor() -> bool:
    """Does this network's single-NAT playbook need the Bell /32-armor stages? (FIX-e)

    ``net single-nat`` is the Bell Advanced-DMZ surface, so it follows the **bell**
    playbook by default — which DOES hand the WAN a ``/1``-poison lease and needs the
    self-healing ``/32`` armor (``stage_armor`` + ``apply_armor``) plus the ``/32``
    poison gate. An instance.yaml ``net.isp`` pin selects a different ISP's playbook;
    for a non-Bell passthrough that yields a normal public lease, the matched
    playbook's ``requires_slash32_armor`` is False, so the orchestrator SKIPS the
    armor stages and the poison gate accepts a healthy public lease of any prefix.

    Defaults to the **bell** playbook — the SAFE default: an unset/unknown ``net.isp``
    never skips the armor on what could be a real Bell ``/1`` lease (fail-safe, not
    fail-open).
    """
    isp = str(config.instance_value("net.isp", "bell"))
    playbook = playbooks.BUILTINS.get(isp, playbooks.BUILTINS["bell"])
    return playbook.requires_slash32_armor


def _box_preflight_ready() -> flip.PreflightDecision:
    """Pre-apply box gate (FIX-f): passwordless sudo + a real ``dhclient`` on the box.

    The cutover's box ops (``wan_dhcp`` / ``dhcp_release``) run ``sudo dhclient`` over
    the SAME key-SSH transport the runner uses. If passwordless sudo is not configured
    the op hangs on a (TTY-less) password prompt and false-fails mid-cutover; if
    ``dhclient`` is absent the re-lease cannot run at all. Both are probed over the
    EXISTING SSH (``sudo -n true`` + ``command -v dhclient``) BEFORE any mutation and
    consulted through the pure :func:`flip.evaluate_box_preflight`, so a misconfigured
    box refuses up front with a clear message rather than stranding the WAN.

    Fail-closed: an unresolved box host / missing SSH key, or an unreachable box, reads
    as not-ready (the probe returns ``(False, False)``). The box host + user + key are
    resolved the SAME way the runner's box ops resolve them (:func:`_firewalla_host`,
    :func:`intents._armor_firewalla_user`, :func:`_firewalla_key_path`), so the gate
    checks the box the cutover will actually drive.
    """
    host = _firewalla_host()
    key_path = _firewalla_key_path()
    key = str(key_path) if key_path.exists() else None
    if host is None or key is None:
        return flip.evaluate_box_preflight(passwordless_sudo=False, dhclient_present=False)
    sudo_ok, dhclient_ok = system.firewalla_box_preflight(
        host, key, user=intents._armor_firewalla_user()
    )
    return flip.evaluate_box_preflight(
        passwordless_sudo=sudo_ok, dhclient_present=dhclient_ok
    )


def _tcp_reachable(host: str, port: int) -> bool:
    """A read-only TCP presence probe (mirrors :func:`_firewalla_present`)."""
    try:
        socket.create_connection((host, port), timeout=2).close()
        return True
    except OSError:
        return False


def _out_of_band_reachable() -> bool:
    """Is the cutover's recovery path reachable right now? Fail-closed, Tailscale-first.

    The flip's start precondition (:func:`flip.gate_ok`): the cutover briefly drops
    the WAN, so the recovery path must exist before we touch anything. After 06-26,
    the recovery path is checked in THREE layers, ALL of which must hold:

    * **PRIMARY — the LAN-INDEPENDENT Tailscale-on-box channel** (FIX-3): a real
      root-SSH round-trip over the tailnet to ``ts-firewalla`` (see
      :func:`sanctum_cli.devices.interlock.tailscale_oob_live`). This is the ONLY
      channel proven to survive a LAN collapse — on 06-26 the LAN-bound channels
      below were up at gate-check time and then died WITH the LAN, so a gate that
      trusted only them green-lit a cutover it could not recover. The tailnet path
      is the safety net that makes the cutover survivable, so it is checked FIRST.
    * the **Mini jump host** (:func:`_out_of_band_host`, config-first from
      ``devices.mini.host`` → the ``_OUT_OF_BAND_HOST`` LAN default) — the
      out-of-band link the operator recovers the box over; and
    * the **Firewalla** (:func:`_firewalla_recovery_host`, the default gateway) —
      the host that actually PERFORMS the recovery re-lease (the rollback's
      ``dhcp_release`` SSHes it). Retained as secondary belt-and-suspenders.

    Any layer failing — the tailnet round-trip not live, either LAN host
    unreachable, or no Firewalla recovery host resolving — means no usable recovery
    path, and the gate refuses (fail-closed). ``--force`` waives the human confirm
    prompt but NEVER this probe. The moment-of-op interlock at the DMZ-engage seam
    re-runs this same check (passed as ``oob_probe``) so a channel that dies between
    preflight and engage is caught at the instant it matters.
    """
    # PRIMARY: the LAN-independent Tailscale-on-box channel (the only one that
    # survives a LAN collapse). Fail-closed if the root-SSH round-trip is not live.
    if not interlock.tailscale_oob_live():
        return False
    if not _tcp_reachable(_out_of_band_host(), _OUT_OF_BAND_PORT):
        return False
    fw_host = _firewalla_recovery_host()
    if fw_host is None:
        # No recovery host resolved → no host the re-lease can reach → no recovery
        # path. Fail-closed rather than guess the Firewalla is reachable.
        return False
    return _tcp_reachable(fw_host, _FIREWALLA_SSH_PORT)


def _print_dmz_stage_plan(plan: list[str]) -> None:
    """Render the staged cutover plan through the onboarding experience helpers.

    A "Step N of M" chapter banner frames the cutover, then each flip stage is
    listed as a green-checked line (the same calm, confident framing the onboarding
    arc uses). Pure presentation; the orchestrator decided the stages.
    """
    console.print(
        chapter_banner(
            1,
            1,
            "Single-NAT cutover (Bell Advanced DMZ + /32)",
            "Put your network behind ONE NAT — guarded, reversible, attended-only.",
        )
    )
    # The first plan line is the title; the rest are the ordered stages/notes.
    for line in plan[1:]:
        stripped = line.strip()
        if stripped.lower().startswith(("note:", "on ")):
            console.print(f"  [dim]{escape(stripped)}[/]")
        else:
            console.print(green_check(stripped))


def _dmz_stage_verifiers(runner: Runner) -> dict[str, Callable[[], bool]]:
    """The REAL per-stage probes the apply path gates each mutating stage on.

    Honest-verify: a stage's ``✓`` MUST derive from a real-world outcome, never
    from :func:`intents._default_stage_verifier`'s unconditional ``True``. Two
    stages carry a probe that can fail-closed the moment the downstream WAN is bad:

    * ``observe_lease`` — read the REAL downstream WAN lease via the runner
      (``("lease_observe",)`` over the Firewalla SSH seam) and REJECT an
      APIPA (169.254.x) / empty / double-NAT lease. This trips the instant the
      router fails to pull a public lease — *before* the hub is left engaged in
      Advanced DMZ on a dead WAN.
    * ``verify`` — the terminal end-to-end check: the SAME
      :func:`sanctum_cli.net.verify.verify` real-site probe net_optimize and the
      old single_nat used. ONLY a single-NAT :class:`Verdict.VERIFIED` passes;
      APIPA / still-double-NAT / inconclusive all fail the stage so
      ``guarded_apply`` unwinds (disable DMZ → re-lease DHCP) and the command
      exits non-zero.

    The remaining stages (preflight/wan_dhcp/enable_dmz/hub_reboot/apply_armor/
    arm) have no extra real-world readback of their own — their I/O already
    fail-closes (a refused ``set``/``reboot``/armor-install returns ``ok=False``
    or raises, which the orchestrator turns into a stage failure) — so they are
    left to fall through; the two probes above are the gates that catch a WAN
    that came up dead.
    """

    def _observe_lease_ok() -> bool:
        # Read the REAL downstream WAN the router just leased. A self-assigned
        # APIPA (169.254.x), an empty/no lease, or a still-private (double-NAT)
        # address means the cutover did NOT land a public single-NAT WAN — fail
        # the stage so the rails unwind before DMZ is left engaged on a dead WAN.
        lease = runner(("lease_observe",)).strip() or None
        if lease is None or detect.is_apipa(lease):
            return False
        # classify_nat off the lease alone (no hop2): a private address is
        # double-NAT, a public one is the single-NAT win. Only SINGLE passes.
        return detect.classify_nat(hop2=None, wan_ip=lease) is Nat.SINGLE

    def _verify_ok() -> bool:
        # The terminal honest-verify: only a confirmed single-NAT public WAN path
        # commits. APIPA_ROLLBACK / NOT_YET (still double) / INCONCLUSIVE all fail.
        return verify.verify(runner=runner)[0] is Verdict.VERIFIED

    return {
        "observe_lease": _observe_lease_ok,
        "verify": _verify_ok,
    }


def _net_single_nat_check() -> None:
    """Phase-2 read-only validation of every live seam — ZERO writes.

    The ``--apply`` path gates the out-of-band probe, the box preflight, and the WAN
    reads behind ``net.single_nat_live``; ``--check`` runs exactly those probes (plus
    the hub connect + the CURRENT DMZ leaf value) WITHOUT engaging anything, so the
    real-hardware seams can be validated remotely and safely before a live fire is
    ever armed. Every probe failure is reported as a seam gap, never a traceback.
    """
    seams: list[tuple[bool, str]] = []

    try:
        oob = _out_of_band_reachable()
    except Exception as exc:  # a probe crash is a failed seam, not a stack trace
        seams.append((False, f"out-of-band recovery probe RAISED: {exc}"))
    else:
        seams.append((oob, f"out-of-band recovery reachable = {oob}"))

    try:
        pf = _box_preflight_ready()
        seams.append((pf.ok, f"box preflight = {pf.ok} \u00b7 {pf.reason}"))
    except Exception as exc:
        seams.append((False, f"box preflight RAISED: {exc}"))

    try:
        runner = _build_runner()
        wan_ip = runner(("fw_wan_ip",))
        wan_cidr = runner(("wan_addr_cidr",)).strip()
        wan_routes = runner(("wan_routes",)).strip()
    except Exception as exc:
        seams.append((False, f"WAN read RAISED: {exc}"))
    else:
        seams.append((True, f"WAN ip={wan_ip!r} classifier={flip.classify_wan_ip(wan_ip)}"))
        seams.append((True, f"  addr: {(wan_cidr[:110] or '(empty)')}"))
        seams.append((True, f"  routes(head): {(wan_routes.splitlines()[:3] or '(empty)')}"))

    try:
        with _connected_hub() as provider:
            dmz_op = provider.capability_op(Capability.DMZ)
            if dmz_op is None:
                seams.append((False, "hub connected but DMZ capability UNSUPPORTED"))
            else:
                cur = provider.get(dmz_op.path)
                seams.append((True, f"hub connected \u00b7 DMZ leaf {dmz_op.path} current={cur!r} (engage target={dmz_op.engaged!r})"))
    except Exception as exc:
        seams.append((False, f"hub connect / DMZ read RAISED: {exc}"))

    typer.echo("net single-nat --check \u00b7 READ-ONLY \u00b7 zero writes")
    for ok, msg in seams:
        typer.echo(f"  [{'OK' if ok else 'XX'}] {msg}")
    typer.echo("")
    if all(ok for ok, _ in seams):
        typer.echo("all probed seams GREEN \u2014 live path still gated behind net.single_nat_live")
    else:
        typer.echo("some seams need attention (XX above) before arming a live fire")


@net_app.command(
    "single-nat",
    help=(
        "Put your network behind a single NAT (Bell Advanced DMZ + /32). "
        "Dry-run by default; --apply fires (attended-only), --rollback undoes."
    ),
)
def net_single_nat(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Fire the cutover (attended-only; briefly drops internet)."),
    ] = False,
    rollback: Annotated[
        bool,
        typer.Option("--rollback", help="Undo a prior cutover: disable DMZ + re-lease DHCP."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Skip the confirm prompt (still honors the recovery gate)."),
    ] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="Read-only Phase-2 validation: probe every live seam (OOB, box preflight, WAN reads, hub+DMZ leaf) with ZERO writes."),
    ] = False,
) -> None:
    if apply + rollback + check > 1:
        exc = DeviceError(
            "--apply, --rollback, and --check are mutually exclusive",
            fix="run one at a time: --check to validate seams read-only, --apply to fire, --rollback to undo.",
        )
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code))

    if check:
        _net_single_nat_check()
        return

    if rollback:
        _net_single_nat_rollback(force=force)
        return

    # LIVE-FIRE GATE (Jedi council, 2026-07-18): the --apply DMZ engage is proven only
    # against fakes (Phase 1) — it has never touched the real Sagemcom hub / Firewalla
    # box. It stays INERT until an attended Phase-2 dry-run validates the hardware seams
    # (DMZ leaf value, reboot signal, route/lease parsing). Checked FIRST on --apply, so
    # a gated-off run makes ZERO probes and ZERO writes.
    if apply and not _single_nat_live_armed():
        exc = DeviceError(
            "live single-NAT cutover is gated OFF (Phase-2 hardware validation pending)",
            fix=(
                "the staged cutover passes its full unit suite but has never run against "
                "the real hub/box. After an attended Phase-2 dry-run validates it live, "
                "arm it with `net.single_nat_live: true` in ~/.sanctum instance.yaml."
            ),
        )
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code))

    # FIX-e: resolve whether THIS network's single-NAT playbook needs the Bell /32
    # armor (and the /32 poison gate). Bell's Advanced DMZ does; other ISPs' passthrough
    # yields a normal public lease, so the armor stages are skipped and any public
    # prefix is accepted. Default is the bell playbook (safe).
    requires_armor = _single_nat_requires_armor()

    # FIX-f: the pre-apply box precondition — the cutover's box ops run `sudo dhclient`
    # over the existing SSH, so passwordless sudo + a real DHCP client MUST be present
    # before we touch anything. Fail-closed with a clear message; zero writes (the gate
    # runs before the hub is even connected).
    if apply:
        preflight = _box_preflight_ready()
        if not preflight.ok:
            exc = DeviceError(
                preflight.reason,
                fix=(
                    "on the box (the Firewalla SSH user): grant passwordless sudo "
                    "(a sudoers NOPASSWD rule) AND install a DHCP client (`dhclient`), "
                    "then re-run with --apply."
                ),
            )
            _report(exc)
            raise typer.Exit(code=int(exc.exit_code))

    # The Firewalla-key-bound runner (the same one net_optimize/net_check use) is
    # what resolves the ("fw_wan_ip",)/("lease_observe",)/("dhcp_release",) tags
    # over the SSH seam; the orchestrator drives the Firewalla through it.
    runner = _build_runner()
    # The out-of-band recovery gate is checked here (CLI layer) so the orchestrator
    # gets a concrete bool; --force NEVER waives it (it only waives the confirm).
    oob = _out_of_band_reachable() if apply else True

    try:
        with _connected_hub() as provider:
            result = intents.single_nat_dmz(
                provider,
                runner,
                # The armor installer is only needed when the playbook requires the
                # /32 armor; for a non-Bell ISP the armor stages are skipped, so pass
                # None (the orchestrator never reaches the install).
                _build_armor_installer() if (apply and requires_armor) else None,
                apply=apply,
                out_of_band_reachable=oob,
                # FIX-3: the live, moment-of-op recovery re-probe the prevent-interlock
                # consults at the DMZ-engage seam — the SAME recovery check (Tailscale
                # round-trip first), re-sampled at the instant DMZ is engaged so a
                # channel that died since preflight refuses the engage (zero DMZ writes).
                oob_probe=_out_of_band_reachable if apply else None,
                # Wire the REAL per-stage probes (honest-verify): the ``verify``
                # stage runs the SAME ``net.verify.verify`` real-world probe that
                # net_optimize/the old single_nat used — only a single-NAT
                # ``Verdict.VERIFIED`` commits; APIPA/double-NAT/inconclusive fail
                # the stage so the rails unwind. ``observe_lease`` rejects an
                # APIPA/empty downstream lease the instant it is read (before the
                # hub is left in DMZ on a dead WAN). Without these the stages would
                # fall through to ``intents._default_stage_verifier`` (unconditional
                # True) and a dead-WAN cutover would COMMIT — fail-to-DARK.
                stage_verifiers=_dmz_stage_verifiers(runner) if apply else None,
                force=force,
                # FIX-e: gate the /32-armor + poison-/32 requirement on the playbook.
                requires_slash32_armor=requires_armor,
                confirm=lambda plan: typer.confirm(f"{plan}\nAre you at the box (not remote)?"),
            )
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc

    _print_dmz_stage_plan(result.plan)
    if not result.applied:
        # Either a dry-run OR the out-of-band gate refused (result carries ok=False).
        if result.result is not None and not result.result.ok:
            console.print(f"\n[red]✗[/] {escape(result.result.detail)}")
            raise typer.Exit(code=1)
        console.print("\n[dim]dry-run: no changes made. Re-run with --apply to fire.[/]")
        return
    assert result.result is not None  # apply path always carries an OpResult
    if result.result.ok:
        console.print(f"\n[green]✓[/] {escape(result.result.detail)}")
    else:
        console.print(f"\n[yellow]{escape(result.result.detail)}[/]")
        raise typer.Exit(code=1)


def _net_single_nat_rollback(*, force: bool) -> None:
    """Undo a prior single-NAT cutover: disable Advanced DMZ + re-lease DHCP.

    The mirror image of the apply path — it drives the SAME audited, reboot-aware,
    verified rollback the rails use on a failed stage
    (:class:`intents._DmzRollbackProvider.rollback`: restore the disengaged
    baseline → reboot to latch the disable → re-lease DHCP → verify the WAN
    recovered to a working double-NAT lease). It does NOT re-run the staged flip; it
    restores the CAPTURED pre-cutover baseline (every single-NAT leaf disengaged via
    :func:`intents.disengaged_baseline_snapshot` — bridge mode AND Advanced DMZ, not
    a fabricated single-key dict that would leave a prior bridge-mode flip silently
    engaged) and re-leases. ``--force`` waives the confirm prompt; without it the
    operator is asked first.
    """
    runner = _build_runner()
    try:
        with _connected_hub() as provider:
            op = provider.capability_op(Capability.DMZ)
            if op is None:
                exc = DeviceError(
                    f"{provider.brand} ({provider.kind}) does not support Advanced DMZ (single-NAT)",
                    fix="use a hub provider that advertises Capability.DMZ, or pin the right brand.",
                )
                _report(exc)
                raise typer.Exit(code=int(exc.exit_code))

            if not force and not typer.confirm(
                f"Disable Advanced DMZ on {provider.brand} and re-lease DHCP "
                "(returns the network to double-NAT)?"
            ):
                console.print("No changes made.")
                return

            # Restore the CAPTURED pre-cutover baseline (every single-NAT leaf
            # disengaged), then reboot → re-lease → verify recovery — through the
            # same _DmzRollbackProvider the rails use so the unwind is ONE audited,
            # reboot-aware, verified path (FIX-5 a/b/c), not a fabricated single-key
            # dict + a blind re-lease that reports green on a still-dark WAN.
            wrapped = intents._DmzRollbackProvider(provider, runner)
            snap = intents.disengaged_baseline_snapshot(provider)
            result: OpResult = wrapped.rollback(snap)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc

    if result.ok:
        console.print(f"[green]✓[/] single-NAT rolled back: {escape(result.detail)}")
    else:
        console.print(f"[yellow]{escape(result.detail)}[/]")
        raise typer.Exit(code=1)


@hub_app.command(
    "single-nat",
    help="DEPRECATED — use `sanctum net single-nat`. Prints the new command's plan.",
)
def hub_single_nat(
    apply: Annotated[  # noqa: ARG001 - retained for CLI stability; redirects to net single-nat
        bool,
        typer.Option("--apply", help="(deprecated — use `sanctum net single-nat --apply`)"),
    ] = False,
    force: Annotated[  # noqa: ARG001 - retained for CLI stability; redirects to net single-nat
        bool, typer.Option("--force", help="(deprecated — use `sanctum net single-nat --force`)")
    ] = False,
) -> None:
    """DEPRECATED single-leaf SetBridgeMode flip — redirected to ``net single-nat``.

    The single-leaf ``SetBridgeMode`` flip was superseded by the staged Advanced-DMZ
    + /32 cutover (:func:`net_single_nat`), which reboots the hub, observes the
    downstream lease, installs the self-healing armor kit, and gates on an
    out-of-band recovery path. To avoid silently firing the OLD path (and to never
    mutate on a deprecated command), this shim prints the NEW command's dry-run plan
    and a deprecation note steering the operator there; it makes ZERO device writes
    regardless of ``--apply``/``--force``.
    """
    console.print(
        "[yellow]deprecated:[/] `net hub single-nat` (single-leaf SetBridgeMode) is "
        "superseded by `sanctum net single-nat` (staged Advanced DMZ + /32 cutover)."
    )
    try:
        with _connected_hub() as provider:
            # Resolve the DMZ op for a dry-run plan via the new orchestrator; an
            # unsupported hub (no DMZ op) still gets the deprecation note + redirect
            # below (never a SetBridgeMode write). apply=False → ZERO mutations.
            result = intents.single_nat_dmz(
                provider,
                _build_runner(),
                None,
                apply=False,
            )
            _print_dmz_stage_plan(result.plan)
    except DeviceError:
        # A hub that does not advertise the new DMZ op (e.g. an older read-only
        # fallback) cannot render the staged plan — but the deprecation command's
        # job is to STEER, not to mutate, so swallow the unsupported-op refusal and
        # still print the redirect. We never reached any write (apply=False).
        console.print(
            "[dim]this hub does not advertise the new Advanced-DMZ op; "
            "run `sanctum net single-nat` for the staged plan once supported.[/]"
        )
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    console.print(
        "\n[dim]dry-run: no changes made. Run `sanctum net single-nat --apply` "
        "to fire the staged cutover.[/]"
    )


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
        f"[green]joined ✓[/] ({escape(ha_green_provider.tailnet_fqdn())})"
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
