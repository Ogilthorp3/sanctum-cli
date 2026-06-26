from __future__ import annotations

import json
import socket
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

from sanctum_cli import config
from sanctum_cli.devices import firewalla as firewalla_provider
from sanctum_cli.devices import intents, rails, registry, sagemcom
from sanctum_cli.devices import orbi as orbi_provider
from sanctum_cli.devices import transport as transport_router
from sanctum_cli.devices.armor import SinglenatArmorInstaller
from sanctum_cli.devices.base import (
    Capability,
    Creds,
    DeviceError,
    NetContext,
    OpResult,
)
from sanctum_cli.errors import SanctumError
from sanctum_cli.net import detect, playbooks, render, safety, speedtest, system, verify
from sanctum_cli.net.types import Nat, SpeedReport, Verdict
from sanctum_cli.onboard_experience import chapter_banner, green_check

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sanctum_cli.devices.base import DeviceProvider
    from sanctum_cli.devices.intents import ArmorInstaller
    from sanctum_cli.net.detect import HttpProbe, Runner

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


def _render_capabilities(provider: DeviceProvider) -> None:
    """Print the per-setting transport plan: live API ops + the Phase-2 ceiling.

    Builds the honest multi-transport plan (:func:`transport_router.plan_routes`)
    from the provider's OWN capability map, then renders two sections:

    * **API (live)** — each advertised capability with the concrete real op that
      backs it (driven now); and
    * the **GUI-only ceiling** — the surfaces the API cannot reach, each tagged with
      the brand's Phase-2 fallback transport (agent-browser for a web-UI box,
      android for the app-only Firewalla) and the ``Phase 2: live recipe`` marker.

    Read-only: it only reads the provider's (already honest-verified) map and
    mutates nothing. The 3 honesty defects (Orbi AP_MODE/CHANNELS, Firewalla
    WAN_MODE) surface here as ceiling rows under a GUI fallback — never as a live
    API op they lack.
    """
    plan = transport_router.plan_routes(provider)
    console.print(f"[bold]{escape(provider.kind)}:[/] {escape(plan.brand)}")
    console.print(
        f"[bold]GUI-only fallback transport:[/] {escape(plan.fallback.value)}"
    )
    live = [r for r in plan.routes if r.live]
    ceiling = [r for r in plan.routes if not r.live]
    console.print("\n[bold]API (live) — driven now:[/]")
    if live:
        for route in live:
            console.print(
                f"  [cyan]{escape(route.setting)}[/]  "
                f"[dim]{escape(route.transport.value)}[/]  {escape(route.op)}"
            )
    else:
        console.print("  [dim](none)[/]")
    console.print("\n[bold]GUI-only ceiling — Phase 2 (not yet implemented):[/]")
    if ceiling:
        for route in ceiling:
            console.print(
                f"  [yellow]{escape(route.transport.value)}[/]  "
                f"{escape(route.setting)}  [dim]({escape(route.op)})[/]"
            )
    else:
        console.print("  [dim](none — the API reaches every known surface)[/]")


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


@hub_app.command(
    "capabilities",
    help="List what can be changed on the hub, per transport (API now; GUI = Phase 2).",
)
def hub_capabilities() -> None:
    try:
        with _connected_hub() as provider:
            _render_capabilities(provider)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


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
    force: Annotated[
        bool, typer.Option("--force", help="Skip the confirmation prompt.")
    ] = False,
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


# ─── single-NAT (the net-level Advanced-DMZ cutover command) ─────────────────
#
# ``sanctum net single-nat`` is the FULL Bell Advanced-DMZ + /32 cutover surface,
# driving the staged :func:`intents.single_nat_dmz` orchestrator (the brain is the
# pure :mod:`sanctum_cli.devices.flip` machine). It supersedes the old single-leaf
# ``net hub single-nat`` (SetBridgeMode), which is now a deprecation shim that
# redirects here (see :func:`hub_single_nat`). Dry-run by default — bare invocation
# makes ZERO device writes; ``--apply`` fires the cutover ONLY when the out-of-band
# recovery probe says reachable; ``--rollback`` undoes a prior cutover (disable DMZ
# + re-lease DHCP).

# The default deploy coordinates for the armor kit, mirroring the kit README's
# deploy section + intents._DEFAULT_ARMOR_* . Held here so the CLI builds the
# installer through one seam tests can swap for a recording double; the dry-run
# never reaches it (zero host contact).
_ARMOR_KIT_DIR = "/Users/bert/Documents/Claude_Code/sanctum-singlenat-armor"
_ARMOR_FIREWALLA_HOST = "10.0.0.1"
_ARMOR_MINI_HOST = "bert@10.0.0.10"

