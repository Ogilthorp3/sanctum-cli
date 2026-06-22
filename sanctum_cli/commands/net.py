from __future__ import annotations

import json
import socket
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.markup import escape

from sanctum_cli import config
from sanctum_cli.devices import firewalla as firewalla_provider
from sanctum_cli.devices import intents, rails, registry, sagemcom
from sanctum_cli.devices import orbi as orbi_provider
from sanctum_cli.devices.base import Capability, Creds, DeviceError, NetContext
from sanctum_cli.errors import SanctumError
from sanctum_cli.net import detect, playbooks, render, safety, speedtest, system, verify
from sanctum_cli.net.types import SpeedReport, Verdict

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sanctum_cli.devices.base import DeviceProvider
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

# Where the hub admin password lives (mirrors the Sagemcom provider's keychain
# tuple); used to build Creds before connect. The provider re-reads the secret
# from the Keychain itself, so a missing secret here is fine for providers that
# self-resolve credentials.
_HUB_KEYCHAIN_ACCOUNT = sagemcom.KEYCHAIN_ACCOUNT
_HUB_KEYCHAIN_SERVICE = sagemcom.KEYCHAIN_SERVICE


def _hub_netcontext() -> NetContext:
    """Build the NetContext the registry fingerprints the hub over.

    Parses the default gateway from the real ``route`` probe (read-only) and
    threads the real runner so a provider's ``detect()`` can probe without owning
    its own subprocess plumbing. Monkeypatched in tests so no shell-out occurs.
    """
    gw = detect.parse_default_gateway(system.real_runner(("route",)))
    return NetContext(gateway_ip=gw, runner=system.real_runner)


def _hub_creds(net: NetContext) -> Creds:
    """Assemble Creds for the resolved hub.

    The host is the detected gateway IP; the username is the hub admin account.
    The secret is left ``None`` on purpose — the provider reads the password from
    the Keychain at connect time (credentials never flow through the CLI layer).
    """
    return Creds(
        host=net.gateway_ip or "",
        username=_HUB_KEYCHAIN_ACCOUNT,
        secret=None,
        key_path=None,
    )


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

            def change(pv: DeviceProvider) -> None:
                # path/value are passed straight through to the provider, which owns
                # the SAH-boundary encoding (the hostile-input contract is enforced
                # at the provider's transport seam, not re-encoded here).
                pv.set(path, value)

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
# that wires the Orbi provider into a real install. There is no `sanctum net orbi`
# sub-app yet (guest-wifi / channel intents are not surfaced as commands), so this
# module-scope reference is what keeps the import from being pruned as unused;
# mirror the sagemcom/firewalla pattern above when the orbi command surface lands.
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