# The out-of-band recovery host the cutover gate probes: the Mini jump host, which
# the armor kit reaches on a SEPARATE link from the WAN the flip is changing (the
# README deploys via ``bert@10.0.0.10``). A reachable Mini means there is a way to
# recover the hub if the cutover strands it; its absence makes the flip refuse.
_OUT_OF_BAND_HOST = "10.0.0.10"
_OUT_OF_BAND_PORT = 22

# The SSH port the recovery re-lease reaches the Firewalla over (the same port
# ``net.system._fw_ssh_argv`` uses for the ``dhcp_release`` op). The Firewalla host
# itself is NOT a constant — it is resolved at gate time as the default gateway
# (see :func:`_firewalla_recovery_host`), the same box ``_build_runner`` targets.
_FIREWALLA_SSH_PORT = 22


def _firewalla_recovery_host() -> str | None:
    """Resolve the Firewalla the recovery re-lease will SSH to: the default gateway.

    The unwind on a failed cutover (and an explicit ``--rollback``) fires the
    ``dhcp_release`` runner tag, which SSHes to the Firewalla — and ``_build_runner``
    resolves that box as the parsed default gateway. The gate must probe the SAME
    host so "the recovery re-lease can reach its box" is what it actually verifies.
    Returns ``None`` when no gateway parses (no recovery host → no recovery path).
    """
    gw = detect.parse_default_gateway(system.real_runner(("route",)))
    return gw or None


def _build_armor_installer() -> ArmorInstaller:
    """Build the real single-NAT armor installer from the README coordinates.

    The one seam through which the ``net single-nat`` command reaches a concrete
    armor install (stage ``apply_armor``). Built only on the apply path (never the
    dry-run / gate-refused paths, which make zero host contact); tests swap this
    for a recording double so the wiring is exercised without shelling out.
    """
    return SinglenatArmorInstaller(
        kit_dir=_ARMOR_KIT_DIR,
        firewalla_host=_ARMOR_FIREWALLA_HOST,
        mini_host=_ARMOR_MINI_HOST,
    )


def _tcp_reachable(host: str, port: int) -> bool:
    """A read-only TCP presence probe (mirrors :func:`_firewalla_present`)."""
    try:
        socket.create_connection((host, port), timeout=2).close()
        return True
    except OSError:
        return False


def _out_of_band_reachable() -> bool:
    """Is the cutover's recovery path reachable right now? Two-sided, fail-closed.

    The flip's start precondition (:func:`flip.gate_ok`): the cutover briefly drops
    the WAN, so the recovery path must exist before we touch anything. Recovery is
    TWO hosts, and BOTH must be reachable:

    * the **Mini jump host** (``_OUT_OF_BAND_HOST``) — the out-of-band link the
      operator reaches the box over if the cutover strands the hub; and
    * the **Firewalla** (:func:`_firewalla_recovery_host`, the default gateway) —
      the host that actually PERFORMS the recovery re-lease: the rollback fires the
      ``dhcp_release`` op, which SSHes to the Firewalla to release + re-acquire the
      WAN lease. A gate that probed only the Mini would green-light a cutover whose
      rollback can never re-lease the WAN (the household stranded dark on an
      un-runnable recovery) — the exact fail-to-DARK this gate exists to prevent.

    Both are probed on the SSH port the recovery transport uses. A failure to
    connect to EITHER — or a Firewalla recovery host that does not even resolve —
    means no usable recovery path, and the gate refuses (fail-closed). ``--force``
    waives the human confirm prompt but NEVER this probe.
    """
    if not _tcp_reachable(_OUT_OF_BAND_HOST, _OUT_OF_BAND_PORT):
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
) -> None:
    if apply and rollback:
        exc = DeviceError(
            "--apply and --rollback are mutually exclusive",
            fix="run one at a time: --apply to fire the cutover, --rollback to undo it.",
        )
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code))

    if rollback:
        _net_single_nat_rollback(force=force)
        return

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
                _build_armor_installer() if apply else None,
                apply=apply,
                out_of_band_reachable=oob,
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


@firewalla_app.command(
    "capabilities",
    help="List what can be changed on the box, per transport (API now; app = Phase 2).",
)
def firewalla_capabilities() -> None:
    try:
        with _connected_firewalla() as provider:
            _render_capabilities(provider)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


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

orbi_app = typer.Typer(
    help="Drive the NETGEAR Orbi mesh router through the device-provider rails."
)
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


@orbi_app.command(
    "capabilities",
    help="List what can be changed on the Orbi, per transport (API now; GUI = Phase 2).",
)
def orbi_capabilities() -> None:
    try:
        with _connected_orbi() as provider:
            _render_capabilities(provider)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


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
        typer.Option("--apply", help="Actually fire the change (guarded by snapshot→verify→rollback)."),
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
            target_value = op.engaged if desired == "on" else ("off" if op.engaged == "on" else "on")

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
                console.print(
                    "\n[dim]dry-run: no changes made. Re-run with --apply to fire.[/]"
                )
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
